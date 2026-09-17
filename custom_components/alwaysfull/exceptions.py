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
