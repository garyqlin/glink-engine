#!/usr/bin/env python3
"""
agent_client — Glink 共享的 Agent 通讯与工作流加载模块

由 glink.py（一次性调度引擎）和 glink-daemon.py（带断点续跑的守护进程）共享。

导出：
- AGENT_PORTS:  Agent 名称 → HTTP 端口的统一映射
- call_agent(): HTTP 调用 Agent 的 /ask 接口
- load_workflow(): 从 workflows/ 或 bus/projects/ 加载 yaml 工作流
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

import yaml

# ── Agent 端口映射（唯一真源）────────────────────────────
# 同一端口可有多个别名（如 标准版/扎古、代码臂/Forge/forge）
AGENT_PORTS: dict[str, int] = {
    "标准版": 8420,
    "扎古": 8420,
    "重锤": 8431,
    "绘墨": 8432,
    "大黄蜂": 8434,
    "Laser": 8435,
    "代码臂": 8436,
    "Forge": 8436,
    "forge": 8436,
}

DEFAULT_AGENT_PORT = 8420
DEFAULT_TIMEOUT = 600

# ── 项目名白名单（防 path traversal，从 bus/__init__.py 导入）──
from . import sanitize_project_name

_sanitize_project_name = sanitize_project_name  # 兼容别名


# ── HTTP 调用 Agent ─────────────────────────────────────
def call_agent(
    agent: str,
    task: str,
    port: int | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    parse_reply: bool = True,
) -> dict[str, Any]:
    """HTTP 调用 agent 的 /ask 接口。

    Args:
        agent:        Agent 名称（如 "重锤"、"Forge"）
        task:         发送给 Agent 的任务描述
        port:         显式端口；不传则查 AGENT_PORTS
        timeout:      请求超时秒数
        parse_reply:  True=尝试解析 JSON 取 reply 字段；False=直接返回原始响应

    Returns:
        {"status": "ok",     "output": "<reply 或原始响应前500字>"}
        {"status": "failed", "error":  "<错误描述>"}
    """
    if port is None:
        port = AGENT_PORTS.get(agent, DEFAULT_AGENT_PORT)

    url = f"http://127.0.0.1:{port}/ask"
    payload = json.dumps({"message": task, "session": True}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})

    max_response_size = 1 * 1024 * 1024  # 1 MB
    chunk_size = 64 * 1024  # 64 KB

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            chunks = []
            total = 0
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_response_size:
                    max_read = max_response_size - (total - len(chunk))
                    if max_read > 0:
                        chunks.append(chunk[:max_read])
                    while resp.read(chunk_size):
                        pass
                    body = b"".join(chunks).decode()
                    body = body[:max_response_size] + "\n\n[TRUNCATED: Response exceeded 1MB limit]"
                    break
                chunks.append(chunk)
            else:
                body = b"".join(chunks).decode()

            if parse_reply:
                try:
                    output = json.loads(body).get("reply", body[:500])
                except json.JSONDecodeError:
                    output = body[:500]
            else:
                output = body[:500]
            return {"status": "ok", "output": output}
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        return {"status": "failed", "error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"status": "failed", "error": str(e)}


# ── 工作流加载 ───────────────────────────────────────────
def load_workflow(project_name: str, base_dir: str | None = None) -> dict[str, Any]:
    """加载工作流 YAML，先查 workflows/，再查 bus/projects/。

    Args:
        project_name: 项目名（会被白名单过滤）
        base_dir:     Glink 根目录；不传则用本文件所在目录的父级

    Returns:
        解析后的工作流字典

    Raises:
        SystemExit(1): 找不到工作流文件
    """
    if base_dir is None:
        # 本文件位于 <glink>/bus/agent_client.py，父级 = glink 根
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    workflows_dir = os.path.join(base_dir, "workflows")
    bus_projects_dir = os.path.join(base_dir, "bus", "projects")

    safe_name = _sanitize_project_name(project_name)
    candidates = [
        os.path.join(workflows_dir, f"{safe_name}.yaml"),
        os.path.join(bus_projects_dir, f"{safe_name}.yaml"),
    ]

    for path in candidates:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return yaml.safe_load(f)

    print(f"❌ 找不到工作流: {safe_name}", file=sys.stderr)
    sys.exit(1)
