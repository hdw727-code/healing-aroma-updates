"""Pure mapping of the proposed Sites operator DTO; no connection/credentials.

Event stream sequence and entity revision are distinct. Site registration,
authorization, push-command contract and a real transport remain unconfigured.
"""
from commerce_store import contact, resource_identity as identity, integer, object_only, reject, text

def catalog_payload(value):
    object_only(value,{"id","revision","title","offering_kind","program_id","price","stock","state","description","art","server_updated_at"},{"id","revision","title","offering_kind"})
    identity(value["id"]);integer(value["revision"],"항목 버전",1,2**53-1);text(value["title"],"항목",80)
    if value["offering_kind"] not in ("experience","finished_goods","kit"):reject("commerce_kind","항목 종류를 확인해 주세요.")
    if value.get("program_id") is not None:text(value["program_id"],"프로그램",80)
    for key,limit in (("description",1000),("art",120),("server_updated_at",40)):
        if key in value:text(value[key],key,limit,optional=True)
    for key in ("price","stock"):integer(value.get(key),key,0,1_000_000_000,optional=True)
    if value.get("state","inquiry") not in ("inquiry","ready","paused"):reject("commerce_state","항목 상태를 확인해 주세요.")
    return dict(value)

def slot_payload(value):
    object_only(value,{"id","revision","offering_id","starts_at","ends_at","capacity","state","server_updated_at"},{"id","revision","offering_id"})
    identity(value["id"]);identity(value["offering_id"]);integer(value["revision"],"일정 버전",1,2**53-1)
    from datetime import datetime
    for key in ("starts_at","ends_at"):
        raw=text(value.get(key),"일정",40,optional=True)
        if raw:
            try:parsed=datetime.fromisoformat(raw)
            except ValueError:reject("commerce_schedule","일정 형식을 확인해 주세요.")
            if parsed.utcoffset() is None:reject("commerce_schedule","시간대가 필요합니다.")
    if value.get("starts_at") and value.get("ends_at") and datetime.fromisoformat(value["ends_at"])<=datetime.fromisoformat(value["starts_at"]):reject("commerce_schedule","일정 순서를 확인해 주세요.")
    integer(value.get("capacity"),"정원",optional=True)
    if value.get("state","draft") not in ("draft","ready","paused"):reject("commerce_state","일정 상태를 확인해 주세요.")
    if value.get("state")=="ready" and (not value.get("starts_at") or value.get("capacity") is None):reject("commerce_schedule","준비된 일정에는 시간과 정원이 필요합니다.")
    return dict(value)

def request_payload(value):
    allowed={"id","kind","adult_name","contact_phone","participant_count","items","slot_id","review_status","booking_status","payment_status","revision","created_at","updated_at"}
    object_only(value,allowed,allowed)
    identity(value["id"]);integer(value["revision"],"접수 버전",1,2**53-1)
    if value["kind"] not in ("experience","product","finished_goods","kit"):reject("commerce_kind","접수 종류를 확인해 주세요.")
    responsible=contact({"name":value["adult_name"],"phone":value["contact_phone"]})
    integer(value["participant_count"],"인원",optional=value["kind"] in ("product","finished_goods","kit"))
    if value["review_status"] not in ("received","reviewing","closed") or value["booking_status"] not in ("inquiry","cancelled") or value["payment_status"]!="not_configured":reject("commerce_policy_unconfigured","이번 문의판의 접수 상태가 아닙니다.",409)
    if not isinstance(value["items"],list) or not 1<=len(value["items"])<=20:reject("commerce_items","문의 항목을 확인해 주세요.")
    items=[]
    for item in value["items"]:
        object_only(item,{"offering_id","title","quantity","price_at_request"},{"offering_id","title","quantity","price_at_request"})
        identity(item["offering_id"]);text(item["title"],"항목",80)
        integer(item["quantity"],"수량",optional=True)
        integer(item["price_at_request"],"문의 당시 가격",0,1_000_000_000,optional=True)
        items.append(dict(item))
    if value["slot_id"] is not None:identity(value["slot_id"])
    for key in ("created_at","updated_at"):text(value[key],"접수 시각",40)
    return {"offering_id":items[0]["offering_id"],"adult_contact":responsible,"participants":value["participant_count"],
        "review_status":value["review_status"],"booking_status":value["booking_status"],"payment_status":value["payment_status"],
        "details":{"kind":"finished_goods" if value["kind"]=="product" else value["kind"],"items":items,"slot_id":value["slot_id"],"price_source":"server_request_snapshot_unconfirmed"},
        "server_created_at":value["created_at"],"server_updated_at":value["updated_at"]}

def map_operator_batch(batch,*,server_epoch,previous_cursor):
    object_only(batch,{"events","next_cursor","has_more"},{"events","next_cursor","has_more"})
    if type(batch["has_more"]) is not bool or not isinstance(batch["events"],list) or len(batch["events"])>100:reject("commerce_batch_limit","서버 이벤트 묶음을 확인해 주세요.")
    if not isinstance(batch["next_cursor"],str) or len(batch["next_cursor"])>200 or not batch["next_cursor"].isascii() or not batch["next_cursor"].isdigit():reject("commerce_cursor","서버 위치를 확인해 주세요.")
    if previous_cursor is not None and (not str(previous_cursor).isdigit() or int(batch["next_cursor"])<int(previous_cursor)):reject("commerce_cursor","서버 위치가 뒤로 이동했습니다.",409)
    events=[]
    for event in batch["events"]:
        object_only(event,{"event_id","server_revision","event_type","resource_id","payload","created_at"},{"event_id","server_revision","event_type","resource_id","payload","created_at"})
        identity(event["event_id"]);identity(event["resource_id"]);integer(event["server_revision"],"이벤트 순번",1,2**53-1);text(event["created_at"],"시각",40)
        if event["server_revision"]>int(batch["next_cursor"]):reject("commerce_cursor","이벤트 순번이 동기화 위치를 넘습니다.")
        kind=event["event_type"]
        if kind=="request.upsert":
            payload=request_payload(event["payload"]);resource="request";operation="upsert";revision=event["payload"]["revision"]
        elif kind=="request.redacted":
            object_only(event["payload"],{"id","revision","deleted","updated_at"},{"id","revision"})
            identity(event["payload"]["id"]);integer(event["payload"]["revision"],"접수 버전",1,2**53-1)
            if "deleted" in event["payload"] and event["payload"]["deleted"] is not True:reject("commerce_event","연락처 삭제 표시를 확인해 주세요.")
            if "updated_at" in event["payload"]:text(event["payload"]["updated_at"],"삭제 시각",40)
            payload={};resource="request";operation="delete";revision=event["payload"]["revision"]
        elif kind=="catalog.upsert":
            raw=event["payload"]
            if "kind" in raw:
                object_only(raw,{"id","kind","title","description","art","price","stock","state","expected_revision","revision","updated_at"},{"id","kind","title","price","stock","state","revision","updated_at"})
                if raw["kind"] not in ("experience","product"):reject("commerce_kind","서버 항목 종류를 확인해 주세요.")
                if raw.get("expected_revision") is not None:integer(raw["expected_revision"],"이전 항목 버전",0,2**53-1)
                value={key:raw[key] for key in ("id","revision","title","price","stock","state","description","art") if key in raw}
                value.update({"offering_kind":"experience" if raw["kind"]=="experience" else "finished_goods","program_id":None,"server_updated_at":raw["updated_at"]})
            else:value=raw # Canonical synthetic fixtures, not a second remote schema.
            payload=catalog_payload(value);resource="catalog";operation="upsert";revision=payload["revision"]
        elif kind=="slot.upsert":
            raw=event["payload"]
            if "expected_revision" in raw or "updated_at" in raw:
                object_only(raw,{"id","offering_id","starts_at","capacity","state","expected_revision","revision","updated_at"},{"id","offering_id","starts_at","capacity","state","revision","updated_at"})
                if raw.get("expected_revision") is not None:integer(raw["expected_revision"],"이전 일정 버전",0,2**53-1)
                value={key:raw[key] for key in ("id","revision","offering_id","starts_at","capacity","state")}
                if value["state"]=="inquiry":value["state"]="draft"
                value.update({"ends_at":None,"server_updated_at":raw["updated_at"]})
            else:value=raw
            payload=slot_payload(value);resource="slot";operation="upsert";revision=payload["revision"]
        else:reject("commerce_event","지원하지 않는 이벤트입니다.")
        if event["payload"]["id"]!=event["resource_id"]:reject("commerce_identity","이벤트와 자료 ID가 다릅니다.")
        events.append({"event_id":event["event_id"],"entity_id":event["resource_id"],"server_revision":revision,"resource_type":resource,"operation":operation,"payload":payload,"event_sequence":event["server_revision"]})
    return {"schema_version":1,"server_epoch":server_epoch,"previous_cursor":previous_cursor,"cursor":batch["next_cursor"],"events":events}

class SiteContractTransport:
    """Wrap a caller-injected fixture client. Never constructs a real client."""
    outgoing_allowed=False
    def __init__(self,client,*,server_epoch):self.client,self.server_epoch=client,identity(server_epoch)
    def pull(self,epoch,cursor,limit):
        if epoch not in (None,self.server_epoch):reject("commerce_epoch_changed","서버가 변경되었습니다.",409)
        return map_operator_batch(self.client.events(cursor,limit),server_epoch=self.server_epoch,previous_cursor=cursor)
    def operation_status(self,operation_id):
        lookup=getattr(self.client,"operation_status",None)
        if not callable(lookup):reject("commerce_push_unconfigured","서버 쓰기 상태 계약이 등록되지 않았습니다.",503)
        return lookup(operation_id)
    def push(self,*args):reject("commerce_push_unconfigured","서버 쓰기 계약이 등록되지 않았습니다. 전송하지 않았습니다.",503)
