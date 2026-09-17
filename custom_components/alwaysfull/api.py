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
    CREDENTIAL_REJECTION_CODES,
    SIGN_SECRET,
)
from .exceptions import (
    AlwaysFullAuthError,
    AlwaysFullCredentialsError,
    AlwaysFullError,
    AlwaysFullRateLimitError,
)

if TYPE_CHECKING:
    import aiohttp

_HTTP_TOO_MANY_REQUESTS = 429


def _truncate_offset_to_hours(offset: timedelta) -> int:
    """Truncate a UTC offset to whole hours, toward zero (not floor).

    A half-hour-or-finer offset like -3:30 must become -3, not -4: floor
    division on a negative value rounds away from zero, which is wrong here.
    """
    return int(offset.total_seconds() / 3600)


def _hash_password(password: str) -> str:
    """Return the vendor's client-side password hash: md5(md5(plaintext)).

    The plaintext password is never sent over the wire, and never logged.
    """
    once = hashlib.md5(password.encode()).hexdigest()
    return hashlib.md5(once.encode()).hexdigest()


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

    @property
    def tz_offset_hours(self) -> int | None:
        """Return the UTC offset, in whole hours, sent as `timeZone`."""
        return self._tz_offset_hours

    @tz_offset_hours.setter
    def tz_offset_hours(self, value: int | None) -> None:
        """Update the offset in place.

        Writable because the correct offset is not constant: a zone that
        observes DST changes offset twice a year, and a client constructed
        once at config-entry setup would otherwise keep sending the
        offset that was correct the day Home Assistant last restarted.
        """
        self._tz_offset_hours = value

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
            raise AlwaysFullRateLimitError(msg)

        payload = await resp.json(content_type=None)
        code = str(payload.get("code"))

        if code == CODE_OK:
            return payload.get("data")
        if code == CODE_TOKEN_EXPIRED:
            raise AlwaysFullAuthError(payload.get("msg") or "Token expired")
        if code in CREDENTIAL_REJECTION_CODES:
            # Distinct from the token-expiry case above (see exceptions.py)
            # but still an `AlwaysFullAuthError`, so every existing caller --
            # the coordinator's one-silent-re-login path included -- behaves
            # exactly as before.
            raise AlwaysFullCredentialsError(
                payload.get("msg") or "Invalid email address or password"
            )

        # Anything unclassified stays a plain error: guessing that an unknown
        # code means "bad credentials" would push users into a reauth flow
        # that cannot fix whatever actually went wrong.
        raise AlwaysFullError(payload.get("msg") or f"Unexpected response code {code}")

    # -- Endpoint methods ---------------------------------------------------
    #
    # These are thin wrappers over request(): no signing/envelope logic lives
    # here, only the path, the HTTP method and the vendor's own (non-uniform)
    # parameter names. See the design doc's "Traps the implementation must
    # honour" section for the full rationale behind each one.

    async def login(self, email: str, password: str) -> Any:
        """Log in, store the returned token on this client, and return it.

        The vendor hashes the password client-side as md5(md5(plaintext))
        before it ever goes over the wire; the plaintext itself is not sent.
        """
        token = await self.request(
            "POST",
            "/app/user/login",
            {"account": email, "password": _hash_password(password)},
        )
        self.token = token
        return token

    async def device_list(self) -> Any:
        """List every bowl on the account.

        Returns the vendor's pagination envelope unchanged --
        ``{pageNum, pageSize, totalPage, totalSize, hasNext, data: [...]}``.
        Each row carries the full device state (list and detail rows share
        the same shape), so one call covers every bowl on the account.
        """
        return await self.request("GET", "/app/device/list", {"pageNum": 1, "pageSize": 50})

    async def device_config(self, device_id: str) -> Any:
        """Return the raw device config object for ``device_id``."""
        return await self.request("GET", "/app/device/config", {"deviceId": device_id})

    async def drinking_log(self, device_id: str, units: int, start: str, end: str) -> Any:
        """Return the drinking log for ``device_id`` between ``start`` and ``end``.

        ``start``/``end`` are local dates as ``YYYY-MM-DD``; they are widened
        to a full local start-of-day/end-of-day timestamp with no timezone
        suffix, matching what the vendor app sends.

        Unlike every other list-shaped endpoint here, this one is **not**
        paginated: the vendor returns a bare array of
        ``{drinkingDate, totalCapacity}`` rows, not a pagination envelope.
        That asymmetry is real (confirmed against the live API) and is
        passed straight through rather than normalised into a fake envelope.
        """
        params = {
            "deviceId": device_id,
            "units": units,
            "startTime": f"{start} 00:00:00",
            "endTime": f"{end} 23:59:59",
        }
        return await self.request("GET", "/app/drinking/log", params)

    async def notify_config(self) -> Any:
        """Return the account-level notification config (no device id)."""
        return await self.request("GET", "/app/notify/getConfig")

    async def save_notify_config(self, config: dict[str, Any]) -> Any:
        """Save the notification config.

        ``saveConfig`` is a whole-object read-modify-write. The live payload
        carries both ``notifyItems`` and ``notifyList`` -- identical arrays
        in the live capture -- and it is not known which one the server
        actually reads back on save. This round-trips the entire object the
        caller hands in (typically the dict from ``notify_config()`` with
        one field toggled) rather than reconstructing a payload from a
        subset of fields, so whichever array the server reads stays
        consistent with the other.
        """
        return await self.request("POST", "/app/notify/saveConfig", config)

    async def notify_log(self, device_id: str, page_size: int = 20) -> Any:
        """Return page 1 of the notification log for ``device_id``."""
        params = {"deviceId": device_id, "pageNum": 1, "pageSize": page_size}
        return await self.request("GET", "/app/notify/getNotifyLog", params)

    # -- Writers ---------------------------------------------------------
    #
    # `device_id` is always the value identifying the bowl; each method maps
    # it onto the wire key the vendor actually expects for that endpoint
    # (`devNo` for most writers, `deviceId` for setUnits). `**fields` are
    # sent verbatim under whatever wire names the caller supplies -- unit
    # conversion (minutes<->seconds, days<->seconds, capacity<->filterCapacity,
    # the deviceType 9"/7" inversion, etc.) is the caller's job, not this
    # layer's.

    async def set_flush_config(self, device_id: str, **fields: Any) -> Any:
        """Set flush config (``cleanCycle`` seconds, ``cleanTime``, ...)."""
        return await self.request("POST", "/app/device/flushConfig", {"devNo": device_id, **fields})

    async def set_sleep_config(self, device_id: str, **fields: Any) -> Any:
        """Set sleep config (``sleepStart``/``sleepEnd`` minutes since midnight, ...)."""
        return await self.request("POST", "/app/device/sleepConfig", {"devNo": device_id, **fields})

    async def set_filter_config(self, device_id: str, **fields: Any) -> Any:
        """Set filter config.

        Filter capacity is READ back as ``capacity`` but must be WRITTEN as
        ``filterCapacity`` -- pass it under that name in ``fields``.
        """
        return await self.request("POST", "/app/device/filterConfig", {"devNo": device_id, **fields})

    async def set_maintenance_config(self, device_id: str, **fields: Any) -> Any:
        """Set maintenance config (``deviceCanUseTime`` seconds, ...)."""
        return await self.request("POST", "/app/device/maintenanceConfig", {"devNo": device_id, **fields})

    async def set_water_config(self, device_id: str, **fields: Any) -> Any:
        """Set water config.

        Must include ``units`` echoed from ``device_config``/``device_list``
        or the server misreads the daily min/max thresholds -- the caller's
        responsibility, not this method's.
        """
        return await self.request("POST", "/app/device/waterConfig", {"devNo": device_id, **fields})

    async def set_log_config(self, device_id: str, **fields: Any) -> Any:
        """Set drinking-log recording config (``logState``, ...)."""
        return await self.request("POST", "/app/device/logConfig", {"devNo": device_id, **fields})

    async def set_units(self, device_id: str, units: int) -> Any:
        """Set display units. Uses ``deviceId``, NOT ``devNo``."""
        return await self.request("POST", "/app/device/setUnits", {"deviceId": device_id, "units": units})

    async def set_device_type(self, device_id: str, device_type: int) -> Any:
        """Set bowl size. NOTE the vendor's enum is inverted: 0 = 9", 1 = 7"."""
        return await self.request("POST", "/app/device/setType", {"devNo": device_id, "deviceType": device_type})

    async def reset_filter(self, device_id: str) -> Any:
        """Reset filter life."""
        return await self.request("POST", "/app/device/reset/filter", {"devNo": device_id})
