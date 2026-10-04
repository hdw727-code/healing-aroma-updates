"""Official app-server image transport with content-free safety preflight."""
from __future__ import annotations

import base64
import binascii
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import struct
import subprocess
import threading
import time
import zlib

try:
    from .runtime_setup import child_environment, discover_runtime, validate_owned_profile, fixed_app_server_command, compatibility_guard, DISABLED, SetupError
except ImportError:
    from runtime_setup import child_environment, discover_runtime, validate_owned_profile, fixed_app_server_command, compatibility_guard, DISABLED, SetupError

PNG_LIMIT = 8 * 1024 * 1024
PROVENANCE_LIMIT = 1024 * 1024
ENCODED_LIMIT = ((PNG_LIMIT + 2) // 3) * 4
RAW_LIMIT = 64 * 1024 * 1024
MAX_LINE = ENCODED_LIMIT + 64 * 1024
MAX_STREAM = 24 * 1024 * 1024
INSTRUCTIONS = (
    "Create one image using only the built-in image_gen.imagegen tool. "
    "Use the provided typed prompt and transparent_background setting. "
    "Do not read local files, use reference images, run commands, browse, use MCP, "
    "or request extra permissions. Return one completed image generation item."
)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<!\d)(?:\+82[- .]?(?:1[016789]|2|[3-6]\d)|0(?:1[016789]|2|[3-6]\d))[- .]?\d{3,4}[- .]?\d{4}(?!\d)")
FILE_PATH = re.compile(r"[A-Za-z]:[\\/]|\\\\[^\\]|file://|sediment://", re.I)
REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
BENIGN_ITEMS = {"agentMessage", "reasoning", "userMessage", "plan"}
TOOL_ITEMS = {"commandExecution", "fileChange", "mcpToolCall", "webSearch",
              "collabAgentToolCall", "dynamicToolCall", "hookPrompt", "hookExecution",
              "functionCall", "functionCallOutput", "computerUse", "browserUse"}
PASSIVE_METHODS = {"thread/started", "thread/status/changed", "turn/started",
                   "item/agentMessage/delta", "item/reasoning/textDelta",
                   "item/reasoning/summaryTextDelta", "item/reasoning/summaryPartAdded"}
KNOWN_METHODS = PASSIVE_METHODS | {"item/started", "item/completed", "turn/completed",
                                  "thread/tokenUsage/updated", "error", "warning", "configWarning"}


class ImageError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def classify_standard_warning(params, *, expected_thread_id=None):
    """Project official V2 WarningNotification to static codes, discard its text.

    EventMsg::Warning means the turn continues. Configuration overrides still
    fail closed. No message, path, or feature list is returned or persisted.
    """
    try:
        if (not isinstance(params, dict) or set(params) - {"threadId", "message"}
                or not isinstance(params.get("message"), str)
                or not 1 <= len(params["message"]) <= 8192):
            raise ImageError("protocol_invalid", "경고 알림의 형식이 다릅니다.")
        target = params.get("threadId")
        if target is not None:
            _safe_id(target)
            if expected_thread_id is not None and target != expected_thread_id:
                raise ImageError("event_scope_mismatch", "다른 작업의 경고 알림을 중단했습니다.")
        message = params["message"]
        if message.startswith("Codex is ignoring"):
            return {"code": "configuration_ignored", "continue": False}
        if message.startswith("Configured value for"):
            return {"code": "configuration_replaced", "continue": False}
        if message.startswith("Under-development features enabled:"):
            return {"code": "under_development_features", "continue": True}
        return {"code": "standard_warning_unclassified", "continue": True}
    finally:
        if isinstance(params, dict):
            params.clear()


def classify_config_warning(params):
    """Allow only the pinned server's successful local-DB recovery notice."""
    try:
        if (not isinstance(params, dict) or set(params) - {"summary", "details", "path", "range"}
                or not isinstance(params.get("summary"), str)
                or not 1 <= len(params["summary"]) <= 8192):
            raise ImageError("protocol_invalid", "설정 경고 알림의 형식이 다릅니다.")
        for key, bound in (("details", 8192), ("path", 4096)):
            value = params.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > bound):
                raise ImageError("protocol_invalid", "설정 경고 알림의 형식이 다릅니다.")
        location = params.get("range")
        if location is not None:
            if not isinstance(location, dict) or set(location) != {"start", "end"}:
                raise ImageError("protocol_invalid", "설정 경고 위치의 형식이 다릅니다.")
            for position in location.values():
                if (not isinstance(position, dict) or set(position) != {"line", "column"}
                        or any(type(v) is not int or not 1 <= v <= 10**9 for v in position.values())):
                    raise ImageError("protocol_invalid", "설정 경고 위치의 형식이 다릅니다.")
        if (params["summary"] == "Codex rebuilt its local database."
                and params.get("path") is None and location is None):
            return {"code": "local_database_rebuilt", "continue": True}
        return {"code": "config_warning_unclassified", "continue": False}
    finally:
        if isinstance(params, dict):
            params.clear()


def _record_warning(method, params, events, *, expected_thread_id=None):
    projection = (classify_standard_warning(params, expected_thread_id=expected_thread_id)
                  if method == "warning" else classify_config_warning(params))
    code = projection["code"]
    events["warning_codes"][code] = events["warning_codes"].get(code, 0) + 1
    counter = "nonfatal_warning_count" if projection["continue"] else "fatal_configuration_warning_count"
    events[counter] += 1
    if not projection["continue"]:
        raise ImageError("configuration_warning", "확인되지 않은 설정 변경 경고로 중단했습니다.")
    return projection


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def preview(payload: dict) -> dict:
    if not isinstance(payload, dict) or set(payload) - {"prompt", "transparent_background"}:
        raise ImageError("invalid_input", "직접 입력한 이미지 설명만 보낼 수 있습니다.")
    prompt = payload.get("prompt")
    transparent = payload.get("transparent_background", False)
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 2000 or type(transparent) is not bool:
        raise ImageError("invalid_input", "이미지 설명은 1~2,000자이며 배경 선택은 예/아니오입니다.")
    if FILE_PATH.search(prompt) or "\x00" in prompt:
        raise ImageError("invalid_input", "파일 경로나 파일 첨부를 보낼 수 없습니다.")
    safe_prompt = PHONE.sub("[전화번호 제외]", EMAIL.sub("[이메일 제외]", prompt.strip()))
    fields = {"prompt": safe_prompt, "transparent_background": transparent}
    transmission = {"kind": "image", "fields": fields, "instructions": INSTRUCTIONS,
                    "reference_images": [], "paid_api_fallback": False}
    checksum = hashlib.sha256(_canonical(transmission).encode("utf-8")).hexdigest()
    return {"kind": "image", "fields": fields, "preview_hash": checksum,
            "transmission_preview": transmission, "consent_required": True,
            "contact_information_removed": safe_prompt != prompt.strip(),
            "warnings": ["입력한 설명과 표시된 AI 지침이 OpenAI에 전송됩니다.",
                         "이미지는 Codex 사용량을 소모하며 텍스트보다 한도가 빨리 줄 수 있습니다.",
                         "원본 PNG는 앱 전용 Codex 설정 폴더에도 남습니다.",
                         "이 컴퓨터에서 이미지 준비 확인을 완료한 뒤 요청해 주세요."]}


def _png_failure(large: bool = False) -> ImageError:
    return ImageError("png_too_large" if large else "png_invalid",
                      "이미지 크기 제한을 초과했습니다." if large else "지원되는 정상 PNG 이미지가 아닙니다.")


def validate_png_base64(value: str) -> tuple[bytes, dict]:
    """Strict bounded PNG parsing, CRC and decoded-scanline validation; no library install."""
    if not isinstance(value, str):
        raise _png_failure()
    if len(value) > ENCODED_LIMIT:
        raise _png_failure(True)
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError, UnicodeError):
        raise _png_failure() from None
    if len(data) > PNG_LIMIT:
        raise _png_failure(True)
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise _png_failure()
    offset, seen_header, seen_idat, idat_finished, ended = 8, False, False, False, False
    width = height = color = 0
    palette = None
    compressed = bytearray()
    seen_ancillary = set()
    while offset < len(data):
        if ended or offset + 12 > len(data):
            raise _png_failure()
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        if not re.fullmatch(rb"[A-Za-z]{4}", kind) or kind[2] & 32 or length > PNG_LIMIT:
            raise _png_failure()
        stop = offset + 12 + length
        if stop > len(data):
            raise _png_failure()
        body = data[offset + 8:offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length:stop])[0]
        if zlib.crc32(kind + body) & 0xffffffff != expected_crc:
            raise _png_failure()
        offset = stop
        if not seen_header and kind != b"IHDR":
            raise _png_failure()
        if kind == b"IHDR":
            if seen_header or length != 13:
                raise _png_failure()
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", body)
            if not width or not height or width > 8192 or height > 8192 or width * height > 16_000_000:
                raise _png_failure(True)
            if depth != 8 or color not in (0, 2, 3, 4, 6) or compression or filtering or interlace:
                raise _png_failure()
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
            raw_expected = height * (1 + width * channels)
            if raw_expected > RAW_LIMIT:
                raise _png_failure(True)
            seen_header = True
        elif kind == b"PLTE":
            if palette is not None or seen_idat or color in (0, 4) or not length or length > 768 or length % 3:
                raise _png_failure()
            palette = length // 3
        elif kind == b"IDAT":
            if idat_finished or (color == 3 and palette is None):
                raise _png_failure()
            seen_idat = True
            compressed.extend(body)
        elif kind == b"IEND":
            if length or not seen_idat:
                raise _png_failure()
            ended = True
        elif kind == b"caBX":
            # C2PA PNG manifests use this exact ancillary/private chunk. Keep
            # bounded bytes opaque and unchanged; do not parse, fetch links,
            # validate signatures, or infer that its provenance is trustworthy.
            if (kind in seen_ancillary or seen_idat or not length
                    or length > PROVENANCE_LIMIT):
                raise _png_failure()
            seen_ancillary.add(kind)
        elif kind in (b"tRNS", b"sRGB", b"gAMA", b"pHYs", b"cHRM"):
            if kind in seen_ancillary or (kind != b"pHYs" and seen_idat):
                raise _png_failure()
            seen_ancillary.add(kind)
            if kind == b"tRNS":
                if color == 3 and (palette is None or not 1 <= length <= palette):
                    raise _png_failure()
                if color in (4, 6) or (color == 0 and length != 2) or (color == 2 and length != 6):
                    raise _png_failure()
            elif length != {b"sRGB": 1, b"gAMA": 4, b"pHYs": 9, b"cHRM": 32}[kind]:
                raise _png_failure()
            if seen_idat:
                idat_finished = True
        else:
            # APNG/compressed metadata/unknown chunks stay unsupported until reviewed.
            raise _png_failure()
    if not ended:
        raise _png_failure()
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(bytes(compressed), raw_expected + 1)
        if len(raw) != raw_expected or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
            raise _png_failure()
    except zlib.error:
        raise _png_failure() from None
    stride = width * channels
    previous = bytes(stride)
    for row_index in range(height):
        start = row_index * (stride + 1)
        row_filter = raw[start]
        if row_filter > 4:
            raise _png_failure()
        if color == 3:
            # Indexed PNG indices must reference an actual PLTE entry after unfiltering.
            row = bytearray(raw[start + 1:start + 1 + stride])
            for x in range(stride):
                left, above = row[x - 1] if x else 0, previous[x]
                upper_left = previous[x - 1] if x else 0
                if row_filter == 1:
                    predictor = left
                elif row_filter == 2:
                    predictor = above
                elif row_filter == 3:
                    predictor = (left + above) // 2
                elif row_filter == 4:
                    prediction = left + above - upper_left
                    distances = (abs(prediction - left), abs(prediction - above), abs(prediction - upper_left))
                    predictor = (left, above, upper_left)[distances.index(min(distances))]
                else:
                    predictor = 0
                row[x] = (row[x] + predictor) & 255
                if row[x] >= palette:
                    raise _png_failure()
            previous = row
    return data, {"width": width, "height": height, "bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(), "mime": "image/png"}


class _OwnedProcessTree:
    def __init__(self, process: subprocess.Popen):
        self.handle = None
        if os.name != "nt":
            return
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel = kernel
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Limits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.CreateJobObjectW(None, None)
        limits = Limits()
        limits.BasicLimitInformation.LimitFlags = 0x2000
        if handle and kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) and kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(int(process._handle))):
            self.handle = handle
        else:
            if handle:
                kernel.CloseHandle(handle)
            process.kill()
            process.wait(timeout=3)
            raise ImageError("process_isolation_failed", "작업 프로세스를 안전하게 격리하지 못했습니다.")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class JsonRpcProcess:
    """Bounded stdio transport; fixed production argv must be supplied by an owner."""
    def __init__(self, command, *, cwd: Path, env: dict):
        self.command = list(command)
        self.cwd, self.env = cwd, env
        self.process = None
        self.messages = queue.Queue(maxsize=128)
        self.problem = None
        self.stdout_total = self.stderr_total = 0
        self.writer_lock = threading.Lock()

    def start(self):
        options = {"cwd": str(self.cwd), "env": self.env, "stdin": subprocess.PIPE,
                   "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "shell": False}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        self.process = subprocess.Popen(self.command, **options)
        self.tree = _OwnedProcessTree(self.process)
        self.readers = [threading.Thread(target=self._read_stdout, daemon=True),
                        threading.Thread(target=self._discard_stderr, daemon=True)]
        for reader in self.readers:
            reader.start()

    def _read_stdout(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_LINE + 1)
                if not line:
                    break
                self.stdout_total += len(line)
                if len(line) > MAX_LINE or self.stdout_total > MAX_STREAM:
                    self.problem = "protocol_overflow"
                    return
                value = json.loads(line.decode("utf-8", errors="strict"))
                if not isinstance(value, dict):
                    self.problem = "protocol_invalid"
                    return
                try:
                    self.messages.put(value, timeout=.2)
                except queue.Full:
                    self.problem = "protocol_overflow"
                    return
        except (UnicodeError, json.JSONDecodeError, OSError):
            self.problem = "protocol_invalid"

    def _discard_stderr(self):
        try:
            for data in iter(lambda: self.process.stderr.read(4096), b""):
                self.stderr_total += len(data)
                if self.stderr_total > 512 * 1024:
                    self.problem = "protocol_overflow"
                    return
        except OSError:
            pass

    def send(self, message: dict):
        encoded = (_canonical(message) + "\n").encode("utf-8")
        if len(encoded) > 24 * 1024:
            raise ImageError("invalid_input", "전송할 입력이 너무 큽니다.")
        try:
            with self.writer_lock:
                self.process.stdin.write(encoded)
                self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise ImageError("runtime_failed", "공식 실행 연결이 중단되었습니다.") from None

    def read(self, timeout=.05):
        if self.problem:
            raise ImageError(self.problem, "이미지 작업 응답을 안전하게 읽을 수 없습니다.")
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty:
            if self.process.poll() is not None:
                raise ImageError("runtime_failed", "이미지 작업이 완료되기 전에 연결이 종료되었습니다.")
            return None

    def close(self):
        if not self.process:
            return
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            self.tree.close()  # Only our Windows job and its own children.
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=3)
        self.tree.close()
        for reader in self.readers:
            reader.join(timeout=1)
        for pipe in (self.process.stdout, self.process.stderr):
            pipe.close()


def validate_png_bytes(data: bytes) -> dict:
    if not isinstance(data, bytes) or len(data) > PNG_LIMIT:
        raise _png_failure(True)
    return validate_png_base64(base64.b64encode(data).decode("ascii"))[1]


def project_effective_config(response: dict) -> dict:
    """Content-free safety projection. Never inspect nested MCP/env/header values.

    Official CLI loads its app-owned home, system and managed configuration. The
    incoming effective configuration lives in memory only; no raw payload is
    logged, returned to HTTP, persisted, or passed to event callbacks.
    """
    try:
        config = response.get("config")
        if not isinstance(config, dict) or response.get("layers") is not None:
            raise ImageError("policy_shape_unknown", "공식 설정 응답 형식을 확인하지 못했습니다.")
        mcp = config.get("mcp_servers", {})
        if not isinstance(mcp, dict):
            raise ImageError("policy_shape_unknown", "MCP 선언 형식이 잘못되었습니다.")
        # A nonzero map fails without iterating its keys or values.
        mcp_count = len(mcp)
        if mcp_count:
            raise ImageError("external_mcp_declared", "외부 MCP 선언이 있어 이미지 실행을 중단했습니다.")
        features = config.get("features")
        if not isinstance(features, dict) or features.get("image_generation") is not True:
            raise ImageError("image_feature_disabled", "공식 이미지 도구가 켜져 있지 않습니다.")
        checked = {}
        for name in DISABLED + ("omit_app_server_notification_media",):
            if name == "code_mode":
                # Exact official FeatureToml form from our fixed process
                # override. No other direct namespace or configuration is
                # accepted. Preserve the disabled feature check separately.
                mode = features.get(name)
                if (not isinstance(mode, dict)
                        or set(mode) != {"enabled", "direct_only_tool_namespaces"}
                        or mode.get("enabled") is not False
                        or type(mode.get("direct_only_tool_namespaces")) is not list
                        or mode["direct_only_tool_namespaces"] != ["image_gen"]):
                    raise ImageError("image_tool_exposure_conflict", "이미지 도구의 직접 노출 설정을 확인하지 못했습니다.")
                checked[name] = False
                continue
            if features.get(name) is not False:
                raise ImageError("unsafe_feature_enabled", "이미지 외 도구 설정을 확인하지 못했습니다.")
            checked[name] = False
        standard = config.get("model_provider") in (None, "openai")
        providers = config.get("model_providers")
        if providers is not None and not isinstance(providers, dict):
            raise ImageError("policy_shape_unknown", "제공자 설정 형식이 잘못되었습니다.")
        # config/defaults.toml in the pinned official 0.160.0 release installs
        # this exact ChatGPT URL. Do not classify the packaged default as a
        # custom provider; every other explicit URL remains rejected.
        chatgpt_default = config.get("chatgpt_base_url") in (None, "https://chatgpt.com/backend-api/")
        if not standard or bool(providers) or config.get("openai_base_url") is not None or not chatgpt_default:
            raise ImageError("nonstandard_provider", "표준 OpenAI 제공자만 사용할 수 있습니다.")
        if config.get("cli_auth_credentials_store") != "keyring":
            raise ImageError("credential_store_conflict", "공식 keyring 저장 설정이 필요합니다.")
        if (config.get("approval_policy") != "never" or config.get("sandbox_mode") != "read-only"
                or config.get("web_search") != "disabled" or config.get("project_doc_max_bytes") != 0):
            raise ImageError("policy_conflict", "읽기 전용·승인 없음·웹 검색 차단 설정이 다릅니다.")
        for name in ("notify", "hooks", "model_instructions_file", "instructions", "developer_instructions"):
            value = config.get(name)
            if value is not None and value not in ([], {}):
                raise ImageError("unsafe_policy_present", "추가 실행 설정이 있어 중단했습니다.")
        for section, child in (("skills", "bundled"), ("cloud", "skills"), ("orchestrator", "mcp")):
            value = config.get(section)
            if not isinstance(value, dict) or not isinstance(value.get(child), dict) or value[child].get("enabled") is not False:
                raise ImageError("policy_conflict", "추가 도구 비활성 설정을 확인하지 못했습니다.")
        return {"effective_mcp_declaration_count": 0, "known_features": checked,
                "image_generation_enabled": True, "standard_provider": True,
                "image_namespace_direct_only": True,
                "official_keyring_store": True, "raw_config_saved": False,
                "nested_mcp_values_inspected": False}
    finally:
        # Drop all received raw settings, including headers/env and layer origins.
        response.clear()


def project_requirements(response: dict) -> dict:
    try:
        value = response.get("requirements")
        if value is None:
            return {"requirements_present": False, "policy_conflict": False}
        if not isinstance(value, dict):
            raise ImageError("policy_shape_unknown", "관리 정책 응답 형식이 다릅니다.")
        if value.get("cliAuthCredentialsStore") not in (None, "keyring") or value.get("modelProvider") not in (None, "openai"):
            raise ImageError("policy_conflict", "관리 정책의 인증 저장 또는 제공자가 다릅니다.")
        for key in ("modelProviders", "hooks", "additionalDeveloperInstructions", "chatgptBaseUrl"):
            if value.get(key) is not None:
                raise ImageError("unsafe_policy_present", "추가 관리 정책을 확인해야 합니다.")
        for key, allowed in (("allowedApprovalPolicies", "never"), ("allowedSandboxModes", "read-only"), ("allowedWebSearchModes", "disabled")):
            constraint = value.get(key)
            if constraint is not None and (not isinstance(constraint, list) or allowed not in constraint):
                raise ImageError("policy_conflict", "관리 정책과 이미지 실행 조건이 충돌합니다.")
        flags = value.get("featureRequirements")
        if flags is not None:
            if not isinstance(flags, dict):
                raise ImageError("policy_shape_unknown", "관리 기능 정책 형식이 다릅니다.")
            for name in DISABLED + ("omit_app_server_notification_media",):
                if flags.get(name) is not None and flags.get(name) is not False:
                    raise ImageError("policy_conflict", "관리 정책이 다른 도구를 요구합니다.")
            if flags.get("image_generation") is False:
                raise ImageError("policy_conflict", "관리 정책에서 이미지 도구를 금지했습니다.")
        return {"requirements_present": True, "policy_conflict": False}
    finally:
        response.clear()


BOOT_METHODS = {"remoteControl/status/changed", "skills/changed", "app/list/updated",
                "account/updated", "account/rateLimits/updated", "serverRequest/resolved"}
PASSIVE_METHODS |= {"turn/plan/updated", "item/reasoning/rawContentDelta"}
KNOWN_METHODS |= PASSIVE_METHODS | BOOT_METHODS


class ImageAdapter:
    """Production transport; setup/preflight is separate from one consented turn.

    Constructor inputs come from trusted local application wiring, never HTTP.
    This module does not issue login codes or read/copy account credentials.
    AIJobManager owns global job serialization and request-id deduplication.
    """
    def __init__(self, app_root: Path, *, data_root=None, private_root=None, profile_root=None,
                 runtime_registry=None, transport_factory=None, timeout_seconds=180, runtime_root=None):
        self.app_root = Path(app_root).resolve(strict=True)
        self.runtime_root = Path(runtime_root).resolve(strict=True) if runtime_root is not None else self.app_root
        self.data_root = Path(data_root).absolute() if data_root is not None else self.app_root / "user_data"
        self.private_root = Path(private_root).absolute() if private_root is not None else self.app_root / "local_private"
        self.profile_root = Path(profile_root).absolute() if profile_root is not None else self.private_root / "codex_home"
        self.runtime_registry = runtime_registry
        self.transport_factory = transport_factory
        self.timeout_seconds = max(.1, min(180, float(timeout_seconds)))
        self._setup = None
        self._image_verified = False
        self._lock = threading.Lock()
        self._early = []

    def _runtime(self):
        registry = self.runtime_registry
        if callable(registry):
            registry = registry()
        if registry is None:
            registry = discover_runtime(self.runtime_root)
        if not isinstance(registry, dict) or registry.get("state") != "runtime_verified":
            raise ImageError("runtime_not_verified", "공식 런타임 검증이 필요합니다.")
        if self.transport_factory is None:
            compatibility = compatibility_guard()
            if compatibility.get("runtime_compatible") is not True:
                raise ImageError(compatibility.get("state", "unsupported_runtime"), compatibility.get("message", "실행 환경 확인이 필요합니다."))
            expected = self.runtime_root / "runtime/codex/bin/codex.exe"
            if Path(registry.get("executable", "")) != expected:
                raise ImageError("runtime_not_verified", "앱에 포함된 고정 런타임만 사용할 수 있습니다.")
            fixed_app_server_command(registry)
        return dict(registry)

    def _profile(self):
        value = validate_owned_profile(self.app_root, self.private_root, profile_root=self.profile_root, data_root=self.data_root)
        if value.get("state") != "profile_verified":
            raise ImageError(value.get("state", "profile_not_owned"), value.get("message", "앱 전용 설정이 필요합니다."))
        return value

    def status(self):
        with self._lock:
            setup = dict(self._setup or {})
        return {"state": setup.get("state", "setup_verification_required"),
                "image_ready": setup.get("state") == "preflight_verified",
                "image_verified": self._image_verified,
                "account_image_available": True if self._image_verified else None,
                "login_state": "managed_by_official_cli", "authenticated_verified": False,
                "simulated": self.transport_factory is not None, "account_read": False,
                "sdk_model_run": False, "paid_api_fallback": False,
                "preflight": {k: v for k, v in setup.items() if k not in {"profile", "executable"}},
                "message": "이미지 생성은 보내기 동의 후 실행합니다. 실제 지원은 첫 이미지 성공 후 확인됩니다."}

    def preview(self, kind, payload):
        if kind != "image" or not isinstance(payload, dict) or set(payload) - {"fields", "include_fields", "include_contacts"}:
            raise ImageError("invalid_input", "이미지 설명만 보낼 수 있습니다.")
        if payload.get("include_contacts", False) is not False:
            raise ImageError("invalid_input", "이미지 요청에 연락처를 포함하지 않습니다.")
        fields = payload.get("fields")
        selected = payload.get("include_fields", ["prompt", "transparent_background"])
        if not isinstance(fields, dict) or not isinstance(selected, list) or any(not isinstance(k, str) for k in selected) or set(selected) - {"prompt", "transparent_background"} or "prompt" not in selected:
            raise ImageError("invalid_input", "보낼 이미지 설명을 선택해 주세요.")
        if set(fields) - {"prompt", "transparent_background"}:
            raise ImageError("invalid_input", "파일이나 추가 자료를 보낼 수 없습니다.")
        value = preview({k: v for k, v in fields.items() if k in selected})
        value["warnings"][-1] = "실제 이미지 지원은 첫 생성 성공 후 확인됩니다. 별도 유료 API로 전환하지 않습니다."
        value.update(preview_text=_canonical(value["transmission_preview"]), include_contacts=False)
        return value

    def _new_transport(self, work_dir, profile):
        if self.transport_factory is not None:
            return self.transport_factory(work_dir, profile)
        return JsonRpcProcess(fixed_app_server_command(self._runtime()), cwd=work_dir, env=child_environment(profile))

    def _check_cancel(self, cancelled, deadline):
        if cancelled.is_set():
            raise ImageError("cancelled", "이미지 작업을 취소했습니다.")
        if time.monotonic() > deadline:
            raise ImageError("timed_out", "이미지 작업 제한 시간을 초과했습니다.")

    def _rpc(self, transport, number, method, params, cancelled, deadline, events):
        transport.send({"id": number, "method": method, "params": params})
        if method == "turn/start":
            events["submitted_turns"] = 1
        while True:
            self._check_cancel(cancelled, deadline)
            message = transport.read()
            if message is None:
                continue
            if "method" in message and "id" in message:
                transport.send({"id": message["id"], "error": {"code": -32601, "message": "Not permitted by image client"}})
                events["approval_requests_denied"] += 1
                raise ImageError("approval_denied", "추가 권한·외부 도구 요청을 거부했습니다.")
            if "id" in message:
                if message["id"] != number or "error" in message or not isinstance(message.get("result"), dict):
                    raise ImageError("rpc_failed", "공식 런타임 응답을 확인하지 못했습니다.")
                return message["result"]
            if number in (10, 11) and message.get("method") not in BOOT_METHODS:
                if len(self._early) >= 64:
                    raise ImageError("protocol_overflow", "초기 작업 이벤트가 너무 많습니다.")
                self._early.append(message)
            else:
                self._boot_notification(message, events)

    @staticmethod
    def _boot_notification(message, events):
        method = message.get("method")
        if method in {"warning", "configWarning"}:
            events["methods"][method] = events["methods"].get(method, 0) + 1
            _record_warning(method, message.get("params"), events)
            return
        if method not in BOOT_METHODS:
            if method in {"warning", "configWarning", "deprecationNotice", "guardianWarning", "error"}:
                raise ImageError("configuration_warning", "공식 실행 설정 경고로 중단했습니다.")
            events["unknown_count"] += 1
            raise ImageError("unknown_event", "알 수 없는 초기 실행 이벤트로 중단했습니다.")
        # Exact official V2 method allowlist; payload (including account data) is discarded.
        events["methods"][method] = events["methods"].get(method, 0) + 1

    def _boot(self, transport, work_dir, cancelled, deadline, events):
        self._rpc(transport, 1, "initialize", {"clientInfo": {"name": "aroma_image", "version": "1.0"}, "capabilities": {"experimentalApi": True}}, cancelled, deadline, events)
        transport.send({"method": "initialized", "params": {}})
        requirements = project_requirements(self._rpc(transport, 2, "configRequirements/read", None, cancelled, deadline, events))
        projection = project_effective_config(self._rpc(transport, 3, "config/read", {"includeLayers": False, "cwd": str(work_dir)}, cancelled, deadline, events))
        provider = self._rpc(transport, 4, "modelProvider/capabilities/read", {}, cancelled, deadline, events)
        provider_ok = provider.get("imageGeneration") is True and provider.get("namespaceTools") is True
        provider.clear()
        if not provider_ok:
            raise ImageError("image_not_supported", "현재 공식 제공자가 이미지 도구를 지원하지 않습니다.")
        remote = self._rpc(transport, 5, "remoteControl/status/read", None, cancelled, deadline, events)
        disabled = remote.get("status") == "disabled"
        remote.clear()
        if not disabled:
            raise ImageError("remote_control_not_disabled", "원격 제어가 꺼져 있음을 확인하지 못했습니다.")
        # Drain startup notifications before any thread/model prewarming request.
        quiet_until = min(deadline, time.monotonic() + .15)
        while time.monotonic() < quiet_until:
            self._check_cancel(cancelled, deadline)
            message = transport.read(timeout=.03)
            if message is not None:
                if "id" in message:
                    raise ImageError("unexpected_response", "초기 설정 확인 후 추가 요청이 발생했습니다.")
                self._boot_notification(message, events)
        # Check our static non-secret config again immediately before thread creation.
        self._profile()
        return {"state": "preflight_verified", **requirements, **projection, "remote_disabled_verified": True,
                "provider_image_capability": True, "actual_image_verified": False,
                "mcp_status_requests": 0, "account_requests": 0, "model_requests": 0,
                "normal_home_read": False, "credentials_read": False,
                "config_projection_in_memory_only": True, "config_snapshot_atomic_guaranteed": False}

    @staticmethod
    def _events():
        return {"methods": {}, "item_types": {}, "image_completed_count": 0, "unknown_count": 0,
                "unauthorized_tool_count": 0, "approval_requests_denied": 0, "turn_completed": False,
                "saved_path_read": False, "raw_events_saved": False,
                "warning_codes": {}, "nonfatal_warning_count": 0,
                "fatal_configuration_warning_count": 0, "warning_messages_saved": False}

    def preflight(self, cancel_event=None):
        cancelled = cancel_event or threading.Event()
        transport = None
        value = {"state": "failed", "image_ready": False}
        try:
            self._runtime()
            profile = self._profile()
            # Only explicit local setup creates a scratch directory, never during GET status.
            work = self.private_root / "image_preflight_work"
            work.mkdir(parents=True, exist_ok=True)
            _safe_directory(work)
            marker = work / ".aroma-image-root"
            if not marker.exists():
                marker.open("xb").close()
            _safe_file(marker)
            transport = self._new_transport(work, Path(profile["profile"]))
            transport.start()
            value = self._boot(transport, work, cancelled, time.monotonic() + 15, self._events())
        except (ImageError, SetupError) as error:
            value = {"state": "failed", "image_ready": False, "error": {"code": error.code, "message": error.message}}
        except (OSError, ValueError, TypeError):
            value = {"state": "failed", "image_ready": False, "error": {"code": "preflight_failed", "message": "이미지 설정을 안전하게 확인하지 못했습니다."}}
        finally:
            if transport:
                transport.close()
                if transport.problem or not transport.messages.empty() or (transport.process is not None and transport.process.returncode != 0):
                    value = {"state": "failed", "image_ready": False, "error": {"code": "preflight_shutdown_failed", "message": "초기 설정 확인 후 실행이 정상 종료되지 않았습니다."}}
        with self._lock:
            self._setup = value
        return {**value, "image_ready": value.get("state") == "preflight_verified", "simulated": self.transport_factory is not None}

    def run(self, snapshot, job_dir, cancel_event, on_event=None):
        events = self._events()
        simulated = self.transport_factory is not None
        result = {"state": "failed", "error": None, "result_files": [], "safe_events": events,
                  "usage": None, "model": None, "ai_used": False, "simulated": simulated,
                  "provider": "codex_app_server_fixture" if simulated else "codex_app_server",
                  "sdk_model_run": False, "submitted_turns": 0, "cost_zero_guaranteed": False}
        transport = None
        thread_id = turn_id = image_id = None
        image_data = image_info = None
        owned_partial = None
        self._early = []
        try:
            if not isinstance(snapshot, dict) or snapshot.get("send_confirmed") is not True:
                raise ImageError("confirmation_required", "이번 이미지 전송에 동의해 주세요.")
            normalized = preview(snapshot.get("fields"))
            if snapshot.get("kind") != "image" or snapshot.get("preview_hash") != normalized["preview_hash"]:
                raise ImageError("preview_changed", "전송 내용이 달라 미리보기를 다시 확인해야 합니다.")
            if not isinstance(snapshot.get("request_id"), str) or not REQUEST_ID.fullmatch(snapshot["request_id"]):
                raise ImageError("invalid_input", "작업 식별자가 필요합니다.")
            self._runtime()
            profile = self._profile()
            jobs = self.data_root / "ai_jobs"
            job = Path(job_dir).absolute()
            if job.parent != jobs or not re.fullmatch(r"[a-f0-9]{32}", job.name):
                raise ImageError("unsafe_job_path", "앱 작업 폴더 안에서만 저장합니다.")
            for path in (self.data_root, jobs, job):
                _safe_directory(path)
            if any(job.iterdir()):
                raise ImageError("job_not_empty", "새 작업 폴더가 필요합니다.")
            work = job / "runtime_work"
            work.mkdir()
            (work / ".aroma-image-root").open("xb").close()
            deadline = time.monotonic() + self.timeout_seconds
            transport = self._new_transport(work, Path(profile["profile"]))
            transport.start()
            preflight = self._boot(transport, work, cancel_event, min(deadline, time.monotonic() + 15), events)
            result["preflight"] = preflight
            with self._lock:
                self._setup = preflight
            response = self._rpc(transport, 10, "thread/start", {"ephemeral": True, "cwd": str(work), "approvalPolicy": "never", "sandbox": "read-only", "baseInstructions": INSTRUCTIONS, "developerInstructions": "Only image generation is permitted. Never use references, files or extra tools.", "dynamicTools": [], "selectedCapabilityRoots": [], "environments": [], "experimentalRawEvents": False}, cancel_event, deadline, events)
            thread_id = _response_id(response, "thread")
            sandbox = response.get("sandbox")
            if (response.get("approvalPolicy") != "never" or not isinstance(sandbox, dict)
                    or sandbox.get("type") != "readOnly" or sandbox.get("networkAccess", False) is not False
                    or set(sandbox) - {"type", "networkAccess"}
                    or response.get("modelProvider") != "openai" or response.get("cwd") != str(work)):
                raise ImageError("policy_conflict", "새 작업의 실행 조건이 달라 중단했습니다.")
            sources = response.get("instructionSources", [])
            if not isinstance(sources, list) or sources:
                raise ImageError("external_instructions", "추가 로컬 지침이 읽힌 작업은 실행하지 않습니다.")
            model = response.get("model")
            if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,80}", model):
                result["model"] = model
            response.clear()
            self._profile()
            response = self._rpc(transport, 11, "turn/start", {"threadId": thread_id, "environments": [], "approvalPolicy": "never", "input": [{"type": "text", "text": _canonical(normalized["transmission_preview"]), "textElements": []}]}, cancel_event, deadline, events)
            result["submitted_turns"] = 1
            result["ai_used"] = not simulated
            turn_id = _response_id(response, "turn")
            response.clear()
            completed_at = None
            while completed_at is None or time.monotonic() - completed_at < .2:
                self._check_cancel(cancel_event, deadline)
                message = self._early.pop(0) if self._early else transport.read()
                if message is None:
                    continue
                if "id" in message:
                    if "method" in message:
                        events["approval_requests_denied"] += 1
                        transport.send({"id": message["id"], "error": {"code": -32601, "message": "No additional tools allowed"}})
                    raise ImageError("unexpected_response", "허용하지 않은 요청·응답으로 중단했습니다.")
                method, params = message.get("method"), message.get("params")
                if method in BOOT_METHODS:
                    self._boot_notification(message, events)
                    continue
                if method not in KNOWN_METHODS or not isinstance(params, dict):
                    events["unknown_count"] += 1
                    raise ImageError("unknown_event", "알 수 없는 이미지 이벤트로 중단했습니다.")
                events["methods"][method] = events["methods"].get(method, 0) + 1
                if method in {"warning", "configWarning"}:
                    projection = _record_warning(method, params, events, expected_thread_id=thread_id)
                    if on_event:
                        on_event({"method": method, "warning_code": projection["code"],
                                  "nonfatal_warning_count": events["nonfatal_warning_count"]})
                    continue
                if params.get("threadId", thread_id) != thread_id or params.get("turnId", turn_id) != turn_id:
                    raise ImageError("event_scope_mismatch", "다른 작업의 이벤트로 중단했습니다.")
                if method == "error":
                    raise ImageError("runtime_failed", "공식 이미지 작업에서 오류가 발생했습니다.")
                if method == "thread/tokenUsage/updated":
                    token_usage = params.get("tokenUsage")
                    total = token_usage.get("total") if isinstance(token_usage, dict) else None
                    if not isinstance(total, dict):
                        raise ImageError("protocol_invalid", "사용량 형식이 잘못되었습니다.")
                    numbers = {k: total[k] for k in ("inputTokens", "outputTokens", "totalTokens") if type(total.get(k)) is int and 0 <= total[k] < 10**9}
                    result["usage"] = {"tokens": numbers, "actual": not simulated, "simulated": simulated}
                elif method in {"item/started", "item/completed"}:
                    item = params.get("item")
                    if not isinstance(item, dict):
                        raise ImageError("protocol_invalid", "이미지 항목 형식이 다릅니다.")
                    kind = item.get("type")
                    safe_kind = kind if kind in BENIGN_ITEMS | TOOL_ITEMS | {"imageGeneration"} else "unknown"
                    events["item_types"][safe_kind] = events["item_types"].get(safe_kind, 0) + 1
                    image_output = (kind == "functionCallOutput" and item.get("namespace") == "image_gen"
                                    and item.get("name") == "imagegen" and image_id is not None
                                    and item.get("id") == image_id and image_info is not None)
                    if kind in TOOL_ITEMS and not image_output:
                        events["unauthorized_tool_count"] += 1
                        raise ImageError("unauthorized_tool", "이미지 외 도구 사용을 발견하여 중단했습니다.")
                    if kind == "imageGeneration":
                        current = _safe_id(item.get("id"))
                        if image_id is not None and image_id != current:
                            raise ImageError("too_many_images", "한 작업에는 이미지 한 장만 허용합니다.")
                        image_id = current
                        if method == "item/completed":
                            failure = item.get("failure")
                            if isinstance(failure, dict) and failure.get("type") == "usageLimitExceeded":
                                raise ImageError("usage_limit", "Codex 이미지 사용 한도가 부족합니다. 자동 재시도하지 않습니다.")
                            if item.get("status") != "completed" or failure is not None:
                                raise ImageError("image_failed", "이미지 생성이 완료되지 않았습니다.")
                            if image_info is not None:
                                raise ImageError("duplicate_image", "중복 이미지 완료 이벤트로 중단했습니다.")
                            image_data, image_info = validate_png_base64(item.get("result"))
                            events["image_completed_count"] = 1
                    elif image_output:
                        events["allowed_image_function_outputs"] = events.get("allowed_image_function_outputs", 0) + 1
                    elif kind not in BENIGN_ITEMS:
                        events["unknown_count"] += 1
                        raise ImageError("unknown_event", "알 수 없는 이미지 항목으로 중단했습니다.")
                elif method == "turn/completed":
                    turn = params.get("turn")
                    if not isinstance(turn, dict) or turn.get("id") != turn_id or turn.get("status") != "completed" or turn.get("error") is not None:
                        raise ImageError("runtime_failed", "이미지 작업이 정상 완료되지 않았습니다.")
                    if completed_at is not None:
                        raise ImageError("duplicate_completion", "중복 작업 완료 이벤트입니다.")
                    events["turn_completed"] = True
                    completed_at = time.monotonic()
                elif method not in PASSIVE_METHODS:
                    raise ImageError("unknown_event", "지원하지 않는 이벤트입니다.")
                if on_event:
                    on_event({"method": method, "image_completed_count": events["image_completed_count"]})
            transport.close()
            if transport.problem or not transport.messages.empty() or (transport.process is not None and transport.process.returncode != 0):
                raise ImageError("late_event", "완료 후 추가 이벤트 또는 실행 오류가 있어 결과 저장을 중단했습니다.")
            transport = None
            self._check_cancel(cancel_event, deadline)
            self._profile()
            if image_info is None or not events["turn_completed"]:
                raise ImageError("image_missing", "완료된 PNG 이미지가 없습니다.")
            destination, partial = job / "generated.png", job / "generated.png.part"
            with partial.open("xb") as stream:
                identity = os.fstat(stream.fileno())
                owned_partial = (partial, identity.st_dev, identity.st_ino)
                stream.write(image_data)
                stream.flush()
                os.fsync(stream.fileno())
            if destination.exists():
                raise ImageError("job_not_empty", "기존 이미지 파일을 덮어쓰지 않습니다.")
            partial.rename(destination)  # Windows rename refuses an existing destination.
            owned_partial = None
            result.update(state="done", image=image_info, result_files=[{"name": "generated.png", "path": str(destination), "mime": "image/png"}])
            if not simulated:
                self._image_verified = True
        except (ImageError, SetupError) as error:
            result["state"] = error.code if error.code in {"cancelled", "timed_out"} else "failed"
            result["error"] = {"code": error.code, "message": error.message}
        except (OSError, ValueError, TypeError, KeyError):
            result["error"] = {"code": "runtime_or_response", "message": "이미지 작업을 안전하게 처리하지 못했습니다."}
        finally:
            self._early.clear()
            result["submitted_turns"] = events.get("submitted_turns", 0)
            result["ai_used"] = False if simulated or not result["submitted_turns"] else (True if events["turn_completed"] or result["usage"] else None)
            if transport:
                if thread_id and turn_id:
                    try:
                        transport.send({"id": 99, "method": "turn/interrupt", "params": {"threadId": thread_id, "turnId": turn_id}})
                    except (ImageError, OSError):
                        pass
                transport.close()
            if owned_partial is not None:
                partial, device, inode = owned_partial
                try:
                    identity = partial.lstat()
                    if (identity.st_dev, identity.st_ino) == (device, inode) and identity.st_nlink == 1 and not partial.is_symlink() and not getattr(identity, "st_file_attributes", 0) & 0x400:
                        partial.unlink()  # Only the incomplete exclusive-created file owned by this job.
                except OSError:
                    pass
        return result


def _safe_id(value):
    if not isinstance(value, str) or not value or len(value) > 128 or any(ord(c) < 32 for c in value):
        raise ImageError("protocol_invalid", "공식 작업 식별자 형식이 잘못되었습니다.")
    return value


def _response_id(response, field):
    value = response.get(field)
    if not isinstance(value, dict):
        raise ImageError("protocol_invalid", "공식 작업 응답 형식이 잘못되었습니다.")
    return _safe_id(value.get("id"))


def _safe_directory(path):
    if not path.is_dir() or path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
        raise ImageError("unsafe_job_path", "일반 작업 폴더만 사용할 수 있습니다.")
    for parent in path.parents:
        if parent.is_symlink() or getattr(parent.lstat(), "st_file_attributes", 0) & 0x400:
            raise ImageError("unsafe_job_path", "폴더 링크를 사용할 수 없습니다.")


def _safe_file(path):
    if not path.is_file() or path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
        raise ImageError("unsafe_job_path", "일반 앱 소유 파일만 사용할 수 있습니다.")
