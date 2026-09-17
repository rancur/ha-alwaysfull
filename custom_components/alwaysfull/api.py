"""Always Full HTTP transport: request signing and error mapping.

This module has no Home Assistant imports so it can be exercised by plain
pytest, with no test harness, which keeps the hard part (signing, auth,
error mapping) cheap to test.

Signing scheme (reverse-engineered from the vendor Android app and verified
against the live production server): merge ``appId`` / ``appType`` /
``appVersion`` / ``timeZone`` into the request body, build ``k=v&`` over the
body's keys sorted ascending (skipping ``None`` values, JSON-encoding
non-string values compactly), append
``f"{timestamp_ms}{token}{secret}"``, then take the lowercase hex MD5 digest.
That digest is sent as the ``sign`` header alongside ``timestamp`` and
``token``.

Security note: the token, the computed ``sign`` value and any password must
never be logged, printed, or embedded in an exception message.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .const import (
    API_BASE,
    APP_ID,
    APP_TYPE,
    APP_VERSION,
    CODE_OK,
    CODE_TOKEN_EXPIRED,
    SIGN_SECRET,
)
from .exceptions import AlwaysFullAuthError, AlwaysFullError, AlwaysFullRateLimit

if TYPE_CHECKING:
    import aiohttp

_HTTP_TOO_MANY_REQUESTS = 429


def _truncate_offset_to_hours(offset: timedelta) -> int:
    """Truncate a UTC offset to whole hours, toward zero (not floor).

    A half-hour-or-finer offset like -3:30 must become -3, not -4: floor
    division on a negative value rounds away from zero, which is wrong here.
    """
    return int(offset.total_seconds() / 3600)


def _local_time_zone_offset_hours() -> int:
    """Return the OS's local UTC offset in whole hours, as a last-resort fallback.

    This reads the offset the *process* is running under, which is not
    necessarily the offset Home Assistant is configured for (a HAOS/Docker
    host may run `TZ=UTC` while HA itself is configured for another zone).
    Callers that know the real zone should pass `tz_offset_hours` to
    `AlwaysFullClient` instead of relying on this.
    """
    offset = datetime.now().astimezone().utcoffset()
    if offset is None:
        return 0
    return _truncate_offset_to_hours(offset)


class AlwaysFullClient:
    """Thin async HTTP client for the Always Full vendor API."""

    def __init__(
        self,
        session: aiohttp.ClientSession | None,
        *,
        token: str = "",
        tz_offset_hours: int | None = None,
    ) -> None:
        """Store the aiohttp session (may be None for signing-only use) and token.

        `tz_offset_hours` is the UTC offset, in whole hours, to send as the
        `timeZone` field. Pass it explicitly whenever the caller knows the
        zone it actually needs (e.g. Home Assistant's configured time zone,
        via Task 4's coordinator) — leaving it `None` falls back to the
        *OS process's* local offset, which can silently disagree with HA's
        configured zone on a HAOS/Docker host and corrupt the vendor's
        daily-reset-boundary logic.
        """
        self._session = session
        self._token = token
        self._tz_offset_hours = tz_offset_hours

    @property
    def token(self) -> str:
        """Return the current auth token."""
        return self._token

    @token.setter
    def token(self, value: str) -> None:
        self._token = value

    def canonical(self, body: dict[str, Any], timestamp: int) -> str:
        """Build the canonical signing string for ``body`` at ``timestamp``.

        Iterates ``body`` keys sorted ascending, skipping ``None`` values,
        rendering strings as-is and everything else as compact JSON, then
        appends ``f"{timestamp}{token}{secret}"``.
        """
        parts: list[str] = []
        for key in sorted(body):
            value = body[key]
            if value is None:
                continue
            rendered = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
            parts.append(f"{key}={rendered}&")
        parts.append(f"{timestamp}{self._token}{SIGN_SECRET}")
        return "".join(parts)

    def sign(self, body: dict[str, Any], timestamp: int) -> str:
        """Return the lowercase hex MD5 signature for ``body`` at ``timestamp``."""
        return hashlib.md5(self.canonical(body, timestamp).encode()).hexdigest()

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Make a signed request to ``path`` and return the envelope's ``data``.

        For ``GET`` the merged parameters are sent as the query string. For
        every other method they are sent as a JSON body. Either way they are
        also what gets signed.
        """
        if self._session is None:
            msg = "AlwaysFullClient has no aiohttp session configured"
            raise AlwaysFullError(msg)

        tz_offset = self._tz_offset_hours if self._tz_offset_hours is not None else _local_time_zone_offset_hours()

        merged: dict[str, Any] = dict(params or {})
        merged["appId"] = APP_ID
        merged["appType"] = APP_TYPE
        merged["appVersion"] = APP_VERSION
        merged["timeZone"] = tz_offset

        # Strip None here, once, so the dict that gets signed is byte-for-byte
        # the same dict that gets sent — canonical()/sign() already skip None
        # keys, but json=merged would otherwise serialise them as JSON `null`,
        # a body/signature mismatch waiting to happen.
        merged = {key: value for key, value in merged.items() if value is not None}

        timestamp = int(time.time() * 1000)
        signature = self.sign(merged, timestamp)
        headers = {
            "timestamp": str(timestamp),
            "sign": signature,
            "token": self._token,
        }

        url = f"{API_BASE}{path}"
        method_upper = method.upper()

        if method_upper == "GET":
            query = {
                key: value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
                for key, value in merged.items()
            }
            async with self._session.request(method_upper, url, headers=headers, params=query) as resp:
                return await self._handle_response(resp)

        async with self._session.request(method_upper, url, headers=headers, json=merged) as resp:
            return await self._handle_response(resp)

    async def _handle_response(self, resp: aiohttp.ClientResponse) -> Any:
        """Map an HTTP response onto the vendor's {code, msg, data} envelope."""
        if resp.status == _HTTP_TOO_MANY_REQUESTS:
            msg = "Server responded 429 Too Many Requests"
            raise AlwaysFullRateLimit(msg)

        payload = await resp.json(content_type=None)
        code = str(payload.get("code"))

        if code == CODE_OK:
            return payload.get("data")
        if code == CODE_TOKEN_EXPIRED:
            raise AlwaysFullAuthError(payload.get("msg") or "Token expired")

        raise AlwaysFullError(payload.get("msg") or f"Unexpected response code {code}")
