"""#签到 / #签退 的业务逻辑（共用同一端点 signOnTV.html）。

向外暴露两个方法（handle_sign_in / handle_sign_out），不依赖具体 bot 框架。
通过 reply(msg_type, group_id, user_id, text) 回写文本，
通过 student_lookup(qq) → (student_id, user_dict) 拿到二课登录所需的 user_dict。
"""

from __future__ import annotations

import logging
import traceback
from typing import Callable

log = logging.getLogger(__name__)


Reply = Callable[[str, int, int, str], None]
StudentLookup = Callable[[int], tuple[str, dict | None]]


class SignCommands:
    def __init__(self, *, reply: Reply, student_lookup: StudentLookup) -> None:
        self._reply = reply
        self._lookup_student = student_lookup

    def handle_sign_in(self, msg_type: str, group_id: int, user_id: int,
                       activity_id: str, channel_id: int = 5) -> None:
        self._do_sign(msg_type, group_id, user_id, activity_id,
                      sign_out=False, channel_id=channel_id)

    def handle_sign_out(self, msg_type: str, group_id: int, user_id: int,
                        activity_id: str, channel_id: int = 5) -> None:
        self._do_sign(msg_type, group_id, user_id, activity_id,
                      sign_out=True, channel_id=channel_id)

    def handle_scan_qr_image(self, msg_type: str, group_id: int, user_id: int,
                             image_bytes: bytes) -> None:
        """收到一张大屏二维码图片 → 解码 → 用 (channelID, rand) 直接打 signOnTV。"""
        from secondclass.qr_decode import decode_qr_image, parse_sign_qr

        try:
            text = decode_qr_image(image_bytes)
        except RuntimeError as e:
            self._reply(msg_type, group_id, user_id, str(e))
            return

        if not text:
            self._reply(msg_type, group_id, user_id,
                        "未识别到二维码内容，请重新拍清晰一些再发")
            return

        qr = parse_sign_qr(text)
        if not qr:
            self._reply(msg_type, group_id, user_id,
                        f"二维码内容不是二课签到链接：{text[:120]}")
            return

        action = "签退" if qr["sign_out"] else "签到"
        self._reply(msg_type, group_id, user_id,
                    f"识别到{action}二维码：活动 {qr['activity_id']}，"
                    f"渠道 {qr['channel_id']}，正在提交…")

        self._do_sign(
            msg_type, group_id, user_id, qr["activity_id"],
            sign_out=qr["sign_out"], channel_id=qr["channel_id"],
            rand=qr["rand"],
        )

    def _do_sign(self, msg_type: str, group_id: int, user_id: int,
                 activity_id: str, *, sign_out: bool, channel_id: int,
                 rand: str | None = None) -> None:
        action = "签退" if sign_out else "签到"
        try:
            student_id, user = self._lookup_student(user_id)
            if not student_id or not user:
                self._reply(msg_type, group_id, user_id,
                            "未找到您的登录凭证，请先 #扫码登录 或 #登录")
                return

            from secondclass.secondclass_tool import (
                ActivitySignError, SecondClassAuthError,
                obtain_secondclass_session_from_user, submit_sign,
            )

            self._reply(msg_type, group_id, user_id,
                        f"正在为活动 {activity_id} 提交{action}…")

            try:
                sess = obtain_secondclass_session_from_user(user)
            except SecondClassAuthError as e:
                self._reply(msg_type, group_id, user_id, str(e))
                return
            if not sess:
                self._reply(msg_type, group_id, user_id,
                            "二课凭证已过期，请重新 #扫码登录")
                return

            try:
                result = submit_sign(
                    sess, activity_id,
                    sign_out=sign_out, channel_id=channel_id, rand=rand,
                )
            except SecondClassAuthError as e:
                self._reply(msg_type, group_id, user_id, str(e))
                return
            except ActivitySignError as e:
                self._reply(msg_type, group_id, user_id,
                            f"{action}失败：{e}")
                return

            prefix = "✅" if result.get("success") else "⚠️"
            self._reply(msg_type, group_id, user_id,
                        f"{prefix} 活动 {activity_id} {action}：{result.get('message', '')}")
        except Exception as e:
            log.error("#%s 异常 aid=%s err=%s\n%s",
                      action, activity_id, e, traceback.format_exc())
            self._reply(msg_type, group_id, user_id, f"{action}失败：{e}")
