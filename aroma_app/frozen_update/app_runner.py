"""Stable child entry. Explicit verified slot path, never PYTHONPATH."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

TRUSTED = Path(__file__).resolve().parent
sys.path.insert(0,str(TRUSTED))
# Freeze these names before the app slot is on sys.path.
import native_signature
import update_client
from app_adapter import HandoffClient, attach


def main():
    sys.stdout.reconfigure(encoding="utf8")
    raw=sys.stdin.buffer.readline(1024*1024+1)
    if len(raw)>1024*1024: raise ValueError("Startup handoff is too large")
    launch=json.loads(raw)
    code=Path(launch["slot"]["code_dir"]).resolve()
    entry=code/"server.py"
    expected=launch["server_sha256"]
    for row in launch['verified_files']:
        path=code.joinpath(*row['path'].split('/'))
        if (path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(code) or
            path.stat().st_size!=row['size'] or hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']):
            raise ValueError('Verified slot file changed')
    if not entry.is_file() or hashlib.sha256(entry.read_bytes()).hexdigest()!=expected:
        raise ValueError("Verified server entry changed")
    sys.path.insert(0,str(code))
    spec=importlib.util.spec_from_file_location("verified_app_server",entry)
    app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)
    slot=launch["slot"]
    store=app.AppStore(code,data_dir=Path(slot["data_dir"]),
        content_dir=Path(slot["local_content_dir"]) if slot.get("local_content_dir") else None,
        private_root=Path(slot["local_config_dir"]) if slot.get("local_config_dir") else None,
        runtime_root=TRUSTED.parent)
    # Generic locally owned template files can remain in the stable install;
    # no USB source or private business template is read/copied by the updater.
    if launch.get("examples_dir"):
        import native_exports
        native_exports.EXAMPLES=Path(launch["examples_dir"])
    client=HandoffClient(launch["supervisor_port"],launch["secret"],launch["nonce"])
    store.remote_update_enabled=launch.get('remote_updates_enabled') is True
    handler=attach(app,store,client)
    server=app.ThreadingHTTPServer(("127.0.0.1",launch["app_port"]),handler)
    server.store,server.verbose,server.daemon_threads=store,False,False
    print(json.dumps({"event":"ready","port":server.server_port,"csrf_token":store.csrf_token,"version":app.APP_VERSION}),flush=True)
    try: server.serve_forever()
    finally:
        server.server_close()
        manager=getattr(store,"_ai_jobs_manager",None)
        if manager: manager.close()
        login=getattr(store,'_runtime_login_controller',None)
        if login: login.close()


if __name__=="__main__": main()
