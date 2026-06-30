# SPDX-License-Identifier: MIT
"""Gate Engine — Loop Engineering 自动验收检查

Workflow step 可以定义 gate 字段，agent 执行完毕后自动验证：
  - script:       运行 shell 命令，exit 0 为通过
  - file_exists:  检查输出文件存在且非空
  - output_contains: agent 输出含指定正则模式

用法 (workflow YAML):
  - id: step-1
    executor: hammer
    title: Build API
    gate:
      type: script
      command: "python3 -m pytest tests/test_api.py -x -q"

  - id: step-2
    executor: ink
    title: Create Layout
    gate:
      type: file_exists
      path: "projects/example/output.html"
"""

import os
import re
import shlex
import subprocess


def evaluate_gate(step, output_text):
    """Evaluate all gates defined in a workflow step.

    Args:
        step: workflow step dict (may contain "gate" field)
        output_text: the agent's output text

    Returns:
        {"passed": bool, "reason": str}
    """
    gate_config = step.get("gate")
    if not gate_config:
        return {"passed": True, "reason": ""}

    # Accept single gate or list of gates
    gates = gate_config if isinstance(gate_config, list) else [gate_config]

    for g in gates:
        gtype = g.get("type", "output_contains")
        if gtype == "script":
            result = _eval_script_gate(g)
        elif gtype == "file_exists":
            result = _eval_file_gate(g)
        elif gtype == "output_contains":
            result = _eval_output_gate(g, output_text)
        else:
            return {"passed": False, "reason": f"Unknown gate type: {gtype!r}"}

        if not result["passed"]:
            return result

    return {"passed": True, "reason": ""}


def _eval_script_gate(config):
    command = config.get("command", "")
    if not command:
        return {"passed": True, "reason": "script gate: empty command, skipped"}

    workdir = config.get("workdir", os.getcwd())
    try:
        cp = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=config.get("timeout", 120),
            cwd=workdir,
        )
        if cp.returncode == 0:
            return {"passed": True, "reason": ""}
        else:
            stderr = cp.stderr.strip() or "(no stderr)"
            stdout = cp.stdout.strip() or "(no stdout)"
            max_log = config.get("max_error_log", 300)
            detail = (stderr[:max_log] if stderr != "(no stderr)" else stdout[:max_log])
            return {
                "passed": False,
                "reason": f"script exit={cp.returncode}: {detail}",
            }
    except subprocess.TimeoutExpired:
        return {"passed": False, "reason": f"script timeout ({config.get('timeout', 120)}s)"}
    except FileNotFoundError as e:
        return {"passed": False, "reason": f"script not found: {e}"}


def _eval_file_gate(config):
    path = config.get("path", "")
    if not path:
        return {"passed": False, "reason": "file_exists gate: no path specified"}

    if not os.path.exists(path):
        return {"passed": False, "reason": f"file not found: {path}"}

    min_bytes = config.get("min_bytes", 1)
    actual = os.path.getsize(path)
    if actual < min_bytes:
        return {
            "passed": False,
            "reason": f"file too small: {path} ({actual} bytes, need >= {min_bytes})",
        }

    return {"passed": True, "reason": ""}


def _eval_output_gate(config, output_text):
    pattern = config.get("pattern", "")
    if not pattern:
        return {"passed": False, "reason": "output_contains gate: no pattern specified"}

    negate = config.get("negate", False)
    try:
        found = bool(re.search(pattern, output_text, re.MULTILINE))
    except re.error as e:
        return {"passed": False, "reason": f"output_contains: invalid regex: {e}"}

    if negate:
        # Pass if pattern is NOT found
        if found:
            return {"passed": False, "reason": f"unexpected pattern found: {pattern!r}"}
        return {"passed": True, "reason": ""}
    else:
        if not found:
            preview = output_text[:100].replace("\n", " ")
            return {"passed": False, "reason": f"pattern not found: {pattern!r} (output: {preview}...)"}
        return {"passed": True, "reason": ""}
