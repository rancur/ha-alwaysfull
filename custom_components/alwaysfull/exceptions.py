"""Exceptions for the Always Full integration."""

from __future__ import annotations


class AlwaysFullError(Exception):
    """Base error, optionally carrying the vendor's own envelope code.

    `code` is the `code` field of the vendor's `{code, msg, data}`
    envelope, as a string, and it is `None` for anything this integration
    raised itself (a transport failure, a refusal of our own).

    It exists because the code was being THROWN AWAY. The vendor's `msg`
    is free prose that changes between endpoints and firmware versions --
    "The setup failed." was what a live install saw -- so the code is the
    only part of a vendor refusal that anyone can look up, compare between
    reports or search an issue tracker for. Carrying it on the exception
    is what lets the user-facing message quote both without `api.py`
    formatting a sentence for a user it knows nothing about.

    Keyword-only, so every existing `AlwaysFullError("some message")` is
    unchanged and a code is never supplied by accident in the position of
    a message.
    """

    def __init__(self, *args: object, code: str | None = None) -> None:
        """Store the vendor's envelope code alongside the usual message."""
        super().__init__(*args)
        self.code = code


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
