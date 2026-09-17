"""Exceptions for the Always Full integration."""

from __future__ import annotations


class AlwaysFullError(Exception):
    """Base error."""


class AlwaysFullAuthError(AlwaysFullError):
    """Credentials or token rejected."""


class AlwaysFullCredentialsError(AlwaysFullAuthError):
    """The email/password pair itself was rejected, not just the token.

    A subclass rather than a flag so that `except AlwaysFullAuthError`
    keeps catching it -- the coordinator's one-silent-re-login-then-reauth
    path is unchanged by this distinction existing. It is here because the
    two cases genuinely differ: a token expiry mid-session is worth one
    silent re-login, whereas this during a re-login means the stored
    credentials are wrong and no amount of retrying will help.
    """


class AlwaysFullRateLimitError(AlwaysFullError):
    """Server asked us to slow down."""


class AlwaysFullReloginThrottledError(AlwaysFullError):
    """WE asked ourselves to slow down: the re-login budget is spent.

    Deliberately NOT an `AlwaysFullAuthError`, and the distinction is the
    whole point of the class existing. A throttled re-login is not an
    authentication failure -- the stored credentials are fine and have not
    been tested -- so it must not reach `ConfigEntryAuthFailed`, which
    would put the user in front of a reauth prompt that succeeds and
    changes nothing. As a plain `AlwaysFullError` it degrades the poll to
    `UpdateFailed` and a write to a readable `HomeAssistantError`, both of
    which recover on their own once the window slides.
    """
