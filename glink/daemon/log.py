# SPDX-License-Identifier: MIT
"""Glink Daemon — 日志 + Reporter 初始化"""

import os
import sys
import threading
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

REPORTER_DIR = os.path.join(os.path.dirname(BASE_DIR), "reporter")
if REPORTER_DIR not in sys.path:
    sys.path.insert(0, REPORTER_DIR)

# lazy import to avoid circular
_reporter = None
_reporter_lock = threading.Lock()


def log(msg: str, tag: str = "  ") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}]{tag} {msg}")


def log_step(msg: str) -> None:
    log(msg, "━━ ")


def log_ok(msg: str) -> None:
    log(msg, " ✅")


def log_err(msg: str) -> None:
    log(msg, " ❌")


def log_warn(msg: str) -> None:
    log(msg, " ⚠️ ")


def log_retry(msg: str) -> None:
    log(msg, " ↻ ")


def get_reporter():
    from reporter import create_reporter

    global _reporter
    with _reporter_lock:
        if _reporter is not None:
            return _reporter
        config_path = os.path.join(BASE_DIR, "glink-config.yaml")
        config = None
        if os.path.exists(config_path):
            try:
                import yaml

                with open(config_path) as f:
                    config = yaml.safe_load(f)
            except Exception:
                pass
        _reporter = create_reporter(config)
        return _reporter


def send_alert(title, message):
    from reporter import C_ERR, C_WARN

    reporter = get_reporter()
    sev = C_ERR if "失败" in title or "error" in title.lower() else C_WARN
    return reporter.alert(title, message, severity=sev)


# ── 颜色常量（reporter 引用） ──────────────────────────
C_OK = "green"
C_WARN = "yellow"
C_ERR = "red"
