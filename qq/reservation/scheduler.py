"""预约提醒调度器。

轮询 ReservationDB.get_all()，在提前 1 分钟和报名开始时刻各 @ 一次。
仅提醒，不自动提交报名。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Callable

from qq.reservation.db import ReservationDB
from qq.reservation.time_parser import parse_apply_start

log = logging.getLogger(__name__)


class ReservationScheduler:
    """提前 1 分钟 + 到点各提醒一次；到点后自动移除预约。"""

    POLL_INTERVAL = 20  # 秒

    def __init__(
        self,
        *,
        send_callback: Callable[[str, int, int, list[dict]], None],
        log_callback: Callable[[str, str], None] | None = None,
    ) -> None:
        """
        send_callback(msg_type, group_id, qq, message_segments)：
            实际发送方。msg_type ∈ {'group','private'}。
        """
        self._send = send_callback
        self._log = log_callback or (lambda level, msg: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="ReservationScheduler", daemon=True
        )
        self._thread.start()
        self._log("info", "预约提醒调度器已启动")

    def stop(self) -> None:
        self._stop.set()
        self._log("info", "预约提醒调度器已停止")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                self._log("error", f"预约调度器轮询异常: {e}")
            self._stop.wait(self.POLL_INTERVAL)

    def _tick(self) -> None:
        reservations = ReservationDB().get_all()
        if not reservations:
            return

        now = datetime.now()
        for r in reservations:
            qq = int(r["qq"])
            aid = r["activity_id"]
            meta = r.get("meta") or {}
            apply_start_raw = meta.get("apply_start", "")
            name = meta.get("name") or aid
            notified = set(meta.get("notified") or [])
            msg_type = meta.get("msg_type") or "private"
            group_id = int(meta.get("group_id") or 0)

            dt = parse_apply_start(apply_start_raw)
            if dt is None:
                continue

            db = ReservationDB()

            if "pre1m" not in notified and dt - timedelta(minutes=1) <= now < dt:
                self._remind(msg_type, group_id, qq, aid, name, kind="pre1m", apply_dt=dt)
                notified.add("pre1m")
                db.patch_meta(qq=qq, activity_id=aid, patch={"notified": sorted(notified)})

            if "ontime" not in notified and now >= dt:
                self._remind(msg_type, group_id, qq, aid, name, kind="ontime", apply_dt=dt)
                db.remove(qq=qq, activity_id=aid)
                self._log("info", f"预约提醒已发送并移除: qq={qq} aid={aid}")

    def _remind(
        self, msg_type: str, group_id: int, qq: int, aid: str, name: str,
        *, kind: str, apply_dt: datetime,
    ) -> None:
        when_str = apply_dt.strftime("%m-%d %H:%M")
        if kind == "pre1m":
            tip = (
                f"【预约提醒】《{name}》将于 {when_str}（约 1 分钟后）开放报名，"
                f"准备发送 #报名 {aid}"
            )
        else:
            tip = (
                f"【预约提醒】《{name}》报名已开始（{when_str}），"
                f"发送 #报名 {aid} 立即报名"
            )
        segments: list[dict] = (
            [
                {"type": "at", "data": {"qq": str(qq)}},
                {"type": "text", "data": {"text": " " + tip}},
            ]
            if msg_type == "group" and group_id
            else [{"type": "text", "data": {"text": tip}}]
        )
        try:
            self._send(msg_type, group_id, qq, segments)
        except Exception as e:
            self._log("error", f"预约提醒发送失败 qq={qq} aid={aid} kind={kind}: {e}")