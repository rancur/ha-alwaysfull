#!/usr/bin/env python3
"""Fail the build if anything identifying is in this repository, ever.

Run from the repository root, with no arguments and no dependencies:

    python scripts/check_no_pii.py

Exits 0 when clean, 1 on any finding, 2 when it could not do its job.

WHY THIS SCANS HISTORY
----------------------

Because a working-tree check is worse than no check. Deleting a leaked
identifier in a new commit makes `grep` on a checkout report green while
the value stays in the object database, fetchable by anyone who clones the
repository and asked for by `git log -p`. Believing a tree-only green is
how this repository leaked an owner's device identifier in the first
place, and it was caught by luck rather than by CI. So every blob
reachable from every ref is scanned, along with every commit message and
every author and committer identity, and a shallow clone is a hard error
rather than a quiet pass -- see `_assert_full_clone`.

The consequence is worth stating plainly: a finding in history CANNOT be
fixed by a commit. It needs a history rewrite and a force-push, which is
why this runs on every push and not once a release.

WHAT IT LOOKS FOR
-----------------

Seven rules, each with an allowlist and each allowlist entry carrying a
reason. An allowlist without reasons decays into "whatever was failing the
day someone was in a hurry".

Two of them -- `street-address` and `zip-state` -- guard against something
this repository has never contained: the account holder's postal address,
which the vendor's `/app/user/loginInfo` endpoint hands out in plain text
to anyone holding the account's token. That endpoint is deliberately not
called (see `api.py`), so these rules are a trap set before the mistake,
not a fix after it.

THIS FILE EXCLUDES ITSELF from the scan, at every path and in every
historical blob. It has to: it necessarily contains examples of the things
it searches for, and a guard that fails on its own source is a guard that
gets deleted.

FINDINGS ARE PRINTED MASKED. A public repository has public CI logs, so
printing a leaked address in full to prove it was found would publish it a
second time, to a place that is also permanent. The rule, the file and the
line number are enough to find it locally.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

# This file, as git spells it. Compared against every path git reports, in
# the tree and in history.
SELF_PATH = "scripts/check_no_pii.py"

# ---------------------------------------------------------------------------
# Emails
# ---------------------------------------------------------------------------

# The canonical synthetic address. Everything in the fixtures and the test
# suite uses this one.
CANONICAL_EMAIL = "user@example.com"

# Anything under a domain RFC 2606 reserves for documentation and testing.
# These domains cannot be registered, so no address under one can belong to
# a person.
#
# This is a deliberate widening of "only `user@example.com` is allowed",
# and the reason is that the narrow version fails a CORRECT test suite:
# `test_config_flow.py` needs a SECOND, different account to prove the
# unique-id abort fires, and `User@Example.COM` to prove the unique id is
# case-folded. Rewriting those to the canonical address would delete what
# they test. A rule that forces a correct test to be made wrong gets
# suppressed, and a suppressed rule guards nothing.
RESERVED_EMAIL_DOMAINS = (
    "example.com",
    "example.org",
    "example.net",
    "invalid",
    "test",
    "localhost",
)

ALLOWED_EMAILS = {
    # Role addresses that appear in commit trailers, not in content.
    "noreply@anthropic.com": "Claude Code's co-author trailer; a no-reply role address.",
    "rancur@users.noreply.github.com": (
        "GitHub's own privacy address for the repository owner's pseudonymous "
        "handle, which is already public in the manifest's codeowners field. "
        "It is what GitHub issues INSTEAD of a real address."
    ),
    # Historical only. Present in blobs that are already published and
    # cannot be removed without rewriting history and force-pushing.
    "u@e.com": (
        "HISTORICAL. A placeholder in tests/test_init.py and the plan doc, "
        "replaced by the canonical address in commit d5e79db. It is nobody's "
        "address -- it was two letters chosen to be short -- but `e.com` is a "
        "registrable domain, so the reserved-domain rule above does not cover "
        "it. It survives only in pre-d5e79db blobs."
    ),
}

# ---------------------------------------------------------------------------
# Device identifiers
# ---------------------------------------------------------------------------

# The vendor's device id IS the bowl's MAC address with the separators
# stripped, so a bare twelve-hex run is the shape being hunted.
#
# Note what is NOT here: the owner's real device id. Writing the banned
# value into the guard would put it in the repository -- permanently, in
# this file's own history -- which is the thing the guard exists to
# prevent. The rule is inverted instead: every twelve-hex run is a finding
# unless it is one of the synthetic ids below. That catches the known leak
# and every identifier nobody has thought of, without naming any of them.
SYNTHETIC_DEVICE_IDS = {
    "aabbccddeeff": "The primary fixture bowl, in tests/fixtures/ and the snapshots.",
    "001122334455": "The second fixture bowl, from device_list_multi.json.",
    "ffeeddccbbaa": "A third synthetic id used only by tests/test_entity.py.",
}

# `entity.py::account_key` truncates a SHA-256 of the account's email to
# twelve hex characters, which is MAC-shaped by construction and lands in
# every account-level entity's unique id -- and therefore in the switch
# snapshot. Computed here rather than pasted, so it stays correct if the
# suite's account address ever changes and so its provenance is obvious.
ACCOUNT_KEY_LENGTH = 12
DERIVED_ACCOUNT_KEY = hashlib.sha256(CANONICAL_EMAIL.encode()).hexdigest()[:ACCOUNT_KEY_LENGTH]

# The separators a MAC address is written with, stripped before the
# twelve-hex test so that every rendering collapses to one comparison.
#
# This rule used to match only a BARE twelve-hex run, which meant the
# canonical spelling walked straight past it: `aa:bb:...` exits 0 while the
# identical value without colons exits 1. That is not a corner case. It is
# how Home Assistant itself writes a MAC (`format_mac()`, and every
# `CONNECTION_NETWORK_MAC` entry in a device registry), how every router UI
# displays one, and how a person pastes one into an issue. The concrete
# leak: adding `connections={(CONNECTION_NETWORK_MAC, format_mac(device_id))}`
# to `device_info` is a completely standard thing to do for a networked
# device, and it would have published with this guard green.
#
# Normalising is deliberately preferred over adding a regex per spelling:
# one more rendering nobody listed is one more silent miss, whereas a
# candidate that normalises to twelve hex characters is a MAC however it
# was punctuated.
MAC_SEPARATORS = str.maketrans("", "", ":-.")


# --------------------------------------------------------------------------
# Words that must never appear, stored WITHOUT the plaintext
# --------------------------------------------------------------------------

# The owner's name must fail the build. Writing it here in plain text would
# put it in the repository permanently -- in this file, and in this file's
# own history -- which is precisely what the guard exists to prevent, and
# the same argument that drove the device-id rule to be inverted rather
# than to name the banned id.
#
# So the name is stored as a digest instead. Every alphabetic run of four
# or more characters is lowercased and hashed, and a token whose digest is
# listed here is a finding. The plaintext never enters the repository and
# the rule still fires.
#
# NOT a security control, and not claimed to be one: a surname is a
# dictionary word to anyone who wants to try, so these digests are not
# irreversible. They exist so the value is not sitting in plain text in a
# file people read. Same standard, and the same honest limit, as the
# device labels in `diagnostics.py`.
WORD_DIGEST_LENGTH = 16

# Four, because every name this covers is longer than that and because
# hashing every two-letter token in every blob in history buys nothing.
MIN_WORD_LENGTH = 4

DENIED_WORD_DIGESTS = {
    "d28e09dad1f40a9b": "The repository owner's surname.",
    "8bcc5527c005fff6": "The same surname pluralised -- the household.",
    "5913d0181e68c520": (
        "Given name and surname run together, which is how the owner's "
        "legacy domain and several old handles spell it. Tokenising splits "
        "on punctuation but not on a word boundary that is not there, so "
        "the compound needs its own digest."
    ),
}

# Deliberately NOT covered, and worth saying so rather than leaving a
# reader to wonder: the owner's GIVEN name on its own. It is an ordinary
# English auxiliary verb, so a digest for it would fire on "it will fail",
# "Home Assistant will reload" and several hundred other lines of honest
# prose. A rule that cries wolf on every page gets switched off, and a
# switched-off rule guards nothing.


def _word_digest(word: str) -> str:
    """Return the truncated digest this guard compares tokens against."""
    return hashlib.sha256(word.lower().encode()).hexdigest()[:WORD_DIGEST_LENGTH]


# ---------------------------------------------------------------------------
# Postal addresses
# ---------------------------------------------------------------------------

# The vendor's `/app/user/loginInfo` endpoint returns the account holder's
# `address1`, `address2`, `city`, `st` and `zip`. This integration does not
# call it (see `api.py`) and `diagnostics.TO_REDACT` covers those fields
# pre-emptively -- but a redaction set only protects the diagnostics file.
# Nothing stopped a home address reaching this repository as a captured
# fixture, a pasted log line or a commit message, and until these rules
# existed nothing would have noticed.
#
# As with the device id and the owner's name, the real address is NOT
# written here. The rule is inverted instead: address-SHAPED text is a
# finding unless it is one of the obviously synthetic addresses below.

# Written in full and abbreviated, because both spellings occur and a
# person pasting an address uses whichever their post office does.
STREET_TYPE_WORDS = (
    "Street",
    "Road",
    "Drive",
    "Avenue",
    "Lane",
    "Boulevard",
    "Court",
    "Circle",
    "Place",
    "Terrace",
    "Way",
)

# Matched CASE-SENSITIVELY, unlike the words above, and that asymmetry is
# the whole reason this rule is usable. Every entry here is also an
# ordinary English abbreviation -- `St` is Saint, `Dr` is Doctor, `Ct` and
# `Pl` and `Cir` turn up in identifiers -- so matching them case-
# insensitively would fire on "version 2 dr" and a hundred similar
# fragments of honest prose and snapshot data. Requiring the capitalised
# spelling costs a lowercased address and buys a rule nobody switches off.
STREET_TYPE_ABBREVIATIONS = (
    "St",
    "Rd",
    "Dr",
    "Ave",
    "Ln",
    "Blvd",
    "Ct",
    "Cir",
    "Pl",
)

# One to four tokens of street name between the house number and the street
# type. Four covers "N 79th Frontage Rd"; requiring at least one is what
# keeps "Task 2 Way" -- a number immediately followed by a street type --
# from being read as an address.
_STREET_NAME_TOKENS = r"(?:[A-Za-z0-9][A-Za-z0-9.'-]*[ \t]+){1,4}"

ALLOWED_ADDRESSES = {
    "1 test street": (
        "The synthetic address in tests/test_diagnostics.py, which feeds a "
        "loginInfo-shaped payload through the diagnostics path to prove the "
        "postal fields are redacted. It has to be address-SHAPED or it would "
        "not exercise the thing it exists to exercise."
    ),
}

# The 50 states, DC and the inhabited territories, uppercase. Membership is
# what makes the ZIP rule quiet: `12345` on its own is a line number, an id
# or a capacity, and only a state code beside it makes it an address.
US_STATE_ABBREVIATIONS = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY", "AS", "GU", "MP", "PR", "VI",
)

US_STATE_NAMES = (
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
    "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "Ohio",
    "Oklahoma", "Oregon", "Pennsylvania", "Tennessee", "Texas", "Utah",
    "Vermont", "Virginia", "Washington", "Wisconsin", "Wyoming",
)

# The multi-word state names, kept separate only because they have to be
# spelled with their spaces.
US_STATE_NAMES_MULTIWORD = (
    "New Hampshire",
    "New Jersey",
    "New Mexico",
    "New York",
    "North Carolina",
    "North Dakota",
    "Rhode Island",
    "South Carolina",
    "South Dakota",
    "West Virginia",
    "District of Columbia",
    "Puerto Rico",
)

ALLOWED_ZIP_STATE_PAIRS: dict[str, str] = {
    # Empty on purpose. No test needs a state/ZIP pair -- the diagnostics
    # test's `st` and `zip` values are sentinels, not plausible ones --
    # so there is nothing legitimate for this to permit yet. An entry here
    # should cost somebody a sentence explaining why.
}


def _normalise_address(value: str) -> str:
    """Collapse an address to the form the allowlists are compared against."""
    return " ".join(value.lower().replace(",", " ").split()).rstrip(".")

# ---------------------------------------------------------------------------
# 32-hex tokens
# ---------------------------------------------------------------------------

# The vendor's request signature is an MD5 digest and its session token is
# an opaque string, so a bare 32-hex run in this repository is either an
# expected test digest or something that should not be here.
#
# The vendor's signing secret is NOT listed: it is 31 characters and
# contains `s` and `v`, so it is not hex and this rule cannot match it. It
# is public in the vendor's distributed APK, ships in const.py on purpose,
# and is nobody's secret.
#
# Each entry below is a digest of a synthetic input, computed by a test.
# Adding one is a line of work, deliberately: a new 32-hex constant in this
# repository SHOULD require a human to write down what it is.
ALLOWED_HEX32 = {
    "d41d8cd98f00b204e9800998ecf8427e": "MD5 of the empty string. Universally known.",
    "09e59d24d687126cda92550f23c5c3f9": "Expected signature, tests/test_api_signing.py.",
    "8f62367db288f5479ce5936207fda3c0": "Expected signature, tests/test_api_signing.py.",
    "b7bcfcb64b0dd8e1f70b6d2e2e0d4c1c": "Expected signature quoted in the plan doc.",
    "d7fe7a84e8373b67aec2832ca6f9d477": "Expected signature, tests/test_api_endpoints.py.",
}


@dataclass(frozen=True)
class Rule:
    """One thing that must not appear, and the exceptions to it."""

    name: str
    pattern: re.Pattern[str]
    why: str
    allowed: frozenset[str] = frozenset()
    fold_case: bool = False

    def permits(self, value: str) -> bool:
        """Return whether `value` is an allowed instance of this pattern."""
        candidate = value.lower() if self.fold_case else value
        return candidate in self.allowed

    def mask(self, value: str) -> str:
        """Return the form of `value` that is safe to print in a CI log."""
        return _mask(value)


def _email_allowlist() -> frozenset[str]:
    """Return every literal address this repository may contain."""
    return frozenset({CANONICAL_EMAIL, *ALLOWED_EMAILS})


class EmailRule(Rule):
    """The email rule, which allows a whole set of domains as well as literals."""

    def permits(self, value: str) -> bool:
        """Allow the literals, plus anything under an RFC 2606 reserved domain."""
        candidate = value.lower()
        if candidate in self.allowed:
            return True
        _, _, domain = candidate.partition("@")
        return any(
            domain == reserved or domain.endswith(f".{reserved}")
            for reserved in RESERVED_EMAIL_DOMAINS
        )


class DeviceIdRule(Rule):
    """The device-id rule, which compares MAC renderings after normalising."""

    def permits(self, value: str) -> bool:
        """Allow only the synthetic ids, whatever punctuation was used.

        `aa:bb:...`, `aa-bb-...`, `aabb.ccdd...` and the bare run are the
        same twelve hex characters, so they are compared as the same
        twelve hex characters.
        """
        return value.translate(MAC_SEPARATORS).lower() in self.allowed


@dataclass(frozen=True)
class HashedWordRule(Rule):
    """A rule whose list is a DENY list of digests, not an allowlist.

    Every other rule here says "this shape is suspicious unless it is one
    of these known values". This one inverts that: ordinary words are fine,
    and a specific few are not -- but those few cannot be written down, so
    they are compared by digest.
    """

    denied: frozenset[str] = frozenset()

    def permits(self, value: str) -> bool:
        """Allow every token except the ones whose digest is listed."""
        return _word_digest(value) not in self.denied

    def mask(self, value: str) -> str:
        """Redact the token ENTIRELY, not just its tail.

        `_mask` keeps the first three characters, which is the right
        trade-off for a MAC or an address -- enough for the owner to
        recognise, useless to anyone else. It is the wrong trade-off for a
        name: three letters of a surname in a public CI log is most of the
        surname. The rule name and the line number are enough to find it.
        """
        return "*" * len(value)


class PostalAddressRule(Rule):
    """A rule whose allowlist is compared after normalising punctuation.

    `1 Test Street`, `1 test street,` and `1  Test  Street.` are the same
    address, so they are compared as the same address.
    """

    def permits(self, value: str) -> bool:
        """Allow only the synthetic addresses, however they were punctuated."""
        return _normalise_address(value) in self.allowed

    def mask(self, value: str) -> str:
        """Redact the finding ENTIRELY, like the name rule and unlike the rest.

        `_mask` keeps the first three characters, which is right for a MAC:
        enough for the owner to recognise it, useless to a reader. It is
        wrong for an address, where the first three characters are most of
        the house number -- the one part of an address a person would
        actually have to guess. The rule name, the file and the line number
        locate it just as well, and they are not somebody's doorstep.
        """
        return "*" * len(value)


RULES: tuple[Rule, ...] = (
    EmailRule(
        name="email",
        pattern=re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
        why="an email address that is not a documentation address",
        allowed=_email_allowlist(),
        fold_case=True,
    ),
    Rule(
        name="private-ip",
        pattern=re.compile(
            r"(?<![\d.])(?:"
            r"192\.168\.\d{1,3}\.\d{1,3}"
            r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
            r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
            # `(?!\d)` and NOT `(?![\d.])`. The dot-excluding version was
            # written first and was wrong in the most ordinary case there
            # is: an address at the END OF A SENTENCE. "...on 192.168.1.44."
            # has a full stop after the last octet, the lookahead rejected
            # it, and the whole line came back clean. Caught only because
            # the proof-of-failure run used a realistic commit message.
            r")(?!\d)"
        ),
        why="an RFC1918 address, which describes somebody's actual network",
    ),
    Rule(
        name="home-path",
        pattern=re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\Users\\)[A-Za-z0-9._-]+"),
        why="a local home-directory path, which usually carries a username",
    ),
    Rule(
        name="hex32",
        pattern=re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])"),
        why="a 32-hex token that is not a known test digest",
        allowed=frozenset(ALLOWED_HEX32),
        fold_case=True,
    ),
    DeviceIdRule(
        name="device-id",
        # Twelve hex characters in any of the four renderings a MAC is
        # written in. The regex only has to FIND a candidate; deciding
        # whether it is allowed happens after the separators are stripped,
        # in `DeviceIdRule.permits`, so a spelling nobody enumerated still
        # collapses onto the same comparison.
        #
        # The second lookbehind excludes the fractional part of a decimal
        # -- `119.531736111111` is a sensor state in a snapshot, not a MAC
        # -- while still catching a real id that happens to follow a dot.
        # `\b` is NOT used: it treats the boundary between hex and non-hex
        # characters inconsistently at the edges of a longer identifier.
        #
        # The trailing guard is `(?![0-9a-fA-F])` and nothing more. Adding
        # `.` or `-` to it would break a MAC at the end of a sentence,
        # which is the same mistake the RFC1918 rule above already made
        # once. The cost is that an unusually long punctuated hex chain can
        # match a six-group window of itself; over-flagging is the safe
        # direction and the allowlist is there for it.
        pattern=re.compile(
            r"(?<![0-9a-fA-F])(?<!\d\.)(?:"
            r"[0-9a-fA-F]{12}"  # aabbccddeeff
            r"|[0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){5}"  # aa:bb:... and aa-bb-...
            r"|[0-9a-fA-F]{4}(?:\.[0-9a-fA-F]{4}){2}"  # aabb.ccdd.eeff
            r")(?![0-9a-fA-F])"
        ),
        why="twelve hex characters, which is the shape of this device's MAC address",
        allowed=frozenset({*SYNTHETIC_DEVICE_IDS, DERIVED_ACCOUNT_KEY}),
    ),
    PostalAddressRule(
        name="street-address",
        # A house number, one to four words of street name, then a street
        # type. The full words are matched case-insensitively with an
        # inline scoped flag; the abbreviations, which are also ordinary
        # English words, only in their capitalised spelling -- see
        # `STREET_TYPE_ABBREVIATIONS` for why that trade is worth making.
        #
        # The trailing guard is `\b` and no more. Adding `.` to it would
        # miss an address at the END OF A SENTENCE, which is exactly the
        # mistake the RFC1918 rule above already made once and the single
        # most likely way an address gets written into a commit message.
        #
        # `[ \t]` rather than `\s`: `_scan` works line by line, so a `\s`
        # that could match a newline would only ever be a way to join two
        # unrelated lines into a false finding.
        #
        # `(?<!\d,)` is why a THOUSANDS SEPARATOR is not a house number.
        # `(?<![\d.])` alone stops a long bare number and a decimal from
        # starting a match -- `10512000` and `1.67` are both safe -- but it
        # does nothing about a comma-grouped one, because the character
        # before the final group is a comma, not a digit. So the prose
        # "dividing it by 2,592,000 on the way out" matched `000 on the
        # way`: house number `000`, street name `on the`, street type
        # `way`. That fired on this repository's own documentation, which
        # is how it was found.
        #
        # This rejects a digit-then-comma before the number and nothing
        # else, so it cannot hide a real address: an address written after
        # a comma ("PO Box 5, 12 Test Street") has a SPACE before the house
        # number and is still caught. What it costs is an address whose
        # house number is glued to the tail of a number, which is not a way
        # anyone writes one.
        pattern=re.compile(
            rf"(?<![\d.])(?<!\d,)\d{{1,6}}[A-Za-z]?[ \t]+{_STREET_NAME_TOKENS}"
            rf"(?:(?i:{'|'.join(STREET_TYPE_WORDS)})|(?:{'|'.join(STREET_TYPE_ABBREVIATIONS)}))"
            r"\b"
        ),
        why="a street address, which is somebody's front door",
        allowed=frozenset(ALLOWED_ADDRESSES),
    ),
    PostalAddressRule(
        name="zip-state",
        # A US state -- spelled out or as its uppercase two-letter code --
        # immediately before a five-digit ZIP, with an optional comma and
        # an optional +4. Five digits ALONE is not a finding: this
        # repository is full of capacities, ids and timestamps of that
        # length, and a rule that flagged them would be noise within a day.
        # The state code beside them is what makes it an address.
        pattern=re.compile(
            r"\b(?:"
            + "|".join((*US_STATE_NAMES_MULTIWORD, *US_STATE_NAMES, *US_STATE_ABBREVIATIONS))
            + r")\.?,?[ \t]+\d{5}(?:-\d{4})?\b"
        ),
        why="a US state beside a ZIP code, which is the tail of a postal address",
        allowed=frozenset(ALLOWED_ZIP_STATE_PAIRS),
    ),
    HashedWordRule(
        name="owner-name",
        # Alphabetic runs only. A name does not contain digits, and
        # including them would hash every identifier in the tree for
        # nothing.
        pattern=re.compile(rf"[A-Za-z]{{{MIN_WORD_LENGTH},}}"),
        why="a word matching a name this repository must never contain",
        denied=frozenset(DENIED_WORD_DIGESTS),
    ),
)


class Finding(NamedTuple):
    """One rule violation, located precisely enough to fix."""

    rule: str
    where: str
    line: int
    masked: str
    why: str

    def render(self) -> str:
        """Return the one-line report for this finding."""
        return f"  {self.rule:<11} {self.where}:{self.line}: {self.masked}  ({self.why})"


# How much of a finding is shown, and the length below which even that is
# too much. Three characters of a MAC or an address is enough for its owner
# to recognise it and nowhere near enough for a reader to reconstruct it.
MASK_PREFIX = 3
MASK_PREFIX_MIN_LENGTH = 6


def _mask(value: str) -> str:
    """Return enough of `value` to recognise it and not enough to reuse it.

    CI logs on a public repository are public and permanent. Echoing a
    leaked address in full to prove it was caught would publish it again.
    """
    keep = MASK_PREFIX if len(value) > MASK_PREFIX_MIN_LENGTH else 1
    return value[:keep] + "*" * (len(value) - keep)


def _scan(text: str, where: str) -> list[Finding]:
    """Return every finding in `text`, attributed to `where`."""
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for rule in RULES:
            findings.extend(
                Finding(rule.name, where, number, rule.mask(match), rule.why)
                for match in rule.pattern.findall(line)
                if not rule.permits(match)
            )
    return findings


def _git(*args: str) -> str:
    """Run a git command and return its stdout, or raise on failure."""
    result = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _assert_full_clone() -> None:
    """Refuse to run against a shallow clone.

    `actions/checkout` defaults to `fetch-depth: 1`. Under that default the
    history scan below would walk exactly one commit, find nothing, and
    report a green that means nothing at all -- the precise false green
    this script exists to prevent, reintroduced by a CI default. Better to
    fail loudly and have someone fix the workflow.
    """
    if _git("rev-parse", "--is-shallow-repository").strip() == "true":
        sys.stderr.write(
            "check_no_pii: this is a SHALLOW clone, so the history scan would be "
            "vacuous.\n"
            "  Set `fetch-depth: 0` on actions/checkout, or run "
            "`git fetch --unshallow` locally.\n"
        )
        raise SystemExit(2)


def working_tree_paths() -> list[str]:
    """Return every path a commit would pick up: tracked AND untracked.

    TWO `git ls-files` calls, and the second one is here because the first
    one alone was a FALSE GREEN in the exact situation this guard matters
    most.

    `ls-files -z` lists only what git already tracks. A brand-new file --
    not yet `git add`ed -- was therefore invisible, and the script printed
    "clean" over a tree containing it. Reproduced deliberately: a new file
    holding a street address exited 0 while untracked and 1 once staged.
    That is backwards. A new file is precisely where fresh personal data
    arrives, and running the guard BEFORE committing is precisely when a
    person wants an answer. "Clean" at that moment is the worst answer this
    script can give, because it is the one that gets believed.

    `--others --exclude-standard` adds the untracked files and keeps
    honouring `.gitignore`, `.git/info/exclude` and the user's global
    excludes. Dropping `--exclude-standard` would pull in `.venv`, the
    caches and every build artefact -- thousands of files, a scan that
    takes minutes, and findings nobody can act on. A guard that noisy gets
    switched off, and a switched-off guard protects nothing. The boundary
    is deliberately "what a commit would pick up", not "every byte on
    disk".

    Sorted and de-duplicated so the report reads the same way twice. A path
    cannot appear in both listings today -- tracked and untracked are
    disjoint by definition -- but the set costs nothing and does not depend
    on that staying true.
    """
    tracked = _git("ls-files", "-z").split("\0")
    untracked = _git("ls-files", "--others", "--exclude-standard", "-z").split("\0")
    return sorted({path for path in (*tracked, *untracked) if path})


def scan_working_tree() -> list[Finding]:
    """Scan every file a commit would pick up, as it exists ON DISK.

    `working_tree_paths()` chooses WHICH files to read -- which is how
    .venv, the caches and everything else ignored stays out of the scan --
    but the contents come from the filesystem, never from
    `git show :<path>`.

    That distinction is not a detail. `git show :<path>` reads the INDEX,
    so an unstaged edit is invisible to it: the first version of this
    function used it, a fixture was poisoned with a MAC and an address, and
    the guard printed "clean". A tool whose job is to catch what a human
    missed must read what the human actually has -- which is the same
    reason untracked files are read too.
    """
    findings: list[Finding] = []
    for path in working_tree_paths():
        if path == SELF_PATH:
            continue
        try:
            text = Path(path).read_bytes().decode()
        except FileNotFoundError:
            # Tracked but deleted in the checkout, or a dangling symlink.
            # History covers the first case and there is nothing to read in
            # the second.
            continue
        except IsADirectoryError:
            # A symlink to a directory, which git lists as a single entry.
            # There is no file content behind it; the files inside are
            # listed separately if they are in the repository at all.
            continue
        except UnicodeDecodeError:
            # A binary file (the brand icon). Nothing to read as text.
            continue
        findings.extend(_scan(text, path))
    return findings


def scan_history() -> list[Finding]:
    """Scan every blob reachable from any ref, at every revision.

    Uses one `git cat-file --batch` for the whole object database rather
    than a process per object: this walks every version of every file that
    has ever been committed, and a fork per blob turns a two-second check
    into a two-minute one as soon as the repository has any age.
    """
    paths: dict[str, str] = {}
    order: list[str] = []
    for line in _git("rev-list", "--objects", "--all").splitlines():
        sha, _, path = line.partition(" ")
        if path and path != SELF_PATH:
            paths[sha] = path
            order.append(sha)

    if not order:
        return []

    batch = subprocess.run(
        ["git", "cat-file", "--batch"],  # noqa: S607
        input="\n".join(order).encode(),
        capture_output=True,
        check=True,
    ).stdout

    findings: list[Finding] = []
    cursor = 0
    while cursor < len(batch):
        end = batch.index(b"\n", cursor)
        sha, kind, size_text = batch[cursor:end].decode().split()
        cursor = end + 1
        size = int(size_text)
        body = batch[cursor : cursor + size]
        # +1 for the newline git writes after each object's contents.
        cursor += size + 1
        if kind != "blob":
            continue
        try:
            text = body.decode()
        except UnicodeDecodeError:
            continue
        findings.extend(_scan(text, f"history:{paths[sha]}@{sha[:10]}"))
    return findings


def scan_commit_metadata() -> list[Finding]:
    """Scan every commit message, author and committer identity.

    A clone carries these exactly as it carries file contents, and an
    address or a path pasted into a commit message is every bit as
    published as one committed to a file.
    """
    log = _git(
        "log",
        "--all",
        "--format=%H%n%an <%ae>%n%cn <%ce>%n%B",
    )
    return [
        finding._replace(where="history:commit-metadata")
        for finding in _scan(log, "history:commit-metadata")
    ]


def main() -> int:
    """Scan the tree, the history and the commit metadata; report and exit."""
    _assert_full_clone()

    findings = scan_working_tree() + scan_history() + scan_commit_metadata()

    if not findings:
        print("check_no_pii: clean -- working tree, full history and commit metadata.")
        return 0

    # De-duplicated: one identifier committed in fifty revisions of one file
    # is one thing to fix, and fifty identical lines buries the other four.
    unique = sorted(set(findings))
    print(f"check_no_pii: {len(unique)} finding(s).\n")
    for finding in unique:
        print(finding.render())
    print(
        "\nFindings are masked on purpose: CI logs on a public repository are "
        "public.\nA `history:` finding cannot be fixed by a commit -- the value is "
        "in the\nobject database and needs a history rewrite and a force-push."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
