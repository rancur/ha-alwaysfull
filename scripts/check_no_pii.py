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

Five rules, each with an allowlist and each allowlist entry carrying a
reason. An allowlist without reasons decays into "whatever was failing the
day someone was in a hurry".

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
    Rule(
        name="device-id",
        # A bare twelve-hex run. The second lookbehind excludes the
        # fractional part of a decimal -- `119.531736111111` is a sensor
        # state in a snapshot, not a MAC -- while still catching a real id
        # that happens to follow a dot. `\b` is NOT used: it treats the
        # boundary between hex and non-hex characters inconsistently at the
        # edges of a longer identifier.
        pattern=re.compile(r"(?<![0-9a-fA-F])(?<!\d\.)[0-9a-fA-F]{12}(?![0-9a-fA-F])"),
        why="a bare twelve-hex run, which is the shape of this device's MAC address",
        allowed=frozenset({*SYNTHETIC_DEVICE_IDS, DERIVED_ACCOUNT_KEY}),
        fold_case=True,
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
                Finding(rule.name, where, number, _mask(match), rule.why)
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


def scan_working_tree() -> list[Finding]:
    """Scan every tracked file as it exists ON DISK.

    `git ls-files` chooses WHICH files to read -- which is how .venv, the
    caches and everything else ignored stays out of the scan -- but the
    contents come from the filesystem, never from `git show :<path>`.

    That distinction is not a detail. `git show :<path>` reads the INDEX,
    so an unstaged edit is invisible to it: the first version of this
    function used it, a fixture was poisoned with a MAC and an address, and
    the guard printed "clean". A tool whose job is to catch what a human
    missed must read what the human actually has.
    """
    findings: list[Finding] = []
    for path in filter(None, _git("ls-files", "-z").split("\0")):
        if path == SELF_PATH:
            continue
        try:
            text = Path(path).read_bytes().decode()
        except FileNotFoundError:
            # Tracked but deleted in the checkout. History covers it.
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
