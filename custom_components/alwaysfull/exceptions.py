"""Exceptions for the Always Full integration."""

from __future__ import annotations


class AlwaysFullError(Exception):
    """Base error."""


class AlwaysFullAuthError(AlwaysFullError):
    """Credentials or token rejected."""


class AlwaysFullRateLimit(AlwaysFullError):
    """Server asked us to slow down."""
