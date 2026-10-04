"""Owner-local raw file copies. Standard library only; no parsing or network.

Copies are stored under user_data/imports with generated names. Original USB
paths are never received or guessed. SQLite backups include the index, not the
raw copies: the imports folder needs its own separate-device backup.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from datetime import datetime, timezone
import uuid

ALLOWED_EXTENSIONS = (".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".hwp", ".hwpx", ".txt", ".csv", ".jpg", ".jpeg", ".png", ".webp")
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TOTAL_BYTES = 500 * 1024 * 1024


class LocalImportError(Exception):
    def __init__(self, message, status=400, code="invalid_file", details=None):
        super().__init__(message)
        self.status, self.code, self.details = status, code, details


def validate_name(name):
    if not isinstance(name, str) or not name or len(name) > 255:
        raise LocalImportError("파일 이름은 255자 이내로 확인해 주세요.")
    if name != name.strip() or name.endswith(".") or re.search(r'[\\/<>:"|?*\x00-\x1f\x7f]', name):
        raise LocalImportError("파일 이름에 사용할 수 없는 문자가 있습니다. 원본은 그대로 두고 다른 파일을 선택해 주세요.")
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise LocalImportError("이 형식은 가져올 수 없습니다. PDF·한글·Office 문서·텍스트·사진을 선택해 주세요.", 415, "unsupported_file")
    # Names are display metadata only, never a filesystem destination.
    return name, suffix


def original_modified(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 100:
        raise LocalImportError("선택한 파일의 수정일 정보를 확인해 주세요.")
    try:
        if re.fullmatch(r"\d{10,13}", value):
            timestamp = int(value)
            if len(value) > 10:
                timestamp /= 1000
            parsed = datetime.fromtimestamp(timestamp, timezone.utc)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not 1900 <= parsed.year <= 2200:
            raise ValueError("date range")
        return parsed.isoformat(timespec="milliseconds")
    except (ValueError, OSError, OverflowError):
        raise LocalImportError("선택한 파일의 수정일 정보를 읽지 못했습니다. 다시 선택해 주세요.")


class ImportStore:
    def __init__(self, app_store, *, max_file_bytes=MAX_FILE_BYTES, max_total_bytes=MAX_TOTAL_BYTES):
        self.app = app_store
        self.folder = self.app.data_dir / "imports"
        self.folder.mkdir(exist_ok=True)
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.ensure()

    def ensure(self):
        # Old metadata-only backups can predate this additive feature.
        with self.app.lock, self.app.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS imported_files (
                  id TEXT PRIMARY KEY, original_name TEXT NOT NULL, stored_name TEXT UNIQUE NOT NULL,
                  size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, original_modified TEXT,
                  imported_at TEXT NOT NULL, processing_status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS imported_file_requests (
                  request_id TEXT PRIMARY KEY, file_id TEXT NOT NULL, original_name TEXT NOT NULL,
                  size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
                  FOREIGN KEY(file_id) REFERENCES imported_files(id));
            """)

    def _path(self, stored_name):
        if not isinstance(stored_name, str) or not re.fullmatch(r"[a-f0-9]{32}\.[a-z0-9]+", stored_name):
            return None
        path = self.folder / stored_name
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(self.folder.resolve()):
                return None
        except OSError:
            return None
        return path

    def _metadata(self, row):
        path = self._path(row["stored_name"])
        missing = path is None or not path.is_file()
        if not missing:
            try:
                missing = path.stat().st_size != row["size_bytes"]
            except OSError:
                missing = True
        return {"id": row["id"], "name": row["original_name"], "title": row["original_name"],
                "extension": Path(row["original_name"]).suffix.lower(), "size_bytes": row["size_bytes"],
                "sha256": row["sha256"], "modified_local": row["original_modified"],
                "original_modified": row["original_modified"], "imported_at": row["imported_at"],
                "processing_status": row["processing_status"], "text_status": "not_analyzed",
                "source_owner": "user_imported", "source_type": "local_copy", "read_only": True,
                "reference_only": True, "current_approval": False, "approval_status": "review_required",
                "missing": missing, "copy_missing": missing, "raw_backup_included": False,
                "download_url": f"/api/files/{row['id']}/download" if not missing else None,
                "note": "로컬 사본 저장 · 내용 분석 전. 원본 파일과 원본 경로는 변경하거나 추정하지 않았습니다." if not missing else "저장된 색인은 있지만 사본 파일이 없습니다. 별도 사본 백업을 복구하거나 파일을 다시 가져와 주세요."}

    def list_files(self):
        with self.app.lock:
            self.ensure()
            with self.app.connect() as conn:
                return [self._metadata(row) for row in conn.execute("SELECT * FROM imported_files ORDER BY imported_at DESC, id DESC")]

    def get_file(self, file_id):
        with self.app.lock:
            self.ensure()
            with self.app.connect() as conn:
                row = conn.execute("SELECT * FROM imported_files WHERE id=?", (file_id,)).fetchone()
                if row is None:
                    raise LocalImportError("가져온 파일 목록에서 다시 선택해 주세요.", 404, "file_not_found")
                return self._metadata(row)

    def download(self, file_id):
        with self.app.lock:
            self.ensure()
            with self.app.connect() as conn:
                row = conn.execute("SELECT * FROM imported_files WHERE id=?", (file_id,)).fetchone()
                if row is None:
                    raise LocalImportError("이 파일은 앱에 가져온 사본이 아닙니다. 원본 자료 색인에서 확인해 주세요.", 404, "file_not_found")
                metadata = self._metadata(row)
                if metadata["missing"]:
                    raise LocalImportError("파일 사본이 없습니다. 별도 사본 백업을 복구하거나 다시 가져와 주세요.", 410, "file_copy_missing", {"file": metadata})
                return self._path(row["stored_name"]), metadata

    def limits(self):
        total = 0
        # Count physical copies and leftovers, including unindexed copies after restore.
        for path in self.folder.iterdir():
            if not path.is_symlink() and path.is_file():
                total += path.stat().st_size
        return {"max_file_bytes": self.max_file_bytes, "max_total_bytes": self.max_total_bytes,
                "current_total_bytes": total, "allowed_extensions": list(ALLOWED_EXTENSIONS),
                "backup_includes_metadata": True, "backup_includes_raw_files": False,
                "raw_files_backup_folder": str(self.folder), "content_parsing": False,
                "external_upload": False}

    def _read(self, stream, size, output=None):
        digest, remaining = hashlib.sha256(), size
        try:
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    raise LocalImportError("가져오기가 중간에 끊겼습니다. 원본은 그대로 있으니 다시 선택해 주세요.", 400, "upload_incomplete")
                if len(chunk) > remaining:
                    raise LocalImportError("파일 크기 정보를 확인해 주세요.")
                remaining -= len(chunk)
                digest.update(chunk)
                if output:
                    output.write(chunk)
            return digest.hexdigest()
        except (TimeoutError, ConnectionError, OSError):
            raise LocalImportError("가져오기가 중간에 끊겼습니다. 원본은 그대로 있으니 다시 선택해 주세요.", 408, "upload_incomplete")

    def import_stream(self, stream, size, name, request_id, modified=None):
        name, extension = validate_name(name)
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise LocalImportError("빈 파일은 가져오지 않았습니다. 내용이 있는 파일을 선택해 주세요.")
        if size > self.max_file_bytes:
            raise LocalImportError("파일 하나는 25MB 이내로 가져와 주세요. 큰 파일의 원본은 그대로 두세요.", 413, "file_too_large")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", request_id):
            raise LocalImportError("가져오기 요청 번호를 확인해 주세요. 파일을 다시 선택하면 됩니다.")
        modified = original_modified(modified)
        temporary = self.folder / f".upload_{uuid.uuid4().hex}.part"
        destination = None
        installed = False
        with self.app.lock:
            self.ensure()
            try:
                with self.app.connect() as conn:
                    previous = conn.execute("SELECT * FROM imported_file_requests WHERE request_id=?", (request_id,)).fetchone()
                    if previous:
                        if previous["original_name"] != name or previous["size_bytes"] != size:
                            raise LocalImportError("같은 가져오기 요청의 파일이 달라졌습니다. 새로 파일을 선택해 주세요.", 409, "request_conflict")
                        digest = self._read(stream, size)
                        if digest != previous["sha256"]:
                            raise LocalImportError("같은 가져오기 요청의 내용이 달라졌습니다. 새로 파일을 선택해 주세요.", 409, "request_conflict")
                        file = self.get_file(previous["file_id"])
                        return {"file": file, "duplicate": True, "status": "duplicate", "file_import_limits": self.limits()}
                if self.limits()["current_total_bytes"] + size > self.max_total_bytes:
                    # A duplicate needs no disk space: hash without creating a partial copy.
                    digest = self._read(stream, size)
                    with self.app.connect() as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        matching = conn.execute("SELECT * FROM imported_files WHERE original_name=? AND size_bytes=? AND sha256=?", (name, size, digest)).fetchall()
                        duplicate_row = next((row for row in matching if not self._metadata(row)["missing"]), None)
                        if duplicate_row:
                            conn.execute("INSERT INTO imported_file_requests VALUES (?,?,?,?,?)", (request_id, duplicate_row["id"], name, size, digest))
                            return {"file": self._metadata(duplicate_row), "duplicate": True, "status": "duplicate", "file_import_limits": self.limits()}
                    raise LocalImportError("앱 자료 사본의 저장 한도에 도달했습니다. 원본은 그대로 두고 아들에게 백업·정리를 요청해 주세요.", 413, "storage_limit")
                with temporary.open("xb") as output:
                    digest = self._read(stream, size, output)
                    output.flush()
                    os.fsync(output.fileno())
                with self.app.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    matching = conn.execute("SELECT * FROM imported_files WHERE original_name=? AND size_bytes=? AND sha256=? ORDER BY imported_at DESC", (name, size, digest)).fetchall()
                    duplicate_row = next((row for row in matching if not self._metadata(row)["missing"]), None)
                    if duplicate_row:
                        file_id = duplicate_row["id"]
                        conn.execute("INSERT INTO imported_file_requests VALUES (?,?,?,?,?)", (request_id, file_id, name, size, digest))
                        temporary.unlink(missing_ok=True)
                        return {"file": self._metadata(duplicate_row), "duplicate": True, "status": "duplicate", "file_import_limits": self.limits()}
                    file_id = "I" + uuid.uuid4().hex
                    stored_name = uuid.uuid4().hex + extension
                    destination = self.folder / stored_name
                    # Exclusive destination: no selected original name is ever used here.
                    if destination.exists():
                        raise LocalImportError("사본 저장 위치가 겹쳤습니다. 다시 가져와 주세요.", 409, "storage_conflict")
                    os.replace(temporary, destination)
                    installed = True
                    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    conn.execute("INSERT INTO imported_files VALUES (?,?,?,?,?,?,?,?)", (file_id, name, stored_name, size, digest, modified, timestamp, "copied_not_analyzed"))
                    conn.execute("INSERT INTO imported_file_requests VALUES (?,?,?,?,?)", (request_id, file_id, name, size, digest))
                file = self.get_file(file_id)
                return {"file": file, "duplicate": False, "status": "imported", "file_import_limits": self.limits()}
            except Exception:
                if installed and destination:
                    destination.unlink(missing_ok=True)
                raise
            finally:
                temporary.unlink(missing_ok=True)
