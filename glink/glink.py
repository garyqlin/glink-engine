#!/usr/bin/env python3
"""
Glink v0.1 — 工作流调度引擎
读取 workflow.yaml → 按顺序调 agent → 每步结果写 Main Bus
"""

import os
import sys

# Main Bus 路径
BUS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bus")
sys.path.insert(0, BUS_DIR)

from agent_client import AGENT_PORTS, call_agent, load_workflow
from main_bus import status as bus_status
from main_bus import write as bus_write


def execute_step(step, project, context):
    """执行一步工作流"""
    print(f"\n{'=' * 60}")
    print(f"▶ 步骤: {step.get('title', '无标题')}")
    print(f"  执行人: {step.get('executor', '?')}")
    print(f"  描述: {step.get('description', step.get('task', '')[:80])}")
    print(f"{'=' * 60}")

    agent = step.get("executor", "标准版")
    port = AGENT_PORTS.get(agent, 8420)

    # 构造给 agent 的任务描述
    task = step.get("description") or step.get("task", "")

    # 写入 Bus: 任务开始
    stage = step.get("stage", "")
    bus_write(
        project,
        "task.started",
        agent,
        {
            "title": step.get("title", ""),
            "task": task[:200],
            "stage": stage,
        },
        stage=stage,
    )

    # 调用 agent（显式传 port，保持与原逻辑一致）
    result = call_agent(agent, task, port=port)

    if result["status"] == "ok":
        bus_write(
            project,
            "task.completed",
            agent,
            {
                "title": step.get("title", ""),
                "output": result.get("output", "")[:200],
            },
            stage=stage,
        )
        print(f"✅ {agent} 完成: {result['output'][:100]}...")
        context["last_output"] = result["output"]
        return True
    else:
        bus_write(
            project,
            "task.failed",
            agent,
            {
                "title": step.get("title", ""),
                "error": result.get("error", ""),
            },
            stage=stage,
        )
        print(f"❌ {agent} 失败: {result['error']}")
        return False


def run(project_name, workflow=None):
    """运行完整工作流"""
    print(f"\n{'#' * 60}")
    print(f"# Glink 启动: {project_name}")
    print(f"{'#' * 60}")

    if workflow is None:
        workflow = load_workflow(project_name)

    # 写入 Bus: 项目启动
    bus_write(
        project_name,
        "project.update",
        "glink",
        {
            "action": "started",
            "title": workflow.get("project", {}).get("title", project_name),
            "goal": workflow.get("project", {}).get("goal", ""),
        },
        stage="bootstrap",
    )

    steps = workflow.get("steps", [])
    total = len(steps)
    context = {"project": project_name}

    for i, step in enumerate(steps):
        print(f"\n  [{i + 1}/{total}] {step.get('title', '无标题')}")
        success = execute_step(step, project_name, context)

        if not success and step.get("optional", False):
            print("  ⚠ 可选的步骤失败了，跳过继续")
            continue
        elif not success:
            print(f"\n❌ 工作流中断在第 {i + 1} 步")
            bus_write(
                project_name,
                "project.update",
                "glink",
                {
                    "action": "failed",
                    "failed_step": i + 1,
                    "failed_title": step.get("title", ""),
                },
                stage="failed",
            )
            return False

    # 写入 Bus: 项目完成
    bus_write(
        project_name,
        "project.update",
        "glink",
        {
            "action": "completed",
            "total_steps": total,
        },
        stage="complete",
    )

    print(f"\n{'#' * 60}")
    print(f"# ✅ 项目完成: {project_name}")
    print(f"{'#' * 60}")

    # 显示最终状态
    s = bus_status(project_name)
    print("\n📊 总线统计:")
    print(f"   总事件: {s['total_events']}")
    print(f"   参与Agent: {', '.join(s['agents_involved'])}")
    print(f"   阶段: {', '.join(s['stages'])}")

    return True


if __name__ == "__main__":
    project = sys.argv[1] if len(sys.argv) > 1 else "testglink"
    run(project)
