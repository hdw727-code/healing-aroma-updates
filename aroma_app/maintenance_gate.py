"""Request ownership gate for whole-data restore; all HTTP handlers opt in.

Install before enabling backup_api restore. A restore handler uses maintenance
directly, not request(), so it does not wait on its own request. Response/file
streaming must finish inside request() to protect readers from directory swaps.
"""
from contextlib import contextmanager
import threading
import time
from whole_backup import WholeBackupError


class MaintenanceGate:
    def __init__(self, *, timeout_seconds=15):
        self.condition = threading.Condition(threading.RLock())
        self.active_requests = 0
        self.maintaining = False
        self.pending_recovery = False
        self.timeout_seconds = timeout_seconds

    def set_pending_recovery(self, pending):
        with self.condition:
            self.pending_recovery = bool(pending)
            self.condition.notify_all()

    def status(self):
        with self.condition:
            return {"maintenance": self.maintaining, "pending_recovery": self.pending_recovery,
                    "active_requests": self.active_requests}

    @contextmanager
    def request(self, *, control=False):
        with self.condition:
            if self.maintaining or (self.pending_recovery and not control):
                raise WholeBackupError("maintenance", "자료 복구 확인 중입니다. 잠시 기다려 주세요.", 409)
            self.active_requests += 1
        try:
            yield
        finally:
            with self.condition:
                self.active_requests -= 1
                self.condition.notify_all()

    @contextmanager
    def maintenance(self):
        with self.condition:
            if self.maintaining:
                raise WholeBackupError("maintenance_busy", "다른 자료 복구가 진행 중입니다.", 409)
            self.maintaining = True
            deadline = time.monotonic() + self.timeout_seconds
            while self.active_requests:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.maintaining = False
                    self.condition.notify_all()
                    raise WholeBackupError("maintenance_readers_busy", "진행 중인 문서 열기 또는 다운로드를 마친 뒤 다시 복구해 주세요.", 409)
                self.condition.wait(remaining)
        try:
            yield
        finally:
            with self.condition:
                self.maintaining = False
                self.condition.notify_all()
