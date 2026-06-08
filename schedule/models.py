"""课表数据模型。"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("schedule.models")


class ScheduleError(Exception):
    """课表操作异常。"""
    pass


@dataclass
class Course:
    name: str = ""
    teacher: str = ""
    room: str = ""
    weeks: str = ""
    day: int = 0        # 1=周一 … 7=周日
    period_start: int = 0
    period_end: int = 0

    def week_matches(self, week_num: int) -> bool:
        """判断课程是否在指定周次。weeks 格式如 '2-5,7-9,15'。"""
        if not self.weeks:
            return True
        for part in self.weeks.split(","):
            part = part.strip()
            if "-" in part:
                lo, hi = part.split("-", 1)
                if lo.isdigit() and hi.isdigit() and int(lo) <= week_num <= int(hi):
                    return True
            elif part.isdigit() and int(part) == week_num:
                return True
        return False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "teacher": self.teacher,
            "room": self.room,
            "weeks": self.weeks,
            "day": self.day,
            "period_start": self.period_start,
            "period_end": self.period_end,
        }


@dataclass
class Schedule:
    semester: str = ""
    student_id: str = ""
    student_name: str = ""
    fetched_at: str = ""
    courses: list[Course] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "semester": self.semester,
            "student_id": self.student_id,
            "student_name": self.student_name,
            "fetched_at": self.fetched_at,
            "courses": [c.to_dict() for c in self.courses],
        }

    def save_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _max_week(self) -> int:
        """从课程周次推算最大周数。"""
        mx = 20
        for c in self.courses:
            for part in c.weeks.split(","):
                part = part.strip()
                if "-" in part:
                    hi = part.split("-", 1)[1]
                    if hi.isdigit():
                        mx = max(mx, int(hi))
                elif part.isdigit():
                    mx = max(mx, int(part))
        return mx

    def save_excel(self, path: Path, week_start: int = 0, week_end: int = 0) -> None:
        """导出标准单表课表 Excel。

        格式：横排星期（周一~周日），纵排大节（5大节），每个单元格=课程名+教师+教室+周次。
        支持 period_start=0 的课程（放在对应星期行，用无名节次标记）。
        """
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

        path.parent.mkdir(parents=True, exist_ok=True)
        wb = Workbook()
        ws = wb.active
        ws.title = self.student_id or "课表"

        # ── 样式 ──
        title_font = Font(bold=True, size=13)
        header_font = Font(bold=True, size=11, color="FFFFFF")
        header_fill = PatternFill("solid", fgColor="4472C4")
        period_font = Font(bold=True, size=10)
        period_fill = PatternFill("solid", fgColor="D6E4F0")
        name_font = Font(bold=True, size=10)
        data_font = Font(size=9)
        thin = Side(style="thin")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        center = Alignment(horizontal="center", vertical="center", wrap_text=True)
        left_top = Alignment(horizontal="left", vertical="top", wrap_text=True)

        # ── 标准大节定义 ──
        period_labels = [
            (1, 3, "第一节"),
            (4, 5, "第二节"),
            (6, 8, "第三节"),
            (9, 10, "第四节"),
            (11, 12, "第五节"),
        ]

        # 从课程实际 period_start 推算应该放在哪个大节
        def _period_group(ps: int, pe: int) -> tuple[int, int, str]:
            """根据起始节次返回 (group_ps, group_pe, label)。
            一门课只归属它起始的那个大节（如 1-5 节属第一节，不跨行显示）。
            """
            for g_ps, g_pe, label in period_labels:
                if g_ps <= ps <= g_pe or (ps < g_ps <= pe):
                    return (g_ps, g_pe, label)
            # period=0 → 无节次信息，归入第五节之后
            return (13, 14, "无节次")

        # ── 标题行 ──
        sid_info = f"{self.student_name}({self.student_id})" if self.student_name else self.student_id
        ws.merge_cells("A1:H1")
        title_cell = ws["A1"]
        title_cell.value = f"课表  |  {sid_info}  |  学期:{self.semester}  |  获取:{self.fetched_at[:10] if self.fetched_at else ''}"
        title_cell.font = title_font
        title_cell.alignment = center

        # ── 表头（第2行）──
        day_names = ["节次", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        for ci, name in enumerate(day_names, 1):
            cell = ws.cell(row=2, column=ci, value=name)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center
            cell.border = border

        # ── 数据行（第3行开始）──
        # 将所有课程按 (period_group_key, day) 分组
        group_map: dict[tuple[int, int, str], list[Course]] = {}
        for c in self.courses:
            if not c.name:
                continue
            g_ps, g_pe, label = _period_group(c.period_start, c.period_end)
            key = (g_ps, g_pe, label)
            group_map.setdefault(key, []).append(c)

        # 按大节顺序 + 天顺序排序输出
        row = 3
        for g_ps, g_pe, label in period_labels + [(13, 14, "无节次")]:
            key = (g_ps, g_pe, label)
            courses_in_group = group_map.get(key, [])
            # 按天排序
            by_day: dict[int, list[Course]] = {}
            for c in courses_in_group:
                by_day.setdefault(c.day, []).append(c)

            max_rows = max((len(by_day.get(d, [])) for d in range(1, 8)), default=0)
            if max_rows == 0:
                # 没有任何课程，但保留行作为占位
                cell = ws.cell(row=row, column=1, value=label)
                cell.font = period_font
                cell.fill = period_fill
                cell.alignment = center
                cell.border = border
                for day in range(1, 8):
                    ws.cell(row=row, column=day + 1).border = border
                row += 1
                continue

            for ei in range(max_rows):
                # 节次列（只在第一行显示大节名）
                if ei == 0:
                    cell = ws.cell(row=row, column=1, value=label)
                    cell.font = period_font
                    cell.fill = period_fill
                else:
                    cell = ws.cell(row=row, column=1, value="")
                cell.alignment = center
                cell.border = border

                for day in range(1, 8):
                    col = day + 1
                    day_courses = by_day.get(day, [])
                    course = day_courses[ei] if ei < len(day_courses) else None
                    cell = ws.cell(row=row, column=col)
                    cell.border = border
                    if course:
                        parts = [course.name]
                        if course.teacher:
                            parts.append(course.teacher)
                        if course.room:
                            parts.append(course.room)
                        if course.weeks:
                            parts.append(f"{course.weeks}(周)")
                        if course.period_start > 0:
                            parts.append(f"[{course.period_start}-{course.period_end}节]")
                        cell.value = "\n".join(parts)
                        cell.font = name_font
                        cell.alignment = left_top
                        ws.row_dimensions[row].height = max(
                            ws.row_dimensions[row].height or 30, len(parts) * 16
                        )
                    else:
                        cell.font = data_font
                        cell.alignment = center
                        cell.value = ""
                row += 1

        # ── 列宽 ──
        ws.column_dimensions["A"].width = 10
        for ci in range(2, 9):
            ws.column_dimensions[chr(64 + ci)].width = 22

        wb.save(path)
