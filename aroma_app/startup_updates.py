"""Nonblocking, metadata-only check of the already approved public feed.

Never downloads an archive or installs code. Installation is delegated to
the separately installed trusted supervisor after an explicit user action.
No customer fields, database, private runtime configuration or credentials.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import re
import threading
import time
from urllib.error import URLError

from startup_update_trusted.github_provider import GithubProvider, REPOSITORY
from startup_update_trusted.publisher_pin import load_publisher_pin
from startup_update_trusted.update_client import (
    ManifestVerifier, PayloadPolicy, UpdateError, MAX_MANIFEST_BYTES, read_json,
)

CURRENT_PUBLIC_VERSION = "1.1.0"
CURRENT_SEQUENCE = 3
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
# Exact installed launcher policy for the schema-2 release. Only manifest
# metadata is inspected; this checker cannot extract or execute any file.
CODE_PATHS = frozenset({
    'ai_jobs.py',
    'ai_native_exports.py',
    'backup_api.py',
    'codex_bridge.py',
    'commerce_http_transport.py',
    'commerce_migration.py',
    'commerce_schema.py',
    'commerce_store.py',
    'commerce_sync.py',
    'image_adapter.py',
    'local_import.py',
    'maintenance_gate.py',
    'native_exports.py',
    'native_signature.py',
    'png_exports.py',
    'public/app.js',
    'public/commerce.css',
    'public/commerce_ui.js',
    'public/full_backup_ui.js',
    'public/image_ui.js',
    'public/index.html',
    'public/native_review.js',
    'public/print.js',
    'public/promo_png.js',
    'public/styles.css',
    'public/update_notice.js',
    'public/update_ui.js',
    'quality.py',
    'runtime_login.py',
    'runtime_setup.py',
    'server.py',
    'site_contract_adapter.py',
    'startup_updates.py',
    'update_client.py',
    'whole_backup.py',
})


def version_tuple(value):
    match = VERSION.fullmatch(value) if isinstance(value, str) and len(value) <= 64 else None
    if not match:
        raise UpdateError("release_version", "Stable numeric release version required.")
    return tuple(map(int, match.groups()))


class MetadataVerifier:
    """Retain all frozen verification and report incompatible signed releases.

The frozen verifier rejects schema mismatch before later file metadata checks.
For a notice only, first authenticate the signed bytes, then run its *complete*
validation using the signed target schema. Actual schema compatibility remains
a separate strict condition and never grants permission to install/migrate.
"""
    def __init__(self, frozen_verifier):
        self.frozen = frozen_verifier

    def verify(self, envelope_data, *, current_sequence, current_schema, now=None):
        if not isinstance(envelope_data, bytes) or len(envelope_data) > MAX_MANIFEST_BYTES:
            raise UpdateError("manifest_size", "Manifest exceeds its bound.")
        envelope = read_json(envelope_data)
        if not isinstance(envelope, dict) or set(envelope) != {"algorithm", "payload_b64", "signature_b64"}:
            raise UpdateError("manifest_envelope", "Invalid signed envelope.")
        native = self.frozen.verifier
        if envelope["algorithm"] != native.algorithm:
            raise UpdateError("signature_algorithm", "Unsupported signature algorithm.")
        try:
            payload = base64.b64decode(envelope["payload_b64"], validate=True)
            signature = base64.b64decode(envelope["signature_b64"], validate=True)
        except (ValueError, TypeError):
            raise UpdateError("signature_encoding", "Invalid signature encoding.") from None
        if len(payload) > MAX_MANIFEST_BYTES or not native.verify(payload, signature):
            raise UpdateError("signature_invalid", "Publisher signature did not verify.")
        document = read_json(payload)
        schema = document.get("schema") if isinstance(document, dict) else None
        target = schema.get("target") if isinstance(schema, dict) else None
        if type(target) is not int or not 1 <= target <= 10000:
            raise UpdateError("schema_migration_required", "Unsupported schema metadata.")
        verified = self.frozen.verify(envelope_data, current_sequence=current_sequence,
                                      current_schema=target, now=now)
        version_tuple(verified["manifest"]["version"])
        return verified


class _TimeoutResponse:
    def __init__(self, response):
        self.response = response

    def __getattr__(self, name):
        return getattr(self.response, name)

    def read(self, count):
        try:
            return self.response.read(count)
        except TimeoutError:
            raise UpdateError("github_timeout", "Public update check timed out.") from None


class _TimeoutOpener:
    def __init__(self, opener):
        self.opener = opener

    def open(self, request, timeout):
        try:
            return _TimeoutResponse(self.opener.open(request, timeout=timeout))
        except TimeoutError:
            raise UpdateError("github_timeout", "Public update check timed out.") from None
        except URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise UpdateError("github_timeout", "Public update check timed out.") from None
            raise


class MetadataProvider(GithubProvider):
    """Frozen URL/TLS/redirect behavior with fixed socket-timeout reporting."""
    def __init__(self, **options):
        super().__init__(**options)
        self.opener = _TimeoutOpener(self.opener)


def production_backend():
    """Public pin only; no key generation or private/profile/config access."""
    public_key = load_publisher_pin()
    if public_key is None:
        raise UpdateError("publisher_pin_unavailable", "The approved public publisher pin is unavailable.")
    verifier = MetadataVerifier(ManifestVerifier(public_key, PayloadPolicy(code_paths=CODE_PATHS)))
    return MetadataProvider(timeout=5, total_timeout=15), verifier


MESSAGES = {
    "idle": "앱을 시작하면 승인된 업데이트 채널을 확인합니다.",
    "checking": "새 버전 정보를 확인 중입니다. 다른 작업을 계속할 수 있습니다.",
    "up_to_date": "현재보다 새로운 호환 버전이 없습니다.",
    "available": "검증된 새 버전이 있습니다. 업데이트 화면에서 내용을 확인하고 적용해 주세요.",
    "incompatible": "새 버전의 자료 구조가 현재 앱과 다릅니다. 새 설치와 자료 가져오기 안내를 확인해 주세요.",
    "offline": "업데이트 채널에 연결하지 못했습니다. 앱은 계속 사용할 수 있습니다.",
    "timeout": "업데이트 확인 시간이 초과되었습니다. 앱은 계속 사용할 수 있습니다.",
    "invalid": "업데이트 정보 검증에 실패했습니다. 설치하지 않습니다.",
    "recovery_required": "중단된 자료 복구를 먼저 확인해 주세요. 업데이트 확인은 보류합니다.",
}


class StartupUpdateChecker:
    def __init__(self, *, current_version="1.1.0", current_public_version=None,
                 current_schema=2, current_sequence=CURRENT_SEQUENCE, backend_factory=production_backend,
                 monotonic=time.monotonic, cooldown_seconds=30):
        if current_public_version is None:
            current_public_version = current_version if isinstance(current_version, str) else None
        version_tuple(current_public_version)
        if type(current_schema) is not int or current_schema < 1 or type(current_sequence) is not int or current_sequence < 0:
            raise ValueError("Invalid current update metadata")
        self.current_version, self.current_public_version = current_version, current_public_version
        self.current_schema, self.current_sequence = current_schema, current_sequence
        self._backend_factory, self._clock, self._cooldown = backend_factory, monotonic, cooldown_seconds
        self._lock = threading.RLock()
        self._thread = None
        self._last_started = None
        self._closed = False
        self._publisher_key_configured = False
        self._state, self._candidate, self._checked_at, self._error_code = "idle", None, None, None

    def status(self, *, recovery_required=False):
        with self._lock:
            state = "recovery_required" if recovery_required else self._state
            candidate = None if recovery_required or self._candidate is None else dict(self._candidate)
            return {"enabled": True, "automatic_check": True, "state": state, "status": state,
                "repository": REPOSITORY, "current_version": self.current_version,
                "current_public_version": self.current_public_version, "current_schema": self.current_schema,
                "current_sequence": self.current_sequence, "candidate": candidate,
                "update_available": bool(state == "available"), "apply_enabled": False,
                "apply_disabled_reason": "schema_incompatible" if state == "incompatible" else
                    "recovery_required" if recovery_required else "supervisor_not_attached",
                "manual_apply_only": True, "automatic_install": False,
                "publisher_key_configured": self._publisher_key_configured,
                "supervisor_connected": False, "metadata_only": True,
                "checked_at": self._checked_at, "error_code": None if recovery_required else self._error_code,
                "message": MESSAGES[state]}

    def start_check(self, *, recovery_required=False):
        # Caller can answer immediately; only the worker performs public fetches.
        with self._lock:
            if recovery_required or self._closed:
                return {**self.status(recovery_required=recovery_required), "started": False}
            if self._thread is not None and self._thread.is_alive():
                return {**self.status(), "started": False}
            now = self._clock()
            if self._last_started is not None and now - self._last_started < self._cooldown:
                return {**self.status(), "started": False}
            self._last_started = now
            self._state, self._candidate, self._error_code = "checking", None, None
            try:
                self._thread = threading.Thread(target=self._run, name="aroma-update-metadata", daemon=True)
                self._thread.start()
            except Exception:
                # Optional update metadata must not prevent the office server
                # from starting when the OS cannot create another worker.
                self._thread = None
                self._state, self._error_code = "invalid", "check_worker_unavailable"
                self._checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                return {**self.status(), "started": False}
            return {**self.status(), "started": True}

    def _run(self):
        provider = None
        try:
            provider, verifier = self._backend_factory()
            with self._lock:
                self._publisher_key_configured = True
            provider.begin_check()
            discovery = provider.discover()
            # Always fully authenticate even a replay/same/older release.
            # Acceptance requires sequence > current below. Passing -1 only
            # permits the frozen verifier to validate metadata without applying.
            _, verified = provider.manifest(discovery, verifier, current_sequence=-1,
                                             current_schema=self.current_schema)
            doc = verified["manifest"]
            schema = doc["schema"]
            compatible = (schema["migration"] is False and schema["target"] == self.current_schema
                          and schema["min"] <= self.current_schema <= schema["max"])
            newer_version = version_tuple(doc["version"]) > version_tuple(self.current_public_version)
            newer_sequence = doc["sequence"] > self.current_sequence
            candidate = {"version": doc["version"], "sequence": doc["sequence"],
                "schema_target": schema["target"], "compatible": compatible,
                "newer": bool(newer_version and newer_sequence)}
            state = "incompatible" if not compatible else "available" if candidate["newer"] else "up_to_date"
            error = "release_replay" if newer_version and not newer_sequence else None
        except UpdateError as exc:
            code = exc.code
            state = "timeout" if code == "github_timeout" else "offline" if code in (
                "github_unavailable", "github_no_release", "github_response") else "invalid"
            # Known codes only, never provider/server reasons or payload strings.
            error = code if isinstance(code, str) and code in SAFE_CODES else "update_invalid"
            candidate = None
        except Exception:
            # No exception text/URL/key/server body is exposed or logged.
            state, candidate, error = "invalid", None, "verification_unavailable"
        finally:
            if provider is not None:
                try:
                    provider.end_check()
                except Exception:
                    pass
        with self._lock:
            if not self._closed:
                self._state, self._candidate, self._error_code = state, candidate, error
                self._checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def wait(self, timeout=2):
        """Synthetic tests/explicit teardown only; startup never calls this."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.status()

    def close(self):
        with self._lock:
            self._closed = True


SAFE_CODES = frozenset({"github_timeout", "github_unavailable", "github_no_release", "github_response",
    "github_url", "github_redirect", "github_size", "github_release", "github_asset", "github_manifest",
    "signature_invalid", "signature_algorithm", "signature_encoding", "manifest_size", "manifest_envelope",
    "manifest_canonical", "manifest_schema", "release_replay", "release_id", "release_version", "release_time",
    "schema_migration_required", "payload_metadata", "release_metadata", "file_count", "file_metadata",
    "file_not_allowed", "private_payload", "unsafe_path", "duplicate_file", "expanded_size",
    "publisher_pin_unavailable", "invalid_json", "duplicate_json_key"})
