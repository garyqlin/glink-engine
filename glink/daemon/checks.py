# SPDX-License-Identifier: MIT
"""Glink Daemon — 自动恢复 + pidfile 管理"""

import json
import os
import subprocess
import sys
from datetime import datetime

from .log import log, log_err, log_ok, log_warn, send_alert

DAEMON_SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "glink-daemon.py"))
PIDFILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".glink-daemon.pid")
BOOT_TIMESTAMP = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".glink-boot.ts")
BUS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bus")
CHECKPOINT_FILE = ".checkpoint.json"


from bus import sanitize_project_name

SANITIZE = sanitize_project_name


def write_pidfile() -> None:
    pid = str(os.getpid())
    try:
        fd = os.open(PIDFILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        with os.fdopen(fd, "w") as f:
            f.write(pid)
    except FileExistsError:
        with open(PIDFILE) as f:
            old_pid = f.read().strip()
        if old_pid.isdigit():
            try:
                os.kill(int(old_pid), 0)
                log_warn(f"已有实例运行 (pid={old_pid})，退出")
                sys.exit(0)
            except OSError:
                pass
        with open(PIDFILE, "w") as f:
            f.write(pid)

    with open(BOOT_TIMESTAMP, "w") as f:
        f.write(datetime.now().isoformat())


def is_alive() -> bool:
    if not os.path.exists(PIDFILE):
        return False
    with open(PIDFILE) as f:
        pid = f.read().strip()
    if not pid.isdigit():
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def ensure_pid() -> None:
    write_pidfile()


def cleanup_pidfile() -> None:
    if os.path.exists(PIDFILE):
        os.remove(PIDFILE)
    if os.path.exists(BOOT_TIMESTAMP):
        os.remove(BOOT_TIMESTAMP)


def self_restart(project: str, force: bool = False) -> None:
    log_warn("⚠ 自动恢复：准备重启自身...")

    resume_step = None
    if not force:
        safe_project = SANITIZE(project)
        ck_path = os.path.join(BUS_DIR, "projects", f"{safe_project}_{CHECKPOINT_FILE}")
        if os.path.exists(ck_path):
            with open(ck_path) as f:
                ck = json.load(f)
            si = ck.get("step_index", -1)
            if si >= 0:
                resume_step = si + 1
                ck_name = ck.get("title", f"step_{si}")
                log(f"   上次完成: step-{si} ({ck_name})，从 step-{resume_step} 续跑")
            else:
                log("   无有效 checkpoint，从头开始")
        else:
            log("   无 checkpoint 文件，从头开始")

    send_alert(
        "Daemon 自动恢复",
        f"**项目**: {project}\n**force**: {force}\n**恢复步**: {'step-' + str(resume_step) if resume_step is not None else '从头'}\n**pid**: {os.getpid()}\n**脚本**: {DAEMON_SCRIPT}",
    )

    cmd = [sys.executable, DAEMON_SCRIPT]
    if force:
        cmd.append("--force")
    if resume_step is not None:
        cmd.append(f"--step={resume_step}")
    if project:
        cmd.append(project)
    log(f"   重启命令: {' '.join(cmd)}")
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        log_err(f"新进程启动失败: {exc}")
        log("本进程保持运行，请手动检查 DAEMON_SCRIPT 路径")
        return
    log_ok("已启动新进程，当前进程即将退出")
    cleanup_pidfile()
    sys.exit(0)
