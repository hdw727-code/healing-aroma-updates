"""Whole local work-data backup/restore prototype, standard library only.

Server integration MUST serialize every imports/ai_jobs/exports write with
store.lock and set a maintenance gate before restore (also wait for readers).
This module never accesses app-root local_private or any Codex/auth profile.
Existing legacy ZIP routes remain separate; dispatch by manifest format.
"""
from __future__ import annotations
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
import zipfile

from update_client import atomic_json, canonical, digest_file, read_json

FORMAT = "aroma-full-work-backup"
DATA_ROOTS = ("imports", "ai_jobs", "exports")
CONTENT_ROOT = "business_content"
CONTENT_NAMES = ("catalog.json", "files.json")
MAX_CONTENT_BYTES = 2 * 1024 * 1024
CATALOG_KEYS = frozenset({"schema_version", "content_version", "approval_status", "content_label",
    "default_prices", "default_tax_mode", "intro", "provenance_policy", "programs", "templates",
    "confirmation_policy", "updated_at", "content_revision", "notice", "note"})
FILES_KEYS = frozenset({"schema_version", "index_version", "scope", "contains_source_documents",
    "contains_private_paths", "approval_status", "files", "notice", "updated_at", "index_revision"})
UNSAFE_CONTENT_KEYS = frozenset({"__proto__", "prototype", "constructor", "$ref", "$schema", "script",
    "scripts", "command", "commands", "executable", "exec", "runtime", "code", "auth", "token",
    "api_key", "cookie", "password", "credentials", "private_key", "admin_pin", "access_token",
    "refresh_token", "client_secret", "session_token", "codex_home", "auth_json"})
MAX_BYTES = 1024 * 1024 * 1024
MAX_FILES = 10000
MAX_MANIFEST = 2 * 1024 * 1024
BACKUP_NAME = re.compile(r"aroma_full_[0-9]{8}_[0-9]{6}_[a-f0-9]{12}\.zip\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
TABLES = {"documents", "history", "requests", "settings", "imported_files", "imported_file_requests", "ai_jobs", "sqlite_sequence"}
SCHEMA_SQL = """
CREATE TABLE documents(id TEXT PRIMARY KEY,kind TEXT NOT NULL,revision INTEGER NOT NULL,form_json TEXT NOT NULL,result_json TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,content_json TEXT NOT NULL);
CREATE TABLE history(document_id TEXT NOT NULL,revision INTEGER NOT NULL,form_json TEXT NOT NULL,result_json TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,content_json TEXT NOT NULL,PRIMARY KEY(document_id,revision));
CREATE TABLE requests(request_id TEXT PRIMARY KEY,operation TEXT NOT NULL,payload_hash TEXT NOT NULL,response_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE settings(key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
CREATE TABLE imported_files(id TEXT PRIMARY KEY,original_name TEXT NOT NULL,stored_name TEXT UNIQUE NOT NULL,size_bytes INTEGER NOT NULL,sha256 TEXT NOT NULL,original_modified TEXT,imported_at TEXT NOT NULL,processing_status TEXT NOT NULL);
CREATE TABLE imported_file_requests(request_id TEXT PRIMARY KEY,file_id TEXT NOT NULL,original_name TEXT NOT NULL,size_bytes INTEGER NOT NULL,sha256 TEXT NOT NULL,FOREIGN KEY(file_id) REFERENCES imported_files(id));
CREATE TABLE ai_jobs(id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,kind TEXT NOT NULL,preview_hash TEXT NOT NULL,snapshot_json TEXT NOT NULL,state TEXT NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,message TEXT NOT NULL);
"""


def table_signature(conn, table):
    columns = [tuple(r[1:]) for r in conn.execute('PRAGMA table_xinfo("' + table + '")')]
    foreign = [tuple(r[2:]) for r in conn.execute('PRAGMA foreign_key_list("' + table + '")')]
    indexes = []
    for row in conn.execute('PRAGMA index_list("' + table + '")'):
        index_name = row[1].replace('"', '""')
        columns_index = [tuple(i[1:]) for i in conn.execute('PRAGMA index_info("' + index_name + '")')]
        indexes.append((row[2], row[3], row[4], tuple(columns_index)))
    return columns, sorted(foreign), sorted(indexes)


with contextlib.closing(sqlite3.connect(":memory:")) as _schema_reference:
    _schema_reference.executescript(SCHEMA_SQL)
    EXPECTED_SCHEMA = {name: table_signature(_schema_reference, name) for name in TABLES if name != "sqlite_sequence"}
    EXPECTED_SQL = {name: re.sub(r"\s+", "", sql).casefold().rstrip(";") for name, sql in
                    _schema_reference.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")}


class WholeBackupError(ValueError):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def error(code, message, status=400):
    raise WholeBackupError(code, message, status)


def member_name(name):
    if name == "office.sqlite3":
        return name
    if isinstance(name, str) and name in {CONTENT_ROOT + "/" + n for n in CONTENT_NAMES}:
        return name
    if not isinstance(name, str) or len(name) > 250 or "\\" in name or ":" in name:
        error("unsafe_member", "백업 파일 경로가 안전하지 않습니다.")
    parts = name.split("/")
    if (len(parts) < 2 or parts[0] not in DATA_ROOTS or any(not p or p in (".", "..") or
        p.endswith((".", " ")) or re.search(r'[\x00-\x1f<>"|?*]', p) or
        re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p) for p in parts)):
        error("unsafe_member", "업무 자료 범위 밖의 백업 항목을 거부했습니다.")
    if any(p.casefold() in ("auth", "profile", "profiles", "credentials", ".codex", "codex_home", "logs", "backups", "local_private") for p in parts):
        error("private_member", "로그인·인증·로그·이전 백업 폴더는 백업 대상이 아닙니다.")
    return name


def move_owned_directory(source, destination):
    """Windows indexers may briefly hold a just-written directory. Retry only
    this exact owned move; never overwrite an existing target or change ACLs.
    A persistent reader/permission error still triggers preserved rollback.
    """
    if destination.exists(): error("move_destination_exists", "보존 대상 폴더가 이미 있습니다.")
    for attempt in range(5):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 4 or destination.exists() or not source.exists(): raise
            time.sleep(.02 * (2 ** attempt))


class WholeBackup:
    def __init__(self, store, *, schema_version=1, max_bytes=MAX_BYTES, max_files=MAX_FILES, fault_hook=None):
        self.store = store
        self.schema_version = schema_version
        self.max_bytes, self.max_files = max_bytes, max_files
        self.fault_hook = fault_hook
        self.data = Path(store.data_dir).resolve()
        self.backups = self.data / "backups"
        self.backups.mkdir(exist_ok=True)
        if self.backups.is_symlink() or self.backups.is_junction():
            error("backup_link", "백업 폴더의 연결 경로는 사용할 수 없습니다.")
        self.content = Path(os.path.abspath(store.content_dir))

    def _content_guard(self):
        # A trusted launcher supplies this directory. No ZIP/HTTP field can
        # select it, and none of its other files are enumerated or opened.
        if Path(os.path.abspath(self.store.content_dir)) != self.content:
            error("content_binding_changed", "실행 중 프로그램 자료 경로가 바뀌어 처리를 중단했습니다.")
        forbidden = {"local_private", ".codex", "codex_profile", "codex_home", "auth",
                     "profile", "profiles", "credentials", "runtime", "templates", ".git", ".aws"}
        if any(p.casefold() in forbidden for p in self.content.parts):
            error("content_path", "프로그램 자료 폴더가 인증·코드 경로와 겹칩니다.")
        private = Path(os.path.abspath(getattr(self.store, "private_root", self.store.root / "local_private")))
        if self.content.is_relative_to(private) or private.is_relative_to(self.content):
            error("content_path", "인증 폴더와 겹치는 프로그램 자료 경로는 거부합니다.")
        for parent in (self.content, *self.content.parents):
            if parent.is_symlink() or parent.is_junction():
                error("content_link", "연결된 프로그램 자료 폴더는 백업·복구할 수 없습니다.")
        if self.content.exists() and not self.content.is_dir():
            error("content_path", "프로그램 자료 경로가 폴더가 아닙니다.")
        for root_name in (*DATA_ROOTS, "backups"):
            root = self.data / root_name
            if self.content.is_relative_to(root) or root.is_relative_to(self.content):
                error("content_path", "업무 파일·백업 경로와 프로그램 자료 경로가 겹칩니다.")

    def _content_path(self, name):
        if name not in CONTENT_NAMES:
            error("content_member", "허용된 프로그램·자료 색인 파일만 처리합니다.")
        self._content_guard()
        path = self.content / name
        if path.is_symlink() or path.is_junction() or (path.exists() and not path.is_file()):
            error("content_link", "프로그램 자료 파일의 연결 경로는 거부합니다.")
        if path.exists():
            info = path.stat()
            if info.st_nlink > 1 or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                error("content_link", "하드링크·외부 연결 표식이 있는 프로그램 자료는 거부합니다.")
            if info.st_size > MAX_CONTENT_BYTES:
                error("content_limit", "프로그램 자료 파일은 각각 2MB 이내여야 합니다.")
        return path

    @staticmethod
    def _validate_content(name, body):
        if name not in CONTENT_NAMES or len(body) > MAX_CONTENT_BYTES:
            error("content_limit", "지원하는 프로그램 자료 형식·크기가 아닙니다.")
        try:
            value = read_json(body.removeprefix(b"\xef\xbb\xbf"))
        except (ValueError, UnicodeDecodeError):
            error("content_json", "프로그램 자료 JSON을 안전하게 읽지 못했습니다.")
        list_key = "programs" if name == "catalog.json" else "files"
        if isinstance(value, dict):
            allowed = CATALOG_KEYS if name == "catalog.json" else FILES_KEYS
            if set(value) - allowed or list_key not in value:
                error("content_json", "프로그램 자료의 최상위 항목 구성이 일치하지 않습니다.")
            records = value[list_key]
            if "schema_version" in value and (type(value["schema_version"]) is not int or value["schema_version"] != 1):
                error("content_schema", "프로그램 자료 구조 버전이 일치하지 않습니다.")
        elif isinstance(value, list):
            records = value  # Existing AppStore supports legacy bare lists.
        else:
            error("content_json", "프로그램 자료는 목록 또는 JSON 객체여야 합니다.")
        limit = 100 if name == "catalog.json" else 10000
        if not isinstance(records, list) or len(records) > limit or not all(isinstance(r, dict) for r in records):
            error("content_json", "프로그램·자료 색인 목록의 형식 또는 수가 잘못되었습니다.")
        nodes = 0
        def visit(item, depth=0):
            nonlocal nodes
            nodes += 1
            if nodes > 50000 or depth > 20:
                error("content_limit", "프로그램 자료의 중첩 또는 항목 수가 너무 많습니다.")
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str) or len(key) > 200 or key.casefold() in UNSAFE_CONTENT_KEYS:
                        error("content_json", "실행·인증·외부 참조 항목이 있는 프로그램 자료는 거부합니다.")
                    visit(child, depth + 1)
            elif isinstance(item, list):
                for child in item: visit(child, depth + 1)
            elif isinstance(item, str):
                if len(item) > 16384 or re.match(r"(?i)^(?:https?://|file:|javascript:|data:|[a-z]:[\\/]|\\\\)", item.strip()):
                    error("content_json", "외부 주소·경로 또는 너무 긴 프로그램 문구는 거부합니다.")
            elif type(item) is float and (not math.isfinite(item) or abs(item) > 10**18):
                error("content_json", "프로그램 자료의 숫자가 지원 범위를 벗어났습니다.")
            elif type(item) is int and abs(item) > 10**18:
                error("content_json", "프로그램 자료의 숫자가 지원 범위를 벗어났습니다.")
            elif item is not None and type(item) not in (int, float, bool):
                error("content_json", "프로그램 자료의 값 형식이 잘못되었습니다.")
        visit(value)
        return value

    def _content_inventory(self):
        rows, total = [], 0
        self._content_guard()
        for name in CONTENT_NAMES:
            path = self._content_path(name)
            if not path.exists(): continue
            body = path.read_bytes()
            self._validate_content(name, body)
            rows.append({"path": CONTENT_ROOT + "/" + name, "size": len(body),
                         "sha256": hashlib.sha256(body).hexdigest()})
            total += len(body)
        return rows, total

    def _content_fingerprint(self):
        self._content_guard()
        return hashlib.sha256(str(self.content).casefold().encode("utf-8")).hexdigest()

    def _snapshot_current_content(self, destination):
        rows, _ = self._content_inventory()
        destination.mkdir(exist_ok=False)
        folder = destination / CONTENT_ROOT; folder.mkdir()
        for row in rows:
            name = row["path"].split("/")[1]
            body = self._content_path(name).read_bytes()
            if len(body) != row["size"] or hashlib.sha256(body).hexdigest() != row["sha256"]:
                error("content_changed", "현재 프로그램 보존 사본을 만드는 중 내용이 바뀌었습니다.")
            with (folder / name).open("xb") as out:
                out.write(body); out.flush(); os.fsync(out.fileno())
        if self._content_inventory()[0] != rows:
            error("content_changed", "현재 프로그램 보존 사본을 만드는 중 내용이 바뀌었습니다.")
        return {"format_version": 2, "members": rows}

    def _check_content_restore_space(self, manifest):
        if manifest.get("format_version") != 2: return
        self._content_guard()
        existing = next((p for p in (self.content, *self.content.parents) if p.exists()), None)
        needed = sum(r["size"] for r in manifest["members"] if r["path"].startswith(CONTENT_ROOT + "/"))
        if existing is None or shutil.disk_usage(existing).free < needed + 50 * 1024 * 1024:
            error("content_disk_space", "프로그램 자료를 복구할 대상 디스크 공간이 부족합니다.")

    def _install_content(self, source_data, manifest, preserved_dir, *, on_member=None):
        """Two allowlisted metadata files only. Preserve current bytes before
        replacement; use destination-volume exclusive temp files. Other files
        in content_dir are never read, copied, renamed or removed.
        """
        if manifest.get("format_version") != 2: return False
        self._content_guard()
        expected = {r["path"]: r for r in manifest["members"] if r["path"].startswith(CONTENT_ROOT + "/")}
        preserved_dir.mkdir(exist_ok=False)
        if expected: self.content.mkdir(parents=True, exist_ok=True)
        for name in CONTENT_NAMES:
            target = self._content_path(name)
            current_body = None
            if target.exists():
                body = target.read_bytes()
                current_body = body
                saved = preserved_dir / name
                with saved.open("xb") as out:
                    out.write(body); out.flush(); os.fsync(out.fileno())
                if saved.read_bytes() != body:
                    error("content_preservation", "프로그램 자료 보존 사본을 확인하지 못했습니다.")
            member = CONTENT_ROOT + "/" + name
            if member in expected:
                source = Path(source_data) / CONTENT_ROOT / name
                body = source.read_bytes()
                self._validate_content(name, body)
                row = expected[member]
                if len(body) != row["size"] or hashlib.sha256(body).hexdigest() != row["sha256"]:
                    error("content_hash", "프로그램 복구 사본의 해시가 일치하지 않습니다.")
                if current_body == body:
                    if on_member: on_member(name)
                    continue
                temporary = self.content / (".aroma_restore_" + uuid.uuid4().hex + ".part")
                owned = False
                try:
                    with temporary.open("xb") as out:
                        owned = True
                        out.write(body); out.flush(); os.fsync(out.fileno())
                    self._content_path(name)
                    os.replace(temporary, target)
                finally:
                    if owned: temporary.unlink(missing_ok=True)
            elif target.exists():
                # Its exact previous bytes are preserved both here and in the
                # verified safety archive before restoring snapshot absence.
                target.unlink()
            if on_member: on_member(name)
        current, _ = self._content_inventory()
        if current != [expected[CONTENT_ROOT + "/" + n] for n in CONTENT_NAMES if CONTENT_ROOT + "/" + n in expected]:
            error("content_changed", "프로그램 자료 복구 후 구성을 확인하지 못했습니다.")
        return True

    def _active_ai(self):
        # No AI manager construction, auth read or external work. Same lock as
        # AIJobManager.start makes this check atomic with snapshot and restore.
        with self.store.connect() as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_jobs'").fetchone()
            if exists and conn.execute("SELECT 1 FROM ai_jobs WHERE state IN ('queued','running','cancelling') LIMIT 1").fetchone():
                error("ai_job_busy", "진행 중인 AI 작업을 마친 뒤 백업 또는 복구해 주세요.", 409)

    def _inventory(self):
        rows, total = [], 0
        for root_name in DATA_ROOTS:
            root = self.data / root_name
            if not root.exists():
                continue
            if root.is_symlink() or root.is_junction() or not root.is_dir():
                error("source_link", "업무 자료 폴더의 연결 경로는 수동 확인이 필요합니다.")
            for directory, dirs, files in os.walk(root, followlinks=False):
                base = Path(directory)
                for name in dirs + files:
                    item = base / name
                    if item.is_symlink() or item.is_junction():
                        error("source_link", "업무 자료의 연결 경로는 백업할 수 없습니다.")
                for name in files:
                    source = base / name
                    relative = member_name(source.relative_to(self.data).as_posix())
                    size = source.stat().st_size
                    total += size
                    if total > self.max_bytes or len(rows) + 1 > self.max_files:
                        error("backup_limit", "전체 백업의 시험 용량 또는 파일 수 제한을 넘었습니다.")
                    rows.append({"path": relative, "size": size, "sha256": digest_file(source)})
        return sorted(rows, key=lambda r: r["path"]), total

    def _snapshot_db(self, destination, *, exclude_pin=True):
        with self.store.connect() as src, contextlib.closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
            if exclude_pin:
                dst.execute("PRAGMA secure_delete=ON")
                dst.execute("DELETE FROM settings WHERE key='admin_pin'")
                dst.commit()
                # Row absence alone is insufficient: free cells can retain a
                # password hash. Compact the portable DB after secure deletion.
                dst.execute("VACUUM")
            self._check_db(dst, require_no_pin=exclude_pin)

    def _check_db(self, conn, *, require_no_pin=False):
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            error("database_integrity", "백업 데이터베이스의 무결성을 확인하지 못했습니다.")
        if conn.execute("PRAGMA user_version").fetchone()[0] != self.schema_version:
            error("schema_mismatch", "앱 자료 구조가 다른 백업입니다.")
        rows = conn.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        tables = {r[1] for r in rows if r[0] == "table"}
        if not {"documents", "history", "requests", "settings"}.issubset(tables) or not tables.issubset(TABLES):
            error("database_schema", "지원하는 업무 데이터베이스가 아닙니다.")
        if any(row[0] in ("trigger", "view") for row in rows):
            error("database_schema", "실행 가능한 데이터베이스 구성을 거부했습니다.")
        if ("imported_files" in tables) != ("imported_file_requests" in tables):
            error("database_schema", "자료 색인 테이블 구성이 일치하지 않습니다.")
        for table in tables:
            if table not in EXPECTED_SCHEMA or table_signature(conn, table) != EXPECTED_SCHEMA[table]:
                error("database_schema", "업무 데이터베이스 열·키·색인 구조가 일치하지 않습니다.")
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
            if re.sub(r"\s+", "", sql).casefold().rstrip(";") != EXPECTED_SQL[table]:
                error("database_schema", "지원하지 않는 CHECK·정렬·제약 구성이 포함된 데이터베이스입니다.")
            json_columns = [r[1] for r in conn.execute('PRAGMA table_info("' + table + '")') if r[1].endswith("_json")]
            if json_columns:
                count = 0
                for record in conn.execute('SELECT ' + ','.join('"' + c + '"' for c in json_columns) + ' FROM "' + table + '"'):
                    count += 1
                    if count > 200000: error("database_records", "백업 기록 수를 수동 확인해야 합니다.")
                    for value in record:
                        if not isinstance(value, str) or len(value.encode("utf8")) > 4 * 1024 * 1024:
                            error("database_json", "업무 기록 JSON 형식을 확인하지 못했습니다.")
                        try: parsed = read_json(value.encode("utf8"))
                        except ValueError: error("database_json", "업무 기록 JSON을 읽지 못했습니다.")
                        if table != "settings" and not isinstance(parsed, dict):
                            error("database_json", "업무 기록 JSON 구성이 잘못되었습니다.")
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            error("database_integrity", "백업 데이터 연결을 확인하지 못했습니다.")
        if require_no_pin and conn.execute("SELECT 1 FROM settings WHERE key='admin_pin'").fetchone():
            error("pin_in_backup", "관리자 PIN이 포함된 백업을 거부했습니다.")

    def create(self):
        with self.store.lock:
            if self.pending_recovery():
                error("recovery_pending", "중단된 자료 복구를 확인한 뒤 새 백업을 만들어 주세요.", 409)
            self._active_ai()
            return self._create_locked()

    def _create_locked(self):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"aroma_full_{stamp}_{uuid.uuid4().hex[:12]}.zip"
        target = self.backups / name
        owned = False
        with tempfile.TemporaryDirectory(prefix="aroma-whole-backup-") as scratch_name:
            scratch = Path(scratch_name)
            database = scratch / "office.sqlite3"
            self._snapshot_db(database)
            rows, total = self._inventory()
            content_rows, content_bytes = self._content_inventory()
            total += content_bytes
            db_size = database.stat().st_size
            if total + db_size > self.max_bytes:
                error("backup_limit", "백업 용량 제한을 넘었습니다.")
            all_rows = [{"path": "office.sqlite3", "size": db_size, "sha256": digest_file(database)}] + rows + content_rows
            manifest = {"format": FORMAT, "format_version": 2, "schema_version": self.schema_version,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "members": all_rows,
                "content_snapshot": {"names": list(CONTENT_NAMES), "present": [r["path"] for r in content_rows]},
                "scope": {"database": True, "imports": True, "ai_jobs": True, "exports": True,
                    "admin_pin": False, "auth_profile": False, "logs": False, "prior_backups": False,
                    "usb_originals": False, "local_program_content": True, "local_file_metadata": True,
                    "content_templates_files": False, "runtime_or_code": False}}
            metadata = canonical(manifest)
            if len(metadata) > MAX_MANIFEST:
                error("manifest_limit", "백업 목록 크기가 제한을 넘었습니다.")
            if shutil.disk_usage(self.backups).free < total + db_size + 50 * 1024 * 1024:
                error("disk_space", "백업을 만들 디스크 공간이 부족합니다.")
            try:
                with target.open("xb") as output:
                    owned = True
                    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                        archive.write(database, "office.sqlite3")
                        for row in rows:
                            archive.write(self.data.joinpath(*row["path"].split("/")), row["path"])
                        for row in content_rows:
                            archive.write(self._content_path(row["path"].split("/")[1]), row["path"])
                        archive.writestr("manifest.json", metadata)
                    output.flush(); os.fsync(output.fileno())
                if self._inventory()[0] != rows:
                    error("source_changed", "파일이 변경되어 백업을 중단했습니다. 다시 시도해 주세요.")
                if self._content_inventory()[0] != content_rows:
                    error("source_changed", "프로그램 자료가 변경되어 백업을 중단했습니다. 다시 시도해 주세요.")
                # Validate actual archived bytes, not just source hashes.
                with tempfile.TemporaryDirectory(prefix="aroma-backup-verify-") as verify_name:
                    self.validate(target, Path(verify_name) / "data")
                return {"name": name, "path": str(target), "size_bytes": target.stat().st_size,
                    "sha256": digest_file(target), "format": FORMAT, "raw_files_included": True,
                    "ai_result_files_included": True, "admin_pin_included": False,
                    "format_version": 2, "local_program_content_included": True, "local_file_metadata_included": True,
                    "file_count": len(all_rows), "uncompressed_bytes": total + db_size, "scope": manifest["scope"]}
            except Exception:
                if owned:
                    target.unlink(missing_ok=True)
                raise

    def list(self):
        result = []
        for path in sorted(self.backups.glob("aroma_full_*.zip"), reverse=True):
            if not BACKUP_NAME.fullmatch(path.name) or not path.is_file() or path.is_symlink(): continue
            version = None
            try:
                with zipfile.ZipFile(path) as archive:
                    info = archive.getinfo("manifest.json")
                    if info.file_size <= MAX_MANIFEST:
                        value = read_json(archive.read(info))
                        if isinstance(value, dict) and value.get("format") == FORMAT and type(value.get("format_version")) is int:
                            version = value["format_version"]
            except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                pass
            result.append({"name": path.name, "size_bytes": path.stat().st_size, "format": FORMAT,
                "raw_files_included": True, "format_version": version,
                "local_program_content_included": version == 2, "local_file_metadata_included": version == 2,
                "content_scope": "current_catalog_and_files_metadata" if version == 2 else "existing_content_preserved_on_restore"})
        return result

    def validate(self, archive_path, destination):
        destination = Path(destination)
        if destination.exists():
            error("restore_stage_exists", "복구 시험 폴더가 이미 있습니다.")
        archive_path = Path(archive_path)
        if archive_path.is_symlink() or archive_path.stat().st_size > self.max_bytes + MAX_MANIFEST:
            error("archive_limit", "백업 파일 크기 또는 경로를 확인해 주세요.")
        try:
            with zipfile.ZipFile(archive_path) as archive:
                infos = archive.infolist()
                names = [i.filename for i in infos]
                if len(infos) > self.max_files + 4 or len(names) != len(set(n.casefold() for n in names)) or "manifest.json" not in names:
                    error("archive_members", "백업 항목 수 또는 중복 경로가 잘못되었습니다.")
                mi = archive.getinfo("manifest.json")
                if mi.file_size > MAX_MANIFEST:
                    error("manifest_limit", "백업 목록이 너무 큽니다.")
                manifest = read_json(archive.read(mi))
                if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT or
                    type(manifest.get("format_version")) is not int or manifest["format_version"] not in (1, 2) or
                    type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != self.schema_version or not isinstance(manifest.get("members"), list)):
                    error("backup_format", "이 앱의 전체 백업 형식이 아닙니다.")
                version = manifest["format_version"]
                content_snapshot = manifest.get("content_snapshot")
                if version == 2 and (not isinstance(content_snapshot, dict) or set(content_snapshot) != {"names", "present"} or
                    content_snapshot["names"] != list(CONTENT_NAMES) or not isinstance(content_snapshot["present"], list)):
                    error("content_manifest", "프로그램 자료 백업의 목록 구성이 잘못되었습니다.")
                expected, total = {}, 0
                for row in manifest["members"]:
                    if (not isinstance(row, dict) or set(row) != {"path", "size", "sha256"} or
                        type(row["size"]) is not int or row["size"] < 0 or not isinstance(row["sha256"], str) or not HASH.fullmatch(row["sha256"])):
                        error("manifest_member", "백업 목록 항목이 잘못되었습니다.")
                    name = member_name(row["path"])
                    if name.startswith(CONTENT_ROOT + "/") and version != 2:
                        error("content_manifest", "이전 백업 형식에는 프로그램 자료 항목을 추가할 수 없습니다.")
                    if name.startswith(CONTENT_ROOT + "/") and row["size"] > MAX_CONTENT_BYTES:
                        error("content_limit", "프로그램 자료 파일은 각각 2MB 이내여야 합니다.")
                    if name.casefold() in {n.casefold() for n in expected}:
                        error("archive_members", "중복된 백업 경로입니다.")
                    expected[name] = row; total += row["size"]
                if ("office.sqlite3" not in expected or set(names) != set(expected) | {"manifest.json"} or
                    total > self.max_bytes or len(expected) > self.max_files + 3):
                    error("archive_members", "백업 목록과 파일 구성이 일치하지 않습니다.")
                content_present = [CONTENT_ROOT + "/" + n for n in CONTENT_NAMES if CONTENT_ROOT + "/" + n in expected]
                if version == 2 and content_snapshot["present"] != content_present:
                    error("content_manifest", "프로그램 자료 목록과 실제 항목이 일치하지 않습니다.")
                for info in infos:
                    mode = info.external_attr >> 16
                    if (info.is_dir() or stat.S_ISLNK(mode) or (mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG)) or
                        info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) or
                        info.file_size > max(info.compress_size, 1) * 2000):
                        error("archive_entry", "백업의 연결·암호화·비정상 압축 항목을 거부했습니다.")
                    if info.filename != "manifest.json" and info.file_size != expected[info.filename]["size"]:
                        error("archive_size", "백업 파일 크기가 목록과 다릅니다.")
                destination.mkdir(parents=True)
                for name, row in expected.items():
                    target = destination.joinpath(*name.split("/")); target.parent.mkdir(parents=True, exist_ok=True)
                    count, h = 0, hashlib.sha256()
                    with archive.open(name) as source, target.open("xb") as output:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            count += len(chunk)
                            if count > row["size"]: error("archive_size", "백업 압축 해제 용량이 일치하지 않습니다.")
                            h.update(chunk); output.write(chunk)
                        output.flush(); os.fsync(output.fileno())
                    if count != row["size"] or h.hexdigest() != row["sha256"]:
                        error("archive_hash", "백업 파일 해시가 일치하지 않습니다.")
                    if name.startswith(CONTENT_ROOT + "/"):
                        self._validate_content(name.split("/")[1], target.read_bytes())
                with contextlib.closing(sqlite3.connect(destination / "office.sqlite3")) as conn:
                    self._check_db(conn, require_no_pin=True)
                return manifest
        except (zipfile.BadZipFile, sqlite3.DatabaseError, OSError, KeyError, ValueError) as exc:
            if isinstance(exc, WholeBackupError): raise
            error("backup_invalid", "백업 파일을 검증하지 못했습니다.")

    def _replace_database(self, source_path, pin):
        with contextlib.closing(sqlite3.connect(source_path)) as source:
            source.execute("DELETE FROM settings WHERE key='admin_pin'")
            if pin is not None:
                source.execute("INSERT INTO settings(key,value_json) VALUES ('admin_pin',?)", (pin,))
            source.commit()
            with self.store.connect() as target:
                source.backup(target)

    def restore(self, name, *, admin_token, confirm=False):
        if confirm is not True:
            error("confirmation_required", "복구 전 직접 확인해 주세요.", 422)
        self.store.require_admin(admin_token)
        if not isinstance(name, str) or not BACKUP_NAME.fullmatch(name):
            error("backup_name", "전체 백업 목록에서 파일을 선택해 주세요.")
        archive = self.backups / name
        if not archive.is_file(): error("backup_not_found", "백업 파일을 찾지 못했습니다.", 404)
        with self.store.lock:
            self.store.require_admin(admin_token)
            if self.pending_recovery():
                error("recovery_pending", "중단된 자료 복구를 확인한 뒤 다른 백업을 복구해 주세요.", 409)
            self._active_ai()
            lock_path, lock_handle = self.backups / ".whole_restore.lock", None
            try:
                try:
                    lock_handle = lock_path.open("xb")
                except FileExistsError:
                    error("restore_busy", "진행 중이거나 중단된 복구를 먼저 확인해 주세요.", 409)
                attempt = self.backups / ("restore_preserved_" + uuid.uuid4().hex)
                attempt.mkdir()
                stage = attempt / "incoming_data"
                manifest = self.validate(archive, stage)
                self._check_content_restore_space(manifest)
                current_rows, current_bytes = self._inventory()
                current_bytes += self._content_inventory()[1]
                if shutil.disk_usage(self.data).free < current_bytes * 2 + sum(r["size"] for r in manifest["members"]) + 50 * 1024 * 1024:
                    error("disk_space", "복구 전 자료를 보존할 공간이 부족합니다.")
                safety = self._create_locked()
                rollback_data = attempt / "rollback_data"
                rollback_manifest = self.validate(Path(safety["path"]), rollback_data)
                with self.store.connect() as conn:
                    row = conn.execute("SELECT value_json FROM settings WHERE key='admin_pin'").fetchone()
                    pin = row[0] if row else None
                state = {"phase": "prepared", "safety_backup": safety["name"], "moved_roots": [], "installed_roots": [], "database_changed": False,
                         "admin_pin_preserved": True, "auth_profile_accessed": False,
                         "content_restore_planned": manifest["format_version"] == 2, "content_started": False,
                         "content_members_installed": [], "content_dir_fingerprint": self._content_fingerprint()}
                journal = attempt / "restore_state.json"; atomic_json(journal, state)
                try:
                    for root_name in DATA_ROOTS:
                        current, incoming = self.data / root_name, stage / root_name
                        if current.exists():
                            move_owned_directory(current, attempt / ("old_" + root_name))
                            state["moved_roots"].append(root_name)
                        incoming.mkdir(exist_ok=True)
                        move_owned_directory(incoming, current)
                        state["installed_roots"].append(root_name)
                        state["phase"] = "files_replaced"; atomic_json(journal, state)
                    if self.fault_hook: self.fault_hook("after_files")
                    if state["content_restore_planned"]:
                        state["content_started"] = True
                        state["phase"] = "content_replacement_started"; atomic_json(journal, state)
                        def content_progress(name):
                            state["content_members_installed"].append(name)
                            state["phase"] = "content_replaced"; atomic_json(journal, state)
                            if self.fault_hook: self.fault_hook("after_content_" + name.split(".")[0])
                        self._install_content(stage, manifest, attempt / "previous_content", on_member=content_progress)
                        if self.fault_hook: self.fault_hook("after_content")
                    self._replace_database(stage / "office.sqlite3", pin)
                    state["database_changed"] = True
                    state["phase"] = "database_replaced"; atomic_json(journal, state)
                    if self.fault_hook: self.fault_hook("after_database")
                    with self.store.connect() as conn:
                        self._check_db(conn)
                        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_jobs'").fetchone():
                            conn.execute("UPDATE ai_jobs SET state='interrupted' WHERE state IN ('queued','running','cancelling')")
                    self.store.admin_sessions.clear()
                    state["phase"] = "restored"; atomic_json(journal, state)
                    return {"restored": True, "format": FORMAT, "safety_backup": safety["name"],
                        "preserved_folder": str(attempt), "raw_files_restored": True, "ai_result_files_restored": True,
                        "admin_pin_preserved": True, "original_usb_changed": False,
                        "content_changed": state["content_restore_planned"],
                        "local_program_content_restored": state["content_restore_planned"],
                        "local_file_metadata_restored": state["content_restore_planned"],
                        "existing_content_preserved": not state["content_restore_planned"],
                        "auth_profile_accessed": False}
                except Exception as exc:
                    # Reapply DB even when replacement threw before the flag;
                    # transaction failures must not be assumed non-mutating.
                    rollback_errors = []
                    try: self._replace_database(rollback_data / "office.sqlite3", pin)
                    except Exception: rollback_errors.append("database")
                    for root_name in DATA_ROOTS:
                        try:
                            current = self.data / root_name
                            old = attempt / ("old_" + root_name)
                            if root_name in state["moved_roots"]:
                                if current.exists(): move_owned_directory(current, attempt / ("failed_" + root_name))
                                move_owned_directory(old, current)
                            elif root_name in state["installed_roots"]:
                                if current.exists(): move_owned_directory(current, attempt / ("failed_" + root_name))
                            # Roots not touched before the failure remain in
                            # place. A later rename failure must not turn those
                            # untouched original roots into a rollback error.
                        except Exception: rollback_errors.append(root_name)
                    if state["content_started"]:
                        try:
                            self._install_content(rollback_data, rollback_manifest, attempt / "failed_content_preserved")
                        except Exception: rollback_errors.append("business_content")
                    state["phase"] = "rollback_failed" if rollback_errors else "rolled_back"
                    state["rollback_incomplete_domains"] = rollback_errors; atomic_json(journal, state)
                    if rollback_errors:
                        error("rollback_failed", "자동 복구를 확인하지 못했습니다. 보존 폴더와 안전 백업을 유지했습니다.", 500)
                    if isinstance(exc, WholeBackupError): raise
                    error("restore_failed_rolled_back", "복구가 실패하여 복구 전 자료로 돌아갔습니다. 모든 사본을 보존했습니다.", 500)
            finally:
                if lock_handle is not None:
                    lock_handle.close(); lock_path.unlink(missing_ok=True)

    def pending_recovery(self):
        pending = []
        # An unknown/stale owner can exist before any journal is written.
        # Block ordinary writers until the trusted supervisor establishes that
        # this owner ended. Never delete this lock based on guessed PID age.
        if (self.backups / ".whole_restore.lock").exists():
            pending.append({"attempt_id": None, "phase": "unknown_restore_lock", "manual_recovery_required": True,
                            "trusted_owner_check_required": True})
        for folder in self.backups.glob("restore_preserved_*"):
            if not re.fullmatch(r"restore_preserved_[a-f0-9]{32}", folder.name):
                continue
            if folder.is_symlink() or folder.is_junction():
                pending.append({"attempt_id": folder.name, "phase": "unsafe_restore_folder", "manual_recovery_required": True})
                continue
            journal = folder / "restore_state.json"
            if journal.is_symlink() or journal.is_junction() or (journal.exists() and not journal.is_file()):
                pending.append({"attempt_id": folder.name, "phase": "unsafe_or_invalid_journal", "manual_recovery_required": True})
                continue
            if journal.is_file():
                try: state = self._read_journal(journal)
                except (WholeBackupError, ValueError):
                    pending.append({"attempt_id": folder.name, "phase": "unsafe_or_invalid_journal", "manual_recovery_required": True})
                    continue
                if state.get("phase") not in ("restored", "rolled_back", "recovered"):
                    pending.append({"attempt_id": folder.name, "phase": state.get("phase"), "manual_recovery_required": True})
        return pending

    def _read_journal(self, path):
        if (path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(self.backups) or
                path.stat().st_size > 65536):
            error("unsafe_journal", "복구 기록 경로 또는 크기를 수동 확인해야 합니다.")
        info = path.stat()
        if info.st_nlink > 1 or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            error("unsafe_journal", "하드링크·외부 연결 표식이 있는 복구 기록은 거부합니다.")
        state = read_json(path.read_bytes())
        if not isinstance(state, dict): error("recovery_state", "복구 기록 형식이 잘못되었습니다.")
        return state

    def recover(self, attempt_id, *, admin_token, confirm=False):
        """Explicit recovery after the trusted supervisor confirmed the old
        owner ended and cleared its stale lock. Never deletes an unknown lock.
        Gate all normal writes while pending_recovery is nonempty.
        """
        if confirm is not True: error("confirmation_required", "중단 복구를 직접 확인해 주세요.", 422)
        self.store.require_admin(admin_token)
        if not isinstance(attempt_id, str) or not re.fullmatch(r"restore_preserved_[a-f0-9]{32}", attempt_id):
            error("recovery_name", "중단 복구 목록에서 선택해 주세요.")
        attempt = self.backups / attempt_id
        if attempt.is_symlink() or attempt.is_junction(): error("source_link", "연결된 복구 경로는 거부합니다.")
        with self.store.lock:
            self.store.require_admin(admin_token); self._active_ai()
            lock_path, handle = self.backups / ".whole_restore.lock", None
            try:
                try: handle = lock_path.open("xb")
                except FileExistsError: error("stale_lock_review", "이전 복구 실행이 종료됐는지 먼저 확인해야 합니다.", 409)
                journal = attempt / "restore_state.json"
                state = self._read_journal(journal)
                if state.get("phase") in ("restored", "rolled_back", "recovered"):
                    return {"status": "no_recovery_needed"}
                safety_name = state.get("safety_backup")
                if not isinstance(safety_name, str) or not BACKUP_NAME.fullmatch(safety_name):
                    error("recovery_state", "안전 백업 정보가 잘못되었습니다.")
                recovery_id = uuid.uuid4().hex
                recovery_data = attempt / ("recovery_data_" + recovery_id)
                recovery_manifest = self.validate(self.backups / safety_name, recovery_data)
                if state.get("content_started"):
                    if state.get("content_dir_fingerprint") != self._content_fingerprint():
                        error("content_recovery_path", "중단 당시 프로그램 자료 경로가 달라 자동 복구를 중단했습니다.")
                    if recovery_manifest.get("format_version") != 2:
                        error("content_recovery_missing", "중단 복구용 프로그램 자료 사본이 없습니다.")
                    self._check_content_restore_space(recovery_manifest)
                with self.store.connect() as conn:
                    row = conn.execute("SELECT value_json FROM settings WHERE key='admin_pin'").fetchone()
                    pin = row[0] if row else None
                # Preserve the live pre-call state as well as the original
                # safety archive. A recovery error must not create a new mixed
                # files/DB/content state. PIN stays local in this owned snapshot,
                # never in the portable ZIP or any auth/profile copy.
                live_database = attempt / ("before_recovery_" + recovery_id + ".sqlite3")
                self._snapshot_db(live_database, exclude_pin=False)
                live_content, live_content_manifest = None, None
                if state.get("content_started"):
                    live_content = attempt / ("before_recovery_content_" + recovery_id)
                    live_content_manifest = self._snapshot_current_content(live_content)
                moved, installed = [], []
                state["phase"] = "recovery_in_progress"
                state["last_recovery_id"] = recovery_id; atomic_json(journal, state)
                try:
                    for name in DATA_ROOTS:
                        current, incoming = self.data / name, recovery_data / name
                        if current.exists():
                            if current.is_symlink() or current.is_junction(): error("source_link", "현재 자료의 연결 경로는 거부합니다.")
                            move_owned_directory(current, attempt / ("recovery_preserved_" + recovery_id + "_" + name))
                            moved.append(name)
                        incoming.mkdir(exist_ok=True)
                        move_owned_directory(incoming, current); installed.append(name)
                        state["recovery_moved_roots"] = moved[:]
                        state["recovery_installed_roots"] = installed[:]; atomic_json(journal, state)
                    if self.fault_hook: self.fault_hook("recover_after_files")
                    self._replace_database(recovery_data / "office.sqlite3", pin)
                    if self.fault_hook: self.fault_hook("recover_after_database")
                    if state.get("content_started"):
                        def content_progress(name):
                            state["recovery_content_last_member"] = name; atomic_json(journal, state)
                            if self.fault_hook: self.fault_hook("recover_after_content_" + name.split(".")[0])
                        self._install_content(recovery_data, recovery_manifest,
                            attempt / ("recovery_preserved_content_" + recovery_id), on_member=content_progress)
                        if self.fault_hook: self.fault_hook("recover_after_content")
                    with self.store.connect() as conn: self._check_db(conn)
                    self.store.admin_sessions.clear()
                    state["phase"] = "recovered"; atomic_json(journal, state)
                    return {"status": "recovered", "safety_backup": safety_name, "admin_pin_preserved": True,
                        "all_failed_files_preserved": True, "original_usb_changed": False,
                        "local_program_content_restored": bool(state.get("content_started")),
                        "local_file_metadata_restored": bool(state.get("content_started"))}
                except Exception as exc:
                    failures = []
                    try: self._replace_database(live_database, pin)
                    except Exception: failures.append("database")
                    for name in DATA_ROOTS:
                        try:
                            current = self.data / name
                            old = attempt / ("recovery_preserved_" + recovery_id + "_" + name)
                            if name in moved:
                                if current.exists():
                                    move_owned_directory(current, attempt / ("failed_recovery_" + recovery_id + "_" + name))
                                move_owned_directory(old, current)
                            elif name in installed and current.exists():
                                move_owned_directory(current, attempt / ("failed_recovery_" + recovery_id + "_" + name))
                        except Exception: failures.append(name)
                    if live_content is not None:
                        try:
                            self._install_content(live_content, live_content_manifest,
                                attempt / ("failed_recovery_content_" + recovery_id))
                        except Exception: failures.append("business_content")
                    state["phase"] = "recovery_rollback_failed" if failures else "recovery_failed_rolled_back"
                    state["recovery_rollback_incomplete_domains"] = failures; atomic_json(journal, state)
                    if failures:
                        error("recovery_rollback_failed", "중단 복구의 자동 되돌리기를 확인하지 못했습니다. 모든 보존 사본을 유지했습니다.", 500)
                    if isinstance(exc, WholeBackupError): raise
                    error("recovery_failed_rolled_back", "중단 복구가 실패하여 이번 복구 시작 전 상태로 돌아갔습니다. 안전 사본과 기록을 유지했습니다.", 500)
            finally:
                if handle is not None: handle.close(); lock_path.unlink(missing_ok=True)
