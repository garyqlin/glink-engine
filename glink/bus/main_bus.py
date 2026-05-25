#!/usr/bin/env python3
"""
Main Bus — 共享项目时间线
所有 agent 通过此文件读写项目级共享记忆

存储格式：JSONL（每行一个事件）
事件类型：
  - task.created: 任务创建
  - task.assigned: 任务分派
  - task.started: 任务开始执行
  - task.completed: 任务完成
  - task.failed: 任务失败
  - task.log: 执行过程中的日志
  - project.update: 项目状态更新
"""

import fcntl
import json
import os
import sys
import threading
from datetime import datetime

from . import sanitize_project_name

# Reporter 延迟导入以避免循环依赖；仅在 write 失败时用于告警
_send_alert = None

# ---- Bus Read Cache ----
# 缓存每个 JSONL 文件的 行号→文件偏移 映射，避免 read() 每次 O(n) 全量扫描
_read_cache: dict[str, tuple[int, list[int]]] = {}
_cache_lock = threading.Lock()


def _rebuild_cache(project_name: str) -> list[int]:
    """重建文件行偏移索引，返回偏移列表"""
    path = bus_path(project_name)
    offsets = [0]
    try:
        with open(path, "rb") as f:
            while True:
                line = f.readline()
                if not line:
                    break
                offsets.append(f.tell())
        _read_cache[project_name] = (len(offsets) - 1, offsets)
        return offsets
    except FileNotFoundError:
        _read_cache[project_name] = (0, [])
        return []


def _invalidate_cache(project_name: str):
    """写入新行后使缓存失效"""
    with _cache_lock:
        _read_cache.pop(project_name, None)


def _cached_read(project_name: str, limit: int = 20, since_type: str | None = None):
    """使用行偏移索引的 O(n) read，首次全量扫描后缓存偏移"""
    path = bus_path(project_name)
    if not os.path.exists(path):
        return []

    with _cache_lock:
        cache_entry = _read_cache.get(project_name)

    offsets = None
    if cache_entry is not None:
        total_lines, offsets = cache_entry
    else:
        with _cache_lock:
            offsets = _rebuild_cache(project_name)

    if not offsets:
        return []

    # 只读取最后 limit 行附近的偏移
    start_offset = offsets[max(0, len(offsets) - limit - 5)]
    entries = []
    try:
        with open(path, encoding="utf-8") as f:
            f.seek(start_offset)
            if start_offset > 0:
                f.readline()  # 跳过可能不完整的行
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if since_type is None or entry["type"] == since_type:
                        entries.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass

    return entries[-limit:]


def _set_alert_handler(handler):
    global _send_alert
    _send_alert = handler


BUS_DIR = os.path.dirname(os.path.abspath(__file__))

# 兼容别名（从 bus/__init__.py 导入）
_sanitize_project_name = sanitize_project_name


def bus_path(project_name: str) -> str:
    """获取项目总线文件路径"""
    safe_name = _sanitize_project_name(project_name)
    projects_dir = os.path.join(BUS_DIR, "projects")
    os.makedirs(projects_dir, exist_ok=True)
    return os.path.join(projects_dir, f"{safe_name}.jsonl")


def write(project_name: str, event_type: str, agent: str, data, stage: str = ""):
    """写入一条事件到 Main Bus（带文件锁，并发安全）"""
    path = bus_path(project_name)
    entry = {
        "ts": datetime.now().isoformat(),
        "type": event_type,
        "agent": agent,
        "data": data,
        "stage": stage,
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        # 写入后使缓存失效，下次 read 重建索引
        _invalidate_cache(project_name)
    except OSError as e:
        # 写入失败不抛出，通过 Reporter 告警（有则发，无则静默）
        msg = f"[main_bus.write] IOError on {path}: {e}"
        sys.stderr.write(msg + "\n")
        if _send_alert is not None:
            _send_alert("IOError", msg, severity="error")
        return None
    return entry


def read(project_name: str, limit: int = 20, since_type: str | None = None):
    """读取 Main Bus 最近的事件（带行偏移缓存，首次 O(n)，后续 O(limit)）"""
    if limit <= 0:
        limit = 20
    return _cached_read(project_name, limit=limit, since_type=since_type)


def latest(project_name: str, event_type: str | None = None, agent: str | None = None):
    """获取最新的一条事件"""
    entries = read(project_name, limit=100)
    for e in reversed(entries):
        if event_type and e["type"] != event_type:
            continue
        if agent and e["agent"] != agent:
            continue
        return e
    return None


def status(project_name: str) -> dict:
    """获取项目当前状态总结"""
    entries = read(project_name, limit=1000)

    tasks = [e for e in entries if e["type"].startswith("task.")]
    completed = [t for t in tasks if t["type"] == "task.completed"]
    failed = [t for t in tasks if t["type"] == "task.failed"]
    started = [t for t in tasks if t["type"] == "task.started"]

    return {
        "project": project_name,
        "total_events": len(entries),
        "tasks_created": len([t for t in tasks if t["type"] == "task.created"]),
        "tasks_started": len(started),
        "tasks_completed": len(completed),
        "tasks_failed": len(failed),
        "agents_involved": list(set(e["agent"] for e in entries)),
        "stages": list(set(e.get("stage", "") for e in entries if e.get("stage"))),
    }


def cli() -> None:
    """命令行入口"""
    if len(sys.argv) < 2:
        print("用法: python3 main-bus.py <命令> [参数...]")
        print("命令: write, read, status, latest")
        sys.exit(1)

    cmd = sys.argv[1]
    project = os.environ.get("GLINK_PROJECT", "testglink")

    if cmd == "write":
        if len(sys.argv) < 5:
            print("用法: write <event_type> <agent> <data_json>")
            sys.exit(1)
        entry = write(project, sys.argv[2], sys.argv[3], json.loads(sys.argv[4]))
        print(json.dumps(entry, ensure_ascii=False))

    elif cmd == "read":
        entries = read(project, limit=int(sys.argv[2]) if len(sys.argv) > 2 else 20)
        for e in entries:
            print(
                f"[{e['ts'][:19]}] {e['type']:20s} | {e['agent']:10s} | {json.dumps(e['data'], ensure_ascii=False)[:80]}"
            )

    elif cmd == "status":
        s = status(project)
        print(f"项目: {s['project']}")
        print(f"总事件: {s['total_events']}")
        print(
            f"任务: 创建{s['tasks_created']} / 开始{s['tasks_started']} / 完成{s['tasks_completed']} / 失败{s['tasks_failed']}"
        )
        print(f"参与Agent: {', '.join(s['agents_involved'])}")
        print(f"阶段: {', '.join(s['stages'])}")

    elif cmd == "latest":
        entry = latest(
            project,
            event_type=sys.argv[2] if len(sys.argv) > 2 else None,
            agent=sys.argv[3] if len(sys.argv) > 3 else None,
        )
        if entry:
            print(json.dumps(entry, ensure_ascii=False))
        else:
            print("null")


if __name__ == "__main__":
    cli()
