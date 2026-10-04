"""Out-of-band public publisher pin. No private-key/account-file access."""
import base64
import hashlib
from pathlib import Path
from native_signature import CNGVerifier
from update_client import read_json

TRUSTED=Path(__file__).resolve().parent
FINGERPRINT='d46122579c50f43525528d2d69902b73be6c3aa8b0fa4d0a3f59ef1d98a27486'


def load_publisher_pin(path=None,*,expected_fingerprint=FINGERPRINT):
    """Only the fixed initial installer supplies this public file. Expected
    fingerprint is compiled into trusted code; remote input never selects it.
    Optional path/fingerprint arguments are for synthetic offline tests only.
    """
    path=TRUSTED/'TRUSTED_PUBLISHER.json' if path is None else Path(path)
    if not path.exists():return None
    if path.is_symlink() or path.is_junction() or path.parent.is_symlink() or path.parent.is_junction() or path.stat().st_size>8192 or path.stat().st_nlink!=1:
        raise ValueError('Unsafe public publisher pin')
    document=read_json(path.read_bytes())
    if (not isinstance(document,dict) or set(document)!={'algorithm','public_key_format','public_key_b64','fingerprint_sha256'} or
        document['algorithm']!='ecdsa-p256-sha256-p1363' or document['public_key_format']!='sec1-uncompressed-p256' or
        document['fingerprint_sha256']!=expected_fingerprint or not isinstance(document['public_key_b64'],str)):
        raise ValueError('Unexpected public publisher pin')
    key=base64.b64decode(document['public_key_b64'],validate=True)
    if len(key)!=65 or key[0]!=4 or hashlib.sha256(key[1:]).hexdigest()!=expected_fingerprint:
        raise ValueError('Public publisher fingerprint mismatch')
    CNGVerifier(key)
    return key
