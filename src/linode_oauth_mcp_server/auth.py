"""Linode API authentication.

Linode's API accepts OAuth access tokens and personal access tokens as bearer
tokens. The server deliberately treats the credential as opaque: callers can
provide either kind via ``LINODE_API_TOKEN``.
"""

import asyncio
import configparser
import json
import logging
import os
import secrets
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional

from .config import cache_root
from .oauth_scopes import merge_scopes, parse_scopes, scopes_satisfy, serialize_scopes

logger = logging.getLogger(__name__)
SIGN_IN_TIMEOUT_SECONDS = 300


def _linode_cli_token() -> Optional[str]:
    """Load a token from the official Linode CLI environment/config file."""
    token = os.getenv("LINODE_CLI_TOKEN")
    if token:
        return token
    config_path = Path(
        os.getenv("LINODE_CLI_CONFIG", str(Path.home() / ".config" / "linode-cli"))
    )
    if not config_path.is_file():
        return None
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(config_path)
        token = parser.defaults().get("token")
        if not token:
            for section in parser.sections():
                token = parser[section].get("token")
                if token:
                    break
        return token
    except (OSError, configparser.Error) as exc:
        logger.debug(
            "Unable to read Linode CLI credentials from %s: %s", config_path, exc
        )
        return None


async def _show_url_in_client(message: str, url: str, elicitation_id: str):
    """Ask the MCP client to present ``url`` through URL-mode elicitation.

    Returns the client's session when the user accepted, ``None`` when there is
    no active MCP request or the client does not support URL elicitation.
    Raises ``PermissionError`` when the user declines.
    """
    try:
        from fastmcp.server.dependencies import get_context

        ctx = get_context()
        session = ctx.session
        request_context = ctx.request_context
    except Exception:
        return None
    if not client_supports_url_elicitation(session):
        logger.info(
            "MCP client did not declare URL elicitation (capabilities: %r); "
            "opening the system browser",
            getattr(session, "client_capabilities", None),
        )
        return None
    try:
        result = await session.elicit_url(
            message=message,
            url=url,
            elicitation_id=elicitation_id,
            related_request_id=request_context.request_id if request_context else None,
        )
    except Exception as exc:
        logger.info(
            "URL elicitation failed, falling back to the system browser: %s", exc
        )
        return None
    if result.action != "accept":
        raise PermissionError(f"Linode sign-in was {result.action}d in the MCP client")
    return session


def _token_path() -> Path:
    return cache_root() / "oauth-token.json"


def client_supports_url_elicitation(session: Any) -> bool:
    capabilities = getattr(session, "client_capabilities", None)
    elicitation = capabilities.elicitation if capabilities else None
    return elicitation is not None and elicitation.url is not None


@dataclass
class SignIn:
    """A Linode browser sign-in waiting for its loopback callback."""

    scopes: FrozenSet[str]
    url: str
    state: str
    done: asyncio.Future
    # True once the MCP client has shown ``url``, so nothing opens it again.
    presented: bool = False
    task: Optional[asyncio.Task] = field(default=None, repr=False)

    @property
    def message(self) -> str:
        return f"Sign in to Linode to grant these API scopes: {', '.join(sorted(self.scopes))}"


async def _notify_complete(session: Any, elicitation_id: str) -> None:
    try:
        await session.send_elicit_complete(elicitation_id)
    except Exception:
        logger.debug("Could not notify client of elicitation completion", exc_info=True)


class AuthManager:
    """Supply a Linode bearer token, signing in through OAuth when needed.

    Tokens from the environment or linode-cli are opaque and sent as-is. OAuth
    tokens track their granted scopes; a call that needs more scopes starts a
    new browser sign-in requesting the union of granted and required scopes.
    """

    def __init__(self, config=None):
        self.token = (
            os.getenv("LINODE_API_TOKEN")
            or os.getenv("LINODE_OAUTH_TOKEN")
            or _linode_cli_token()
        )
        oauth_config = getattr(config, "oauth", {}) or {}
        self.client_id = os.getenv("LINODE_OAUTH_CLIENT_ID") or oauth_config.get(
            "client_id"
        )
        self.scopes = parse_scopes(
            os.getenv("LINODE_OAUTH_SCOPES")
            or oauth_config.get("scopes", "linodes:read_only stackscripts:read_only")
        )
        self.granted_scopes: FrozenSet[str] = frozenset()
        self._static_token = bool(self.token)
        self.redirect_uri = os.getenv(
            "LINODE_OAUTH_REDIRECT_URI",
            oauth_config.get("redirect_uri", "http://localhost:8765/oauth/callback"),
        )
        self._expires_at = float("inf") if self.token else 0
        self._oauth_lock = asyncio.Lock()
        self._sign_in: Optional[SignIn] = None
        if not self.token:
            self._load_saved_token()

    def has_token(self) -> bool:
        return bool(self.token) and time.time() < self._expires_at - 30

    def _satisfies(self, required: FrozenSet[str]) -> bool:
        return self.has_token() and (
            self._static_token or scopes_satisfy(self.granted_scopes, required)
        )

    def needs_sign_in(self, required: FrozenSet[str]) -> bool:
        """Whether a call needing ``required`` must first run OAuth sign-in."""
        return bool(self.client_id) and not self._satisfies(required)

    async def get_auth_header(
        self, required_scopes: FrozenSet[str] = frozenset()
    ) -> Dict[str, str]:
        """Return a bearer header whose token covers ``required_scopes``."""
        if self._satisfies(required_scopes):
            return {"Authorization": f"Bearer {self.token}"}
        if not self.client_id:
            raise ValueError(
                "Linode credentials are missing. Set LINODE_API_TOKEN, configure "
                "linode-cli, or set LINODE_OAUTH_CLIENT_ID to enable browser sign-in."
            )
        sign_in = await self.begin_sign_in(required_scopes)
        if not sign_in.presented:
            sign_in.presented = True
            session = await _show_url_in_client(
                sign_in.message, sign_in.url, sign_in.state
            )
            if session is None:
                opened = await asyncio.to_thread(webbrowser.open, sign_in.url)
                if not opened:
                    logger.info("Open this Linode authorization URL: %s", sign_in.url)
            else:
                sign_in.done.add_done_callback(
                    lambda _: asyncio.ensure_future(
                        _notify_complete(session, sign_in.state)
                    )
                )
        await self.wait_for_sign_in(sign_in)
        return {"Authorization": f"Bearer {self.token}"}

    async def begin_sign_in(self, required: FrozenSet[str]) -> SignIn:
        """Start (or reuse) a sign-in covering granted plus ``required`` scopes.

        Earlier grants are kept so stepping up never drops access.
        """
        scopes = merge_scopes(self.granted_scopes or self.scopes, required)
        async with self._oauth_lock:
            current = self._sign_in
            if current is not None and not current.done.done():
                if scopes_satisfy(current.scopes, scopes):
                    return current
                if current.task is not None:
                    current.task.cancel()
                    await asyncio.gather(current.task, return_exceptions=True)
            self._sign_in = await self._start_sign_in(scopes)
            return self._sign_in

    async def wait_for_sign_in(self, sign_in: SignIn) -> None:
        try:
            await asyncio.shield(sign_in.done)
        except asyncio.CancelledError:
            if sign_in.done.cancelled():
                raise PermissionError(
                    "Linode sign-in was replaced by a newer request"
                ) from None
            raise

    async def _start_sign_in(self, scopes: FrozenSet[str]) -> SignIn:
        """Listen for the OAuth callback and return the authorization URL."""
        parsed = urllib.parse.urlparse(self.redirect_uri)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError(
                "OAuth redirect URI must use http://127.0.0.1 or localhost"
            )
        port = parsed.port or 80
        expected_path = parsed.path or "/"
        state = secrets.token_urlsafe(24)
        result: asyncio.Future[dict] = asyncio.get_running_loop().create_future()

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
                request_line = head.split(b"\r\n", 1)[0].decode("ascii", "replace")
                method, path, _ = request_line.split(" ", 2)
                if method == "GET" and path.split("?", 1)[0] == expected_path:
                    body = (
                        "<!doctype html><meta charset=utf-8><title>Linode sign-in</title>"
                        "<p>Completing Linode sign-in…</p><script>"
                        "const q=new URLSearchParams(location.hash.slice(1));"
                        "fetch('/oauth/token',{method:'POST',headers:{'Content-Type':'application/json'},"
                        "body:JSON.stringify({access_token:q.get('access_token'),"
                        "expires_in:q.get('expires_in'),scopes:q.get('scopes'),state:q.get('state')})})"
                        ".then(r=>document.body.innerText=r.ok?'Sign-in complete. Return to your MCP client.':'Sign-in failed. Return to your MCP client.')"
                        ".catch(()=>document.body.innerText='Sign-in failed. Return to your MCP client.');"
                        "</script>"
                    ).encode()
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                        + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                        + body
                    )
                elif method == "POST" and path == "/oauth/token":
                    headers = head.decode("latin1").split("\r\n")
                    length = next(
                        (
                            int(line.split(":", 1)[1])
                            for line in headers
                            if line.lower().startswith("content-length:")
                        ),
                        0,
                    )
                    payload = json.loads(await reader.readexactly(length))
                    if payload.get("state") != state or not payload.get("access_token"):
                        raise ValueError("OAuth state mismatch or access token missing")
                    if not result.done():
                        result.set_result(payload)
                    body = b"ok"
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                        + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                        + body
                    )
                else:
                    body = b"not found"
                    writer.write(
                        b"HTTP/1.1 404 Not Found\r\nContent-Length: 9\r\nConnection: close\r\n\r\nnot found"
                    )
            except Exception as exc:
                if not result.done():
                    result.set_exception(exc)
            finally:
                try:
                    await writer.drain()
                finally:
                    writer.close()

        server = await asyncio.start_server(handle, parsed.hostname, port)
        query = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "response_type": "token",
                "scopes": serialize_scopes(scopes),
                "redirect_uri": self.redirect_uri,
                "state": state,
            }
        )
        sign_in = SignIn(
            scopes=scopes,
            url=f"https://login.linode.com/oauth/authorize?{query}",
            state=state,
            done=asyncio.get_running_loop().create_future(),
        )
        # Nobody may await a sign-in that is abandoned; don't warn about it.
        sign_in.done.add_done_callback(lambda f: f.cancelled() or f.exception())
        logger.info(
            "Waiting for Linode OAuth authorization for scopes: %s",
            serialize_scopes(scopes),
        )

        async def complete() -> None:
            try:
                payload = await asyncio.wait_for(
                    result, timeout=SIGN_IN_TIMEOUT_SECONDS
                )
                self.token = payload["access_token"]
                self._expires_at = time.time() + int(payload.get("expires_in") or 7200)
                # Linode's token redirect may omit scopes; assume the request was granted.
                self.granted_scopes = parse_scopes(payload.get("scopes")) or scopes
                self._static_token = False
                self._save_token()
                sign_in.done.set_result(None)
            except asyncio.CancelledError:
                sign_in.done.cancel()
                raise
            except asyncio.TimeoutError:
                sign_in.done.set_exception(
                    TimeoutError(
                        f"Linode sign-in for scopes {serialize_scopes(scopes)} was not "
                        f"completed within 5 minutes. Authorization URL: {sign_in.url}"
                    )
                )
            except Exception as exc:
                sign_in.done.set_exception(exc)
            finally:
                server.close()
                await server.wait_closed()

        sign_in.task = asyncio.create_task(complete())
        return sign_in

    def clear_token_cache(self) -> None:
        """Clear an OAuth token so the next call starts a fresh sign-in."""
        self.token = None
        self._expires_at = 0
        self.granted_scopes = frozenset()
        self._static_token = False
        _token_path().unlink(missing_ok=True)

    def _load_saved_token(self) -> None:
        """Resume an unexpired OAuth token saved by an earlier server process."""
        path = _token_path()
        try:
            saved = json.loads(path.read_text())
            expires_at = float(saved["expires_at"])
            token = saved["access_token"]
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Ignoring unreadable saved Linode token %s: %s", path, exc)
            return
        if saved.get("client_id") != self.client_id or time.time() >= expires_at - 30:
            path.unlink(missing_ok=True)
            return
        self.token = token
        self._expires_at = expires_at
        self.granted_scopes = parse_scopes(saved.get("scopes"))
        self._static_token = False
        logger.info(
            "Resumed saved Linode sign-in for scopes %s",
            serialize_scopes(self.granted_scopes),
        )

    def _save_token(self) -> None:
        """Persist the OAuth token until it expires, readable only by this user."""
        path = _token_path()
        tmp = path.with_suffix(".tmp")
        data = json.dumps(
            {
                "client_id": self.client_id,
                "access_token": self.token,
                "expires_at": self._expires_at,
                "scopes": serialize_scopes(self.granted_scopes),
            }
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(data)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("Could not save Linode sign-in to %s: %s", path, exc)
