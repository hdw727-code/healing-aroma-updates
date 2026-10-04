"""Staging-only public runtime discovery; no account/credential file reads."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import sys

VERSION = "0.160.0"
PUBLIC_FILES = {
    "runtime/codex/bin/codex.exe": (326872368, "fdda5fa3cf3fb3d000b876720742857676293e4315e4b045fae6f8bd7e866d1d"),
    "runtime/codex/bin/codex-code-mode-host.exe": (74697520, "1d448bfde19e7a280d600d8d0bcddf77afbe9feaec1e804905becc5f39bc9db6"),
    "runtime/codex/codex-path/rg.exe": (4218880, "14231169855ec5205cf5a1b6f1db358ff4aed4247c86b69ce8aae647c77f6680"),
    "runtime/codex/codex-resources/codex-command-runner.exe": (8207152, "dd13f8e95faba5cf539d870dd3b64c226b00d062a5b523e7e08c44fdc8235224"),
    "runtime/codex/codex-resources/codex-windows-sandbox-setup.exe": (17685808, "8f91d62aca2aca87b0ebf446848b817720415dd3080905ffc3085c17b70bd57a"),
    "runtime/codex/codex-package.json": (215, "0ccad88219ce824ea2eda63d64fb3618e81ea0345cc7281c2e1b5a8abd19964d"),
    "runtime/codex/LICENSE": (10926, "d17f227e4df5da1600391338865ce0f3055211760a36688f816941d58232d8dc"),
    "runtime/codex/NOTICE": (242, "9d71575ecfd9a843fc1677b0efb08053c6ba9fd686a0de1a6f5382fd3c220915"),
    "runtime/codex/third-party/RIPGREP-LICENSE-MIT.txt": (1081, "0f96a83840e146e43c0ec96a22ec1f392e0680e6c1226e6f3ba87e0740af850f"),
    "runtime/codex/third-party/PCRE2-LICENCE.md": (3867, "9cf7ac6976099a1d856826d3ef1b093bd6b84489dc6100628ac79e740cf9885a"),
}
OS_KEYS = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP",
           "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "PROGRAMDATA")
DISABLED = ("apps", "plugins", "shell_tool", "unified_exec", "code_mode",
            "code_mode_host", "code_mode_prewarm", "browser_use", "browser_use_external",
            "computer_use", "hooks", "skill_search", "workspace_dependencies",
            "multi_agent", "multi_agent_v2", "view_image", "shell_snapshot", "memories",
            "request_permissions_tool", "sleep_tool", "goals")
PROFILE_CONFIG = 'cli_auth_credentials_store = "keyring"\n' + (
    '# Aroma app-owned staging profile. Account secrets are never copied.\n'
    'approval_policy = "never"\nsandbox_mode = "read-only"\nweb_search = "disabled"\n'
    'project_doc_max_bytes = 0\n'
    '[history]\npersistence = "none"\n[features]\nimage_generation = true\n'
    'omit_app_server_notification_media = false\n'
    + ''.join(f'{feature} = false\n' for feature in DISABLED)
    + '[skills]\ninclude_instructions = false\n'
      '[skills.bundled]\nenabled = false\n'
      '[cloud.skills]\nenabled = false\n'
      '[orchestrator.mcp]\nenabled = false\n'
)


def native_architecture() -> str:
    """Get Windows native CPU metadata without platform.machine/WMI processes."""
    if os.name != "nt":
        return "unknown"
    import ctypes
    from ctypes import wintypes
    class SystemInfo(ctypes.Structure):
        _fields_ = [("processor_architecture", wintypes.WORD), ("reserved", wintypes.WORD),
                    ("page_size", wintypes.DWORD), ("minimum_address", ctypes.c_void_p),
                    ("maximum_address", ctypes.c_void_p), ("active_processor_mask", ctypes.c_size_t),
                    ("processor_count", wintypes.DWORD), ("processor_type", wintypes.DWORD),
                    ("allocation_granularity", wintypes.DWORD), ("processor_level", wintypes.WORD),
                    ("processor_revision", wintypes.WORD)]
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        query = kernel.GetNativeSystemInfo
        query.argtypes = [ctypes.POINTER(SystemInfo)]
        query.restype = None
        info = SystemInfo()
        query(ctypes.byref(info))
        # GetNativeSystemInfo alone can report emulated x64 on ARM64. Read the
        # actual native PE machine as well; unknown/API failure remains closed.
        current = kernel.GetCurrentProcess
        current.argtypes = []
        current.restype = wintypes.HANDLE
        native_query = kernel.IsWow64Process2
        native_query.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.USHORT), ctypes.POINTER(wintypes.USHORT)]
        native_query.restype = wintypes.BOOL
        process_machine, native_machine = wintypes.USHORT(), wintypes.USHORT()
        if not native_query(current(), ctypes.byref(process_machine), ctypes.byref(native_machine)):
            return "unknown"
        machine = {0x8664: "AMD64", 0xAA64: "ARM64", 0x14C: "x86", 0x1C4: "ARM"}.get(native_machine.value, "unknown")
        reported = {9: "AMD64", 12: "ARM64", 0: "x86", 5: "ARM"}.get(info.processor_architecture, "unknown")
        return machine if machine == reported or machine == "ARM64" else "unknown"
    except (AttributeError, OSError):
        return "unknown"


def compatibility_guard() -> dict:
    """Read OS/process metadata only. No registry, setup, auth, or bypass switch."""
    details = {"os": os.name, "architecture": native_architecture(),
               "process_bits": struct.calcsize("P") * 8,
               "python_version": ".".join(str(x) for x in sys.version_info[:3]),
               "windows_build": None, "windows_family": None,
               "minimum_windows_build": 17763, "actual_windows10_tested": False,
               "actual_windows11_tested": False, "runtime_compatible": False}
    if os.name != "nt":
        details.update(state="unsupported_os", message="이 배포판은 Windows 10·11 x64용입니다.")
        return details
    if details["process_bits"] != 64 or details["architecture"].lower() not in ("amd64", "x86_64"):
        details.update(state="unsupported_architecture", message="64비트 Intel/AMD Windows PC가 필요합니다. 32비트·ARM 배포판은 없습니다.")
        return details
    try:
        version = sys.getwindowsversion()
        details["windows_build"] = version.build
        details["windows_family"] = "Windows 11" if version.build >= 22000 else "Windows 10"
        if version.major != 10 or version.build < 17763 or version.product_type != 1:
            details.update(state="unsupported_windows_build", message="Windows 10 1809 이상 또는 Windows 11 데스크톱이 필요합니다.")
            return details
    except (AttributeError, OSError):
        details.update(state="os_not_verified", message="Windows 버전을 확인하지 못해 공식 실행을 중단합니다.")
        return details
    if sys.version_info[:3] != (3, 13, 16):
        details.update(state="python_runtime_mismatch", message="이 앱에 포함된 Python 3.13.16 실행 파일이 필요합니다.")
        return details
    details.update(state="windows11_candidate" if version.build >= 22000 else "windows10_best_effort",
                   runtime_compatible=True, support_level="recommended" if version.build >= 22000 else "best_effort",
                   message="운영체제 기본 조건을 충족했습니다. 이미지·로그인·어머니 PC 실기 검증은 별도입니다.")
    return details


class SetupError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _regular(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    if not directory and info.st_nlink != 1:
        raise SetupError("runtime_unsafe_path", "하드링크 파일은 앱 전용 설정으로 사용할 수 없습니다.")
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise SetupError("runtime_unsafe_path", "링크로 연결된 실행 경로는 사용할 수 없습니다.")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise SetupError("runtime_unsafe_path", "실행 경로가 폴더가 아닙니다.")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise SetupError("runtime_unsafe_path", "실행 파일이 일반 파일이 아닙니다.")


def _contained(root: Path, path: Path) -> Path:
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError:
        raise SetupError("runtime_unsafe_path", "실행 파일이 앱 폴더 밖에 있습니다.") from None
    cursor = path
    while cursor != root:
        _regular(cursor, directory=cursor != path)
        cursor = cursor.parent
    return resolved


def discover_runtime(app_root: Path) -> dict:
    """Validate only fixed public files inside a bundle; never PATH-search CLI."""
    try:
        root = Path(app_root).resolve(strict=True)
        _regular(root, directory=True)
        manifest = root / "runtime" / "CODEX_RUNTIME.json"
        _contained(root, manifest)
        if manifest.stat().st_size > 128 * 1024:
            raise SetupError("manifest_invalid", "실행 파일 목록이 너무 큽니다.")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise SetupError("manifest_invalid", "공식 실행 파일 목록의 형식이 잘못되었습니다.")
        entries = data.get("files")
        if data.get("version") != VERSION or not isinstance(entries, list):
            raise SetupError("manifest_invalid", "공식 실행 파일 버전 목록을 확인할 수 없습니다.")
        recorded = {}
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("path") in recorded:
                raise SetupError("manifest_invalid", "실행 파일 목록에 잘못된 항목이 있습니다.")
            recorded[entry.get("path")] = (entry.get("bytes"), entry.get("sha256"))
        if recorded != PUBLIC_FILES:
            raise SetupError("manifest_invalid", "고정된 공식 실행 파일 목록과 다릅니다.")
        for relative, (size, digest) in PUBLIC_FILES.items():
            file_path = _contained(root, root / relative)
            if file_path.stat().st_size != size:
                raise SetupError("runtime_hash_mismatch", "실행 파일 크기가 원본과 다릅니다.")
            checksum = hashlib.sha256()
            with file_path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    checksum.update(chunk)
            if checksum.hexdigest() != digest:
                raise SetupError("runtime_hash_mismatch", "실행 파일 검증에 실패했습니다.")
        return {"state": "runtime_verified", "version": VERSION,
                "executable": str(root / "runtime/codex/bin/codex.exe"),
                "sha256": PUBLIC_FILES["runtime/codex/bin/codex.exe"][1],
                "login_state": "not_checked", "image_ready": False,
                "image_state": "verification_hold", "files_verified": len(PUBLIC_FILES)}
    except SetupError as error:
        return {"state": error.code, "message": error.message, "image_ready": False}
    except PermissionError:
        return {"state": "runtime_access_denied", "message": "공식 실행 파일을 읽을 권한이 없습니다.", "image_ready": False}
    except FileNotFoundError:
        return {"state": "runtime_missing", "message": "앱에 포함된 공식 실행 파일을 찾을 수 없습니다.", "image_ready": False}
    except (OSError, ValueError, TypeError, KeyError):
        return {"state": "manifest_invalid", "message": "공식 실행 파일 정보를 확인할 수 없습니다.", "image_ready": False}


def prepare_profile(app_root: Path, *, private_root: Path | None = None) -> Path:
    """Explicit setup action only. Refuse an existing directory; no config reads."""
    root = Path(app_root).resolve(strict=True)
    _regular(root, directory=True)
    private = Path(private_root).absolute() if private_root is not None else root / "local_private"
    if ".." in private.parts:
        raise SetupError("unsafe_profile_path", "상위 폴더 이동 경로를 사용할 수 없습니다.")
    for ancestor in (private, *private.parents):
        if ancestor.exists() or ancestor.is_symlink():
            _regular(ancestor, directory=True)
    if private.exists():
        _regular(private, directory=True)
    else:
        private.mkdir(parents=True)
    profile = private / "codex_home"
    try:
        profile.mkdir()  # Existing config/auth/profile is never opened or overwritten.
    except FileExistsError:
        raise SetupError("profile_exists", "앱 전용 설정 폴더가 이미 있습니다. 기존 설정은 변경하지 않았습니다.") from None
    with (profile / "config.toml").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(PROFILE_CONFIG)
    with (profile / "APP_OWNED_PROFILE.json").open("x", encoding="utf-8") as stream:
        json.dump({"created_by": "aroma-image-production-setup", "schema_version": 1,
                   "runtime_version": VERSION, "credential_store": "keyring",
                   "config_sha256": hashlib.sha256(PROFILE_CONFIG.encode("utf-8")).hexdigest(),
                   "credentials_copied": False, "login_verified": False,
                   "image_verified": False}, stream, ensure_ascii=False, indent=2)
    return profile


PROFILE_SHA256 = hashlib.sha256(PROFILE_CONFIG.encode("utf-8")).hexdigest()
OWNED_CREATORS = {"aroma-image-production-setup", "aroma-approved-device-setup"}


def validate_owned_profile(app_root: Path, private_root: Path, *, profile_root: Path | None = None,
                           data_root: Path | None = None) -> dict:
    """Read only the two public app-owned setup files, never auth or directory contents.

    profile_root is a trusted constructor-only trial override. It is never accepted
    from an HTTP payload and cannot designate a normal Codex home: exact ownership
    marker and our static keyring configuration are required.
    """
    try:
        app = Path(app_root).resolve(strict=True)
        private = Path(private_root).absolute()
        profile = Path(profile_root).absolute() if profile_root is not None else private / "codex_home"
        if ".." in private.parts or ".." in profile.parts:
            raise SetupError("unsafe_profile_path", "상위 폴더 이동 경로를 사용할 수 없습니다.")
        designated = profile == private / "codex_home"
        for forbidden in (None if designated else app, Path(data_root).resolve(strict=True) if data_root is not None else None):
            if forbidden is not None:
                try:
                    profile.resolve(strict=True).relative_to(forbidden)
                except ValueError:
                    pass
                else:
                    raise SetupError("unsafe_profile_path", "로그인 폴더는 앱 코드·업무 데이터 밖에 있어야 합니다.")
        # Reject reparse points in all existing ancestors without enumerating them.
        for directory in (profile, *profile.parents):
            _regular(directory, directory=True)
        marker_path, config_path = profile / "APP_OWNED_PROFILE.json", profile / "config.toml"
        _regular(marker_path)
        _regular(config_path)
        if marker_path.stat().st_size > 4096 or config_path.stat().st_size != len(PROFILE_CONFIG.encode("utf-8")):
            raise SetupError("profile_not_owned", "앱 전용 설정을 확인하지 못했습니다.")
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if (not isinstance(marker, dict) or marker.get("created_by") not in OWNED_CREATORS
                or marker.get("runtime_version") != VERSION or marker.get("credential_store") != "keyring"
                or marker.get("credentials_copied") is not False
                or marker.get("config_sha256") != PROFILE_SHA256):
            raise SetupError("profile_not_owned", "앱 전용 로그인 설정 표식이 일치하지 않습니다.")
        # These bytes are the app's fixed non-secret config; existing user config is never read.
        digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
        if digest != PROFILE_SHA256:
            raise SetupError("profile_changed", "앱 전용 설정이 바뀌어 실행을 중단했습니다.")
        return {"state": "profile_verified", "profile": str(profile), "config_sha256": digest,
                "credential_store": "keyring", "credentials_read": False, "credentials_copied": False}
    except SetupError as error:
        return {"state": error.code, "message": error.message}
    except FileNotFoundError:
        return {"state": "own_login_required", "message": "앱 전용 공식 로그인이 필요합니다."}
    except PermissionError:
        return {"state": "profile_access_denied", "message": "앱 전용 설정을 확인할 권한이 없습니다."}
    except (OSError, ValueError, TypeError):
        return {"state": "profile_not_owned", "message": "앱 전용 설정을 확인하지 못했습니다."}


def fixed_app_server_command(runtime: dict) -> list[str]:
    if runtime.get("state") != "runtime_verified" or runtime.get("version") != VERSION or runtime.get("sha256") != PUBLIC_FILES["runtime/codex/bin/codex.exe"][1]:
        raise SetupError("runtime_not_verified", "공식 런타임 해시 검증이 필요합니다.")
    args = [runtime["executable"], "app-server", "--listen", "stdio://", "--strict-config",
            "-c", 'cli_auth_credentials_store="keyring"', "-c", 'approval_policy="never"',
            "-c", 'forced_login_method="chatgpt"',
            "-c", 'sandbox_mode="read-only"', "-c", 'web_search="disabled"',
            "-c", 'project_doc_max_bytes=0', "-c", 'project_root_markers=[".aroma-image-root"]',
            "-c", 'history.persistence="none"', "-c", 'features.image_generation=true',
            "-c", 'features.omit_app_server_notification_media=false']
    for name in DISABLED:
        if name != "code_mode":
            args += ["-c", f"features.{name}=false"]
    # The pinned model catalog can select CodeModeOnly independently of the
    # enabled flag. This official FeatureToml table leaves CodeMode disabled
    # while exposing only the image namespace directly, outside nested code.
    # Keep this a process override; the owned profile and its hash are unchanged.
    args += ["-c", 'features.code_mode={enabled=false,direct_only_tool_namespaces=["image_gen"]}']
    args += ["-c", "skills.include_instructions=false", "-c", "skills.bundled.enabled=false",
             "-c", "cloud.skills.enabled=false", "-c", "orchestrator.mcp.enabled=false"]
    return args


def child_environment(profile: Path) -> dict[str, str]:
    """A new child env; global process environment and saved credentials untouched."""
    env = {key: os.environ[key] for key in OS_KEYS if key in os.environ}
    env["CODEX_HOME"] = str(Path(profile).absolute())
    return env


def login_plan(app_root: Path, runtime: dict) -> dict:
    environment_check = compatibility_guard()
    if not environment_check["runtime_compatible"]:
        raise SetupError(environment_check["state"], environment_check["message"])
    expected = Path(app_root).resolve(strict=True) / "runtime/codex/bin/codex.exe"
    if (runtime.get("state") != "runtime_verified" or runtime.get("version") != VERSION
            or runtime.get("sha256") != PUBLIC_FILES["runtime/codex/bin/codex.exe"][1]
            or not isinstance(runtime.get("executable"), str) or Path(runtime["executable"]) != expected):
        raise SetupError("runtime_not_verified", "공식 실행 파일 검증이 먼저 필요합니다.")
    return {"state": "own_login_required", "command": [str(expected), "login"],
            "profile_relative": "local_private/codex_home", "automatic_login": False,
            "existing_credentials_reused": False, "new_oauth_scopes_allowed": False,
            "compatibility": environment_check,
            "messages": ["어머니가 공식 Codex 로그인 화면에서 본인 계정으로 직접 로그인합니다.",
                         "이 앱은 비밀번호·토큰을 읽거나 복사하지 않습니다.",
                         "새 권한 추가나 별도 API 결제가 필요하면 진행을 멈춥니다.",
                         "이미지 요청도 계정 사용량을 소비하며 추가 비용이나 무료 사용을 보장하지 않습니다.",
                         "Codex가 생성한 원본 PNG도 앱 전용 Codex 설정 폴더에 남습니다.",
                         "이 컴퓨터에서 본인 로그인과 이미지 준비 확인을 완료한 뒤 요청해 주세요."]}


class FakeOfficialLoginAdapter:
    """UI contract fixture only; never claims an account is connected."""
    def run(self, *, confirmed: bool, cancel_event, outcome: str = "success") -> dict:
        if not confirmed:
            return {"state": "confirmation_required", "authenticated_verified": False, "simulated": True}
        if cancel_event.is_set() or outcome == "cancelled":
            state = "cancelled"
        elif outcome == "success":
            state = "simulated_login_completed"
        elif outcome in {"failed", "timed_out", "rights_expansion_required"}:
            state = outcome
        else:
            state = "failed"
        return {"state": state, "authenticated_verified": False, "simulated": True,
                "credentials_read": False, "credentials_copied": False, "model_calls": 0}
