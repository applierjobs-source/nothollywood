"""Supabase JWT verification for Not Hollywood.

Uses JWKS from the Supabase project's public discovery endpoint to verify
ES256-signed JWTs issued by Supabase Auth. No shared secret needed.

The JWKS is cached in-process (5 minutes) so we don't hit Supabase on every
request. If Supabase rotates keys mid-flight, verification will fail once and
the next call refreshes.

Environment:
    SUPABASE_URL   e.g. https://xxx.supabase.co
"""
from __future__ import annotations

import os
import time
from functools import lru_cache
from typing import Optional

import requests
from fastapi import HTTPException, Request
from jose import jwt, JWTError
from jose.backends import ECKey
from jose.utils import base64url_decode
import json

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_JWT_ISSUER = f"{SUPABASE_URL}/auth/v1" if SUPABASE_URL else ""

_JWKS_CACHE: dict = {"fetched_at": 0, "keys": {}}
_JWKS_TTL = 300  # 5 minutes


def _fetch_jwks() -> dict:
    """Return {kid: jwk_dict}. Cached for 5 minutes."""
    now = time.time()
    if _JWKS_CACHE["keys"] and (now - _JWKS_CACHE["fetched_at"]) < _JWKS_TTL:
        return _JWKS_CACHE["keys"]
    if not SUPABASE_URL:
        raise RuntimeError("SUPABASE_URL is not set")
    url = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    keys = {}
    for jwk in data.get("keys", []):
        kid = jwk.get("kid")
        if kid:
            keys[kid] = jwk
    _JWKS_CACHE["keys"] = keys
    _JWKS_CACHE["fetched_at"] = now
    return keys


def verify_jwt(token: str) -> dict:
    """Verify a Supabase-issued JWT and return its claims.

    Raises HTTPException(401) on any validation failure.
    """
    if not SUPABASE_URL:
        # Auth is not configured on this deploy — fail closed so we don't accept
        # unsigned tokens as authenticated users.
        raise HTTPException(500, "auth backend not configured on server")
    try:
        # Peek at the header to find kid
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
        if not kid:
            raise HTTPException(401, "token missing kid header")

        keys = _fetch_jwks()
        jwk = keys.get(kid)
        if not jwk:
            # Try a forced refresh in case Supabase rotated
            _JWKS_CACHE["fetched_at"] = 0
            keys = _fetch_jwks()
            jwk = keys.get(kid)
        if not jwk:
            raise HTTPException(401, "unknown signing key")

        claims = jwt.decode(
            token,
            jwk,
            algorithms=[jwk.get("alg", "ES256")],
            audience="authenticated",
            issuer=SUPABASE_JWT_ISSUER,
            options={"verify_at_hash": False},
        )
        return claims
    except JWTError as e:
        raise HTTPException(401, f"invalid token: {e}") from e


def get_user(request: Request) -> Optional[dict]:
    """Extract and verify the user from the Authorization header, if any.

    Returns the JWT claims dict when a valid token is present, or None when no
    Authorization header is set. Raises 401 for malformed / invalid tokens.
    """
    auth = request.headers.get("Authorization", "")
    if not auth:
        return None
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Authorization must be Bearer token")
    token = auth[7:].strip()
    if not token:
        return None
    return verify_jwt(token)


def require_user(request: Request) -> dict:
    """Return claims dict for the authenticated user or raise 401."""
    claims = get_user(request)
    if not claims or not claims.get("sub"):
        raise HTTPException(401, "authentication required")
    return claims
