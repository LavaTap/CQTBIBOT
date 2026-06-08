"""apply_start 时间字符串多格式解析。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_RE_FULL = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})")
_RE_NO_YEAR = re.compile(r"^(\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{2})")
_RE_RANGE_PART = re.compile(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2})")


def parse_apply_start(raw: str) -> datetime | None:
    """解析多种格式的 apply_start，返回本地 naive datetime；失败返回 None。

    支持：
        '2026-06-08 10:00'                       全格式
        '06-08 10:00'                            无年份（当年；若已过 6 个月则视为明年）
        '2026-06-08 10:00 - 2026-06-09 10:00'    范围 → 取起点
    """
    if not raw:
        return None
    s = str(raw).strip()
    m_range = _RE_RANGE_PART.search(s)
    if m_range and "-" in s and s.count(":") >= 2:
        s = m_range.group(1)
    s = s.replace("T", " ")

    m = _RE_FULL.match(s)
    if m:
        y, mo, d, h, mi = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d, h, mi)
        except ValueError:
            return None
    m = _RE_NO_YEAR.match(s)
    if m:
        mo, d, h, mi = (int(x) for x in m.groups())
        now = datetime.now()
        try:
            candidate = datetime(now.year, mo, d, h, mi)
        except ValueError:
            return None
        if candidate < now - timedelta(days=180):
            try:
                candidate = candidate.replace(year=now.year + 1)
            except ValueError:
                return None
        return candidate
    return None
