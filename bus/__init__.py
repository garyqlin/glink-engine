"""Glink bus — shared utilities."""

import os
import re
from pathlib import Path

_PROJECT_NAME_CLEAN = re.compile(r"[^\w\-]")


def sanitize_project_name(name: str) -> str:
    """Remove all non-word, non-hyphen characters from project name."""
    return _PROJECT_NAME_CLEAN.sub("", name)


def safe_project_path(base_dir: str | Path, user_path: str) -> str:
    """Resolve a user-provided path and enforce it stays under base_dir/projects/."""
    projects_dir = os.path.join(str(base_dir), "projects")
    # P0: 防御绝对路径绕过（os.path.join 忽略绝对路径第一个参数）
    safe_path = user_path.lstrip("/") if user_path.startswith("/") else user_path
    resolved = os.path.realpath(os.path.join(projects_dir, os.path.normpath(safe_path)))
    projects_real = os.path.realpath(projects_dir)
    if not resolved.startswith(projects_real + os.sep) and resolved != projects_real:
        raise ValueError(
            f"path traversal denied: {user_path!r} resolved to {resolved!r}, which is outside {projects_real!r}"
        )
    return resolved
