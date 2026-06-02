# SPDX-License-Identifier: MIT
"""项目级共享记忆 — Glink Project Memory 后端

每个项目在 .glk/<project_id>/ 目录下存储：
  - context.md    项目上下文（LLM 可读）
  - events.jsonl  操作流水
  - progress.json 进展快照
  - materials/    资料文件
"""

import json
import os
import re
import time

# ── 项目存储根目录 ──────────────────────────────────
GLK_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".glk")


def _glk_path(project_id: str) -> str:
    return os.path.join(GLK_ROOT, sanitize(project_id))


def sanitize(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:64]


def project_init(project_id: str, context: str = "") -> dict:
    """创建立项：初始化 .glk/<project_id>/ 目录结构"""
    pdir = _glk_path(project_id)
    os.makedirs(pdir, exist_ok=True)
    os.makedirs(os.path.join(pdir, "materials"), exist_ok=True)

    ctx_path = os.path.join(pdir, "context.md")
    if not os.path.exists(ctx_path) and context:
        with open(ctx_path, "w") as f:
            f.write(context)

    prog_path = os.path.join(pdir, "progress.json")
    if not os.path.exists(prog_path):
        with open(prog_path, "w") as f:
            json.dump({"project_id": project_id, "created_at": time.time(), "status": "active", "steps": [], "current_step": None}, f)

    ev_path = os.path.join(pdir, "events.jsonl")
    if not os.path.exists(ev_path):
        open(ev_path, "w").close()

    return {"status": "ok", "project_id": project_id, "path": pdir}


def project_read_context(project_id: str) -> str | None:
    """读取项目 context.md"""
    ctx_path = os.path.join(_glk_path(project_id), "context.md")
    if os.path.exists(ctx_path):
        with open(ctx_path) as f:
            return f.read()
    return None


def project_update_context(project_id: str, context: str = "", event_type: str = "", agent: str = "", detail: str = ""):
    """更新项目：追加事件 + 可选更新 context"""
    pdir = _glk_path(project_id)
    os.makedirs(pdir, exist_ok=True)

    if event_type and agent:
        ev_path = os.path.join(pdir, "events.jsonl")
        ev = {"ts": time.time(), "type": event_type, "agent": agent, "detail": detail}
        with open(ev_path, "a") as f:
            f.write(json.dumps(ev) + "\n")

    if context:
        with open(os.path.join(pdir, "context.md"), "w") as f:
            f.write(context)

    return {"status": "ok", "project_id": project_id}


def project_list() -> list[dict]:
    """列出所有项目"""
    os.makedirs(GLK_ROOT, exist_ok=True)
    projects = []
    for name in sorted(os.listdir(GLK_ROOT)):
        pdir = os.path.join(GLK_ROOT, name)
        if not os.path.isdir(pdir):
            continue
        prog_path = os.path.join(pdir, "progress.json")
        prog = {}
        if os.path.exists(prog_path):
            with open(prog_path) as f:
                try:
                    prog = json.load(f)
                except json.JSONDecodeError:
                    prog = {"status": "corrupted"}
        projects.append({"project_id": name, "path": pdir, "progress": prog})
    return projects


def project_get(project_id: str) -> dict | None:
    """获取单个项目详情"""
    pdir = _glk_path(project_id)
    if not os.path.exists(pdir):
        return None

    prog_path = os.path.join(pdir, "progress.json")
    prog = {}
    if os.path.exists(prog_path):
        with open(prog_path) as f:
            try:
                prog = json.load(f)
            except json.JSONDecodeError:
                pass

    ev_path = os.path.join(pdir, "events.jsonl")
    last_event = None
    if os.path.exists(ev_path):
        with open(ev_path) as f:
            lines = f.readlines()
        if lines:
            try:
                last_event = json.loads(lines[-1])
            except json.JSONDecodeError:
                pass

    context = project_read_context(project_id)
    summary = context[:200] + "..." if context and len(context) > 200 else context

    return {"project_id": project_id, "progress": prog, "last_event": last_event, "context_summary": summary}
