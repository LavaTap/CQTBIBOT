"""OneBot v11 WebSocket 客户端。

提供正向 WebSocket 连接、消息收发、自动重连功能。
被 qq_forward.py 和 monitor_forward.py 共享使用。
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Callable

import websocket

log = logging.getLogger("onebot_client")


class OneBotError(Exception):
    """OneBot 协议/连接错误。"""
    pass


class OneBotClient:
    """OneBot v11 正向 WebSocket 客户端。

    用法：
        client = OneBotClient(ws_url, on_event)
        client.connect()
    """

    def __init__(self, ws_url: str, on_event: Callable[[dict], None],
                 access_token: str = "") -> None:
        self._ws_url = ws_url
        self._access_token = access_token
        self._on_event = on_event
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._callback: Callable[[str, str], None] | None = None

    def set_log_callback(self, cb: Callable[[str, str], None]) -> None:
        self._callback = cb

    def _log(self, level: str, msg: str) -> None:
        if self._callback:
            self._callback(level, msg)
        else:
            log.info("[%s] %s", level, msg)

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._running

    def connect(self) -> None:
        if self._running:
            return

        url = self._ws_url
        if self._access_token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}access_token={self._access_token}"

        self._ws = websocket.WebSocketApp(
            url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._running = True
        self._thread = threading.Thread(target=self._ws.run_forever, daemon=True)
        self._thread.start()

    def disconnect(self) -> None:
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._ws = None
        self._thread = None

    def send_group_msg(self, group_id: int, message: list[dict] | str) -> None:
        if not self._ws:
            raise OneBotError("WebSocket 未连接")
        params = {"group_id": group_id, "message": message}
        payload = {"action": "send_group_msg", "params": params}
        self._ws.send(json.dumps(payload))

    def send_private_msg(self, user_id: int, message: list[dict] | str) -> None:
        if not self._ws:
            raise OneBotError("WebSocket 未连接")
        params = {"user_id": user_id, "message": message}
        payload = {"action": "send_private_msg", "params": params}
        self._ws.send(json.dumps(payload))

    def forward_group_single_msg(self, group_id: int, message_id: int) -> None:
        """NapCat 群消息单条转发：将 message_id 转发到目标群。"""
        if not self._ws:
            raise OneBotError("WebSocket 未连接")
        params = {"group_id": group_id, "message_id": message_id}
        payload = {"action": "forward_group_single_msg", "params": params}
        self._ws.send(json.dumps(payload))

    def send_group_forward_msg(self, group_id: int, messages: list[dict]) -> None:
        if not self._ws:
            raise OneBotError("WebSocket 未连接")
        params = {"group_id": group_id, "messages": messages}
        payload = {"action": "send_group_forward_msg", "params": params}
        self._ws.send(json.dumps(payload))

    def send_private_forward_msg(self, user_id: int, messages: list[dict]) -> None:
        if not self._ws:
            raise OneBotError("WebSocket 未连接")
        params = {"user_id": user_id, "messages": messages}
        payload = {"action": "send_private_forward_msg", "params": params}
        self._ws.send(json.dumps(payload))

    def get_group_list(self) -> list[dict]:
        """获取 Bot 加入的群列表（同步 HTTP 调用）。"""
        import requests

        http_url = self._ws_url.replace("ws://", "http://").replace("wss://", "https://")
        resp = requests.get(f"{http_url}/get_group_list", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "ok":
            raise OneBotError(f"get_group_list 失败: {data}")
        return data.get("data", [])

    def delete_msg(self, message_id: int) -> None:
        if not self._ws:
            raise OneBotError("WebSocket 未连接")
        payload = {"action": "delete_msg", "params": {"message_id": message_id}}
        self._ws.send(json.dumps(payload))

    # ---- WebSocket 回调 ----

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        self._log("info", "已连接到 OneBot 服务")

    def _on_message(self, ws: websocket.WebSocketApp, raw: str) -> None:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return
        post_type = event.get("post_type")
        if post_type:
            if post_type == "meta_event":
                return
            detail = event.get("message_type") or event.get("notice_type") or ""
            group_id = event.get("group_id", "")
            self._log("info", f"收到事件: {post_type}/{detail} group={group_id}")
            try:
                self._on_event(event)
            except Exception as e:
                self._log("error", f"事件处理异常: {e}")

    def _on_error(self, ws: websocket.WebSocketApp, exc: Exception) -> None:
        self._log("error", f"WebSocket 错误: {exc}")

    def _on_close(self, ws: websocket.WebSocketApp, code: int, reason: str) -> None:
        self._running = False
        self._log("info", f"WebSocket 已断开 (code={code})")
