"""Tests for the watcher's auto-PR git handling.

``_create_docs_pr`` stashes the user's uncommitted work before switching
branches.  Every exit path must put that work back — a failed push is the
common case (no remote, no credentials) and must not silently leave the
user's changes sitting in the stash.
"""

from __future__ import annotations

import subprocess

import pytest

from opendocs.core import watcher
from opendocs.core.watcher import _create_docs_pr

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git is not available",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A git repo with one commit, on branch 'main', with no remote."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "test@example.com")
    _git(r, "config", "user.name", "Test")
    _git(r, "config", "commit.gpgsign", "false")
    (r / "README.md").write_text("# Original\n", encoding="utf-8")
    _git(r, "add", ".")
    _git(r, "commit", "-m", "initial")
    return r


def _stash_depth(repo) -> int:
    out = _git(repo, "stash", "list").stdout.strip()
    return len(out.splitlines()) if out else 0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAutoPrRestoresWorkingTree:
    def test_uncommitted_work_survives_a_failed_push(self, repo):
        """Regression: the push failure path returned without popping the stash."""
        (repo / "README.md").write_text("# Work in progress\n", encoding="utf-8")
        output_dir = repo / "output"
        output_dir.mkdir()
        (output_dir / "doc.md").write_text("generated\n", encoding="utf-8")

        # No remote is configured, so the push fails.
        ok = _create_docs_pr(repo, output_dir, branch_name="docs/auto-update")

        assert ok is False
        assert _stash_depth(repo) == 0, "user's changes were left in the stash"
        assert (repo / "README.md").read_text(encoding="utf-8") == "# Work in progress\n"

    def test_returns_to_the_original_branch(self, repo):
        (repo / "README.md").write_text("# Edited\n", encoding="utf-8")
        output_dir = repo / "output"
        output_dir.mkdir()
        (output_dir / "doc.md").write_text("generated\n", encoding="utf-8")

        _create_docs_pr(repo, output_dir, branch_name="docs/auto-update")

        current = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert current == "main"

    def test_nothing_to_commit_still_restores(self, repo):
        """With no generated output there is nothing to commit — still restore."""
        (repo / "README.md").write_text("# Edited\n", encoding="utf-8")
        output_dir = repo / "output"
        output_dir.mkdir()

        _create_docs_pr(repo, output_dir, branch_name="docs/auto-update")

        assert _stash_depth(repo) == 0
        assert (repo / "README.md").read_text(encoding="utf-8") == "# Edited\n"

    def test_clean_tree_does_not_pop_an_unrelated_stash(self, repo):
        """With nothing to stash we must not pop a stash the user created."""
        (repo / "README.md").write_text("# User's own stash\n", encoding="utf-8")
        _git(repo, "stash", "--include-untracked")
        assert _stash_depth(repo) == 1

        output_dir = repo / "output"
        output_dir.mkdir()
        (output_dir / "doc.md").write_text("generated\n", encoding="utf-8")

        _create_docs_pr(repo, output_dir, branch_name="docs/auto-update")

        assert _stash_depth(repo) == 1, "popped a stash we did not create"

    def test_restores_when_an_exception_escapes(self, repo, monkeypatch):
        (repo / "README.md").write_text("# Work in progress\n", encoding="utf-8")
        output_dir = repo / "output"
        output_dir.mkdir()
        (output_dir / "doc.md").write_text("generated\n", encoding="utf-8")

        real_git_run = watcher._git_run
        calls = {"n": 0}

        def exploding_git_run(repo_dir, *args):
            # Let the stash through, then fail on the next git call.
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")
            return real_git_run(repo_dir, *args)

        monkeypatch.setattr(watcher, "_git_run", exploding_git_run)

        ok = _create_docs_pr(repo, output_dir, branch_name="docs/auto-update")

        assert ok is False
        monkeypatch.undo()
        assert _stash_depth(repo) == 0
        assert (repo / "README.md").read_text(encoding="utf-8") == "# Work in progress\n"
