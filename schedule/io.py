"""课表文件 I/O —— 路径管理、JSON 加载/迁移。"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from schedule.models import Course, Schedule

log = logging.getLogger("schedule.io")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEDULE_DIR = PROJECT_ROOT / "schedules"


def _schedule_path(ext: str, student_id: str = "", semester: str = "") -> Path:
    """返回课表文件路径，按学号分子目录存储。

    格式: schedules/{student_id}/{semester}.{ext}
    例:   schedules/2403740/2025-2026-2.json
    """
    sid = student_id or "schedule"
    sem = semester or "current"
    subdir = SCHEDULE_DIR / sid
    subdir.mkdir(parents=True, exist_ok=True)
    return subdir / f"{sem}.{ext}"


def _migrate_schedule_files() -> None:
    """将旧格式 schedules/{sid}_{sem}.{ext} 迁移到 schedules/{sid}/{sem}.{ext}。"""
    if not SCHEDULE_DIR.exists():
        return
    for old_path in SCHEDULE_DIR.iterdir():
        if not old_path.is_file():
            continue
        name = old_path.name
        m = re.match(r"^(\d{7})_(\d{4}-\d{4}-\d)\.(json|xlsx)$", name)
        if not m:
            continue
        sid, sem, ext = m.group(1), m.group(2), m.group(3)
        new_path = SCHEDULE_DIR / sid / f"{sem}.{ext}"
        new_path.parent.mkdir(parents=True, exist_ok=True)
        if new_path.exists():
            continue
        try:
            old_path.rename(new_path)
            log.info("迁移: %s → %s", old_path, new_path)
        except OSError as e:
            log.warning("迁移失败: %s → %s: %s", old_path, new_path, e)


def _load_merged_schedule(json_path: Path) -> Schedule | None:
    """加载 base JSON，若存在 -new.json 则合并课程，返回合并后的 Schedule。

    不会修改磁盘上的文件，仅在内存中合并。
    """
    base = _load_schedule_from_json(json_path)
    if base is None:
        return None
    new_path = json_path.parent / (json_path.stem + "-new.json")
    if new_path.exists():
        new_sched = _load_schedule_from_json(new_path)
        if new_sched and new_sched.courses:
            base.merge(new_sched)
    return base


def _load_schedule_from_json(path: Path) -> Schedule | None:
    """从 JSON 文件加载 Schedule 对象。"""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    schedule = Schedule(
        semester=data.get("semester", ""),
        student_id=data.get("student_id", ""),
        student_name=data.get("student_name", ""),
        fetched_at=data.get("fetched_at", ""),
    )
    for c in data.get("courses", []):
        schedule.courses.append(Course(
            name=c.get("name", ""),
            teacher=c.get("teacher", ""),
            room=c.get("room", ""),
            weeks=c.get("weeks", ""),
            day=c.get("day", 0),
            period_start=c.get("period_start", 0),
            period_end=c.get("period_end", 0),
        ))
    return schedule
