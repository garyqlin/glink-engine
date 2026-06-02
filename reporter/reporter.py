#!/usr/bin/env python3
"""
Glink Reporter — 渠道无关的 Session 抽象层

核心接口：ReportSession
  - push(message)   → 推情报（概述/状态更新）
  - alert(title)    → 推异常告警
  - summary(steps)  → 推 step 进展摘要

内置实现（当前）：
  - WebhookReporter → JSON to any webhook URL
  - ConsoleReporter → stdout (default fallback)
  - SilentReporter  → quiet (log only, no push)
  - MultiReporter   → multi-channel aggregation

待实现：
  - ZaguReporter    → push to agent session

配置方式（按优先级）：
  1. glink-config.yaml 的 reporting.channels 列表
  2. GLINK_REPORTER=webhook|console|silent
  3. 默认：ConsoleReporter
"""

import json
import logging
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime

log = logging.getLogger("glink.reporter")

# ── 卡片颜色 ────────────────────────────────────────────
C_OK = "green"
C_WARN = "yellow"
C_ERR = "red"
C_INFO = "blue"


# ══════════════════════════════════════════════════════════
# 抽象接口
# ══════════════════════════════════════════════════════════


class ReportSession(ABC):
    """汇报会话 — 渠道无关"""

    @abstractmethod
    def push(self, message: str) -> bool:
        """推一条情报文本"""
        ...

    @abstractmethod
    def alert(self, title: str, detail: str = "", severity: str = C_ERR) -> bool:
        _ = (detail, severity)
        """推一条告警"""
        ...

    def summary(
        self, project: str, step_index: int, total: int, status: str, agent: str, duration: str, detail: str = ""
    ) -> bool:
        """推一条 step 进展摘要"""
        bar = "█" * step_index + "░" * (total - step_index)
        msg = (
            f"📋 {project} · 步骤 {step_index}/{total}\n"
            f"   {bar}\n"
            f"   战甲：{agent}  |  状态：{status}  |  耗时：{duration}"
        )
        if detail:
            msg += f"\n   📝 {detail}"
        return self.push(msg)


# ══════════════════════════════════════════════════════════
# Webhook implementation
# ══════════════════════════════════════════════════════════

SEVERITY_COLORS = {
    C_OK: "green",
    C_WARN: "yellow",
    C_ERR: "red",
    C_INFO: "blue",
}


class WebhookReporter(ReportSession):
    """Generic webhook reporter (Feishu/Slack/Discord compatible)"""

    def __init__(self, webhook_url: str, session_label: str = ""):
        self.url = webhook_url
        self.label = session_label or "Glink"

    def push(self, message: str) -> bool:
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"📡 {self.label}"},
                    "template": "blue",
                },
                "elements": [
                    {"tag": "markdown", "content": message},
                    {"tag": "hr"},
                    {
                        "tag": "note",
                        "elements": [
                            {"tag": "plain_text", "content": f"Glink Reporter | {datetime.now().strftime('%H:%M:%S')}"}
                        ],
                    },
                ],
            },
        }
        return self._send(payload)

    def alert(self, title: str, detail: str = "", severity: str = C_ERR) -> bool:
        _ = (detail, severity)
        color = SEVERITY_COLORS.get(severity, "red")
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"⚠️ {self.label}: {title}"},
                    "template": color,
                },
                "elements": [
                    {"tag": "markdown", "content": detail or "无详细信息"},
                    {"tag": "hr"},
                    {
                        "tag": "note",
                        "elements": [
                            {"tag": "plain_text", "content": f"Glink Reporter | {datetime.now().strftime('%H:%M:%S')}"}
                        ],
                    },
                ],
            },
        }
        return self._send(payload)

    def _send(self, payload: dict) -> bool:
        try:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                self.url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read().decode()
                ok = json.loads(body).get("StatusCode", 1) == 0
                return ok
        except Exception as e:
            log.warning(f"Webhook send failed: {e}")
            return False


# ══════════════════════════════════════════════════════════
# 控制台实现
# ══════════════════════════════════════════════════════════


class ConsoleReporter(ReportSession):
    """标准输出（默认实现）"""

    def __init__(self, label: str = "Glink"):
        self.label = label

    def push(self, message: str) -> bool:
        ts = datetime.now().strftime("%H:%M:%S")
        banner = f"\n{'─' * 50}\n📡 [{ts}] {self.label}\n{'─' * 50}"
        print(f"{banner}\n{message}\n")
        return True

    def alert(self, title: str, detail: str = "", severity: str = C_ERR) -> bool:
        _ = (detail, severity)
        tag = {"green": "✅", "yellow": "⚠️", "red": "❌", "blue": "ℹ️"}.get(severity, "❌")
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"\n{tag} [{ts}] {self.label}: {title}")
        if detail:
            print(f"   {detail}")
        print()
        return True


# ══════════════════════════════════════════════════════════
# 静默实现
# ══════════════════════════════════════════════════════════


class SilentReporter(ReportSession):
    """静默模式 — 只打 DEBUG 日志，不推任何消息"""

    def push(self, message: str) -> bool:
        log.debug(f"[Silent] push: {message[:80]}...")
        return True

    def alert(self, title: str, detail: str = "", severity: str = C_ERR) -> bool:
        _ = (detail, severity)
        log.debug(f"[Silent] alert: {title}")
        return True


# ══════════════════════════════════════════════════════════
# 多路聚合 — 多个 Reporter 并联
# ══════════════════════════════════════════════════════════


class MultiReporter(ReportSession):
    """同时推送到多个渠道"""

    def __init__(self, reporters: list[ReportSession]):
        self.reporters = reporters

    def push(self, message: str) -> bool:
        results = [r.push(message) for r in self.reporters]
        return all(results)

    def alert(self, title: str, detail: str = "", severity: str = C_ERR) -> bool:
        _ = (detail, severity)
        results = [r.alert(title, detail, severity) for r in self.reporters]
        return all(results)

    def summary(
        self, project: str, step_index: int, total: int, status: str, agent: str, duration: str, detail: str = ""
    ) -> bool:
        results = [r.summary(project, step_index, total, status, agent, duration, detail) for r in self.reporters]
        return all(results)


# ══════════════════════════════════════════════════════════
# 工厂（从配置或环境变量自动创建）
# ══════════════════════════════════════════════════════════

DEFAULT_FEISHU_WEBHOOK = os.environ.get("GLINK_ALERT_WEBHOOK", "")


def create_reporter(config: dict | None = None) -> ReportSession:
    """
    按优先级创建 Reporter：
    1. config 中的 reporting.channels
    2. 环境变量 GLINK_REPORTER
    3. 默认 ConsoleReporter
    """
    reporters: list[ReportSession] = []

    # 1. 从 config 取渠道列表
    if config:
        channels = config.get("reporting", {}).get("channels", [])
        for ch in channels:
            t = ch.get("type", "")
            label = ch.get("session", ch.get("label", "Glink"))
            if t == "webhook":
                url = ch.get("webhook", "") or DEFAULT_FEISHU_WEBHOOK
                if url:
                    reporters.append(WebhookReporter(url, label))
                else:
                    log.warning("webhook channel: no webhook URL configured")
            elif t == "console":
                reporters.append(ConsoleReporter(label))
            elif t == "silent":
                reporters.append(SilentReporter())
            else:
                log.warning(f"未知渠道类型: {t}")

    # 2. 环境变量
    env_reporter = os.environ.get("GLINK_REPORTER", "").lower()
    if not reporters and env_reporter:
        if env_reporter == "webhook":
            if DEFAULT_FEISHU_WEBHOOK:
                reporters.append(WebhookReporter(DEFAULT_FEISHU_WEBHOOK))
            else:
                log.warning("GLINK_REPORTER=webhook but GLINK_ALERT_WEBHOOK not set")
        elif env_reporter == "silent":
            reporters.append(SilentReporter())
        else:
            reporters.append(ConsoleReporter())

    # 3. 默认 console
    if not reporters:
        reporters.append(ConsoleReporter())

    if len(reporters) == 1:
        return reporters[0]
    return MultiReporter(reporters)


# ══════════════════════════════════════════════════════════
# CLI 测试
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if "--test-webhook" in sys.argv:
        url = sys.argv[sys.argv.index("--test-webhook") + 1]
        r = WebhookReporter(url, "Glink Test")
        r.push("这是 Glink Reporter 抽象层的测试消息 ✅\n渠道无关的 Session 设计已就绪。")
        r.alert("API 测试", "这是来自 Glink Reporter 抽象层的告警测试", "red")
        r.summary("sandbox-builder", 5, 10, "✅ Done", "ink", "1m23s", "UI + score panel complete")
        print("✅ Webhook test sent")

    elif "--list" in sys.argv:
        r = ConsoleReporter("Glink 测试")
        r.push(
            "Available Reporter implementations:\n- ConsoleReporter (default)\n- WebhookReporter\n- SilentReporter\n- MultiReporter"}]}
        )
    else:
        r = ConsoleReporter()
        r.push(
            "Glink Reporter Layer v1.0\n\nUsage:\n  python3 reporter.py --test-webhook <webhook_url>\n  python3 reporter.py --list"
        )
