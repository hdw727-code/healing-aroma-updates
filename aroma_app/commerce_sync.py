"""Disconnected by default. Injectable transport is a synthetic-test seam only.

No URL, token, profile, HTTP client, automatic polling or credential store exists.
Expected remote contract is version 1 and independent of local DB schema 2.
"""
from __future__ import annotations
import hashlib
import json
import uuid
from commerce_store import canonical, contact, identity, resource_identity, integer, object_only, reject, timestamp

class CommerceSync:
    def __init__(self, app, transport=None):
        self.app, self.transport = app, transport
        self.active = False

    def require_idle(self):
        if self.active:reject("commerce_sync_busy","접수 동기화가 진행 중입니다. 완료 후 다시 시도해 주세요.",409)

    def status(self):
        with self.app.lock,self.app.connect() as conn:
            state={r["key"]:json.loads(r["value_json"]) for r in conn.execute("SELECT * FROM commerce_sync_state")}
            operations=[{"operation_id":r["operation_id"],"state":r["state"]} for r in conn.execute("SELECT operation_id,state FROM commerce_outbox WHERE state!='applied'")]
        return {"status":"synthetic_fixture" if self.transport is not None else "not_connected",
            "server_registered":False,"active":self.active,"automatic_sync":False,
            "server_epoch":state.get("server_epoch"),"cursor":state.get("cursor"),
            "operations":operations,"restore_reconciliation_required":True}

    def run(self,payload):
        object_only(payload,{"confirm"},{"confirm"})
        if payload["confirm"] is not True:reject("commerce_confirmation","동기화 시작을 직접 확인해 주세요.",422)
        if self.transport is None:reject("commerce_not_connected","접수 서버가 등록되지 않았습니다. 외부로 전송하지 않았습니다.",503)
        with self.app.lock:
            self.app.require_no_recovery()
            self.app.require_runtime_idle()
            self.require_idle()
            self.active=True
            with self.app.connect() as conn:
                state={r["key"]:json.loads(r["value_json"]) for r in conn.execute("SELECT * FROM commerce_sync_state")}
                pending=[dict(r) for r in conn.execute("SELECT * FROM commerce_outbox WHERE state='pending' ORDER BY created_at,operation_id")]
        try:
            # Network seam deliberately runs outside the store lock. Real
            # registration/auth/timeout/cancellation is not implemented here.
            # Restored operation IDs are checked at the server before any push.
            writable=getattr(self.transport,"outgoing_allowed",True) is True
            checked={}
            if writable:
                for operation in pending:
                    receipt=self.transport.operation_status(operation["operation_id"])
                    checked[operation["operation_id"]]=receipt
                    if receipt is not None:self._receipt(operation,receipt)
            batch=self.transport.pull(state.get("server_epoch"),state.get("cursor"),100)
            applied=self.apply_batch(batch,expected_state=state)
            # Read before write: receive deletion events even while the local
            # review queue is pending or the cloud write contract is absent.
            if writable:
                with self.app.lock,self.app.connect() as conn:
                    remaining=[dict(r) for r in conn.execute("SELECT * FROM commerce_outbox WHERE state='pending'")]
                for operation in remaining:
                    if checked.get(operation["operation_id"]) is None:
                        self._receipt(operation,self.transport.push(operation["operation_id"],json.loads(operation["payload_json"])))
            with self.app.lock:self.active=False
            return {"sync":self.status(),"applied_events":applied,"external_service_connected":False,"cloud_writes":"fixture_only" if writable else "not_configured_read_only"}
        finally:
            with self.app.lock:self.active=False

    def _receipt(self,operation,receipt):
        object_only(receipt,{"operation_id","state","server_revision"},{"operation_id","state","server_revision"})
        if receipt["operation_id"]!=operation["operation_id"] or receipt["state"] not in ("applied","conflict"):
            reject("commerce_receipt","서버 작업 응답을 확인하지 못했습니다.")
        revision=integer(receipt["server_revision"],"서버 버전",1,2**53-1)
        with self.app.lock,self.app.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current=conn.execute("SELECT * FROM commerce_outbox WHERE operation_id=?",(operation["operation_id"],)).fetchone()
            if not current or current["payload_hash"]!=operation["payload_hash"]:reject("commerce_operation_changed","로컬 작업이 바뀌었습니다.",409)
            if current["state"]=="cancelled":return
            conn.execute("UPDATE commerce_outbox SET state=?,response_json=?,updated_at=? WHERE operation_id=?",(receipt["state"],canonical(receipt),timestamp(),operation["operation_id"]))
            if receipt["state"]=="applied":
                conn.execute("UPDATE commerce_requests SET server_revision=? WHERE id=? AND (server_revision IS NULL OR server_revision<=?)",(revision,operation["request_id"],revision))

    def apply_batch(self,batch,expected_state=None):
        object_only(batch,{"schema_version","server_epoch","previous_cursor","cursor","events"},{"schema_version","server_epoch","previous_cursor","cursor","events"})
        if batch["schema_version"]!=1 or type(batch["schema_version"]) is not int:reject("commerce_remote_schema","지원하지 않는 접수 서버 형식입니다.",409)
        epoch=resource_identity(batch["server_epoch"])
        for key in ("cursor","previous_cursor"):
            if batch[key] is not None and (not isinstance(batch[key],str) or len(batch[key])>200 or not batch[key].isascii()):reject("commerce_cursor","동기화 위치를 확인해 주세요.")
        if not isinstance(batch["events"],list) or len(batch["events"])>100:reject("commerce_batch_limit","접수 묶음이 너무 큽니다.")
        parsed=[]
        for event in batch["events"]:
            object_only(event,{"event_id","entity_id","server_revision","operation","payload","resource_type","event_sequence"},{"event_id","entity_id","server_revision","operation","payload"})
            resource_identity(event["event_id"]);resource_identity(event["entity_id"])
            integer(event["server_revision"],"서버 버전",1,2**53-1)
            resource=event.get("resource_type","request")
            if resource not in ("request","catalog","slot"):reject("commerce_event","지원하지 않는 자료 종류입니다.")
            if "event_sequence" in event:integer(event["event_sequence"],"이벤트 순번",1,2**53-1)
            if event["operation"]=="upsert" and resource=="request":
                object_only(event["payload"],{"offering_id","adult_contact","participants","details","review_status","booking_status","payment_status","server_created_at","server_updated_at"},{"offering_id","adult_contact","participants"})
                contact(event["payload"]["adult_contact"]);integer(event["payload"]["participants"],"인원",optional=event["payload"].get("details",{}).get("kind") in ("finished_goods","kit"))
                if event["payload"].get("review_status","received") not in ("received","reviewing","closed") or event["payload"].get("booking_status","inquiry") not in ("inquiry","cancelled") or event["payload"].get("payment_status","not_configured")!="not_configured":reject("commerce_policy_unconfigured","확정 접수·결제 상태는 지원하지 않습니다.",409)
                if "details" in event["payload"]:
                    details=event["payload"]["details"]
                    object_only(details,{"kind","items","slot_id","price_source"},{"kind","items","slot_id","price_source"})
                    # Validate the canonical site details through the same flat
                    # public-contract allowlist, not an unchecked JSON field.
                    from site_contract_adapter import request_payload
                    request_payload({"id":event["entity_id"],"revision":event["server_revision"],"adult_name":event["payload"]["adult_contact"].get("name"),"contact_phone":event["payload"]["adult_contact"].get("phone"),"participant_count":event["payload"]["participants"],"kind":details["kind"],"items":details["items"],"slot_id":details["slot_id"],"review_status":event["payload"].get("review_status","received"),"booking_status":event["payload"].get("booking_status","inquiry"),"payment_status":"not_configured","created_at":event["payload"].get("server_created_at","not_supplied"),"updated_at":event["payload"].get("server_updated_at","not_supplied")})
                    if details["price_source"]!="server_request_snapshot_unconfirmed":reject("commerce_fields","가격 근거 표시를 확인해 주세요.")
            elif event["operation"]=="upsert" and resource in ("catalog","slot"):
                from site_contract_adapter import catalog_payload,slot_payload
                (catalog_payload if resource=="catalog" else slot_payload)(event["payload"])
                if event["payload"]["id"]!=event["entity_id"] or event["payload"]["revision"]!=event["server_revision"]:reject("commerce_identity","이벤트 ID 또는 자료 버전이 다릅니다.")
            elif event["operation"]=="delete" and resource=="request":object_only(event["payload"],set())
            else:reject("commerce_event","지원하지 않는 접수 변경입니다.")
            parsed.append((event,hashlib.sha256(canonical(event).encode("utf-8")).hexdigest()))
        applied=0
        with self.app.lock,self.app.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state={r["key"]:json.loads(r["value_json"]) for r in conn.execute("SELECT * FROM commerce_sync_state")}
            if expected_state is not None and state!=expected_state:reject("commerce_cursor_changed","다른 동기화가 먼저 반영되었습니다.",409)
            if state.get("server_epoch") not in (None,epoch):reject("commerce_epoch_changed","서버가 변경되었습니다. 자동으로 합치지 않고 재확인을 기다립니다.",409)
            if state.get("cursor")!=batch["previous_cursor"]:reject("commerce_cursor_changed","동기화 시작 위치가 다릅니다.",409)
            # Privacy deletion has priority. Its own committed inbox/redaction
            # survives an unrelated upsert conflict; ordinary cursor advancement
            # remains atomic with the rest of the successful batch.
            redacted=0
            for event,digest in parsed:
                if event["operation"]!="delete":continue
                key="request:"+event["entity_id"]
                old=conn.execute("SELECT payload_hash FROM commerce_inbox WHERE server_epoch=? AND event_id=?",(epoch,event["event_id"])).fetchone()
                if old:
                    if old[0]!=digest:reject("commerce_event_conflict","같은 삭제 이벤트의 내용이 다릅니다.",409)
                    continue
                row=conn.execute("SELECT * FROM commerce_requests WHERE server_request_id=?",(event["entity_id"],)).fetchone()
                highest=conn.execute("SELECT max(server_revision) FROM commerce_inbox WHERE server_epoch=? AND entity_id=?",(epoch,key)).fetchone()[0]
                if event["server_revision"]>max(highest or 0,row["server_revision"] or 0 if row else 0):
                    if row:
                        self._delete_contact(conn,row)
                        conn.execute("UPDATE commerce_requests SET server_revision=? WHERE id=?",(event["server_revision"],row["id"]))
                    redacted+=1
                conn.execute("INSERT INTO commerce_inbox VALUES (?,?,?,?,?,?,?)",(epoch,event["event_id"],key,event["server_revision"],digest,canonical({"operation":"delete","resource_type":"request","event_sequence":event.get("event_sequence")}),timestamp()))
            if any(event["operation"]=="delete" for event,_ in parsed):
                conn.commit()
                conn.execute("BEGIN IMMEDIATE")
            applied+=redacted
            for event,digest in parsed:
                resource=event.get("resource_type","request")
                entity_key=resource+":"+event["entity_id"]
                old=conn.execute("SELECT * FROM commerce_inbox WHERE server_epoch=? AND event_id=?",(epoch,event["event_id"])).fetchone()
                if old:
                    if old["payload_hash"]!=digest:reject("commerce_event_conflict","같은 서버 이벤트의 내용이 다릅니다.",409)
                    continue
                row=conn.execute("SELECT * FROM commerce_requests WHERE server_request_id=?",(event["entity_id"],)).fetchone() if resource=="request" else None
                highest=conn.execute("SELECT max(server_revision) FROM commerce_inbox WHERE server_epoch=? AND entity_id=?",(epoch,entity_key)).fetchone()[0]
                if event["server_revision"]>max(highest or 0,row["server_revision"] or 0 if row else 0):
                    if row and conn.execute("SELECT 1 FROM commerce_outbox WHERE request_id=? AND state IN ('pending','conflict')",(row["id"],)).fetchone():
                        reject("commerce_sync_conflict","확인 중인 로컬 변경과 서버 변경이 겹칩니다. 자동 덮어쓰기를 중단했습니다.",409)
                    if resource=="catalog":self._catalog(conn,event["payload"])
                    elif resource=="slot":self._slot(conn,event["payload"])
                    elif event["operation"]=="delete":
                        if row:self._delete_contact(conn,row)
                    else:
                        incoming=event["payload"]
                        offering=conn.execute("SELECT * FROM commerce_offerings WHERE id=?",(incoming["offering_id"],)).fetchone()
                        if not offering:reject("commerce_unknown_offering","서버 항목과 로컬 항목 연결을 먼저 확인해 주세요.",409)
                        if offering["offering_kind"]=="experience":integer(incoming["participants"],"인원")
                        if incoming.get("details",{}).get("kind",offering["offering_kind"])!=offering["offering_kind"]:reject("commerce_kind","문의와 항목 종류가 다릅니다.",409)
                        if row:
                            conn.execute("UPDATE commerce_requests SET revision=revision+1,server_revision=?,offering_id=?,contact_json=?,participants=?,updated_at=? WHERE id=?",(event["server_revision"],offering["id"],canonical(contact(incoming["adult_contact"])),incoming["participants"],timestamp(),row["id"]))
                            updated=self.app.commerce_store()._request(conn.execute("SELECT * FROM commerce_requests WHERE id=?",(row["id"],)).fetchone())
                            conn.execute("INSERT INTO commerce_request_history VALUES (?,?,?,?)",(row["id"],updated["revision"],canonical(updated),timestamp()))
                        else:
                            result=self.app.commerce_store()._insert_request(conn,uuid.uuid4().hex,"server",event["entity_id"],event["server_revision"],offering,contact(incoming["adult_contact"]),incoming["participants"])
                            row=conn.execute("SELECT * FROM commerce_requests WHERE id=?",(result["id"],)).fetchone()
                        if row and ("details" in incoming or "review_status" in incoming):
                            details=json.loads(row["details_json"]);details.update(incoming.get("details",{}))
                            conn.execute("UPDATE commerce_requests SET details_json=?,review_status=?,booking_status=?,payment_status='not_configured' WHERE id=?",(canonical(details),incoming.get("review_status",row["review_status"]),incoming.get("booking_status",row["booking_status"]),row["id"]))
                            updated=self.app.commerce_store()._request(conn.execute("SELECT * FROM commerce_requests WHERE id=?",(row["id"],)).fetchone())
                            conn.execute("UPDATE commerce_request_history SET snapshot_json=? WHERE request_id=? AND revision=?",(canonical(updated),row["id"],updated["revision"]))
                            if offering["offering_kind"] in ("finished_goods","kit"):
                                conn.execute("UPDATE commerce_orders SET details_json=?,revision=revision+1,updated_at=? WHERE request_id=?",(canonical({"status":"draft",**incoming.get("details",{}),"total":None,"fulfillment":None}),timestamp(),row["id"]))
                    if event["operation"]=="delete" and row:
                        conn.execute("UPDATE commerce_requests SET server_revision=? WHERE id=?",(event["server_revision"],row["id"]))
                    applied+=1
                # Inbox contains no contact copy, only provenance and a digest.
                conn.execute("INSERT INTO commerce_inbox VALUES (?,?,?,?,?,?,?)",(epoch,event["event_id"],entity_key,event["server_revision"],digest,canonical({"operation":event["operation"],"resource_type":resource,"event_sequence":event.get("event_sequence")}),timestamp()))
            for key,value in {"server_epoch":epoch,"cursor":batch["cursor"]}.items():
                conn.execute("INSERT INTO commerce_sync_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",(key,canonical(value)))
        return applied

    def _catalog(self,conn,payload):
        current=conn.execute("SELECT * FROM commerce_offerings WHERE id=?",(payload["id"],)).fetchone()
        old=json.loads(current["details_json"]) if current else {}
        if old.get("edited_locally"):reject("commerce_sync_conflict","로컬에서 수정한 항목과 서버 변경을 직접 비교해 주세요.",409)
        details={"price":payload.get("price"),"stock":payload.get("stock"),"state":payload.get("state","inquiry"),"sales_policy":None,"payment_policy":None,"published":False,"approval_status":"server_reference_unconfirmed","server_revision":payload["revision"],"edited_locally":False,"description":payload.get("description"),"art":payload.get("art"),"server_updated_at":payload.get("server_updated_at")}
        if current:conn.execute("UPDATE commerce_offerings SET revision=revision+1,title=?,offering_kind=?,program_id=?,details_json=?,updated_at=? WHERE id=?",(payload["title"],payload["offering_kind"],payload.get("program_id"),canonical(details),timestamp(),payload["id"]))
        else:conn.execute("INSERT INTO commerce_offerings VALUES (?,?,?,?,?,?,?,?)",(payload["id"],1,payload["title"],payload["offering_kind"],payload.get("program_id"),canonical(details),timestamp(),timestamp()))

    def _slot(self,conn,payload):
        if not conn.execute("SELECT 1 FROM commerce_offerings WHERE id=?",(payload["offering_id"],)).fetchone():reject("commerce_unknown_offering","일정에 연결된 항목이 없습니다.",409)
        current=conn.execute("SELECT * FROM commerce_slots WHERE id=?",(payload["id"],)).fetchone()
        old=json.loads(current["schedule_json"]) if current else {}
        if old.get("edited_locally"):reject("commerce_sync_conflict","로컬에서 수정한 일정과 서버 변경을 직접 비교해 주세요.",409)
        schedule={"starts_at":payload.get("starts_at"),"ends_at":payload.get("ends_at"),"capacity":payload.get("capacity"),"state":payload.get("state","draft"),"status":payload.get("state","draft"),"server_revision":payload["revision"],"edited_locally":False}
        if current:conn.execute("UPDATE commerce_slots SET offering_id=?,revision=revision+1,schedule_json=?,updated_at=? WHERE id=?",(payload["offering_id"],canonical(schedule),timestamp(),payload["id"]))
        else:conn.execute("INSERT INTO commerce_slots VALUES (?,?,?,?,?,?)",(payload["id"],payload["offering_id"],1,canonical(schedule),timestamp(),timestamp()))

    def _delete_contact(self,conn,row):
        conn.execute("UPDATE commerce_outbox SET state='cancelled',response_json=?,updated_at=? WHERE request_id=? AND state IN ('pending','conflict')",(canonical({"cancelled_by_redaction":True}),timestamp(),row["id"]))
        conn.execute("UPDATE commerce_requests SET revision=revision+1,contact_json='{}',review_status='closed',booking_status='cancelled',details_json=?,updated_at=? WHERE id=?",(canonical({"deleted_at_server":True}),timestamp(),row["id"]))
        for old in conn.execute("SELECT revision,snapshot_json FROM commerce_request_history WHERE request_id=?",(row["id"],)).fetchall():
            snapshot=json.loads(old["snapshot_json"]);snapshot["adult_contact"]={};snapshot["details"]={"deleted_at_server":True}
            conn.execute("UPDATE commerce_request_history SET snapshot_json=? WHERE request_id=? AND revision=?",(canonical(snapshot),row["id"],old["revision"]))
        for old in conn.execute("SELECT request_id,response_json FROM requests WHERE operation LIKE 'commerce_%'").fetchall():
            response=json.loads(old["response_json"])
            if isinstance(response.get("request"),dict) and response["request"].get("id")==row["id"]:
                response["request"]["adult_contact"]={};response["request"]["details"]={"deleted_at_server":True}
                conn.execute("UPDATE requests SET response_json=? WHERE request_id=?",(canonical(response),old["request_id"]))
