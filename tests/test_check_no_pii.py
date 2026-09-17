"""The PII guard is itself guarded, because it has failed open twice.

`scripts/check_no_pii.py` is the only thing standing between a moment's
inattention and a public repository holding somebody's address. It has now
twice reported "clean" over a tree that was not:

- reading `git show :<path>` (the INDEX) instead of the filesystem, so an
  unstaged edit was invisible;
- listing only TRACKED files, so a brand-new file was invisible until it
  was `git add`ed -- which is the exact moment a person runs the guard, and
  the exact place fresh personal data arrives.

Both were found by hand. Neither would have survived this module.

Every test here drives the real script, as a subprocess, against a real
throwaway git repository, and asserts on its EXIT CODE -- not on whether
some string appears in its output. A guard that is only ever observed
passing is not a verified guard, so each case has a matching opposite: the
address tests are paired with a clean-tree test, and the "ignored files are
skipped" test is paired with one proving the same bytes ARE caught when the
file is not ignored. Without the pair, an exit code of 0 could mean the
scan worked or that it read nothing at all.

The addresses below are invented. `1 test street` -- the one the
diagnostics test uses -- is on the script's own allowlist and would prove
nothing here.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
GUARD = "scripts/check_no_pii.py"

# Invented, and ASSEMBLED FROM PIECES so the address-shaped string never
# appears on any line of any file.
#
# Written out in full, it is a finding in THIS file: the guard scans the
# whole repository, including its own test data, and it was correct to flag
# it -- proved by doing it, which is how this comment came to exist.
#
# Allowlisting it would be worse than useless. The allowlist is keyed on
# the address itself, so an entry here would make the guard skip this
# string everywhere, and every test below -- each of which depends on this
# address being CAUGHT -- would pass without the guard doing anything. That
# is the shape of a test that guards nothing.
#
# Deliberately not the allowlisted `1 test street`, for the same reason.
_HOUSE_NUMBER = "742"
_STREET_NAME = "Evergreen"
_STREET_TYPE = "Boulevard"
FAKE_ADDRESS = f"{_HOUSE_NUMBER} {_STREET_NAME} {_STREET_TYPE}"

# The prose shape that produced a FALSE POSITIVE in this repository's own
# documentation: a thousands separator read as a house number, `on the` as
# a street name and `way` as a street type. Kept verbatim so the fix cannot
# be "the sentence was reworded".
THOUSANDS_SEPARATOR_PROSE = "Dividing it by 2,592,000 on the way out turns a one-week warning into 0."


def _git(repo: Path, *args: str) -> None:
    """Run one git command in `repo`, failing the test if it fails."""
    subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def guard_repo(tmp_path: Path) -> Path:
    """Return a throwaway git repository with the real guard script in it.

    A real repository, not a mock: the script's whole job is to ask git
    what a commit would pick up, so a fake git would be testing the fake.

    It starts with one commit, because the history and commit-metadata
    scans run `git log --all` and `git rev-list --all`, and a repository
    with no commits at all is a shape this script never meets in practice.

    The identity is synthetic and the email is the script's own canonical
    documentation address, so the commit metadata it scans is clean.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(REPO_ROOT / GUARD, repo / GUARD)
    (repo / ".gitignore").write_text("ignored/\n")

    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "user@example.com")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "Add the guard")
    return repo


def run_guard(repo: Path) -> subprocess.CompletedProcess[str]:
    """Run the guard in `repo` and return the completed process.

    `check=False`: a non-zero exit is the thing under test, not an error.
    """
    return subprocess.run(  # noqa: S603
        [sys.executable, GUARD],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_repository_holding_only_the_guard_is_clean(guard_repo: Path) -> None:
    """The baseline, without which every other exit code here is ambiguous.

    If this failed, a `1` below could mean the guard caught the planted
    address or that it objects to the fixture itself, and a `0` could mean
    the scan is working or that it never read anything.
    """
    result = run_guard(guard_repo)
    assert result.returncode == 0, result.stdout


def test_an_untracked_file_is_scanned(guard_repo: Path) -> None:
    """A file a commit would pick up is scanned before it is ever added.

    THE REGRESSION THIS MODULE EXISTS FOR. Before the fix this exited 0 --
    the guard listed tracked files only -- and exited 1 the moment the same
    file was staged. A person who runs the guard before committing got the
    reassuring answer, which is the worst possible time to be wrong.
    """
    (guard_repo / "notes.md").write_text(f"Ship it to {FAKE_ADDRESS}.\n")

    result = run_guard(guard_repo)

    assert result.returncode == 1, result.stdout
    assert "notes.md" in result.stdout
    assert "street-address" in result.stdout


def test_staging_the_same_file_changes_nothing(guard_repo: Path) -> None:
    """Staging is not the boundary: tracked or not, the answer is the same.

    The old behaviour made `git add` change the verdict on identical bytes.
    That is the property being removed, so it is asserted directly rather
    than left implied by the test above.
    """
    (guard_repo / "notes.md").write_text(f"Ship it to {FAKE_ADDRESS}.\n")
    _git(guard_repo, "add", "notes.md")

    result = run_guard(guard_repo)

    assert result.returncode == 1, result.stdout
    assert "notes.md" in result.stdout


def test_an_ignored_file_is_not_scanned(guard_repo: Path) -> None:
    """`.gitignore` is still honoured, with the same bytes as the test above.

    This is the half that keeps the guard usable. Scanning ignored paths
    means scanning `.venv` and every cache -- minutes of runtime and
    findings nobody can act on -- and a guard that noisy gets switched off.
    The boundary is "what a commit would pick up", and an ignored file is
    not that.

    The content is IDENTICAL to the caught cases, so a pass here can only
    mean the path was skipped, never that the address was unrecognisable.
    """
    ignored = guard_repo / "ignored"
    ignored.mkdir()
    (ignored / "notes.md").write_text(f"Ship it to {FAKE_ADDRESS}.\n")

    result = run_guard(guard_repo)

    assert result.returncode == 0, result.stdout


def test_a_thousands_separator_is_not_a_house_number(guard_repo: Path) -> None:
    """The false positive that fired on this repository's own documentation.

    `2,592,000 on the way out` was read as house number `000`, street name
    `on the`, street type `way`. The sentence is reproduced verbatim so
    that rewording the docs cannot make this pass; the fix has to be in the
    rule.
    """
    (guard_repo / "doc.md").write_text(f"{THOUSANDS_SEPARATOR_PROSE}\n")

    result = run_guard(guard_repo)

    assert result.returncode == 0, result.stdout


def test_the_thousands_separator_fix_did_not_blind_the_rule(guard_repo: Path) -> None:
    """An address written after a comma is still caught.

    The narrowing added for the case above rejects a house number glued to
    the tail of a comma-grouped number. An address that merely FOLLOWS a
    comma has a space in front of it and must still be found -- otherwise
    the fix for a false positive quietly bought a false negative, which is
    the trade that matters here.
    """
    (guard_repo / "doc.md").write_text(f"Suite 5, {FAKE_ADDRESS}\n")

    result = run_guard(guard_repo)

    assert result.returncode == 1, result.stdout
    assert "street-address" in result.stdout
