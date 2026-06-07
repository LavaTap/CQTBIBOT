"""命令处理工具函数 —— 回复、图片、文件转发等公共方法。

提供 qq_forward.py 和 monitor_forward.py 共享的消息回复工具。
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from core.onebot_client import OneBotClient

log = logging.getLogger("command_utils")


def reply(client: "OneBotClient", msg_type: str, group_id: int,
          user_id: int, text: str, log_cb: Callable | None = None) -> None:
    """发送文本回复。"""
    try:
        if msg_type == "group":
            client.send_group_msg(group_id, text)
        else:
            client.send_private_msg(user_id, text)
    except Exception as e:
        cb = log_cb or log.error
        cb(f"指令回复失败: {e}")


def reply_image(client: "OneBotClient", msg_type: str, group_id: int,
                user_id: int, img_bytes: bytes, caption: str = "",
                log_cb: Callable | None = None) -> None:
    """发送 base64 图片 + 可选文字。"""
    img_b64 = base64.b64encode(img_bytes).decode("ascii")
    message: list[dict] = [{"type": "image", "data": {"file": f"base64://{img_b64}"}}]
    if caption:
        message.append({"type": "text", "data": {"text": caption}})
    try:
        if msg_type == "group":
            client.send_group_msg(group_id, message)
        else:
            client.send_private_msg(user_id, message)
    except Exception as e:
        cb = log_cb or log.error
        cb(f"发送图片失败: {e}")


def forward_files(client: "OneBotClient", msg_type: str, group_id: int,
                  user_id: int, files: list[Path], sender_name: str,
                  admin_qq: int, log_cb: Callable | None = None) -> None:
    """以合并转发形式发送多个本地文件给用户。"""
    nodes: list[dict] = []
    for fp in files:
        if not fp.exists():
            continue
        file_uri = fp.resolve().as_uri()
        nodes.append({
            "type": "node",
            "data": {
                "name": sender_name,
                "uin": str(admin_qq),
                "content": [
                    {"type": "file", "data": {"file": file_uri, "name": fp.name}},
                ],
            },
        })
    if not nodes:
        return
    try:
        if msg_type == "group":
            client.send_group_forward_msg(group_id, nodes)
        else:
            client.send_private_forward_msg(user_id, nodes)
        cb = log_cb or log.info
        cb(f"合并转发文件成功: {len(nodes)} 个")
    except Exception as e:
        cb = log_cb or log.error
        cb(f"合并转发文件失败: {e}")


def forward_error(client: "OneBotClient", msg_type: str, group_id: int,
                  user_id: int, error_text: str, admin_qq: int) -> None:
    """以合并转发形式发送错误日志。"""
    nodes = [{
        "type": "node",
        "data": {
            "name": "错误日志",
            "uin": str(admin_qq),
            "content": [{"type": "text", "data": {"text": error_text}}],
        },
    }]
    try:
        if msg_type == "group":
            client.send_group_forward_msg(group_id, nodes)
        else:
            client.send_private_forward_msg(user_id, nodes)
    except Exception as e:
        log.error(f"转发错误日志失败: {e}")
