"""Explicit, memory-only HTTPS reader; no site is configured in public releases.

This module is not wired to AppStore. Construction/status performs no I/O.
Only separately reviewed local configuration can enable a site. A caller must
also supply both separate access grants before GET methods can be called.
No credential file, environment lookup, login, OAuth, logging or write API.
"""
from __future__ import annotations
import json
import math
import re
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPSHandler, HTTPRedirectHandler
from commerce_store import CommerceError
from site_contract_adapter import map_operator_batch

APPROVED_SITE_ORIGIN=None
MAX_RESPONSE_BYTES=2*1024*1024
MAX_JSON_DEPTH=32
MAX_JSON_NODES=100000

class CommerceHTTPError(CommerceError):
    """Public fields contain fixed messages only, never upstream exception text."""

def fail(code,message,status=502):
    raise CommerceHTTPError(code,message,status) from None

class NoRedirect(HTTPRedirectHandler):
    def _blocked(self,request,fp,code,msg,headers):
        try:fp.close()
        except Exception:pass
        fail("commerce_redirect_blocked","접속 주소 변경을 거부했습니다. 등록된 사이트 주소를 확인해 주세요.")
    http_error_301=_blocked
    http_error_302=_blocked
    http_error_303=_blocked
    http_error_307=_blocked
    http_error_308=_blocked
    def redirect_request(self,*args,**kwargs):
        fail("commerce_redirect_blocked","접속 주소 변경을 거부했습니다.")

def _pairs(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ValueError("duplicate key")
        value[key]=item
    return value

def _constant(value):raise ValueError("nonfinite number")

def _bounded_json(raw):
    value=json.loads(raw.decode("utf-8"),object_pairs_hook=_pairs,parse_constant=_constant)
    stack=[(value,0)];nodes=0
    while stack:
        node,depth=stack.pop();nodes+=1
        if depth>MAX_JSON_DEPTH or nodes>MAX_JSON_NODES:raise ValueError("JSON structure limit")
        if isinstance(node,float) and not math.isfinite(node):raise ValueError("nonfinite number")
        if isinstance(node,dict):stack.extend((v,depth+1) for v in node.values())
        elif isinstance(node,list):stack.extend((v,depth+1) for v in node)
    if not isinstance(value,dict):raise ValueError("object required")
    return value

class ReadOnlyHTTPSClient:
    outgoing_allowed=False
    def __init__(self,*,site_origin,device_bearer,private_site_bearer,opener=None,
                 timeout_seconds=10,max_response_bytes=MAX_RESPONSE_BYTES):
        # No environment/profile probing, URL parsing, credential validation,
        # network setup or opener inspection before this fail-closed guard.
        if not isinstance(APPROVED_SITE_ORIGIN,str) or not APPROVED_SITE_ORIGIN:
            fail("commerce_not_configured","접수 사이트 연결이 설정되지 않았습니다.",503)
        # Exact origin: no userinfo, port, subdomain, relative path or query.
        if not isinstance(site_origin,str):fail("commerce_site_origin","등록된 HTTPS 사이트 주소가 필요합니다.",400)
        if site_origin not in (APPROVED_SITE_ORIGIN,APPROVED_SITE_ORIGIN+"/"):
            fail("commerce_site_origin","허용된 HTTPS 사이트 주소만 사용할 수 있습니다.",400)
        parsed=urlsplit(site_origin)
        if (site_origin.rstrip("/")!=APPROVED_SITE_ORIGIN or parsed.scheme!="https" or
            parsed.netloc!=urlsplit(APPROVED_SITE_ORIGIN).netloc or parsed.path not in ("","/") or parsed.query or parsed.fragment):
            fail("commerce_site_origin","허용된 HTTPS 사이트 주소만 사용할 수 있습니다.",400)
        for secret in (device_bearer,private_site_bearer):
            if not isinstance(secret,str) or not 8<=len(secret)<=4096 or not re.fullmatch(r"[A-Za-z0-9._~+/\-]+=*",secret):
                fail("commerce_access_required","별도로 승인된 두 접속 권한이 필요합니다.",400)
        if device_bearer==private_site_bearer:fail("commerce_access_separation","사이트와 앱 접속 권한은 별도 값이어야 합니다.",400)
        if isinstance(timeout_seconds,bool) or not isinstance(timeout_seconds,(int,float)) or not 0.1<=timeout_seconds<=30:
            fail("commerce_timeout_configuration","접속 제한 시간을 확인해 주세요.",400)
        if type(max_response_bytes) is not int or not 256<=max_response_bytes<=8*1024*1024:
            fail("commerce_response_configuration","응답 크기 제한을 확인해 주세요.",400)
        if opener is not None and not callable(getattr(opener,"open",None)):
            fail("commerce_opener_configuration","접속 처리기를 확인해 주세요.",400)
        self.origin=APPROVED_SITE_ORIGIN
        self._device_bearer,self._private_site_bearer=device_bearer,private_site_bearer
        self._opener=opener
        self.timeout_seconds,self.max_response_bytes=timeout_seconds,max_response_bytes
        self._capabilities_checked=False

    def __repr__(self):return "<ReadOnlyHTTPSClient explicit-memory-access; no automatic connection>"

    def status(self):
        return {"mode":"read_only_https","configured":True,"connected":False,
            "capabilities_checked":self._capabilities_checked,"credentials_persisted":False,
            "automatic_sync":False,"cloud_writes":False,"constructor_wired":False}

    def _production_opener(self):
        context=ssl.create_default_context()
        context.check_hostname=True
        context.verify_mode=ssl.CERT_REQUIRED
        context.minimum_version=ssl.TLSVersion.TLSv1_2
        # Do not use ambient proxy credentials/configuration; no cookies/login.
        return build_opener(ProxyHandler({}),NoRedirect(),HTTPSHandler(context=context))

    def _get(self,path,query=None):
        if not isinstance(APPROVED_SITE_ORIGIN,str) or not APPROVED_SITE_ORIGIN:
            fail("commerce_not_configured","접수 사이트 연결이 설정되지 않았습니다.",503)
        if self.origin!=APPROVED_SITE_ORIGIN:
            fail("commerce_site_origin","등록된 사이트 주소가 변경되어 접속하지 않습니다.",400)
        if path not in ("/api/v1/operator/capabilities","/api/v1/operator/events"):
            fail("commerce_read_only","이 접속기는 접수 읽기만 지원합니다.",405)
        if path.endswith("/capabilities") and query is not None:
            fail("commerce_read_only","접속 확인에는 조회 인자를 보내지 않습니다.",400)
        if path.endswith("/events") and (not isinstance(query,dict) or set(query)!={"after","limit"} or
            not isinstance(query["after"],str) or not re.fullmatch(r"[0-9]{1,12}",query["after"]) or
            not isinstance(query["limit"],str) or not re.fullmatch(r"[0-9]{1,3}",query["limit"]) or not 1<=int(query["limit"])<=100):
            fail("commerce_read_only","접수 조회 인자를 확인해 주세요.",400)
        url=APPROVED_SITE_ORIGIN+path+("?"+urlencode(query) if query else "")
        request=Request(url,method="GET",headers={"Accept":"application/json","Accept-Encoding":"identity",
            "Authorization":"Bearer "+self._device_bearer,
            "OAI-Sites-Authorization":"Bearer "+self._private_site_bearer,
            "User-Agent":"Aroma-Commerce/2"})
        response=None
        try:
            opener=self._opener if self._opener is not None else self._production_opener()
            response=opener.open(request,timeout=self.timeout_seconds)
            status=response.status
            if 300<=status<400:fail("commerce_redirect_blocked","접속 주소 변경을 거부했습니다.")
            if status!=200:self._status_error(status)
            if response.geturl()!=url:fail("commerce_redirect_blocked","응답 주소가 등록된 요청과 다릅니다.")
            headers=response.headers
            content_type=headers.get("Content-Type","").split(";",1)[0].strip().lower()
            if content_type!="application/json":fail("commerce_response_invalid","JSON 접수 응답을 확인하지 못했습니다.")
            if headers.get("Content-Encoding","").strip().lower() not in ("","identity"):
                fail("commerce_response_encoding","압축된 응답은 받지 않습니다.")
            length=headers.get("Content-Length")
            if length is not None:
                if not re.fullmatch(r"[0-9]{1,12}",length):fail("commerce_response_invalid","응답 크기 형식을 확인하지 못했습니다.")
                if int(length)>self.max_response_bytes:fail("commerce_response_too_large","접수 응답이 크기 제한을 넘었습니다.")
            raw=response.read(self.max_response_bytes+1)
            if not isinstance(raw,bytes):fail("commerce_response_invalid","접수 응답을 읽지 못했습니다.")
            if len(raw)>self.max_response_bytes:fail("commerce_response_too_large","접수 응답이 크기 제한을 넘었습니다.")
            if length is not None and len(raw)!=int(length):fail("commerce_response_invalid","접수 응답이 끝까지 도착하지 않았습니다.")
            return _bounded_json(raw)
        except CommerceHTTPError:raise
        except HTTPError as error:
            # Never read or echo its body/reason/url: it may include credentials.
            try:self._status_error(error.code)
            finally:
                try:error.close()
                except Exception:pass
        except (TimeoutError,socket.timeout):
            fail("commerce_timeout","접수 서버 응답이 늦습니다. 잠시 후 직접 다시 시도해 주세요.",504)
        except URLError as error:
            if isinstance(error.reason,(TimeoutError,socket.timeout)):
                fail("commerce_timeout","접수 서버 응답이 늦습니다.",504)
            fail("commerce_connection_failed","접수 서버에 연결하지 못했습니다.",503)
        except Exception:
            fail("commerce_response_invalid","접수 응답을 안전하게 확인하지 못했습니다.")
        finally:
            if response is not None:
                try:response.close()
                except Exception:pass

    def _status_error(self,status):
        if 300<=status<400:fail("commerce_redirect_blocked","접속 주소 변경을 거부했습니다.")
        if status in (401,403):fail("commerce_authorization_required","사이트와 앱의 접속 권한을 확인해 주세요.",503)
        if status==503:fail("commerce_remote_unavailable","앱 연결 자격이 미등록이거나 서버가 준비 중입니다.",503)
        fail("commerce_remote_rejected","접수 서버가 읽기 요청을 처리하지 못했습니다.")

    def capabilities(self):
        value=self._get("/api/v1/operator/capabilities")
        if (set(value)!={"protocol_version","mode","payments","confirmation"} or
            type(value["protocol_version"]) is not int or value["protocol_version"]!=1 or
            value["mode"]!="read_only_intake" or value["payments"] is not False or value["confirmation"] is not False):
            fail("commerce_protocol_mismatch","이 앱과 호환되는 읽기 전용 접수 서버가 아닙니다.",409)
        self._capabilities_checked=True
        return value

    def events(self,after,limit=100):
        if after is None:after="0"
        if not isinstance(after,str) or not re.fullmatch(r"[0-9]{1,12}",after):
            fail("commerce_cursor","동기화 위치는 숫자 순번이어야 합니다.",400)
        if type(limit) is not int or not 1<=limit<=100:fail("commerce_limit","한 번에 읽는 접수 수를 확인해 주세요.",400)
        value=self._get("/api/v1/operator/events",{"after":after,"limit":str(limit)})
        try:
            # Existing strict mapping rejects child/health/extra fields and
            # validates event/resource revisions. This temporary epoch is not an
            # auth identity; the owning SiteContractTransport provides its epoch.
            map_operator_batch(value,server_epoch="http_validation",previous_cursor=after)
        except Exception:
            fail("commerce_event_dto_invalid","접수 서버의 자료 형식을 확인하지 못했습니다.")
        return value
