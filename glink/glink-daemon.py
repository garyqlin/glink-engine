#!/usr/bin/env python3
"""
Glink Daemon v0.5 — 监控 Dashboard + 智能路由 + 自动恢复
兼容入口，实现在 daemon/ 模块中。
"""

import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "bus"))


import yaml

# 注册 Bus 告警处理器（write 失败时通过 Reporter 通知）
from bus.main_bus import _set_alert_handler
from daemon import (
    cleanup_pidfile,
    ensure_pid,
    get_reporter,
    load_workflow,
    log,
    log_err,
    log_ok,
    log_warn,
    run_workflow,
    start_api_server,
)
from daemon import send_alert as _daemon_alert
from daemon.config import get_default_project

_set_alert_handler(_daemon_alert)

WORKFLOWS_DIR = os.path.join(BASE_DIR, "workflows")
_BOOT_TIMESTAMP = os.path.join(BASE_DIR, ".glink-boot.ts")

_REST_PROJECT = {"name": get_default_project()}


# ── 智能规划接口 ────────────────────────────────────────
_PLAN_TEMPLATE = """# EXAMPLE workflow — sandbox-builder (沙盒建造游戏)
# Use this exact format. Adapt the steps for the user's project.
project:
  name: sandbox-builder
  title: 3D沙盒建造游戏
  goal: Build a Minecraft-style sandbox game
steps:
  - id: step-1
    executor: hammer
    fallback_agents: ["standard"]
    title: Scene Setup
    task: "Three.js scene + camera + lighting + render loop + OrbitControls"
    output_file: projects/sandbox-builder/sandbox-builder-step1.html

  - id: step-2
    executor: hammer
    fallback_agents: ["standard"]
    title: Block Placement
    description: "Implement block placement/removal with raycasting"
    input_file: projects/sandbox-builder/sandbox-builder-step1.html
    output_file: projects/sandbox-builder/sandbox-builder-step2.html
    depends_on: [step-1]"""

_ALLOWED_STEP_TYPES = {"regular", "review", "compact"}
_SHELL_STEP_HELP = "shell-type steps require explicit --allow-shell flag and are not auto-generated"

# 从 bus/__init__.py 导入项目名白名单
from bus import sanitize_project_name as _sanitize_plan_project


def _validate_plan_steps(steps: list, allow_shell: bool = False) -> list[str]:
    """Validate steps from LLM-generated plan. Returns list of error messages (empty = safe)."""
    errors = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"Step {i}: not a dict")
            continue
        step_type = step.get("type", "regular")
        if step_type not in _ALLOWED_STEP_TYPES:
            if step_type == "shell" and not allow_shell:
                errors.append(
                    f"Step {i + 1}: shell-type steps are not allowed in auto-generated plans. {_SHELL_STEP_HELP}"
                )
            elif step_type not in _ALLOWED_STEP_TYPES and step_type != "shell":
                errors.append(
                    f"Step {i + 1}: unknown step type {step_type!r} (allowed: {', '.join(sorted(_ALLOWED_STEP_TYPES))})"
                )
        # Validate command field (should not exist in non-shell steps)
        command = step.get("command")
        if command and step_type != "shell":
            errors.append(f"Step {i + 1}: 'command' field present on non-shell step type {step_type!r}")
        if command and step_type == "shell" and allow_shell:
            from daemon.core import _validate_shell_command

            cmd_err = _validate_shell_command(command)
            if cmd_err:
                errors.append(f"Step {i + 1} command blocked: {cmd_err}")
        # Validate string fields
        for field in ("executor", "title", "task", "description"):
            val = step.get(field)
            if val is not None and not isinstance(val, str):
                errors.append(f"Step {i + 1}.{field}: expected string, got {type(val).__name__}")
        # Validate depends_on
        deps = step.get("depends_on", [])
        if not isinstance(deps, list):
            errors.append(f"Step {i + 1}.depends_on: expected list, got {type(deps).__name__}")
        # P1-C: 长度限制校验
        for i, step in enumerate(steps):
            # project name ≤64 字符
            project_name = step.get("project_name", "")
            if len(project_name) > 64:
                old_name = project_name
                step["project_name"] = project_name[:64]
                errors.append(f"Step {i + 1}: project_name truncated from {len(old_name)} to 64 chars")
            # task/description ≤5000 字符
            for field in ("task", "description"):
                val = step.get(field, "")
                if len(val) > 5000:
                    step[field] = val[:5000]
                    errors.append(f"Step {i + 1}.{field} truncated from {len(val)} to 5000 chars")

        # P0-C: executor 白名单校验
        known_executors = {
            "standard",
            "hammer",
            "ink",
            "bumblebee",
            "Laser",
            "Forge",
            "forge",
        }
        for i, step in enumerate(steps):
            executor = step.get("executor", "standard")
            if executor not in known_executors:
                errors.append(f"Step {i + 1}: executor {executor!r} not in whitelist, falling back to 'standard'")
                step["executor"] = "standard"

        return errors


_PLAN_SYSTEM_PROMPT = f"""You are a workflow architect for Glink, an agent orchestration engine.
Given a user's one-sentence project description, generate a complete workflow YAML.

RULES (strict, no exceptions):
1. Each step MUST have: id, executor, type (optional, default: regular), title, task
2. Only use these fields per step: id, executor, fallback_agents, title, task, description, input_file, output_file, depends_on, type, optional, command (for shell type)
3. For incremental builds: step-2 depends_on step-1, uses input_file/output_file
4. Use executors by role: hammer(engineering/coding), ink(UI/design), bumblebee(testing), Laser(final QA)
5. First step: NO input_file or depends_on
6. Last step: type: review for code review
7. Add type: shell for command-only steps (use command field instead of task)
8. Output ONLY valid YAML, wrapped in ```yaml ... ```, no other text

TEMPLATE (use this structure, adapt for your project):
{_PLAN_TEMPLATE}"""


def glink_plan(description: str, output_name: str | None = None):
    """LLM 智能规划：一句话 → workflow YAML"""
    from daemon.core import call_agent

    log(f"🧠 智能规划: {description}")

    name = output_name or description.lower().replace(" ", "-")[:36]
    prompt = f"""Project: {description}
Name: {name}

Generate the YAML now."""

    result = call_agent(
        "standard",
        f"{_PLAN_SYSTEM_PROMPT}\n\n{prompt}",
        timeout=180,
    )

    if result["status"] == "failed":
        log_err(f"规划失败: {result.get('error', 'unknown')}")
        return None

    raw_yaml = result["output"]

    # Strip markdown code fences if present
    if "```yaml" in raw_yaml:
        raw_yaml = raw_yaml.split("```yaml")[1].split("```")[0]
    elif "```" in raw_yaml:
        raw_yaml = raw_yaml.split("```")[1].split("```")[0]

    raw_yaml = raw_yaml.strip()

    # Validate YAML
    try:
        parsed = yaml.safe_load(raw_yaml)
        if not parsed or not isinstance(parsed, dict):
            log_err("LLM 返回的不是有效 YAML")
            return None
        steps = parsed.get("steps", [])
        if not steps:
            log_err("YAML 中没有 steps")
            return None
        log_ok(f"规划完成: {len(steps)} 步")
    except yaml.YAMLError as e:
        log_err(f"YAML 解析失败: {e}")
        return None

    # ⚠️ Security: Validate all steps from LLM output
    allow_shell = "--allow-shell" in sys.argv
    plan_errors = _validate_plan_steps(steps, allow_shell=allow_shell)
    if plan_errors:
        for err in plan_errors:
            log_err(f"  安全校验失败: {err}")
        log_err("--plan 输出包含不安全内容，已拒绝生成。请重试或手动创建 workflow。")
        return None

    # Sanitize project name
    project_name = parsed.get("project", {}).get("name", name)
    parsed.setdefault("project", {})["name"] = _sanitize_plan_project(project_name)

    # Ensure project section
    if "project" not in parsed:
        parsed["project"] = {
            "name": name,
            "title": description,
            "goal": description,
        }
    raw_yaml = yaml.dump(parsed, default_flow_style=False, allow_unicode=True, sort_keys=False)

    out_path = os.path.join(WORKFLOWS_DIR, f"{name}.yaml")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(raw_yaml)

    log_ok(f"工作流已保存: {out_path}")
    return name


def run_daemon(project, force=False, start_step=None):
    from daemon import C_OK, C_WARN

    ensure_pid()
    log(f"🚀 Glink Daemon v0.5 | 项目: {project}")
    start_api_server()
    log(f"   工作流: {WORKFLOWS_DIR}/{project}.yaml")
    log(f"   Bus:    {BASE_DIR}/bus/projects/{project}.jsonl")
    log("   重试:   2x | 轮询间隔: 3s")

    workflow = load_workflow(project)
    wf_title = workflow.get("project", {}).get("title", project)
    step_count = len(workflow.get("steps", []))
    log(f"   加载: {wf_title}")
    log(f"   步骤: {step_count} 步")

    get_reporter().push(
        f"🚀 **Glink 工作流启动**\n   项目：{project}\n   步骤：{step_count} 步\n   模式：{'重跑' if force else '断点续跑'}"
    )

    success = run_workflow(project, workflow, force_start=force, start_step=start_step)

    if success:
        get_reporter().alert("✅ Glink 工作流完成", f"项目 **{project}** 的全部 {step_count} 步已完成。", severity=C_OK)
    else:
        get_reporter().alert(
            "⚠️ Glink 工作流中断", f"项目 **{project}** 未完成，可通过 /restart 重启。", severity=C_WARN
        )

    log("")
    if success:
        log_ok("工作流完成，API 持续运行中...")
    else:
        log_warn("工作流中断，API 持续运行中（可通过 /restart 重启）...")
    log("  📡 Dashboard: http://127.0.0.1:8426")
    log("  停止: kill $(lsof -ti :8426)")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        cleanup_pidfile()
        log("Glink Daemon 已停止")
        sys.exit(0)


DEFAULT_PROJECT = get_default_project()


def main():
    project = DEFAULT_PROJECT
    force = False
    start_step = None
    serve_only = False
    plan_desc = None
    plan_output = None

    for arg in sys.argv[1:]:
        if arg == "--force":
            force = True
        elif arg == "--serve":
            serve_only = True
        elif arg.startswith("--plan="):
            plan_desc = arg.split("=", 1)[1]
        elif arg.startswith("--plan-output="):
            plan_output = arg.split("=", 1)[1]
        elif arg.startswith("--step="):
            raw = arg.split("=", 1)[1]
            try:
                start_step = int(raw)
            except ValueError:
                log_err(f"--step 参数必须是整数，收到: {raw!r}")
                sys.exit(2)
        elif not arg.startswith("-"):
            project = arg

    # 智能规划模式
    if plan_desc:
        result_name = glink_plan(plan_desc, plan_output)
        if result_name:
            log_ok(f"✅ 规划完成，运行: python3 glink-daemon.py {result_name}")
        sys.exit(0 if result_name else 1)

    _REST_PROJECT["name"] = project
    try:
        from daemon.api import set_project as _set_api_project

        _set_api_project(project)
    except ImportError:
        pass

    if serve_only:
        log("🔄 Glink Daemon v0.5 (serve-only) | API 端口 8426")
        ensure_pid()
        start_api_server()
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            cleanup_pidfile()
            sys.exit(0)
    else:
        run_daemon(project, force=force, start_step=start_step)


if __name__ == "__main__":
    main()
