"""Resume tokens: a workflow's progress, held by the client.

A workflow tool call that would wait longer than its budget returns a
resume token instead. The token carries everything needed to continue (the
next step, the values the remaining steps use, the polling retries used so
far) so the server keeps no state between calls, and a run can continue
after the server restarts, days later.

A token is readable JSON, base64url encoded, followed by an HMAC-SHA256
signature. Only the server computes the signature; the client passes the
token back unchanged. Signing doesn't grant access (every API call still
uses the caller's own credentials). It stops a token from being edited to
skip a step or change a value partway through. Tokens never hold
credentials.

The key is ``LINODE_MCP_RESUME_KEY`` if set, otherwise a random key created
on first use and kept next to the saved sign-in in the cache directory.
"""

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

from .config import cache_root

logger = logging.getLogger(__name__)

TOKEN_VERSION = 1
DEFAULT_TTL = 7 * 24 * 3600.0
# Wall-clock time tokens expire by; tests replace it
_now = time.time


class ResumeTokenError(ValueError):
    """A resume token that can't be used."""


def _key_path() -> Path:
    return cache_root() / "resume.key"


def signing_key() -> bytes:
    """The key resume tokens are signed with, created on first use."""
    configured = os.getenv("LINODE_MCP_RESUME_KEY")
    if configured:
        return hashlib.sha256(configured.encode()).digest()
    path = _key_path()
    try:
        return bytes.fromhex(path.read_text().strip())
    except FileNotFoundError:
        pass
    except ValueError:
        logger.warning("Replacing unreadable resume key %s", path)
        path.unlink(missing_ok=True)
    key = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another server process created it first
        return bytes.fromhex(path.read_text().strip())
    with os.fdopen(fd, "w") as f:
        f.write(key.hex())
    return key


def token_ttl() -> float:
    """Seconds a resume token stays valid: ``LINODE_MCP_RESUME_TTL``, or a week."""
    try:
        return float(os.getenv("LINODE_MCP_RESUME_TTL", DEFAULT_TTL))
    except ValueError:
        logger.warning("Ignoring invalid LINODE_MCP_RESUME_TTL")
        return DEFAULT_TTL


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def encode(payload: dict[str, Any], key: bytes) -> str:
    """Sign a payload; it expires ``token_ttl()`` seconds from now."""
    body = {"v": TOKEN_VERSION, **payload, "exp": int(_now() + token_ttl())}
    data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(key, data, hashlib.sha256).digest()
    return f"{_b64(data)}.{_b64(signature)}"


def decode(token: str, key: bytes) -> dict[str, Any]:
    """Check a token's signature and expiry and return its payload."""
    try:
        data_part, signature_part = token.strip().split(".")
        data, signature = _unb64(data_part), _unb64(signature_part)
    except (ValueError, binascii.Error) as exc:
        raise ResumeTokenError("is not a resume token from this server") from exc
    expected = hmac.new(key, data, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise ResumeTokenError(
            "was changed, or was issued by another installation of this server"
        )
    payload = json.loads(data)
    if payload.get("v") != TOKEN_VERSION:
        raise ResumeTokenError("is from a different version of this server")
    if payload["exp"] < _now():
        expired = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(payload["exp"]))
        raise ResumeTokenError(f"expired at {expired}")
    return payload
