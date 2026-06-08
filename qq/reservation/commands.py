"""#预约报名 / #我的预约 的业务逻辑。

向外暴露两个方法（handle_reserve / handle_my_reservations），
都不依赖具体 bot 框架——通过 reply(msg_type, group_id, user_id, text) 回写文本。

学号查找由调用方传入 (find_student_id)，因为 qq_forward 和 monitor_forward
取学号的途径不一样（UserDB vs find_account_by_qq）。
"""

from __future__ import annotations

import logging
import sqlite3
import traceback
from datetime import datetime
from typing import Callable

from qq.reservation.db import ReservationDB
from qq.reservation.time_parser import parse_apply_start

log = logging.getLogger(__name__)


Reply = Callable[[str, int, int, str], None]
"""reply(msg_type, group_id, user_id, text) → None"""

StudentLookup = Callable[[int], tuple[str, dict | None]]
"""student_lookup(qq) → (student_id, user_dict_or_None).
user_dict 用于后续 obtain_secondclass_session_from_user 调用。"""


class ReservationCommands:
    def __init__(self, *, reply: Reply, student_lookup: StudentLookup) -> None:
        self._reply = reply
        self._lookup_student = student_lookup

    # ──────────────── #预约报名 <活动ID> ────────────────

    def handle_reserve(
        self, msg_type: str, group_id: int, user_id: int, activity_id: str,
    ) -> None:
        try:
            student_id, user = self._lookup_student(user_id)
            if not student_id:
                self._reply(msg_type, group_id, user_id,
                            "未找到您的登录凭证，请先 #扫码登录 或 #登录")
                return

            name, apply_start, status_name = lookup_activity_meta(activity_id)

            # DB 拿不到 apply_start 或状态不是「未开始」→ 走 API 兜底
            if not apply_start or "未开始" not in (status_name or ""):
                ok = self._verify_via_api(msg_type, group_id, user_id, user, activity_id)
                if ok is False:
                    return
                if not apply_start:
                    name2, ap2, _ = lookup_activity_meta(activity_id)
                    name = name or name2
                    apply_start = apply_start or ap2

            if not apply_start:
                self._reply(msg_type, group_id, user_id,
                            f"活动 {activity_id} 缺少报名开始时间，无法预约。\n"
                            f"请先发送 #二课列表 或 #{activity_id} 拉取活动详情后再试。")
                return

            dt = parse_apply_start(apply_start)
            if dt is None:
                self._reply(msg_type, group_id, user_id,
                            f"无法解析报名开始时间「{apply_start}」")
                return
            if dt < datetime.now():
                self._reply(msg_type, group_id, user_id,
                            f"活动「{name or activity_id}」报名时间 {apply_start} 已过，"
                            "请直接 #报名")
                return

            ok = ReservationDB().add(
                qq=user_id, student_id=student_id, activity_id=activity_id,
                meta={
                    "name": name or activity_id,
                    "apply_start": apply_start,
                    "msg_type": msg_type,
                    "group_id": group_id if msg_type == "group" else 0,
                },
            )
            if ok:
                self._reply(msg_type, group_id, user_id,
                            f"✅ 预约成功：《{name or activity_id}》\n"
                            f"报名开始：{apply_start}\n"
                            "到点 / 提前 1 分钟会 @ 你提醒，发送 #我的预约 可查看")
            else:
                self._reply(msg_type, group_id, user_id,
                            f"您已预约过活动 {activity_id}，无需重复预约")
        except Exception as e:
            log.error("#预约报名 异常: aid=%s err=%s\n%s",
                      activity_id, e, traceback.format_exc())
            self._reply(msg_type, group_id, user_id, f"预约失败：{e}")

    def _verify_via_api(
        self, msg_type: str, group_id: int, user_id: int,
        user: dict | None, activity_id: str,
    ) -> bool | None:
        """向二课服务器确认状态。返回：
            None  → 状态确实是「报名未开始」，可以继续预约流程
            False → 不可预约，已经回复用户
        """
        if not user:
            self._reply(msg_type, group_id, user_id,
                        "未找到您的登录凭证，请先 #扫码登录 或 #登录")
            return False

        from secondclass.secondclass_tool import (
            ActivityApplyError, SecondClassAuthError,
            fetch_apply_page, obtain_secondclass_session_from_user,
        )
        self._reply(msg_type, group_id, user_id,
                    f"正在向二课服务器确认活动 {activity_id} 状态…")
        sess = obtain_secondclass_session_from_user(user)
        if not sess:
            self._reply(msg_type, group_id, user_id,
                        "二课凭证已过期，请重新 #扫码登录")
            return False
        try:
            fetch_apply_page(sess, activity_id)
        except ActivityApplyError as e:
            msg = str(e)
            if "报名未开始" not in msg:
                self._reply(msg_type, group_id, user_id, f"无法预约：{msg}")
                return False
        except SecondClassAuthError as e:
            self._reply(msg_type, group_id, user_id, str(e))
            return False
        return None

    # ──────────────── #我的预约 ────────────────

    def handle_my_reservations(self, msg_type: str, group_id: int, user_id: int) -> None:
        meta_all = ReservationDB().get_user(qq=user_id)
        if not meta_all:
            self._reply(msg_type, group_id, user_id, "您当前没有预约报名")
            return
        lines = [f"您的预约报名（{len(meta_all)} 个）："]
        for aid, m in meta_all.items():
            lines.append(
                f"  • [{aid}] {m.get('name', '')} — 报名开始 {m.get('apply_start', '')}"
            )
        lines.append("发送 #预约报名 <活动ID> 添加，到点会 @ 你")
        self._reply(msg_type, group_id, user_id, "\n".join(lines))


# ──────────────── DB 查询：活动元数据 ────────────────


def lookup_activity_meta(activity_id: str) -> tuple[str, str, str]:
    """查 master_v2 + detail_v3，返回 (name, apply_start, status_name)。"""
    from secondclass.secondclass_tool import USER_DB_FILE

    name = apply_start = status_name = ""
    try:
        conn = sqlite3.connect(str(USER_DB_FILE))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT activity_name, apply_start, status_name "
            "FROM second_class_master_v2 WHERE activity_id=? LIMIT 1",
            (activity_id,),
        ).fetchone()
        if row:
            name = row["activity_name"] or ""
            apply_start = row["apply_start"] or ""
            status_name = row["status_name"] or ""
        if not apply_start:
            drow = conn.execute(
                "SELECT activity_name, apply_time "
                "FROM second_class_activity_detail_v3 WHERE activity_id=? LIMIT 1",
                (activity_id,),
            ).fetchone()
            if drow:
                name = name or (drow["activity_name"] or "")
                apply_start = drow["apply_time"] or ""
        conn.close()
    except Exception as e:
        log.warning("查询活动元数据失败 aid=%s: %s", activity_id, e)
    return name, apply_start, status_name
