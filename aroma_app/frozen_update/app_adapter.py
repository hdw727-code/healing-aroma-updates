"""Stable local handoff adapter. Startup secret stays in process memory only."""
import hashlib
import hmac
import http.client
import json
import re
from urllib.parse import urlsplit


class HandoffClient:
    def __init__(self, port, secret, nonce):
        self.port, self.secret, self.nonce = port, secret, nonce

    def request(self, method, path, payload=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60 if path=='/check' else 5)
        headers = {"X-Supervisor-Secret": self.secret, "X-App-Instance": self.nonce, "Content-Type":"application/json"}
        try:
            conn.request(method,path,None if payload is None else json.dumps(payload).encode(),headers)
            response = conn.getresponse(); data = response.read(65537)
            if len(data) > 65536: raise ValueError("Supervisor response is too large")
            result = json.loads(data)
            if response.status >= 400: raise ValueError(result.get("message","Update handoff failed"))
            return result
        finally: conn.close()


def attach(server_module, store, client):
    """Return a Handler class for the trusted runner; never alters a main file.

    Parent production patch may call the same small hooks after its normal
    security checks instead. This prototype requires the full serialized server.
    """
    parent = server_module.Handler
    store.update_pending = False
    store.update_pending_operation = None
    class UpdateHandler(parent):
        def _do_GET_serialized(self):
            path = urlsplit(self.path).path
            if path == "/api/health":
                try:
                    self.check_local()
                    with self.store.connect() as conn:
                        schema = conn.execute("PRAGMA user_version").fetchone()[0]
                    return self.json_response({"app_id":"aroma-work-helper","instance_root":str(self.store.root),
                        "data_dir":str(self.store.data_dir),"schema_version":schema,
                        "recovery_required":bool(self.store.recovery_pending()),"version":server_module.APP_VERSION})
                except server_module.AppError as error:
                    return self.error_response(error)
                except Exception:
                    return self.error_response(server_module.AppError("App health is unavailable.",503,"health_failed"))
            if (path == "/api/updates/status" or re.fullmatch(r"/api/updates/operations/[a-f0-9]{32}",path) or
                re.fullmatch(r'/api/updates/requests/[A-Za-z0-9_-]{8,100}',path)):
                try:
                    self.check_local()
                    route = "/status" if path == "/api/updates/status" else ('/requests/' if '/requests/' in path else '/operations/') + path.rsplit("/",1)[1]
                    result = client.request("GET",route)
                    if route=='/status' and self.store.recovery_pending():
                        result.update({'enabled':False,'apply_available':False,'status':'work_recovery_required','recovery_required':True})
                    if route=='/status': self.store.remote_update_enabled=result.get('enabled') is True
                    operation = result.get("operation") if route.startswith('/requests/') else result
                    if (self.store.update_pending and isinstance(operation, dict)
                            and operation.get("id") == self.store.update_pending_operation
                            and self.store.update_pending_operation is not None
                            and operation.get("state") in ("expired", "failed")
                            and operation.get("old_app_still_running") is True):
                        self.store.update_pending = False
                        self.store.update_pending_operation = None
                    return self.json_response(result)
                except server_module.AppError as error:
                    return self.error_response(error)
                except (ValueError,OSError):
                    return self.error_response(server_module.AppError("Update supervisor is unavailable.",503,"update_unavailable"))
            if self.store.update_pending and path.startswith(("/api/","/exports/")) and path != "/api/bootstrap":
                try: self.check_local()
                except server_module.AppError as error:return self.error_response(error)
                return self.error_response(server_module.AppError("Update is preparing. Work data is preserved.",409,"update_in_progress"))
            return super()._do_GET_serialized()

        def _do_POST_serialized(self):
            path = urlsplit(self.path).path
            if path == '/api/updates/check':
                self._json_body_consumed=False
                try:
                    self.check_local()
                    token=self.headers.get('X-App-Token','')
                    if not token.isascii() or len(token)!=len(self.store.csrf_token) or not hmac.compare_digest(token,self.store.csrf_token):
                        raise server_module.AppError('Reopen the app and try again.',403,'session_expired')
                    self.read_json();self.store.require_no_recovery()
                    if self.store.update_pending:raise server_module.AppError('Update is preparing.',409,'update_in_progress')
                    return self.json_response(client.request('POST','/check',{}))
                except server_module.AppError as error:
                    self.drain_rejected_json();return self.error_response(error)
                except (ValueError,OSError):
                    self.drain_rejected_json();return self.error_response(server_module.AppError('Public update check failed. Current work remains available.',503,'update_check_failed'))
            if path != "/api/updates/apply":
                if self.store.update_pending and path != "/api/shutdown":
                    self._json_body_consumed=False
                    try: self.check_local()
                    except server_module.AppError as error:
                        self.drain_rejected_json(); return self.error_response(error)
                    self.drain_rejected_json()
                    return self.error_response(server_module.AppError("Update is preparing.",409,"update_in_progress"))
                return super()._do_POST_serialized()
            self._json_body_consumed=False
            response_written = False
            previous_pending = bool(self.store.update_pending)
            previous_operation = self.store.update_pending_operation
            try:
                self.check_local()
                token=self.headers.get("X-App-Token","")
                if not token.isascii() or len(token)!=len(self.store.csrf_token) or not hmac.compare_digest(token,self.store.csrf_token):
                    raise server_module.AppError("Reopen the app and try again.",403,"session_expired")
                if previous_pending:
                    raise server_module.AppError("Update is preparing.",409,"update_in_progress")
                payload=self.read_json()
                self.store.require_admin(self.headers.get("X-Admin-Token"))
                self.store.require_no_recovery()
                self.store.require_runtime_idle()
                if payload.get("confirm") is not True:
                    raise server_module.AppError("Confirm manual update first.",422,"confirmation_required")
                self.store.full_backup_api().service._active_ai()
                # Existing all-request RLock means previous file readers/writers
                # ended. Block new work immediately, before handoff is sent.
                self.store.update_pending=True
                offer=client.request("POST","/handoff/apply",{**payload,"pin_checked":True})
                operation_id = offer.get("operation_id") if isinstance(offer, dict) else None
                if not isinstance(operation_id, str) or not re.fullmatch(r"[a-f0-9]{32}", operation_id):
                    raise ValueError("Invalid apply operation identity")
                self.store.update_pending_operation = operation_id
                self.json_response(offer,202)
                response_written = True
                # Starting work requires this separate acknowledgement, emitted
                # only AFTER the app's response was completely written.
                client.request("POST","/handoff/ack",{"operation_id":offer["operation_id"]})
            except server_module.AppError as error:
                if response_written: return
                self.store.update_pending=previous_pending
                self.store.update_pending_operation=previous_operation
                self.drain_rejected_json(); self.error_response(error)
            except (ValueError,OSError) as error:
                # Once the browser received 202, acknowledgement may have
                # succeeded even if its reply was lost. Keep work blocked and
                # let status/expiry reconcile it; never write a second reply.
                if response_written: return
                self.store.update_pending=previous_pending
                self.store.update_pending_operation=previous_operation
                if not self.wfile.closed:
                    self.drain_rejected_json()
                    self.error_response(server_module.AppError("Update handoff failed; current data is preserved.",503,"update_handoff_failed"))
    return UpdateHandler
