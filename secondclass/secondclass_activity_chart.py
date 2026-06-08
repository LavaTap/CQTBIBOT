"""二课活动详情图表渲染模块 —— 将单个活动的详细信息渲染为 PNG 卡片。

每个活动渲染一张竖版卡片，以 activity_id 命名输出到 schedules/_activity_charts/。

重写说明 (2026-06-05):
  - 新增活动详尽信息图标渲染风格
  - 展示字段：activity_id, activity_name, category_name, implementation_method,
    apply_time, activity_time, overview, score_detail, location, duration,
    organizer, limit_college, limit_grade, contact_phone
  - 使用彩色圆点+标签的图标风格，类似 huodong.html 中的图标呈现
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("secondclass_activity_chart")

# ---------- 常量 ----------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "schedules" / "_activity_charts"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

CARD_WIDTH = 420
CARD_MIN_HEIGHT = 650
CARD_MAX_HEIGHT = 1100
PADDING = 16
BG_COLOR = "#F0F2F5"
CARD_BG = "#FFFFFF"
ACCENT = "#4A90D9"
TEXT_DARK = "#2C3E50"
TEXT_GRAY = "#7F8C8D"
TEXT_LIGHT = "#BDC3C6"
GREEN = "#27AE60"
ORANGE = "#E67E22"
RED = "#E74C3C"
PURPLE = "#8E44AD"
TEAL = "#1ABC9C"
PINK = "#E91E63"
DIVIDER = "#ECECEC"

# 图标颜色对照表 —— 每个字段对应一个独特颜色（模拟图标感）
ICON_COLORS = {
    "category_name": "#E74C3C",        # 红色 - 类别
    "implementation_method": "#3498DB", # 蓝色 - 实施方式
    "score_detail": "#F39C12",         # 金色 - 积分
    "apply_time": "#2ECC71",           # 绿色 - 报名时间
    "activity_time": "#9B59B6",        # 紫色 - 活动时间
    "location": "#1ABC9C",             # 青色 - 地点
    "duration": "#E67E22",             # 橙色 - 时长
    "organizer": "#2980B9",            # 深蓝 - 主办方
    "contact_phone": "#E91E63",        # 粉色 - 电话
    "limit_college": "#F1C40F",        # 黄色 - 限制学院
    "limit_grade": "#8E44AD",          # 紫罗兰 - 限制年级
}

# 图标符号（Unicode 字符，模拟图标）
ICON_SYMBOLS = {
    "category_name": "▣",
    "implementation_method": "✎",
    "score_detail": "★",
    "apply_time": "◷",
    "activity_time": "◷",
    "location": "⌂",
    "duration": "⏱",
    "organizer": "⚑",
    "contact_phone": "✆",
    "limit_college": "⊞",
    "limit_grade": "⊞",
}

_FONT_PATHS = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]

_font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont | None] = {}


def _get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key in _font_cache:
        f = _font_cache[key]
        if f is not None:
            return f
        return ImageFont.load_default()
    for path in _FONT_PATHS:
        try:
            if path.endswith(".ttc"):
                font = ImageFont.truetype(path, size, index=0)
            else:
                font = ImageFont.truetype(path, size)
            _font_cache[key] = font
            return font
        except (OSError, IOError):
            continue
    log.warning("未找到中文字体，使用默认字体")
    _font_cache[key] = None
    return ImageFont.load_default()


def _draw_rounded_rect(
    draw: ImageDraw.Draw, xy: tuple[float, float, float, float],
    radius: int = 8, fill: str = "#FFFFFF", outline: str | None = None,
) -> None:
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=1 if outline else 0)


def _draw_dot_icon(
    draw: ImageDraw.Draw,
    x: int, y: int,
    color: str,
    symbol: str = "",
    size: int = 8,
) -> None:
    """绘制彩色圆点图标（模拟图标的彩色圆点）。"""
    dot_r = size // 2
    draw.ellipse([(x, y - dot_r), (x + size, y + dot_r)], fill=color)
    if symbol:
        font_sym = _get_font(size - 1, bold=True)
        bbox = draw.textbbox((0, 0), symbol, font=font_sym)
        sw = bbox[2] - bbox[0]
        sh = bbox[3] - bbox[1]
        sx = x + (size - sw) // 2
        sy = y - sh // 2 - 1
        draw.text((sx, sy), symbol, fill="#FFFFFF", font=font_sym)


def _draw_icon_row(
    draw: ImageDraw.Draw,
    x: int, y: int,
    label: str, value: str,
    icon_color: str,
    icon_symbol: str = "",
    max_w: int = 380,
    label_width: int = 72,
    value_color: str = TEXT_DARK,
) -> int:
    """绘制带彩色圆点图标的字段行，支持多行值（如报名时间含换行），返回新的 y 坐标。"""
    dot_size = 10
    icon_x = x
    icon_y = y + 8

    # 绘制彩色的圆点图标
    _draw_dot_icon(draw, icon_x, icon_y, icon_color, icon_symbol, dot_size)

    # 标签（如 "类别"）
    label_x = x + dot_size + 8
    font_label = _get_font(9)
    draw.text((label_x, y + 1), label, fill=TEXT_GRAY, font=font_label)

    # 值 —— 支持多行（以 \n 分隔）
    val_x = x + dot_size + 8 + label_width
    raw_val = str(value).strip() if value else "—"
    lines = raw_val.split("\n")
    font_val = _get_font(10)
    avail_w = max_w - val_x + x - 8
    line_h = 16  # 每行高度

    for i, line in enumerate(lines):
        display_line = line.strip()
        # 截断过长的单行
        bbox_v = draw.textbbox((0, 0), display_line, font=font_val)
        vw = bbox_v[2] - bbox_v[0]
        if vw > avail_w and avail_w > 0 and len(display_line) > 4:
            while vw > avail_w and len(display_line) > 4:
                display_line = display_line[:-1]
                bbox_v = draw.textbbox((0, 0), display_line + "…", font=font_val)
                vw = bbox_v[2] - bbox_v[0]
            display_line += "…"
        draw.text((val_x, y + 1 + i * line_h), display_line, fill=value_color, font=font_val)

    total_lines = max(len(lines), 1)
    return y + 4 + total_lines * line_h


def _draw_section_title(draw: ImageDraw.Draw, x: int, y: int, title: str, color: str = TEXT_DARK) -> int:
    """绘制区域标题，左侧带小色条装饰，返回新 y。"""
    # 色条
    _draw_rounded_rect(draw, (x, y + 2, x + 3, y + 14), radius=1, fill=ACCENT)
    draw.text((x + 10, y), title, fill=color, font=_get_font(11, bold=True))
    return y + 22


class ActivityCardRenderer:
    """将单个活动详情渲染为 PNG 信息卡片，带彩色图标风格。"""

    def __init__(self, detail: dict) -> None:
        """
        Args:
            detail: 活动详情字典，来自 second_class_activity_detail_v3 表。
        """
        self.detail = detail

    def render(self) -> Image.Image:
        d = self.detail
        # 动态计算高度
        h = self._calc_height(d)
        img = Image.new("RGB", (CARD_WIDTH, h), BG_COLOR)
        draw = ImageDraw.Draw(img)
        cx = PADDING + 6
        cw = CARD_WIDTH - 2 * cx
        y = PADDING + 4

        # 卡片背景
        _draw_rounded_rect(
            draw, (6, 6, CARD_WIDTH - 6, h - 6),
            radius=14, fill=CARD_BG,
        )

        # ═══════════════════════ 1. 头部：活动名称 + ID ═══════════════════════
        name = d.get("activity_name", "未知活动")
        aid = d.get("activity_id", "")

        # 顶部彩色装饰条
        _draw_rounded_rect(
            draw, (cx, y, cx + cw, y + 56),
            radius=8, fill="#F8FAFD",
        )

        # 左侧蓝色粗竖条
        _draw_rounded_rect(
            draw, (cx + 4, y + 8, cx + 8, y + 48),
            radius=2, fill=ACCENT,
        )

        font_name = _get_font(17, bold=True)
        max_name_w = cw - 30
        name_display = name
        bbox_n = draw.textbbox((0, 0), name_display, font=font_name)
        nw = bbox_n[2] - bbox_n[0]
        if nw > max_name_w:
            while nw > max_name_w and len(name_display) > 6:
                name_display = name_display[:-1]
                bbox_n = draw.textbbox((0, 0), name_display + "…", font=font_name)
                nw = bbox_n[2] - bbox_n[0]
            name_display += "…"
        draw.text((cx + 18, y + 6), name_display, fill=TEXT_DARK, font=font_name)

        # ID 标签行
        font_info = _get_font(9)
        id_label = f"#{aid}" if aid else ""
        if id_label:
            # ID 背景标签
            id_bbox = draw.textbbox((0, 0), id_label, font=font_info)
            id_w = id_bbox[2] - id_bbox[0] + 10
            id_h = 16
            _draw_rounded_rect(
                draw,
                (cx + 18, y + 32, cx + 18 + id_w, y + 32 + id_h),
                radius=6, fill=ACCENT,
            )
            draw.text((cx + 18 + 5, y + 32 + 1), id_label, fill="#FFFFFF", font=font_info)

        y += 66
        draw.line([(cx + 4, y), (cx + cw - 4, y)], fill=DIVIDER, width=1)
        y += 14

        # ═══════════════════════ 2. 活动分类与方式 ═══════════════════════
        y = _draw_section_title(draw, cx, y, "活动分类")

        category_name = d.get("category_name", "")
        if category_name:
            y = _draw_icon_row(
                draw, cx, y, "类    别", category_name,
                ICON_COLORS["category_name"], ICON_SYMBOLS["category_name"], cw,
            )

        implementation_method = d.get("implementation_method", "")
        if implementation_method:
            y = _draw_icon_row(
                draw, cx, y, "实施方式", implementation_method,
                ICON_COLORS["implementation_method"], ICON_SYMBOLS["implementation_method"], cw,
            )

        y += 2
        draw.line([(cx + 4, y), (cx + cw - 4, y)], fill=DIVIDER, width=1)
        y += 10

        # ═══════════════════════ 3. 时间信息 ═══════════════════════
        y = _draw_section_title(draw, cx, y, "时间信息")

        apply_time = d.get("apply_time", "")
        if apply_time:
            y = _draw_icon_row(
                draw, cx, y, "报名时间", apply_time,
                ICON_COLORS["apply_time"], ICON_SYMBOLS["apply_time"], cw,
            )

        activity_time = d.get("activity_time", "")
        if activity_time:
            y = _draw_icon_row(
                draw, cx, y, "活动时间", activity_time,
                ICON_COLORS["activity_time"], ICON_SYMBOLS["activity_time"], cw,
            )

        y += 2
        draw.line([(cx + 4, y), (cx + cw - 4, y)], fill=DIVIDER, width=1)
        y += 10

        # ═══════════════════════ 4. 积分与地点 ═══════════════════════
        y = _draw_section_title(draw, cx, y, "积分与地点")

        score_detail = d.get("score_detail", "")
        if score_detail:
            y = _draw_icon_row(
                draw, cx, y, "积分详情", score_detail,
                ICON_COLORS["score_detail"], ICON_SYMBOLS["score_detail"], cw,
                value_color=ORANGE,
            )

        location = d.get("location", "")
        if location:
            y = _draw_icon_row(
                draw, cx, y, "活动地点", location,
                ICON_COLORS["location"], ICON_SYMBOLS["location"], cw,
            )

        duration = d.get("duration", "")
        if duration:
            y = _draw_icon_row(
                draw, cx, y, "活动时长", duration,
                ICON_COLORS["duration"], ICON_SYMBOLS["duration"], cw,
            )

        y += 2
        draw.line([(cx + 4, y), (cx + cw - 4, y)], fill=DIVIDER, width=1)
        y += 10

        # ═══════════════════════ 5. 主办方 ═══════════════════════
        y = _draw_section_title(draw, cx, y, "主办方")

        organizer = d.get("organizer", "")
        if organizer:
            y = _draw_icon_row(
                draw, cx, y, "主办方", organizer,
                ICON_COLORS["organizer"], ICON_SYMBOLS["organizer"], cw,
                value_color=ACCENT,
            )

        contact_phone = d.get("contact_phone", "")
        if contact_phone:
            y = _draw_icon_row(
                draw, cx, y, "联系电话", contact_phone,
                ICON_COLORS["contact_phone"], ICON_SYMBOLS["contact_phone"], cw,
                value_color=PINK,
            )

        y += 2
        draw.line([(cx + 4, y), (cx + cw - 4, y)], fill=DIVIDER, width=1)
        y += 10

        # ═══════════════════════ 6. 限制条件 ═══════════════════════
        has_limit = bool(d.get("limit_college", "") or d.get("limit_grade", ""))
        if has_limit:
            y = _draw_section_title(draw, cx, y, "限制条件")

            limit_college = d.get("limit_college", "")
            if limit_college:
                y = _draw_icon_row(
                    draw, cx, y, "限制学院", limit_college,
                    ICON_COLORS["limit_college"], ICON_SYMBOLS["limit_college"], cw,
                    value_color=RED,
                )

            limit_grade = d.get("limit_grade", "")
            if limit_grade:
                y = _draw_icon_row(
                    draw, cx, y, "限制年级", limit_grade,
                    ICON_COLORS["limit_grade"], ICON_SYMBOLS["limit_grade"], cw,
                    value_color=PURPLE,
                )

            y += 2
            draw.line([(cx + 4, y), (cx + cw - 4, y)], fill=DIVIDER, width=1)
            y += 10

        # ═══════════════════════ 7. 活动概述 ═══════════════════════
        overview = d.get("overview", "")
        if overview:
            y = _draw_section_title(draw, cx, y, "活动概述")

            font_overview = _get_font(9)
            overview_text = overview.strip()
            if len(overview_text) > 300:
                overview_text = overview_text[:300] + "…"

            # 自动换行
            max_chars_per_line = int(cw / 8)
            lines = []
            current = ""
            for ch in overview_text:
                if len(current) >= max_chars_per_line and ch in (" ", "　", "，", "、", "。", "\n"):
                    lines.append(current + ch)
                    current = ""
                elif len(current) >= max_chars_per_line:
                    lines.append(current)
                    current = ch
                else:
                    if ch == "\n":
                        lines.append(current)
                        current = ""
                    else:
                        current += ch
            if current:
                lines.append(current)

            # 最多显示 8 行
            overview_bg_top = y
            for line in lines[:8]:
                if y + 14 > h - 50:
                    break
                draw.text((cx + 4, y), line, fill=TEXT_GRAY, font=font_overview)
                y += 16

            # 背景浅色区域
            overview_bg_h = y - overview_bg_top + 4
            if overview_bg_h > 0:
                _draw_rounded_rect(
                    draw,
                    (cx, overview_bg_top - 2, cx + cw, overview_bg_top + overview_bg_h),
                    radius=6, fill="#F9FAFB",
                )
                # 重新绘制文本(因为背景覆盖了)
                ly = overview_bg_top
                for line in lines[:8]:
                    if ly + 14 > h - 50:
                        break
                    draw.text((cx + 4, ly), line, fill=TEXT_GRAY, font=font_overview)
                    ly += 16
                y = overview_bg_top + overview_bg_h + 4

        # ═══════════════════════ 8. 底部 ═══════════════════════
        # 数据更新时间
        fetched_at = d.get("fetched_at", "")
        if not fetched_at:
            fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        font_footer = _get_font(8)
        footer = f"数据更新: {fetched_at}"
        bbox = draw.textbbox((0, 0), footer, font=font_footer)
        fw = bbox[2] - bbox[0]
        draw.text(
            (CARD_WIDTH - cx - fw, h - PADDING - 14),
            footer, fill=TEXT_LIGHT, font=font_footer,
        )

        return img

    def _calc_height(self, d: dict) -> int:
        """根据数据量动态计算卡片高度。"""
        base = 160  # 头部 + 基础间距
        row_h = 22  # 每行高度
        section_gap = 36  # 每个 section 标题 + 分隔线

        # 估算行数
        rows = 0
        for field in ["category_name", "implementation_method",
                       "apply_time", "activity_time",
                       "score_detail", "location", "duration",
                       "organizer", "contact_phone",
                       "limit_college", "limit_grade"]:
            if d.get(field, ""):
                rows += 1

        has_sections = 0
        # 分类与方式 section
        if d.get("category_name") or d.get("implementation_method"):
            has_sections += 1
        # 时间 section
        if d.get("apply_time") or d.get("activity_time"):
            has_sections += 1
        # 积分与地点 section
        if d.get("score_detail") or d.get("location") or d.get("duration"):
            has_sections += 1
        # 主办方 section
        if d.get("organizer") or d.get("contact_phone"):
            has_sections += 1
        # 限制 section
        if d.get("limit_college") or d.get("limit_grade"):
            has_sections += 1

        overview_h = 0
        overview = d.get("overview", "")
        if overview:
            overview_h = min(8, len(overview) // 30 + 3) * 16 + 30

        h = base + rows * row_h + has_sections * section_gap + overview_h + 40
        return max(CARD_MIN_HEIGHT, min(h, CARD_MAX_HEIGHT))

    def save(self, path: Path) -> Path:
        """渲染并保存 PNG。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        img = self.render()
        img.save(str(path), "PNG")
        log.info("活动卡片已保存: %s (size=%dx%d)", path, img.width, img.height)
        return path


# ---------- 便捷入口 ----------

def render_activity_card(detail: dict, output_dir: Path | None = None) -> Path:
    """渲染单个活动卡片并返回路径。

    默认保存到 schedules/_activity_charts/{activity_id}.png。
    """
    if output_dir is None:
        output_dir = OUTPUT_DIR
    activity_id = detail.get("activity_id", "unknown")
    output_path = output_dir / f"{activity_id}.png"
    renderer = ActivityCardRenderer(detail)
    renderer.save(output_path)
    return output_path


def render_activity_cards(details: list[dict], output_dir: Path | None = None) -> list[Path]:
    """批量渲染活动卡片，返回所有输出路径列表。"""
    if output_dir is None:
        output_dir = OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for detail in details:
        try:
            p = render_activity_card(detail, output_dir)
            paths.append(p)
        except Exception as e:
            log.warning("活动卡片渲染失败: activity_id=%s error=%s",
                        detail.get("activity_id", "?"), e)
    return paths


def generate_sample_card() -> Path:
    """用样本活动数据生成样图（用于验证视觉效果）。"""
    sample_detail = {
        "activity_id": "100055",
        "activity_name": "前锦证券协会团日活动",
        "category_name": "【必修】志愿服务",
        "implementation_method": "参与学生社团组织的各类志愿服务活动",
        "score_detail": "0.2分/次/2小时",
        "apply_time": "2025-05-25 14:00\n至2025-05-25 17:00",
        "activity_time": "2025-05-26 08:00\n至2025-05-26 12:00",
        "location": "线上",
        "duration": "2.0",
        "organizer": "新能源汽车学院",
        "contact_phone": "19923921022",
        "limit_college": "新能源汽车学院",
        "limit_grade": "2023,2024",
        "overview": "本活动是针对「方寸之间，叠出精彩」工作人员补发的活动。\n此活动已有名单，请勿乱报，谢谢！！！\n总结截止时间：2025年5月26日20:00\n审核截止时间：2025年5月27日21:00",
        "fetched_at": "2026-06-05T14:30:00",
    }
    return render_activity_card(sample_detail, OUTPUT_DIR)


# ════════════════════ 批量列表渲染（最多10个活动一图）════════════════════

# 调用方必须传入 output_dir（按学号分子目录，如 schedules/<student_id>/list/）。

LIST_CARD_W = 420
LIST_ROW_H = 50
LIST_HEADER_H = 70
LIST_FOOTER_H = 30
LIST_PAD = 12
MAX_PER_CHART = 10
LIST_TOTAL_H = LIST_HEADER_H + MAX_PER_CHART * LIST_ROW_H + LIST_FOOTER_H + LIST_PAD * 4


class ActivityListRenderer:
    """将最多10个活动渲染为一张竖版列表图。"""

    def __init__(self, activities: list[dict], page: int = 1,
                 student_info: str = "") -> None:
        self.activities = activities[:MAX_PER_CHART]
        self.page = page
        self.student_info = student_info

    def render(self) -> Image.Image:
        acts = self.activities
        h = LIST_HEADER_H + len(acts) * LIST_ROW_H + LIST_FOOTER_H + LIST_PAD * 4
        img = Image.new("RGB", (LIST_CARD_W, h), BG_COLOR)
        draw = ImageDraw.Draw(img)

        cx = LIST_PAD + 4
        cw = LIST_CARD_W - 2 * cx

        # 卡片背景
        _draw_rounded_rect(draw, (4, 4, LIST_CARD_W - 4, h - 4), radius=10, fill=CARD_BG)

        y = LIST_PAD + 6
        # ── 头部 ──
        _draw_rounded_rect(draw, (cx, y, cx + 4, y + 32), radius=2, fill=ACCENT)
        title = "二课活动列表"
        if self.student_info:
            title += " - " + self.student_info
        draw.text((cx + 12, y + 2), title, fill=TEXT_DARK, font=_get_font(14, bold=True))
        draw.text((cx + 12, y + 24), "共%d个活动 | 每页最多%d个" % (len(acts), MAX_PER_CHART),
                  fill=TEXT_GRAY, font=_get_font(9))
        y += 42
        draw.line([(cx, y), (cx + cw, y)], fill=DIVIDER, width=1)
        y += 6

        # ── 表头行 ──
        font_hd = _get_font(9, bold=True)
        hd_color = "#555555"
        col_x = [cx, cx + 46, cx + 46 + 170, cx + 46 + 170 + 70, cx + 46 + 170 + 70 + 60]
        headers = ["#", "活动名称", "模块", "积分", "状态/时间"]
        for hi, (hx, hdr) in enumerate(zip(col_x, headers)):
            draw.text((hx, y), hdr, fill=hd_color, font=font_hd)
        y += 16

        # ── 活动行 ──
        font_idx = _get_font(10, bold=True)
        font_name = _get_font(9)
        font_small = _get_font(8)
        for i, act in enumerate(acts):
            row_bg = "#F8F9FA" if i % 2 == 0 else "#FFFFFF"
            draw.rectangle([(cx, y), (cx + cw, y + LIST_ROW_H - 2)], fill=row_bg)

            # 序号
            draw.text((cx + 4, y + 6), str(i + 1), fill=ACCENT, font=font_idx)

            # 活动名称
            aname = act.get("activity_name", "未知")
            aid = act.get("activity_id", "")
            if len(aname) > 16:
                aname = aname[:15] + "…"
            draw.text((col_x[1] + 2, y + 4), aname, fill=TEXT_DARK, font=font_name)
            draw.text((col_x[1] + 2, y + 22), "#%s" % aid, fill=TEXT_LIGHT, font=font_small)

            # 模块
            mn = act.get("module_name", "")
            if len(mn) > 10:
                mn = mn[:9] + "…"
            draw.text((col_x[2] + 2, y + 6), mn, fill=TEXT_GRAY, font=font_small)

            # 积分（兼容 "院级：0.3分/每人每次" 等非纯数字格式）
            raw = act.get("score", 0)
            if isinstance(raw, (int, float)):
                sc = float(raw)
            else:
                m = re.search(r'(\d+(?:\.\d+)?)', str(raw))
                sc = float(m.group(1)) if m else 0.0
            draw.text((col_x[3] + 2, y + 6), "%.1f分" % sc, fill=GREEN if sc > 0 else TEXT_LIGHT, font=font_name)

            # 状态/时间
            st = act.get("status_name", "")
            dt = (act.get("start_date", "") or "")[:10]
            if st:
                st_color = GREEN if "已结束" in st else (ORANGE if "活动" in st else ACCENT)
                draw.text((col_x[4] + 2, y + 4), st, fill=st_color, font=font_small)
            if dt:
                draw.text((col_x[4] + 2, y + 18), dt, fill=TEXT_LIGHT, font=_get_font(7))

            # 分隔线
            y += LIST_ROW_H
            if i < len(acts) - 1:
                draw.line([(cx + 4, y - 1), (cx + cw - 4, y - 1)], fill="#F0F0F0", width=1)

        # ── 底部 ──
        y += 4
        now_str = datetime.now().strftime("%m-%d %H:%M")
        draw.text((cx, y + 2), "更新: %s" % now_str, fill=TEXT_LIGHT, font=_get_font(8))
        if self.page > 1:
            draw.text((cx + cw - 60, y + 2), "第%d页" % self.page,
                      fill=TEXT_LIGHT, font=_get_font(8))

        return img

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        img = self.render()
        img.save(str(path), "PNG")
        log.info("活动列表图已保存: %s (size=%dx%d)", path, img.width, img.height)
        return path


def render_activity_list(activities: list[dict], page: int = 1,
                         student_info: str = "",
                         output_dir: Path | None = None) -> Path:
    """渲染一批活动中最多10个到一张图。output_dir 必填。"""
    if output_dir is None:
        raise ValueError("render_activity_list: output_dir 必填（按学号分子目录）")
    output_dir.mkdir(parents=True, exist_ok=True)
    fname = "activity_list_p%d.png" % page
    output_path = output_dir / fname
    renderer = ActivityListRenderer(activities, page=page, student_info=student_info)
    renderer.save(output_path)
    return output_path


def render_all_activity_lists(activities: list[dict],
                              student_info: str = "",
                              output_dir: Path | None = None) -> list[Path]:
    """将所有活动分页渲染，每页最多10个，返回所有图片路径。output_dir 必填。"""
    if output_dir is None:
        raise ValueError("render_all_activity_lists: output_dir 必填（按学号分子目录）")
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    total = len(activities)
    page = 1
    for i in range(0, total, MAX_PER_CHART):
        batch = activities[i:i + MAX_PER_CHART]
        p = render_activity_list(batch, page=page, student_info=student_info, output_dir=output_dir)
        paths.append(p)
        page += 1
    return paths


def generate_sample_list() -> Path:
    """用样本数据生成活动列表样图。"""
    sample_acts = [
        {"activity_id": "100055", "activity_name": "前锦证券协会团日活动", "module_name": "社团活动",
         "score": 0.0, "status_name": "已结束", "start_date": "2025-05-22"},
        {"activity_id": "100088", "activity_name": "校园歌手大赛复赛", "module_name": "文艺美育",
         "score": 2.0, "status_name": "已结束", "start_date": "2025-06-15"},
        {"activity_id": "100112", "activity_name": "职业生涯规划讲座", "module_name": "职业技能",
         "score": 1.5, "status_name": "已结束", "start_date": "2025-06-20"},
        {"activity_id": "100156", "activity_name": "社区志愿服务活动", "module_name": "志愿服务",
         "score": 3.0, "status_name": "已结束", "start_date": "2025-07-01"},
        {"activity_id": "100178", "activity_name": "思想政治主题团课", "module_name": "思想成长",
         "score": 0.5, "status_name": "报名中", "start_date": "2025-07-10"},
        {"activity_id": "100201", "activity_name": "大数据技术创新创业大赛", "module_name": "专业技能",
         "score": 4.0, "status_name": "活动中", "start_date": "2025-07-15"},
    ]
    return render_activity_list(
        sample_acts, page=1, student_info="阳历样例",
        output_dir=PROJECT_ROOT / "schedules" / "_sample" / "list",
    )
