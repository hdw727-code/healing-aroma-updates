"""One explicit official device-login lifecycle. No credential reads or copies.

Only a future, user-confirmed setup action calls start(). Import, construction,
and status() never launch the CLI. This helper does not adopt another process or
issue a replacement for the root agent's existing approval/login session.
"""
from __future__ import annotations

import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time

try:
    from .runtime_setup import (discover_runtime, validate_owned_profile, prepare_profile,
                                child_environment, fixed_app_server_command, compatibility_guard, SetupError)
    from .image_adapter import _OwnedProcessTree
except ImportError:
    from runtime_setup import (discover_runtime, validate_owned_profile, prepare_profile,
                               child_environment, fixed_app_server_command, compatibility_guard, SetupError)
    from image_adapter import _OwnedProcessTree

DEVICE_URL = "https://auth.openai.com/codex/device"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
CODE = re.compile(r"^[A-Z0-9]{4,8}-[A-Z0-9]{4,8}$")


class DeviceProcess:
    def __init__(self, command, *, cwd, env):
        self.command, self.cwd, self.env = command, cwd, env
        self.process = None
        self.tree = None
        self.lines = queue.Queue(maxsize=64)
        self.problem = False
        self.total = 0
        self.error_category = None

    def start(self):
        if os.name != "nt":
            raise SetupError("unsupported_os", "공식 Windows 로그인만 지원합니다.")
        self.process = subprocess.Popen(self.command, cwd=str(self.cwd), env=self.env,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, shell=False,
                                        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        self.tree = _OwnedProcessTree(self.process)
        self.readers = [threading.Thread(target=self._read, daemon=True), threading.Thread(target=self._discard, daemon=True)]
        for reader in self.readers:
            reader.start()

    def _read(self):
        try:
            while True:
                value = self.process.stdout.readline(4097)
                if not value:
                    return
                self.total += len(value)
                if len(value) > 4096 or self.total > 32768:
                    self.problem = True
                    return
                # Decode in memory; raw login output is never stored or logged.
                self.lines.put(value.decode("utf-8", errors="strict"), timeout=.2)
        except (OSError, UnicodeError, queue.Full):
            self.problem = True

    def _discard(self):
        total = 0
        try:
            for value in iter(lambda: self.process.stderr.readline(4097), b""):
                total += len(value)
                if len(value) > 4096 or total > 32768:
                    self.problem = True
                    return
                text = value.decode("utf-8", errors="replace").lower()
                if "device code login is not enabled" in text or "enable device code login" in text:
                    self.error_category = "device_auth_disabled"
        except OSError:
            pass

    def read(self, timeout=.05):
        if self.problem:
            raise SetupError("login_output_invalid", "공식 로그인 응답을 안전하게 처리하지 못했습니다.")
        try:
            return self.lines.get(timeout=timeout)
        except queue.Empty:
            return None

    def poll(self):
        code = self.process.poll()
        if code is not None:
            for reader in self.readers:
                reader.join(timeout=.2)
            if not self.lines.empty():
                return None
        return code

    def close(self):
        if self.tree:
            self.tree.close()  # Kill only our owned Windows process/job if still running.
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=3)
            for reader in getattr(self, "readers", []):
                reader.join(timeout=.5)
            for pipe in (self.process.stdout, self.process.stderr):
                pipe.close()
        while not self.lines.empty():
            self.lines.get_nowait()


class RuntimeLogin:
    def __init__(self, app_root, *, private_root=None, profile_root=None, runtime_registry=None,
                 process_factory=None, timeout_seconds=900, runtime_root=None):
        self.app_root = Path(app_root).resolve(strict=True)
        self.runtime_root = Path(runtime_root).resolve(strict=True) if runtime_root is not None else self.app_root
        self.private_root = Path(private_root).absolute() if private_root is not None else self.app_root / "local_private"
        self.profile_root = Path(profile_root).absolute() if profile_root is not None else self.private_root / "codex_home"
        self.runtime_registry = runtime_registry
        self.process_factory = process_factory
        self.timeout_seconds = max(.1, min(900, float(timeout_seconds)))
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._code_ready = threading.Event()
        self._worker = None
        self._code = None
        self._state = "not_started"
        self._error = None
        self._issued = False

    def status(self):
        with self._lock:
            return {"state": self._state, "authenticated_verified": self._state == "done" and self.process_factory is None,
                    "simulated": self.process_factory is not None, "model_calls": 0,
                    "device_code_persisted": False, "credentials_read": False, "credentials_copied": False,
                    "code_available": self._state == "waiting_user" and self._issued,
                    "error": self._error, "automatic_retry": False,
                    "restart_required": self._state in {"done", "failed", "timed_out", "cancelled"},
                    "message": "공식 화면에서 직접 로그인합니다. 이미지 지원은 생성 시험 후 확인됩니다."}

    def start(self, *, send_confirmed=False):
        if send_confirmed is not True:
            raise SetupError("confirmation_required", "앱 전용 공식 로그인 시작에 동의해 주세요.")
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                raise SetupError("login_busy", "이미 공식 로그인이 진행 중입니다. 새 코드를 발급하지 않았습니다.")
            # One helper instance cannot silently issue a second code after a failure.
            if self._issued or self._state != "not_started":
                raise SetupError("login_already_attempted", "이 로그인 시도는 끝났습니다. 새 시도는 별도 확인이 필요합니다.")
            self._state = "starting"
            self._cancel.clear()
            self._worker = threading.Thread(target=self._run, daemon=True)
            self._worker.start()
        self._code_ready.wait(timeout=10)
        value = self.status()
        with self._lock:
            if self._code is not None:
                value.update(verification_url=DEVICE_URL, user_code=self._code, expires_in_seconds=900)
                self._code = None  # Returned once to the confirmed POST, never GET status.
        return value

    def _run(self):
        process = None
        try:
            runtime = self.runtime_registry() if callable(self.runtime_registry) else self.runtime_registry
            runtime = discover_runtime(self.runtime_root) if runtime is None else runtime
            if not isinstance(runtime, dict):
                raise SetupError("runtime_not_verified", "공식 런타임 검증이 필요합니다.")
            fixed_app_server_command(runtime)  # Validates version/hash; it does not launch app-server.
            if self.process_factory is None:
                compatibility = compatibility_guard()
                if compatibility.get("runtime_compatible") is not True:
                    raise SetupError(compatibility.get("state", "unsupported_runtime"), compatibility.get("message", "실행 환경 확인이 필요합니다."))
            if self.process_factory is None and Path(runtime.get("executable", "")) != self.runtime_root / "runtime/codex/bin/codex.exe":
                raise SetupError("runtime_not_verified", "앱에 포함된 고정 런타임만 사용할 수 있습니다.")
            profile = validate_owned_profile(self.app_root, self.private_root, profile_root=self.profile_root)
            if profile.get("state") == "own_login_required" and self.profile_root == self.private_root / "codex_home":
                prepare_profile(self.app_root, private_root=self.private_root)
                profile = validate_owned_profile(self.app_root, self.private_root, profile_root=self.profile_root)
            if profile.get("state") != "profile_verified":
                raise SetupError(profile.get("state", "profile_not_owned"), profile.get("message", "앱 전용 설정이 필요합니다."))
            command = [runtime["executable"], "-c", 'cli_auth_credentials_store="keyring"',
                       "-c", 'forced_login_method="chatgpt"', "login", "--device-auth"]
            factory = self.process_factory or DeviceProcess
            process = factory(command, cwd=self.private_root, env=child_environment(self.profile_root))
            process.start()
            deadline = time.monotonic() + self.timeout_seconds
            url_seen = code_expected = False
            while True:
                if self._cancel.is_set():
                    raise SetupError("cancelled", "공식 로그인을 취소했습니다.")
                if time.monotonic() > deadline:
                    raise SetupError("timed_out", "공식 로그인 코드의 제한 시간이 지났습니다.")
                line = process.read()
                if line is not None:
                    text = ANSI.sub("", line).strip()
                    if text == DEVICE_URL:
                        url_seen = True
                    elif "Enter this one-time code" in text and url_seen:
                        code_expected = True
                    elif code_expected and CODE.fullmatch(text):
                        with self._lock:
                            if self._issued:
                                raise SetupError("login_output_invalid", "중복 로그인 코드로 중단했습니다.")
                            self._code, self._issued, self._state = text, True, "waiting_user"
                        self._code_ready.set()
                        code_expected = False
                    elif "https://" in text and "auth.openai.com" not in text:
                        raise SetupError("login_output_invalid", "표준 공식 로그인 주소가 아닙니다.")
                exit_code = process.poll()
                if exit_code is not None:
                    if exit_code != 0 or not self._issued:
                        if getattr(process, "error_category", None) == "device_auth_disabled":
                            raise SetupError("device_auth_disabled", "본인이 ChatGPT 보안 설정에서 기기 코드 로그인을 확인해야 합니다. 설정을 자동 변경하거나 코드를 다시 발급하지 않습니다.")
                        raise SetupError("official_login_failed", "공식 로그인이 완료되지 않았습니다. 새 권한이나 유료 API로 전환하지 않습니다.")
                    with self._lock:
                        self._state = "done"
                        self._code = None
                    break
        except SetupError as error:
            with self._lock:
                self._state = error.code if error.code in {"cancelled", "timed_out"} else "failed"
                self._error = {"code": error.code, "message": error.message}
                self._code = None
        except (OSError, ValueError, TypeError, KeyError):
            with self._lock:
                self._state = "failed"
                self._error = {"code": "official_login_failed", "message": "공식 로그인 실행을 완료하지 못했습니다."}
                self._code = None
        finally:
            if process:
                process.close()
            self._code_ready.set()

    def cancel(self):
        self._cancel.set()
        with self._lock:
            self._code = None
        return self.status()

    def close(self):
        self.cancel()
        worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=5)
