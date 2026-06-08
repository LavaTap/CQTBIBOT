"""课表 HTML 解析器 —— 从正方教务系统 xskb_list.do 返回的 HTML 解析课表。

正方教务课表 HTML 特点：
  - <table id="kbtable"> 主表格
  - 表头含 "星期一"~"星期日" 列
  - 每个 <td> 含多个隐藏 <div>：
      - div.kbcontent1：简化视图（课程名+周次+教室，无教师）
      - div.kbcontent（第二个）：完整视图（含教师、教学楼、教室、通知单等）
  - 必须只解析 div.kbcontent（完整视图），否则字段会错位
  - 单元格内多门课用连续短横线分隔
  - 每门课格式：课程名 / 教师 / 周次[节次] / 教学楼(隐藏) / 教室 / 通知单(隐藏) / 班级(隐藏) / 备注(隐藏)
  - 周次格式如 "2-5,7-9,15(周)"，节次如 "[04-05节]"
  - rowspan 表示跨行（跨大节）
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from schedule.models import Course, Schedule

log = logging.getLogger("schedule.parser")

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ScheduleParser:
    """从正方教务系统课表 HTML 解析结构化课表数据。"""

    DAY_MAP = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7}

    @staticmethod
    def parse(html: str, semester: str = "", student_id: str = "",
              student_name: str = "") -> Schedule:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        schedule = Schedule(
            semester=semester,
            student_id=student_id,
            student_name=student_name,
            fetched_at=datetime.now().isoformat(timespec="seconds"),
        )

        debug_path = PROJECT_ROOT / "_xskb_debug.html"
        debug_path.write_text(html, encoding="utf-8")
        log.info("parse: 已保存调试 HTML → %s (%dB)", debug_path, len(html))

        table = (
            soup.find("table", id="kbtable")
            or soup.find("table", id=re.compile(r"kb", re.I))
            or soup.find("table", class_=re.compile(r"kb|Nsb_r"))
        )
        if not table:
            tables = soup.find_all("table")
            for t in tables:
                if len(t.find_all("td")) > 20:
                    table = t
                    break
        if not table:
            log.warning("parse: 未找到课表表格")
            return schedule

        header_row = table.find("tr")
        day_for_col: dict[int, int] = {}
        if header_row:
            for col_idx, cell in enumerate(header_row.find_all(["td", "th"])):
                text = cell.get_text(strip=True)
                for key, day_num in ScheduleParser.DAY_MAP.items():
                    if f"星期{key}" in text or f"周{key}" in text:
                        day_for_col[col_idx] = day_num
                        break
        log.info("parse: day_for_col=%s", day_for_col)

        rows_html = table.find_all("tr")
        seen: set[tuple[int, int]] = set()
        for row_idx, tr in enumerate(rows_html):
            if row_idx == 0:
                continue
            col_idx = 0
            for cell in tr.find_all(["td", "th"]):
                while (row_idx, col_idx) in seen:
                    col_idx += 1
                if col_idx not in day_for_col:
                    colspan = int(cell.get("colspan", 1))
                    rowspan = int(cell.get("rowspan", 1))
                    for dc in range(colspan):
                        for dr in range(rowspan):
                            seen.add((row_idx + dr, col_idx + dc))
                    col_idx += colspan
                    continue
                day = day_for_col[col_idx]
                full_divs = cell.find_all("div", class_="kbcontent")
                if full_divs:
                    courses = ScheduleParser._parse_div(full_divs[0], day)
                else:
                    cell_text = cell.get_text("\n", strip=True)
                    courses = ScheduleParser._parse_cell(cell_text, day)
                schedule.courses.extend(courses)
                colspan = int(cell.get("colspan", 1))
                rowspan = int(cell.get("rowspan", 1))
                for dc in range(colspan):
                    for dr in range(rowspan):
                        seen.add((row_idx + dr, col_idx + dc))
                col_idx += colspan

        if not schedule.semester:
            schedule.semester = ScheduleParser._extract_semester(html)

        seen_courses: set[tuple] = set()
        unique: list[Course] = []
        for c in schedule.courses:
            key = (c.name, c.teacher, c.weeks, c.day, c.period_start, c.period_end, c.room)
            if key not in seen_courses:
                seen_courses.add(key)
                unique.append(c)
        schedule.courses = unique

        log.info("parse: 共解析 %d 门课", len(schedule.courses))
        return schedule

    @staticmethod
    def _parse_div(div, day: int) -> list[Course]:
        """从 div.kbcontent 解析多门课。"""
        html_str = str(div)
        parts = re.split(r"(?:——)+|[-]{4,}|—{3,}", html_str)
        courses = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            course = ScheduleParser._parse_one_course(part, day)
            if course:
                courses.append(course)
        return courses

    @staticmethod
    def _parse_cell(text: str, day: int) -> list[Course]:
        """从纯文本单元格解析课程（降级方案）。"""
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        courses = []
        current: list[str] = []
        for line in lines:
            if re.match(r"^[─—\-]{2,}$", line):
                if current:
                    c = ScheduleParser._parse_one_course("\n".join(current), day)
                    if c:
                        courses.append(c)
                    current = []
            else:
                current.append(line)
        if current:
            c = ScheduleParser._parse_one_course("\n".join(current), day)
            if c:
                courses.append(c)
        return courses

    @staticmethod
    def _parse_one_course(text: str, day: int) -> Course | None:
        """解析单个课程文本，返回 Course 对象。"""
        from bs4 import BeautifulSoup

        # 去除 HTML 标签，提取纯文本
        soup = BeautifulSoup(text, "html.parser")
        clean_text = soup.get_text(separator="\n")
        lines = [l.strip() for l in clean_text.split("\n") if l.strip()]
        if not lines:
            return None
        name = lines[0].strip()
        if not name or name in ("&nbsp;", "&nbsp", " "):
            return None
        teacher = ""
        weeks = ""
        room = ""
        period_start = 0
        period_end = 0

        # ── 分离教师、周次、节次、教室 ──
        # 正方课表 HTML 每行格式：课程名 / 教师 / 周次[节次] / ... / 教室 / ...
        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue
            # 周次: "17" 或 "2-5,7-9,15(周)"
            wm = re.search(r"(\d[\d,\-]*)\s*\(?(周|周次|week)\)?", line)
            if wm:
                weeks = wm.group(1)
                continue  # 周次行不参与 teacher/room 匹配

            # 节次: "[01-02-03-04-05节]" → period_start=1, period_end=5
            pm = re.search(r"\[(\d{2})(?:-\d{2})*-(\d{2})节\]", line)
            if pm:
                period_start = int(pm.group(1))
                period_end = int(pm.group(2))
                continue  # 节次行不参与 teacher/room 匹配

            if line == name or line.startswith("["):
                continue

            if not teacher:
                teacher = line
            elif not room:
                room = line
            # 后续行（通知单、班级等）不参与 room，保留第一次设置的 room

        return Course(
            name=name.strip(),
            teacher=teacher.strip(),
            room=room.strip(),
            weeks=weeks.strip(),
            day=day,
            period_start=period_start,
            period_end=period_end,
        )

    @staticmethod
    def _extract_semester(html: str) -> str:
        """从课表 HTML 提取当前学期。"""
        m = re.search(r"xnxq01id['\"]?\s*(?:value|:)\s*['\"]?(\d{4}-\d{4}-\d)", html)
        if m:
            return m.group(1)
        m = re.search(r"(\d{4}-\d{4}-\d)", html)
        if m:
            return m.group(1)
        return ""
