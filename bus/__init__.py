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
    resolved = os.path.realpath(os.path.join(projects_dir, os.path.normpath(user_path)))
    projects_real = os.path.realpath(projects_dir)
    if not resolved.startswith(projects_real + os.sep) and resolved != projects_real:
        raise ValueError(
            f"path traversal denied: {user_path!r} resolved to {resolved!r}, which is outside {projects_real!r}"
        )
    return resolved
