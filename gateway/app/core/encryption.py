import base64
import os
from hashlib import sha256

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import get_settings


def _key() -> bytes:
    value = get_settings().encryption_key
    return base64.urlsafe_b64decode(value) if value else sha256(b"unsafe-development-key").digest()


def encrypt(value: str, aad: bytes) -> tuple[bytes, bytes]:
    nonce = os.urandom(12)
    return AESGCM(_key()).encrypt(nonce, value.encode(), aad), nonce


def decrypt(ciphertext: bytes, nonce: bytes, aad: bytes) -> str:
    return AESGCM(_key()).decrypt(nonce, ciphertext, aad).decode()
