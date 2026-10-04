"""Reviewable START_APP replacement hook; does not change the existing launcher."""
import argparse
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import webbrowser


def compatibility_preflight():
    """Only the initial installed metadata guard may run before app startup."""
    guard=Path(__file__).resolve().parent/'runtime_setup.py'
    try:
        if (not guard.is_file() or guard.is_symlink() or guard.is_junction() or
            guard.stat().st_nlink!=1 or guard.stat().st_size>256*1024):
            raise ValueError('Initial compatibility guard unavailable')
        spec=importlib.util.spec_from_file_location('trusted_initial_runtime_setup',guard)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        details=module.compatibility_guard()
        if not isinstance(details,dict) or type(details.get('runtime_compatible')) is not bool:
            raise ValueError('Compatibility metadata unavailable')
        return details
    except Exception:
        return {'runtime_compatible':False,'state':'compatibility_guard_unavailable'}


def health(port):
    connection=http.client.HTTPConnection('127.0.0.1',port,timeout=.8)
    try:
        connection.request('GET','/api/health');response=connection.getresponse();raw=response.read(65537)
        return json.loads(raw) if response.status==200 and len(raw)<=65536 else None
    except (OSError,ValueError):return None
    finally:connection.close()


def main():
    sys.stdout.reconfigure(encoding='utf8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-root',default=str(Path(__file__).resolve().parent))
    parser.add_argument('--code-dir')
    parser.add_argument('--data-dir')
    parser.add_argument('--runtime-sha256')
    parser.add_argument('--content-dir')
    parser.add_argument('--private-root')
    parser.add_argument('--examples-dir')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    install=Path(args.install_root).resolve()
    args.code_dir=args.code_dir or str(install)
    args.data_dir=args.data_dir or str(install/'user_data')
    args.content_dir=args.content_dir or str(install/'content')
    args.private_root=args.private_root or str(install/'local_private')
    args.examples_dir=args.examples_dir or str(install/'examples')
    if args.runtime_sha256 is None:
        pin=install/'frozen_update/TRUSTED_RUNTIME.json'
        if not pin.is_file() or pin.is_symlink() or pin.is_junction() or pin.stat().st_size>8192 or pin.stat().st_nlink!=1:
            print(json.dumps({'status':'runtime_pin_missing'}));return 4
        document=json.loads(pin.read_bytes())
        if (not isinstance(document,dict) or set(document)!={'schema_version','relative_executable','sha256'} or
            type(document['schema_version']) is not int or document['schema_version']!=1 or
            document['relative_executable']!='runtime/python/python.exe' or not isinstance(document['sha256'],str) or
            not re.fullmatch('[a-f0-9]{64}',document['sha256'])):
            print(json.dumps({'status':'runtime_pin_invalid'}));return 4
        args.runtime_sha256=document['sha256']
    compatibility=compatibility_preflight()
    if compatibility['runtime_compatible'] is not True:
        print(json.dumps({'status':'runtime_incompatible','check':'runtime_incompatible',
            'message':'Windows 10 1809 이상 또는 Windows 11 Intel/AMD x64와 포함된 Python이 필요합니다. 호환성 검사를 통과하지 못해 앱을 시작하지 않았습니다.',
            'codex_runtime_compatibility':compatibility,'actual_mother_device_tested':False},ensure_ascii=False));return 4
    if args.check:
        runtime=Path(sys.executable).resolve()
        okay=hashlib.sha256(runtime.read_bytes()).hexdigest()==args.runtime_sha256
        print(json.dumps({'check':'ok' if okay else 'runtime_mismatch','runtime_type':'existing_official',
                          'python':sys.version.split()[0],'codex_runtime_compatibility':compatibility,
                          'actual_mother_device_tested':False},ensure_ascii=False));return 0 if okay else 4
    current=health(args.port)
    if current:
        app_root=Path(current.get('instance_root','')).resolve()
        if current.get('app_id')=='aroma-work-helper' and (app_root==Path(args.code_dir).resolve() or app_root.is_relative_to(install/'versions')):
            if not args.no_browser:webbrowser.open('http://127.0.0.1:%d'%args.port)
            print(json.dumps({'status':'already_running','new_process':False}));return 0
        print(json.dumps({'status':'port_conflict','other_process_terminated':False}));return 5
    runtime=Path(sys.executable).resolve()
    if hashlib.sha256(runtime.read_bytes()).hexdigest()!=args.runtime_sha256:
        print(json.dumps({'status':'runtime_mismatch'}));return 4
    stable=Path(__file__).resolve().parent/'frozen_update'/'start_stable.py'
    command=[str(runtime),'-E',str(stable),'--install-root',str(install),'--code-dir',args.code_dir,
        '--data-dir',args.data_dir,'--runtime-sha256',args.runtime_sha256,'--port',str(args.port)]
    for flag,value in (('--content-dir',args.content_dir),('--private-root',args.private_root),('--examples-dir',args.examples_dir)):
        if value:command.extend([flag,value])
    environment={key:os.environ[key] for key in ('SystemRoot','WINDIR','SystemDrive','TEMP','TMP','PATH','COMSPEC',
        'USERPROFILE','LOCALAPPDATA','APPDATA','PROGRAMFILES','PROGRAMFILES(X86)','NUMBER_OF_PROCESSORS',
        'PROCESSOR_ARCHITECTURE') if key in os.environ}
    child=subprocess.Popen(command,cwd=str(stable.parent),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,env=environment)
    deadline=time.monotonic()+12
    while time.monotonic()<deadline:
        if child.poll() is not None:
            print(json.dumps({'status':'start_failed','owned_supervisor_exit':child.returncode}));return 6
        current=health(args.port)
        if current and current.get('app_id')=='aroma-work-helper':
            app_root=Path(current.get('instance_root','')).resolve()
            if app_root==Path(args.code_dir).resolve() or app_root.is_relative_to(install/'versions'):
                if not args.no_browser:webbrowser.open('http://127.0.0.1:%d'%args.port)
                print(json.dumps({'status':'started'}));return 0
        time.sleep(.1)
    # Never kill an uncertain app owner. The supervisor has its own bounded
    # startup cleanup; leave any still-active owner for manual review.
    print(json.dumps({'status':'startup_not_confirmed','other_process_terminated':False}));return 7


if __name__=='__main__':raise SystemExit(main())
