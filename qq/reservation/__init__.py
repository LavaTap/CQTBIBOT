"""二课「#预约报名」独立模块。

对外暴露：
    ReservationDB           — 预约存储（复用 second_class_users 表的 reserved_* 列）
    ReservationScheduler    — 提前 1 分钟 + 到点 @ 提醒的后台调度器
    ReservationCommands     — #预约报名 / #我的预约 的处理逻辑
    parse_apply_start       — 报名开始时间的多格式解析

qq_forward.py / monitor_forward.py 只需注册指令和启停调度器，无需感知实现。
"""

from qq.reservation.db import ReservationDB
from qq.reservation.scheduler import ReservationScheduler
from qq.reservation.commands import ReservationCommands
from qq.reservation.time_parser import parse_apply_start

__all__ = [
    "ReservationDB",
    "ReservationScheduler",
    "ReservationCommands",
    "parse_apply_start",
]
