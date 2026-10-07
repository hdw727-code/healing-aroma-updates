"""Local commerce drafts. Prices, stock, reservations and payment stay unconfirmed.

No public form is forwarded to the permissive document form API. No credentials
or free-text child/health data are accepted by this strict contact-only boundary.
"""
from __future__ import annotations
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

class CommerceError(ValueError):
    def __init__(self, code, message, status=400, details=None):
        self.code, self.status, self.details = code, status, details
        super().__init__(message)

def reject(code, message, status=400, details=None):
    raise CommerceError(code, message, status, details)

def object_only(value, allowed, required=()):
    if not isinstance(value, dict) or not set(required).issubset(value) or not set(value).issubset(allowed):
        reject("commerce_fields", "허용된 접수 항목만 입력해 주세요.")
    return value

def text(value, label, length=120, *, optional=False):
    if optional and value in (None, ""): return None
    if not isinstance(value, str) or not value.strip() or len(value) > length or any(ord(c) < 32 or ord(c) == 127 for c in value):
        reject("commerce_input", label + " 입력을 확인해 주세요.")
    return value.strip()

def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", value):
        reject("commerce_identity", "작업 식별자를 확인해 주세요.")
    return value

def resource_identity(value):
    # Site resource names can be short (for example a fixed soap offering).
    # These IDs are SQL parameters, never filesystem names or credentials.
    if not isinstance(value,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}",value):
        reject("commerce_identity","자료 식별자를 확인해 주세요.")
    return value

def integer(value, label, minimum=1, maximum=10000, optional=False):
    if optional and value is None: return None
    if type(value) is not int or not minimum <= value <= maximum:
        reject("commerce_input", label + " 값은 허용 범위의 정수여야 합니다.")
    return value

def contact(value):
    object_only(value, {"name", "phone", "email"})
    result = {key: text(value.get(key), "어른 담당 " + key, {"name":60,"phone":40,"email":254}[key], optional=True) for key in ("name", "phone", "email")}
    if not result["phone"] and not result["email"]:
        reject("commerce_contact", "어른 담당 연락처를 한 가지 입력해 주세요.")
    if result["phone"] and not re.fullmatch(r"[+0-9() .-]{5,40}", result["phone"]):
        reject("commerce_contact", "전화번호 형식을 확인해 주세요.")
    if result["email"] and not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", result["email"]):
        reject("commerce_contact", "이메일 형식을 확인해 주세요.")
    return result

class CommerceStore:
    def __init__(self, app):
        self.app = app

    def status(self):
        with self.app.lock, self.app.connect() as conn:
            count = conn.execute("SELECT count(*) FROM commerce_requests WHERE review_status!='closed'").fetchone()[0]
            queued = conn.execute("SELECT count(*) FROM commerce_outbox WHERE state='pending'").fetchone()[0]
            details=[json.loads(r[0]) for r in conn.execute("SELECT details_json FROM commerce_offerings")]
        return {"schema_version":2, "development_only":True, "connection_status":"not_connected",
            "server_registered":False, "automatic_sync":False, "open_requests":count,
            "pending_operations":queued, "card_payment_enabled":False,
            "prices_configured":any(r.get("price") is not None for r in details),
            "inventory_configured":any(r.get("stock") is not None for r in details),
            "public_submission_enabled":False, "contacts_require_admin":True,
            "collects_child_or_health_data":False}

    def _cached(self, conn, request_id, operation, payload):
        identity(request_id)
        digest = hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()
        row = conn.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["operation"] != operation or row["payload_hash"] != digest:
                reject("commerce_request_conflict", "같은 작업 번호에 다른 내용이 있습니다.", 409)
            return json.loads(row["response_json"]), digest
        return None, digest

    def _remember(self, conn, request_id, operation, digest, result):
        conn.execute("INSERT INTO requests VALUES (?,?,?,?,?)", (request_id,operation,digest,canonical(result),timestamp()))

    def _revision(self, current, requested):
        if type(requested) is not int or requested != current["revision"]:
            reject("commerce_revision_conflict", "다른 화면에서 변경되었습니다. 최신 내용을 확인해 주세요.",409,{"current_revision":current["revision"]})

    def catalog(self):
        with self.app.lock, self.app.connect() as conn:
            return [self._offering(r) for r in conn.execute("SELECT * FROM commerce_offerings ORDER BY created_at,id")]

    def _offering(self, row):
        details=json.loads(row["details_json"])
        return {"id":row["id"],"revision":row["revision"],"title":row["title"],"offering_kind":row["offering_kind"],"program_id":row["program_id"],**details,"unit_price":details.get("price")}

    def save_offering(self, payload):
        object_only(payload,{"request_id","id","revision","title","offering_kind","program_id","price","stock","state"},{"request_id","title","offering_kind"})
        title=text(payload["title"],"항목 이름",80)
        kind=payload["offering_kind"]
        if kind not in ("experience","finished_goods","kit"):reject("commerce_kind","체험/완성품/키트 중 선택해 주세요.")
        program=text(payload.get("program_id"),"프로그램",80,optional=True)
        if program and program not in {p["id"] for p in self.app.content()["programs"]}:
            reject("commerce_program","연결할 프로그램을 확인해 주세요.")
        with self.app.lock, self.app.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            saved,digest=self._cached(conn,payload["request_id"],"commerce_catalog",payload)
            if saved:return saved
            current=conn.execute("SELECT * FROM commerce_offerings WHERE id=?",(payload.get("id"),)).fetchone()
            if payload.get("id") and not current:reject("commerce_not_found","항목이 없습니다.",404)
            if current:self._revision(current,payload.get("revision"))
            item_id=current["id"] if current else uuid.uuid4().hex
            revision=current["revision"]+1 if current else 1
            details=json.loads(current["details_json"]) if current else {"price":None,"stock":None,"state":"inquiry","sales_policy":None,"payment_policy":None,"published":False,"approval_status":"review_required"}
            for field in ("price","stock"):
                if field in payload:details[field]=integer(payload[field],"가격" if field=="price" else "재고",0,1_000_000_000,optional=True)
            if "state" in payload:
                if payload["state"] not in ("inquiry","ready","paused"):reject("commerce_state","항목 준비 상태를 확인해 주세요.")
                details["state"]=payload["state"]
            # Local management values are not a remote publication or booking.
            details["published"]=False
            if details.get("server_revision") is not None:details["edited_locally"]=True
            created=current["created_at"] if current else timestamp()
            if current:
                conn.execute("UPDATE commerce_offerings SET revision=?,title=?,offering_kind=?,program_id=?,details_json=?,updated_at=? WHERE id=?",(revision,title,kind,program,canonical(details),timestamp(),item_id))
            else:
                conn.execute("INSERT INTO commerce_offerings VALUES (?,?,?,?,?,?,?,?)",(item_id,revision,title,kind,program,canonical(details),created,timestamp()))
            result={"item":self._offering(conn.execute("SELECT * FROM commerce_offerings WHERE id=?",(item_id,)).fetchone())}
            self._remember(conn,payload["request_id"],"commerce_catalog",digest,result)
            return result

    def slots(self):
        with self.app.lock,self.app.connect() as conn:
            return [{"id":r["id"],"offering_id":r["offering_id"],"revision":r["revision"],**json.loads(r["schedule_json"])} for r in conn.execute("SELECT * FROM commerce_slots ORDER BY created_at,id")]

    def save_slot(self,payload):
        object_only(payload,{"request_id","offering_id","id","revision","starts_at","ends_at","capacity","state"},{"request_id","offering_id"})
        with self.app.lock,self.app.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            saved,digest=self._cached(conn,payload["request_id"],"commerce_slot",payload)
            if saved:return saved
            if not conn.execute("SELECT 1 FROM commerce_offerings WHERE id=?",(payload["offering_id"],)).fetchone():reject("commerce_not_found","체험 항목이 없습니다.",404)
            current=conn.execute("SELECT * FROM commerce_slots WHERE id=?",(payload.get("id"),)).fetchone()
            if payload.get("id") and not current:reject("commerce_not_found","일정 항목이 없습니다.",404)
            if current:self._revision(current,payload.get("revision"))
            slot_id=current["id"] if current else uuid.uuid4().hex
            revision=current["revision"]+1 if current else 1
            schedule=json.loads(current["schedule_json"]) if current else {"starts_at":None,"ends_at":None,"capacity":None,"state":"draft","status":"draft"}
            for field in ("starts_at","ends_at"):
                if field in payload:
                    value=text(payload[field],"일정",40,optional=True)
                    if value is not None:
                        try:parsed=datetime.fromisoformat(value)
                        except ValueError:reject("commerce_schedule","날짜와 시간을 확인해 주세요.")
                        if parsed.utcoffset() is None:reject("commerce_schedule","시간대가 포함된 날짜와 시간을 입력해 주세요.")
                        value=parsed.isoformat(timespec="seconds")
                    schedule[field]=value
            if schedule["starts_at"] and schedule["ends_at"] and datetime.fromisoformat(schedule["ends_at"])<=datetime.fromisoformat(schedule["starts_at"]):reject("commerce_schedule","종료 시간은 시작 시간보다 뒤여야 합니다.")
            if "capacity" in payload:schedule["capacity"]=integer(payload["capacity"],"정원",optional=True)
            if "state" in payload:
                if payload["state"] not in ("draft","ready","paused"):reject("commerce_state","일정 준비 상태를 확인해 주세요.")
                schedule["state"]=payload["state"]
            if schedule["state"]=="ready" and (schedule["starts_at"] is None or schedule["capacity"] is None):reject("commerce_schedule","준비된 일정에는 시작 시간과 정원을 입력해 주세요.")
            schedule["status"]=schedule["state"]
            if schedule.get("server_revision") is not None:schedule["edited_locally"]=True
            if current:
                conn.execute("UPDATE commerce_slots SET offering_id=?,revision=?,schedule_json=?,updated_at=? WHERE id=?",(payload["offering_id"],revision,canonical(schedule),timestamp(),slot_id))
            else:conn.execute("INSERT INTO commerce_slots VALUES (?,?,?,?,?,?)",(slot_id,payload["offering_id"],revision,canonical(schedule),timestamp(),timestamp()))
            result={"slot":{"id":slot_id,"offering_id":payload["offering_id"],"revision":revision,**schedule}}
            self._remember(conn,payload["request_id"],"commerce_slot",digest,result)
            return result

    def _request(self,row):
        return {key:row[key] for key in ("id","revision","source","server_request_id","server_revision","offering_id","participants","review_status","booking_status","payment_status","created_at","updated_at")}|{"adult_contact":json.loads(row["contact_json"]),"details":json.loads(row["details_json"])}

    def list_requests(self):
        with self.app.lock,self.app.connect() as conn:
            return [self._request(r) for r in conn.execute("SELECT * FROM commerce_requests ORDER BY created_at DESC,id")]

    def save_request(self,payload):
        object_only(payload,{"request_id","offering_id","adult_contact","participants"},{"request_id","offering_id","adult_contact","participants"})
        responsible=contact(payload["adult_contact"])
        participants=integer(payload["participants"],"인원",optional=True)
        with self.app.lock,self.app.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            saved,digest=self._cached(conn,payload["request_id"],"commerce_request",payload)
            if saved:return saved
            row=conn.execute("SELECT * FROM commerce_offerings WHERE id=?",(payload["offering_id"],)).fetchone()
            if not row:reject("commerce_not_found","체험/상품 항목이 없습니다.",404)
            if row["offering_kind"]=="experience":integer(participants,"인원")
            result=self._insert_request(conn,uuid.uuid4().hex,"local",None,None,row,responsible,participants)
            self._remember(conn,payload["request_id"],"commerce_request",digest,{"request":result})
            return {"request":result}

    def _insert_request(self,conn,item_id,source,server_id,server_revision,offering,responsible,participants):
        details={"offering_snapshot":self._offering(offering),"total":None,"quantity":None,"slot_id":None,"document_id":None,"payment_method":None}
        conn.execute("INSERT INTO commerce_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(item_id,1,source,server_id,server_revision,offering["id"],canonical(responsible),participants,"received","inquiry","not_configured",canonical(details),timestamp(),timestamp()))
        if offering["offering_kind"] in ("finished_goods","kit"):
            conn.execute("INSERT INTO commerce_orders VALUES (?,?,?,?,?,?)",(uuid.uuid4().hex,item_id,1,canonical({"status":"draft","quantity":None,"unit_price":None,"total":None,"fulfillment":None}),timestamp(),timestamp()))
        result=self._request(conn.execute("SELECT * FROM commerce_requests WHERE id=?",(item_id,)).fetchone())
        conn.execute("INSERT INTO commerce_request_history VALUES (?,?,?,?)",(item_id,1,canonical(result),timestamp()))
        return result

    def review(self,payload):
        object_only(payload,{"request_id","id","revision","review_status","booking_status","payment_status"},{"request_id","id","revision"})
        with self.app.lock,self.app.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            saved,digest=self._cached(conn,payload["request_id"],"commerce_review",payload)
            if saved:return saved
            current=conn.execute("SELECT * FROM commerce_requests WHERE id=?",(payload["id"],)).fetchone()
            if not current:reject("commerce_not_found","접수가 없습니다.",404)
            if json.loads(current["details_json"]).get("deleted_at_server"):reject("commerce_deleted","서버에서 연락처가 삭제된 접수는 다시 전송하지 않습니다.",410)
            self._revision(current,payload["revision"])
            review=payload.get("review_status",current["review_status"])
            booking=payload.get("booking_status",current["booking_status"])
            payment=payload.get("payment_status",current["payment_status"])
            if review not in ("received","reviewing","closed") or booking not in ("inquiry","confirmed","cancelled") or payment not in ("not_configured","onsite_pending","onsite_confirmed"):
                reject("commerce_status","접수 상태를 확인해 주세요.")
            if booking == "confirmed" or payment != "not_configured":
                reject("commerce_policy_unconfigured","일정·판매·현장결제 정책을 먼저 확정해야 합니다.",409)
            revision=current["revision"]+1
            conn.execute("UPDATE commerce_requests SET revision=?,review_status=?,booking_status=?,payment_status=?,updated_at=? WHERE id=?",(revision,review,booking,payment,timestamp(),current["id"]))
            result=self._request(conn.execute("SELECT * FROM commerce_requests WHERE id=?",(current["id"],)).fetchone())
            conn.execute("INSERT INTO commerce_request_history VALUES (?,?,?,?)",(current["id"],revision,canonical(result),timestamp()))
            operation_id=None
            if current["source"] == "server":
                operation_id=uuid.uuid4().hex
                command={"entity_id":current["server_request_id"],"base_server_revision":current["server_revision"],"review_status":review,"booking_status":booking,"payment_status":payment}
                conn.execute("INSERT INTO commerce_outbox VALUES (?,?,?,?,?,?,?,?,?)",(operation_id,current["id"],current["server_revision"],hashlib.sha256(canonical(command).encode()).hexdigest(),canonical(command),"pending","{}",timestamp(),timestamp()))
            response={"request":result,"operation_id":operation_id,"server_confirmed":False}
            self._remember(conn,payload["request_id"],"commerce_review",digest,response)
            return response
