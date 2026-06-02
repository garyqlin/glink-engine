# SPDX-License-Identifier: MIT
"""Glink Daemon — 工作流编排核心：运行、检查点、步骤执行"""

import concurrent.futures
import contextlib
import fcntl
import hashlib
import json
import os
import pwd
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


from bus import sanitize_project_name as _sanitize
from bus.agent_client import AGENT_PORTS
from bus.agent_client import call_agent as _call_agent
from bus.agent_client import load_workflow as _load_workflow

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


def probe_agent(agent):
    port = AGENT_PORTS.get(agent, 8420)
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
        f"Checked ports: {[AGENT_PORTS.get(a, 8420) for a in all_names]}. "
        "Cannot execute step."
    )


def call_agent(agent, task_desc, timeout=600):
    return _call_agent(agent, task_desc, timeout=timeout)


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


STEP_TYPES = {"regular", "review", "compact", "shell"}


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
    }
    return dispatch[step_type](project_name, step, step_index, total_steps, retries)


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

    last_error = None
    for attempt in range(retries + 1):
        if attempt > 0:
            log_retry(f"retry {attempt}/{retries} -> {title}")
            time.sleep(3)
        log(f"  calling {actual_agent}(:{port}) [try-{attempt + 1}]{label_suffix}")
        log(f"  task: {len(enriched_task)} chars")
        result = call_agent(actual_agent, enriched_task)

        if result["status"] == "ok":
            if not _bus_write(
                project_name,
                "task.completed",
                actual_agent,
                {
                    "title": title,
                    "output_preview": result["output"][:200],
                    "stage": stage,
                    "step_index": step_index,
                    "planned_agent": planned_agent,
                    "fallback_from": fallback_from,
                },
                stage=stage,
            ):
                return False
            prev = result["output"][:200]
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
                    hint = (
                        f"\n### Incremental task\n{base_task}\n"
                        f"\n### Input file: {resolved_input} ({len(prev_content)} chars)\n"
                        f"Read fully, then modify. Output complete HTML to: {resolved_output}\n"
                        if output_file_path
                        else ""
                    )
                    task_out = hint + "\n" + f"```\n{prev_content}\n```\n"
                    log(f"  read {resolved_input} ({len(prev_content)} chars)")
                except Exception as exc:
                    log_warn(f"  cannot read input: {exc}")
            else:
                log_warn(f"  input not found: {resolved_input}")

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

    parallel_timeout = workflow.get("timeout", 600)  # 工作流级并行超时
    step_timeout = workflow.get("step_timeout", 300)  # 单步超时（秒）
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
            if elapsed >= parallel_timeout:
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

    success = True
    for i in range(start_index, total):
        step = steps[i]
        ok = execute_step(project_name, step, i, total)
        if not ok:
            save_checkpoint(project_name, i, step.get("title", f"step-{i + 1}"), "failed")
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
