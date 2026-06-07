"""二课信息图表渲染模块 —— 将用户的第二课堂数据渲染为信息图 PNG。

功能：
  - 用户信息头部（姓名、班级、学院、专业）
  - 二课总分环形进度图
  - 活动概况柱状图（我的活动/未签到/未提交/社团）
  - 分类积分水平条形图（思想成长/专业技能/职业技能）
  - QQ 可直接发送的图片格式
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("secondclass_image")

# ---------- 常量 ----------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "schedules" / "_secondclass_charts"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

CARD_WIDTH = 360
CARD_HEIGHT = 690  # 9:16 竖版，增加高度容纳时长展示
CARD_PADDING = 12
BG_COLOR = "#F8F9FA"
CARD_BG = "#FFFFFF"
ACCENT_BLUE = "#4A90D9"
ACCENT_GREEN = "#27AE60"
ACCENT_ORANGE = "#E67E22"
ACCENT_RED = "#E74C3C"
ACCENT_PURPLE = "#8E44AD"
TEXT_DARK = "#2C3E50"
TEXT_GRAY = "#7F8C8D"
TEXT_LIGHT = "#BDC3C7"

# ---------- 字体 ----------
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


# ---------- 辅助函数 ----------

def _draw_rounded_rect(
    draw: ImageDraw.Draw, xy: tuple[float, float, float, float],
    radius: int = 8, fill: str = "#FFFFFF", outline: str | None = None,
) -> None:
    """绘制圆角矩形。"""
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=1 if outline else 0)


def _draw_donut(
    draw: ImageDraw.Draw, cx: int, cy: int, outer_r: int, inner_r: int,
    progress: float, color: str, bg_color: str = "#E8E8E8",
) -> None:
    """绘制环形进度图。"""
    # 背景圆环
    draw.ellipse([(cx - outer_r, cy - outer_r), (cx + outer_r, cy + outer_r)],
                 fill=bg_color)
    # 前景扇形（用圆弧近似）
    # Pillow 没有直接画扇形的函数，用 pieslice 绘制进度弧
    if progress > 0:
        # 从 12 点方向顺时针：-90 度开始
        start_angle = -90
        end_angle = -90 + 360 * progress
        draw.pieslice(
            [(cx - outer_r, cy - outer_r), (cx + outer_r, cy + outer_r)],
            start=start_angle, end=end_angle, fill=color,
        )
    # 内圆（挖空形成环形）
    draw.ellipse([(cx - inner_r, cy - inner_r), (cx + inner_r, cy + inner_r)],
                 fill=CARD_BG)


# ---------- 渲染器 ----------

class SecondClassRenderer:
    """将二课数据渲染为信息图卡片。"""

    def __init__(self, data: dict) -> None:
        """
        Args:
            data: 二课数据字典，包含以下键：
                realname, student_id, deptname, college, major,
                total_score, activity_count, unsigned_count,
                unfinished_count, club_count, thought_score,
                skill_score, career_score, ideology_score,
                labor_score, art_score, volunteer_score,
                total_duration
        """
        self.data = data

    def render(self) -> Image.Image:
        r"""渲染完整二课信息图（9:16 竖版）。
        布局（自上而下）：
          头部: 姓名 | 学号 | 班级 | 学院
          二课总分 (环形图)
          活动概况 — 堆叠条 + 图例 + 总活动数 + 社团
          2025-2026-2 分类积分 (三角雷达图)
          底部: 更新时间
        """
        d = self.data
        img = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), BG_COLOR)
        draw = ImageDraw.Draw(img)

        # 卡片白色圆角背景
        _draw_rounded_rect(
            draw, (6, 6, CARD_WIDTH - 6, CARD_HEIGHT - 6),
            radius=10, fill=CARD_BG,
        )
        cx = CARD_WIDTH // 2
        content_x = CARD_PADDING + 6
        content_w = CARD_WIDTH - 2 * (CARD_PADDING + 6)
        y = CARD_PADDING + 4

        # ========== 1. 头部信息 ==========
        name = d.get("realname", "未知")
        deptname = d.get("deptname", "")
        college = d.get("college", "")
        major = d.get("major", "")
        student_id = d.get("student_id", "")

        _draw_rounded_rect(
            draw, (content_x, y, content_x + 4, y + 38),
            radius=2, fill=ACCENT_BLUE,
        )
        font_name = _get_font(18, bold=True)
        draw.text((content_x + 12, y), name, fill=TEXT_DARK, font=font_name)

        info_parts = []
        if student_id:
            info_parts.append(f"学号: {student_id}")
        if deptname:
            info_parts.append(deptname)
        if college:
            info_parts.append(college)
        if major:
            info_parts.append(major)
        font_info = _get_font(9)
        info_line = "  |  ".join(info_parts)
        draw.text((content_x + 12, y + 24), info_line, fill=TEXT_GRAY, font=font_info)

        y += 48
        draw.line([(content_x, y), (content_x + content_w, y)], fill="#ECECEC", width=1)
        y += 10

        # ========== 2. 总分环形 ==========
        total_score = float(d.get("total_score") or 0)
        progress = min(total_score / 100.0, 1.0)

        donut_cy = y + 48
        outer_r = 42
        inner_r = 30
        _draw_donut(draw, cx, donut_cy, outer_r, inner_r, progress, ACCENT_BLUE, "#E8EDF2")

        font_score = _get_font(16, bold=True)
        bbox = draw.textbbox((0, 0), f"{total_score:.1f}", font=font_score)
        tw = bbox[2] - bbox[0]
        draw.text((cx - tw // 2, donut_cy - 12), f"{total_score:.1f}",
                  fill=ACCENT_BLUE, font=font_score)
        font_label = _get_font(9)
        bbox = draw.textbbox((0, 0), "二课总分", font=font_label)
        tw = bbox[2] - bbox[0]
        draw.text((cx - tw // 2, donut_cy + 5), "二课总分",
                  fill=TEXT_GRAY, font=font_label)

        y += 108
        draw.line([(content_x, y), (content_x + content_w, y)], fill="#ECECEC", width=1)
        y += 8

        # ========== 3. 活动概况 ==========
        font_section = _get_font(11, bold=True)
        draw.text((content_x, y), "活动概况", fill=TEXT_DARK, font=font_section)

        total_act = int(d.get("activity_count", 0))
        unsigned_v = int(d.get("unsigned_count", 0))
        unfinished_v = int(d.get("unfinished_count", 0))
        club_v = int(d.get("club_count", 0))
        completed_v = max(total_act - unsigned_v - unfinished_v, 0)

        # 活动总数
        y += 20
        font_total_label = _get_font(10)
        font_total_val = _get_font(14, bold=True)
        draw.text((content_x, y), "活动总数", fill=TEXT_GRAY, font=font_total_label)
        bbox = draw.textbbox((0, 0), str(total_act), font=font_total_val)
        dw = bbox[2] - bbox[0]
        draw.text((content_x + content_w - dw, y), str(total_act),
                  fill=ACCENT_BLUE, font=font_total_val)

        # 堆叠条
        stack_items = [
            ("已完成", completed_v, "#2ECC71"),
            ("未签到", unsigned_v, ACCENT_ORANGE),
            ("未提交", unfinished_v, ACCENT_RED),
        ]
        stack_total = max(sum(v for _, v, _ in stack_items), 1)

        y += 22
        stack_h = 22
        _draw_rounded_rect(
            draw, (content_x, y, content_x + content_w, y + stack_h),
            radius=4, fill="#F0F0F0",
        )
        cx_seg = content_x
        for label, val, color in stack_items:
            if val <= 0:
                continue
            seg_w = int(content_w * val / stack_total)
            seg_w = max(seg_w, 1)
            _draw_rounded_rect(
                draw, (cx_seg, y, cx_seg + seg_w, y + stack_h),
                radius=4, fill=color,
            )
            if seg_w >= 22:
                font_seg = _get_font(8, bold=True)
                seg_label = str(val)
                bbox = draw.textbbox((0, 0), seg_label, font=font_seg)
                sw = bbox[2] - bbox[0]
                if sw <= seg_w - 4:
                    draw.text((cx_seg + (seg_w - sw) // 2, y + (stack_h - 8) // 2),
                              seg_label, fill="#FFFFFF", font=font_seg)
            cx_seg += seg_w

        # 图例（两行）
        y += stack_h + 6
        font_legend = _get_font(8)
        # 第一行：已完成 + 未签到
        lx = content_x
        for label, val, color in [stack_items[0], stack_items[1]]:
            draw.rectangle([(lx, y + 2), (lx + 8, y + 10)], fill=color)
            draw.text((lx + 12, y), f"{label} {val}", fill=TEXT_GRAY, font=font_legend)
            bbox = draw.textbbox((0, 0), f"{label} {val}", font=font_legend)
            lx += bbox[2] - bbox[0] + 14
        # 第二行：未提交
        y += 14
        lx = content_x
        label, val, color = stack_items[2]
        draw.rectangle([(lx, y + 2), (lx + 8, y + 10)], fill=color)
        draw.text((lx + 12, y), f"{label} {val}", fill=TEXT_GRAY, font=font_legend)

        # 社团
        y += 18
        font_club_label = _get_font(9)
        font_club_val = _get_font(12, bold=True)
        draw.text((content_x, y), "我的社团", fill=TEXT_GRAY, font=font_club_label)
        bbox = draw.textbbox((0, 0), f"{club_v} 个", font=font_club_val)
        cw = bbox[2] - bbox[0]
        draw.text((content_x + content_w - cw, y), f"{club_v} 个",
                  fill=ACCENT_PURPLE, font=font_club_val)

        y += 22
        draw.line([(content_x, y), (content_x + content_w, y)], fill="#ECECEC", width=1)
        y += 8

        # ========== 4. 累计时长 ==========
        semester = d.get("year_id", "20252026")
        if len(semester) == 8 and semester.isdigit():
            display_sem = f"{semester[:4]}-{semester[4]}"
        else:
            display_sem = semester

        font_section = _get_font(11, bold=True)
        draw.text((content_x, y), f"当前学期累计志愿时长 {display_sem}", fill=TEXT_DARK, font=font_section)

        total_duration = float(d.get('total_duration') or 0)
        font_total_val = _get_font(14, bold=True)
        duration_text = f"{total_duration:.1f} 小时"
        bbox = draw.textbbox((0, 0), duration_text, font=font_total_val)
        dw = bbox[2] - bbox[0]
        draw.text((content_x + content_w - dw, y), duration_text, fill="#3498DB", font=font_total_val)

        y += 30
        draw.line([(content_x, y), (content_x + content_w, y)], fill="#ECECEC", width=1)
        y += 10

        # ========== 5. 分类积分四边形雷达图 ==========
        # 学期描述显示
        if len(semester) == 8 and semester.isdigit():
            # 20252026 → 2025-2026学年 + 末位(1=秋季/2=春季)
            sem_season = "春季" if semester[7] == "2" else "秋季"
            display_title = f"当前学期（{semester[:4]}-{semester[4:6]}学年{sem_season}学期）"
        else:
            display_title = f"当前学期（{semester}）"

        font_section = _get_font(11, bold=True)
        draw.text((content_x, y), display_title,
                  fill=TEXT_DARK, font=font_section)
        y += 20

        # 向下移动雷达图，避免顶部标签被标题挡住
        y += 10

        cat_items = [
            ("思想政治", float(d.get("ideology_score") or 0), ACCENT_RED),
            ("劳动教育", float(d.get("labor_score") or 0), ACCENT_ORANGE),
            ("文艺美育", float(d.get("art_score") or 0), ACCENT_PURPLE),
            ("志愿服务", float(d.get("volunteer_score") or 0), ACCENT_GREEN),
        ]
        cat_max = max((v for _, v, _ in cat_items), default=1)
        cat_max = max(cat_max, 1)

        radar_cy = y + 70
        radius = 55
        label_dist = 70
        angles_deg = [-90, 0, 90, 180]

        def _polar(deg: float, r: float) -> tuple[float, float]:
            rad = math.radians(deg)
            return cx + r * math.cos(rad), radar_cy + r * math.sin(rad)

        for level in [0.25, 0.5, 0.75, 1.0]:
            pts = [_polar(angles_deg[i], radius * level) for i in range(4)]
            draw.polygon(pts, outline="#E0E0E0", width=1)

        for i in range(4):
            end = _polar(angles_deg[i], radius)
            draw.line([(cx, radar_cy), end], fill="#D0D0D0", width=1)

        data_pts = []
        for i, (_, val, _) in enumerate(cat_items):
            r_val = radius * (val / cat_max) if cat_max > 0 else 0
            data_pts.append(_polar(angles_deg[i], r_val))

        if data_pts:
            overlay = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay)
            overlay_draw.polygon(data_pts, fill=(74, 144, 217, 60))
            img.paste(overlay, (0, 0), overlay)
            draw.polygon(data_pts, outline=ACCENT_BLUE, width=2)
            for pt in data_pts:
                draw.ellipse(
                    [(pt[0] - 3, pt[1] - 3), (pt[0] + 3, pt[1] + 3)],
                    fill=ACCENT_BLUE, outline="#FFFFFF", width=2,
                )

        font_cat_label = _get_font(9, bold=True)
        font_cat_val = _get_font(8)
        for i, (label, val, color) in enumerate(cat_items):
            lp = _polar(angles_deg[i], label_dist)
            bbox = draw.textbbox((0, 0), label, font=font_cat_label)
            lw, lh = bbox[2] - bbox[0], bbox[3] - bbox[1]
            if angles_deg[i] == 0:  # 右侧
                draw.text((lp[0], lp[1] - lh // 2), label, fill=color, font=font_cat_label)
            elif angles_deg[i] == 90:  # 底部
                draw.text((lp[0] - lw // 2, lp[1] - lh), label, fill=color, font=font_cat_label)
            elif angles_deg[i] == 180:  # 左侧
                draw.text((lp[0] - lw, lp[1] - lh // 2), label, fill=color, font=font_cat_label)
            else:  # 顶部
                draw.text((lp[0] - lw // 2, lp[1]), label, fill=color, font=font_cat_label)

            vp = _polar(angles_deg[i], radius + 8)
            val_text = f"{val:.1f}"
            bbox2 = draw.textbbox((0, 0), val_text, font=font_cat_val)
            vw = bbox2[2] - bbox2[0]
            draw.text((vp[0] - vw // 2, vp[1] - 5),
                      val_text, fill=TEXT_GRAY, font=font_cat_val)

        font_center = _get_font(8)
        total = sum(v for _, v, _ in cat_items)
        center_label = f"合计\n{total:.1f}分"
        bbox_c = draw.textbbox((0, 0), center_label, font=font_center)
        cw = bbox_c[2] - bbox_c[0]
        draw.text((cx - cw // 2, radar_cy - 8), center_label,
                  fill=TEXT_GRAY, font=font_center)

        # ========== 5b. 及格要求 ==========
        req_y = radar_cy + radius + 22
        font_req = _get_font(7)
        req_text = "及格要求：每学期总分6分  思想政治1分  劳动教育1分  志愿服务1分  文艺美育1分  志愿时长20h"
        bbox_req = draw.textbbox((0, 0), req_text, font=font_req)
        rw = bbox_req[2] - bbox_req[0]
        draw.text((cx - rw // 2, req_y), req_text, fill=TEXT_LIGHT, font=font_req)

        # ========== 6. 底部 ==========
        font_footer = _get_font(8)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        footer = f"数据更新时间: {d.get('updated_at', now_str)}"
        bbox = draw.textbbox((0, 0), footer, font=font_footer)
        fw = bbox[2] - bbox[0]
        draw.text(
            (CARD_WIDTH - content_x - fw, CARD_HEIGHT - CARD_PADDING - 14),
            footer, fill=TEXT_LIGHT, font=font_footer,
        )

        return img

    def save(self, path: Path) -> Path:
        """渲染并保存 PNG。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        img = self.render()
        img.save(str(path), "PNG")
        log.info("二课信息图已保存: %s (size=%dx%d)", path, img.width, img.height)
        return path


# ---------- 便捷入口 ----------

def render_secondclass_chart(data: dict, output_path: Path | None = None) -> Path:
    """渲染二课信息图并返回路径。默认保存到对应用户的学号子文件夹。"""
    if output_path is None:
        student_id = data.get("student_id", "unknown")
        from schedule import SCHEDULE_DIR
        user_dir = SCHEDULE_DIR / student_id
        user_dir.mkdir(parents=True, exist_ok=True)
        output_path = user_dir / "secondclass.png"
    renderer = SecondClassRenderer(data)
    renderer.save(output_path)
    return output_path


def generate_sample_chart() -> Path:
    """使用示例数据生成样图（用于验证视觉效果）。"""
    sample_data = {
        "realname": "沈昱作",
        "student_id": "2403740",
        "deptname": "24大数据3班（学徒制）",
        "college": "电子信息工商学院",
        "major": "大数据技术",
        "total_score": 38.5,
        "activity_count": 219,
        "unsigned_count": 77,
        "unfinished_count": 57,
        "club_count": 23,
        "thought_score": 12.5,
        "skill_score": 18.0,
        "career_score": 8.0,
        "ideology_score": 12.5,
        "labor_score": 8.0,
        "art_score": 6.0,
        "volunteer_score": 12.0,
        "total_duration": 45.5,
        "year_id": "20252026",
        "updated_at": "2026-06-03T19:44:52",
    }
    output_path = OUTPUT_DIR / "sample_secondclass_chart.png"
    return render_secondclass_chart(sample_data, output_path)
