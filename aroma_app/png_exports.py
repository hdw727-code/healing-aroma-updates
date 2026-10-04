"""Save the existing A4 canvas poster into the local full-backup scope.

No AI, subprocess, network, authentication, or original-file access occurs here.
The HTTP integration must retain its own loopback/CSRF/maintenance guards and
an 8 MiB JSON body limit. Every write shares AppStore.lock with full backups.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from image_adapter import ImageError, validate_png_base64

MAX_ENCODED_BYTES = 7 * 1024 * 1024
MAX_PNG_BYTES = 5 * 1024 * 1024
WIDTH, HEIGHT = 2480, 3508
OPERATION = "studio:png"
FILENAME = re.compile(r"studio_[a-f0-9]{32}\.png\Z")
DATA_PREFIX = "data:image/png;base64,"


class PngExportError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def _fail(code, message, status=400):
    raise PngExportError(code, message, status)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _linked(details):
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _guard_path(path, *, directory):
    """Reject symlinks/junctions/reparse points on the complete existing chain."""
    path = Path(os.path.abspath(path))
    for parent in reversed((path, *path.parents)):
        details = parent.stat(follow_symlinks=False)
        if _linked(details):
            _fail("png_storage_link", "PNG 저장 폴더의 연결 경로를 확인해 주세요.", 409)
        expected_directory = parent != path or directory
        if expected_directory and not stat.S_ISDIR(details.st_mode):
            _fail("png_storage_path", "PNG 저장 폴더를 확인해 주세요.", 409)
        if not expected_directory and not stat.S_ISREG(details.st_mode):
            _fail("png_storage_path", "PNG 저장 파일을 확인해 주세요.", 409)
    return path


def _identity(path):
    details = path.stat(follow_symlinks=False)
    if _linked(details) or not stat.S_ISREG(details.st_mode):
        _fail("png_storage_link", "PNG 저장 파일의 연결 경로를 확인해 주세요.", 409)
    return details.st_dev, details.st_ino


def _remove_owned(path, identity):
    if path is None or identity is None:
        return
    try:
        _guard_path(path.parent, directory=True)
        if _identity(path) == identity:
            path.unlink()
    except (OSError, PngExportError):
        # Never remove a replacement file or follow a changed directory link.
        pass


def _validate_payload(payload):
    if not isinstance(payload, dict) or set(payload) != {"request_id", "png_base64"}:
        _fail("png_input", "PNG 저장 내용을 확인해 주세요.")
    request = payload["request_id"]
    if not isinstance(request, str) or len(request) != 36:
        _fail("png_request_id", "PNG 저장 요청 번호를 확인해 주세요.")
    try:
        parsed = uuid.UUID(request)
    except (ValueError, AttributeError):
        _fail("png_request_id", "PNG 저장 요청 번호를 확인해 주세요.")
    if str(parsed) != request or not parsed.int:
        _fail("png_request_id", "PNG 저장 요청 번호를 확인해 주세요.")
    encoded = payload["png_base64"]
    if not isinstance(encoded, str):
        _fail("png_input", "PNG 저장 내용을 확인해 주세요.")
    if encoded.startswith(DATA_PREFIX):
        encoded = encoded[len(DATA_PREFIX):]
    if len(encoded) > MAX_ENCODED_BYTES:
        _fail("png_too_large", "PNG 파일은 5 MiB 이하로 저장해 주세요.", 413)
    try:
        data, metadata = validate_png_base64(encoded)
    except ImageError as error:
        # The shared parser emits fixed messages and never includes input data.
        raise PngExportError(error.code, error.message, 400) from None
    if len(data) > MAX_PNG_BYTES:
        _fail("png_too_large", "PNG 파일은 5 MiB 이하로 저장해 주세요.", 413)
    if metadata["width"] != WIDTH or metadata["height"] != HEIGHT:
        _fail("png_dimensions", "A4 홍보물 크기(2480 × 3508)를 확인해 주세요.")
    normalized = {"sha256": metadata["sha256"], "width": WIDTH, "height": HEIGHT,
                  "bytes": len(data)}
    checksum = hashlib.sha256(_canonical(normalized).encode("utf-8")).hexdigest()
    return request, data, metadata, checksum


def _cached_response(row, checksum, exports, metadata):
    if row["operation"] != OPERATION or row["payload_hash"] != checksum:
        _fail("request_conflict", "같은 저장 요청에 다른 내용이 들어왔습니다. 다시 저장해 주세요.", 409)
    try:
        result = json.loads(row["response_json"])
    except (ValueError, TypeError):
        _fail("png_saved_state", "저장 기록을 확인하지 못했습니다. 관리자에게 확인을 요청해 주세요.", 409)
    if (not isinstance(result, dict) or set(result) != {"url", "filename", "format", "stored_in_full_backup"}
            or not isinstance(result.get("filename"), str) or not FILENAME.fullmatch(result["filename"])
            or result.get("url") != "/exports/" + result["filename"]
            or result.get("format") != "png" or result.get("stored_in_full_backup") is not True):
        _fail("png_saved_state", "저장 기록을 확인하지 못했습니다. 관리자에게 확인을 요청해 주세요.", 409)
    try:
        saved = _guard_path(exports / result["filename"], directory=False)
        if saved.stat().st_size != metadata["bytes"] or saved.stat().st_size > MAX_PNG_BYTES:
            raise ValueError
        if hashlib.sha256(saved.read_bytes()).hexdigest() != metadata["sha256"]:
            raise ValueError
    except (OSError, ValueError):
        _fail("png_export_missing", "저장된 PNG를 확인하지 못했습니다. 전체 백업 또는 관리자 확인이 필요합니다.", 409)
    return result


def save_studio_png(store, payload) -> dict:
    """Store validated bytes once; return only a download URL and safe filename."""
    request, data, metadata, checksum = _validate_payload(payload)
    part = final = None
    part_identity = final_identity = None
    with store.lock:
        try:
            data_root = _guard_path(store.data_dir, directory=True)
            exports = _guard_path(data_root / "exports", directory=True)
            if Path(os.path.abspath(store.export_dir)) != exports:
                _fail("png_storage_path", "PNG 저장 폴더를 확인해 주세요.", 409)
            _guard_path(store.db_path, directory=False)
            with store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM requests WHERE request_id=?", (request,)).fetchone()
                if row is not None:
                    return _cached_response(row, checksum, exports, metadata)
                filename = "studio_" + uuid.uuid4().hex + ".png"
                final = exports / filename
                part = exports / ("." + filename + "." + uuid.uuid4().hex + ".part")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                _guard_path(exports, directory=True)
                descriptor = os.open(part, flags, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    part_identity = _identity(part)
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                _guard_path(exports, directory=True)
                if os.name == "nt":
                    # Windows rename fails if a target exists; never os.replace.
                    os.rename(part, final)
                else:
                    # POSIX rename overwrites: link() provides exclusive creation.
                    os.link(part, final, follow_symlinks=False)
                    part.unlink()
                final_identity = part_identity
                result = {"url": "/exports/" + filename, "filename": filename,
                          "format": "png", "stored_in_full_backup": True}
                conn.execute("INSERT INTO requests VALUES (?,?,?,?,?)", (
                    request, OPERATION, checksum, _canonical(result),
                    datetime.now(timezone.utc).isoformat(timespec="seconds")))
                _guard_path(exports, directory=True)
                # AppStore.connect commits before this returns to the HTTP route.
            return result
        except BaseException as error:
            _remove_owned(part, part_identity)
            _remove_owned(final, final_identity)
            if isinstance(error, PngExportError):
                raise
            if not isinstance(error, Exception):
                raise
            raise PngExportError("png_storage_failed", "PNG를 저장하지 못했습니다. 저장 공간을 확인한 뒤 다시 시도해 주세요.", 500) from None
