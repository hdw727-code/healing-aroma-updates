"""Provider-neutral update staging prototype; never imported by the main app.

No provider is configured and no production lifecycle/PIN adapter is included.
The frozen updater and its pinned key must sit outside downloaded app folders.
Downloaded code is verified and prepared, never executed by this module.
"""
from __future__ import annotations
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import threading
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import zipfile

from native_signature import CNGVerifier, SignatureError

APP_ID = "aroma-work-helper"
MAX_MANIFEST_BYTES = 512 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_EXPANDED_BYTES = 400 * 1024 * 1024
MAX_BACKUP_BYTES = 3 * 1024 * 1024 * 1024
MAX_FILES = 4096
ROOT_CODE = frozenset({"server.py", "local_import.py", "ai_jobs.py", "codex_bridge.py", "quality.py",
    "native_exports.py", "app_start.py", "native_signature.py", "START_HERE.html", "AGENTS.md",
    "START_APP.cmd", "시작하기.cmd"})
CONTENT_FILES = frozenset({"content/catalog.json", "content/files.json", "content/templates.json"})
PRIVATE_PARTS = frozenset({"user_data", "local_private", "imports", "ai_jobs", "exports", "backups",
    "credentials", "auth", ".git", ".codex", ".aws", ".env", "sdk_probe",
    "frozen_update", "startup_update_trusted"})
FORBIDDEN_SUFFIX = frozenset({".sqlite", ".sqlite3", ".db", ".pem", ".key", ".pfx", ".p12", ".hwp",
    ".hwpx", ".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".csv", ".zip"})
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
STABLE_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")


class UpdateError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def fail(code, message):
    raise UpdateError(code, message)


def version_tuple(value):
    match = STABLE_VERSION.fullmatch(value) if isinstance(value, str) and len(value) <= 64 else None
    if not match:
        fail("release_version", "A stable numeric release version is required.")
    return tuple(map(int, match.groups()))


def require_newer_version(version, current_version):
    if version_tuple(version) <= version_tuple(current_version):
        fail("release_version_not_newer", "The signed version must be newer than the active app.")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            fail("duplicate_json_key", "중복된 업데이트 항목을 거부했습니다.")
        value[key] = item
    return value


def read_json(data):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=lambda x: fail("invalid_json", "잘못된 JSON 숫자를 거부했습니다."))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateError("invalid_json", "업데이트 JSON을 읽을 수 없습니다.") from exc


def digest_file(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_path(name):
    if not isinstance(name, str) or len(name) > 240 or not name or "\\" in name or ":" in name:
        fail("unsafe_path", "배포 파일 경로가 안전하지 않습니다.")
    parts = name.split("/")
    if any(not p or p in (".", "..") or p.endswith((".", " ")) or
           re.search(r'[\x00-\x1f<>"|?*]', p) or
           re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p) for p in parts):
        fail("unsafe_path", "배포 파일 경로가 안전하지 않습니다.")
    if any(p.casefold() in PRIVATE_PARTS for p in parts) or PurePosixPath(name).suffix.casefold() in FORBIDDEN_SUFFIX:
        fail("private_payload", "개인 자료와 인증 자료는 배포할 수 없습니다.")
    return name


class PayloadPolicy:
    """Explicit publisher file allowlist plus independently pinned official runtime files.

    runtime_pins map exact runtime/python/... path to official SHA256. They cannot
    come from the downloaded manifest. This prototype ships an empty map.
    """
    def __init__(self, runtime_pins=None, code_paths=None):
        self.runtime_pins = dict(runtime_pins or {})
        self.code_paths = frozenset(code_paths or ROOT_CODE)
        for name, sha in self.runtime_pins.items():
            if not name.startswith("runtime/python/") or not HEX64.fullmatch(sha):
                fail("runtime_pin", "공식 실행환경 해시 설정이 잘못되었습니다.")
            safe_path(name)

    def allow(self, name, sha):
        safe_path(name)
        if name.startswith("runtime/"):
            if self.runtime_pins.get(name) != sha:
                fail("runtime_unpinned", "미리 확인된 공식 실행환경만 배포할 수 있습니다.")
            return
        if name in self.code_paths or name in CONTENT_FILES:
            return
        if re.fullmatch(r"public/[A-Za-z0-9_/-]+\.(?:js|css|html|png|webp|jpg|ico|svg)", name):
            return
        fail("file_not_allowed", "허용 목록 밖의 파일을 거부했습니다.")

    def check_generic_content(self, name, data):
        if name == "content/files.json":
            doc = read_json(data)
            if not isinstance(doc, dict) or doc.get("files") != []:
                fail("private_catalog", "배포 자료 색인은 빈 목록이어야 합니다.")
        if name == "content/catalog.json":
            doc = read_json(data)
            if not isinstance(doc, dict) or doc.get("programs") != []:
                fail("private_catalog", "공개 배포 프로그램 목록은 비워 두고 기존 로컬 프로그램을 계승해야 합니다.")
        if name in CONTENT_FILES:
            doc = read_json(data)
            # A semantic publisher review is still required; these are explicit
            # structural barriers, not a claim to detect every personal string.
            blocked = {"original_path", "stored_path", "customer_phone", "customer_email", "api_key",
                       "access_token", "refresh_token", "password", "private_key", "executable"}
            def walk(value):
                if isinstance(value, dict):
                    if any(str(k).casefold() in blocked for k in value):
                        fail("private_catalog", "개인 설정·연락처 항목은 배포할 수 없습니다.")
                    if value.get("source_ids") or value.get("source_facts"):
                        fail("source_catalog", "원본 자료 근거가 없는 공용 템플릿만 배포할 수 있습니다.")
                    for child in value.values():
                        walk(child)
                elif isinstance(value, list):
                    for child in value:
                        walk(child)
            walk(doc)


class ManifestVerifier:
    def __init__(self, pinned_public_key, policy=None):
        self.verifier = CNGVerifier(pinned_public_key)
        self.policy = policy or PayloadPolicy()

    def verify(self, envelope_data, *, current_sequence, current_schema, now=None, current_version=None):
        if not isinstance(envelope_data, bytes) or len(envelope_data) > MAX_MANIFEST_BYTES:
            fail("manifest_size", "배포 목록이 너무 큽니다.")
        envelope = read_json(envelope_data)
        if not isinstance(envelope, dict) or set(envelope) != {"algorithm", "payload_b64", "signature_b64"}:
            fail("manifest_envelope", "서명 목록 형식이 잘못되었습니다.")
        if envelope["algorithm"] != self.verifier.algorithm:
            fail("signature_algorithm", "허용된 서명 방식이 아닙니다.")
        try:
            payload = base64.b64decode(envelope["payload_b64"], validate=True)
            signature = base64.b64decode(envelope["signature_b64"], validate=True)
        except (TypeError, ValueError) as exc:
            raise UpdateError("signature_encoding", "서명 인코딩을 읽을 수 없습니다.") from exc
        if len(payload) > MAX_MANIFEST_BYTES or not self.verifier.verify(payload, signature):
            fail("signature_invalid", "배포자의 서명을 확인할 수 없습니다.")
        doc = read_json(payload)
        if canonical(doc) != payload:
            fail("manifest_canonical", "배포 목록 표현이 일치하지 않습니다.")
        keys = {"manifest_version", "app_id", "release_id", "version", "sequence", "published_at", "expires_at",
                "schema", "payload", "files", "entrypoint", "release_notes"}
        if (not isinstance(doc, dict) or set(doc) != keys or type(doc["manifest_version"]) is not int or
                doc["manifest_version"] != 1 or doc["app_id"] != APP_ID):
            fail("manifest_schema", "이 앱의 배포 목록이 아닙니다.")
        integer = lambda v: isinstance(v, int) and not isinstance(v, bool)
        if not integer(doc["sequence"]) or doc["sequence"] <= current_sequence:
            fail("release_replay", "이미 적용했거나 이전인 배포를 거부했습니다.")
        if not isinstance(doc["release_id"], str) or not RELEASE_ID.fullmatch(doc["release_id"]):
            fail("release_id", "배포 이름이 잘못되었습니다.")
        if not isinstance(doc["version"], str) or len(doc["version"]) > 64 or not doc["version"]:
            fail("release_version", "배포 버전이 잘못되었습니다.")
        if current_version is not None:
            require_newer_version(doc["version"], current_version)
        clock = int(time.time()) if now is None else int(now)
        if not integer(doc["published_at"]) or not integer(doc["expires_at"]) or not (
                doc["published_at"] <= clock + 300 < doc["expires_at"] + 300) or doc["expires_at"] <= clock:
            fail("release_time", "배포 목록이 만료되었거나 날짜 확인이 필요합니다.")
        if doc["expires_at"] - doc["published_at"] > 90 * 86400 or doc["expires_at"] <= doc["published_at"]:
            fail("release_time", "배포 목록의 유효 기간이 잘못되었습니다.")
        schema = doc["schema"]
        if (not isinstance(schema, dict) or set(schema) != {"min", "max", "target", "migration"} or
                not all(integer(schema[k]) for k in ("min", "max", "target")) or
                schema["migration"] is not False or schema["target"] != current_schema or
                not schema["min"] <= current_schema <= schema["max"]):
            fail("schema_migration_required", "자료 구조를 바꾸는 업데이트는 별도 이관 절차가 필요합니다.")
        payload_info = doc["payload"]
        if (not isinstance(payload_info, dict) or set(payload_info) != {"url", "sha256", "size"} or
                not isinstance(payload_info["sha256"], str) or not HEX64.fullmatch(payload_info["sha256"]) or
                not integer(payload_info["size"]) or not 0 < payload_info["size"] <= MAX_ARCHIVE_BYTES or
                not isinstance(payload_info["url"], str) or len(payload_info["url"]) > 2048):
            fail("payload_metadata", "다운로드 파일 정보가 잘못되었습니다.")
        if doc["entrypoint"] not in ("server.py", "app_start.py") or not isinstance(doc["release_notes"], str) or len(doc["release_notes"]) > 2000:
            fail("release_metadata", "시작 파일 또는 변경 안내가 잘못되었습니다.")
        files = doc["files"]
        if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
            fail("file_count", "배포 파일 수가 허용 범위를 넘었습니다.")
        seen, total = set(), 0
        for row in files:
            if (not isinstance(row, dict) or set(row) != {"path", "sha256", "size"} or
                    not isinstance(row["sha256"], str) or not HEX64.fullmatch(row["sha256"]) or
                    not integer(row["size"]) or not 0 <= row["size"] <= MAX_EXPANDED_BYTES):
                fail("file_metadata", "배포 파일 정보가 잘못되었습니다.")
            self.policy.allow(row["path"], row["sha256"])
            folded = row["path"].casefold()
            if folded in seen:
                fail("duplicate_file", "중복된 배포 경로를 거부했습니다.")
            seen.add(folded)
            total += row["size"]
        if total > MAX_EXPANDED_BYTES or doc["entrypoint"] not in seen:
            fail("expanded_size", "배포 용량 또는 시작 파일을 확인해야 합니다.")
        return {"manifest": doc, "manifest_sha256": hashlib.sha256(payload).hexdigest(),
                "envelope_sha256": hashlib.sha256(envelope_data).hexdigest(),
                "publisher_key_fingerprint": self.verifier.fingerprint}

    def extract(self, verified, archive_path, destination):
        doc = verified["manifest"]
        info = doc["payload"]
        if archive_path.stat().st_size != info["size"] or digest_file(archive_path) != info["sha256"]:
            fail("payload_hash", "다운로드 파일의 해시 또는 크기가 일치하지 않습니다.")
        if destination.exists():
            fail("destination_exists", "새 버전 폴더가 이미 있습니다.")
        expected = {f["path"]: f for f in doc["files"]}
        try:
            with zipfile.ZipFile(archive_path) as archive:
                entries = archive.infolist()
                if len(entries) != len(expected) or len(entries) > MAX_FILES:
                    fail("zip_file_set", "서명 목록과 ZIP 항목 수가 다릅니다.")
                seen = set()
                for entry in entries:
                    name = safe_path(entry.filename)
                    row = expected.get(name)
                    mode = entry.external_attr >> 16
                    if (name.casefold() in seen or row is None or entry.is_dir() or stat.S_ISLNK(mode) or
                            (mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG)) or entry.flag_bits & 1 or
                            entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) or
                            entry.file_size != row["size"] or
                            entry.file_size > max(entry.compress_size, 1) * 1000):
                        fail("zip_entry", "안전하지 않거나 서명 목록과 다른 ZIP 항목을 거부했습니다.")
                    seen.add(name.casefold())
                destination.mkdir(parents=True)
                for entry in entries:
                    row = expected[entry.filename]
                    target = destination.joinpath(*entry.filename.split("/"))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    h, count = hashlib.sha256(), 0
                    with archive.open(entry) as source, target.open("xb") as output:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            count += len(chunk)
                            if count > row["size"]:
                                fail("zip_size", "ZIP 파일 크기가 일치하지 않습니다.")
                            h.update(chunk); output.write(chunk)
                        output.flush(); os.fsync(output.fileno())
                    if count != row["size"] or h.hexdigest() != row["sha256"]:
                        fail("file_hash", "배포 파일의 해시가 일치하지 않습니다.")
                    self.policy.check_generic_content(entry.filename, target.read_bytes()) if entry.filename in CONTENT_FILES else None
        except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
            raise UpdateError("archive_failed", "배포 압축 파일을 준비할 수 없습니다.") from exc


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fail("redirect_blocked", "다운로드 주소 변경은 승인된 설정 후 지원합니다.")


class UpdateTransport:
    """Disabled until a provider, manifest URL and exact hosts are selected.

    No cookies, credentials, POST, accounts or uploads. Redirects are rejected.
    loopback_test enables only numeric 127.0.0.1 HTTP synthetic fixtures.
    """
    def __init__(self, *, enabled=False, allowed_hosts=(), loopback_test=False):
        self.enabled = enabled
        self.allowed_hosts = frozenset(allowed_hosts)
        self.loopback_test = loopback_test

    def validate_url(self, url):
        if not self.enabled or not url:
            fail("provider_not_selected", "업데이트 서버가 아직 선택되지 않았습니다.")
        parsed = urllib.parse.urlsplit(url)
        if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
            fail("url_invalid", "업데이트 주소가 잘못되었습니다.")
        if self.loopback_test and parsed.scheme == "http" and parsed.hostname == "127.0.0.1":
            return
        if (parsed.scheme != "https" or parsed.port not in (None, 443) or
                parsed.hostname not in self.allowed_hosts):
            fail("url_not_approved", "승인된 HTTPS 업데이트 주소만 사용할 수 있습니다.")

    def download(self, url, destination, *, max_bytes, expected_size=None, expected_sha=None):
        self.validate_url(url)
        if destination.exists():
            fail("download_exists", "다운로드 사본이 이미 있습니다.")
        request = urllib.request.Request(url, headers={"User-Agent": "Aroma-Update-Prototype/1", "Accept": "application/octet-stream"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        count, digest, owned_file = 0, hashlib.sha256(), False
        try:
            with opener.open(request, timeout=10) as response, destination.open("xb") as output:
                owned_file = True
                declared = response.headers.get("Content-Length")
                if declared and (not declared.isdecimal() or int(declared) > max_bytes):
                    fail("download_size", "다운로드 파일이 너무 큽니다.")
                deadline = time.monotonic() + 120
                while True:
                    if time.monotonic() > deadline:
                        fail("download_timeout", "다운로드 제한 시간을 넘었습니다.")
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > max_bytes:
                        fail("download_size", "다운로드 파일이 너무 큽니다.")
                    digest.update(chunk); output.write(chunk)
                output.flush(); os.fsync(output.fileno())
            if (expected_size is not None and count != expected_size) or (expected_sha and digest.hexdigest() != expected_sha):
                fail("download_integrity", "다운로드 파일이 끊겼거나 해시가 다릅니다.")
            return {"size": count, "sha256": digest.hexdigest()}
        except Exception:
            if owned_file:
                destination.unlink(missing_ok=True)
            raise


class UpdateClient:
    """Signed public release download without a dependency on one provider API.

    The feed URL and trusted TLS hosts are a separate manually selected setting.
    A manifest cannot add a host or replace the pinned publisher public key.
    Preparation never applies, launches, uploads, or asks for account credentials.
    """
    def __init__(self, verifier, transport=None):
        self.verifier = verifier
        self.transport = transport or UpdateTransport()

    def prepare(self, manifest_url, destination, *, current_sequence, current_schema):
        self.transport.validate_url(manifest_url)  # No directory/network mutation if unset.
        destination = Path(destination)
        if destination.exists():
            fail("download_exists", "이번 다운로드 폴더가 이미 있습니다.")
        destination.mkdir(parents=True)
        envelope_path, archive_path = destination / "manifest.json", destination / "payload.zip"
        self.transport.download(manifest_url, envelope_path, max_bytes=MAX_MANIFEST_BYTES)
        envelope = envelope_path.read_bytes()
        verified = self.verifier.verify(envelope, current_sequence=current_sequence, current_schema=current_schema)
        payload = verified["manifest"]["payload"]
        self.transport.download(payload["url"], archive_path, max_bytes=MAX_ARCHIVE_BYTES,
                                expected_size=payload["size"], expected_sha=payload["sha256"])
        return {"envelope_path": str(envelope_path), "archive_path": str(archive_path),
                "verified": verified, "applied": False, "external_upload": False}


def tree_inventory(root, *, limit=MAX_BACKUP_BYTES):
    if not root.is_dir() or root.is_symlink() or root.is_junction():
        fail("data_root", "사용자 자료 폴더를 안전하게 확인할 수 없습니다.")
    rows, total = [], 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in dirs + files:
            item = base / name
            if item.is_symlink() or item.is_junction():
                fail("data_link", "자료 폴더의 연결 경로는 수동 백업이 필요합니다.")
        for name in files:
            item = base / name
            size = item.stat().st_size
            total += size
            if total > limit:
                fail("backup_size", "전체 자료 사본 용량이 시험 허용 범위를 넘었습니다.")
            rows.append({"path": item.relative_to(root).as_posix(), "size": size, "sha256": digest_file(item)})
    return sorted(rows, key=lambda r: r["path"]), total


def full_data_snapshot(source, destination, database_name):
    """Called only after the trusted lifecycle has stopped and joined the app.

    A SQLite logical snapshot includes committed WAL transactions. All other
    files (including imports, AI files, exports, existing backups and PIN) are
    copied. Database sidecars are not replayed into the new snapshot.
    Source files are never overwritten. Race detection is an extra check, not
    a substitute for exclusive quiescence.
    """
    before, total = tree_inventory(source)
    if destination.exists():
        fail("backup_exists", "새 자료 사본 폴더가 이미 있습니다.")
    destination.mkdir(parents=True)
    db = source / database_name
    if not db.is_file() or Path(database_name).name != database_name:
        fail("database_missing", "백업할 앱 데이터베이스를 찾을 수 없습니다.")
    sidecars = {database_name, database_name + "-wal", database_name + "-shm", database_name + "-journal"}
    for row in before:
        if row["path"] in sidecars:
            continue
        target = destination.joinpath(*row["path"].split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source.joinpath(*row["path"].split("/")), target)
    # SQLite may create WAL/SHM even for a read-only WAL-mode connection. Open
    # a quiescent raw scratch copy, never the original DB, to preserve its bytes
    # and directory entries. SQLite applies committed WAL there before backup.
    with tempfile.TemporaryDirectory(prefix="aroma-update-db-") as scratch_name:
        scratch = Path(scratch_name)
        for name in sidecars:
            if (source / name).is_file():
                shutil.copy2(source / name, scratch / name)
        uri = (scratch / database_name).resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as src:
            with contextlib.closing(sqlite3.connect(destination / database_name)) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    fail("backup_integrity", "데이터베이스 사본 무결성 확인이 실패했습니다.")
    after, _ = tree_inventory(source)
    if before != after:
        fail("data_changed", "자료가 변경되어 업데이트를 중단했습니다. 앱을 완전히 멈춘 뒤 다시 시도하세요.")
    copied, _ = tree_inventory(destination)
    expected_other = [r for r in before if r["path"] not in sidecars]
    copied_other = [r for r in copied if r["path"] != database_name]
    if expected_other != copied_other:
        fail("backup_copy_mismatch", "전체 파일 사본 검증이 실패했습니다.")
    return {"source_file_count": len(before), "copied_file_count": len(copied), "source_bytes": total,
            "files": copied, "database_method": "sqlite_backup_committed_transactions",
            "raw_imports_included": True, "ai_results_included": True, "admin_pin_included_local_only": True}


class SideBySideUpdater:
    """A trusted launcher must implement lifecycle; no real process adapter here.

    authorize(ticket, manifest_hash): revalidates local PIN-granted session.
    stop_and_wait(slot): stops only its owned app and joins writers; raises on failure.
    start(slot): starts exact verified code using selected data_dir, returns opaque own handle.
    health(handle, slot): checks app_id, instance_root, data/schema, startup+read/save compatibility.
    stop(handle): stops only that own process and joins writers.
    Caller must not implement these as no-op booleans in a released app.
    """
    def __init__(self, root, verifier, lifecycle, *, database_name="office.sqlite3", max_backup_bytes=MAX_BACKUP_BYTES,
                 installation_root=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.verifier, self.lifecycle = verifier, lifecycle
        self.database_name = database_name
        self.max_backup_bytes = max_backup_bytes
        # Production caller selects its existing installation root explicitly.
        # updater state, version code and persistent private profile are separate.
        self.installation_root = Path(installation_root).resolve() if installation_root else self.root.parent
        self.version_root = self.installation_root / "versions"
        self.state_path = self.root / "active.json"
        self.journal_path = self.root / "transaction.json"
        self.lock = threading.Lock()

    def initialize(self, *, code_dir, data_dir, sequence=0, schema_version=1, version=None):
        if self.state_path.exists():
            fail("already_initialized", "실행 버전 정보가 이미 있습니다.")
        code, data = Path(code_dir).resolve(), Path(data_dir).resolve()
        if not code.is_dir() or not data.is_dir() or code == data:
            fail("initial_paths", "기존 앱과 자료 경로를 확인해야 합니다.")
        if version is not None:
            version_tuple(version)
        atomic_json(self.state_path, {"app_id": APP_ID, "active": {"code_dir": str(code), "data_dir": str(data),
                    "sequence": sequence, "schema_version": schema_version, "release_id": "initial",
                    **({"version": version} if version is not None else {})},
                    "highest_accepted_sequence": sequence})

    def status(self):
        if not self.state_path.exists():
            fail("not_initialized", "기존 앱 경로가 아직 등록되지 않았습니다.")
        return read_json(self.state_path.read_bytes())

    def apply(self, envelope, archive, *, ticket, confirm=False):
        if confirm is not True:
            fail("manual_confirmation", "업데이트 적용을 직접 확인해야 합니다.")
        if not self.lock.acquire(blocking=False):
            fail("update_busy", "다른 업데이트가 진행 중입니다.")
        lock_path = self.root / "apply.lock"
        lock_handle = None
        try:
            try:
                lock_handle = lock_path.open("xb")
                lock_handle.write(str(os.getpid()).encode("ascii")); lock_handle.flush()
            except FileExistsError:
                fail("update_busy", "이전 업데이트 상태를 먼저 확인해야 합니다.")
            state = self.status()
            old = state["active"]
            if self.journal_path.exists() and read_json(self.journal_path.read_bytes()).get("phase") not in ("committed", "rolled_back", "aborted"):
                fail("recovery_required", "중단된 업데이트를 먼저 복구해야 합니다.")
            verified = self.verifier.verify(envelope, current_sequence=state["highest_accepted_sequence"],
                                            current_schema=old["schema_version"], current_version=old.get("version"))
            if self.lifecycle.authorize(ticket, verified["manifest_sha256"]) is not True:
                fail("admin_required", "관리자 PIN 확인 후 직접 적용해야 합니다.")
            attempt = uuid.uuid4().hex
            folder = self.root / "attempts" / attempt
            code, backup, work = self.version_root / attempt / "app", folder / "backup_data", folder / "working_data"
            private_state = folder / "private_state"
            folder.mkdir(parents=True)
            transaction = {"attempt_id": attempt, "phase": "verified", "old": old,
                           "new": {"code_dir": str(code), "data_dir": str(work), "sequence": verified["manifest"]["sequence"],
                           "schema_version": old["schema_version"], "release_id": verified["manifest"]["release_id"],
                           "version": verified["manifest"]["version"],
                           "local_content_dir": str(private_state / "content"),
                           "local_config_dir": old.get("local_config_dir") or str(Path(old["code_dir"]) / "local_private")},
                           "backup_dir": str(backup), "manifest_sha256": verified["manifest_sha256"],
                           "publisher_key_fingerprint": verified["publisher_key_fingerprint"],
                           "snapshot_complete": False}
            atomic_json(self.journal_path, transaction)
            stopped, handle = False, None
            try:
                # Prepare and inspect code before interrupting the current app.
                self.verifier.extract(verified, Path(archive), code)
                _, source_bytes = tree_inventory(Path(old["data_dir"]), limit=self.max_backup_bytes)
                if shutil.disk_usage(folder).free < source_bytes * 2 + MAX_EXPANDED_BYTES + 50 * 1024 * 1024:
                    fail("disk_space", "전체 사본과 새 버전을 준비할 디스크 공간이 부족합니다.")
                if self.lifecycle.authorize(ticket, verified["manifest_sha256"]) is not True:
                    fail("admin_required", "관리자 확인이 만료되어 다시 확인해야 합니다.")
                self.lifecycle.stop_and_wait(old)
                stopped = True
                transaction["phase"] = "stopped"
                atomic_json(self.journal_path, transaction)
                inherited = self._inherit_private_state(old, private_state)
                snapshot = full_data_snapshot(Path(old["data_dir"]), backup, self.database_name)
                snapshot["local_state"] = inherited
                atomic_json(folder / "backup_manifest.json", snapshot)
                transaction["snapshot_complete"] = True
                transaction["phase"] = "backed_up"
                atomic_json(self.journal_path, transaction)
                shutil.copytree(backup, work)
                if tree_inventory(backup)[0] != tree_inventory(work)[0]:
                    fail("working_copy_mismatch", "새 버전 자료 사본이 일치하지 않습니다.")
                transaction["phase"] = "starting_new"
                atomic_json(self.journal_path, transaction)
                handle = self.lifecycle.start(transaction["new"])
                if self.lifecycle.health(handle, transaction["new"]) is not True:
                    fail("new_health_failed", "새 버전 시작 확인에 실패했습니다.")
                # SQLite schema MUST remain the verified value, even if the new
                # code unexpectedly changed it despite claiming no migration.
                self._check_database_schema(work, old["schema_version"])
                atomic_json(self.state_path, {"app_id": APP_ID, "active": transaction["new"],
                    "highest_accepted_sequence": transaction["new"]["sequence"], "previous": old})
                transaction["phase"] = "committed"
                atomic_json(self.journal_path, transaction)
                return {"status": "applied", "active": transaction["new"], "previous": old,
                        "backup_dir": str(backup), "old_data_preserved": True, "snapshot": snapshot}
            except Exception as exc:
                if handle is not None:
                    # If stopping a possibly-writing new app fails, abort safe:
                    # never start a second owner and never overwrite any data.
                    try:
                        self.lifecycle.stop(handle)
                    except Exception:
                        transaction["phase"] = "rollback_stop_failed"
                        atomic_json(self.journal_path, transaction)
                        fail("rollback_stop_failed", "새 앱 종료를 확인할 수 없어 수동 복구가 필요합니다. 모든 사본을 보존했습니다.")
                transaction["phase"] = "rollback_required" if stopped else "aborted"
                transaction["error_code"] = getattr(exc, "code", "update_failed")
                atomic_json(self.journal_path, transaction)
                if stopped:
                    # Old app receives the untouched OLD DATA. Failed/migrated
                    # working data and snapshot are kept, never replayed into it.
                    self._restart_old(old, transaction)
                    atomic_json(self.state_path, state)
                    transaction["phase"] = "rolled_back"
                    atomic_json(self.journal_path, transaction)
                if isinstance(exc, UpdateError):
                    raise
                raise UpdateError("update_failed", "업데이트가 실패했습니다. 기존 앱과 자료는 보존했습니다.") from exc
        finally:
            if lock_handle is not None:
                lock_handle.close()
                lock_path.unlink(missing_ok=True)
            self.lock.release()

    def _check_database_schema(self, data, expected):
        path = data / self.database_name
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                fail("new_database_integrity", "새 버전 자료 무결성 확인이 실패했습니다.")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version != expected:
                fail("unexpected_schema_change", "새 버전이 자료 구조를 바꾸어 기존 자료 사본으로 돌아갑니다.")

    def _restart_old(self, old, transaction):
        try:
            handle = self.lifecycle.start(old)
        except Exception:
            transaction["phase"] = "rollback_start_failed"
            atomic_json(self.journal_path, transaction)
            fail("rollback_start_failed", "이전 앱 시작에 실패했습니다. 모든 자료와 사본을 보존했습니다.")
        if self.lifecycle.health(handle, old) is not True:
            try:
                self.lifecycle.stop(handle)
            except Exception:
                transaction["phase"] = "rollback_old_stop_failed"
                atomic_json(self.journal_path, transaction)
                fail("rollback_old_stop_failed", "이전 앱 종료도 확인하지 못했습니다. 수동 복구가 필요합니다.")
            transaction["phase"] = "rollback_health_failed"
            atomic_json(self.journal_path, transaction)
            fail("rollback_health_failed", "이전 앱 시작도 확인하지 못했습니다. 기존 자료와 사본을 보존했습니다.")
        return handle

    def _inherit_private_state(self, old, destination):
        """Local-only copies; never in any publisher manifest/payload.

        Future launcher/backend must read local_content_dir preferentially.
        Frozen current backend has no such option; integration is outstanding.
        """
        result = {"private_profile": {"policy": "persistent_reference_only", "read": False, "copied": False,
            "path": old.get("local_config_dir") or str(Path(old["code_dir"]) / "local_private"),
            "public_distribution": False}}
        destination.mkdir()
        for name, field in (("content", "local_content_dir"),):
            source = Path(old.get(field) or (Path(old["code_dir"]) / name))
            target = destination / name
            if source.exists():
                before, size = tree_inventory(source, limit=self.max_backup_bytes)
                shutil.copytree(source, target)
                if tree_inventory(source)[0] != before or tree_inventory(target)[0] != before:
                    fail("private_state_changed", "로컬 프로그램·설정 사본 검증이 실패했습니다.")
                result[name] = {"files": before, "bytes": size, "public_distribution": False}
            else:
                target.mkdir()
                result[name] = {"files": [], "bytes": 0, "public_distribution": False}
        return result

    def recover_interrupted(self, *, ticket, confirm=False):
        """Manual recovery only. Do not guess/kill a PID from a stale file.

        Lifecycle must inspect its owned handles and stop the new app; a stale
        filesystem lock requires its separate explicit owner-confirmation flow.
        Main integration must provide that flow. This method does not unlink it.
        """
        if not confirm or self.lifecycle.authorize(ticket, "recovery") is not True:
            fail("admin_required", "중단 복구는 관리자 확인 후 직접 실행해야 합니다.")
        if not self.lock.acquire(blocking=False):
            fail("update_busy", "다른 업데이트 또는 복구가 진행 중입니다.")
        lock_path, lock_handle = self.root / "apply.lock", None
        try:
            try:
                lock_handle = lock_path.open("xb")
                lock_handle.write(str(os.getpid()).encode("ascii")); lock_handle.flush()
            except FileExistsError:
                fail("stale_lock_review", "이전 실행이 종료되었는지 관리자가 먼저 확인해야 합니다.")
            tx = read_json(self.journal_path.read_bytes())
            if tx["phase"] in ("committed", "rolled_back", "aborted"):
                return {"status": "no_recovery_needed"}
            # 'verified' may still have a living old owner; failed rollback may
            # have an old handle too. Trusted supervisor reconciles BOTH slots,
            # stops+joins only its owned handles, and refuses an unknown owner.
            self.lifecycle.ensure_slots_stopped(tx["old"], tx["new"])
            self._restart_old(tx["old"], tx)
            state = self.status()
            state["active"] = tx["old"]
            atomic_json(self.state_path, state)
            tx["phase"] = "rolled_back"
            atomic_json(self.journal_path, tx)
            return {"status": "recovered", "active": tx["old"], "all_attempt_files_preserved": True}
        finally:
            if lock_handle is not None:
                lock_handle.close(); lock_path.unlink(missing_ok=True)
            self.lock.release()
