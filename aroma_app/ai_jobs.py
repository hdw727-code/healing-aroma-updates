"""Explicitly consented local AI jobs. Network/model work is owned by the bridge.

No customer file is read here. SQLite stores metadata; result files live in
user_data/ai_jobs and are not included in the existing metadata-only DB ZIP.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import re
import threading
import uuid
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class AIJobError(Exception):
    def __init__(self, message, status=400, code="invalid_ai_job", details=None):
        super().__init__(message)
        self.status, self.code, self.details = status, code, details


class AIJobManager:
    ACTIVE = ("queued", "running", "cancelling")
    TERMINAL = ("done", "failed", "cancelled", "timed_out", "interrupted")
    FILE_TYPES = {".json": "application/json; charset=utf-8", ".txt": "text/plain; charset=utf-8",
                  ".html": "text/html; charset=utf-8", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                  ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".png": "image/png"}
    MAX_IMAGE_BYTES = 8 * 1024 * 1024

    def __init__(self, app_store, bridge=None, *, image_adapter=None):
        self.app = app_store
        if bridge is None:
            from codex_bridge import CodexBridge
            bridge = CodexBridge(self.app.root, data_root=self.app.data_dir,
                                 private_root=getattr(self.app, "private_root", None),
                                 profile_root=getattr(self.app, "image_profile_dir", None),
                                 runtime_root=getattr(self.app, "runtime_root", None))
        self.bridge = bridge
        self.image_adapter = image_adapter
        self.folder = self.app.data_dir / "ai_jobs"
        self.folder.mkdir(exist_ok=True)
        self.workers = {}
        self.ensure()
        self.recover_interrupted()

    def recover_interrupted(self):
        self.ensure()
        with self.app.lock, self.app.connect() as conn:
            conn.execute("UPDATE ai_jobs SET state='interrupted',updated_at=?,message=? WHERE state IN ('queued','running','cancelling')",
                         (_now(), "앱 시작 또는 복구로 이전 AI 작업을 중단 상태로 표시했습니다. 자동으로 다시 전송하지 않습니다."))

    def ensure(self):
        with self.app.lock, self.app.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS ai_jobs (
              id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
              preview_hash TEXT NOT NULL, snapshot_json TEXT NOT NULL, state TEXT NOT NULL,
              result_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              message TEXT NOT NULL)""")

    def status(self):
        result = dict(self.bridge.status())
        result.update({"jobs_supported": True, "max_parallel_jobs": 1,
                       "contacts_included_by_default": False, "file_upload_to_ai": False,
                       "result_files_in_database_backup": False, "result_files_in_full_backup": True,
                       "image": self.image_status()})
        return result

    def _image_worker(self):
        if self.image_adapter is None:
            try:
                from image_adapter import ImageAdapter
                self.image_adapter = ImageAdapter(self.app.root, data_root=self.app.data_dir,
                    private_root=self.app.private_root, profile_root=getattr(self.app, "image_profile_dir", None),
                    runtime_root=getattr(self.app, "runtime_root", None))
            except (ImportError, OSError, ValueError, TypeError):
                raise AIJobError("이미지 준비 모듈을 찾지 못했습니다. 앱 설치 안내를 확인해 주세요.",
                                 503, "ai_image_unavailable") from None
        return self.image_adapter

    def _worker_for(self, kind):
        return self._image_worker() if kind == "image" else self.bridge

    def image_status(self):
        try:
            status = dict(self._image_worker().status())
            status["image_ready"] = status.get("image_ready") is True
            return status
        except (AIJobError, ValueError, TypeError, OSError):
            return {"image_ready": False, "image_verified": False, "account_image_available": None,
                    "status": "unavailable", "message": "이미지 연결 준비가 필요합니다.", "automatic_login": False}

    def require_idle(self):
        with self.app.lock:
            controller = getattr(self.app, "_runtime_login_controller", None)
            if controller and controller.status().get("state") in ("starting", "waiting_user", "cancelling"):
                raise AIJobError("공식 로그인을 완료하거나 취소한 뒤 다시 시도해 주세요.",
                                 409, "runtime_login_busy")
            self.ensure()
            with self.app.connect() as conn:
                active = conn.execute("SELECT * FROM ai_jobs WHERE state IN ('queued','running','cancelling') LIMIT 1").fetchone()
                if active:
                    raise AIJobError("현재 AI 작업이 진행 중입니다. 완료하거나 취소한 뒤 준비해 주세요.",
                                     409, "ai_job_busy", {"job": self._public(active)})

    def preflight_image(self):
        with self.app.lock:
            self.require_idle()
            try:
                self._image_worker().preflight(cancel_event=None)
                status = self.image_status()
                if status.get("image_ready") is not True:
                    raise AIJobError("이미지 연결 준비를 완료하지 못했습니다. 로그인과 준비 상태를 확인해 주세요.",
                                     503, "ai_image_prepare_failed", {"image": status})
                return status
            except (ValueError, OSError, TypeError):
                raise AIJobError("이미지 연결 준비를 확인하지 못했습니다. 로그인과 준비 상태를 확인해 주세요.",
                                 503, "ai_image_prepare_failed") from None

    def preview(self, kind, payload):
        if not isinstance(payload, dict):
            raise AIJobError("AI 작업 입력을 확인해 주세요.")
        if any(key in payload for key in ("profile_root", "image_profile_dir", "private_root", "runtime_root", "codex_home", "api_key", "access_token", "auth")):
            raise AIJobError("프로필 경로와 계정 비밀정보는 이 요청으로 전달할 수 없습니다.", 400, "invalid_ai_profile_input")
        try:
            snapshot = self._worker_for(kind).preview(kind, payload)
        except ValueError as error:
            raise AIJobError(str(error), code=getattr(error, "code", "invalid_ai_job"))
        if not isinstance(snapshot, dict) or not re.fullmatch(r"[a-fA-F0-9]{64}", snapshot.get("preview_hash", "")):
            raise AIJobError("AI 전송 미리보기를 만들지 못했습니다. 입력을 다시 확인해 주세요.", 503, "ai_preview_unavailable")
        return snapshot

    def _safe_path(self, job_id, name):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id) or not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+\.(json|txt|html|docx|pptx|png)", name):
            return None
        folder = self.folder / job_id
        path = folder / name
        if self.folder.is_symlink() or self.folder.is_junction() or folder.is_symlink() or folder.is_junction() or path.is_symlink() or path.is_junction():
            return None
        if not path.resolve().is_relative_to(self.folder.resolve()):
            return None
        return path

    def _verified_image_bytes(self, path):
        # Check file ownership and the byte bound before opening PNG data.
        if path.stat().st_nlink != 1 or path.stat().st_size > self.MAX_IMAGE_BYTES:
            raise ValueError("invalid image result size or ownership")
        with path.open("rb") as stream:
            data = stream.read(self.MAX_IMAGE_BYTES + 1)
        if len(data) > self.MAX_IMAGE_BYTES:
            raise ValueError("image result too large")
        from image_adapter import validate_png_bytes
        validate_png_bytes(data)
        return data

    def _public(self, row):
        result = json.loads(row["result_json"])
        files = []
        for entry in result.get("result_files", []):
            name = entry.get("name")
            path = self._safe_path(row["id"], name)
            missing = path is None or not path.is_file()
            files.append({"name": name, "mime": self.FILE_TYPES.get(Path(name).suffix if name else "", "application/octet-stream"),
                          "missing": missing, "url": None if missing else f"/api/ai/jobs/{row['id']}/files/{name}"})
        snapshot = json.loads(row["snapshot_json"])
        error = result.get("error") or {}
        event_types = (result.get("safe_events") or {}).get("event_types", [])
        model_used = row["state"] == "done" or any(event in event_types for event in ("turn.started", "turn.completed", "turn.failed")) or bool(result.get("usage"))
        return {"id": row["id"], "kind": row["kind"], "state": row["state"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "preview_hash": row["preview_hash"], "preview_text": snapshot.get("preview_text", ""),
                "include_contacts": snapshot.get("include_contacts", False), "result_files": files,
                "response": result.get("response"), "message": row["message"],
                "error_code": error.get("code"), "events": result.get("safe_events", {}),
                "usage": result.get("usage", {}), "model": result.get("model"),
                "provider": result.get("provider"), "sdk_model_run": result.get("sdk_model_run", False),
                "image": result.get("image"), "submitted_turns": result.get("submitted_turns"),
                "ai_used": result.get("ai_used") if type(result.get("ai_used")) is bool else (True if model_used else (False if row["state"] == "queued" else None)),
                "subscription_usage_may_be_consumed": row["state"] != "queued",
                "cost_zero_guaranteed": False, "auto_retry": False}

    def get(self, job_id):
        with self.app.lock:
            self.ensure()
            with self.app.connect() as conn:
                row = conn.execute("SELECT * FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
                if row is None:
                    raise AIJobError("이 AI 작업을 찾지 못했습니다. 작업 목록에서 다시 선택해 주세요.", 404, "ai_job_not_found")
                return self._public(row)

    def list_jobs(self):
        with self.app.lock:
            self.ensure()
            with self.app.connect() as conn:
                return [self._public(row) for row in conn.execute("SELECT * FROM ai_jobs ORDER BY created_at DESC,id DESC LIMIT 100")]

    def start(self, payload):
        if not isinstance(payload, dict) or payload.get("send_confirmed") is not True:
            raise AIJobError("AI로 보낼 내용을 살펴보고 이번 전송에 동의해 주세요.", 422, "ai_send_confirmation_required")
        request_id = payload.get("request_id")
        request_pattern = r"[A-Za-z0-9_-]{8,80}" if payload.get("kind") == "image" else r"[A-Za-z0-9_-]{1,120}"
        if not isinstance(request_id, str) or not re.fullmatch(request_pattern, request_id):
            raise AIJobError("AI 작업 요청 번호를 확인해 주세요. 다시 시작 버튼을 누르면 됩니다.")
        preview_payload = {key: payload.get(key) for key in ("fields", "include_fields", "include_contacts") if key in payload}
        forbidden = ("files", "file", "attachments", "reference_files", "reference", "source_files", "source_path", "path", "user_data", "data",
                     "profile_root", "image_profile_dir", "private_root", "runtime_root", "codex_home", "api_key", "access_token", "auth")
        preview_payload.update({key: payload[key] for key in forbidden if key in payload})
        snapshot = self.preview(payload.get("kind"), preview_payload)
        supplied_hash = payload.get("preview_hash")
        if not isinstance(supplied_hash, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", supplied_hash) or not hmac.compare_digest(snapshot["preview_hash"], supplied_hash):
            raise AIJobError("입력 내용이 미리보기 뒤에 바뀌었습니다. 전송 미리보기를 다시 확인해 주세요.", 409, "ai_preview_changed")
        with self.app.lock:
            self.ensure()
            with self.app.connect() as conn:
                row = conn.execute("SELECT * FROM ai_jobs WHERE request_id=?", (request_id,)).fetchone()
                if row:
                    if row["kind"] != payload.get("kind") or row["preview_hash"] != supplied_hash:
                        raise AIJobError("같은 작업 요청에 다른 내용이 들어왔습니다. 새 작업으로 시작해 주세요.", 409, "ai_request_conflict")
                    return self._public(row)
                controller = getattr(self.app, "_runtime_login_controller", None)
                if controller and controller.status().get("state") in ("starting", "waiting_user", "cancelling"):
                    raise AIJobError("공식 로그인을 완료하거나 취소한 뒤 AI 작업을 시작해 주세요.",
                                     409, "runtime_login_busy")
                active = conn.execute("SELECT * FROM ai_jobs WHERE state IN ('queued','running','cancelling') LIMIT 1").fetchone()
                if active:
                    raise AIJobError("AI가 한 작업을 진행하고 있습니다. 끝날 때까지 기다리거나 현재 작업을 중지해 주세요.", 409, "ai_job_busy", {"job": self._public(active)})
                selected = self._worker_for(payload.get("kind"))
                status = selected.status()
                available = status.get("image_ready") is True if payload.get("kind") == "image" else status.get("status") in ("configured", "ready")
                if not available:
                    raise AIJobError(status.get("message") or "이 컴퓨터의 실제 AI 연결을 먼저 준비해 주세요.", 503, "ai_not_configured", {"connection": status})
                job_id = uuid.uuid4().hex
                stamp = _now()
                snapshot = json.loads(_json(snapshot))
                snapshot["send_confirmed"] = True
                if payload.get("kind") == "image":
                    snapshot["request_id"] = request_id
                conn.execute("INSERT INTO ai_jobs VALUES (?,?,?,?,?,?,?,?,?,?)", (job_id, request_id, payload.get("kind"), supplied_hash,
                    _json(snapshot), "queued", "{}", stamp, stamp, "사용자가 확인한 내용으로 AI 작업을 준비하고 있습니다."))
            event = threading.Event()
            worker = threading.Thread(target=self._work, args=(job_id, snapshot, event), daemon=False, name=f"aroma-ai-{job_id[:8]}")
            self.workers[job_id] = (event, worker)
            worker.start()
            return self.get(job_id)

    def _set(self, job_id, state, message, result=None):
        with self.app.lock, self.app.connect() as conn:
            if result is None:
                conn.execute("UPDATE ai_jobs SET state=?,updated_at=?,message=? WHERE id=?", (state, _now(), message, job_id))
            else:
                conn.execute("UPDATE ai_jobs SET state=?,result_json=?,updated_at=?,message=? WHERE id=?", (state, _json(result), _now(), message, job_id))

    def _work(self, job_id, snapshot, event):
        try:
            if event.is_set():
                self._set(job_id, "cancelled", "AI 전송 전에 작업을 취소했습니다.")
                return
            self._set(job_id, "running", "실제 AI가 작업하고 있습니다. 입력 원본과 저장된 견적은 그대로 유지됩니다.")
            directory = self.folder / job_id
            directory.mkdir(exist_ok=False)
            result = self._worker_for(snapshot.get("kind")).run(snapshot, directory, event, on_event=None)
            if not isinstance(result, dict) or result.get("state") not in self.TERMINAL:
                raise ValueError("invalid bridge result")
            safe_files = []
            for entry in result.get("result_files", []):
                if not isinstance(entry, dict):
                    raise ValueError("invalid result file")
                path = self._safe_path(job_id, entry.get("name"))
                supplied_path = Path(entry.get("path", "")).resolve()
                if path is None or supplied_path != path.resolve() or not path.is_file():
                    raise ValueError("invalid result file path")
                if path.suffix == ".png":
                    if snapshot.get("kind") != "image":
                        raise ValueError("invalid image result")
                    self._verified_image_bytes(path)
                safe_files.append({"name": entry["name"], "mime": self.FILE_TYPES[path.suffix]})
            if snapshot.get("kind") == "image" and result["state"] == "done" and not any(file["name"].endswith(".png") for file in safe_files):
                raise ValueError("image result file missing")
            # Persist safe names, never worker command lines, secrets, or arbitrary paths.
            stored = {"state": result["state"], "result_files": safe_files,
                      "response": result.get("response"), "safe_events": result.get("safe_events", {}),
                      "usage": result.get("usage", {}), "model": result.get("model"), "error": result.get("error"),
                      "provider": result.get("provider"), "sdk_model_run": result.get("sdk_model_run", False),
                      "image": result.get("image"), "submitted_turns": result.get("submitted_turns"),
                      "ai_used": result.get("ai_used")}
            message = {"done": "AI 결과를 저장했습니다. 내용을 확인한 뒤 사용해 주세요.",
                       "cancelled": "AI 작업을 취소했습니다. 기존 자료는 그대로 남아 있습니다.",
                       "timed_out": "AI 응답을 기다리는 시간이 지나 작업을 중지했습니다. 자동으로 다시 전송하지 않습니다.",
                       "failed": (result.get("error") or {}).get("message") or "AI 작업을 완료하지 못했습니다. 기존 자료는 그대로 남아 있습니다.",
                       "interrupted": "AI 작업이 중단됐습니다. 자동으로 다시 전송하지 않습니다."}[result["state"]]
            self._set(job_id, result["state"], message, stored)
        except Exception:
            self._set(job_id, "failed", "AI 결과를 저장하지 못했습니다. 입력과 기존 자료는 그대로 남아 있습니다.",
                      {"result_files": [], "error": {"code": "ai_worker_failed", "message": "AI 작업 결과를 확인하지 못했습니다. 다시 시도하려면 전송 내용을 새로 확인해 주세요."}})
        finally:
            with self.app.lock:
                self.workers.pop(job_id, None)

    def cancel(self, job_id):
        with self.app.lock:
            job = self.get(job_id)
            if job["state"] in self.ACTIVE:
                pair = self.workers.get(job_id)
                if pair:
                    pair[0].set()
                    self._set(job_id, "cancelling", "현재 AI 작업의 중지를 요청했습니다. 기존 자료는 삭제하지 않습니다.")
                else:
                    self._set(job_id, "interrupted", "실행 중인 AI 작업을 찾지 못했습니다. 자동으로 다시 전송하지 않습니다.")
            return self.get(job_id)

    def cancel_all(self):
        with self.app.lock:
            for job_id, (event, worker) in list(self.workers.items()):
                event.set()
                self._set(job_id, "cancelling", "앱 종료로 AI 작업의 중지를 요청했습니다.")

    def close(self):
        self.cancel_all()
        with self.app.lock:
            workers = list(self.workers.values())
        for event, worker in workers:
            worker.join(timeout=5)

    def download(self, job_id, filename):
        job = self.get(job_id)
        file = next((entry for entry in job["result_files"] if entry["name"] == filename), None)
        if not file:
            raise AIJobError("이 작업의 결과 파일을 찾지 못했습니다.", 404, "ai_result_not_found")
        path = self._safe_path(job_id, filename)
        if file["missing"] or path is None or not path.is_file():
            raise AIJobError("AI 결과 파일의 사본이 없습니다. 별도 결과 폴더 백업을 확인해 주세요.", 410, "ai_result_missing")
        if path.suffix == ".png":
            try:
                self._verified_image_bytes(path)
            except (ValueError, OSError, ImportError):
                raise AIJobError("이미지 결과 파일을 안전하게 확인하지 못했습니다. 파일과 전체 백업을 확인해 주세요.",
                                 410, "ai_image_result_invalid") from None
        return path, file
