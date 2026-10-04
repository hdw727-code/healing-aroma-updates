"""Offline aroma office server. Python standard library only; never calls an AI API.

Run: python server.py --port 8765
All customer data belongs to user_data; public and content are separate.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
import zipfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit, quote, parse_qs
from local_import import ImportStore, LocalImportError
from ai_jobs import AIJobManager, AIJobError
from backup_api import FullBackupAPI
from whole_backup import WholeBackupError

APP_ROOT = Path(__file__).resolve().parent
SCHEMA_VERSION = 1
APP_VERSION = "1.0.1"
MAX_BODY = 2 * 1024 * 1024
MAX_MONEY = 1_000_000_000


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class AppError(Exception):
    def __init__(self, message, status=400, code="invalid_input", details=None):
        super().__init__(message)
        self.status, self.code, self.details = status, code, details


def number(value, label, *, optional=False, minimum=0, maximum=MAX_MONEY):
    if value is None or value == "":
        if optional:
            return None
        raise AppError(f"{label}을 입력해 주세요.")
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value).strip()):
        raise AppError(f"{label}은 0 이상의 정수로 입력해 주세요.")
    result = int(str(value).strip())
    if not minimum <= result <= maximum:
        raise AppError(f"{label}은 {minimum:,} ~ {maximum:,} 범위로 입력해 주세요.")
    return result


def clean_text(value, limit=2000):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AppError("글자 입력란의 값을 확인해 주세요.")
    if len(value) > limit:
        raise AppError(f"입력 글자가 너무 깁니다. {limit:,}자 이내로 줄여 주세요.")
    return value.strip()


def contact_issues(form):
    values = {}
    for field, label, limit in (("customer_phone", "전화번호", 40), ("customer_email", "이메일 주소", 254)):
        value = form.get(field)
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise AppError(f"{label} 입력란에는 글자를 입력해 주세요.", details={"field": field})
        if len(value) > limit:
            raise AppError(f"{label}은 {limit}자 이내로 입력해 주세요.", details={"field": field})
        values[field] = value.strip()
    email = values["customer_email"]
    issues = []
    if email and (not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or re.search(r"[\x00-\x1f\x7f]", email)):
        issues.append("이메일 주소 형식을 확인해 주세요. 예: name@example.com. 미확정이면 초안으로 저장할 수 있습니다.")
    return issues


def rounded_div(numerator, denominator):
    return int((Decimal(numerator) / Decimal(denominator)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calculate_quote(form):
    """Integer KRW arithmetic; rates are explicit basis points, never inferred.

    per_person item = quantity * unit_price * participants.
    Included tax is split half-up to the nearest won; supply + tax == total.
    Incomplete forms get total=None so missing prices cannot appear as zero.
    """
    if not isinstance(form, dict):
        raise AppError("견적 입력 내용을 확인해 주세요.")
    issues, warnings, lines = [], [], []
    participants = number(form.get("participants"), "참여 인원", optional=True, minimum=1, maximum=100000)
    if participants is None:
        issues.append("참여 인원을 입력해 주세요.")
    items = form.get("items", [])
    if not isinstance(items, list) or len(items) > 100:
        raise AppError("견적 항목은 100개 이내로 입력해 주세요.")
    if not items:
        issues.append("재료 또는 서비스 항목을 한 개 이상 입력해 주세요.")
    missing = False
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise AppError("견적 항목 내용을 확인해 주세요.")
        name = clean_text(item.get("name"), 200)
        if not name:
            issues.append(f"{index + 1}번째 항목 이름을 입력해 주세요.")
        quantity = number(item.get("quantity"), f"{index + 1}번째 수량", optional=True, minimum=1, maximum=100000)
        unit_price = number(item.get("unit_price"), f"{index + 1}번째 단가", optional=True)
        scaling = item.get("scaling", "fixed")
        if scaling not in ("fixed", "per_person"):
            raise AppError("항목 계산 방법을 다시 선택해 주세요.")
        multiplier = participants if scaling == "per_person" else 1
        subtotal = None if None in (quantity, unit_price, multiplier) else quantity * unit_price * multiplier
        if subtotal is None:
            missing = True
            issues.append(f"{name or str(index + 1) + '번째 항목'}의 수량·단가·인원을 확인해 주세요.")
        elif subtotal > MAX_MONEY * 100:
            raise AppError("항목 금액이 너무 큽니다. 수량과 단가를 확인해 주세요.")
        confirmed_price = item.get("price_confirmed") is True or form.get("prices_confirmed") is True
        if unit_price is not None and not confirmed_price:
            warnings.append(f"{name or '항목'} 단가는 사용자가 입력한 값입니다. 기관과 확인해 주세요.")
        lines.append({"name": name, "quantity": quantity, "unit_price": unit_price,
                      "scaling": scaling, "unit": clean_text(item.get("unit"), 30),
                      "multiplier": multiplier, "subtotal": subtotal,
                      "price_confirmed": confirmed_price})
    # Optional additional fees are zero only when the caller explicitly omits them.
    teaching = number(form.get("teaching_fee", 0), "강사비", optional=True)
    travel = number(form.get("travel_fee", 0), "출장비", optional=True)
    discount = number(form.get("discount", 0), "할인", optional=True)
    if None in (teaching, travel, discount):
        missing = True
        issues.append("강사비·출장비·할인을 확인해 주세요. 없으면 0을 입력해 주세요.")
    material_sum = None if missing else sum(line["subtotal"] for line in lines)
    subtotal = None if material_sum is None else material_sum + teaching + travel - discount
    if subtotal is not None and subtotal < 0:
        raise AppError("할인이 전체 금액보다 큽니다. 할인 금액을 확인해 주세요.")
    tax_mode = form.get("tax_mode", "unconfirmed")
    if tax_mode not in ("none", "included", "added", "unconfirmed"):
        raise AppError("세금 계산 방법을 다시 선택해 주세요.")
    rate = number(form.get("tax_rate"), "세금 비율", optional=True, maximum=10000)
    tax_amount, supply, total = None, None, None
    if tax_mode == "unconfirmed":
        issues.append("세금 계산 방법을 기관 또는 담당자와 확인해 주세요.")
    elif tax_mode in ("included", "added") and rate is None:
        issues.append("확인한 세금 비율을 입력해 주세요.")
    elif subtotal is not None:
        if tax_mode == "none":
            supply, tax_amount, total = subtotal, 0, subtotal
        elif tax_mode == "added":
            supply = subtotal
            tax_amount = rounded_div(subtotal * rate, 10000)
            total = supply + tax_amount
        else:
            total = subtotal
            tax_amount = rounded_div(total * rate, 10000 + rate)
            supply = total - tax_amount
    if not clean_text(form.get("customer"), 200):
        issues.append("기관 또는 고객 이름을 입력해 주세요.")
    if not clean_text(form.get("program_id"), 200):
        issues.append("프로그램을 선택해 주세요.")
    if form.get("schedule_confirmed") is not True:
        warnings.append("날짜와 시간은 아직 확정되지 않은 초안입니다.")
    return {"lines": lines, "participants": participants, "materials_total": material_sum,
            "teaching_fee": teaching, "travel_fee": travel, "discount": discount,
            "subtotal": subtotal, "supply_amount": supply, "tax_amount": tax_amount,
            "total": total, "tax_mode": tax_mode, "tax_rate": rate,
            "issues": list(dict.fromkeys(issues)), "warnings": list(dict.fromkeys(warnings)),
            "ready": not issues, "calculation_version": 1, "rounding": "원 단위 반올림(0.5 올림)"}


def validate_plan(form):
    if not isinstance(form, dict):
        raise AppError("계획서 입력 내용을 확인해 주세요.")
    issues = []
    for field, label in (("customer", "기관 이름"), ("program_id", "프로그램")):
        if not clean_text(form.get(field), 200):
            issues.append(f"{label}을 입력해 주세요.")
    participants = number(form.get("participants"), "참여 인원", optional=True, minimum=1, maximum=100000)
    if participants is None:
        issues.append("참여 인원을 입력해 주세요.")
    for field in ("date", "time", "title", "purpose", "description", "notes", "duration"):
        clean_text(form.get(field), 5000)
    checklist = form.get("checklist", [])
    if not isinstance(checklist, list) or len(checklist) > 100:
        raise AppError("준비물 목록을 확인해 주세요.")
    for item in checklist:
        if isinstance(item, str):
            clean_text(item, 200)
        elif isinstance(item, dict):
            clean_text(item.get("name", item.get("label")), 200)
        else:
            raise AppError("준비물 이름을 확인해 주세요.")
    sessions = form.get("sessions", [])
    if not isinstance(sessions, list) or len(sessions) > 52:
        raise AppError("수업 회차는 52개 이내로 입력해 주세요.")
    for index, session in enumerate(sessions):
        if not isinstance(session, dict):
            raise AppError("회차 내용을 확인해 주세요.")
        clean_text(session.get("title"), 200)
        clean_text(session.get("description", session.get("goal")), 5000)
    if "session_count" in form:
        session_count = number(form.get("session_count"), "수업 회차", optional=True, minimum=1, maximum=52)
        if session_count is None or session_count != len(sessions):
            issues.append("총 회차와 입력한 회차 수를 맞춰 주세요.")
    if "duration_minutes" in form and number(form.get("duration_minutes"), "수업 시간", optional=True, minimum=1, maximum=1440) is None:
        issues.append("1회 수업 시간을 입력해 주세요.")
    warnings = []
    if form.get("schedule_confirmed") is not True:
        warnings.append("날짜와 시간은 미확정입니다. 예약 또는 승인으로 사용하지 마세요.")
    if form.get("content_confirmed") is not True:
        warnings.append("수업 내용과 안전 안내를 확인한 뒤 사용해 주세요.")
    return {"ready": not issues, "issues": issues, "warnings": warnings,
            "participants": participants, "generation_mode": "template", "ai_used": False}


class AppStore:
    def __init__(self, root=APP_ROOT, data_dir=None, content_dir=None, private_root=None, image_profile_dir=None, runtime_root=None):
        self.root = Path(root).resolve()
        # Stable launcher constructor only; no HTTP path or profile contents.
        self.runtime_root = Path(runtime_root).resolve() if runtime_root is not None else self.root
        self.content_dir = Path(content_dir).resolve() if content_dir is not None else self.root / "content"
        # Trusted launcher arguments only. No profile file probe/read/copy here.
        self.private_root = Path(os.path.abspath(private_root)) if private_root is not None else self.root / "local_private"
        # Trusted constructor reference only. No profile probe, copy, or HTTP path input.
        self.image_profile_dir = Path(os.path.abspath(image_profile_dir)) if image_profile_dir is not None else None
        self.data_dir = Path(data_dir).resolve() if data_dir else self.root / "user_data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir = self.data_dir / "backups"
        self.export_dir = self.data_dir / "exports"
        self.backup_dir.mkdir(exist_ok=True)
        self.export_dir.mkdir(exist_ok=True)
        self.db_path = self.data_dir / "office.sqlite3"
        self.lock = threading.RLock()
        self.csrf_token = secrets.token_urlsafe(32)
        self.admin_sessions = {}
        self.admin_failed_attempts = 0
        self.admin_retry_after = 0
        self.native_previews = {}
        # Do not migrate/initialize a potentially interrupted restore before
        # the manual recovery flow. Existing PIN can still authorize recovery.
        self._startup_recovery_mode = bool(self.recovery_pending())
        if not self._startup_recovery_mode:
            self._initialize()

    @contextmanager
    def restore_maintenance(self):
        # Every HTTP GET/POST holds this same reentrant lock through its entire
        # response/file stream. Entering here therefore waits for all readers
        # and writers; a restore handler does not wait on its own request.
        with self.lock:
            self.require_runtime_idle()
            yield

    def full_backup_api(self):
        with self.lock:
            api = getattr(self, "_full_backup_api", None)
            if api is None:
                api = FullBackupAPI(self, maintenance=self.restore_maintenance)
                self._full_backup_api = api
            return api

    def recovery_pending(self):
        return self.full_backup_api().service.pending_recovery()

    def require_no_recovery(self):
        if self.recovery_pending():
            raise AppError("중단된 자료 복구를 관리자 화면에서 먼저 확인해 주세요. 현재 자료를 보존하고 있습니다.",
                             409, "backup_recovery_required")

    def require_runtime_idle(self):
        # Do not instantiate a controller, inspect credentials, or start login.
        # Runtime setup is asynchronous even while HTTP requests hold our lock.
        controller = getattr(self, "_runtime_login_controller", None)
        if controller and controller.status().get("state") in ("starting", "waiting_user", "cancelling"):
            raise AppError("공식 로그인 작업이 진행 중입니다. 완료하거나 취소한 뒤 다시 시도해 주세요.",
                           409, "runtime_login_busy")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self):
        with self.lock, self.connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("이 데이터는 더 새 버전의 앱에서 만들어졌습니다. 앱 업데이트를 확인해 주세요.")
            if version and version < SCHEMA_VERSION:
                destination = self.backup_dir / f"before_migration_{int(time.time())}.sqlite3"
                with closing(sqlite3.connect(destination)) as backup:
                    conn.backup(backup)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                  id TEXT PRIMARY KEY, kind TEXT NOT NULL, revision INTEGER NOT NULL,
                  form_json TEXT NOT NULL, result_json TEXT NOT NULL, status TEXT NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, content_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS history (
                  document_id TEXT NOT NULL, revision INTEGER NOT NULL, form_json TEXT NOT NULL,
                  result_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                  content_json TEXT NOT NULL, PRIMARY KEY(document_id,revision));
                CREATE TABLE IF NOT EXISTS requests (
                  request_id TEXT PRIMARY KEY, operation TEXT NOT NULL, payload_hash TEXT NOT NULL,
                  response_json TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
            """)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("PRAGMA journal_mode = WAL")

    def content(self):
        result = {}
        for name in ("catalog", "files"):
            path = self.content_dir / f"{name}.json"
            if path.exists():
                try:
                    result[name] = json.loads(path.read_text(encoding="utf-8-sig"))
                except (ValueError, OSError):
                    raise AppError("프로그램 자료를 읽지 못했습니다. 아들 관리자에게 자료 파일 확인을 요청해 주세요.", 503, "content_unavailable")
            else:
                result[name] = []
        catalog = result["catalog"]
        files = result["files"]
        programs = catalog.get("programs", []) if isinstance(catalog, dict) else catalog
        file_list = files.get("files", []) if isinstance(files, dict) else files
        if not isinstance(programs, list) or not all(isinstance(program, dict) for program in programs):
            raise AppError("프로그램 목록 형식을 확인해 주세요. 사용자 저장 자료는 그대로 남아 있습니다.", 503, "content_unavailable")
        if not isinstance(file_list, list) or not all(isinstance(file, dict) for file in file_list):
            raise AppError("자료 색인 형식을 확인해 주세요.", 503, "content_unavailable")
        private_path = self.private_root / "files.local.json"
        if private_path.is_file():
            try:
                private = json.loads(private_path.read_text(encoding="utf-8-sig"))
                private_files = private.get("files", [])
                if isinstance(private_files, list) and all(isinstance(file, dict) for file in private_files):
                    file_list = file_list + private_files
                else:
                    raise ValueError("invalid file index")
            except (ValueError, OSError):
                raise AppError("이 컴퓨터의 자료 색인을 읽지 못했습니다. 관리자에게 확인을 요청해 주세요.", 503, "private_index_unavailable")
        return {"programs": programs, "files": file_list, "catalog_meta": catalog if isinstance(catalog, dict) else {}}

    def bootstrap(self):
        full = self.full_backup_api().status()
        if full["pending_recovery"]:
            # Do not instantiate importer/AI manager or expose mixed work rows.
            return {"programs": [], "files": [], "quotes": [], "plans": [], "ai_jobs": [],
                "csrf_token": self.csrf_token, "full_backup": full, "recovery_required": True,
                "settings": {"schema_version": SCHEMA_VERSION, "admin_configured": self.admin_configured(),
                    "storage": "local_only", "remote_update_enabled": False},
                "connection": {"mode": "template", "status": "recovery_required", "ai_used": False},
                "ai_connection": {"status": "recovery_required", "auth": "not_checked"}}
        content = self.content()
        importer = self.import_store()
        content["files"] = content["files"] + importer.list_files()
        return {**content, "full_backup": full, "recovery_required": False,
                "png_server_storage_ready": True, "native_ready": self.native_ready(),
                "native_capabilities": {"formats": {"ppt": "pptx", "report": "docx"},
                    "review_required": True, "full_response": True, "additional_model_call": False},
                "file_import_limits": importer.limits(), "ai_connection": self.ai_manager().status(),
                "ai_jobs": self.ai_manager().list_jobs(), "settings": {"schema_version": SCHEMA_VERSION,
                    "business_name": content["catalog_meta"].get("business_name", "아로마 업무"),
                    "data_folder": str(self.data_dir), "export_folder": str(self.export_dir),
                    "backup_folder": str(self.backup_dir), "admin_configured": self.admin_configured(),
                    "storage": "local_only", "remote_update_enabled": bool(getattr(self, "remote_update_enabled", False))},
                "quotes": self.list_documents("quotes"), "plans": self.list_documents("plans"),
                "backups": self.list_backups(), "csrf_token": self.csrf_token,
                "connection": {"mode": "template", "status": "not_connected", "ai_used": False,
                    "message": "현재는 저장된 서식으로 작성합니다. 실제 AI와 외부 서비스는 연결되지 않았습니다."}}

    def import_store(self):
        return ImportStore(self)

    def ai_manager(self):
        with self.lock:
            self.require_no_recovery()
            manager = getattr(self, "_ai_jobs_manager", None)
            if manager is None:
                manager = AIJobManager(self)
                self._ai_jobs_manager = manager
            return manager

    def native_ready(self):
        try:
            from ai_native_exports import prepare_ai_export
            from native_exports import EXAMPLES
            return callable(prepare_ai_export) and all((EXAMPLES / name).is_file()
                for name in ("lecture_example.pptx", "report_example.docx"))
        except ImportError:
            return False

    def runtime_login_controller(self):
        with self.lock:
            controller = getattr(self, "_runtime_login_controller", None)
            if controller is None:
                try:
                    from runtime_login import RuntimeLogin
                    controller = RuntimeLogin(self.root, private_root=self.private_root,
                                              profile_root=self.image_profile_dir, runtime_root=self.runtime_root)
                except (ImportError, OSError, ValueError, TypeError):
                    raise AppError("공식 로그인 준비 모듈을 찾지 못했습니다. 앱 설치 안내를 확인해 주세요.",
                                   503, "runtime_login_unavailable") from None
                self._runtime_login_controller = controller
            return controller

    @staticmethod
    def _login_public(value):
        if not isinstance(value, dict):
            raise AppError("로그인 준비 상태를 확인하지 못했습니다.", 503, "runtime_login_unavailable")
        # Device code is returned only by the explicit start response below.
        allowed = ("state", "message", "authenticated_verified", "automatic_login", "credentials_read",
                   "credentials_copied", "simulated", "auto_retry", "automatic_retry", "restart_required",
                   "model_calls", "device_code_persisted", "code_available", "error")
        result = {key: value[key] for key in allowed if key in value}
        result.setdefault("model_calls", 0)
        result.setdefault("device_code_persisted", False)
        result.setdefault("authenticated_verified", False)
        if result.get("state") in ("done", "failed", "cancelled", "timed_out"):
            result["restart_required"] = True
        return result

    def runtime_login_status(self):
        return {"login": self._login_public(self.runtime_login_controller().status()),
                "image": self.ai_manager().image_status()}

    def runtime_setup_idle(self):
        self.require_no_recovery()
        self.ai_manager().require_idle()
        status = self.runtime_login_controller().status()
        if status.get("state") in ("starting", "waiting_user", "cancelling"):
            raise AppError("공식 로그인 작업이 진행 중입니다. 완료하거나 취소한 뒤 다시 준비해 주세요.",
                           409, "runtime_login_busy")

    def runtime_login_start(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"send_confirmed"} or payload["send_confirmed"] is not True:
            raise AppError("공식 로그인 화면을 열기 전에 이번 연결에 동의해 주세요.", 422, "runtime_login_confirmation_required")
        self.runtime_setup_idle()
        try:
            value = self.runtime_login_controller().start(send_confirmed=True)
        except (ValueError, OSError, TypeError):
            raise AppError("공식 로그인 준비를 시작하지 못했습니다. 기존 계정 정보는 그대로 두었습니다.",
                           503, "runtime_login_failed") from None
        result = {"login": self._login_public(value), "image": self.ai_manager().image_status()}
        # Whitelist the official human device page, never a bearer token or arbitrary URL.
        device_url = value.get("device_url", value.get("verification_url"))
        device_code = value.get("device_code", value.get("user_code"))
        if device_url == "https://auth.openai.com/codex/device" and isinstance(device_code, str) and re.fullmatch(r"[A-Za-z0-9-]{4,32}", device_code):
            result["device_url"], result["device_code"] = device_url, device_code
            if type(value.get("expires_in_seconds")) is int and 0 < value["expires_in_seconds"] <= 900:
                result["expires_in_seconds"] = value["expires_in_seconds"]
        return result

    def runtime_login_cancel(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"confirm"} or payload["confirm"] is not True:
            raise AppError("로그인 준비를 취소하려면 확인해 주세요.", 422, "confirmation_required")
        return {"login": self._login_public(self.runtime_login_controller().cancel()),
                "image": self.ai_manager().image_status()}

    def runtime_image_preflight(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"send_confirmed"} or payload["send_confirmed"] is not True:
            raise AppError("이미지 연결 준비를 확인하기 전에 이번 연결에 동의해 주세요.", 422, "runtime_setup_confirmation_required")
        self.runtime_setup_idle()
        return {"image": self.ai_manager().preflight_image()}

    @staticmethod
    def _native_public_plan(plan):
        # Original raw AI response remains only in the local job store.
        # Never include it in downloadable metadata, preview HTML or responses.
        return {key: value for key, value in plan.items() if key != "original_response"}

    @staticmethod
    def _native_payload(payload, exporting=False):
        allowed = {"kind", "reviewed_response"}
        if exporting:
            allowed |= {"preview_hash", "review_confirmed", "request_id"}
        if (not isinstance(payload, dict) or set(payload) - allowed or
                not {"kind", "reviewed_response"}.issubset(payload)):
            raise AppError("전체 AI 초안과 검토 항목을 확인해 주세요. 원문·파일 경로는 서버의 작업 기록에서만 가져옵니다.",
                400, "invalid_native_request")
        if payload["kind"] not in ("ppt", "report") or not isinstance(payload["reviewed_response"], dict):
            raise AppError("PowerPoint 또는 Word와 전체 검토 내용을 확인해 주세요.", 400, "invalid_native_request")
        if exporting:
            if payload.get("review_confirmed") is not True:
                raise AppError("전체 내용과 확인사항·주의문구를 검토한 뒤 저장 확인을 눌러 주세요.",
                    422, "native_review_required")
            if not isinstance(payload.get("preview_hash"), str) or not re.fullmatch(r"[a-f0-9]{64}", payload["preview_hash"]):
                raise AppError("미리보기 확인값을 새로 받아 주세요.", 409, "native_preview_changed")
            if not isinstance(payload.get("request_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", payload["request_id"]):
                raise AppError("문서 저장 요청 번호를 확인해 주세요.", 400, "invalid_native_request_id")

    def _native_source(self, job_id):
        # Caller holds the same lock as full backup/restore and local writers.
        manager = self.ai_manager()
        job = manager.get(job_id)
        if job["state"] != "done":
            raise AppError("완료한 AI 작업을 선택해 주세요. 새 모델 요청은 하지 않았습니다.",
                409, "native_job_not_done")
        if job.get("kind") not in ("ppt", "report") or not isinstance(job.get("response"), dict):
            raise AppError("이 AI 작업은 전체 Word·PowerPoint 구조를 지원하지 않습니다.",
                422, "native_result_schema")
        folder = manager.folder / job_id
        if (manager.folder.is_symlink() or manager.folder.is_junction() or
                folder.is_symlink() or folder.is_junction() or not folder.is_dir() or
                folder.resolve().parent != manager.folder.resolve()):
            raise AppError("AI 작업 결과 폴더를 먼저 복구해 주세요. 문서를 새로 만들지 않았습니다.",
                410, "native_source_missing")
        original_file = next((file for file in job["result_files"] if file["name"] == "result.json"), None)
        source = manager._safe_path(job_id, "result.json")
        if (original_file is None or original_file["missing"] or source is None or
                source.is_symlink() or not source.is_file()):
            raise AppError("AI 원본 결과 파일을 먼저 복구해 주세요. 저장된 작업을 다시 실행하지 않았습니다.",
                410, "native_source_missing")
        return job, folder

    @staticmethod
    def _native_preview_hash(job_id, kind, plan):
        binding = {"job_id": job_id, "kind": kind,
            "original_response_sha256": plan["original_response_sha256"],
            "reviewed_response": plan["response"]}
        canonical = json.dumps(binding, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def native_preview(self, job_id, payload):
        self._native_payload(payload)
        if not self.native_ready():
            raise AppError("전체 AI 문서 템플릿을 찾지 못했습니다.", 503, "native_unavailable")
        from ai_native_exports import prepare_ai_export, render_ai_preview
        with self.lock:
            job, _ = self._native_source(job_id)
            try:
                plan = prepare_ai_export(payload["kind"], payload["reviewed_response"],
                    original_response=job["response"], source_job_id=job_id)
            except ValueError as error:
                raise AppError(str(error), 422, "native_result_schema")
            preview_hash = self._native_preview_hash(job_id, payload["kind"], plan)
            timestamp = time.monotonic()
            self.native_previews = {key: value for key, value in self.native_previews.items()
                if timestamp - value["issued_at"] < 1800}
            if len(self.native_previews) >= 128:
                self.native_previews.pop(next(iter(self.native_previews)))
            self.native_previews[preview_hash] = {"job_id": job_id, "kind": payload["kind"], "issued_at": timestamp}
            public = self._native_public_plan(plan)
            display = dict(plan)
            display["original_response"] = {"보존 위치": "로컬 AI 작업 기록", "작업 ID": job_id,
                "원문 내용 해시": plan["original_response_sha256"]}
            return {"plan": public, "preview_hash": preview_hash, "preview_html": render_ai_preview(display)}

    def native_export(self, job_id, payload):
        self._native_payload(payload, exporting=True)
        if not self.native_ready():
            raise AppError("전체 AI 문서 템플릿을 찾지 못했습니다.", 503, "native_unavailable")
        from ai_native_exports import prepare_ai_export, export_ai_response
        from native_exports import MIME
        with self.lock:
            job, folder = self._native_source(job_id)
            try:
                plan = prepare_ai_export(payload["kind"], payload["reviewed_response"],
                    original_response=job["response"], source_job_id=job_id)
            except ValueError as error:
                raise AppError(str(error), 422, "native_result_schema")
            expected = self._native_preview_hash(job_id, payload["kind"], plan)
            if not hmac.compare_digest(payload["preview_hash"], expected):
                raise AppError("초안이 미리보기 후 바뀌었습니다. 현재 전체 내용을 다시 검토해 주세요.",
                    409, "native_preview_changed")
            request = {"job_id": job_id, "kind": payload["kind"], "preview_hash": expected,
                "reviewed_response_sha256": plan["reviewed_response_sha256"]}
            extension = "pptx" if payload["kind"] == "ppt" else "docx"
            output = None
            owned_output = False
            try:
                with self.connect() as conn:
                    cached = self._idempotent(conn, payload["request_id"], "ai_native_export", request)
                    if cached:
                        path, _ = self.ai_manager().download(job_id, cached["filename"])
                        if not path.is_file():
                            raise AppError("기존 내보내기 파일의 사본이 없습니다. 새로 생성하지 않았습니다.",
                                410, "native_export_missing")
                        return cached
                    issued = self.native_previews.get(expected)
                    if (issued is None or issued["job_id"] != job_id or issued["kind"] != payload["kind"] or
                            time.monotonic() - issued["issued_at"] >= 1800):
                        raise AppError("이 앱에서 현재 전체 초안의 미리보기를 먼저 확인해 주세요.",
                            409, "native_preview_required")
                    if plan["blocking"]:
                        raise AppError("\n".join(plan["blocking"]), 422, "native_revision_required",
                            {"plan": self._native_public_plan(plan)})
                    try:
                        data, exported_plan = export_ai_response(payload["kind"], payload["reviewed_response"],
                            reviewed=True, original_response=job["response"], source_job_id=job_id)
                    except ValueError as error:
                        raise AppError(str(error), 422, "native_revision_required")
                    filename = "native_" + uuid.uuid4().hex + "." + extension
                    output = folder / filename
                    if self.ai_manager()._safe_path(job_id, filename) != output:
                        raise AppError("문서 저장 경로를 안전하게 확인하지 못했습니다.", 409, "native_path_changed")
                    with output.open("xb") as handle:
                        owned_output = True
                        handle.write(data); handle.flush(); os.fsync(handle.fileno())
                    row = conn.execute("SELECT state,result_json FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
                    if row is None or row["state"] != "done":
                        raise AppError("원본 AI 작업 상태가 바뀌었습니다. 문서를 다시 확인해 주세요.",
                            409, "native_job_changed")
                    result = json.loads(row["result_json"])
                    result.setdefault("result_files", []).append({"name": filename, "mime": MIME[extension],
                        "sha256": hashlib.sha256(data).hexdigest(), "origin": "reviewed_ai_native"})
                    conn.execute("UPDATE ai_jobs SET result_json=?,updated_at=? WHERE id=? AND state='done'",
                        (dump(result), now(), job_id))
                    response = {"url": f"/api/ai/jobs/{job_id}/files/{filename}", "filename": filename,
                        "mime_type": MIME[extension], "plan": self._native_public_plan(exported_plan),
                        "job_id": job_id, "format": extension, "ai_used": True,
                        "content_origin": "ai_generated", "additional_model_call": False}
                    self._remember(conn, payload["request_id"], "ai_native_export", request, response)
                return response
            except Exception:
                if owned_output and output is not None:
                    output.unlink(missing_ok=True)
                raise

    def files_payload(self):
        importer = self.import_store()
        return {"files": self.content()["files"] + importer.list_files(), "file_import_limits": importer.limits()}

    def program_snapshot(self, program_id):
        for program in self.content()["programs"]:
            if str(program.get("id", "")) == str(program_id):
                return json.loads(dump(program))
        return {"id": program_id, "title": "미확인 프로그램", "approval_status": "unverified"}

    def _document(self, row):
        return {"id": row["id"], "kind": row["kind"], "revision": row["revision"],
                "form": json.loads(row["form_json"]), "result": json.loads(row["result_json"]),
                "status": row["status"], "created_at": row["created_at"], "updated_at": row["updated_at"],
                "program_snapshot": json.loads(row["content_json"])}

    def get_document(self, kind, doc_id):
        with self.lock, self.connect() as conn:
            row = conn.execute("SELECT * FROM documents WHERE id=? AND kind=?", (doc_id, kind)).fetchone()
            if row is None:
                raise AppError("저장한 문서를 찾지 못했습니다. 저장 목록에서 다시 선택해 주세요.", 404, "not_found")
            return self._document(row)

    def list_documents(self, kind):
        with self.lock, self.connect() as conn:
            return [self._document(row) for row in conn.execute("SELECT * FROM documents WHERE kind=? ORDER BY updated_at DESC", (kind,)).fetchall()]

    def _idempotent(self, conn, request_id, operation, payload):
        if not request_id:
            return None
        if not isinstance(request_id, str) or len(request_id) > 120:
            raise AppError("저장 요청 번호를 확인해 주세요.")
        row = conn.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["operation"] != operation or row["payload_hash"] != hashlib.sha256(dump(payload).encode()).hexdigest():
                raise AppError("같은 저장 요청에 다른 내용이 들어왔습니다. 다시 저장해 주세요.", 409, "request_conflict")
            return json.loads(row["response_json"])
        return None

    def _remember(self, conn, request_id, operation, payload, result):
        if request_id:
            conn.execute("INSERT INTO requests VALUES (?,?,?,?,?)", (request_id, operation,
                hashlib.sha256(dump(payload).encode()).hexdigest(), dump(result), now()))

    def save_document(self, kind, payload):
        if kind not in ("quotes", "plans") or not isinstance(payload, dict):
            raise AppError("저장 내용을 확인해 주세요.")
        form = payload.get("form")
        if form is None:
            form = {k: v for k, v in payload.items() if k not in ("id", "revision", "request_id", "finalize")}
        if not isinstance(form, dict):
            raise AppError("문서 내용을 확인해 주세요.")
        # Server strips script-like code only through HTML escaping at export; JSON is inert.
        clean_text(form.get("date"), 100)
        clean_text(form.get("time"), 100)
        clean_text(form.get("notes"), 10000)
        contacts = contact_issues(form)
        result = calculate_quote(form) if kind == "quotes" else validate_plan(form)
        result["contact_issues"] = contacts
        result["warnings"].extend(contacts)
        snapshot = self.program_snapshot(form.get("program_id"))
        status = "ready" if payload.get("finalize") is True else "draft"
        doc_id = payload.get("id") or uuid.uuid4().hex
        if not isinstance(doc_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", doc_id):
            raise AppError("문서 번호를 확인해 주세요.")
        request_id = payload.get("request_id")
        with self.lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cached = self._idempotent(conn, request_id, f"save:{kind}", payload)
            if cached:
                return cached
            row = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
            if row:
                if row["kind"] != kind:
                    raise AppError("다른 종류의 문서 번호입니다.", 409, "document_conflict")
                revision = number(payload.get("revision"), "저장 버전", minimum=1)
                if revision != row["revision"]:
                    raise AppError("다른 창에서 먼저 저장했습니다. 최근 저장본을 불러온 뒤 다시 저장해 주세요.",
                                   409, "revision_conflict", {"current": self._document(row)})
                current_revision, created = revision + 1, row["created_at"]
                # Existing work keeps the selected program as it was when created.
                previous_form = json.loads(row["form_json"])
                if previous_form.get("program_id") == form.get("program_id"):
                    snapshot = json.loads(row["content_json"])
            else:
                if payload.get("id") and payload.get("revision") not in (None, 0):
                    raise AppError("이전 저장본이 없습니다. 새 문서로 저장해 주세요.", 409, "not_found")
                current_revision, created = 1, now()
            if snapshot.get("title") == "미확인 프로그램":
                result["issues"].append("현재 자료 목록에 있는 프로그램을 선택해 주세요.")
                result["ready"] = False
            confirmations = list(contacts)
            if form.get("program_confirmed") is not True and form.get("content_confirmed") is not True:
                confirmations.append("이번 문서의 프로그램 내용을 직접 확인해 주세요.")
            if form.get("schedule_confirmed") is not True:
                confirmations.append("날짜와 시간을 확인해 주세요. 아직 미확정이면 초안으로 저장할 수 있습니다.")
            elif not clean_text(form.get("date"), 100) or not clean_text(form.get("time"), 100):
                confirmations.append("확인한 날짜와 시간을 모두 입력해 주세요.")
            if kind == "quotes" and form.get("prices_confirmed") is not True:
                if any(item.get("price_confirmed") is not True for item in form.get("items", [])):
                    confirmations.append("이번 문서의 단가를 직접 확인해 주세요.")
            if kind == "plans":
                if form.get("content_confirmed") is not True:
                    confirmations.append("수업 내용과 안전 안내를 직접 확인해 주세요.")
                if not clean_text(form.get("description"), 5000):
                    confirmations.append("계획서에 사용할 실제 수업 내용을 입력해 주세요.")
                sessions = form.get("sessions", [])
                snapshot_sessions = snapshot.get("sessions", [])
                for index, session in enumerate(sessions):
                    original = snapshot_sessions[index] if index < len(snapshot_sessions) and len(sessions) == len(snapshot_sessions) else {}
                    if session.get("requires_manual_activity") is True or original.get("requires_manual_activity") is True:
                        title = clean_text(session.get("title"), 200)
                        description = clean_text(session.get("description", session.get("goal")), 5000)
                        if session.get("activity_confirmed") is not True or not title or not description or "활동 정하기" in title:
                            confirmations.append(f"{index + 1}회차의 실제 활동·진행 내용·준비물을 정하고 직접 확인해 주세요.")
            result["confirmation_issues"] = confirmations
            result["finalizable"] = result["ready"] and not confirmations
            if payload.get("finalize") is True and not result["finalizable"]:
                raise AppError("확인할 내용이 남아 있습니다. 초안으로 저장하거나 안내 항목을 채워 주세요.",
                               422, "confirmation_required", result)
            timestamp = now()
            values = (doc_id, kind, current_revision, dump(form), dump(result), status,
                      created, timestamp, dump(snapshot))
            conn.execute("INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,?,?)", values)
            conn.execute("INSERT INTO history VALUES (?,?,?,?,?,?,?)", (doc_id, current_revision,
                dump(form), dump(result), status, timestamp, dump(snapshot)))
            response = {"id": doc_id, "kind": kind, "revision": current_revision, "form": form,
                        "result": result, "status": status, "created_at": created,
                        "updated_at": timestamp, "program_snapshot": snapshot, "saved": True}
            self._remember(conn, request_id, f"save:{kind}", payload, response)
            return response

    def history(self, kind, doc_id):
        self.get_document(kind, doc_id)
        with self.lock, self.connect() as conn:
            return [{"revision": row["revision"], "created_at": row["created_at"], "status": row["status"],
                     "form": json.loads(row["form_json"])} for row in conn.execute(
                         "SELECT * FROM history WHERE document_id=? ORDER BY revision DESC", (doc_id,))]

    def undo(self, kind, doc_id, payload):
        with self.lock:
            operation = f"undo:{kind}:{doc_id}"
            with self.connect() as conn:
                cached = self._idempotent(conn, payload.get("request_id"), operation, payload)
                if cached:
                    return cached
            current = self.get_document(kind, doc_id)
            expected = number(payload.get("revision"), "저장 버전", minimum=1)
            if expected != current["revision"]:
                raise AppError("저장본이 바뀌었습니다. 최근 저장본을 불러와 주세요.", 409, "revision_conflict", {"current": current})
            target = payload.get("target_revision", expected - 1)
            target = number(target, "되돌릴 버전", minimum=1)
            if target >= expected:
                raise AppError("이전 저장 버전을 선택해 주세요.")
            with self.connect() as conn:
                row = conn.execute("SELECT * FROM history WHERE document_id=? AND revision=?", (doc_id, target)).fetchone()
            if row is None:
                raise AppError("되돌릴 저장본이 없습니다.", 404, "not_found")
            response = self.save_document(kind, {"id": doc_id, "revision": expected,
                "form": json.loads(row["form_json"]), "finalize": False})
            with self.connect() as conn:
                self._remember(conn, payload.get("request_id"), operation, payload, response)
            return response

    def export(self, kind, doc_id, payload):
        document = self.get_document(kind, doc_id)
        if payload.get("revision") != document["revision"]:
            raise AppError("최근 저장본을 불러온 뒤 미리보기를 열어 주세요.", 409, "revision_conflict", {"current": document})
        name = f"{kind}_{doc_id}_v{document['revision']}_{int(time.time() * 1000)}.html"
        output = self.export_dir / name
        output.write_text(render_document(document), encoding="utf-8")
        return {"url": f"/exports/{name}", "path": str(output), "filename": name,
                "format": "html", "message": "미리보기에서 인쇄를 누르면 종이 또는 PDF로 저장할 수 있습니다."}

    def admin_configured(self):
        with self.lock, self.connect() as conn:
            return conn.execute("SELECT 1 FROM settings WHERE key='admin_pin'").fetchone() is not None

    def setup_admin(self, pin):
        if not isinstance(pin, str) or not re.fullmatch(r"\d{6,12}", pin):
            raise AppError("관리자 비밀번호는 숫자 6 ~ 12자리로 정해 주세요.")
        with self.lock, self.connect() as conn:
            if conn.execute("SELECT 1 FROM settings WHERE key='admin_pin'").fetchone():
                raise AppError("관리자 비밀번호가 이미 있습니다. 관리자 로그인을 사용해 주세요.", 409, "already_configured")
            salt = secrets.token_bytes(16)
            digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 200000)
            data = {"salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(digest).decode(), "iterations": 200000}
            conn.execute("INSERT INTO settings VALUES ('admin_pin',?)", (dump(data),))
        return {"configured": True, "message": "관리자 비밀번호를 저장했습니다. 어머니 화면에는 필요하지 않습니다."}

    def admin_login(self, pin):
        if not isinstance(pin, str) or len(pin) > 30:
            raise AppError("관리자 비밀번호를 확인해 주세요.", 403, "admin_required")
        with self.lock, self.connect() as conn:
            if self.admin_retry_after > time.monotonic():
                raise AppError("비밀번호 확인이 여러 번 실패했습니다. 1분 후 다시 시도해 주세요.", 429, "admin_retry_later")
            row = conn.execute("SELECT value_json FROM settings WHERE key='admin_pin'").fetchone()
            if not row:
                raise AppError("먼저 이 컴퓨터에서 관리자 비밀번호를 정해 주세요.", 403, "admin_setup_required")
            config = json.loads(row[0])
            digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), base64.b64decode(config["salt"]), config["iterations"])
            if not hmac.compare_digest(digest, base64.b64decode(config["hash"])):
                self.admin_failed_attempts += 1
                if self.admin_failed_attempts >= 5:
                    self.admin_failed_attempts = 0
                    self.admin_retry_after = time.monotonic() + 60
                raise AppError("관리자 비밀번호가 맞지 않습니다.", 403, "admin_required")
            self.admin_failed_attempts = 0
            token = secrets.token_urlsafe(32)
            self.admin_sessions[token] = time.monotonic() + 900
        return {"admin_token": token, "expires_in": 900, "remote_enabled": False}

    def require_admin(self, token):
        with self.lock:
            if not token or self.admin_sessions.get(token, 0) < time.monotonic():
                raise AppError("관리자 로그인이 필요합니다. 어머니 업무 자료는 그대로 저장돼 있습니다.", 403, "admin_required")

    def update_content(self, payload):
        programs = payload.get("programs")
        if not isinstance(programs, list) or len(programs) > 100:
            raise AppError("프로그램 목록을 확인해 주세요.")
        identifiers = set()
        for program in programs:
            if not isinstance(program, dict):
                raise AppError("프로그램 내용을 확인해 주세요.")
            identifier = clean_text(program.get("id"), 100)
            if not identifier or identifier in identifiers:
                raise AppError("프로그램 번호가 비어 있거나 겹칩니다.")
            identifiers.add(identifier)
            if not clean_text(program.get("title", program.get("name")), 200):
                raise AppError("프로그램 이름을 입력해 주세요.")
        with self.lock:
            original = self.content()["catalog_meta"]
            updated = dict(original)
            updated["programs"] = programs
            updated["updated_at"] = now()
            updated["content_revision"] = int(original.get("content_revision", 0)) + 1
            self.content_dir.mkdir(exist_ok=True)
            destination = self.content_dir / "catalog.json"
            backup_dir = self.data_dir / "content_backups"
            backup_dir.mkdir(exist_ok=True)
            if destination.exists():
                (backup_dir / f"catalog_{time.time_ns()}.json").write_bytes(destination.read_bytes())
            temporary = self.content_dir / "catalog.pending.json"
            temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, destination)
            return {"saved": True, "content_revision": updated["content_revision"],
                    "message": "프로그램 자료를 저장했습니다. 이전 견적은 작성 당시 내용을 유지합니다."}

    def create_backup(self):
        with self.lock:
            name = f"aroma_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}.zip"
            temporary = self.backup_dir / f".{name}.sqlite3"
            try:
                with self.connect() as source, closing(sqlite3.connect(temporary)) as target:
                    source.backup(target)
                    target.execute("PRAGMA secure_delete=ON")
                    target.execute("DELETE FROM settings WHERE key='admin_pin'")
                    target.commit()
                    target.execute("VACUUM")
                manifest = {"schema_version": SCHEMA_VERSION, "created_at": now(),
                            "database_sha256": hashlib.sha256(temporary.read_bytes()).hexdigest(),
                            "scope": "documents_and_local_settings", "admin_pin_included": False,
                            "import_index_included_if_present": True, "import_raw_files_included": False,
                            "imports_backup_note": "이 ZIP은 자료 색인만 포함합니다. imports 폴더의 실제 사본은 별도 저장장치에 따로 백업하세요.",
                            "ai_job_index_included_if_present": True, "ai_result_files_included": False,
                            "ai_results_backup_note": "이 ZIP은 AI 작업 상태만 포함합니다. ai_jobs 폴더의 실제 결과 파일은 별도 저장장치에 따로 백업하세요.",
                            "content_restoration": "snapshot_only_no_overwrite",
                            "original_usb_included": False}
                destination = self.backup_dir / name
                with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
                    archive.write(temporary, "office.sqlite3")
                    archive.writestr("manifest.json", dump(manifest))
                    content = self.content()
                    archive.writestr("content_snapshot.json", dump({"programs": content["programs"], "catalog_meta": content["catalog_meta"]}))
                return {"name": name, "path": str(destination), "size": destination.stat().st_size, **manifest}
            finally:
                temporary.unlink(missing_ok=True)

    def list_backups(self):
        return [{"name": path.name, "size": path.stat().st_size,
                 "created_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")}
                for path in sorted(self.backup_dir.glob("aroma_backup_*.zip"), reverse=True)]

    def restore_backup(self, payload):
        if payload.get("confirm") is not True:
            raise AppError("복구 전 확인 버튼을 눌러 주세요.", 422, "confirmation_required")
        name = payload.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"aroma_backup_[A-Za-z0-9_]+\.zip", name):
            raise AppError("백업 목록에서 복구할 파일을 선택해 주세요.")
        path = self.backup_dir / name
        if not path.is_file():
            raise AppError("백업 파일을 찾지 못했습니다.", 404, "not_found")
        temporary = self.data_dir / f"restore_{secrets.token_hex(8)}.sqlite3"
        with self.lock:
            # The check and restoration share the same lock used by job start.
            manager = getattr(self, "_ai_jobs_manager", None)
            if manager and any(job["state"] in AIJobManager.ACTIVE for job in manager.list_jobs()):
                raise AppError("진행 중인 AI 작업을 먼저 중지한 뒤 복구해 주세요. 지금 자료는 그대로 남아 있습니다.", 409, "ai_job_busy")
            try:
                with zipfile.ZipFile(path) as archive:
                    if set(archive.namelist()) != {"office.sqlite3", "manifest.json", "content_snapshot.json"}:
                        raise AppError("이 앱에서 만든 백업 파일이 아닙니다.")
                    if archive.getinfo("office.sqlite3").file_size > 200 * 1024 * 1024:
                        raise AppError("백업이 너무 큽니다. 관리자에게 확인을 요청해 주세요.")
                    if archive.getinfo("manifest.json").file_size > 65536 or archive.getinfo("content_snapshot.json").file_size > 10 * 1024 * 1024:
                        raise AppError("백업 설명 파일이 너무 큽니다. 관리자에게 확인을 요청해 주세요.")
                    manifest = json.loads(archive.read("manifest.json"))
                    database = archive.read("office.sqlite3")
                if manifest.get("schema_version") != SCHEMA_VERSION:
                    raise AppError("백업의 앱 버전이 다릅니다. 관리자에게 확인을 요청해 주세요.")
                if not hmac.compare_digest(hashlib.sha256(database).hexdigest(), manifest.get("database_sha256", "")):
                    raise AppError("백업 파일이 손상됐습니다. 다른 백업을 선택해 주세요.")
                temporary.write_bytes(database)
                with closing(sqlite3.connect(temporary)) as check:
                    if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise AppError("백업 데이터를 확인하지 못했습니다. 다른 백업을 선택해 주세요.")
                    if check.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                        raise AppError("백업 데이터 버전이 맞지 않습니다.")
                    actual_tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if not {"documents", "history", "requests", "settings"}.issubset(actual_tables):
                        raise AppError("백업의 업무 자료를 찾지 못했습니다.")
                safety = self.create_backup()
                with self.connect() as current:
                    admin = current.execute("SELECT value_json FROM settings WHERE key='admin_pin'").fetchone()
                with closing(sqlite3.connect(temporary)) as source, self.connect() as target:
                    source.backup(target)
                    if admin:
                        target.execute("INSERT OR REPLACE INTO settings VALUES ('admin_pin',?)", (admin[0],))
                    target.commit()
                self.admin_sessions.clear()
                if manager:
                    manager.recover_interrupted()
                return {"restored": True, "safety_backup": safety["name"],
                        "message": "업무 자료를 복구했습니다. 복구 직전 자료도 안전 백업으로 남겼습니다.",
                        "content_changed": False, "original_usb_changed": False}
            except (zipfile.BadZipFile, json.JSONDecodeError, sqlite3.DatabaseError, KeyError):
                raise AppError("백업 파일을 읽지 못했습니다. 다른 백업을 선택해 주세요.")
            finally:
                temporary.unlink(missing_ok=True)


def render_document(document):
    escape = lambda v: html.escape(str(v if v is not None else "확인 필요"), quote=True)
    form, result = document["form"], document["result"]
    program = document["program_snapshot"]
    title = "견적서" if document["kind"] == "quotes" else "수업 계획서"
    money = lambda value: "확인 필요" if value is None else f"{value:,}원"
    rows = ""
    contact_html = "".join(f"<dt>{label}</dt><dd>{escape(form[field])}</dd>" for field, label in (("customer_phone", "전화"), ("customer_email", "이메일")) if form.get(field))
    if document["kind"] == "quotes":
        for line in result["lines"]:
            rows += f"<tr><td>{escape(line['name'])}</td><td>{escape(line['quantity'])}</td><td>{escape('1인당' if line['scaling']=='per_person' else '전체')}</td><td>{escape(money(line['unit_price']))}</td><td>{escape(money(line['subtotal']))}</td></tr>"
        body = f"<table><thead><tr><th>항목</th><th>수량</th><th>계산</th><th>단가</th><th>금액</th></tr></thead><tbody>{rows}</tbody></table>"
        body += "<dl>" + "".join(f"<dt>{label}</dt><dd>{escape(money(result.get(key)))}</dd>" for key, label in (
            ("materials_total", "항목 합계"), ("teaching_fee", "강사비"), ("travel_fee", "출장비"),
            ("discount", "할인"), ("supply_amount", "공급가액"), ("tax_amount", "세금"), ("total", "최종 금액"))) + "</dl>"
        body += f"<p>세금 계산: {escape({'none':'세금 별도 계산 없음','included':'금액에 포함','added':'금액에 더함','unconfirmed':'미확정'}[result['tax_mode']])}. {escape(result['rounding'])}.</p>"
    else:
        for key, label in (("title", "수업 이름"), ("purpose", "수업 목표"), ("description", "수업 내용"), ("duration", "수업 길이")):
            body = rows + f"<h2>{label}</h2><p>{escape(form.get(key,'')).replace(chr(10), '<br>')}</p>"
            rows = body
        body += "<h2>준비할 것</h2><ul>"
        for item in form.get("checklist", []):
            label = item if isinstance(item, str) else item.get("name", item.get("label", ""))
            body += f"<li>{escape(label)}</li>"
        body += "</ul>"
    messages = result.get("issues", []) + result.get("warnings", [])
    banner = "초안 · 확인 후 사용" if document["status"] != "ready" else "사용자가 입력 내용을 확인한 문서"
    source_ids = program.get("source_ids", program.get("evidence_ids", []))
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{font-family:'Malgun Gothic',sans-serif;max-width:900px;margin:32px auto;padding:24px;color:#182b26;font-size:17px}}table{{width:100%;border-collapse:collapse}}th,td{{border:1px solid #aabbb4;padding:12px;text-align:left}}h1{{font-size:34px}}dl{{display:grid;grid-template-columns:160px 1fr}}dt,dd{{margin:0;padding:8px;border-bottom:1px solid #ddd}}.notice{{background:#fff3d6;padding:16px}}.toolbar{{display:flex;gap:16px}}button{{padding:14px;font-size:19px}}@media print{{.toolbar{{display:none}}body{{margin:0;padding:0}}}}</style></head>
<body><div class="toolbar"><button id="print-document">인쇄 / PDF로 저장</button><a href="/">앱으로 돌아가기</a></div><h1>아로마 업무 · {title}</h1><p class="notice">{banner}</p>
<dl><dt>기관 / 고객</dt><dd>{escape(form.get('customer',''))}</dd>{contact_html}<dt>프로그램</dt><dd>{escape(program.get('title',program.get('name','')))}</dd><dt>날짜 / 시간</dt><dd>{escape(form.get('date',''))} {escape(form.get('time',''))} ({'확인함' if form.get('schedule_confirmed') is True else '미확정'})</dd><dt>참여 인원</dt><dd>{escape(form.get('participants',''))}명</dd></dl>
{body}<h2>메모</h2><p>{escape(form.get('notes','')).replace(chr(10), '<br>')}</p><h2>확인할 내용</h2><ul>{''.join('<li>'+escape(message)+'</li>' for message in messages) or '<li>입력 내용을 다시 살펴보세요.</li>'}</ul>
<p>예약·승인·의학적 효능을 보증하는 문서가 아닙니다. 실제 AI 호출 없이 저장된 자료와 입력값으로 작성했습니다.</p><p>참고 자료: {escape(', '.join(str(s) for s in source_ids)) or '프로그램 자료 확인 필요'} · 문서 {escape(document['id'])} · 버전 {document['revision']}</p><script src="/print.js"></script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "AromaLocal/1.0"
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    @property
    def store(self):
        return self.server.store

    def log_message(self, format, *args):
        # Do not log customer text, tokens, bodies, or query strings.
        if getattr(self.server, "verbose", False):
            print(f"{self.log_date_time_string()} {self.command} {urlsplit(self.path).path} {args[1] if len(args)>1 else ''}")

    def check_local(self):
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "").lower() not in allowed:
            raise AppError("앱의 로컬 주소로 접속해 주세요.", 403, "invalid_host")
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in allowed}:
            raise AppError("다른 웹사이트에서는 이 앱의 자료에 접근할 수 없습니다.", 403, "invalid_origin")
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site not in (None, "none", "same-origin"):
            raise AppError("앱 화면에서 다시 눌러 주세요.", 403, "cross_site_blocked")

    def send_bytes(self, payload, status=200, content_type="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(payload)

    def json_response(self, value, status=200):
        self.send_bytes(dump(value).encode("utf-8"), status)

    def error_response(self, error):
        self.json_response({"error": str(error), "code": error.code, "details": error.details}, error.status)

    def read_json(self, *, body_limit=MAX_BODY):
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            raise AppError("앱 화면에서 다시 저장해 주세요.", 415, "json_required")
        if self.headers.get("Transfer-Encoding"):
            raise AppError("저장 요청 형식을 확인해 주세요.")
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            raise AppError("저장 요청 형식을 확인해 주세요.")
        if length < 0 or length > body_limit:
            self.close_connection = True
            raise AppError("저장 내용이 너무 큽니다. 첨부파일은 자료 목록으로 확인해 주세요.", 413, "body_too_large")
        try:
            body = self.rfile.read(length)
            self._json_body_consumed = True
            value = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise AppError("입력 내용을 읽지 못했습니다. 다시 저장해 주세요.")
        if not isinstance(value, dict):
            raise AppError("입력 내용을 확인해 주세요.")
        return value

    def drain_rejected_json(self):
        # Windows can send TCP RST when an unread request body is closed, losing
        # the useful 403 response. Drain only bounded JSON, never large file input.
        if getattr(self, "_json_body_consumed", False):
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json" or self.headers.get("Transfer-Encoding"):
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= MAX_BODY:
                return
            self.connection.settimeout(0.2)
            self.rfile.read(length)
            self._json_body_consumed = True
        except (OSError, ValueError):
            pass
        finally:
            self.connection.settimeout(10)

    def do_GET(self):
        # Serialize through the complete response so restore cannot rename raw
        # files while an attachment reader still holds them on Windows.
        with self.store.lock:
            self._do_GET_serialized()

    def _do_GET_serialized(self):
        try:
            self.check_local()
            path = unquote(urlsplit(self.path).path)
            if (path.startswith("/api/") or path.startswith("/exports/")) and path not in (
                    "/api/health", "/api/bootstrap", "/api/admin/status", "/api/backups/full", "/api/updates/status"):
                self.store.require_no_recovery()
            if path == "/api/health":
                return self.json_response({"app_id": "aroma-work-helper", "instance_root": str(self.store.root),
                    "version": APP_VERSION, "schema_version": SCHEMA_VERSION, "storage": "local_only"})
            if path == "/api/bootstrap":
                return self.json_response(self.store.bootstrap())
            if path == "/api/backups/full":
                return self.json_response(self.store.full_backup_api().status())
            if path == "/api/updates/status":
                return self.json_response({"enabled": False, "status": "provider_not_selected", "manual_apply_only": True,
                    "publisher_key_configured": False, "supervisor_connected": False, "external_fetch": False})
            full_match = re.fullmatch(r"/api/backups/full/(aroma_full_[A-Za-z0-9_]+\.zip)/download", path)
            if full_match:
                file = self.store.full_backup_api().download(full_match.group(1))
                file_path = file["path"]
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", "attachment; filename=" + file["filename"])
                self.send_header("Content-Length", str(file_path.stat().st_size))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                with file_path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(65536), b""):
                        self.wfile.write(chunk)
                return
            if path == "/api/ai/status":
                return self.json_response(self.store.ai_manager().status())
            if path == "/api/runtime/login/status":
                return self.json_response(self.store.runtime_login_status())
            if path == "/api/ai/jobs":
                return self.json_response({"jobs": self.store.ai_manager().list_jobs()})
            ai_match = re.fullmatch(r"/api/ai/jobs/([a-f0-9]{32})(?:/files/([A-Za-z0-9_-]+\.(?:json|txt|html|docx|pptx|png)))?", path)
            if ai_match:
                job_id, filename = ai_match.groups()
                if not filename:
                    return self.json_response({"job": self.store.ai_manager().get(job_id)})
                file_path, metadata = self.store.ai_manager().download(job_id, filename)
                data = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", metadata["mime"])
                inline = filename.endswith(".png") and parse_qs(urlsplit(self.path).query).get("inline") == ["1"]
                self.send_header("Content-Disposition", ("inline" if inline else "attachment") + "; filename=" + filename)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/files":
                return self.json_response(self.store.files_payload())
            file_match = re.fullmatch(r"/api/files/([A-Za-z0-9_-]+)(?:/(download))?", path)
            if file_match:
                file_id, action = file_match.groups()
                importer = self.store.import_store()
                if not action:
                    return self.json_response({"file": importer.get_file(file_id)})
                file_path, metadata = importer.download(file_id)
                # Attachment prevents browser parsing or script execution.
                data = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", "attachment; filename=local-file" + metadata["extension"] + "; filename*=UTF-8''" + quote(metadata["name"], safe=""))
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/backups":
                return self.json_response({"backups": self.store.list_backups()})
            if path == "/api/admin/content":
                self.store.require_admin(self.headers.get("X-Admin-Token"))
                return self.json_response(self.store.content())
            if path == "/api/admin/status":
                return self.json_response({"configured": self.store.admin_configured(), "remote_enabled": False})
            match = re.fullmatch(r"/api/(quotes|plans)(?:/([A-Za-z0-9_-]+)(?:/(history))?)?", path)
            if match:
                kind, doc_id, action = match.groups()
                value = self.store.history(kind, doc_id) if action else (self.store.get_document(kind, doc_id) if doc_id else self.store.list_documents(kind))
                return self.json_response({"history": value} if action else value)
            if path.startswith("/api/"):
                raise AppError("요청한 기능을 찾지 못했습니다.", 404, "not_found")
            if path.startswith("/exports/"):
                relative = path[len("/exports/"):]
                if not re.fullmatch(r"[A-Za-z0-9_-]+\.(html|txt|json|svg|pptx|docx|csv|png)", relative):
                    raise AppError("문서 경로를 확인해 주세요.", 404, "not_found")
                file_path = self.store.export_dir / relative
            else:
                relative = path.lstrip("/") or "index.html"
                public = (self.store.root / "public").resolve()
                file_path = (public / relative).resolve()
                if not file_path.is_relative_to(public):
                    raise AppError("파일 경로를 확인해 주세요.", 403, "invalid_path")
            if not file_path.is_file():
                raise AppError("이 화면을 아직 준비 중입니다. 앱을 다시 열어 주세요.", 404, "not_found")
            types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                     ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
                     ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".ico": "image/x-icon",
                     ".txt": "text/plain; charset=utf-8", ".csv": "text/csv; charset=utf-8",
                     ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                     ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}
            if file_path.suffix.lower() not in types:
                raise AppError("이 파일 형식은 앱에서 열 수 없습니다.", 403, "unsupported_type")
            self.send_bytes(file_path.read_bytes(), content_type=types[file_path.suffix.lower()])
        except WholeBackupError as error:
            self.error_response(AppError(error.message, error.status, error.code))
        except (AppError, LocalImportError, AIJobError) as error:
            self.error_response(error)
        except (OSError, sqlite3.Error, ValueError) as error:
            self.error_response(AppError("자료를 읽지 못했습니다. 앱을 다시 열어 주세요. 저장 자료는 그대로 남아 있습니다.", 500, "storage_error"))

    def do_POST(self):
        with self.store.lock:
            self._do_POST_serialized()

    def _do_POST_serialized(self):
        self._json_body_consumed = False
        try:
            self.check_local()
            app_token = self.headers.get("X-App-Token", "")
            if not app_token.isascii() or len(app_token) != len(self.store.csrf_token) or not hmac.compare_digest(app_token, self.store.csrf_token):
                self.close_connection = True
                raise AppError("앱을 다시 열고 저장해 주세요. 입력 내용은 화면에 남아 있습니다.", 403, "session_expired")
            path = urlsplit(self.path).path
            if path not in ("/api/admin/login", "/api/backups/full/recover", "/api/shutdown"):
                self.store.require_no_recovery()
            if path == "/api/files/upload":
                if self.headers.get("Transfer-Encoding"):
                    raise AppError("파일 전송 형식을 확인해 주세요.")
                if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/octet-stream":
                    raise AppError("파일 선택 화면에서 다시 가져와 주세요.", 415, "binary_required")
                try:
                    size = int(self.headers.get("Content-Length", "-1"))
                    encoded_name = self.headers.get("X-File-Name", "")
                    if len(encoded_name) > 3072:
                        raise ValueError("filename length")
                    name = unquote(encoded_name, encoding="utf-8", errors="strict")
                except (ValueError, UnicodeDecodeError):
                    raise AppError("선택한 파일 정보를 확인해 주세요.")
                self.close_connection = True
                result = self.store.import_store().import_stream(self.rfile, size, name,
                    self.headers.get("X-Request-Id"), self.headers.get("X-File-Modified"))
                return self.json_response(result)
            payload = self.read_json(body_limit=8 * 1024 * 1024 if path == "/api/studio/png" else MAX_BODY)
            if path == "/api/studio/png":
                from png_exports import PngExportError, save_studio_png
                try:
                    saved = save_studio_png(self.store, payload)
                except PngExportError as error:
                    raise AppError(error.message, error.status, error.code) from None
                return self.json_response(saved)
            if path == "/api/ai/preview":
                body = {key: payload.get(key) for key in ("fields", "include_fields", "include_contacts") if key in payload}
                # Do not silently ignore explicit file/path keys.
                body.update({key: payload[key] for key in ("files", "file", "attachments", "reference_files", "reference", "source_files", "source_path", "path", "user_data", "data",
                    "profile_root", "image_profile_dir", "private_root", "runtime_root", "codex_home", "api_key", "access_token", "auth") if key in payload})
                return self.json_response(self.store.ai_manager().preview(payload.get("kind"), body))
            if path in ("/api/runtime/login/start", "/api/runtime/login/cancel", "/api/runtime/image/preflight"):
                self.store.require_admin(self.headers.get("X-Admin-Token"))
                method = {"/api/runtime/login/start": self.store.runtime_login_start,
                          "/api/runtime/login/cancel": self.store.runtime_login_cancel,
                          "/api/runtime/image/preflight": self.store.runtime_image_preflight}[path]
                return self.json_response(method(payload))
            if path == "/api/ai/jobs":
                return self.json_response({"job": self.store.ai_manager().start(payload)})
            ai_native = re.fullmatch(r"/api/ai/jobs/([a-f0-9]{32})/(native-preview|native-export)", path)
            if ai_native:
                job_id, action = ai_native.groups()
                method = self.store.native_preview if action == "native-preview" else self.store.native_export
                return self.json_response(method(job_id, payload))
            ai_cancel = re.fullmatch(r"/api/ai/jobs/([a-f0-9]{32})/cancel", path)
            if ai_cancel:
                return self.json_response({"job": self.store.ai_manager().cancel(ai_cancel.group(1))})
            if path == "/api/shutdown":
                if payload.get("confirm") is not True:
                    raise AppError("앱을 종료하려면 확인 버튼을 눌러 주세요.", 422, "confirmation_required")
                self.json_response({"shutdown": True, "data_deleted": False,
                    "message": "업무 앱을 종료합니다. 저장 자료는 그대로 남아 있습니다."})
                self.wfile.flush()
                manager = getattr(self.store, "_ai_jobs_manager", None)
                if manager:
                    manager.cancel_all()
                controller = getattr(self.store, "_runtime_login_controller", None)
                if controller:
                    if callable(getattr(controller, "close", None)):
                        controller.close()
                    else:
                        controller.cancel()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            match = re.fullmatch(r"/api/(quotes|plans)/save", path)
            if match:
                return self.json_response(self.store.save_document(match.group(1), payload))
            if path == "/api/quotes/calculate":
                return self.json_response(calculate_quote(payload.get("form", payload)))
            if path == "/api/studio/draft":
                from quality import create_brief
                try:
                    brief = create_brief(payload.get("kind"), payload.get("payload", {}))
                except ValueError as error:
                    raise AppError(str(error))
                return self.json_response(brief)
            if path == "/api/studio/export":
                from quality import export_brief
                try:
                    data, mime, extension = export_brief(payload.get("kind"), payload.get("payload", {}), payload.get("format", "html"))
                except ValueError as error:
                    raise AppError(str(error))
                extension = str(extension).lstrip(".")
                if extension not in ("html", "txt", "json", "svg", "pptx", "docx", "csv") or not isinstance(data, bytes):
                    raise AppError("이 출력 형식을 확인해 주세요.")
                with self.store.lock:
                    filename = f"studio_{uuid.uuid4().hex}.{extension}"
                    output = self.store.export_dir / filename
                    with output.open("xb") as handle:
                        handle.write(data); handle.flush(); os.fsync(handle.fileno())
                return self.json_response({"url": f"/exports/{filename}", "path": str(output),
                    "filename": filename, "format": extension, "mode": "local_template", "ai_used": False})
            match = re.fullmatch(r"/api/(quotes|plans)/([A-Za-z0-9_-]+)/(undo|export)", path)
            if match:
                kind, doc_id, action = match.groups()
                method = self.store.undo if action == "undo" else self.store.export
                return self.json_response(method(kind, doc_id, payload))
            if path == "/api/backups/create":
                self.store.require_runtime_idle()
                return self.json_response(self.store.create_backup())
            if path == "/api/backups/full/create":
                self.store.require_runtime_idle()
                return self.json_response(self.store.full_backup_api().create())
            if path == "/api/backups/full/restore":
                return self.json_response(self.store.full_backup_api().restore(payload, self.headers.get("X-Admin-Token")))
            if path == "/api/backups/full/recover":
                return self.json_response(self.store.full_backup_api().recover(payload, self.headers.get("X-Admin-Token")))
            if path == "/api/backups/restore":
                self.store.require_runtime_idle()
                return self.json_response(self.store.restore_backup(payload))
            if path == "/api/admin/setup":
                return self.json_response(self.store.setup_admin(payload.get("pin")))
            if path == "/api/admin/login":
                return self.json_response(self.store.admin_login(payload.get("pin")))
            if path == "/api/admin/content":
                self.store.require_admin(self.headers.get("X-Admin-Token"))
                return self.json_response(self.store.update_content(payload))
            if path == "/api/connection/test":
                # Accept no secrets and make no network requests, even when an URL is supplied.
                if any(key in payload for key in ("api_key", "key", "token", "url", "endpoint")):
                    raise AppError("지금은 외부 연결 전입니다. 비밀번호나 API 키를 입력하지 마세요.", 422, "external_connection_disabled")
                return self.json_response({"mode": "template", "status": "not_connected", "ai_used": False,
                    "tested": "local_template_only", "external_requests": 0,
                    "message": "로컬 서식 기능은 사용할 수 있습니다. 실제 AI·원격 서버 연결은 승인을 받아 별도로 설정해야 합니다."})
            raise AppError("요청한 기능을 찾지 못했습니다.", 404, "not_found")
        except WholeBackupError as error:
            self.drain_rejected_json()
            self.error_response(AppError(error.message, error.status, error.code))
        except (AppError, LocalImportError, AIJobError) as error:
            self.drain_rejected_json()
            self.error_response(error)
        except (OSError, sqlite3.Error, ValueError, ImportError) as error:
            self.error_response(AppError("저장하지 못했습니다. 입력 내용을 그대로 두고 다시 저장해 주세요.", 500, "storage_error"))


def create_server(root=APP_ROOT, port=8765, data_dir=None, verbose=False, content_dir=None, private_root=None, image_profile_dir=None, runtime_root=None):
    store = AppStore(root, data_dir, content_dir=content_dir, private_root=private_root, image_profile_dir=image_profile_dir, runtime_root=runtime_root)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    # Completed shutdown waits for active saves, rather than abandoning them.
    # Each response closes its connection so idle browser sockets do not delay exit.
    server.daemon_threads = False
    server.store = store
    server.verbose = verbose
    return server


def main():
    parser = argparse.ArgumentParser(description="아로마 업무 로컬 업무 앱")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ["AROMA_DATA_DIR"]) if os.environ.get("AROMA_DATA_DIR") else None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--content-dir", type=Path, default=Path(os.environ["AROMA_CONTENT_DIR"]) if os.environ.get("AROMA_CONTENT_DIR") else None)
    parser.add_argument("--private-root", type=Path, default=Path(os.environ["AROMA_PRIVATE_ROOT"]) if os.environ.get("AROMA_PRIVATE_ROOT") else None)
    args = parser.parse_args()
    server = create_server(port=args.port, data_dir=args.data_dir, verbose=args.verbose,
                           content_dir=args.content_dir, private_root=args.private_root)
    print(f"로컬 업무 앱: http://127.0.0.1:{server.server_address[1]}")
    print(f"저장 폴더: {server.store.data_dir}")
    print("외부 AI·계정·원격 서버는 연결하지 않았습니다. 종료: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        manager = getattr(server.store, "_ai_jobs_manager", None)
        if manager:
            manager.close()


if __name__ == "__main__":
    main()
