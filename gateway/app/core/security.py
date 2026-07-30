from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from .config import get_settings

_passwords = PasswordHasher()


def hash_password(password: str) -> str:
    return _passwords.hash(password)


def verify_password(password: str, digest: str) -> bool:
    try:
        return _passwords.verify(digest, password)
    except VerifyMismatchError:
        return False


def create_token(user_id: str) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    return jwt.encode({"sub": user_id, "iss": settings.jwt_issuer, "aud": settings.jwt_audience, "iat": now, "exp": now + timedelta(minutes=60), "jti": str(uuid4())}, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> str:
    settings = get_settings()
    return str(jwt.decode(token, settings.jwt_secret, algorithms=["HS256"], issuer=settings.jwt_issuer, audience=settings.jwt_audience)["sub"])
