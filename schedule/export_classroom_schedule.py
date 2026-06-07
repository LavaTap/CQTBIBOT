"""导出教室课表（kbxx_classroom_ifr）为 Excel，每周一个 sheet。

格式参照手动课表：竖排星期，横排大节，内容=教室|教师|班级|课程

用法：
  1. 配置 COOKIE_STR（替换为你的有效 cookie）
  2. python export_classroom_schedule.py

可选参数：
  python export_classroom_schedule.py --week 5    仅导出第5周
  python export_classroom_schedule.py --weeks 1-10 导出第1到10周
  python export_classroom_schedule.py --title "汽车实训室"  自定义标题中的场所名称
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests
import urllib3
from bs4 import BeautifulSoup, Tag

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("export_cr")

# ════════════════════ 配置 ════════════════════
COOKIE_STR = (
    "bzb_jsxsd=496E26D764C242103CB9978FA38CCDBF; "
    "SF_cookie_60=21348339; SF_cookie_231=21754675; "
    "bzb_njw=3B2006B41A355B6F769CBED3FB9302C6"
)

PARAMS = {
    "xnxqh": "2025-2026-2",
    "kbjcmsid": "28B3B1840433431B8148697B1A53F5D4",
    "skyx": "D08185960E544F11A87535728B8FF037",
    "xqid": "2",
    "jzwid": "", "jxlvalue": "", "skjsid": "", "skjs": "", "jsid": "",
    "zc1": "", "zc2": "",
    "skxq1": "", "skxq2": "",
    "jc1": "", "jc2": "",
}

URL = "https://jwgl.cqtbi.edu.cn:81/jsxsd/kbcx/kbxx_classroom_ifr"
REFERER = "https://jwgl.cqtbi.edu.cn:81/jsxsd/kbcx/kbxx_classroom"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

OUTPUT_DIR = PROJECT_ROOT / "schedules"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
    ),
    "Origin": "https://jwgl.cqtbi.edu.cn:81",
    "Referer": REFERER,
    "Content-Type": "application/x-www-form-urlencoded",
}

PERIOD_LABELS = ["1-3节", "4-5节", "6-8节", "9-10节", "11-12节"]
DAY_NAMES = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-5s | %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stderr)


# ════════════════════ 请求 ════════════════════
def fetch_html(cookie_str: str = COOKIE_STR) -> str:
    sess = requests.Session()
    sess.trust_env = False
    sess.headers.update(HEADERS)
    for item in cookie_str.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            sess.cookies.set(k.strip(), v.strip())

    log.info("POST %s", URL)
    r = sess.post(URL, data=PARAMS, timeout=30, verify=False)
    r.raise_for_status()
    log.info("响应 %dB", len(r.content))

    debug_path = PROJECT_ROOT / "_classroom_ifr_debug.html"
    debug_path.write_bytes(r.content)
    log.info("调试 HTML → %s", debug_path)
    return r.text


# ════════════════════ 数据结构 ════════════════════

@dataclass
class CourseEntry:
    """一门课在某个教室/天/大节的完整信息。"""
    classroom: str = ""
    teacher: str = ""
    class_info: str = ""
    course_name: str = ""
    weeks: list[int] = field(default_factory=list)


@dataclass
class DaySlot:
    """某天某大节的所有课程条目列表。"""
    day: int          # 1=周一 … 7=周日
    period: int       # 1~5 大节
    entries: list[CourseEntry] = field(default_factory=list)


# ════════════════════ 解析 ════════════════════

def parse_html(html: str) -> list[DaySlot]:
    """解析教室课表 HTML，返回按天/大节组织的课程数据。"""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="timetable")
    if not table:
        log.error("未找到 id='timetable' 的表格")
        return []

    rows = table.find_all("tr")
    if len(rows) < 3:
        return []

    # 收集所有 slot: { (day, period) → [CourseEntry, ...] }
    slot_map: dict[tuple[int, int], list[CourseEntry]] = {}

    for tr in rows[2:]:
        cells = tr.find_all(["td", "th"], recursive=False)
        if not cells:
            continue
        classroom = cells[0].get_text(strip=True)
        if not classroom:
            continue

        for ci, cell in enumerate(cells[1:], 1):
            cell_text = cell.get_text("\n", strip=True)
            if not cell_text or cell_text in ("010203", "0405", "060708", "0910", "1112"):
                continue

            day = (ci - 1) // 5          # 0~6
            period = (ci - 1) % 5        # 0~4
            key = (day, period)

            courses_raw = re.split(r"\n-{3,}\n|\n{2,}", cell_text)
            for raw in courses_raw:
                raw = raw.strip()
                if not raw or raw in ("010203", "0405", "060708", "0910", "1112"):
                    continue
                entry = _parse_one_course(raw, classroom)
                if entry:
                    slot_map.setdefault(key, []).append(entry)

    # 转为 DaySlot 列表
    result: list[DaySlot] = []
    for (d, p), entries in sorted(slot_map.items()):
        result.append(DaySlot(day=d + 1, period=p + 1, entries=entries))

    log.info("共解析 %d 个课程槽位", sum(len(s.entries) for s in result))
    return result


def _parse_one_course(text: str, classroom: str) -> CourseEntry | None:
    """解析单个单元格文本，提取课程名/教师/班级/周次。"""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        return None

    entry = CourseEntry()
    weeks_str = ""

    for line in lines:
        # 周次信息: (2-6周) 或 (2-6,8-10周) — 先提取并移除
        m = re.search(r"[（(]([\d\-,]+)周[)）]", line)
        if m:
            weeks_str = m.group(1)
            entry.weeks = _expand_weeks(weeks_str)
            line_clean = re.sub(r"[（(][\d\-,]+周[)）]", "", line).strip()
        else:
            line_clean = line

        # 班级信息: 24新能源1班 等
        if not entry.class_info and re.match(r"^\d{2}", line_clean) and "班" in line_clean:
            entry.class_info = line_clean
            continue

        # 第一行通常为课程名
        if not entry.course_name:
            entry.course_name = line
            continue

        # 2-6 字中文 → 教师
        if not entry.teacher and re.match(r"^[\u4e00-\u9fff·,\s]{1,8}$", line.replace(" ", "")):
            entry.teacher = line
            continue

        # 用清理后的文本再试 class_info
        if not entry.class_info and line_clean and len(line_clean) > 2:
            entry.class_info = line_clean

    if not entry.course_name:
        return None

    # 从 classroom 提取短编号（如 "行知楼1004-1" → "1004-1"）
    entry.classroom = re.sub(r"^[\u4e00-\u9fff]+", "", classroom).strip()
    if not entry.classroom:
        entry.classroom = classroom

    return entry


def _expand_weeks(week_str: str) -> list[int]:
    result: list[int] = []
    for part in week_str.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            if a.isdigit() and b.isdigit():
                result.extend(range(int(a), int(b) + 1))
        elif part.isdigit():
            result.append(int(part))
    return sorted(set(result))


# ════════════════════ Excel 导出 ════════════════════

def export_excel(slots: list[DaySlot], output_path: Path,
                 min_week: int = 0, max_week: int = 0,
                 title_place: str = "实训室") -> None:
    """导出为竖星期、横大节、内容=教室|教师|班级|课程的格式，每周一个 sheet。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)

    # 收集所有周次
    all_weeks: set[int] = set()
    for slot in slots:
        for e in slot.entries:
            all_weeks.update(e.weeks)
    weeks_sorted = sorted(w for w in all_weeks
                          if (min_week <= 0 or w >= min_week)
                          and (max_week <= 0 or w <= max_week))

    if not weeks_sorted:
        log.warning("没有数据可导出")
        return

    # 预计算: {(week, day, period) → [CourseEntry, ...]}
    week_day_period: dict[tuple[int, int, int], list[CourseEntry]] = {}
    for slot in slots:
        for e in slot.entries:
            for w in e.weeks:
                if w in weeks_sorted:
                    week_day_period.setdefault((w, slot.day, slot.period), []).append(e)

    # 样式
    title_font = Font(bold=True, size=14)
    period_font = Font(bold=True, size=10, color="FFFFFF")
    period_fill = PatternFill("solid", fgColor="4472C4")
    sub_header_font = Font(bold=True, size=9)
    sub_header_fill = PatternFill("solid", fgColor="D6E4F0")
    day_font = Font(bold=True, size=10)
    data_font = Font(size=9)
    thin = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # 列布局：每大节 4 列：教室 | 教师 | 班级 | 课程
    SUB_COLS = ["教室", "教师", "班级", "课程"]
    COL_COUNT = 1 + 5 * 4  # 星期列 + 5大节×4列

    for week in weeks_sorted:
        ws = wb.create_sheet(title=f"第{week}周")
        semester = PARAMS.get("xnxqh", "")

        # Row 1: 标题
        title = f"重庆工商职业学院{title_place}{week}周教室课表"
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=COL_COUNT)
        cell = ws.cell(row=1, column=1, value=title)
        cell.font = title_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

        # Row 2: 大节表头
        ws.cell(row=2, column=1, value="节次\\星期").font = period_font
        ws.cell(row=2, column=1).fill = period_fill
        ws.cell(row=2, column=1).alignment = center
        ws.cell(row=2, column=1).border = border
        col = 2
        for pi, label in enumerate(PERIOD_LABELS):
            ws.merge_cells(start_row=2, start_column=col,
                           end_row=2, end_column=col + 3)
            cell = ws.cell(row=2, column=col, value=label)
            cell.font = period_font
            cell.fill = period_fill
            cell.alignment = center
            for dc in range(4):
                ws.cell(row=2, column=col + dc).border = border
            col += 4

        # Row 3: 子表头（星期 + 各列的教室/教师/班级/课程）
        ws.cell(row=3, column=1, value="星期").font = sub_header_font
        ws.cell(row=3, column=1).fill = sub_header_fill
        ws.cell(row=3, column=1).alignment = center
        ws.cell(row=3, column=1).border = border
        col = 2
        for _ in range(5):
            for sc in SUB_COLS:
                cell = ws.cell(row=3, column=col, value=sc)
                cell.font = sub_header_font
                cell.fill = sub_header_fill
                cell.alignment = center
                cell.border = border
                col += 1

        # Row 4+: 数据行
        row = 4
        for di, day_name in enumerate(DAY_NAMES, 1):
            # 收集这一天有课的所有教室
            day_courses: list[tuple[int, list[CourseEntry | None]]] = []
            for pi in range(1, 6):
                entries = week_day_period.get((week, di, pi), [])
                day_courses.append((pi, entries))

            # 确定这一天的最大行数（所有大节中课程条目数的最大值）
            max_entries = max((len(entries) for _, entries in day_courses), default=0)
            if max_entries == 0:
                continue

            for ei in range(max_entries):
                # 星期列（只在第一行显示）
                if ei == 0:
                    cell = ws.cell(row=row, column=1, value=day_name)
                    cell.font = day_font
                else:
                    cell = ws.cell(row=row, column=1, value="")
                cell.alignment = center
                cell.border = border

                col = 2
                for _, entries in day_courses:
                    entry = entries[ei] if ei < len(entries) else None
                    if entry:
                        vals = [entry.classroom, entry.teacher,
                                entry.class_info, entry.course_name]
                    else:
                        vals = ["", "", "", ""]
                    for v in vals:
                        cell = ws.cell(row=row, column=col, value=v)
                        cell.font = data_font
                        cell.alignment = Alignment(
                            horizontal="center", vertical="center", wrap_text=True)
                        cell.border = border
                        col += 1
                row += 1

        # 列宽
        ws.column_dimensions["A"].width = 8
        for ci in range(2, COL_COUNT + 1):
            letter = get_column_letter(ci)
            # 教室/教师列稍宽，班级/课程列更宽
            col_mod = (ci - 2) % 4
            if col_mod == 0:    # 教室
                ws.column_dimensions[letter].width = 12
            elif col_mod == 1:  # 教师
                ws.column_dimensions[letter].width = 10
            elif col_mod == 2:  # 班级
                ws.column_dimensions[letter].width = 16
            else:               # 课程
                ws.column_dimensions[letter].width = 14

    if not wb.sheetnames:
        ws = wb.create_sheet(title="空")
        ws.cell(row=1, column=1, value="无数据")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(output_path))
    log.info("已导出 → %s (%d 个 sheet)", output_path, len(wb.sheetnames))
    for ws in wb.worksheets:
        log.info("  %s: %d 行", ws.title, ws.max_row)


# ════════════════════ 主流程 ════════════════════

def parse_weeks_arg(arg: str) -> tuple[int, int]:
    m = re.match(r"(\d+)(?:-(\d+))?", arg)
    if not m:
        return 0, 0
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    return min(a, b), max(a, b)


def get_available_weeks(slots: list[DaySlot]) -> list[int]:
    """从解析的课程数据中提取所有可用周次。"""
    all_weeks: set[int] = set()
    for slot in slots:
        for e in slot.entries:
            all_weeks.update(e.weeks)
    return sorted(all_weeks)


def gui_select_weeks(slots: list[DaySlot]) -> tuple[str, int, int] | None:
    """弹出 tkinter 窗口让用户选择场所名称和导出周次区间。
    返回 (title, week_start, week_end) 或 None（取消）。
    """
    import tkinter as tk
    from tkinter import ttk

    available = get_available_weeks(slots)
    if not available:
        return None

    root = tk.Tk()
    root.title("导出教室课表")
    root.resizable(False, False)
    root.geometry("420x220")

    result: list = [None]

    # --- 场所名称 ---
    ttk.Label(root, text="场所名称:").grid(row=0, column=0, padx=12, pady=(20, 4), sticky="w")
    title_var = tk.StringVar(value="实训室")
    ttk.Entry(root, textvariable=title_var, width=30).grid(
        row=0, column=1, padx=12, pady=(20, 4), sticky="ew")

    # --- 周次区间 ---
    frame = ttk.Frame(root)
    frame.grid(row=1, column=0, columnspan=2, padx=12, pady=8, sticky="ew")
    ttk.Label(frame, text="导出周次:").pack(side="left")

    week_vals = [str(w) for w in available]
    start_var = tk.StringVar(value=week_vals[0])
    start_cb = ttk.Combobox(frame, textvariable=start_var, values=week_vals,
                            width=6, state="readonly")
    start_cb.pack(side="left", padx=4)

    ttk.Label(frame, text="至").pack(side="left")

    end_var = tk.StringVar(value=week_vals[-1])
    end_cb = ttk.Combobox(frame, textvariable=end_var, values=week_vals,
                          width=6, state="readonly")
    end_cb.pack(side="left", padx=4)
    ttk.Label(frame, text="周").pack(side="left")

    # --- 摘要信息 ---
    summary = ttk.Label(root, text=f"共 {len(available)} 周数据（第{available[0]}~{available[-1]}周）",
                        foreground="#888")
    summary.grid(row=2, column=0, columnspan=2, padx=12, pady=4)

    # --- 按钮 ---
    btn_frame = ttk.Frame(root)
    btn_frame.grid(row=3, column=0, columnspan=2, pady=(12, 20))

    def on_export() -> None:
        ws = int(start_var.get())
        we = int(end_var.get())
        if ws > we:
            ws, we = we, ws
        result[0] = (title_var.get().strip() or "实训室", ws, we)
        root.destroy()

    def on_cancel() -> None:
        root.destroy()

    ttk.Button(btn_frame, text="导出", command=on_export, width=10).pack(side="left", padx=8)
    ttk.Button(btn_frame, text="取消", command=on_cancel, width=10).pack(side="left", padx=8)

    root.mainloop()
    return result[0]


def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(description="导出教室课表（竖星期、横大节）")
    parser.add_argument("--week", type=int, default=0, help="仅导出指定周")
    parser.add_argument("--weeks", type=str, default="", help="周次范围，如 1-10")
    parser.add_argument("--title", type=str, default="",
                        help="标题中的场所名称，如 汽车实训室")
    parser.add_argument("--gui", action="store_true", default=False,
                        help="弹出窗口选择导出参数")
    args = parser.parse_args()

    # 1. 获取 HTML
    html = fetch_html()

    # 2. 解析课程数据
    slots = parse_html(html)
    if not slots:
        log.error("未解析到任何课程数据")
        sys.exit(1)

    # 3. 确定导出参数
    title = args.title or "实训室"
    min_w, max_w = 0, 0

    if args.gui or (args.week == 0 and not args.weeks and not args.title):
        # GUI 模式
        gui_result = gui_select_weeks(slots)
        if gui_result is None:
            log.info("用户取消导出")
            return
        title, min_w, max_w = gui_result
    else:
        if args.week > 0:
            min_w = max_w = args.week
        elif args.weeks:
            min_w, max_w = parse_weeks_arg(args.weeks)

    # 4. 导出 Excel
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{min_w}-{max_w}周" if min_w > 0 else ""
    filename = f"教室课表_{PARAMS['xnxqh']}{suffix}_{ts}.xlsx"
    output_path = OUTPUT_DIR / filename
    export_excel(slots, output_path, min_w, max_w, title)

    log.info("完成！输出: %s", output_path)


if __name__ == "__main__":
    main()
