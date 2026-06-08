"""会话状态管理器 —— 管理多步对话（如 #登录 的学号→密码→验证码）。

每个用户的会话由 user_id（QQ号）索引，存储在全局 dict 中。
后台线程定时清理超时会话（5分钟无操作）。
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from typing import Any

log = logging.getLogger("user_session")


class SessionStep(enum.Enum):
    """会话步骤枚举，标识多步登录流程的当前阶段。"""
    IDLE = 0
    WAITING_STUDENT_ID = 1          # 等待用户输入学号
    WAITING_PASSWORD = 2            # 等待用户输入密码
    WAITING_CAPTCHA = 3             # 等待用户输入验证码（SSO登录）
    WAITING_QR_SCAN = 4             # 等待二维码被扫码
    WAITING_QR_CONFIRM = 5          # 等待扫码确认
    WAITING_UPDATE_CAPTCHA = 6      # 等待验证码（#更新 流程中重新登录）
    WAITING_PASSWORD_UPDATE_CAPTCHA = 7  # 等待验证码（#密码更新 流程中重新登录）
    WAITING_APPLY_CAPTCHA = 8        # 等待验证码（#报名 流程中输入活动报名验证码）
    WAITING_APPLY_ACTIVITY_ID = 9    # 等待输入活动 ID（#报名 流程，未带参数时）
    WAITING_SIGN_QR_IMAGE = 10       # 等待用户发送签到二维码图片（#扫码签到 流程）


class UserSession:
    """单个用户的会话状态。"""

    __slots__ = (
        "user_id", "step", "student_id", "password",
        "captcha_client", "acc_key", "last_active",
        "msg_type", "group_id",
        "activity_id", "apply_data",
    )

    def __init__(self, user_id: int, msg_type: str, group_id: int = 0) -> None:
        self.user_id = user_id
        self.step = SessionStep.IDLE
        self.student_id: str = ""
        self.password: str = ""
        self.captcha_client: Any = None   # JWGLClient 实例
        self.acc_key: str = ""
        self.last_active: float = time.time()
        self.msg_type: str = msg_type     # "group" or "private"
        self.group_id: int = group_id
        self.activity_id: str = ""        # #报名 活动ID
        self.apply_data: dict = {}        # #报名 所需的 s1/s2 等隐藏字段

    def touch(self) -> None:
        """更新最后活动时间。"""
        self.last_active = time.time()

    @property
    def is_expired(self, timeout: float = 300.0) -> bool:
        """会话是否超时（默认5分钟）。"""
        return time.time() - self.last_active > timeout

    @property
    def is_active(self) -> bool:
        """会话是否处于活跃状态（非 IDLE）。"""
        return self.step != SessionStep.IDLE


# ---------- 全局会话管理器 ----------
_sessions: dict[int, UserSession] = {}
_lock = threading.Lock()
_cleanup_running = False


def get_session(user_id: int) -> UserSession | None:
    """获取指定用户的会话。

    Args:
        user_id: 用户 QQ 号。

    Returns:
        UserSession 实例，不存在则返回 None。
    """
    with _lock:
        return _sessions.get(user_id)


def create_or_get_session(user_id: int, msg_type: str, group_id: int = 0) -> UserSession:
    """获取或创建用户会话。若已存在则更新 msg_type 和 group_id。

    Args:
        user_id: 用户 QQ 号。
        msg_type: 消息类型（"group" / "private"）。
        group_id: 群号（私聊时为 0）。

    Returns:
        UserSession 实例。
    """
    with _lock:
        s = _sessions.get(user_id)
        if s is None:
            s = UserSession(user_id, msg_type, group_id)
            _sessions[user_id] = s
        else:
            s.msg_type = msg_type
            s.group_id = group_id
        return s


def remove_session(user_id: int) -> None:
    """移除指定用户的会话。

    Args:
        user_id: 用户 QQ 号。
    """
    with _lock:
        _sessions.pop(user_id, None)


def cleanup_expired() -> int:
    """清理超时会话，返回清理数量。

    Returns:
        被清理的会话数量。
    """
    now = time.time()
    expired = []
    with _lock:
        for uid, s in _sessions.items():
            if s.is_expired:
                expired.append(uid)
        for uid in expired:
            del _sessions[uid]
    if expired:
        log.info("会话清理: 清理 %d 个超时会话", len(expired))
    return len(expired)


def _cleanup_loop() -> None:
    """后台线程：每60秒清理一次超时会话。"""
    global _cleanup_running
    if _cleanup_running:
        return
    _cleanup_running = True
    while True:
        time.sleep(60)
        try:
            cleanup_expired()
        except Exception as e:
            log.error("会话清理异常: %s", e)


def start_cleanup_thread() -> None:
    """启动会话清理后台线程（daemon 模式，每 60 秒清理一次）。"""
    t = threading.Thread(target=_cleanup_loop, daemon=True)
    t.start()
    log.info("会话清理线程已启动")
