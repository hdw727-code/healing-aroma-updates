"""Small routing adapter. Main Handler must enforce Host/Origin/CSRF first.

Returns JSON DTOs; no HTTP server is started and no file is uploaded here.
The caller must implement the maintenance/readers gate described in the
integration contract before enabling restore/recover in a released build.
"""
from pathlib import Path
from whole_backup import WholeBackup, WholeBackupError, BACKUP_NAME


class FullBackupAPI:
    def __init__(self, store, *, maintenance=None):
        self.service = WholeBackup(store)
        self.maintenance = maintenance

    def status(self):
        pending = self.service.pending_recovery()
        return {"format": "aroma-full-work-backup", "backups": self.service.list(),
            "pending_recovery": pending, "raw_files_included": True,
            "ai_result_files_included": True, "admin_pin_included": False,
            "auth_profile_included": False, "logs_included": False, "prior_backups_included": False,
            "local_program_content_included": True, "local_file_metadata_included": True,
            "new_backup_format_version": 2, "older_backups_may_omit_program_content": True,
            "create_enabled": not pending, "restore_enabled": self.maintenance is not None and not pending,
            "recover_enabled": self.maintenance is not None and bool(pending),
            "legacy_zip_supported_by_existing_route": True}

    def create(self):
        return self.service.create()

    def restore(self, payload, admin_token):
        if self.maintenance is None:
            raise WholeBackupError("maintenance_adapter_missing", "안전한 복구 연결이 준비되지 않았습니다.", 503)
        # The context must block/wait all read/write HTTP handlers and local
        # jobs. It MUST NOT terminate an unrelated application.
        with self.maintenance():
            return self.service.restore(payload.get("name"), admin_token=admin_token, confirm=payload.get("confirm") is True)

    def recover(self, payload, admin_token):
        if self.maintenance is None:
            raise WholeBackupError("maintenance_adapter_missing", "안전한 중단 복구 연결이 준비되지 않았습니다.", 503)
        with self.maintenance():
            return self.service.recover(payload.get("attempt_id"), admin_token=admin_token, confirm=payload.get("confirm") is True)

    def download(self, name):
        if not isinstance(name, str) or not BACKUP_NAME.fullmatch(name):
            raise WholeBackupError("backup_name", "전체 백업 목록에서 파일을 선택해 주세요.")
        path = self.service.backups / name
        if not path.is_file() or path.is_symlink():
            raise WholeBackupError("backup_not_found", "백업 파일을 찾지 못했습니다.", 404)
        return {"path": path, "filename": name, "mime": "application/zip", "attachment": True}
