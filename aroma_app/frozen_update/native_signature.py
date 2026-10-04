"""Windows CNG signature verification. No installer or third-party dependency.

The trusted publisher key is supplied out of band by the eventual launcher.
Envelope data must never supply or replace that key. P-256 public key uses the
SEC1 uncompressed form (04 || X || Y); signatures use 32-byte r || 32-byte s.
Only this verifier belongs in a client. Test key generation is in tests.
"""
from __future__ import annotations
import ctypes
import hashlib
import os
import struct


class SignatureError(ValueError):
    pass


class CNGVerifier:
    algorithm = "ecdsa-p256-sha256-p1363"

    def __init__(self, public_key: bytes):
        if os.name != "nt":
            raise SignatureError("Windows CNG is required; verification is unavailable.")
        if not isinstance(public_key, bytes) or len(public_key) != 65 or public_key[0] != 4:
            raise SignatureError("A pinned uncompressed P-256 public key is required.")
        # BCRYPT_ECDSA_PUBLIC_P256_MAGIC = 'ECS1'; cbKey = 32.
        self.blob = b"ECS1" + struct.pack("<I", 32) + public_key[1:]
        # System32-only DLL lookup, never a DLL in the downloaded application.
        self.dll = ctypes.WinDLL("bcrypt.dll", winmode=0x00000800)
        P, U, W = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p
        specs = {
            "BCryptOpenAlgorithmProvider": [ctypes.POINTER(P), W, W, U],
            "BCryptImportKeyPair": [P, P, W, ctypes.POINTER(P), P, U, U],
            "BCryptVerifySignature": [P, P, P, U, P, U, U],
            "BCryptDestroyKey": [P],
            "BCryptCloseAlgorithmProvider": [P, U],
        }
        for name, args in specs.items():
            fn = getattr(self.dll, name)
            fn.argtypes, fn.restype = args, ctypes.c_long

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.blob[8:]).hexdigest()

    def verify(self, message: bytes, signature: bytes) -> bool:
        if not isinstance(message, bytes) or not isinstance(signature, bytes) or len(signature) != 64:
            return False
        alg, key = ctypes.c_void_p(), ctypes.c_void_p()
        blob, digest, sig = (ctypes.create_string_buffer(v) for v in
                             (self.blob, hashlib.sha256(message).digest(), signature))
        try:
            if self.dll.BCryptOpenAlgorithmProvider(ctypes.byref(alg), "ECDSA_P256", None, 0) != 0:
                raise SignatureError("CNG ECDSA provider is unavailable.")
            if self.dll.BCryptImportKeyPair(alg, None, "ECCPUBLICBLOB", ctypes.byref(key),
                                          blob, len(self.blob), 0) != 0:
                raise SignatureError("The pinned publisher public key is invalid.")
            status = self.dll.BCryptVerifySignature(key, None, digest, 32, sig, 64, 0)
            if status == 0:
                return True
            if (status & 0xffffffff) == 0xc000a000:  # STATUS_INVALID_SIGNATURE
                return False
            raise SignatureError("Native signature verification failed closed.")
        finally:
            if key.value:
                self.dll.BCryptDestroyKey(key)
            if alg.value:
                self.dll.BCryptCloseAlgorithmProvider(alg, 0)
