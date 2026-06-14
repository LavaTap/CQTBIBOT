"""课表图片渲染模块 —— 参考 Yunzai-Bot v3 风格，生成课程表 PNG 图片。

功能：
  - 完整周课表图片（周一~周日 + 课时段 + 日期栏 + 色块分区）
  - 单日课表卡片（#今日课表 / #明天课表）
  - 本地 PNG 缓存（JSON 更新后自动失效）
  - 学期周次计算（从 semester_config.json 读取学期起始日期）
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont

if TYPE_CHECKING:
    from schedule import Course, Schedule

log = logging.getLogger("schedule_image")

# ---------- 课表渲染屏蔽课程名关键词 ----------
FILTERED_COURSE_KEYWORDS: list[str] = ["AI", "AIGC"]


def _should_filter_course(name: str) -> bool:
    """判断课程是否应被渲染屏蔽。"""
    upper = name.upper()
    return any(kw.upper() in upper for kw in FILTERED_COURSE_KEYWORDS)


# ---------- 常量 ----------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEDULE_DIR = PROJECT_ROOT / "schedules"
SEMESTER_CONFIG_FILE = SCHEDULE_DIR / "semester_config.json"

# 课时分段（与 schedule_tool.py 的 period_labels 一致）
PERIOD_SEGMENTS = [
    (1, 3, "1-3节"),
    (4, 5, "4-5节"),
    (6, 8, "6-8节"),
    (9, 10, "9-10节"),
    (11, 12, "11-12节"),
]

DAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 柔和色板（20色，按课程名 hash 分配）
PALETTE = [
    "#FFB3BA", "#FFDFBA", "#FFFFBA", "#BAFFC9", "#BAE1FF",
    "#E8BAFF", "#FFB3E6", "#B3FFE0", "#FFDAB3", "#B3D9FF",
    "#D9B3FF", "#B3FFB3", "#FFB3B3", "#B3FFFF", "#FFE0B3",
    "#E0B3FF", "#B3FFD9", "#FFB3D9", "#D9FFB3", "#B3E0FF",
]

# 深色变体（用于课程名文字）
PALETTE_DARK = [
    "#CC8E94", "#CCB394", "#CCCC94", "#94CC9E", "#94B3CC",
    "#BA94CC", "#CC94B3", "#94CCAE", "#CCAE94", "#94AECC",
    "#AE94CC", "#94CC94", "#CC9494", "#94CCCC", "#CCAE94",
    "#AE94CC", "#94CCAE", "#CC94AE", "#AECC94", "#94AECC",
]

# ---------- 布局常量（完整周课表）----------
MARGIN = 20
PERIOD_COL_WIDTH = 60
DAY_COL_WIDTH = 120
HEADER_HEIGHT = 50
DATE_BAR_HEIGHT = 30
FOOTER_HEIGHT = 30
CELL_PADDING = 6
BORDER_RADIUS = 8

# 每个时段行高（按内容量调整：1-3节和6-8节内容多，行高大）
ROW_HEIGHTS = {
    (1, 3): 100,
    (4, 5): 80,
    (6, 8): 100,
    (9, 10): 80,
    (11, 12): 80,
}

# 单日卡片布局
SINGLE_DAY_WIDTH = 380
SINGLE_DAY_MARGIN = 16
SINGLE_DAY_PERIOD_HEIGHT = 70
SINGLE_DAY_HEADER_HEIGHT = 50
SINGLE_DAY_FOOTER_HEIGHT = 30

# ---------- 字体 ----------
_FONT_PATHS = [
    "C:/Windows/Fonts/msyh.ttc",   # Microsoft YaHei
    "C:/Windows/Fonts/simhei.ttf",  # SimHei
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",  # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]

_font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont | None] = {}


def _get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """加载中文字体，带缓存。"""
    key = (size, bold)
    if key in _font_cache:
        f = _font_cache[key]
        if f is not None:
            return f
        # 缓存为 None 说明所有路径都失败，用默认字体
        return ImageFont.load_default()

    for path in _FONT_PATHS:
        try:
            # .ttc 文件需要 index 参数
            if path.endswith(".ttc"):
                font = ImageFont.truetype(path, size, index=0)
            else:
                font = ImageFont.truetype(path, size)
            _font_cache[key] = font
            return font
        except (OSError, IOError):
            continue

    log.warning("未找到中文字体，使用默认字体（中文可能无法正常显示）")
    _font_cache[key] = None
    return ImageFont.load_default()


# ---------- 学期周次计算 ----------
def load_semester_config() -> dict:
    """加载学期配置。"""
    if not SEMESTER_CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(SEMESTER_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def get_current_week_num(semester: str) -> int:
    """根据学期配置计算当前周次。返回 0 表示无法计算。"""
    config = load_semester_config()
    sem_info = config.get(semester, {})
    start_str = sem_info.get("start_date")
    if not start_str:
        return 0
    start = date.fromisoformat(start_str)
    today = date.today()
    delta = (today - start).days
    if delta < 0:
        return 1
    week = (delta // 7) + 1
    total = sem_info.get("total_weeks", 25)
    return min(week, total)


def get_week_dates(semester: str, week_num: int) -> list[date]:
    """计算指定周次每一天的日期。返回 7 个 date 对象（周一~周日）。"""
    config = load_semester_config()
    sem_info = config.get(semester, {})
    start_str = sem_info.get("start_date")
    if not start_str:
        return []
    start = date.fromisoformat(start_str)
    # start 是第一周的周一
    week_monday = start + timedelta(weeks=week_num - 1)
    return [week_monday + timedelta(days=i) for i in range(7)]


# ---------- PNG 缓存 ----------
def get_cached_image_path(student_id: str, week_num: int) -> Path:
    """返回缓存图片路径: schedules/{student_id}/week{N}.png"""
    d = SCHEDULE_DIR / student_id
    d.mkdir(parents=True, exist_ok=True)
    return d / f"week{week_num}.png"


def is_cache_valid(student_id: str, semester: str, week_num: int) -> bool:
    """检查缓存是否有效（JSON 比 PNG 旧则有效）。"""
    from schedule import _schedule_path
    json_path = _schedule_path("json", student_id=student_id, semester=semester)
    png_path = get_cached_image_path(student_id, week_num)
    if not png_path.exists() or not json_path.exists():
        return False
    return png_path.stat().st_mtime >= json_path.stat().st_mtime


# ---------- 颜色分配 ----------
def _color_index(course_name: str) -> int:
    """根据课程名计算颜色索引（确定性）。"""
    return sum(ord(c) for c in course_name) % len(PALETTE)


def get_course_color(course_name: str) -> str:
    """返回课程对应的柔和背景色。"""
    return PALETTE[_color_index(course_name)]


def get_course_dark_color(course_name: str) -> str:
    """返回课程对应的深色（用于文字或边框）。"""
    return PALETTE_DARK[_color_index(course_name)]


# ---------- 渲染器 ----------
class ScheduleRenderer:
    """将 Schedule 渲染为 Yunzai-Bot v3 风格的课表 PNG 图片。"""

    def __init__(self, schedule: Schedule, week_num: int) -> None:
        self.schedule = schedule
        self.week_num = week_num
        # 渲染前过滤掉屏蔽课程
        self._filtered_courses = [
            c for c in schedule.courses if not _should_filter_course(c.name)
        ]

    def render(self) -> Image.Image:
        """渲染完整周课表图片。"""
        total_width = MARGIN + PERIOD_COL_WIDTH + 7 * DAY_COL_WIDTH + MARGIN
        body_height = sum(ROW_HEIGHTS.values())
        total_height = MARGIN + HEADER_HEIGHT + DATE_BAR_HEIGHT + body_height + FOOTER_HEIGHT + MARGIN

        img = Image.new("RGB", (total_width, total_height), "#FFFFFF")
        draw = ImageDraw.Draw(img)

        y = MARGIN

        # --- 标题栏 ---
        y = self._draw_header(draw, y, total_width)

        # --- 日期栏 ---
        y = self._draw_date_bar(draw, y)

        # --- 网格主体 ---
        y = self._draw_grid(draw, y)

        # --- 底部 ---
        self._draw_footer(draw, y, total_width)

        return img

    def render_single_day(self, day: int) -> Image.Image:
        """渲染单日课表卡片。

        Args:
            day: 1=周一 … 7=周日
        """
        body_height = len(PERIOD_SEGMENTS) * SINGLE_DAY_PERIOD_HEIGHT
        total_height = (
            SINGLE_DAY_MARGIN + SINGLE_DAY_HEADER_HEIGHT
            + body_height + SINGLE_DAY_FOOTER_HEIGHT + SINGLE_DAY_MARGIN
        )
        content_width = SINGLE_DAY_WIDTH - 2 * SINGLE_DAY_MARGIN

        img = Image.new("RGB", (SINGLE_DAY_WIDTH, total_height), "#FFFFFF")
        draw = ImageDraw.Draw(img)

        y = SINGLE_DAY_MARGIN

        # --- 标题 ---
        day_label = DAY_LABELS[day - 1] if 1 <= day <= 7 else f"周{day}"
        dates = get_week_dates(self.schedule.semester, self.week_num)
        date_str = ""
        if dates and 1 <= day <= 7:
            d = dates[day - 1]
            date_str = f"  {d.month}/{d.day}"

        title = f"{day_label}{date_str}"
        font_title = _get_font(18, bold=True)
        bbox = draw.textbbox((0, 0), title, font=font_title)
        tw = bbox[2] - bbox[0]
        draw.text(
            ((SINGLE_DAY_WIDTH - tw) // 2, y + 10),
            title, fill="#333333", font=font_title,
        )
        y += SINGLE_DAY_HEADER_HEIGHT

        # --- 分割线 ---
        draw.line(
            [(SINGLE_DAY_MARGIN, y), (SINGLE_DAY_WIDTH - SINGLE_DAY_MARGIN, y)],
            fill="#E0E0E0", width=1,
        )
        y += 4

        # --- 时段列表 ---
        courses = self._filtered_courses
        for ps, pe, label in PERIOD_SEGMENTS:
            matches = [
                c for c in courses
                if c.day == day and c.period_start <= pe and c.period_end >= ps
                and c.week_matches(self.week_num)
            ]

            if matches:
                for c in matches:
                    color = get_course_color(c.name)
                    dark = get_course_dark_color(c.name)
                    self._draw_single_day_block(
                        draw, SINGLE_DAY_MARGIN, y, content_width,
                        SINGLE_DAY_PERIOD_HEIGHT - 4, c, color, dark,
                    )
            else:
                # 无课
                draw.rounded_rectangle(
                    [(SINGLE_DAY_MARGIN, y),
                     (SINGLE_DAY_MARGIN + content_width, y + SINGLE_DAY_PERIOD_HEIGHT - 4)],
                    radius=6, fill="#F5F5F5",
                )
                font_sm = _get_font(10)
                no_class = f"{label}  无课"
                bbox = draw.textbbox((0, 0), no_class, font=font_sm)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
                draw.text(
                    (SINGLE_DAY_MARGIN + (content_width - tw) // 2,
                     y + (SINGLE_DAY_PERIOD_HEIGHT - 4 - th) // 2),
                    no_class, fill="#AAAAAA", font=font_sm,
                )

            y += SINGLE_DAY_PERIOD_HEIGHT

        # --- 底部 ---
        timestamp = self.schedule.fetched_at or date.today().isoformat()
        font_footer = _get_font(9)
        footer_text = f"更新时间: {timestamp}"
        bbox = draw.textbbox((0, 0), footer_text, font=font_footer)
        tw = bbox[2] - bbox[0]
        draw.text(
            (SINGLE_DAY_WIDTH - SINGLE_DAY_MARGIN - tw, y + 6),
            footer_text, fill="#999999", font=font_footer,
        )

        return img

    def save(self, path: Path) -> Path:
        """渲染并保存 PNG 到指定路径。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        img = self.render()
        img.save(str(path), "PNG")
        log.info("课表图片已保存: %s", path)
        return path

    # ======================== 内部绘制方法 ========================

    def _draw_header(self, draw: ImageDraw.Draw, y: int, total_width: int) -> int:
        """绘制标题栏，返回下一区域的 y 坐标。"""
        # 标题: "第N周课表"
        title = f"第{self.week_num}周课表"
        font_title = _get_font(20, bold=True)
        bbox = draw.textbbox((0, 0), title, font=font_title)
        tw = bbox[2] - bbox[0]
        center_x = (total_width - tw) // 2
        draw.text((center_x, y + 10), title, fill="#333333", font=font_title)

        # 左侧: 学生姓名
        if self.schedule.student_name:
            font_info = _get_font(11)
            draw.text((MARGIN + 4, y + 16), self.schedule.student_name,
                      fill="#666666", font=font_info)

        # 右侧: 学期
        if self.schedule.semester:
            font_info = _get_font(11)
            sem_text = self.schedule.semester
            bbox = draw.textbbox((0, 0), sem_text, font=font_info)
            sw = bbox[2] - bbox[0]
            draw.text((total_width - MARGIN - sw - 4, y + 16), sem_text,
                      fill="#999999", font=font_info)

        # 底部分割线
        y += HEADER_HEIGHT
        draw.line([(MARGIN, y), (total_width - MARGIN, y)], fill="#E0E0E0", width=1)
        return y

    def _draw_date_bar(self, draw: ImageDraw.Draw, y: int) -> int:
        """绘制日期栏，返回下一区域的 y 坐标。"""
        dates = get_week_dates(self.schedule.semester, self.week_num)
        today = date.today()

        x = MARGIN
        # 课时列空白
        x += PERIOD_COL_WIDTH

        for i in range(7):
            date_str = ""
            is_today = False
            if dates and i < len(dates):
                d = dates[i]
                date_str = f"{d.month}/{d.day}"
                is_today = d == today

            font_date = _get_font(12)
            color = "#E74C3C" if is_today else "#666666"
            bbox = draw.textbbox((0, 0), date_str, font=font_date)
            tw = bbox[2] - bbox[0]
            cx = x + (DAY_COL_WIDTH - tw) // 2
            draw.text((cx, y + 6), date_str, fill=color, font=font_date)
            x += DAY_COL_WIDTH

        y += DATE_BAR_HEIGHT
        draw.line([(MARGIN, y), (MARGIN + PERIOD_COL_WIDTH + 7 * DAY_COL_WIDTH, y)],
                  fill="#E0E0E0", width=1)
        return y

    def _draw_grid(self, draw: ImageDraw.Draw, y_start: int) -> int:
        """绘制网格主体，返回底部的 y 坐标。"""
        x_start = MARGIN
        grid_width = PERIOD_COL_WIDTH + 7 * DAY_COL_WIDTH
        courses = self._filtered_courses
        y = y_start

        for seg_idx, (ps, pe, label) in enumerate(PERIOD_SEGMENTS):
            row_h = ROW_HEIGHTS[(ps, pe)]

            # --- 课时列 ---
            # 浅灰背景
            draw.rectangle(
                [(x_start, y), (x_start + PERIOD_COL_WIDTH, y + row_h)],
                fill="#F8F8F8",
            )
            font_period = _get_font(11)
            bbox = draw.textbbox((0, 0), label, font=font_period)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            draw.text(
                (x_start + (PERIOD_COL_WIDTH - tw) // 2,
                 y + (row_h - th) // 2),
                label, fill="#888888", font=font_period,
            )

            # --- 7 天列 ---
            for day in range(1, 8):
                cell_x = x_start + PERIOD_COL_WIDTH + (day - 1) * DAY_COL_WIDTH
                cell_y = y

                # 交替列背景
                if day % 2 == 0:
                    draw.rectangle(
                        [(cell_x, cell_y), (cell_x + DAY_COL_WIDTH, cell_y + row_h)],
                        fill="#FAFAFA",
                    )

                # 细网格线
                draw.rectangle(
                    [(cell_x, cell_y), (cell_x + DAY_COL_WIDTH, cell_y + row_h)],
                    outline="#EEEEEE", width=1,
                )

                # 匹配该格的课程
                matches = [
                    c for c in courses
                    if c.day == day
                    and c.period_start <= pe and c.period_end >= ps
                    and c.week_matches(self.week_num)
                ]

                if matches:
                    self._draw_cell_courses(draw, cell_x, cell_y, DAY_COL_WIDTH, row_h, matches)

            y += row_h

        return y

    def _draw_cell_courses(
        self, draw: ImageDraw.Draw,
        cell_x: int, cell_y: int,
        cell_w: int, cell_h: int,
        courses: list[Course],
    ) -> None:
        """在单元格内绘制课程色块。"""
        n = len(courses)
        if n == 1:
            c = courses[0]
            color = get_course_color(c.name)
            dark = get_course_dark_color(c.name)
            self._draw_course_block(
                draw,
                cell_x + CELL_PADDING,
                cell_y + CELL_PADDING,
                cell_w - 2 * CELL_PADDING,
                cell_h - 2 * CELL_PADDING,
                c, color, dark,
            )
        else:
            # 多门课垂直分割
            block_h = (cell_h - CELL_PADDING * (n + 1)) / n
            for i, c in enumerate(courses):
                color = get_course_color(c.name)
                dark = get_course_dark_color(c.name)
                by = cell_y + CELL_PADDING + i * (block_h + CELL_PADDING)
                self._draw_course_block(
                    draw,
                    cell_x + CELL_PADDING,
                    int(by),
                    cell_w - 2 * CELL_PADDING,
                    int(block_h),
                    c, color, dark,
                )

    def _draw_course_block(
        self, draw: ImageDraw.Draw,
        x: int, y: int, w: int, h: int,
        course: Course, color: str, dark: str,
    ) -> None:
        """绘制单个课程色块（圆角矩形 + 课程信息）。"""
        # 圆角矩形背景
        draw.rounded_rectangle(
            [(x, y), (x + w, y + h)],
            radius=BORDER_RADIUS, fill=color, outline=dark, width=1,
        )

        # 课程名
        font_name = _get_font(13, bold=True)
        name = course.name
        # 截断过长的名称
        if len(name) > 8:
            name = name[:7] + "…"
        draw.text((x + 6, y + 4), name, fill="#333333", font=font_name)

        # 教师
        if course.teacher and h > 40:
            font_info = _get_font(10)
            teacher = course.teacher
            if len(teacher) > 10:
                teacher = teacher[:9] + "…"
            draw.text((x + 6, y + 22), teacher, fill="#666666", font=font_info)

        # 教室
        if course.room and h > 55:
            font_info = _get_font(10)
            room = course.room
            if len(room) > 10:
                room = room[:9] + "…"
            draw.text((x + 6, y + 36), room, fill="#666666", font=font_info)

    def _draw_single_day_block(
        self, draw: ImageDraw.Draw,
        x: int, y: int, w: int, h: int,
        course: Course, color: str, dark: str,
    ) -> None:
        """单日卡片中的课程色块。"""
        draw.rounded_rectangle(
            [(x, y), (x + w, y + h)],
            radius=6, fill=color, outline=dark, width=1,
        )

        font_name = _get_font(13, bold=True)
        font_info = _get_font(10)

        # 课程名
        name = course.name
        if len(name) > 10:
            name = name[:9] + "…"
        draw.text((x + 8, y + 4), name, fill="#333333", font=font_name)

        # 教师
        if course.teacher:
            teacher = course.teacher
            if len(teacher) > 14:
                teacher = teacher[:13] + "…"
            draw.text((x + 8, y + 22), teacher, fill="#666666", font=font_info)

        # 教室
        if course.room and h > 50:
            room = course.room
            if len(room) > 14:
                room = room[:13] + "…"
            draw.text((x + 8, y + 36), room, fill="#666666", font=font_info)

    def _draw_footer(self, draw: ImageDraw.Draw, y: int, total_width: int) -> None:
        """绘制底部更新时间。"""
        timestamp = self.schedule.fetched_at or date.today().isoformat()
        font_footer = _get_font(9)
        footer_text = f"更新时间: {timestamp}"
        bbox = draw.textbbox((0, 0), footer_text, font=font_footer)
        tw = bbox[2] - bbox[0]
        draw.text(
            (total_width - MARGIN - tw, y + 8),
            footer_text, fill="#999999", font=font_footer,
        )


# ---------- 便捷入口 ----------
def render_schedule_image(
    schedule: Schedule,
    week_num: int,
    student_id: str = "",
) -> Path:
    """渲染课表图片并返回缓存路径。"""
    sid = student_id or schedule.student_id
    png_path = get_cached_image_path(sid, week_num)

    # 检查缓存
    if is_cache_valid(sid, schedule.semester, week_num):
        log.info("使用缓存课表图片: %s", png_path)
        return png_path

    renderer = ScheduleRenderer(schedule, week_num)
    renderer.save(png_path)
    return png_path


def render_day_image(
    schedule: Schedule,
    week_num: int,
    day: int,
    student_id: str = "",
) -> Path:
    """渲染单日课表卡片并返回临时路径。"""
    sid = student_id or schedule.student_id
    # 单日图片不缓存长期，用临时命名
    png_path = get_cached_image_path(sid, week_num).parent / f"week{week_num}_day{day}.png"

    renderer = ScheduleRenderer(schedule, week_num)
    img = renderer.render_single_day(day)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(png_path), "PNG")
    return png_path
