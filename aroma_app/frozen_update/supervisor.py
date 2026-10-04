"""Stable own-process update supervisor. No provider/production key configured.

Private startup secrets are ephemeral local capabilities, never account tokens.
The real-process synthetic test supplies an ephemeral CNG publisher public key.
"""
from __future__ import annotations
import contextlib
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

TRUSTED=Path(__file__).resolve().parent
sys.path.insert(0,str(TRUSTED))
from update_client import SideBySideUpdater, ManifestVerifier, PayloadPolicy, UpdateError, canonical, digest_file, read_json, atomic_json


class SupervisorError(ValueError):
    def __init__(self,code,message,status=400):
        self.code,self.message,self.status=code,message,status
        super().__init__(message)


class OwnedProcess:
    def __init__(self,process,slot,nonce,secret,ready):
        self.process,self.slot,self.nonce,self.secret=process,dict(slot),nonce,secret
        self.port,self.csrf=ready["port"],ready["csrf_token"]
        self.version=ready['version']


class StableSupervisor:
    def __init__(self, state_root, *, python_exe, runtime_sha256, pinned_public_key=None,
                 app_port=8765, installation_root=None, examples_dir=None, grant_ttl=60,
                 startup_timeout=10, stop_timeout=12, code_paths=None, provider=None):
        self.root=Path(state_root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.python=Path(python_exe).resolve()
        if not re.fullmatch(r"[a-f0-9]{64}",runtime_sha256) or digest_file(self.python)!=runtime_sha256:
            raise SupervisorError("runtime_mismatch","The stable runtime did not match its pinned hash.")
        self.owner_path=self.root/'.supervisor.lock'
        self.runtime_sha=runtime_sha256
        self.app_port,self.examples_dir=app_port,None if examples_dir is None else str(Path(examples_dir).resolve())
        self.grant_ttl,self.startup_timeout,self.stop_timeout=grant_ttl,startup_timeout,stop_timeout
        self.secret=secrets.token_hex(32)
        self.current=None;self.owned=[];self.grants={};self.operations={};self.requests={};self.candidates={};self.release_versions={};self.launch_files={}
        self.lock=threading.RLock();self.workers=[]
        self.provider,self.check_count,self.checking=provider,0,False
        self.verifier=None if pinned_public_key is None else ManifestVerifier(pinned_public_key,PayloadPolicy(code_paths=code_paths))
        self.policy=PayloadPolicy(code_paths=code_paths)
        self.updater=None if self.verifier is None else SideBySideUpdater(self.root,self.verifier,self,installation_root=installation_root)
        try:self.owner_handle=self.owner_path.open('xb')
        except FileExistsError:
            raise SupervisorError('supervisor_owner_unknown','An existing supervisor owner must be reviewed; no PID was terminated.',409)
        try:self.http=ThreadingHTTPServer(("127.0.0.1",0),self._handler_type())
        except Exception:
            self.owner_handle.close();self.owner_path.unlink(missing_ok=True);raise
        self.http.daemon_threads=False
        self.thread=threading.Thread(target=self.http.serve_forever,name="trusted-local-supervisor",daemon=True);self.thread.start()

    def initialize(self, code_dir, data_dir, *, content_dir=None, private_root=None):
        if (self.root/'initial_slot.json').exists() or (self.updater and self.updater.state_path.exists()):
            raise SupervisorError('already_initialized','Use resume for the existing installation.',409)
        # The first public install has no user_data yet. This trusted launcher
        # path must be created before updater state is initialized, while linked
        # ancestors are rejected before canonicalization or any app DB write.
        initial_data = Path(os.path.abspath(data_dir))
        try:
            for phase in range(2):
                for probe in (initial_data, *initial_data.parents):
                    if probe.is_symlink() or probe.is_junction():
                        raise SupervisorError("initial_data_link", "Initial data path must not contain links.")
                if phase == 0:
                    initial_data.mkdir(parents=True, exist_ok=True)
            if not initial_data.is_dir():
                raise SupervisorError("initial_data_path", "Initial data directory is unavailable.")
            initial_data = initial_data.resolve(strict=True)
        except OSError:
            raise SupervisorError("initial_data_path", "Initial data directory could not be prepared.") from None
        slot={"code_dir":str(Path(code_dir).resolve()),"data_dir":str(initial_data),
              "schema_version":1,"sequence":0,"release_id":"initial"}
        if content_dir is not None:slot["local_content_dir"]=str(Path(content_dir).resolve())
        if private_root is not None:slot["local_config_dir"]=os.path.abspath(private_root) # No profile probe/read.
        # Only explicitly allowed existing code files are read. Never inventory
        # the installation root containing private profiles or business data.
        self.launch_files['initial']=[{'path':name,'size':(Path(slot['code_dir'])/name).stat().st_size,
            'sha256':digest_file(Path(slot['code_dir'])/name)} for name in sorted(self.policy.code_paths)
            if (Path(slot['code_dir'])/name).is_file()]
        atomic_json(self.root/'initial_slot.json',{'slot':slot,'verified_files':self.launch_files['initial'],'app_port':self.app_port})
        if self.updater:
            self.updater.initialize(code_dir=slot["code_dir"],data_dir=slot["data_dir"])
            state=self.updater.status();state["active"].update(slot)
            atomic_json(self.updater.state_path,state)
        self.current=self.start(slot)
        if not self.health(self.current,slot,allow_recovery=True):
            self.stop(self.current)
            raise SupervisorError("initial_health_failed","The initial owned app failed its health check.")
        atomic_json(self.root/'initial_slot.json',{'slot':slot,'verified_files':self.launch_files['initial'],'app_port':self.app_port})
        return self.safe_status()

    def resume(self):
        """Resume only a previously verified active slot after a clean exit.
        Stale supervisor/update locks or an incomplete journal require manual
        owner review; never guess a PID or start a second writer.
        """
        initial=self._read_local_json(self.root/'initial_slot.json',1024*1024)
        saved_port=initial.get('app_port')
        if type(saved_port) is not int or not 1<=saved_port<=65535:raise SupervisorError('port_state','Saved app origin is unavailable.')
        if self.app_port not in (0,saved_port):raise SupervisorError('port_changed','The saved app origin must remain the same.',409)
        self.app_port=saved_port
        self.launch_files['initial']=initial['verified_files']
        slot=initial['slot'] if self.updater is None else self.updater.status()['active']
        if self.updater:
            if (self.root/'apply.lock').exists():
                raise SupervisorError('update_owner_unknown','A previous update owner must be reviewed.',409)
            if self.updater.journal_path.exists() and self._read_local_json(self.updater.journal_path).get('phase') not in ('committed','rolled_back','aborted'):
                raise SupervisorError('update_recovery_required','Interrupted update requires explicit recovery.',409)
        if slot['release_id']!='initial':
            candidate_root=self.root/'candidates'
            if candidate_root.is_symlink() or candidate_root.is_junction():raise SupervisorError('receipt_path','Unsafe release directory.')
            receipts=list(candidate_root.glob('*/accepted.json'))
            if len(receipts)>4096:raise SupervisorError('receipt_limit','Too many local release receipts.')
            matched=False
            for receipt_path in receipts:
                if (receipt_path.is_symlink() or receipt_path.parent.is_symlink() or receipt_path.parent.is_junction() or
                    not re.fullmatch('[a-f0-9]{32}',receipt_path.parent.name) or receipt_path.stat().st_size>16384):
                    raise SupervisorError('receipt_path','Local receipt path needs review.')
                receipt=self._read_local_json(receipt_path)
                if receipt.get('release_id')!=slot['release_id']:continue
                path=receipt_path.parent/'manifest.json'
                if path.is_symlink() or path.stat().st_size>512*1024:raise SupervisorError('receipt_path','Unsafe release manifest.')
                verified=self.verifier.verify(path.read_bytes(),current_sequence=slot['sequence']-1,
                    current_schema=slot['schema_version'],now=receipt['accepted_at'])
                manifest=verified['manifest']
                if (manifest['release_id']!=slot['release_id'] or manifest['sequence']!=slot['sequence'] or
                    verified['manifest_sha256']!=receipt['manifest_sha256']):
                    raise SupervisorError('receipt_changed','Accepted release receipt changed.')
                self.launch_files[slot['release_id']]=manifest['files'];self.release_versions[slot['release_id']]=manifest['version']
                matched=True;break
            if not matched:raise SupervisorError('receipt_missing','Active release has no trusted signed receipt.')
        self.current=self.start(slot)
        if not self.health(self.current,slot,allow_recovery=True):
            self.stop(self.current);raise SupervisorError('resume_health_failed','Active app did not pass startup health.')
        return self.safe_status()

    def _read_local_json(self,path,limit=16384):
        if not path.is_relative_to(self.root):raise SupervisorError('state_path','Unexpected updater state path.')
        for part in (path,*path.parents):
            if part==self.root:break
            if part.is_symlink() or part.is_junction():raise SupervisorError('state_link','Linked updater state is forbidden.')
        if path.stat().st_size>limit:raise SupervisorError('state_size','Updater state exceeds its bound.')
        return read_json(path.read_bytes())

    def register_local_candidate(self,envelope,archive_path):
        """Development/offline installer entry only, not exposed as an HTTP API.
        A real feed must use UpdateClient's chosen HTTPS policy first.
        """
        if self.verifier is None:raise SupervisorError("publisher_not_selected","No publisher key is configured.",503)
        state=self.updater.status()
        verified=self.verifier.verify(envelope,current_sequence=state["highest_accepted_sequence"],current_schema=state["active"]["schema_version"])
        candidate_id=uuid.uuid4().hex;folder=self.root/"candidates"/candidate_id;folder.mkdir(parents=True)
        (folder/"manifest.json").write_bytes(envelope)
        shutil.copyfile(archive_path,folder/"payload.zip")
        self.verifier.extract(verified,folder/"payload.zip",folder/"checked_app")
        atomic_json(folder/'accepted.json',{'accepted_at':int(time.time()),'release_id':verified['manifest']['release_id'],
            'sequence':verified['manifest']['sequence'],'manifest_sha256':verified['manifest_sha256']})
        self.candidates[candidate_id]={"id":candidate_id,"folder":folder,"verified":verified}
        self.release_versions[verified["manifest"]["release_id"]]=verified["manifest"]["version"]
        self.launch_files[verified['manifest']['release_id']]=verified['manifest']['files']
        return {"candidate_id":candidate_id,"manifest_sha256":verified["manifest_sha256"],"version":verified["manifest"]["version"]}

    def keep_alive(self):
        """Own update workers bridge the intentional old/new app stop gap."""
        with self.lock:
            self._expire_waiting()
            return (bool(self.current and self.current.process.poll() is None)
                    or any(worker.is_alive() for worker in self.workers)
                    or any(op["state"] in ("awaiting_app_response", "applying")
                           for op in self.operations.values()))

    def safe_status(self):
        with self.lock:
            self._expire_waiting()
            configured=self.provider is not None and self.verifier is not None
            return {"enabled":configured,"status":"ready_to_check" if configured else "provider_not_selected","supervisor_connected":True,
                    "manual_apply_only":True,"publisher_key_configured":self.verifier is not None,
                    "feed_selected":self.provider is not None,"external_fetch":self.check_count>0,"automatic_fetch":False,
                    "apply_available":bool(self.candidates),
                    "check_in_progress":self.checking,
                    "active_release_id":self.current.slot['release_id'] if self.current else None,
                    "active_version":self.current.version if self.current else None,
                    "app_port":self.current.port if self.current and self.current.process.poll() is None else None,
                    "live_owned_process_count":sum(p.process.poll() is None for p in self.owned)}

    def check_for_update(self):
        if self.provider is None or self.verifier is None:raise SupervisorError('provider_not_selected','Publisher and public feed are not configured.',503)
        with self.lock:
            if self.checking or any(op['state'] in ('awaiting_app_response','applying') for op in self.operations.values()):
                raise SupervisorError('update_busy','Another update is active.',409)
            state=self.updater.status();self.check_count+=1;self.checking=True
        try:
            if hasattr(self.provider,'begin_check'):self.provider.begin_check()
            discovery=self.provider.discover()
            envelope,verified=self.provider.manifest(discovery,self.verifier,
                current_sequence=state['highest_accepted_sequence'],current_schema=state['active']['schema_version'])
            folder=self.root/'checks'/uuid.uuid4().hex;folder.mkdir(parents=True)
            archive=self.provider.download(verified,folder/'aroma-code.zip')
            candidate=self.register_local_candidate(envelope,archive)
            return {'status':'update_available',**candidate,'release_notes':verified['manifest']['release_notes'],
                'schema_version':verified['manifest']['schema']['target'],'manual_apply_required':True}
        except UpdateError as error:
            if error.code in ('github_no_release','release_replay'):
                return {'status':'no_release' if error.code=='github_no_release' else 'up_to_date','apply_available':False,
                    'message':'No new verified public release is available.'}
            raise SupervisorError(error.code,'Public update verification failed. Current work remains available.',502)
        finally:
            if hasattr(self.provider,'end_check'):self.provider.end_check()
            with self.lock:self.checking=False

    def _expire_waiting(self):
        for op in self.operations.values():
            grant=self.grants.get(op["ticket"])
            if op["state"]=="awaiting_app_response" and (not grant or grant["expires"]<=time.monotonic()):
                op["state"]="expired";op["error_code"]="grant_expired"
                self.grants.pop(op["ticket"],None)

    def _app_request(self, handle, method, path, payload=None, timeout=3):
        conn=http.client.HTTPConnection("127.0.0.1",handle.port,timeout=timeout)
        try:
            conn.request(method,path,None if payload is None else canonical(payload),
                         {"Content-Type":"application/json","X-App-Token":handle.csrf})
            response=conn.getresponse();data=response.read(65537)
            if len(data)>65536:raise ValueError("App response limit")
            if response.status>=400:raise ValueError("Owned app request failed")
            return json.loads(data)
        finally:conn.close()

    def start(self,slot):
        if any(p.process.poll() is None for p in self.owned):
            raise SupervisorError("owned_app_already_running","Another owned app is still running.",409)
        if digest_file(self.python)!=self.runtime_sha:raise SupervisorError("runtime_changed","Stable runtime changed.")
        code=Path(slot["code_dir"]).resolve();entry=code/"server.py"
        files=self.launch_files.get(slot['release_id'])
        if not files:raise SupervisorError('launch_manifest_missing','Verified slot files are unavailable.')
        for row in files:
            path=code.joinpath(*row['path'].split('/'))
            if (path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(code) or
                path.stat().st_size!=row['size'] or digest_file(path)!=row['sha256']):
                raise SupervisorError('slot_changed','Verified app code changed before launch.')
        nonce=secrets.token_hex(16)
        launch={"slot":dict(slot),"server_sha256":digest_file(entry),"app_port":self.app_port,
                "supervisor_port":self.http.server_port,"secret":self.secret,"nonce":nonce,"examples_dir":self.examples_dir,
                "verified_files":files,"remote_updates_enabled":bool(self.verifier and self.provider)}
        if len(canonical(launch))>1024*1024:raise SupervisorError('launch_size','Verified launch data exceeds its bound.')
        flags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0
        # Explicit OS environment only; account/API keys and inherited Codex or
        # Python profile settings are not handed to the app process.
        environment={key:os.environ[key] for key in ('SystemRoot','WINDIR','SystemDrive','TEMP','TMP','PATH','COMSPEC',
            'USERPROFILE','LOCALAPPDATA','APPDATA','PROGRAMFILES','PROGRAMFILES(X86)','NUMBER_OF_PROCESSORS',
            'PROCESSOR_ARCHITECTURE') if key in os.environ}
        process=subprocess.Popen([str(self.python),"-E",str(TRUSTED/"app_runner.py")],cwd=str(TRUSTED),
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=flags,env=environment)
        ready_queue=queue.Queue()
        def read_ready():
            try:
                line=process.stdout.readline(8193)
                if len(line)>8192:raise ValueError("Startup line limit")
                ready_queue.put(json.loads(line))
            except Exception:ready_queue.put(None)
        reader=threading.Thread(target=read_ready,daemon=True);reader.start()
        try:
            process.stdin.write(canonical(launch)+b"\n");process.stdin.flush();process.stdin.close()
            ready=ready_queue.get(timeout=self.startup_timeout)
            if (not isinstance(ready,dict) or ready.get("event")!="ready" or type(ready.get("port")) is not int or
                not isinstance(ready.get("csrf_token"),str) or process.poll() is not None):
                raise SupervisorError("app_start_failed","New app did not report ready.")
            owned=OwnedProcess(process,slot,nonce,self.secret,ready)
            self.owned.append(owned);self.current=owned
            if self.app_port==0:self.app_port=owned.port
            return owned
        except Exception:
            # The handle was created by this supervisor. No PID lookup/guessing.
            if process.poll() is None:process.kill()
            process.wait(timeout=5)
            if process.stdout:process.stdout.close()
            raise SupervisorError("app_start_failed","Owned app startup failed; other processes were untouched.")

    def health(self,handle,slot,allow_recovery=False):
        if handle not in self.owned or handle.process.poll() is not None:return False
        try:
            health=self._app_request(handle,"GET","/api/health")
            expected=self.release_versions.get(slot["release_id"])
            return (health.get("app_id")=="aroma-work-helper" and health.get("instance_root")==slot["code_dir"] and
                    health.get("data_dir")==slot["data_dir"] and health.get("schema_version")==slot["schema_version"] and
                    (health.get("recovery_required") is False or allow_recovery and health.get("recovery_required") is True) and
                    (expected is None or health.get("version")==expected))
        except (OSError,ValueError):return False

    def stop(self,handle):
        if handle not in self.owned:raise SupervisorError("unknown_process","Unknown process handles are never terminated.")
        if handle.process.poll() is None:
            self._app_request(handle,"POST","/api/shutdown",{"confirm":True},timeout=5)
            try:handle.process.wait(timeout=self.stop_timeout)
            except subprocess.TimeoutExpired:
                # Refuse snapshot if graceful joining has not completed. Keep
                # current work untouched rather than force a potentially busy writer.
                raise SupervisorError("app_stop_unconfirmed","Owned app writers did not finish; manual review is required.")
        if handle.process.poll() is None:raise SupervisorError("app_stop_unconfirmed","App is still alive.")
        if handle.process.stdout:handle.process.stdout.close()

    def stop_and_wait(self,slot):
        matches=[p for p in self.owned if p.slot["code_dir"]==slot["code_dir"] and p.slot["data_dir"]==slot["data_dir"] and p.process.poll() is None]
        if len(matches)!=1:raise SupervisorError("unknown_owner","Exact owned old app could not be established.")
        self.stop(matches[0])

    def ensure_slots_stopped(self,old,new):
        for handle in self.owned:
            if handle.process.poll() is None:
                if (handle.slot["code_dir"],handle.slot["data_dir"]) not in ((old["code_dir"],old["data_dir"]),(new["code_dir"],new["data_dir"])):
                    raise SupervisorError("unknown_owner","An unexpected owned app is alive.")
                self.stop(handle)

    def authorize(self,ticket,digest):
        with self.lock:
            grant=self.grants.get(ticket)
            return bool(grant and grant["digest"]==digest and grant["expires"]>time.monotonic() and
                        self.current and grant["nonce"]==self.current.nonce and self.current.process.poll() is None)

    def _offer(self,nonce,payload):
        if payload.get("confirm") is not True or payload.get("pin_checked") is not True:
            raise SupervisorError("pin_confirmation_required","PIN-checked manual apply is required.",403)
        candidate_id=payload.get("candidate_id");digest=payload.get("manifest_sha256");request_id=payload.get("request_id")
        if (not isinstance(candidate_id,str) or not re.fullmatch(r"[a-f0-9]{32}",candidate_id) or
            not isinstance(digest,str) or not re.fullmatch(r"[a-f0-9]{64}",digest) or
            not isinstance(request_id,str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}",request_id)):
            raise SupervisorError("handoff_fields","Invalid candidate identity.")
        candidate=self.candidates.get(candidate_id)
        if not candidate or candidate["verified"]["manifest_sha256"]!=digest:
            raise SupervisorError("candidate_changed","Candidate preview changed.",409)
        key=(nonce,request_id);body_hash=hashlib.sha256(canonical(payload)).hexdigest()
        with self.lock:
            self._expire_waiting()
            if self.checking:raise SupervisorError('update_busy','Public update verification is active.',409)
            previous=self.requests.get(key)
            if previous:
                if previous["hash"]!=body_hash:raise SupervisorError("request_conflict","Request identity was reused.",409)
                return {"operation_id":previous["operation"],"state":self.operations[previous["operation"]]["state"],"duplicate":True}
            if any(op["state"] in ("awaiting_app_response","applying") for op in self.operations.values()):
                raise SupervisorError("update_busy","Another update is active.",409)
            ticket=secrets.token_hex(32);operation_id=uuid.uuid4().hex
            self.grants[ticket]={"digest":digest,"nonce":nonce,"expires":time.monotonic()+self.grant_ttl}
            self.operations[operation_id]={"id":operation_id,"state":"awaiting_app_response","candidate_id":candidate_id,
                "ticket":ticket,"nonce":nonce,"manifest_sha256":digest,"error_code":None}
            self.requests[key]={"hash":body_hash,"operation":operation_id}
            return {"operation_id":operation_id,"state":"awaiting_app_response","duplicate":False}

    def _ack(self,nonce,payload):
        operation_id=payload.get("operation_id")
        if not isinstance(operation_id,str) or not re.fullmatch(r"[a-f0-9]{32}",operation_id):
            raise SupervisorError("operation_missing","Operation not found.",404)
        with self.lock:
            op=self.operations.get(operation_id)
            if not op or op["nonce"]!=nonce:raise SupervisorError("operation_missing","Operation not found.",404)
            if op["state"]!="awaiting_app_response":return {"accepted":True,"duplicate":True},None
            grant=self.grants.get(op["ticket"])
            if not grant or grant["expires"]<=time.monotonic():
                op["state"]="expired";op["error_code"]="grant_expired"
                raise SupervisorError("grant_expired","Apply confirmation expired.",409)
            op["state"]="applying"
            return {"accepted":True,"duplicate":False},lambda:self._apply(operation_id)

    def _apply(self,operation_id):
        op=self.operations[operation_id];candidate=self.candidates[op["candidate_id"]]
        try:
            folder=candidate["folder"]
            result=self.updater.apply((folder/"manifest.json").read_bytes(),folder/"payload.zip",ticket=op["ticket"],confirm=True)
            with self.lock:op["state"]="applied";op["result"]={"release_id":result["active"]["release_id"],"old_data_preserved":True}
        except Exception as exc:
            phase=None
            if self.updater.journal_path.is_file():
                from update_client import read_json
                phase=read_json(self.updater.journal_path.read_bytes()).get("phase")
            with self.lock:
                op["state"]="rolled_back" if phase=="rolled_back" else "failed"
                op["error_code"]=getattr(exc,"code","update_failed")
        finally:
            with self.lock:self.grants.pop(op["ticket"],None)

    def operation_status(self,operation_id):
        with self.lock:
            self._expire_waiting()
            op=self.operations.get(operation_id)
            if not op:raise SupervisorError("operation_missing","Operation not found.",404)
            result={key:op.get(key) for key in ("id","state","candidate_id","manifest_sha256","error_code","result")}
            result["old_app_still_running"]=bool(self.current and self.current.nonce==op["nonce"] and self.current.process.poll() is None)
            return result

    def request_status(self,request_id):
        with self.lock:
            matches=[value['operation'] for (nonce,identifier),value in self.requests.items() if identifier==request_id]
            if not matches:return {'state':'unknown','operation':None}
            if len(set(matches))!=1:raise SupervisorError('request_identity_ambiguous','Request identity needs manual review.',409)
            return {'state':'found','operation':self.operation_status(matches[0])}

    def _handler_type(self):
        supervisor=self
        class Handler(BaseHTTPRequestHandler):
            protocol_version="HTTP/1.1"
            def setup(self):
                super().setup();self.connection.settimeout(5)
            def log_message(self,*args):pass
            def _check(self):
                if self.headers.get("Host","") not in ("127.0.0.1:%d"%supervisor.http.server_port,"localhost:%d"%supervisor.http.server_port):
                    raise SupervisorError("host_forbidden","Local host required.",403)
                origin=self.headers.get("Origin")
                if origin is not None and origin not in ("http://127.0.0.1:%d"%supervisor.http.server_port,"http://localhost:%d"%supervisor.http.server_port):
                    raise SupervisorError("origin_forbidden","Local origin required.",403)
                if self.headers.get("Sec-Fetch-Site") in ("cross-site","same-site"):
                    raise SupervisorError("origin_forbidden","Cross-site handoff is forbidden.",403)
                secret=self.headers.get("X-Supervisor-Secret","");nonce=self.headers.get("X-App-Instance","")
                if not secret.isascii() or len(secret)!=len(supervisor.secret) or not hmac.compare_digest(secret,supervisor.secret):
                    raise SupervisorError("handoff_forbidden","Local handoff required.",403)
                if not supervisor.current or nonce!=supervisor.current.nonce or supervisor.current.process.poll() is not None:
                    raise SupervisorError("instance_changed","App instance changed.",409)
                return nonce
            def _send(self,status,body):
                data=canonical(body);self.send_response(status);self.send_header("Content-Type","application/json")
                self.send_header("Content-Length",str(len(data)));self.send_header("Connection","close")
                self.send_header("Cache-Control","no-store");self.send_header("X-Content-Type-Options","nosniff")
                self.close_connection=True;self.end_headers();self.wfile.write(data)
            def do_GET(self):
                try:
                    self._check();path=urlsplit(self.path).path
                    if path=="/status":return self._send(200,supervisor.safe_status())
                    match=re.fullmatch(r"/operations/([a-f0-9]{32})",path)
                    if match:return self._send(200,supervisor.operation_status(match.group(1)))
                    match=re.fullmatch(r'/requests/([A-Za-z0-9_-]{8,100})',path)
                    if match:return self._send(200,supervisor.request_status(match.group(1)))
                    raise SupervisorError("not_found","Route not found.",404)
                except SupervisorError as exc:self._send(exc.status,{"code":exc.code,"message":exc.message})
            def do_POST(self):
                task=None
                consumed=False
                def drain():
                    if consumed or self.headers.get("Transfer-Encoding"):return
                    try:length=int(self.headers.get("Content-Length","-1"))
                    except ValueError:return
                    if not 0<=length<=16384:return
                    try:
                        self.connection.settimeout(.3)
                        self.rfile.read(length)
                    except OSError:pass
                try:
                    nonce=self._check()
                    if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type","").split(";")[0]!="application/json":
                        raise SupervisorError("json_required","Bounded JSON required.",415)
                    try:length=int(self.headers.get("Content-Length","-1"))
                    except ValueError:length=-1
                    if not 0<=length<=16384:raise SupervisorError("body_size","Handoff size limit.",413)
                    raw=self.rfile.read(length);consumed=True
                    payload=read_json(raw)
                    if not isinstance(payload,dict):raise SupervisorError("body_type","Object required.")
                    path=urlsplit(self.path).path
                    if path=="/handoff/apply":body=supervisor._offer(nonce,payload)
                    elif path=="/handoff/ack":body,task=supervisor._ack(nonce,payload)
                    elif path=="/check":body=supervisor.check_for_update()
                    else:raise SupervisorError("not_found","Route not found.",404)
                    self._send(202,body)
                    if task:
                        thread=threading.Thread(target=task,name="owned-update-worker",daemon=False)
                        supervisor.workers.append(thread);thread.start()
                except SupervisorError as exc:
                    drain()
                    self._send(exc.status,{"code":exc.code,"message":exc.message})
                except (ValueError,OSError):
                    drain();self._send(400,{"code":"handoff_invalid","message":"Handoff request invalid."})
        return Handler

    def close(self):
        for worker in self.workers:worker.join(25)
        if any(w.is_alive() for w in self.workers):raise SupervisorError("worker_active","Update worker is still active.")
        for process in self.owned:
            if process.process.poll() is None:self.stop(process)
        self.http.shutdown();self.http.server_close();self.thread.join(3)
        if not self.owner_handle.closed:
            self.owner_handle.close();self.owner_path.unlink(missing_ok=True)
