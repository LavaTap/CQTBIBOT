"""渲染 #帮助 指令列表为静态图片，替代纯文本回复。

渲染（render）与发送分离：
  - 模块首次导入时自动预渲染两个版本的帮助图片到磁盘
  - #帮助 指令只读文件发送，绝对不会触发实时渲染
  - 指令列表变更后重启进程即可刷新（文件名含哈希，旧文件自动清理）

依赖：Pillow（PIL），已存在于项目依赖中。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("help_image")
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── 缓存目录 ──
_HELP_DIR = PROJECT_ROOT / "_temp" / "help_images"

# ── 样式常量 ──
WIDTH = 680
PAD = 24
LINE_HEIGHT = 28
TITLE_HEIGHT = 52
SECTION_GAP = 8
BG_COLOR = (245, 247, 250)       # 浅灰蓝底
CARD_COLOR = (255, 255, 255)     # 白色卡片
TITLE_COLOR = (30, 64, 175)      # 深蓝色标题
SECTION_COLOR = (66, 133, 244)   # 蓝色分区标题
CMD_COLOR = (30, 64, 175)        # 指令名蓝色
DESC_COLOR = (80, 80, 80)        # 说明文字深灰
FOOTER_COLOR = (150, 150, 150)   # 底部灰色
BORDER_COLOR = (220, 220, 220)   # 边框浅灰
ACCENT_GREEN = (52, 168, 83)     # 绿色强调

# ── 字体 ──
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """加载系统字体，fallback 到默认。"""
    try:
        if bold:
            return ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", size)
        return ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", size)
    except (IOError, OSError):
        try:
            return ImageFont.truetype("C:/Windows/Fonts/simsun.ttc", size)
        except (IOError, OSError):
            return ImageFont.load_default()


FONT_TITLE = _font(22, bold=True)
FONT_SECTION = _font(14, bold=True)
FONT_CMD = _font(13, bold=True)
FONT_DESC = _font(12)
FONT_FOOTER = _font(11)

# ── 指令数据 ──
ALL_COMMANDS = [
    ("📋 基本指令", [
        ("#帮助", "查看本帮助图片（所有人可用）"),
        ("#扫码登录", "扫码安全登录（所有人，推荐）"),
        ("#登录", "账号密码+验证码登录（所有人）"),
        ("#取消", "取消当前登录流程（所有人）"),
    ]),
    ("📅 课表相关", [
        ("#更新课表", "拉取个人课表并渲染本周课表图片"),
        ("#本周课表", "查看本周课表图片"),
        ("#今日课表", "查看今日课表卡片"),
        ("#明天课表", "查看明天课表卡片"),
        ("#第N周课表", "查看指定周次（如 #第17周课表）"),
        ("#导出课表", "导出个人课表为 Excel 文件"),
        ("#更新模板课表", "获取 2403740 模板课表文件"),
    ]),
    ("🔄 凭证与更新", [
        ("#更新", "更新登录凭证（需先登录）"),
        ("#密码更新", "用保存的密码重登刷新凭证"),
    ]),
    ("📊 第二课堂", [
        ("#二课信息", "查询第二课堂活动与积分"),
        ("#二课图表", "生成第二课堂信息图表"),
        ("#报名 [活动ID]", "二课活动报名，不带 ID 时 bot 会询问"),
        ("#预约报名 <活动ID>", "预约「报名未开始」的活动，到点 @ 提醒"),
        ("#签到 <活动ID>", "签到（默认渠道 5，签到/签退共用端点）"),
        ("#签退 <活动ID>", "签退活动（同端点 isSignOut=1）"),
        ("#扫码签到", "发送指令后，拍大屏二维码图片发过来自动签到"),
        ("#我的预约", "查看当前所有预约报名"),
    ]),
]

ADMIN_COMMANDS = [
    ("🛠️ 管理员指令", [
        ("#更新调试", "自动更新课表+二课信息（调试用）"),
        ("#查询用户", "导出用户列表为 Excel"),
        ("#二课列表", "二课活动列表分页图表"),
        ("#查看二课", "渲染全部活动卡片并发送"),
        ("#我的二课", "查询用户的未结束活动卡片（报名中/活动中/未开始）"),
    ]),
]


# ── 辅助函数 ──

def _commands_hash() -> str:
    """计算当前指令列表的 MD5 哈希。"""
    raw = json.dumps([ALL_COMMANDS, ADMIN_COMMANDS],
                     ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()


def _measure_section_height(sections: list[tuple[str, list[tuple[str, str]]]],
                            ) -> int:
    """计算所有分区所需的总高度。"""
    h = TITLE_HEIGHT
    for _name, cmds in sections:
        h += SECTION_GAP + LINE_HEIGHT  # 分区标题
        for _ in cmds:
            h += LINE_HEIGHT  # 每行指令
        h += 8  # 分区底部间距
    h += SECTION_GAP + LINE_HEIGHT + 20  # 页脚
    return h


# ── 内部渲染（模块加载时执行，不对外暴露）──

def _render(is_admin: bool) -> bytes:
    """渲染单版本帮助图片，返回 PNG bytes。"""
    sections = list(ALL_COMMANDS)
    if is_admin:
        sections.extend(ADMIN_COMMANDS)

    total_height = _measure_section_height(sections)
    img = Image.new("RGB", (WIDTH, total_height), BG_COLOR)
    draw = ImageDraw.Draw(img)

    y = 0

    # ── 标题栏 ──
    draw.rectangle((0, 0, WIDTH, TITLE_HEIGHT), fill=TITLE_COLOR)
    draw.text((PAD, 12), "📖 可用指令列表", fill=(255, 255, 255), font=FONT_TITLE)
    y = TITLE_HEIGHT

    # ── 分区循环 ──
    for section_name, cmds in sections:
        y += SECTION_GAP

        # 分区标题背景
        title_h = LINE_HEIGHT + 4
        draw.rectangle((PAD - 4, y, WIDTH - PAD + 4, y + title_h),
                       fill=CARD_COLOR, outline=BORDER_COLOR)
        # 左侧色条
        draw.rectangle((PAD - 4, y, PAD + 2, y + title_h), fill=SECTION_COLOR)
        draw.text((PAD + 12, y + 2), section_name, fill=SECTION_COLOR, font=FONT_SECTION)
        y += title_h

        # 指令行
        for cmd, desc in cmds:
            row_bg = CARD_COLOR if (y // LINE_HEIGHT) % 2 == 0 else BG_COLOR
            draw.rectangle((PAD - 4, y, WIDTH - PAD + 4, y + LINE_HEIGHT),
                           fill=row_bg)
            draw.text((PAD + 12, y + 2), cmd, fill=CMD_COLOR, font=FONT_CMD)
            draw.text((PAD + 180, y + 2), desc, fill=DESC_COLOR, font=FONT_DESC)
            y += LINE_HEIGHT

    # ── 页脚 ──
    y += 8
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    draw.text((PAD, y), f"生成时间: {now_str}", fill=FOOTER_COLOR, font=FONT_FOOTER)

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ── 公共接口 ──

def get_help_image(is_admin: bool = False) -> Path:
    """获取预渲染的帮助图片文件路径。

    仅返回已存在的磁盘缓存文件，**绝不触发渲染**。
    模块导入时已自动预渲染两个版本。

    Args:
        is_admin: 是否包含管理员指令。

    Returns:
        图片文件路径（PNG 格式）。

    Raises:
        FileNotFoundError: 帮助图片未在导入时成功生成。
    """
    h = _commands_hash()
    suffix = "admin" if is_admin else "user"
    path = _HELP_DIR / f"help_{h}_{suffix}.png"
    if not path.exists():
        raise FileNotFoundError(
            f"帮助图片未生成（模块导入时渲染失败）: {path}")
    return path


# ── 模块导入时：预渲染两个版本 ──
_HELP_DIR.mkdir(parents=True, exist_ok=True)
_current_hash = _commands_hash()

for _is_admin in (False, True):
    _suffix = "admin" if _is_admin else "user"
    _path = _HELP_DIR / f"help_{_current_hash}_{_suffix}.png"
    if not _path.exists():
        log.info("预渲染帮助图片: %s", _path.name)
        _data = _render(is_admin=_is_admin)
        _ = _path.write_bytes(_data)
    else:
        log.info("帮助图片已存在: %s", _path.name)

# 清理旧版本缓存（保留当前哈希的文件，仅保留两个版本）
_KEEP_USER = f"help_{_current_hash}_user.png"
_KEEP_ADMIN = f"help_{_current_hash}_admin.png"
for _old in _HELP_DIR.glob("help_*.png"):
    if _old.name != _KEEP_USER and _old.name != _KEEP_ADMIN:
        _old.unlink(missing_ok=True)
