# SPDX-License-Identifier: MIT
"""Glink Daemon — 配置加载（从 glink-config.yaml / 环境变量 / 默认值）"""

import os

_CONFIG_INSTANCE: dict | None = None


def _load_yaml() -> dict:
    """从 glink-config.yaml 加载配置，不存在则返回空 dict"""
    paths = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "glink-config.yaml"),
        "glink-config.yaml",
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                import yaml

                with open(p) as f:
                    return yaml.safe_load(f) or {}
            except Exception:
                pass
    return {}


def load_config() -> dict:
    """加载完整配置（含环境变量覆盖）"""
    global _CONFIG_INSTANCE
    if _CONFIG_INSTANCE is not None:
        return _CONFIG_INSTANCE

    cfg = _load_yaml()

    # 环境变量覆盖
    env_overrides = {
        ("project", "default"): os.environ.get("GLINK_DEFAULT_PROJECT"),
        ("server", "port"): (int(os.environ["GLINK_PORT"]) if "GLINK_PORT" in os.environ else None),
        ("reporting", "channels"): (
            [{"type": os.environ["GLINK_REPORTER"]}] if "GLINK_REPORTER" in os.environ else None
        ),
    }

    for keys, env_val in env_overrides.items():
        if env_val is None:
            continue
        d = cfg
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = env_val

    # 飞书 webhook 环境变量
    webhook = os.environ.get("GLINK_ALERT_WEBHOOK", "")
    if webhook:
        channels = cfg.setdefault("reporting", {}).setdefault("channels", [])
        has_feishu = any(ch.get("type") == "feishu" for ch in channels)
        if not has_feishu:
            channels.append({"type": "feishu", "webhook": webhook, "label": "Glink"})

    _CONFIG_INSTANCE = cfg
    return cfg


def get_config(key: str, default=None):
    """嵌套路径获取配置值，如 get_config('scheduling.max_retries', 2)"""
    cfg = load_config()
    parts = key.split(".")
    d = cfg
    for p in parts:
        if isinstance(d, dict):
            d = d.get(p, {})
        else:
            return default
    return d if d != {} else default


def get_server_port() -> int:
    return get_config("server.port", 8426)


def get_reporter_config() -> dict | None:
    return load_config()


def get_default_project() -> str:
    return get_config("project.default", "testglink")


def get_max_retries() -> int:
    return get_config("scheduling.max_retries", 2)


def get_poll_interval() -> int:
    return get_config("scheduling.poll_interval", 3)


def get_poll_max_wait() -> int:
    return get_config("scheduling.poll_max_wait", 180)


def get_max_concurrent_steps() -> int:
    """获取并行模式最大并发步数（默认 4）"""
    return get_config("scheduling.max_concurrent_steps", 4)
