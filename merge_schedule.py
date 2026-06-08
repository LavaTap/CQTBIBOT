"""课表 JSON 合并工具。

读取 schedules/{student_id}/{semester}.json 和 {semester}-new.json，
剥离 JS 风格 // 注释后，合并 courses 数组（去重），输出合并后的 JSON。

用法：
    python merge_schedule.py
        # 默认 schedules/2403740/2025-2026-2.json + -new.json 合并

    python merge_schedule.py --sid 2403740 --sem 2025-2026-2
        # 指定学号和学期

    python merge_schedule.py --base path/to/base.json --new path/to/new.json
        # 指定两个独立文件

    python merge_schedule.py --output merged.json
        # 指定输出路径（默认覆盖原 base.json）
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def strip_js_comments(text: str) -> str:
    """去除 JSON 中的 JS 风格 // 和 /* */ 注释。"""
    # 去除 // 单行注释（注意保留字符串内的 //）
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    # 去除 /* */ 多行注释
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return text


def load_json(path: Path) -> dict | None:
    """加载 JSON 文件，自动去除 // 注释。"""
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        cleaned = strip_js_comments(raw)
        return json.loads(cleaned)
    except (OSError, json.JSONDecodeError) as e:
        print(f"❌ 解析失败 {path.name}: {e}")
        return None


def validate_course(c: dict, idx: int) -> list[str]:
    """校验单个课程字段，返回错误列表。"""
    errors: list[str] = []
    if not str(c.get("name", "")).strip():
        errors.append(f"courses[{idx}] name 为空")
    day = c.get("day", 0)
    if not isinstance(day, int) or day < 1 or day > 7:
        errors.append(f"courses[{idx}] day={day} 超出 1-7")
    ps = c.get("period_start", 0)
    pe = c.get("period_end", 0)
    if not isinstance(ps, int) or ps < 0 or ps > 12:
        errors.append(f"courses[{idx}] period_start={ps} 超出 0-12")
    if not isinstance(pe, int) or pe < 0 or pe > 12:
        errors.append(f"courses[{idx}] period_end={pe} 超出 0-12")
    if isinstance(ps, int) and isinstance(pe, int) and pe < ps:
        errors.append(f"courses[{idx}] period_end({pe}) < period_start({ps})")
    weeks = str(c.get("weeks", "")).strip()
    if weeks and not re.match(r"^[\d,\-]+$", weeks):
        errors.append(f"courses[{idx}] weeks 格式异常: {weeks}")
    return errors


def course_key(c: dict) -> tuple:
    """生成课程去重键。"""
    return (
        str(c.get("name", "")).strip(),
        str(c.get("teacher", "")).strip(),
        str(c.get("weeks", "")).strip(),
        c.get("day", 0),
        c.get("period_start", 0),
        c.get("period_end", 0),
        str(c.get("room", "")).strip(),
    )


def merge(base: dict, new: dict) -> dict:
    """合并 base 和 new 的 courses，以 (name,teacher,weeks,day,periods,room) 去重。"""
    result = dict(base)

    existing = {course_key(c) for c in base.get("courses", [])}
    merged = list(base.get("courses", []))

    added = 0
    for c in new.get("courses", []):
        k = course_key(c)
        if k not in existing:
            existing.add(k)
            merged.append(c)
            added += 1

    result["courses"] = merged
    return result, added


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="课表 JSON 合并工具")
    parser.add_argument("--sid", default="2403740", help="学号（默认 2403740）")
    parser.add_argument("--sem", default="2025-2026-2", help="学期（默认 2025-2026-2）")
    parser.add_argument("--base", help="基础 JSON 路径（覆盖 --sid/--sem）")
    parser.add_argument("--new", help="new JSON 路径（覆盖 --sid/--sem）")
    parser.add_argument("--output", help="输出路径（默认覆盖 base）")
    args = parser.parse_args()

    # 确定路径
    if args.base and args.new:
        base_path = Path(args.base)
        new_path = Path(args.new)
    else:
        base_dir = Path(__file__).resolve().parent / "schedules" / args.sid
        base_path = base_dir / f"{args.sem}.json"
        new_path = base_dir / f"{args.sem}-new.json"

    output_path = Path(args.output) if args.output else new_path

    # 加载
    print(f"📖 基础: {base_path}")
    base = load_json(base_path)
    if base is None:
        sys.exit(1)

    print(f"📖 new:   {new_path}")
    new = load_json(new_path)
    if new is None:
        print("⚠️  未找到 new 文件，直接输出基础 JSON")
        output_path.write_text(
            json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"✅ 已写入: {output_path}")
        return

    # 校验一致性
    errors = []
    if base.get("semester") != new.get("semester"):
        errors.append(f"semester 不一致: base={base.get('semester')} new={new.get('semester')}")
    if base.get("student_id") != new.get("student_id"):
        errors.append(f"student_id 不一致: base={base.get('student_id')} new={new.get('student_id')}")

    # 校验每条课程
    for i, c in enumerate(new.get("courses", [])):
        errors.extend(validate_course(c, i))

    if errors:
        print("❌ 校验失败:")
        for e in errors:
            print(f"   • {e}")
        sys.exit(1)

    # 合并
    result, added = merge(base, new)
    total = len(result["courses"])

    # 输出
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"✅ 合并完成: 新增 {added} 门课，共 {total} 门")
    print(f"📁 输出: {output_path}")


if __name__ == "__main__":
    main()
