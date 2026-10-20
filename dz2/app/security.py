import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt
from pwdlib import PasswordHash

from .db import transaction
from .errors import fail

passwords = PasswordHash.recommended()
DUMMY_HASH = passwords.hash("invalid-user-password")


def secret():
    value = os.environ["JWT_SECRET"]
    if len(value) < 32:
        raise RuntimeError("JWT_SECRET must contain at least 32 characters")
    return value


def token_pair(user):
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(user["id"]),
        "role": user["role"],
        "iat": now,
        "iss": "marketplace",
        "aud": "marketplace-api",
    }

    def token(kind, lifetime):
        return jwt.encode(
            dict(claims, type=kind, exp=now + lifetime, jti=str(uuid4())),
            secret(),
            algorithm="HS256",
        )

    return {
        "access_token": token("access", timedelta(minutes=20)),
        "refresh_token": token("refresh", timedelta(days=14)),
        "token_type": "bearer",
        "expires_in": 1200,
    }


def decode_token(token, kind):
    code = "REFRESH_TOKEN_INVALID" if kind == "refresh" else "TOKEN_INVALID"
    try:
        data = jwt.decode(
            token,
            secret(),
            algorithms=["HS256"],
            audience="marketplace-api",
            issuer="marketplace",
            options={"require": ["sub", "role", "type", "exp", "iat", "jti"]},
        )
        if data["type"] != kind or data["role"] not in ("USER", "SELLER", "ADMIN"):
            raise jwt.InvalidTokenError()
        UUID(data["sub"])
        return data
    except jwt.ExpiredSignatureError:
        fail(401, "TOKEN_EXPIRED" if kind == "access" else code, "Token expired")
    except (jwt.InvalidTokenError, ValueError, TypeError):
        fail(401, code, "Invalid token")


def authenticate(request):
    auth = request.headers.get("authorization", "").split()
    if len(auth) != 2 or auth[0].lower() != "bearer":
        fail(401, "TOKEN_INVALID", "Bearer access token required")
    data = decode_token(auth[1], "access")
    with transaction() as conn:
        user = conn.execute(
            "SELECT id, username, role FROM users WHERE id=%s", (data["sub"],)
        ).fetchone()
    if not user or user["role"] != data["role"]:
        fail(401, "TOKEN_INVALID", "User or role is no longer valid")
    return user


def require(request, *roles):
    user = request.state.user
    if user["role"] not in roles:
        fail(403, "ACCESS_DENIED", "Role is not allowed to perform this operation")
    return user


def bootstrap_admin():
    secret()
    username, password = os.getenv("ADMIN_USERNAME"), os.getenv("ADMIN_PASSWORD")
    if username and password:
        if len(password) < 8:
            raise RuntimeError("ADMIN_PASSWORD must contain at least 8 characters")
        with transaction(write=True) as conn:
            conn.execute(
                "INSERT INTO users(id,username,password_hash,role) VALUES (%s,%s,%s,'ADMIN') ON CONFLICT(username) DO NOTHING",
                (uuid4(), username, passwords.hash(password)),
            )
