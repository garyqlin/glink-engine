"""Shared fixtures for Glink tests."""

import os
import sys

import pytest

GLINK_DIR = os.path.dirname(os.path.dirname(__file__))
if GLINK_DIR not in sys.path:
    sys.path.insert(0, GLINK_DIR)


@pytest.fixture
def tmp_project():
    import uuid

    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def sample_workflow_serial():
    return {
        "project": "test-flow",
        "version": "0.1",
        "mode": "serial",
        "steps": [
            {"id": "step-1", "executor": "hammer", "title": "Setup", "task": "Initialize"},
            {"id": "step-2", "executor": "painter", "title": "Build UI", "task": "Create UI"},
            {"id": "step-3", "executor": "laser", "title": "Test", "task": "Run tests"},
        ],
    }


@pytest.fixture
def sample_workflow_parallel():
    return {
        "project": "test-parallel",
        "version": "0.1",
        "mode": "parallel",
        "steps": [
            {"id": "step-1", "executor": "hammer", "title": "Setup", "task": "Init"},
            {"id": "step-2a", "title": "Build A", "task": "A", "depends_on": ["step-1"]},
            {"id": "step-2b", "title": "Build B", "task": "B", "depends_on": ["step-1"]},
            {"id": "step-3", "title": "Merge", "task": "Merge", "depends_on": ["step-2a", "step-2b"]},
        ],
    }
