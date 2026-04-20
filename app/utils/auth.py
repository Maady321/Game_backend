"""
JWT authentication utilities.

Design decisions:
- Using python-jose over PyJWT because it supports JWK, JWE, and more
  algorithms out of the box — future-proof for key rotation.
- Tokens carry minimal claims: sub (player_id) and exp. No sensitive data
  in the payload — the token is a key, not a data store.
- Symmetric HS256 for single-issuer setups. Switch to RS256/ES256 if you
  need third-party verification without sharing the secret.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt

from app.config import get_settings
from app.utils.logging import get_logger

logger = get_logger(__name__)


class AuthError(Exception):
    """Raised when authentication fails."""

    def __init__(self, detail: str = "Authentication failed"):
        self.detail = detail
        super().__init__(detail)


def create_access_token(player_id: str, extra_claims: dict[str, Any] | None = None) -> str:
    """Create a signed JWT for the given player.

    Args:
        player_id: Unique player identifier (becomes the `sub` claim).
        extra_claims: Optional additional claims to embed.

    Returns:
        Encoded JWT string.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": player_id,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expiry_minutes),
        **(extra_claims or {}),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    """Decode and validate a JWT.

    Raises:
        AuthError: If the token is expired, malformed, or has an invalid signature.

    Returns:
        Decoded payload dictionary.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        if "sub" not in payload:
            raise AuthError("Token missing 'sub' claim")
        return payload
    except JWTError as exc:
        logger.warning("jwt_decode_failed", error=str(exc))
        raise AuthError(f"Invalid token: {exc}") from exc


def extract_player_id(token: str) -> str:
    """Convenience: extract player_id from a valid token.

    Raises AuthError on any failure.
    """
    payload = decode_token(token)
    return payload["sub"]
