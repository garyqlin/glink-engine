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

import http.client
import json
import os
import sys
import time
from typing import Any

import yaml

# ── Agent 端口映射（唯一真源）────────────────────────────
# 从环境变量 GLINK_AGENT_PORTS 加载（JSON 格式），未设置时使用通用默认值。
# 也可在 glink 根目录放 glink.local.yaml（被 .gitignore 排除），即可本地覆盖。
# 同一端口可有多个别名（如 "agent-alpha": 9001, "agent-a": 9001）

_GLINK_LOCAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "glink.local.yaml")


def _load_agent_ports() -> dict[str, int]:
    # 优先级 1：环境变量
    env = os.environ.get("GLINK_AGENT_PORTS", "")
    if env:
        try:
            return json.loads(env)
        except (json.JSONDecodeError, TypeError):
            pass

    # 优先级 2：本地配置文件（glink.local.yaml，被 gitignore）
    if os.path.exists(_GLINK_LOCAL):
        try:
            with open(_GLINK_LOCAL) as f:
                local_cfg = yaml.safe_load(f) or {}
                if "agent_ports" in local_cfg:
                    return local_cfg["agent_ports"]
        except Exception:
            pass

    # 优先级 3：通用默认值（供国际版演示/开发使用）
    return {
        "default": 8000,
    }


AGENT_PORTS: dict[str, int] = _load_agent_ports()
DEFAULT_AGENT_PORT = 8000
DEFAULT_TIMEOUT = 3600

# ── 项目名白名单（防 path traversal，从 bus/__init__.py 导入）──
import contextlib

from . import sanitize_project_name

_sanitize_project_name = sanitize_project_name  # 兼容别名


# ── HTTP 连接池（按 port 复用 TCP） ────────────────────
_agent_conn_pool: dict[int, http.client.HTTPConnection] = {}
_agent_conn_last: dict[int, float] = {}


def _get_agent_conn(port: int, timeout: int) -> http.client.HTTPConnection:
    now = time.time()
    conn = _agent_conn_pool.get(port)
    if conn is not None:
        last = _agent_conn_last.get(port, 0)
        # 超过 30 秒复用重建
        if now - last > 30:
            with contextlib.suppress(Exception):
                conn.close()
        else:
            _agent_conn_last[port] = now
            return conn
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    _agent_conn_pool[port] = conn
    _agent_conn_last[port] = now
    return conn


def _close_agent_conn(port: int) -> None:
    conn = _agent_conn_pool.pop(port, None)
    if conn:
        with contextlib.suppress(Exception):
            conn.close()


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
        agent:        Agent 名称（如 "agent-a"、"agent-b"）
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

    payload = json.dumps({"message": task, "session": True}).encode()
    headers = {"Content-Type": "application/json", "Connection": "keep-alive"}

    1 * 1024 * 1024  # 1 MB

    def _do_request() -> dict:
        conn = _get_agent_conn(port, timeout)
        try:
            conn.request("POST", "/ask", body=payload, headers=headers)
            resp = conn.getresponse()
            body_b = resp.read()
            if parse_reply:
                try:
                    output = json.loads(body_b).get("reply", body_b[:500].decode(errors="replace"))
                except json.JSONDecodeError:
                    output = body_b[:500].decode(errors="replace")
            else:
                output = body_b[:500].decode(errors="replace")
            return {"status": "ok", "output": output}
        except (
            TimeoutError,
            http.client.HTTPException,
            BrokenPipeError,
            ConnectionResetError,
            ConnectionRefusedError,
        ) as e:
            _close_agent_conn(port)
            raise e

    try:
        return _do_request()
    except Exception as e:
        err_msg = str(e)
        if any(
            kw in err_msg
            for kw in ("Remote end closed", "Connection refused", "Connection reset", "Broken pipe", "timeout")
        ):
            import time as _time

            _time.sleep(1)
            try:
                result = _do_request()
                result["retried"] = True
                return result
            except Exception:
                pass
        return {"status": "failed", "error": err_msg}


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
