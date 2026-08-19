"""自编译版 BongoCat（内置控制通道，见 BongoCat 项目 src-tauri/src/core/mcp.rs）的 driver。

行为与旧版 server.py 的 HTTP 客户端完全一致：
  - 从 <APPDATA>/com.ayangweb.BongoCat/mcp-server.json 发现端口与 token
  - POST /api/<cmd>，Bearer 鉴权
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from bongocat_mcp import config

from .base import ALL_COMMANDS, CatDriver, DriverError

DISCOVERY_FILE_NAME = "mcp-server.json"
DEFAULT_HOST = "127.0.0.1"


class EmbeddedHttpDriver(CatDriver):
    name = "embedded-http"

    def __init__(self) -> None:
        self._config: dict | None = None

    # ---- 配置发现 ----

    @staticmethod
    def _discovery_path() -> Path | None:
        override = config.get("embedded_config")
        if override:
            return Path(override)

        base = os.environ.get("BONGOCAT_CONFIG_DIR")
        if not base:
            apd = os.environ.get("APPDATA")
            base = os.path.join(apd, "com.ayangweb.BongoCat") if apd else str(Path.home())

        return Path(base) / DISCOVERY_FILE_NAME

    def _load_config(self, *, refresh: bool = False) -> dict:
        if self._config is not None and not refresh:
            return self._config

        host = config.get("host") or DEFAULT_HOST

        token = config.get("embedded_token")
        port = config.get("embedded_port")

        if token and port:
            self._config = {"host": host, "port": int(port), "token": token}
            return self._config

        path = self._discovery_path()

        if path and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._config = {"host": host, "port": int(data["port"]), "token": data["token"]}
                return self._config
            except (OSError, ValueError, KeyError) as exc:
                raise DriverError(f"读取 BongoCat 控制服务配置失败: {path} ({exc})") from exc

        raise DriverError(
            f"找不到 BongoCat 控制服务配置文件 {path}。"
            "请先启动带控制通道的 BongoCat 版本（会在启动时生成该文件）。"
        )

    # ---- 探测 ----

    @classmethod
    def probe(cls) -> bool:
        """发现文件存在且 ping 通才认为可用。"""
        try:
            return cls()._request("ping", {}) .get("ok") is True
        except DriverError:
            return False

    # ---- HTTP ----

    def _request(self, cmd: str, payload: dict | None = None) -> dict:
        config = self._load_config()

        url = f"http://{config['host']}:{config['port']}/api/{cmd}"

        data = json.dumps(payload or {}).encode("utf-8")

        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config['token']}",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise DriverError(f"BongoCat 控制服务返回 {exc.code}: {body}") from exc
        except OSError as exc:
            self._config = None  # 下次调用重新发现
            raise DriverError(
                f"无法连接 BongoCat 控制服务（{config['host']}:{config['port']}）：{exc}"
            ) from exc

    # ---- CatDriver ----

    def capabilities(self) -> set[str]:
        return set(ALL_COMMANDS)

    def call(self, cmd: str, payload: dict) -> dict:
        return self._request(cmd, payload)
