"""Chosen public GitHub feed. Unsigned discovery never authorizes code.

No account token, cookies, machine proxy, upload or customer fields. Automatic
urllib redirects are disabled; only a bounded public GitHub asset transition
to its fixed storage host is allowed, with signed SHA/length verification.
"""
import hashlib
import os
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, HTTPRedirectHandler, build_opener

from update_client import MAX_MANIFEST_BYTES, MAX_ARCHIVE_BYTES, UpdateError, read_json

REPOSITORY='hdw727-code/healing-aroma-updates'
LATEST='https://api.github.com/repos/'+REPOSITORY+'/releases/latest'
ASSET_BASE='https://github.com/'+REPOSITORY+'/releases/download/'
MANIFEST_NAME='update-manifest.json'
PAYLOAD_NAME='aroma-code.zip'
TAG=re.compile(r'v[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9_.-]+)?\Z')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def failure(code,message,status=400):raise UpdateError(code,message)


class GithubProvider:
    def __init__(self,*,opener=None,timeout=10,total_timeout=40):
        self.opener=opener or build_opener(ProxyHandler({}),NoRedirect())
        self.timeout,self.total_timeout=timeout,total_timeout
        self.local=threading.local()

    def begin_check(self):self.local.deadline=time.monotonic()+self.total_timeout
    def end_check(self):self.local.__dict__.clear()
    def _remaining(self):
        remaining=getattr(self.local,'deadline',time.monotonic()+self.total_timeout)-time.monotonic()
        if remaining<=0:failure('github_timeout','Public update check timed out.')
        return remaining

    def _validate_url(self,url,*,storage=False):
        if not isinstance(url,str) or len(url)>8192 or any(ord(c)<33 or ord(c)>126 for c in url):
            failure('github_url','Invalid public update address.')
        parsed=urlsplit(url)
        try:port=parsed.port
        except ValueError:failure('github_url','Invalid public update address.')
        if parsed.scheme!='https' or port not in (None,443) or parsed.username or parsed.password or parsed.fragment:
            failure('github_url','HTTPS public update address required.')
        if storage:
            if parsed.hostname!='release-assets.githubusercontent.com' or not parsed.path.startswith('/github-production-release-asset/'):
                failure('github_redirect','Public release storage redirect rejected.')
            return
        if url==LATEST:return
        if parsed.hostname!='github.com' or parsed.query or not url.startswith(ASSET_BASE):
            failure('github_url','Chosen public repository required.')
        suffix=url[len(ASSET_BASE):].split('/')
        if len(suffix)!=2 or not TAG.fullmatch(suffix[0]) or suffix[1] not in (MANIFEST_NAME,PAYLOAD_NAME):
            failure('github_url','Immutable tagged asset address required.')

    def _response(self,url):
        self._validate_url(url)
        original=url;current=url
        for attempt in range(3):
            request=Request(current,method='GET',headers={'Accept':'application/vnd.github+json' if current==LATEST else 'application/octet-stream',
                'User-Agent':'HealingAromaLocalUpdater/1.0','X-GitHub-Api-Version':'2026-03-10'})
            try:response=self.opener.open(request,timeout=min(self.timeout,self._remaining()))
            except HTTPError as error:
                if error.code in (301,302,303,307,308):
                    location=error.headers.get('Location');error.close()
                    if original==LATEST or attempt==2:failure('github_redirect','Unexpected public feed redirect.')
                    self._validate_url(location,storage=True);current=location;continue
                code=error.code;error.close()
                failure('github_no_release' if code==404 else 'github_unavailable','No public release is available.' if code==404 else 'Public update check is temporarily unavailable.')
            except (OSError,URLError):failure('github_unavailable','Public update check is temporarily unavailable.')
            if response.getcode()!=200:
                response.close();failure('github_response','Unexpected public feed response.')
            return response
        failure('github_redirect','Too many public release redirects.')

    def fetch_bytes(self,url,limit):
        start=time.monotonic();response=self._response(url)
        try:
            length=response.headers.get('Content-Length')
            if length is not None and (not length.isdecimal() or int(length)>limit):failure('github_size','Public response exceeds its bound.')
            chunks=[];count=0
            while True:
                self._remaining()
                if time.monotonic()-start>self.total_timeout:failure('github_timeout','Public update download timed out.')
                chunk=response.read(min(65536,limit-count+1))
                if not chunk:break
                count+=len(chunk)
                if count>limit:failure('github_size','Public response exceeds its bound.')
                chunks.append(chunk)
            return b''.join(chunks)
        finally:response.close()

    def discover(self):
        document=read_json(self.fetch_bytes(LATEST,1024*1024))
        if not isinstance(document,dict) or document.get('draft') is not False or document.get('prerelease') is not False:
            failure('github_release','Stable public release required.')
        tag=document.get('tag_name');assets=document.get('assets')
        if not isinstance(tag,str) or not TAG.fullmatch(tag) or not isinstance(assets,list) or len(assets)>100:
            failure('github_release','Public release metadata is invalid.')
        selected={}
        for asset in assets:
            if not isinstance(asset,dict):failure('github_release','Public asset metadata is invalid.')
            name=asset.get('name')
            if name not in (MANIFEST_NAME,PAYLOAD_NAME):continue
            if name in selected or asset.get('state')!='uploaded' or type(asset.get('size')) is not int:
                failure('github_asset','Public asset metadata is invalid.')
            url=ASSET_BASE+tag+'/'+name
            if asset.get('browser_download_url')!=url:failure('github_asset','Public asset address is outside the chosen release.')
            limit=MAX_MANIFEST_BYTES if name==MANIFEST_NAME else MAX_ARCHIVE_BYTES
            if not 0<asset['size']<=limit:failure('github_size','Public asset exceeds its bound.')
            selected[name]=url
        if set(selected)!={MANIFEST_NAME,PAYLOAD_NAME}:failure('github_no_release','Signed manifest and code archive are not published yet.')
        return {'tag':tag,'manifest_url':selected[MANIFEST_NAME],'payload_url':selected[PAYLOAD_NAME]}

    def manifest(self,discovery,verifier,*,current_sequence,current_schema):
        expected=ASSET_BASE+discovery['tag']+'/'+MANIFEST_NAME
        if discovery.get('manifest_url')!=expected:failure('github_asset','Manifest address changed.')
        envelope=self.fetch_bytes(expected,MAX_MANIFEST_BYTES)
        verified=verifier.verify(envelope,current_sequence=current_sequence,current_schema=current_schema)
        payload=verified['manifest']['payload']
        if payload['url']!=discovery['payload_url'] or discovery['tag']!='v'+verified['manifest']['version']:
            failure('github_manifest','Signed release does not match the chosen immutable asset.')
        self._validate_url(payload['url'])
        return envelope,verified

    def download(self,verified,destination):
        payload=verified['manifest']['payload'];self._validate_url(payload['url'])
        destination=Path(destination)
        if destination.exists():failure('download_exists','Download destination already exists.')
        start=time.monotonic();response=self._response(payload['url']);owned=False
        try:
            declared=response.headers.get('Content-Length')
            if declared is not None and (not declared.isdecimal() or int(declared)!=payload['size']):failure('github_size','Signed payload length does not match.')
            size=0;digest=hashlib.sha256()
            with destination.open('xb') as output:
                owned=True
                while True:
                    self._remaining()
                    if time.monotonic()-start>self.total_timeout:failure('github_timeout','Public update download timed out.')
                    chunk=response.read(1024*1024)
                    if not chunk:break
                    size+=len(chunk)
                    if size>payload['size']:failure('payload_size','Signed payload length does not match.')
                    digest.update(chunk);output.write(chunk)
                output.flush();os.fsync(output.fileno())
            if size!=payload['size'] or digest.hexdigest()!=payload['sha256']:
                failure('payload_hash','Signed public payload did not match its hash.')
            return destination
        except Exception:
            if owned:destination.unlink(missing_ok=True)
            raise
        finally:response.close()
