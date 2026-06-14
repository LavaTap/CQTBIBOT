"""schedule 包 — 课表工具

子模块：
  - models: Course, Schedule, ScheduleError
  - parser: ScheduleParser (HTML → Schedule)
  - jwgl_client: JWGLClient (教务 HTTP 客户端)
  - io: 文件 I/O (_schedule_path, _load_schedule_from_json, _migrate_schedule_files)

向后兼容：schedule_tool.py 从各子模块 re-export 所有公开符号。
"""
from schedule.models import Course, Schedule, ScheduleError
from schedule.parser import ScheduleParser
from schedule.jwgl_client import JWGLClient
from schedule.io import _schedule_path, _load_schedule_from_json, _load_merged_schedule, _migrate_schedule_files, SCHEDULE_DIR

__all__ = [
    "Course", "Schedule", "ScheduleError",
    "ScheduleParser", "JWGLClient",
    "_schedule_path", "_load_schedule_from_json", "_load_merged_schedule",
    "_migrate_schedule_files", "SCHEDULE_DIR",
]
