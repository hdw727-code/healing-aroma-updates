"""Small routing adapter. Main Handler must enforce Host/Origin/CSRF first.

Returns JSON DTOs; no HTTP server is started and no file is uploaded here.
The caller must implement the maintenance/readers gate described in the
integration contract before enabling restore/recover in a released build.
"""
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import uuid
import zipfile
from whole_backup import WholeBackup, WholeBackupError, BACKUP_NAME, MAX_MANIFEST
from update_client import read_json, digest_file


class FullBackupAPI:
    def __init__(self, store, *, maintenance=None):
        self.service = WholeBackup(store)
        self.maintenance = maintenance

    def status(self):
        pending = self.service.pending_recovery()
        sync=getattr(self.service.store,"_commerce_sync_manager",None)
        syncing=bool(sync and sync.active)
        return {"format": "aroma-full-work-backup", "schema_version":2, "commerce_database_included":True, "sync_credentials_included":False, "schema1_restore_requires_offline_conversion":True, "schema1_import_conversion_supported":True, "import_max_bytes":self.service.max_bytes + MAX_MANIFEST, "backups": self.service.list(),
            "pending_recovery": pending, "raw_files_included": True,
            "ai_result_files_included": True, "admin_pin_included": False,
            "auth_profile_included": False, "logs_included": False, "prior_backups_included": False,
            "local_program_content_included": True, "local_file_metadata_included": True,
            "new_backup_format_version": 2, "older_backups_may_omit_program_content": True,
            "sync_active":syncing, "create_enabled": not pending and not syncing, "import_enabled": self.maintenance is not None and not pending and not syncing, "convert_enabled": self.maintenance is not None and not pending and not syncing, "restore_enabled": self.maintenance is not None and not pending and not syncing,
            "recover_enabled": self.maintenance is not None and bool(pending),
            "legacy_zip_supported_by_existing_route": True}

    def create(self):
        with self.service.store.lock:
            self._check_sync_idle()
            return self.service.create()

    def _check_sync_idle(self):
        sync = getattr(self.service.store, "_commerce_sync_manager", None)
        if sync and sync.active:
            raise WholeBackupError("sync_busy", "문의 동기화가 끝난 뒤 다시 시도해 주세요.", 409)

    def _check_import_idle(self):
        self._check_sync_idle()
        self.service.store.require_no_recovery()
        self.service.store.require_runtime_idle()
        self.service._active_ai()

    def import_stream(self, stream, size, filename, admin_token):
        """Validate an uploaded copy without restoring it or opening its source."""
        self.service.store.require_admin(admin_token)
        if self.maintenance is None:
            raise WholeBackupError("maintenance_adapter_missing", "자료 가져오기 준비를 확인해 주세요.", 503)
        if (not isinstance(filename, str) or not filename.lower().endswith(".zip")
                or len(filename) > 255 or any(c in filename for c in '/\\:')
                or any(ord(c) < 32 or ord(c) == 127 for c in filename)):
            raise WholeBackupError("backup_name", "경로가 없는 전체백업 ZIP 이름을 확인해 주세요.")
        if type(size) is not int or not 1 <= size <= self.service.max_bytes + MAX_MANIFEST:
            raise WholeBackupError("archive_limit", "전체백업 ZIP의 크기 제한을 확인해 주세요.")
        with self.maintenance():
            self.service.store.require_admin(admin_token)
            self._check_import_idle()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            name = "aroma_full_" + stamp + "_" + uuid.uuid4().hex[:12] + ".zip"
            target = self.service.backups / name
            # The user's selected file never becomes a filesystem path here.
            # Scratch files and the eventual archive are owned by this app.
            with tempfile.TemporaryDirectory(prefix="backup_import_", dir=self.service.backups) as tmp:
                scratch = Path(tmp)
                archive_path = scratch / "upload.zip"
                with archive_path.open("xb") as output:
                    remaining = size
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise WholeBackupError("transfer_incomplete", "파일 전송이 끝나지 않았습니다. 다시 선택해 주세요.")
                        output.write(chunk)
                        remaining -= len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    with zipfile.ZipFile(archive_path) as archive:
                        info = archive.getinfo("manifest.json")
                        if info.file_size > MAX_MANIFEST:
                            raise WholeBackupError("manifest_limit", "백업 목록이 너무 큽니다.")
                        manifest = read_json(archive.read(info))
                    schema = manifest.get("schema_version") if isinstance(manifest, dict) else None
                    if type(schema) is not int or schema not in (1, 2):
                        raise WholeBackupError("backup_format", "이 앱의 전체백업 ZIP을 선택해 주세요.")
                except (zipfile.BadZipFile, KeyError, ValueError):
                    raise WholeBackupError("backup_invalid", "전체백업 ZIP을 검증하지 못했습니다.") from None
                manifest = WholeBackup(self.service.store, schema_version=schema,
                    max_bytes=self.service.max_bytes, max_files=self.service.max_files).validate(
                        archive_path, scratch / "validated")
                digest = digest_file(archive_path)
                # A random reserved name is never allowed to overwrite a backup.
                owned = False
                try:
                    with target.open("xb") as output, archive_path.open("rb") as source:
                        owned = True
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if digest_file(target) != digest:
                        raise WholeBackupError("archive_hash", "가져온 백업 복사본을 확인하지 못했습니다.")
                except BaseException:
                    if owned:
                        target.unlink(missing_ok=True)
                    raise
                return {"imported": True, "name": name, "format": "aroma-full-work-backup",
                    "schema_version": schema, "format_version": manifest["format_version"],
                    "archive_sha256": digest, "original_archive_unchanged": True,
                    "live_database_changed": False, "conversion_required": schema == 1,
                    "local_program_content_included": manifest["format_version"] == 2,
                    "local_file_metadata_included": manifest["format_version"] == 2}

    def convert_schema1(self, payload, admin_token):
        self.service.store.require_admin(admin_token)
        if not isinstance(payload, dict) or set(payload) != {"name", "confirm"}:
            raise WholeBackupError("backup_fields", "백업 이름과 변환 확인만 보내 주세요.")
        if payload.get("confirm") is not True:
            raise WholeBackupError("confirmation_required", "이전 백업 변환을 먼저 확인해 주세요.", 422)
        name = payload.get("name")
        if not isinstance(name, str) or not BACKUP_NAME.fullmatch(name):
            raise WholeBackupError("backup_name", "전체백업 목록에서 변환할 파일을 선택해 주세요.")
        if self.maintenance is None:
            raise WholeBackupError("maintenance_adapter_missing", "자료 변환 준비를 확인해 주세요.", 503)
        with self.maintenance():
            self.service.store.require_admin(admin_token)
            self._check_import_idle()
            source = self.service.backups / name
            if not source.is_file():
                raise WholeBackupError("backup_not_found", "변환할 백업을 찾지 못했습니다.", 404)
            from commerce_migration import convert_schema1_backup
            return convert_schema1_backup(self.service.store, source, confirmed=True)

    def restore(self, payload, admin_token):
        if self.maintenance is None:
            raise WholeBackupError("maintenance_adapter_missing", "안전한 복구 연결이 준비되지 않았습니다.", 503)
        # The context must block/wait all read/write HTTP handlers and local
        # jobs. It MUST NOT terminate an unrelated application.
        with self.maintenance():
            self._check_sync_idle()
            return self.service.restore(payload.get("name"), admin_token=admin_token, confirm=payload.get("confirm") is True)

    def recover(self, payload, admin_token):
        if self.maintenance is None:
            raise WholeBackupError("maintenance_adapter_missing", "안전한 중단 복구 연결이 준비되지 않았습니다.", 503)
        with self.maintenance():
            self._check_sync_idle()
            return self.service.recover(payload.get("attempt_id"), admin_token=admin_token, confirm=payload.get("confirm") is True)

    def download(self, name):
        if not isinstance(name, str) or not BACKUP_NAME.fullmatch(name):
            raise WholeBackupError("backup_name", "전체 백업 목록에서 파일을 선택해 주세요.")
        path = self.service.backups / name
        if not path.is_file() or path.is_symlink():
            raise WholeBackupError("backup_not_found", "백업 파일을 찾지 못했습니다.", 404)
        return {"path": path, "filename": name, "mime": "application/zip", "attachment": True}
