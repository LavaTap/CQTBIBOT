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
        """从 div.kbcontent 解析多门课。

        正方教务课表完整视图（div.kbcontent）每门课结构（按出现顺序）：
          <font>课程名</font><br>
          <font title='教师'>教师</font><br>
          <font title='周次(节次)'>2-4(周)[01-02-03节]</font><br>
          <font title='教学楼' name='jxlmc' style='display:none;'>【行知楼】</font>   ← 必须忽略
          <font title='教室'>行知楼2018汽车技术虚拟仿真实训室</font><br>
          ... 通知单、班级等隐藏字段 ...
        多门课之间用 21 个以上的短横线分隔（<br>---------------------<br>）。
        """
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
        """解析单个课程文本，返回 Course 对象。

        优先使用 <font title='...'> 结构化标签提取字段；找不到时降级到逐行文本解析。
        关键规则：
          - title='教室' 才是真正的教室（如「行知楼2018汽车技术虚拟仿真实训室」）
          - title='教学楼'（name='jxlmc'，display:none）是建筑名（如「【行知楼】」），必须忽略
          - title='周次(节次)' 的文本里同时含周次和节次，例如「2-4(周)[01-02-03节]」
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(text, "html.parser")

        teacher = ""
        room = ""
        weeks = ""
        period_start = 0
        period_end = 0
        name = ""

        # ── 优先按 <font title='...'> 结构化解析 ──
        fonts = soup.find_all("font")
        for f in fonts:
            title = (f.get("title") or "").strip()
            name_attr = (f.get("name") or "").strip()
            txt = f.get_text(strip=True)
            if not txt:
                continue

            # 隐藏字段（教学楼、通知单、班级、备注、学时数等）一律跳过
            if name_attr in ("jxlmc", "tzdbh", "wkxx", "ktmcstr", "bzstr", "xsks"):
                continue
            if title == "教学楼":
                continue

            if title == "教师":
                if not teacher:
                    teacher = txt
                continue
            if title == "教室":
                if not room:
                    room = txt
                continue
            if title == "周次(节次)":
                wm = re.search(r"(\d[\d,\-]*)\s*\(?周\)?", txt)
                if wm:
                    weeks = wm.group(1)
                pm = re.search(r"\[(\d{1,2})(?:-\d{1,2})*-(\d{1,2})节\]", txt)
                if pm:
                    period_start = int(pm.group(1))
                    period_end = int(pm.group(2))
                continue

            # 无 title 的 font 一般是课程名（第一个）
            if not name:
                name = txt

        # ── 找不到结构化标签时降级：按纯文本逐行 ──
        if not name or (not room and not teacher and not weeks):
            clean_text = soup.get_text(separator="\n")
            lines = [l.strip() for l in clean_text.split("\n") if l.strip()]
            if lines and not name:
                name = lines[0].strip()
            for line in lines[1:] if lines else []:
                line = line.strip()
                if not line:
                    continue
                if line == name or line.startswith("["):
                    continue
                # 跳过【】包裹的教学楼名（如【行知楼】、【桔园】）
                if re.match(r"^【[^】]+】$", line):
                    continue
                wm = re.search(r"(\d[\d,\-]*)\s*\(?周\)?", line)
                if wm and not weeks:
                    weeks = wm.group(1)
                pm = re.search(r"\[(\d{1,2})(?:-\d{1,2})*-(\d{1,2})节\]", line)
                if pm and not period_start:
                    period_start = int(pm.group(1))
                    period_end = int(pm.group(2))
                if wm or pm:
                    continue
                if not teacher:
                    teacher = line
                elif not room:
                    room = line

        if not name or name in ("&nbsp;", "&nbsp", " "):
            return None

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
