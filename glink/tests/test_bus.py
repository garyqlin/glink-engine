"""Tests for Main Bus (read/write/cache)."""

import os
import time

from bus import main_bus as bus


def _patch_bus_dir(monkeypatch, tmp_path):
    new_bus = str(tmp_path / "bus-projects")
    os.makedirs(new_bus, exist_ok=True)
    monkeypatch.setattr(bus, "BUS_DIR", new_bus)
    monkeypatch.setattr(bus, "_read_cache", {})  # clear cache


class TestBusWriteRead:
    def test_write_and_read(self, monkeypatch, tmp_path, tmp_project):
        _patch_bus_dir(monkeypatch, tmp_path)
        entry = bus.write(tmp_project, "task.created", "test", {"msg": "hello"})
        assert entry is not None
        assert entry["type"] == "task.created"
        entries = bus.read(tmp_project, limit=10)
        assert len(entries) == 1
        assert entries[0]["data"]["msg"] == "hello"

    def test_read_empty(self, monkeypatch, tmp_path, tmp_project):
        _patch_bus_dir(monkeypatch, tmp_path)
        assert bus.read(tmp_project, limit=10) == []

    def test_read_limit(self, monkeypatch, tmp_path, tmp_project):
        _patch_bus_dir(monkeypatch, tmp_path)
        for i in range(10):
            bus.write(tmp_project, "task.log", "test", {"idx": i})
        entries = bus.read(tmp_project, limit=3)
        assert len(entries) == 3
        assert entries[0]["data"]["idx"] == 7

    def test_since_type(self, monkeypatch, tmp_path, tmp_project):
        _patch_bus_dir(monkeypatch, tmp_path)
        bus.write(tmp_project, "task.created", "test", {})
        bus.write(tmp_project, "task.started", "test", {})
        bus.write(tmp_project, "task.completed", "test", {})
        started = bus.read(tmp_project, limit=10, since_type="task.started")
        assert len(started) == 1
        assert started[0]["type"] == "task.started"

    def test_latest(self, monkeypatch, tmp_path, tmp_project):
        _patch_bus_dir(monkeypatch, tmp_path)
        bus.write(tmp_project, "task.created", "a", {"idx": 1})
        time.sleep(0.01)
        bus.write(tmp_project, "task.created", "b", {"idx": 2})
        latest = bus.latest(tmp_project, event_type="task.created")
        assert latest["agent"] == "b"

    def test_status(self, monkeypatch, tmp_path, tmp_project):
        _patch_bus_dir(monkeypatch, tmp_path)
        bus.write(tmp_project, "task.created", "a", {})
        bus.write(tmp_project, "task.started", "a", {})
        bus.write(tmp_project, "task.completed", "a", {})
        s = bus.status(tmp_project)
        assert s["tasks_created"] == 1
        assert s["tasks_completed"] == 1
        assert s["tasks_failed"] == 0


class TestBusCache:
    def test_cache_hit(self, monkeypatch, tmp_path, tmp_project):
        _patch_bus_dir(monkeypatch, tmp_path)
        for i in range(5):
            bus.write(tmp_project, "task.log", "t", {"i": i})
        bus.read(tmp_project, limit=100)
        assert tmp_project in bus._read_cache
        assert bus._read_cache[tmp_project][0] == 5

    def test_cache_invalidated_on_write(self, monkeypatch, tmp_path, tmp_project):
        _patch_bus_dir(monkeypatch, tmp_path)
        bus.write(tmp_project, "task.created", "t", {})
        bus.read(tmp_project, limit=10)
        assert tmp_project in bus._read_cache
        bus.write(tmp_project, "task.log", "t", {})
        assert tmp_project not in bus._read_cache

    def test_concurrent_write(self, monkeypatch, tmp_path, tmp_project):
        _patch_bus_dir(monkeypatch, tmp_path)

        def writer(n):
            for i in range(20):
                bus.write(tmp_project, "task.log", "w", {"n": n, "i": i})

        import threading

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        entries = bus.read(tmp_project, limit=100)
        assert len(entries) == 60
