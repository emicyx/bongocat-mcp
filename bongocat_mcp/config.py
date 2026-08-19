"""统一配置层：环境变量 BONGOCAT_* > config.json > 内置默认值。

仪表盘编辑的即是 config.json（项目根目录，可用环境变量
BONGOCAT_MCP_CONFIG_FILE 指定其他路径）。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULTS: dict = {
    # "" = 自动探测；可强制 embedded / cdp / mver
    "driver": "",
    "host": "127.0.0.1",
    # cdp：BongoCat.exe / bongo-cat.exe 路径（单值与额外候选列表）
    "app_path": "",
    "app_paths": [],
    "cdp_port": 9223,
    # mver：皮肤目录（含 config.json）；mver_port 留空 None 表示从皮肤 config 读
    "mver_dir": "",
    "mver_port": None,
    # embedded：发现文件与端口/token 覆盖（留空走自动发现）
    "embedded_config": "",
    "embedded_port": None,
    "embedded_token": "",
    # 仪表盘
    "dashboard_host": "127.0.0.1",
    "dashboard_port": 8766,
}

# 配置键 -> 环境变量名（保持与旧版 mcp-server 兼容）
ENV_MAP = {
    "driver": "BONGOCAT_MCP_DRIVER",
    "host": "BONGOCAT_MCP_HOST",
    "app_path": "BONGOCAT_APP_PATH",
    "cdp_port": "BONGOCAT_CDP_PORT",
    "mver_dir": "BONGOCAT_MVER_DIR",
    "mver_port": "BONGOCAT_MVER_PORT",
    "embedded_config": "BONGOCAT_MCP_CONFIG",
    "embedded_port": "BONGOCAT_MCP_PORT",
    "embedded_token": "BONGOCAT_MCP_TOKEN",
    "dashboard_port": "BONGOCAT_DASHBOARD_PORT",
}

_lock = threading.RLock()  # 可重入：save() 内部会再调 _load_file()
_cache: dict | None = None


def _config_path() -> Path:
    override = os.environ.get("BONGOCAT_MCP_CONFIG_FILE")
    if override:
        return Path(override)
    return PROJECT_ROOT / "config.json"


def _load_file() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        data: dict = {}
        path = _config_path()
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                data = {}
        _cache = data
        return _cache


def _int_or_none(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get(key: str):
    """取配置：env > config.json > 默认值。端口类键返回 int 或 None。"""
    if key not in DEFAULTS:
        raise KeyError(key)

    env_name = ENV_MAP.get(key)
    if env_name and os.environ.get(env_name, "") != "":
        raw = os.environ[env_name]
        if key in ("cdp_port", "mver_port", "embedded_port", "dashboard_port"):
            return _int_or_none(raw) if raw != "" else None
        return raw

    file_value = _load_file().get(key)
    if file_value is not None and file_value != "":
        if key in ("cdp_port", "mver_port", "embedded_port", "dashboard_port"):
            return _int_or_none(file_value)
        return file_value

    return DEFAULTS[key]


def snapshot() -> dict:
    """当前生效配置全量（env 覆盖后的合并视图），供仪表盘展示。"""
    result = {}
    for key in DEFAULTS:
        value = get(key)
        if key in ("mver_port", "embedded_port") and value is None:
            value = ""
        result[key] = value
    return result


def file_config() -> dict:
    """仅 config.json 内容（仪表盘表单的底稿）。"""
    return dict(_load_file())


def save(updates: dict) -> dict:
    """合并写入 config.json 并刷新缓存。未知键忽略。"""
    global _cache
    known = {k: v for k, v in updates.items() if k in DEFAULTS}

    with _lock:
        data = dict(_load_file())
        for key, value in known.items():
            # 端口类空串归一为 null
            if key in ("mver_port", "embedded_port") and value == "":
                value = None
            data[key] = value

        path = _config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        _cache = data

    return data


def config_file_path() -> str:
    return str(_config_path())
