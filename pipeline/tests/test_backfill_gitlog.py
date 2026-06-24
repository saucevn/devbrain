"""Characterization tests for backfill's git-history parsing + URL helpers.

Builds a throwaway git repo and asserts read_commits() returns chronological
commits with correct churn, including binary files (numstat '-' → 0).
"""
import subprocess

import backfill


def _run(cwd, *args):
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True)


def _init_repo(path):
    _run(path, "git", "init", "-q")
    _run(path, "git", "config", "user.email", "t@example.com")
    _run(path, "git", "config", "user.name", "Tester")
    (path / "a.txt").write_text("a1\na2\n")
    (path / "b.txt").write_text("b1\n")
    _run(path, "git", "add", "-A")
    _run(path, "git", "commit", "-q", "-m", "c1")
    (path / "a.txt").write_text("a1\na2\na3\n")          # +1 line
    (path / "img.bin").write_bytes(bytes(range(256)))    # binary → numstat '-'
    _run(path, "git", "add", "-A")
    _run(path, "git", "commit", "-q", "-m", "c2")


def test_read_commits_parses_chronological_with_churn_and_binary(tmp_path):
    _init_repo(tmp_path)
    commits = backfill.read_commits(str(tmp_path))

    assert len(commits) == 2

    c1 = {f["filename"]: f for f in commits[0]["files"]}   # oldest first
    assert c1["a.txt"]["additions"] == 2
    assert c1["b.txt"]["additions"] == 1

    c2 = {f["filename"]: f for f in commits[1]["files"]}
    assert c2["a.txt"]["additions"] == 1
    assert c2["img.bin"]["additions"] == 0                 # binary churn → 0
    assert c2["img.bin"]["deletions"] == 0


def test_looks_like_url():
    assert backfill.looks_like_url("https://github.com/o/r.git")
    assert backfill.looks_like_url("git@github.com:o/r.git")
    assert not backfill.looks_like_url("/local/path/repo")


def test_github_base_normalizes_clone_url():
    assert backfill.github_base("/x", "https://github.com/o/r.git") == "https://github.com/o/r"
    assert backfill.github_base("/x", "git@github.com:o/r.git") == "https://github.com/o/r"
    assert backfill.github_base("/x", "https://example.com/o/r.git") is None
