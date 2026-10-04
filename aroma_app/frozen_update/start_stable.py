"""Hidden stable supervisor entry with a fixed public publisher pin and feed."""
import argparse
import json
from pathlib import Path
import sys
import time

STABLE=Path(__file__).resolve().parent
sys.path.insert(0,str(STABLE))
from supervisor import StableSupervisor, SupervisorError
from publisher_pin import load_publisher_pin
from github_provider import GithubProvider

CODE_PATHS={'server.py','app_start.py','local_import.py','ai_jobs.py','codex_bridge.py','quality.py',
    'native_exports.py','ai_native_exports.py','whole_backup.py','backup_api.py','maintenance_gate.py',
    'update_client.py','native_signature.py','runtime_setup.py','runtime_login.py','image_adapter.py','png_exports.py',
    'public/update_ui.js','public/image_ui.js','public/app.js','public/index.html','public/styles.css','public/print.js',
    'public/native_review.js','public/promo_png.js','public/full_backup_ui.js'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-root',required=True)
    parser.add_argument('--code-dir',required=True)
    parser.add_argument('--data-dir',required=True)
    parser.add_argument('--runtime-sha256',required=True)
    parser.add_argument('--content-dir')
    parser.add_argument('--private-root')
    parser.add_argument('--examples-dir')
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    install=Path(args.install_root).resolve()
    public_key=load_publisher_pin()
    supervisor=StableSupervisor(install/'update_state',python_exe=sys.executable,
        runtime_sha256=args.runtime_sha256,installation_root=install,app_port=args.port,
        examples_dir=args.examples_dir,code_paths=CODE_PATHS,pinned_public_key=public_key,
        provider=GithubProvider() if public_key else None)
    try:
        # The public key is installed out of band. HTTP manifests cannot change
        # the publisher pin or the fixed provider repository.
        if (supervisor.root/'initial_slot.json').exists():supervisor.resume()
        else:supervisor.initialize(args.code_dir,args.data_dir,content_dir=args.content_dir,private_root=args.private_root)
        print(json.dumps({'event':'supervisor_ready','app_port':supervisor.current.port,
                          'remote_updates_enabled':public_key is not None}),flush=True)
        while supervisor.keep_alive():
            time.sleep(.1)
        return 0
    finally:supervisor.close()


if __name__=='__main__':
    try:raise SystemExit(main())
    except SupervisorError as error:
        # Fixed code only, no startup payload, PIN, auth/profile or capability.
        print(json.dumps({'event':'supervisor_failed','code':error.code}),flush=True)
        raise SystemExit(2)
