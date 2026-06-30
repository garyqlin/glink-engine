# SPDX-License-Identifier: MIT
"""Glink Daemon — 工作流编排核心：运行、检查点、步骤执行"""

import concurrent.futures
import contextlib
import fcntl
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

from .checks import BUS_DIR, CHECKPOINT_FILE

# 状态门禁（可选）
try:
    from state.glink_hook import StateGateHook
except ImportError:
    StateGateHook = None
from .log import (
    get_reporter,
    log,
    log_err,
    log_ok,
    log_retry,
    log_step,
    log_warn,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "bus"))

from bus import main_bus


# ── Bus 写入安全包装（P0-A: 检查返回值，写入失败时让 step 失败）──
def _bus_write(project_name: str, event_type: str, agent: str, data, stage: str = "") -> bool:
    """安全包装 _bus_write()，失败时记录日志并返回 False"""
    result = main_bus.write(project_name, event_type, agent, data, stage)
    if result is None:
        log_err(f"[P0-A] Bus 写入失败: {event_type} @ {stage} (project={project_name}, agent={agent})")
        return False
    return True


from bus.agent_client import AGENT_PORTS, DEFAULT_AGENT_PORT
from bus.agent_client import call_agent as _call_agent
from bus.agent_client import load_workflow as _load_workflow

from bus import sanitize_project_name as _sanitize

from .config import get_max_concurrent_steps, get_max_retries, get_poll_interval, get_poll_max_wait

MAX_RETRIES = get_max_retries()
POLL_INTERVAL = get_poll_interval()
POLL_MAX_WAIT = get_poll_max_wait()


# ── Path traversal safety ──────────────────────────
PROJECTS_DIR = os.path.join(BASE_DIR, "projects")


def _safe_project_path(file_path: str) -> str:
    """Resolve a file path and ensure it stays within PROJECTS_DIR.
    Raises ValueError if the resolved path escapes."""
    if not file_path:
        return ""
    resolved = os.path.realpath(os.path.normpath(os.path.join(PROJECTS_DIR, file_path)))
    projects_real = os.path.realpath(PROJECTS_DIR)
    if not resolved.startswith(projects_real + os.sep) and resolved != projects_real:
        raise ValueError(
            f"Path traversal blocked: {file_path!r} resolves to {resolved!r}, "
            f"which is outside projects directory {projects_real!r}"
        )
    return resolved


def _extract_key_patterns(content: str) -> list[str]:
    """从代码中提取关键函数/类名，用于验证迭代是否保留了已有结构"""
    patterns = []
    # 匹配函数定义 (function name, def name)
    funcs = re.findall(r'(?:function\s+|def\s+)([a-zA-Z_][a-zA-Z0-9_]*)', content)
    patterns.extend(funcs)
    # 匹配类定义
    classes = re.findall(r'(?:class\s+)([a-zA-Z_][a-zA-Z0-9_]*)', content)
    patterns.extend(classes)
    # 匹配 const/let/var 顶层赋值（全局变量）
    globals = re.findall(r'^(?:const|let|var)\s+([a-zA-Z_][a-zA-Z0-9_]*)', content, re.MULTILINE)
    patterns.extend(globals)
    return patterns


def _verify_iteration(output: str, key_patterns: list[str], input_path: str) -> tuple[bool, list[str]]:
    """验证输出是否确实是迭代（保留关键结构）而不是重写"""
    missing = []
    for pat in key_patterns:
        if pat not in output:
            missing.append(pat)
    if len(key_patterns) == 0:
        return True, []
    ratio = 1.0 - (len(missing) / len(key_patterns))
    threshold = 0.6  # 至少保留 60% 的关键结构
    if ratio < threshold:
        return False, [f"仅保留了 {ratio:.0%} 的关键结构 ({len(missing)}/{len(key_patterns)} 丢失: {missing[:5]})"]
    return True, []


def _write_output_file(output_path: str, content: str) -> bool:
    """将 agent 返回的内容写入输出文件"""
    try:
        resolved = _safe_project_path(output_path)
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        with open(resolved, 'w') as f:
            f.write(content)
        log(f"  💾 已写入输出文件: {resolved} ({len(content)} 字符)")
        return True
    except Exception as exc:
        log_warn(f"  ⚠️ 写入输出文件失败: {exc}")
        return False


def load_workflow(project_name: str):
    safe = _sanitize(project_name)
    wf = _load_workflow(project_name, base_dir=BASE_DIR)
    log(f"加载工作流: {safe}")
    return wf


def _checkpoint_checksum(ck: dict) -> str:
    """Compute SHA256 checksum for a checkpoint dict (excluding checksum field itself)."""
    ck_copy = {k: v for k, v in ck.items() if k != "_checksum"}
    raw = json.dumps(ck_copy, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def load_checkpoint(project_name: str):
    safe = _sanitize(project_name)
    path = os.path.join(BUS_DIR, "projects", f"{safe}_{CHECKPOINT_FILE}")
    if os.path.exists(path):
        try:
            with open(path) as f:
                ck = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log_warn(f"Checkpoint 文件损坏，丢弃: {exc}")
            return -1, None
        # Verify checksum integrity
        stored_checksum = ck.pop("_checksum", None)
        actual_checksum = _checkpoint_checksum(ck)
        if stored_checksum is not None and stored_checksum != actual_checksum:
            log_warn("Checkpoint 校验和不匹配（可能并发写入不完整），丢弃 checkpoint 从头跑")
            return -1, None
        return ck.get("step_index", -1), ck
    return -1, None


def save_checkpoint(
    project_name: str,
    step_index: int,
    title: str,
    status: str = "running",
):
    safe = _sanitize(project_name)
    path = os.path.join(BUS_DIR, "projects", f"{safe}_{CHECKPOINT_FILE}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ck = {
        "project": project_name,
        "step_index": step_index,
        "title": title,
        "status": status,
        "ts": datetime.now().isoformat(),
    }
    ck["_checksum"] = _checkpoint_checksum(ck)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        json.dump(ck, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return ck


def clear_checkpoint(project_name: str) -> None:
    safe = _sanitize(project_name)
    path = os.path.join(BUS_DIR, "projects", f"{safe}_{CHECKPOINT_FILE}")
    if os.path.exists(path):
        os.remove(path)


def find_resume_point(project_name, steps, force_start=False):
    if force_start:
        clear_checkpoint(project_name)
        return 0, []

    events = main_bus.read(project_name, limit=500)
    step_status = {}
    for e in events:
        etype = e.get("type", "")
        stage = e.get("stage", "")
        if not stage:
            continue
        if etype == "task.completed":
            step_status[stage] = "completed"
        elif etype == "task.failed" and step_status.get(stage) != "completed":
            step_status[stage] = "failed"
        elif etype == "task.started" and stage not in step_status:
            step_status[stage] = "started"

    skipped = []
    for i, step in enumerate(steps):
        stage = step.get("stage", f"step-{i + 1}")
        s = step_status.get(stage, "pending")
        if s == "pending":
            return i, skipped
        elif s in ("completed",):
            skipped.append((i + 1, step.get("title", stage), s))
        elif s == "failed":
            skipped.append((i + 1, step.get("title", stage), "failed-previous"))
            return i, skipped

    return len(steps), skipped


# ── 战甲心跳缓存（后台 TCP 扫描，15s 间隔）──────────────────
# 避免每次 /status/agents 都 HTTP 探活（11 个 agent 慢 33s）
_agent_heartbeat: dict[str, dict] = {}
_HEARTBEAT_CACHE_TTL = 15


def _probe_port(port: int, timeout: float = 1.0) -> bool:
    """TCP 端口连通检测，比 HTTP GET /health 快 10 倍"""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _heartbeat_scan():
    """后台线程入口：每 _HEARTBEAT_CACHE_TTL 秒扫描全部 agent"""
    while True:
        for name, port in AGENT_PORTS.items():
            online = _probe_port(port)
            _agent_heartbeat[name] = {
                "online": online,
                "last_seen": time.time(),
                "port": port,
            }
        time.sleep(_HEARTBEAT_CACHE_TTL)


# 启动心跳扫描（非阻塞，主进程 fork 后自动运行）
_heartbeat_thread = threading.Thread(target=_heartbeat_scan, daemon=True)
_heartbeat_thread.start()


def probe_agent(agent):
    port = AGENT_PORTS.get(agent, DEFAULT_AGENT_PORT)
    url = f"http://127.0.0.1:{port}/health"
    try:
        req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3):
            return True, port
    except Exception:
        return False, port


def resolve_agent(agent, fallback_agents=None):
    online, port = probe_agent(agent)
    if online:
        return agent, port, None
    fallbacks = fallback_agents or []
    for fb in fallbacks:
        fb_online, fb_port = probe_agent(fb)
        if fb_online:
            log_warn(f"主 agent [{agent}] 不在线，切换至 fallback [{fb}]")
            return fb, fb_port, agent
    # All agents offline — raise immediately instead of returning a dead port
    all_names = [agent] + fallbacks
    raise RuntimeError(
        f"All agents offline: {', '.join(all_names)}. "
        f"Checked ports: {[AGENT_PORTS.get(a, DEFAULT_AGENT_PORT) for a in all_names]}. "
        "Cannot execute step."
    )


def call_agent(agent, task_desc, timeout=None, health_check=True):
    """调用 agent：心跳缓存 → 可选 HTTP 健康检查 → 实际调用。

    Args:
        agent:      Agent 名称
        task_desc:  任务描述
        timeout:    请求超时秒数；None 用 agent_client 默认值（600s）
        health_check: 是否先做 GET /health 检查（默认 True，用 _heartbeat 缓存检查，除非 offline 先拒绝）
    """
    # 第 1 道：心跳缓存拒绝（fast path）
    heartbeat = _agent_heartbeat.get(agent)
    if heartbeat and not heartbeat["online"]:
        return {"status": "failed", "error": f"Agent [{agent}] 不在线 (端口 {heartbeat['port']})", "output": ""}

    # 第 2 道：HTTP 健康检查（确认真正在线）
    if health_check:
        online, port = probe_agent(agent)
        if not online:
            return {"status": "failed", "error": f"Agent [{agent}] HTTP /health 不通过", "output": ""}

    kwargs = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return _call_agent(agent, task_desc, **kwargs)


def detect_circular_dependency(steps: list) -> None:
    """Detect circular dependencies in workflow steps. Raises ValueError if found."""
    deps_map = {}
    for i, step in enumerate(steps):
        stage = step.get("stage", f"step-{i + 1}")
        deps = step.get("depends_on", [])
        deps = [d if d.startswith("step-") else d for d in deps]
        deps_map[stage] = deps

    visited = set()
    rec_stack = set()

    def _dfs(node):
        if node in rec_stack:
            raise ValueError(f"Circular dependency detected: node {node!r} is part of a dependency cycle")
        if node in visited:
            return
        visited.add(node)
        rec_stack.add(node)
        for dep in deps_map.get(node, []):
            _dfs(dep)
        rec_stack.discard(node)

    for stage in deps_map:
        _dfs(stage)


def wait_for_deps(
    project_name,
    depends_on,
    poll_interval=POLL_INTERVAL,
    max_wait=POLL_MAX_WAIT,
):
    if not depends_on:
        return True
    start = time.time()
    dep_stages = [d if d.startswith("step-") else d for d in depends_on]
    while time.time() - start < max_wait:
        events = main_bus.read(project_name, limit=200)
        completed = {e.get("stage") for e in events if e["type"] == "task.completed"}
        if all(ds in completed for ds in dep_stages):
            log(f"  依赖满足: {dep_stages}")
            return True
        time.sleep(poll_interval)
    log_warn(f"依赖超时: {dep_stages}")
    return False


STEP_TYPES = {"regular", "review", "compact", "shell", "verify"}


def execute_step(
    project_name,
    step,
    step_index,
    total_steps,
    retries=MAX_RETRIES,
):
    step_type = step.get("type", "regular")
    if step_type not in STEP_TYPES:
        log_warn(f"Unknown step type: {step_type}, falling back to regular")
        step_type = "regular"

    dispatch = {
        "regular": _execute_regular,
        "review": _execute_review,
        "compact": _execute_compact,
        "shell": _execute_shell,
        "verify": _execute_verify,
    }
    return dispatch[step_type](project_name, step, step_index, total_steps, retries)


def _inject_feedback(original_task, last_error, attempt, max_retries):
    """Inject Loop Engineering feedback into task for retry.

    Prepends the previous failure reason so the agent knows what went wrong.
    """
    feedback = (
        f"\n\n[Loop Engineering: Round {attempt}/{max_retries} Retry]\n"
        f"Previous attempt failed — {last_error}\n"
        f"Please analyze and fix the issue above, then re-output the corrected result.\n"
    )
    return feedback + "\n---\n" + original_task


def _execute_with_template(
    project_name,
    step,
    step_index,
    total_steps,
    build_enriched_task,
    step_label="",
    retries=MAX_RETRIES,
):
    """Shared execution template for agent steps (regular/review/compact)."""
    planned_agent = step.get("executor", "standard")
    fallback_agents = step.get("fallback_agents", [])
    title = step.get("title", f"Step {step_index + 1}")
    stage = step.get("stage", f"step-{step_index + 1}")
    depends_on = step.get("depends_on", [])
    optional = step.get("optional", False)

    try:
        actual_agent, port, fallback_from = resolve_agent(planned_agent, fallback_agents)
    except RuntimeError as exc:
        msg = str(exc)
        log_err(msg)
        _bus_write(
            project_name, "task.failed", planned_agent, {"title": title, "error": msg, "stage": stage}, stage=stage
        )
        return False

    label_suffix = f" [{step_label}]" if step_label else ""
    log_step(
        f"[{step_index + 1}/{total_steps}] {title} -> {actual_agent}{label_suffix}"
        + (f" (fallback: {fallback_from})" if fallback_from else "")
    )
    save_checkpoint(project_name, step_index, title, "running")
    if not _bus_write(
        project_name,
        "task.started",
        actual_agent,
        {
            "title": title,
            "stage": stage,
            "step_index": step_index,
            "planned_agent": planned_agent,
            "fallback_from": fallback_from,
        },
        stage=stage,
    ):
        return False

    if depends_on:
        log(f"  waiting on deps: {depends_on}")
        if not wait_for_deps(project_name, depends_on):
            _bus_write(
                project_name,
                "task.failed",
                "glink",
                {"title": title, "error": f"deps timeout: {depends_on}", "stage": stage},
                stage=stage,
            )
            return False

    ctx_events = main_bus.read(project_name, limit=30)
    prev_completed = [ev for ev in ctx_events if ev["type"] == "task.completed" and ev.get("stage", "") != stage]
    enriched_task = build_enriched_task(step, project_name, prev_completed)
    original_task = enriched_task  # save for Loop feedback reinjection

    last_error = None
    for attempt in range(retries + 1):
        if attempt > 0:
            enriched_task = _inject_feedback(original_task, last_error, attempt, retries)
            log_retry(f"retry {attempt}/{retries} -> {title}")
            time.sleep(3)
        log(f"  calling {actual_agent}(:{port}) [try-{attempt + 1}]{label_suffix}")
        log(f"  task: {len(enriched_task)} chars")
        result = call_agent(actual_agent, enriched_task)

        if result["status"] == "ok":
            output_text = result["output"]

            # ── 写入输出文件（保证文件存在供下一步使用） ──
            output_path = step.get("output_file", "")
            if output_path:
                _write_output_file(output_path, output_text)

            # ── 迭代验证（有 input_file 时检查是否保留了关键结构） ──
            input_path = step.get("input_file", "")
            key_patterns = step.get("_key_patterns", [])
            if input_path and key_patterns:
                ok, issues = _verify_iteration(output_text, key_patterns, input_path)
                if not ok:
                    log_warn(f"  ⚠️ 迭代验证未通过: {'; '.join(issues)}")
                    _bus_write(
                        project_name, "iteration.warning", actual_agent,
                        {"title": title, "issues": issues, "stage": stage},
                        stage=stage,
                    )
                else:
                    log(f"  ✅ 迭代验证通过 ({len(key_patterns)} 个关键结构)")

            # ── Loop Gate 验证 ──
            if step.get("gate"):
                from .gate import evaluate_gate
                gate_result = evaluate_gate(step, output_text)
                if not gate_result["passed"]:
                    last_error = gate_result["reason"]
                    log_warn(f"  ⚠️ Gate 未通过 (attempt {attempt + 1}): {last_error}")
                    _bus_write(
                        project_name, "gate.failed", "glink",
                        {"title": title, "error": last_error, "attempt": attempt + 1, "stage": stage},
                        stage=stage,
                    )
                    continue  # retry with injected feedback

            if not _bus_write(
                project_name,
                "task.completed",
                actual_agent,
                {
                    "title": title,
                    "output_preview": output_text[:200],
                    "stage": stage,
                    "step_index": step_index,
                    "planned_agent": planned_agent,
                    "fallback_from": fallback_from,
                },
                stage=stage,
            ):
                return False
            prev = output_text[:200]
            log_ok(f"done | {prev}...")
            dur = result.get("duration", 0)
            ds = f"{int(dur // 60)}m{int(dur % 60)}s" if isinstance(dur, (int, float)) else str(dur)
            get_reporter().summary(
                project=project_name,
                step_index=step_index + 1,
                total=total_steps,
                status=actual_agent,
                agent=actual_agent,
                duration=ds,
                detail=prev[:100],
            )
            return True
        else:
            last_error = result.get("error", "unknown")
            log_warn(f"  try-{attempt + 1} failed: {last_error}")

    if optional:
        _bus_write(
            project_name,
            "task.completed",
            actual_agent,
            {"title": title, "status": "skipped_optional", "error": last_error, "stage": stage},
            stage=stage,
        )
        log_warn(f"optional {title} skipped: {last_error[:80]}")
        get_reporter().alert(f"skipped: {title}", last_error[:80], severity="yellow")
        return True

    _bus_write(
        project_name,
        "task.failed",
        actual_agent,
        {
            "title": title,
            "error": last_error,
            "stage": stage,
            "step_index": step_index,
            "planned_agent": planned_agent,
            "fallback_from": fallback_from,
        },
        stage=stage,
    )
    log_err(f"step {title} failed: {last_error[:120]}")
    get_reporter().alert(f"failed: {title}", last_error[:120], severity="red")
    return False


def _execute_regular(project_name, step, step_index, total_steps, retries=MAX_RETRIES):
    base_task = step.get("description") or step.get("task", "")
    input_file_path = step.get("input_file", "")
    output_file_path = step.get("output_file", "")

    def build_task(_step, _proj, prev_completed):  # noqa: ARG001
        task_out = base_task
        if input_file_path:
            try:
                resolved_input = _safe_project_path(input_file_path)
            except ValueError as exc:
                log_err(f"input_file path traversal: {exc}")
                raise
            if os.path.isfile(resolved_input):
                try:
                    with open(resolved_input) as f:
                        prev_content = f.read()
                    resolved_output = _safe_project_path(output_file_path) if output_file_path else ""

                    # 提取关键函数/类名用于后续验证
                    _key_patterns = _extract_key_patterns(prev_content)
                    step["_key_patterns"] = _key_patterns

                    hint = (
                        f"""
═══════════════════════════════════════════════════
⬆️ ITERATION: 迭代修改 — 请勿重写
═══════════════════════════════════════════════════

你现在在做的是 **迭代修改** 已有代码。

❗ 铁律
a. 先完整阅读下面的代码
b. 只做必要的修改（下面 "修改目标" 部分）
c. 保留所有已有功能、结构、样式

✅ 必须保留
- 所有已有的函数、变量、类、HTML结构
- 所有已有的CSS样式、UI元素
- 所有已有的交互逻辑和事件绑定
- 注释、代码结构、命名规范

🔧 修改目标
{base_task}

📂 输入文件: {resolved_input} ({len(prev_content)} 字符)
📂 输出文件: {resolved_output}

⚠️ 输出要求
1. 返回完整的、可运行的代码文件
2. 你的回复必须 **只包含代码**（纯文本，不含解释说明）
3. 代码将在后台被自动保存到输出文件

═══════════════════════════════════════════════════
现有代码（请在此基础之上修改）：
═══════════════════════════════════════════════════
"""
                        if output_file_path
                        else (
                            f"""
═══════════════════════════════════════════════════
⬆️ ITERATION: 迭代修改 — 请勿重写
═══════════════════════════════════════════════════

你在做 **迭代修改**。完整阅读下面代码，只改需要改的部分。

修改目标:
{base_task}

═══════════════════════════════════════════════════
现有代码：
"""
                        )
                    )
                    task_out = hint + f"""```
{prev_content}
```
"""
                    log(f"  ✅ 读入 {resolved_input} ({len(prev_content)} 字符, {len(_key_patterns)} 个关键结构)")
                except Exception as exc:
                    log_warn(f"  ⚠️ 读取输入文件失败: {exc}")
            else:
                log_warn(f"  ⚠️ 输入文件不存在: {resolved_input}")

        if prev_completed:
            ctx = ["\n📋 已完成的步骤"]
            for ev in prev_completed[-5:]:
                s = ev.get("stage", "?")
                t = ev.get("data", {}).get("title", "?")
                o = ev.get("data", {}).get("output_preview", "")[:150]
                ctx.append(f"  ✅ {t} ({s}): {o}")
            task_out += "\n" + "\n".join(ctx)
        return task_out

    try:
        return _execute_with_template(project_name, step, step_index, total_steps, build_task, retries=retries)
    except ValueError:
        return False


def _execute_review(project_name, step, step_index, total_steps, retries=MAX_RETRIES):
    base_task = step.get("description") or step.get("task", "")
    input_file_path = step.get("input_file", "")
    output_file_path = step.get("output_file", "")

    def build_task(_step, _proj, prev_completed):  # noqa: ARG001
        task_out = "[CODE REVIEW]\n" + base_task
        if input_file_path:
            try:
                resolved_input = _safe_project_path(input_file_path)
            except ValueError as exc:
                log_err(f"review input_file: {exc}")
                raise
            if os.path.isfile(resolved_input):
                try:
                    with open(resolved_input) as f:
                        content = f.read()
                    resolved_output = _safe_project_path(output_file_path) if output_file_path else ""
                    out_hint = f"\n### Save report to\n  {resolved_output}\n" if output_file_path else ""
                    task_out += f"\n\n{out_hint}\n### Code\n```\n{content}\n```\n"
                    log(f"  review input: {resolved_input} ({len(content)} chars)")
                except Exception as exc:
                    log_warn(f"  review read error: {exc}")
            else:
                log_warn(f"  review input not found: {resolved_input}")

        if prev_completed:
            ctx = ["\n### Prior steps"]
            for ev in prev_completed[-5:]:
                s = ev.get("stage", "?")
                t = ev.get("data", {}).get("title", "?")
                o = ev.get("data", {}).get("output_preview", "")[:150]
                ctx.append(f"- {t} ({s}): {o}")
            task_out += "\n" + "\n".join(ctx)
        return task_out

    try:
        return _execute_with_template(
            project_name, step, step_index, total_steps, build_task, step_label="review", retries=retries
        )
    except ValueError:
        return False


def _execute_compact(project_name, step, step_index, total_steps, retries=MAX_RETRIES):
    title = step.get("title", f"Step {step_index + 1}")

    def build_task(step, proj, prev_completed):  # noqa: ARG001
        task_out = (
            "[CONTEXT COMPRESSION] Summarize:\n"
            "1. Key decisions\n2. Code changes\n3. Open issues\n"
            f"\nProject: {proj}\nStep: {step_index + 1}/{total_steps}\nTitle: {title}\n"
        )
        if prev_completed:
            ctx = ["\n### Context"]
            for ev in prev_completed[-10:]:
                s = ev.get("stage", "?")
                t = ev.get("data", {}).get("title", "?")
                o = ev.get("data", {}).get("output_preview", "")[:300]
                ctx.append(f"- {t} ({s}): {o}")
            task_out += "\n" + "\n".join(ctx)
        return task_out

    return _execute_with_template(
        project_name, step, step_index, total_steps, build_task, step_label="compact", retries=retries
    )


# ── Sandbox Security ──────────────────────────────────────
# macOS sandbox-exec profile for shell steps
_SANDBOX_PROFILE = """(version 1)
(allow default)
(deny network*)
(deny file-write*)
(allow file-read*)
(deny file-write* (subpath "/private/etc"))
(deny file-write* (subpath "/etc"))
(deny file-write* (subpath "/Users"))
(deny file-write* (subpath "/root"))
(allow file-write* (subpath "/tmp"))
(allow file-write* (subpath (param "ALLOWED_DIR")))
(allow process-exec)
(deny sysctl-write)
"""

# Attempt to resolve nobody uid for privilege dropping
_NOBODY_UID = None
try:
    _NOBODY_UID = pwd.getpwnam("nobody").pw_uid
except (KeyError, ImportError, Exception):
    _NOBODY_UID = None


def _sandbox_run(command: str, allowed_dir: str, timeout: int = 120):
    """Run a shell command under macOS sandbox-exec.

    Refuses to run if sandbox-exec is not available (no silent degradation).
    Drops privileges to nobody when possible.
    """
    sandbox_path = shutil.which("sandbox-exec")
    if not sandbox_path:
        raise RuntimeError(
            "sandbox-exec not found — shell step execution denied. "
            "sandbox-exec is required for secure shell execution. "
            "Install it or use a regular step type instead."
        )

    profile_path = f"/tmp/glink-sandbox-{os.getpid()}.sb"
    profile = _SANDBOX_PROFILE.replace('(param "ALLOWED_DIR")', f'"{allowed_dir}"')
    with open(profile_path, "w") as f:
        f.write(profile)

    preexec = None
    if _NOBODY_UID is not None:

        def _drop_privs():
            os.setuid(_NOBODY_UID)

        preexec = _drop_privs

    wrapped = f"{sandbox_path} -f {profile_path} /bin/bash -c {shlex.quote(command)}"
    try:
        result = subprocess.run(
            wrapped,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            preexec_fn=preexec,
        )
        return result
    finally:
        with contextlib.suppress(OSError):
            os.remove(profile_path)


_DANGEROUS_PATTERNS = [
    # ── Wipe / destroy patterns ──
    "rm -rf /",
    "rm -rf ~",
    "rm -rf /*",
    "rm -rf /var",
    "rm -rf /etc",
    "rm -rf /usr",
    "rm -rf /bin",
    "rm -rf /boot",
    "rm -rf /dev",
    "rm -rf /root",
    "rm -rf /home",
    "mkfs.",
    "dd if=/dev/",
    "dd if=",
    # ── Fork bomb ──
    ":(){ :|:& };:",
    # ── Overwrite system files ──
    "> /dev/",
    ">/dev/",
    "> /etc/",
    ">/etc/",
    # ── Permission escalation ──
    "chmod 777 /",
    "chmod 777 /var",
    "chmod 777 /etc",
    "chmod 777 /usr",
    "chmod 777 /bin",
    "chmod 777 /dev",
    "chmod 777 /root",
    "chmod 777 /etc/shadow",
    "chmod 777 /etc/passwd",
    "chmod 777 /etc/sudoers",
    # ── Network downloads (potential payload delivery) ──
    "curl http://",
    "curl https://",
    "wget http://",
    "wget https://",
    "fetch http://",
    "fetch https://",
    # ── Pipe-to-shell patterns ──
    "curl | bash",
    "curl | sh",
    "wget | bash",
    "wget | sh",
    "fetch | bash",
    "fetch | sh",
    # ── Arbitrary code execution ──
    "python3 -c ",
    "python -c ",
    "bash -c ",
    "sh -c ",
    "eval ",
    "eval$(",
    # ── Subshell injection ──
    "$(",
    "`",
    "exec ",
    "source /",
    ". /etc/",
    # ── Crypto mining / backdoor ──
    "minerd",
    "xmrig",
    "stratum+tcp",
    # ── SSH / credential access ──
    "ssh-keygen",
    "cat ~/.ssh/",
    "cat /etc/shadow",
    "cat /etc/passwd",
    # ── Aliases (command substring safety) ──
    r':"\$(``',
    "$(cat ",
    "`cat ",
]


def _validate_shell_command(command: str) -> str | None:
    """Validate a shell command, return error message or None if safe."""
    for pattern in _DANGEROUS_PATTERNS:
        if pattern in command:
            return f"Blocked by safety policy: {pattern}"
    return None


def _execute_shell(
    project_name,
    step,
    step_index,
    total_steps,
    retries=MAX_RETRIES,
):
    command = step.get("command", "")
    title = step.get("title", f"Shell Step {step_index + 1}")
    stage = step.get("stage", f"step-{step_index + 1}")

    log_step(f"╔══ [{step_index + 1}/{total_steps}] {title} [shell]")
    save_checkpoint(project_name, step_index, title, "running")
    if not _bus_write(
        project_name,
        "task.started",
        "shell",
        {"title": title, "stage": stage, "step_index": step_index},
        stage=stage,
    ):
        return False

    # Validate command
    err = _validate_shell_command(command)
    if err:
        log_err(err)
        _bus_write(
            project_name,
            "task.failed",
            "shell",
            {"title": title, "error": err, "stage": stage},
            stage=stage,
        )
        return False

    # Allowed directory for sandbox = projects dir
    allowed_dir = PROJECTS_DIR

    last_error = ""
    for attempt in range(retries + 1):
        if attempt > 0:
            log_retry(f"重试 {attempt}/{retries} → {title}")
            time.sleep(3)
        try:
            result = _sandbox_run(command, allowed_dir)
            output = result.stdout + result.stderr
            if result.returncode == 0:
                if not _bus_write(
                    project_name,
                    "task.completed",
                    "shell",
                    {
                        "title": title,
                        "output_preview": output[:200],
                        "stage": stage,
                        "step_index": step_index,
                    },
                    stage=stage,
                ):
                    return False
                log_ok(f"Shell 完成 | returncode=0, {len(output)} chars")
                return True
            else:
                log_warn(f"Shell returncode {result.returncode}: {output[:100]}")
                last_error = output[:200]
        except subprocess.TimeoutExpired:
            log_warn(f"Shell timeout (120s): {command[:50]}")
            last_error = "timeout"

    _bus_write(
        project_name,
        "task.failed",
        "shell",
        {"title": title, "error": last_error, "stage": stage, "step_index": step_index},
        stage=stage,
    )
    log_err(f"Shell failed: {title}: {last_error[:100]}")
    return False


def _execute_verify(project_name, step, step_index, total_steps, retries=1):
    """验证类型步骤。执行程序化检查（ScoreCard），不调动 LLM。

    Verify step 支持两种验证方式：
    1. `agent_verify: <agent名>` — 调其他 agent 做验证（如 Laser 审计）
    2. `script: <命令>` — 本地执行 shell 脚本做检查
    3. `check_handoff: true` — 验证上一步的 handoff schema 完整性
    """
    title = step.get("title", f"Verify {step_index + 1}")
    stage = step.get("stage", f"step-{step_index + 1}")
    log_step(f"[{step_index + 1}/{total_steps}] Verify: {title}")
    save_checkpoint(project_name, step_index, title, "running")

    # Mode 1: check_handoff — 验证上一步的 handoff schema 完整性
    if step.get("check_handoff"):
        log("  check_handoff: 验证上一步 handoff schema")
        events = main_bus.read(project_name, limit=5)
        prev_completed = [e for e in events if e["type"] == "task.completed"]
        if prev_completed:
            last = prev_completed[-1]
            data = last.get("data", {})
            out = data.get("output_preview", "")
            # 尝试解析上一步的 handoff
            try:
                import json
                handoff_raw = data.get("handoff", {})
                if isinstance(handoff_raw, str):
                    handoff_raw = json.loads(handoff_raw)
                from lib.handoff_schema import validate_handoff
                validation = validate_handoff(handoff_raw)
                if not validation["valid"]:
                    log_warn(f"  handoff schema 校验失败: {validation['errors']}")
                    _bus_write(project_name, "task.failed", "verify",
                               {"title": title, "error": f"handoff schema: {validation['errors']}", "stage": stage}, stage=stage)
                    return False
            except (ImportError, json.JSONDecodeError) as e:
                log_warn(f"  handoff schema 校验异常（用简单校验兜底）: {e}")
                # 兜底：简单校验
                if not out or "ERROR" in out.upper():
                    log_warn("  handoff check failed: empty or error output")
                    _bus_write(project_name, "task.failed", "verify",
                               {"title": title, "error": "handoff check failed", "stage": stage}, stage=stage)
                    return False
        log_ok("  handoff check passed")
        _bus_write(project_name, "task.completed", "verify",
                   {"title": title, "output_preview": "handoff verified", "stage": stage}, stage=stage)
        return True

    # Mode 2: call another agent for verification
    agent = step.get("agent_verify", "")
    tag = step.get("task", "")
    if agent and tag:
        log(f"  calling {agent} for verification")
        result = call_agent(agent, tag)
        if result["status"] == "ok":
            _bus_write(project_name, "task.completed", agent,
                       {"title": title, "output_preview": result["output"][:200], "stage": stage}, stage=stage)
            log_ok(f"  {agent} verify passed")
            return True
        else:
            log_warn(f"  {agent} verify failed: {result.get('error', 'unknown')}")
            return False

    # Mode 3: local script check
    script = step.get("script", "")
    if script:
        import subprocess
        try:
            r = subprocess.run(script, shell=True, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                _bus_write(project_name, "task.completed", "verify",
                           {"title": title, "output_preview": r.stdout[:200], "stage": stage}, stage=stage)
                log_ok("  script verify passed")
                return True
            else:
                log_warn(f"  script verify failed: {r.stderr[:100]}")
                return False
        except Exception as e:
            log_warn(f"  script verify exception: {e}")
            return False

    # 默认：没有指定验证方式则标记已完成
    log_warn("  verify step has no check method, marking ok")
    _bus_write(project_name, "task.completed", "verify",
               {"title": title, "output_preview": "no check method specified", "stage": stage}, stage=stage)
    return True


def _build_step_graph(steps):
    """Build execution dependency graph from workflow steps.

    Returns (ready_queue, dep_map, step_map) tuple:
    - ready_queue: list of step dicts with no remaining dependencies
    - dep_map: {step_id: {dep_id, ...}}
    - step_map: {step_id: step_dict}
    """
    dep_map = {}
    step_map = {}
    for i, step in enumerate(steps):
        step_id = step.get("id") or step.get("stage") or f"step-{i + 1}"
        deps = set(step.get("depends_on", []))
        dep_map[step_id] = deps
        step_map[step_id] = step

    ready_queue = [step_map[sid] for sid in step_map if not dep_map[sid]]
    return ready_queue, dep_map, step_map


# ── Thread-local for parallel execution context ──
_parallel_ctx = threading.local()


def _run_parallel(project_name, workflow, force_start=False, start_step=None):
    """Parallel execution engine for mode: parallel workflows.

    Runs independent steps concurrently using ThreadPoolExecutor.
    Steps with depends_on wait for all their dependencies to complete first.
    If any step fails, remaining pending steps are cancelled.
    """
    steps = workflow.get("steps", [])
    total = len(steps)
    if total == 0:
        log_err("工作流没有步骤")
        return False

    # Detect circular dependencies before starting execution
    try:
        detect_circular_dependency(steps)
    except ValueError as exc:
        log_err(f"工作流启动失败: {exc}")
        return False

    events = main_bus.read(project_name, limit=5)
    if not any(e["type"] == "project.update" for e in events) and not _bus_write(
        project_name,
        "project.update",
        "glink",
        {
            "action": "started",
            "title": workflow.get("project", {}).get("title", project_name),
            "goal": workflow.get("project", {}).get("goal", ""),
            "total_steps": total,
        },
    ):
        return False

    if force_start:
        clear_checkpoint(project_name)
        log("强制重跑，清除 checkpoint")
    elif start_step is not None:
        log(f"强制从 step-{start_step} 开始（并行模式）")
    else:
        # Checkpoint resume: fall back to serial if we need to resume mid-way
        start_index, skipped = find_resume_point(project_name, steps)
        if start_index > 0 and start_index < total:
            log_warn("并行模式下检测到未完成 checkpoint，改用串行模式恢复")
            workflow["project"]["mode"] = "serial"
            return run_workflow(project_name, workflow, force_start=False, start_step=None)
        for num, t, s in skipped:
            tag = "✅" if s == "completed" else "⚠️"
            log(f"  {tag} Step-{num} {s}: {t[:50]}")

    start_index = max(0, int(start_step) - 1) if start_step is not None else 0

    if start_index >= total:
        s = main_bus.status(project_name)
        log_ok(f"工作流已完成！Bus 统计: {s['tasks_completed']}/{total} 步")
        clear_checkpoint(project_name)
        return True

    parallel_timeout = workflow.get("timeout")  # 工作流级并行超时（默认无超时）
    step_timeout = workflow.get("step_timeout")  # 单步超时（默认无超时）
    log(f"并行执行 → {total} 步，从 Step-{start_index + 1} 开始")
    log(
        f"配置: max_concurrent={get_max_concurrent_steps()}, "
        f"workflow_timeout={parallel_timeout}s, step_timeout={step_timeout}s"
    )

    ready_queue, dep_map, step_map = _build_step_graph(steps)

    # Thread-safe state
    completed = set()
    failed = set()
    _lock = threading.Lock()
    cancel_event = threading.Event()

    # Build reverse dep map: step_id -> [step_ids that depend on it]
    reverse_dep_map = {sid: [] for sid in step_map}
    for sid, deps in dep_map.items():
        for d in deps:
            if d in reverse_dep_map:
                reverse_dep_map[d].append(sid)

    # Thread-safe remaining dependencies
    remaining_deps = {}
    for sid, deps in dep_map.items():
        remaining_deps[sid] = set(deps)

    pending_futures = {}

    def _submit_step(step_dict):
        """Submit a single step for execution via executor."""
        step_index = [
            _i
            for _i, s in enumerate(steps)
            if s is step_dict or s.get("stage", f"step-{_i + 1}") == step_dict.get("stage", "")
        ]
        idx = step_index[0] if step_index else steps.index(step_dict)

        def _run():
            if cancel_event.is_set():
                return False
            _parallel_ctx.is_parallel = True
            try:
                result = execute_step(project_name, step_dict, idx, total)
                if result and not cancel_event.is_set():
                    return True
                else:
                    if not cancel_event.is_set():
                        cancel_event.set()
                    return False
            except Exception as exc:
                log_err(f"并行步骤 {step_dict.get('title', idx)} 异常: {exc}")
                if not cancel_event.is_set():
                    cancel_event.set()
                return False

        return executor.submit(_run)

    max_workers = min(len(steps), get_max_concurrent_steps())
    log(f"启动 ThreadPoolExecutor (max_workers={max_workers})")

    success = False
    _parallel_start = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all initially ready steps
        for step_dict in ready_queue:
            fut = _submit_step(step_dict)
            sid = step_dict.get("stage", f"{steps.index(step_dict)}")
            pending_futures[fut] = sid

        # Process futures as they complete
        while pending_futures and not cancel_event.is_set():
            elapsed = time.time() - _parallel_start
            if parallel_timeout is not None and elapsed >= parallel_timeout:
                log_err(f"并行执行超时 ({parallel_timeout}s)，{len(pending_futures)} 步未完成")
                for pfut in list(pending_futures):
                    pfut.cancel()
                pending_futures.clear()
                break
            done, _ = concurrent.futures.wait(
                pending_futures.keys(),
                return_when=concurrent.futures.FIRST_COMPLETED,
                timeout=POLL_INTERVAL,
            )

            for fut in done:
                sid = pending_futures.pop(fut, None)
                if sid is None:
                    continue
                try:
                    step_ok = fut.result()
                except Exception as exc:
                    log_err(f"步骤 {sid} 执行异常: {exc}")
                    step_ok = False

                if step_ok:
                    with _lock:
                        completed.add(sid)
                    # Check if any dependents are now ready
                    for dependent_id in reverse_dep_map.get(sid, []):
                        with _lock:
                            remaining_deps[dependent_id].discard(sid)
                            if not remaining_deps[dependent_id]:
                                dep_step = step_map.get(dependent_id)
                                if dep_step:
                                    log(f"  依赖满足: {dependent_id}（前驱 {sid} 完成）")
                                    dep_fut = _submit_step(dep_step)
                                    pending_futures[dep_fut] = dependent_id
                else:
                    with _lock:
                        failed.add(sid)
                    cancel_event.set()
                    # Cancel all pending
                    for pfut in list(pending_futures):
                        pfut.cancel()
                    pending_futures.clear()
                    break

        if not failed:
            success = True

    if success:
        clear_checkpoint(project_name)
        if not _bus_write(
            project_name,
            "project.update",
            "glink",
            {"action": "completed", "total_steps": total},
        ):
            return False
        s = main_bus.status(project_name)
        log("")
        log("=" * 50)
        log_ok(f"[{project_name}] ✅ 并行执行完成！{total}/{total} 步")
        log(f"  Bus: {s['total_events']} 事件 | Agent: {', '.join(s['agents_involved'])}")
        log("=" * 50)
    else:
        save_checkpoint(
            project_name,
            0,
            steps[0].get("title", ""),
            "interrupted",
        )
        s = main_bus.status(project_name)
        log_err("并行执行中断，checkpoint 已保存")

    return success


def run_workflow(project_name, workflow, force_start=False, start_step=None):
    steps = workflow.get("steps", [])
    total = len(steps)
    if total == 0:
        log_err("工作流没有步骤")
        return

    # 初始化状态门禁
    state_hook = None
    if StateGateHook is not None:
        state_hook = StateGateHook(project_name, reporter=get_reporter())
        if state_hook.enabled:
            log(f"[StateGate] ✅ 已启用: {project_name}")

    # Route to parallel engine if mode is parallel
    mode = workflow.get("project", {}).get("mode", "serial")
    if mode == "parallel":
        return _run_parallel(project_name, workflow, force_start, start_step)

    # Detect circular dependencies before starting execution
    try:
        detect_circular_dependency(steps)
    except ValueError as exc:
        log_err(f"工作流启动失败: {exc}")
        return

    events = main_bus.read(project_name, limit=5)
    if not any(e["type"] == "project.update" for e in events) and not _bus_write(
        project_name,
        "project.update",
        "glink",
        {
            "action": "started",
            "title": workflow.get("project", {}).get("title", project_name),
            "goal": workflow.get("project", {}).get("goal", ""),
            "total_steps": total,
        },
    ):
        return False

    if start_step is not None:
        start_index = max(0, int(start_step) - 1)
        skipped = []
        log(f"强制从 step-{start_index + 1} 开始")
    elif force_start:
        start_index, skipped = 0, []
        clear_checkpoint(project_name)
        log("强制重跑，清除 checkpoint")
    else:
        start_index, skipped = find_resume_point(project_name, steps)
        for num, t, s in skipped:
            tag = "✅" if s == "completed" else "⚠️"
            log(f"  {tag} Step-{num} {s}: {t[:50]}")

    if start_index >= total:
        s = main_bus.status(project_name)
        log_ok(f"工作流已完成！Bus 统计: {s['tasks_completed']}/{total} 步")
        clear_checkpoint(project_name)
        return True

    log(f"断点续跑 → 从 Step-{start_index + 1} 开始（共 {total} 步）")

    # 条件路由循环（支持 on_success / on_failure 跳转）
    success = True
    loop_counters: dict[str, int] = {}
    i = start_index
    visited = set()
    max_global_loops = 20
    global_loop = 0

    while 0 <= i < total:
        step = steps[i]
        stage = step.get("stage", f"step-{i + 1}")
        title = step.get("title", stage)

        # 检测死循环
        if global_loop > max_global_loops:
            log_err(f"全局死循环检测: {max_global_loops}次跳转，停止")
            save_checkpoint(project_name, i, title, "deadlocked")
            success = False
            break
        global_loop += 1

        # 检测循环依赖（同一 step 超 max_loops 次）
        max_loops = int(step.get("max_loops", 1))
        loop_counters[stage] = loop_counters.get(stage, 0) + 1
        if loop_counters[stage] > max_loops:
            log_err(f"{title} 超过 max_loops={max_loops}，标记 unrecoverable")
            save_checkpoint(project_name, i, title, "unrecoverable")
            _bus_write(
                project_name, "task.failed", "glink",
                {"title": title, "error": f"unrecoverable: max_loops={max_loops}", "stage": stage},
                stage=stage
            )
            success = False
            break

        # ── 状态门禁检查 ──
        if state_hook and state_hook.enabled and not state_hook.check_step(step, i):
            _bus_write(project_name, "state.blocked", "glink",
                       {"step": step.get("id", f"step-{i+1}"), "stage": stage, "title": title},
                       stage=stage)
            success = False
            break

        ok = execute_step(project_name, step, i, total)

        # 步骤成功，更新状态
        if ok and state_hook and state_hook.enabled:
            state_hook.after_step(step, {"status": "ok"})

        if ok:
            on_success = step.get("on_success", "")
            if on_success:
                next_i = -1
                for si, s in enumerate(steps):
                    s_stage = s.get("stage", f"step-{si + 1}")
                    if s_stage == on_success or f"step-{si + 1}" == on_success:
                        next_i = si
                        break
                if next_i >= 0:
                    log(f"  {title} -> on_success: {on_success} (step-{next_i + 1})")
                    i = next_i
                else:
                    log_warn(f"  on_success '{on_success}' 未找到，按顺序继续")
                    i += 1
            else:
                i += 1
        else:
            save_checkpoint(project_name, i, title, "failed")
            on_failure = step.get("on_failure", "")
            if on_failure:
                next_i = -1
                for fi, s in enumerate(steps):
                    s_stage = s.get("stage", f"step-{fi + 1}")
                    if s_stage == on_failure or f"step-{fi + 1}" == on_failure:
                        next_i = fi
                        break
                if next_i >= 0:
                    log(f"  {title} failed -> on_failure: {on_failure} (step-{next_i + 1})")
                    i = next_i
                else:
                    log_warn(f"  on_failure '{on_failure}' 未找到，停止")
                    success = False
                    break
            else:
                log(f"  {title} failed -> 无 on_failure 路由，停止")
                success = False
                break

        time.sleep(1)

    if success:
        clear_checkpoint(project_name)
        if not _bus_write(
            project_name,
            "project.update",
            "glink",
            {"action": "completed", "total_steps": total},
        ):
            return False
        s = main_bus.status(project_name)
        log("")
        log("=" * 50)
        log_ok(f"[{project_name}] ✅ 全流程完成！{total}/{total} 步")
        log(f"  Bus: {s['total_events']} 事件 | Agent: {', '.join(s['agents_involved'])}")
        log("=" * 50)
    else:
        save_checkpoint(
            project_name,
            start_index,
            steps[start_index].get("title", ""),
            "interrupted",
        )
        s = main_bus.status(project_name)
        log_err(f"流程中断于 Step-{start_index + 1}，checkpoint 已保存")

    return success
