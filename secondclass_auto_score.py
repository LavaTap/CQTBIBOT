"""二课自动积分工具 — 自动报名、签到、签退、提交总结。

用法:
  python secondclass_auto_score.py status           查看积分仪表盘
  python secondclass_auto_score.py signup            批量报名活动
  python secondclass_auto_score.py signup --dry-run  仅显示可报名活动
  python secondclass_auto_score.py probe             探测签到/签退/总结端点
  python secondclass_auto_score.py monitor           启动签到/签退监控
  python secondclass_auto_score.py run               全自动模式(报名+监控)
  python secondclass_auto_score.py ticket            门票→SSID转换
  python secondclass_auto_score.py ticket --token xxx --ticket xxx
  python secondclass_auto_score.py ticket --json creds.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import requests as _requests  # noqa: E402

from secondclass import secondclass_tool  # noqa: E402

log = logging.getLogger("auto_score")

# ── 配置 ──

DEFAULT_CONFIG = {
    "year_term": "20252026-2",
    "campus_latitude": "29.97",
    "campus_longitude": "106.27",
    "poll_interval_seconds": 60,
    "signup_delay_seconds": 3.0,
    "sign_in_early_seconds": 30,
    "sign_out_late_seconds": 60,
    "max_captcha_retries": 3,
    "max_requests_per_minute": 25,
    "skip_location_sign_in": True,
    "skip_captcha": False,
}

CONFIG_DIR = PROJECT_ROOT / "_temp"
CONFIG_FILE = CONFIG_DIR / "auto_score_config.json"
STATE_FILE = CONFIG_DIR / "auto_score_state.json"


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            cfg = {**DEFAULT_CONFIG, **saved}
            return cfg
        except Exception:
            pass
    return {**DEFAULT_CONFIG}


def _save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"activities": {}, "last_full_scan": ""}


def _save_state(state: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ── 获取用户会话 ──


def _get_session() -> tuple[_requests.Session, dict]:
    """获取已认证的会话和用户信息。

    优先使用环境变量 AUTO_SCORE_SSID 或 --ssid 参数提供的 SSID。
    否则从本地存储的凭证自动获取。
    """
    # 检查环境变量
    ssid = os.environ.get("AUTO_SCORE_SSID", "")
    if not ssid and hasattr(_get_session, "_ssid_override"):
        ssid = _get_session._ssid_override

    if ssid:
        sess = secondclass_tool._session(ssid)
        # 验证SSID是否有效
        try:
            r = sess.get(
                f"{secondclass_tool.BASE_URL}/Student/My/index.html", timeout=10
            )
            if r.status_code == 200 and "top.location.href" not in r.text:
                print(f"使用SSID: {ssid[:8]}...")
                user = {"student_id": "", "realname": ""}
                # 尝试从页面提取学生信息
                return sess, user
        except Exception:
            print("SSID已过期")

    # 从存储的凭证获取
    users = secondclass_tool.get_all_user_credentials()
    if not users:
        print("错误: 没有找到用户凭证，请先通过 #扫码登录")
        sys.exit(1)

    user = users[0]
    print(f"使用用户: {user.get('realname', '?')} ({user.get('student_id', '?')})")

    try:
        sess = secondclass_tool.obtain_secondclass_session_from_user(user)
        return sess, user
    except secondclass_tool.SecondClassAuthError as e:
        print(f"错误: 认证失败 — {e}")
        sys.exit(1)


# ════════════════════ 积分仪表盘 ════════════════════


def cmd_status(args) -> None:
    """显示积分仪表盘。"""
    sess, user = _get_session()
    cfg = _load_config()

    # 获取详细积分数据
    print("正在获取积分数据...")
    try:
        data = secondclass_tool.fetch_score_data_json(sess, year_term=cfg["year_term"])
    except Exception as e:
        log.warning("getScoreDataJson 失败: %s, 回退到旧接口", e)
        data = None

    # 获取首页计数
    try:
        counts = secondclass_tool.fetch_index_counts_with_session(sess)
    except Exception:
        counts = {}

    print()
    print("=" * 50)
    print("  二课积分仪表盘")
    print("=" * 50)

    if data:
        student = data.get("dataStudent") or {}
        print(f"  姓名: {student.get('studentName', '?')}")
        print(f"  学号: {student.get('studentID', '?')}")
        print(f"  班级: {student.get('className', '?')}")
        print(f"  学院: {student.get('collegeName', '?')}")
        print(f"  专业: {student.get('major', '?')}")
        print()

        total = float(data.get("scoreTotal", 0))
        limit = float(data.get("scoreTotalLimit", 0))
        status = "[OK] 达标" if total >= limit else "[!!] 未达标"
        print(f"  总积分: {total} / {limit}  {status}")
        print(f"  总时长: {data.get('hoursTotal', 0)} 小时")
        print()

        modules = data.get("modules", [])
        if modules:
            print("  ┌──────────────────────┬────────┬────────┬────────┐")
            print("  │ 模块                 │  当前  │  需要  │  差距  │")
            print("  ├──────────────────────┼────────┼────────┼────────┤")
            for m in modules:
                name = m.get("name", "")
                zf = float(m.get("zf") or 0)
                avg = float(m.get("avg") or 0)
                gap = zf - avg
                gap_str = f"{gap:+.1f}" if gap < 0 else "  OK"
                print(f"  │ {name:<20s} │ {zf:>5.1f}  │ {avg:>5.1f}  │ {gap_str:>6s} │")
            print("  └──────────────────────┴────────┴────────┴────────┘")
    else:
        # 回退到旧接口
        try:
            all_data = secondclass_tool.fetch_all_with_session(sess)
            print(f"  总积分: {all_data.get('total_score', 0)}")
            print(f"  思想成长: {all_data.get('thought_score', 0)}")
            print(f"  专业技能: {all_data.get('skill_score', 0)}")
            print(f"  职业技能: {all_data.get('career_score', 0)}")
        except Exception as e:
            print(f"  获取积分数据失败: {e}")

    # 首页计数
    if counts:
        print()
        act_count = int(counts.get("activity_count", 0))
        unsigned = int(counts.get("unsigned_count", 0))
        unfinished = int(counts.get("unfinished_count", 0))
        club = int(counts.get("club_count", 0))
        print(
            f"  我的活动: {act_count}  未签到: {unsigned}  未总结: {unfinished}  社团: {club}"
        )

    print("=" * 50)


# ════════════════════ 自动报名 ════════════════════

def _manual_captcha_input(sess, *, activity_name: str = "") -> str:
    """获取验证码图片并提示用户手动输入。

    将验证码图片保存到临时文件并用系统默认图片查看器打开，
    等待用户在终端输入验证码。

    Returns:
        用户输入的验证码字符串。

    Raises:
        ActivityApplyError: 用户取消或输入失败。
    """
    # 获取验证码图片
    img_bytes = secondclass_tool.fetch_verifycode_image(sess)

    # 保存到临时文件
    tmp_dir = PROJECT_ROOT / "_temp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # 清理之前的旧验证码图片
    for old in tmp_dir.glob("captcha_*.jpg"):
        try:
            old.unlink()
        except Exception:
            pass
    tmp_path = tmp_dir / f"captcha_{int(time.time())}.jpg"
    tmp_path.write_bytes(img_bytes)

    # 用系统默认图片查看器打开
    title_info = f"活动「{activity_name}」" if activity_name else "该活动"
    print(f"\n{title_info}需要输入验证码")
    print(f"验证码图片已保存至: {tmp_path}")
    if sys.platform == "win32":
        os.startfile(str(tmp_path))
        print("已自动打开图片，请查看后输入验证码")
    else:
        try:
            import subprocess

            subprocess.Popen(
                ["xdg-open", str(tmp_path)],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print("已自动打开图片，请查看后输入验证码")
        except Exception:
            print("请手动打开图片查看验证码")

    # 用户输入
    for attempt in range(3):
        try:
            raw = input("请输入验证码 (直接回车重试, 输入 q 退出): ")
        except (EOFError, KeyboardInterrupt):
            print()
            raise secondclass_tool.ActivityApplyError("用户取消操作")

        code = raw.strip()
        if code.lower() == "q":
            raise secondclass_tool.ActivityApplyError("用户取消报名")
        if code:
            return code

        # 空输入 → 重试，重新获取验证码
        if attempt < 2:
            print("验证码为空，重新获取...")
            img_bytes = secondclass_tool.fetch_verifycode_image(sess)
            tmp_path.write_bytes(img_bytes)
            if sys.platform == "win32":
                os.startfile(str(tmp_path))

    raise secondclass_tool.ActivityApplyError("验证码输入失败（连续3次未输入）")


def auto_signup_activity(
    sess, activity_id: str, *, max_retries: int = 3, skip_captcha: bool = False
) -> dict:
    """自动报名一个活动（手动输入验证码）。

    Args:
        sess: 已认证的 requests.Session（含 SSID）。
        activity_id: 活动 ID。
        max_retries: 保留参数，未使用（手动输入本身含重试）。
        skip_captcha: 跳过需要验证码的活动。

    Returns:
        {"success": bool, "message": str}
    """
    # 获取报名页面
    try:
        apply_data = secondclass_tool.fetch_apply_page(sess, activity_id)
    except Exception as e:
        return {"success": False, "message": f"获取报名页面失败: {e}"}

    if not apply_data.get("s1") or not apply_data.get("s2"):
        return {"success": False, "message": "报名页面缺少必要字段"}

    activity_name = apply_data.get("activity_name", "")

    captcha_code = ""
    if apply_data.get("need_captcha") and not skip_captcha:
        try:
            captcha_code = _manual_captcha_input(
                sess, activity_name=activity_name
            )
        except Exception as e:
            return {"success": False, "message": f"验证码输入失败: {e}"}
    elif apply_data.get("need_captcha") and skip_captcha:
        return {"success": False, "message": "需要验证码，已跳过"}

    # 提交报名
    try:
        result = secondclass_tool.submit_activity_apply(
            sess,
            activity_id,
            captcha_code,
            apply_data["s1"],
            apply_data["s2"],
        )
        return result
    except Exception as e:
        return {"success": False, "message": f"报名请求失败: {e}"}


def filter_eligible_activities(
    activities: list[dict],
    *,
    student_college: str = "",
    student_grade: str = "",
    needed_modules: dict | None = None,
    exclude_ids: set | None = None,
    min_score: float = 0,
) -> list[dict]:
    """过滤出学生可报名且有助于积分缺口的活动。"""
    if exclude_ids is None:
        exclude_ids = set()
    result = []
    for act in activities:
        aid = str(act.get("activity_id", act.get("activityID", "")))
        if aid in exclude_ids:
            continue

        # 学院限制检查
        limit_college = act.get("limit_college", act.get("limitCollege", ""))
        if limit_college and student_college and limit_college != student_college:
            continue

        # 年级限制检查
        limit_grade = act.get("limit_grade", act.get("limitGrade", ""))
        if limit_grade and student_grade and student_grade not in str(limit_grade):
            continue

        # 最低积分
        score = float(act.get("score", 0))
        if min_score > 0 and score < min_score:
            continue

        # 模块需求优先排序
        module_name = act.get("module_name", act.get("moduleName", ""))
        priority = 0
        if needed_modules:
            for mod_name, gap in needed_modules.items():
                if mod_name in module_name and gap < 0:
                    priority = abs(gap)

        act["_priority"] = priority
        result.append(act)

    # 按优先级（模块缺口大的优先）和积分排序
    result.sort(
        key=lambda a: (a.get("_priority", 0), float(a.get("score", 0))), reverse=True
    )
    return result


def cmd_signup(args) -> None:
    """批量报名活动。"""
    sess, user = _get_session()
    cfg = _load_config()
    dry_run = args.dry_run
    max_signup = args.max

    # 获取积分缺口
    print("正在分析积分缺口...")
    needed_modules = {}
    try:
        data = secondclass_tool.fetch_score_data_json(sess, year_term=cfg["year_term"])
        for m in data.get("modules", []):
            zf = float(m.get("zf") or 0)
            avg = float(m.get("avg") or 0)
            if zf < avg:
                needed_modules[m.get("name", "")] = zf - avg
    except Exception as e:
        log.warning("获取积分缺口失败: %s", e)

    if needed_modules:
        print("积分缺口:")
        for name, gap in needed_modules.items():
            print(f"  {name}: 差 {abs(gap):.1f} 分")
    else:
        print("所有模块积分已达标或无法获取缺口信息")

    # 获取可报名活动
    print("\n正在获取可报名活动列表...")
    activities = secondclass_tool.fetch_activities_can_apply(
        sess, sort_by_score="desc", max_results=200
    )

    # 获取已报名活动ID
    my_activities = secondclass_tool.fetch_all_my_activities(sess)
    signed_ids = set()
    for tab_acts in my_activities.values():
        for act in tab_acts:
            aid = str(act.get("activity_id", act.get("activityID", "")))
            if aid:
                signed_ids.add(aid)

    # 获取学生信息
    student_college = user.get("college", "")
    student_grade = user.get("student_id", "")[:4] if user.get("student_id") else ""

    # 过滤
    eligible = filter_eligible_activities(
        activities,
        student_college=student_college,
        student_grade=student_grade,
        needed_modules=needed_modules,
        exclude_ids=signed_ids,
    )

    if not eligible:
        print("没有找到可报名的活动")
        return

    print(f"\n找到 {len(eligible)} 个可报名活动:")
    print("-" * 70)
    for i, act in enumerate(eligible[:max_signup], 1):
        aid = act.get("activity_id", act.get("activityID", ""))
        name = act.get("activity_name", act.get("activityName", "?"))
        score = act.get("score", 0)
        module = act.get("module_name", act.get("moduleName", ""))
        start = act.get("start_date", act.get("startDate", ""))
        if isinstance(start, int) and start > 0:
            start = datetime.fromtimestamp(start).strftime("%m-%d %H:%M")
        print(f"  {i:2d}. [{score:>4.1f}分] {name}")
        print(f"      模块: {module}  开始: {start}  ID: {aid}")

    if dry_run:
        print(f"\n[dry-run] 以上 {min(len(eligible), max_signup)} 个活动未实际报名")
        return

    # 实际报名
    print(f"\n开始报名 (最多 {max_signup} 个)...")
    success_count = 0
    fail_count = 0

    for i, act in enumerate(eligible[:max_signup], 1):
        aid = str(act.get("activity_id", act.get("activityID", "")))
        name = act.get("activity_name", act.get("activityName", "?"))
        score = act.get("score", 0)
        print(f"\n[{i}/{min(len(eligible), max_signup)}] 报名: {name} ({score}分)")

        result = auto_signup_activity(
            sess,
            aid,
            max_retries=cfg.get("max_captcha_retries", 3),
            skip_captcha=cfg.get("skip_captcha", False),
        )

        if result.get("success"):
            success_count += 1
            print(f"  ✓ 报名成功: {result.get('message', 'OK')}")
        else:
            fail_count += 1
            print(f"  ✗ 报名失败: {result.get('message', '未知错误')}")

        # 随机延迟
        delay = cfg.get("signup_delay_seconds", 3.0)
        jitter = random.uniform(0.5, 2.0)
        time.sleep(delay + jitter)

    print(f"\n报名完成: 成功 {success_count}, 失败 {fail_count}")


# ════════════════════ 端点探测 ════════════════════

# 候选端点列表（基于逆向分析）
# 签到: 通过扫码完成，QR码内容由主办方动态生成，无法伪造
# GPS签到: /Student/My/activityListGps.html（活动需开启gps=1）
# 总结提交: /Student/My/myActivitySummary.html（已确认）
PROBE_ENDPOINTS = {
    "签到": [
        "/Student/Activity/sign.html",
        "/Student/My/sign.html",
        "/Student/Activity/scanSign.html",
        "/Student/Activity/qrSign.html",
        "/Student/Activity/signIn.html",
        "/Student/Activity/doSign.html",
        "/Student/Activity/signGo.html",
    ],
    "GPS签到": [
        "/Student/My/activityListGps.html",
        "/Student/My/signGps.html",
        "/Student/Activity/gpsSign.html",
    ],
    "签退": [
        "/Student/Activity/signOut.html",
        "/Student/Activity/qiantui.html",
        "/Student/My/signOut.html",
        "/Student/Activity/activitySignOut.html",
    ],
    "提交总结": [
        "/Student/My/myActivitySummary.html",
        "/Student/Activity/submitSummary.html",
        "/Student/My/summary.html",
    ],
}


def probe_endpoints(sess, activity_id: str) -> dict[str, str | None]:
    """系统性探测签到/签退/总结的API端点。

    对每个候选URL发送POST请求，记录哪些返回了有效响应。
    """
    discovered = {}
    base = secondclass_tool.BASE_URL

    for action_name, paths in PROBE_ENDPOINTS.items():
        print(f"\n探测 {action_name} 端点:")
        found = None
        for path in paths:
            url = f"{base}{path}"
            try:
                # 尝试POST
                r = sess.post(
                    url,
                    data={"activityID": activity_id},
                    timeout=10,
                    headers={
                        "X-Requested-With": "XMLHttpRequest",
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "Referer": f"{base}/Student/My/myActivity.html",
                    },
                    allow_redirects=False,
                )
                status = r.status_code
                size = len(r.content)
                text_preview = r.text[:100].replace("\n", " ")

                if status == 200:
                    # 判断是否为有效响应（非登录重定向）
                    if (
                        "top.location.href" not in r.text
                        and "login" not in r.text.lower()[:200]
                    ):
                        print(f"  ✓ {path} → 200 ({size}B) {text_preview}")
                        found = path
                        break
                    else:
                        print(f"  - {path} → 200 (登录重定向)")
                elif status in (301, 302, 303, 307):
                    loc = r.headers.get("Location", "")
                    print(f"  - {path} → {status} → {loc[:60]}")
                elif status == 404:
                    pass  # 静默跳过404
                else:
                    print(f"  ? {path} → {status} ({size}B)")

            except Exception as e:
                print(f"  ! {path} → 异常: {e}")

            time.sleep(0.5)

        if found:
            discovered[action_name] = found
            print(f"  >>> 发现 {action_name} 端点: {found}")
        else:
            discovered[action_name] = None
            print(f"  >>> {action_name}: 未找到端点")

    return discovered


def cmd_summary(args) -> None:
    """批量提交活动总结。"""
    sess, user = _get_session()
    dry_run = args.dry_run
    max_submit = args.max

    print("正在获取需要总结的活动...")
    actions = fetch_my_activities_needing_action(sess)
    need_summary = actions["need_summary"]

    if not need_summary:
        print("没有需要提交总结的活动（需已签到且活动已结束）")
        return

    print(f"\n找到 {len(need_summary)} 个需要总结的活动:")
    print("-" * 60)
    for i, act in enumerate(need_summary[:max_submit], 1):
        name = act.get("activity_name", "?")
        score = act.get("score", 0)
        print(f"  {i:2d}. [{score:>4.1f}分] {name}")

    if dry_run:
        print(
            f"\n[dry-run] 以上 {min(len(need_summary), max_submit)} 个活动未实际提交总结"
        )
        return

    print(f"\n开始提交总结 (最多 {max_submit} 个)...")
    success_count = 0
    fail_count = 0

    for i, act in enumerate(need_summary[:max_submit], 1):
        aid = act.get("activity_id", "")
        name = act.get("activity_name", "?")
        score = act.get("score", 0)
        summary = generate_activity_summary(act)
        print(f"\n[{i}/{min(len(need_summary), max_submit)}] 总结: {name} ({score}分)")

        result = submit_activity_summary(sess, aid, summary)
        if result["success"]:
            success_count += 1
            print(f"  OK {result['message']}")
        else:
            fail_count += 1
            print(f"  FAIL {result['message']}")

        time.sleep(random.uniform(2, 4))

    print(f"\n总结提交完成: 成功 {success_count}, 失败 {fail_count}")


def cmd_probe(args) -> None:
    """探测签到/签退/总结端点。"""
    sess, user = _get_session()

    print("=" * 50)
    print("  二课系统逆向分析结果")
    print("=" * 50)
    print()
    print("已知信息:")
    print("  签到方式: 扫码签到（QR码由主办方生成，需微信/APP扫码）")
    print("  GPS签到:  /Student/My/activityListGps.html")
    print("  提交总结: /Student/My/myActivitySummary.html (已确认)")
    print("  未签到:   /Student/My/activityNoSign.html")
    print("  未总结:   /Student/My/activityNoSummary.html")
    print("  取消报名: /Student/Activity/applyCancel.html")
    print()

    # 先从我的活动中找一个进行中的活动ID
    print("正在获取活动列表...")
    my_activities = secondclass_tool.fetch_all_my_activities(sess)

    # 优先找"活动中"或"报名中"的活动
    test_id = ""
    for tab in ["活动中", "报名中", "未开始", "已结束"]:
        acts = my_activities.get(tab, [])
        if acts:
            test_id = str(acts[0].get("activity_id", acts[0].get("activityID", "")))
            if test_id:
                print(
                    f"使用测试活动: {acts[0].get('activity_name', acts[0].get('activityName', '?'))} (ID: {test_id})"
                )
                break

    if not test_id:
        print("没有找到可用的测试活动，尝试从活动列表获取...")
        activities = secondclass_tool.fetch_activities_can_apply(sess, max_results=1)
        if activities:
            test_id = str(
                activities[0].get("activity_id", activities[0].get("activityID", ""))
            )

    if not test_id:
        print("错误: 没有可用的活动ID进行探测")
        return

    # 额外：分析 myActivity.html 页面JS代码，寻找AJAX端点
    print("\n分析 myActivity.html 页面JS代码...")
    try:
        r = sess.get(
            f"{secondclass_tool.BASE_URL}/Student/My/myActivity.html", timeout=15
        )
        if r.status_code == 200:
            js_hints = _extract_js_endpoints(r.text)
            if js_hints:
                print("  从JS中发现可能的端点:")
                for hint in js_hints:
                    print(f"    - {hint}")
            else:
                print("  未从JS中发现明显的端点URL")
    except Exception as e:
        print(f"  JS分析失败: {e}")

    # 探测
    results = probe_endpoints(sess, test_id)

    # 保存发现的端点
    print("\n" + "=" * 50)
    print("探测结果汇总:")
    for action, endpoint in results.items():
        if endpoint:
            print(f"  {action}: {endpoint}")
        else:
            print(f"  {action}: 未发现")

    # 更新配置
    cfg = _load_config()
    for action, endpoint in results.items():
        if endpoint:
            cfg[f"endpoint_{action}"] = endpoint
    _save_config(cfg)


def _extract_js_endpoints(html: str) -> list[str]:
    """从HTML/JS中提取可能的AJAX端点URL。"""
    import re

    urls = []
    # 匹配 url: "...", $.post("..."), $.ajax({url: "...", mui.post("...
    patterns = [
        r'url\s*:\s*["\']([^"\']+\.html)["\']',
        r'\$\.(?:post|get|ajax)\s*\(\s*["\']([^"\']+\.html)["\']',
        r'mui\.(?:post|get|ajax)\s*\(\s*["\']([^"\']+\.html)["\']',
        r'fetch\s*\(\s*["\']([^"\']+\.html)["\']',
    ]
    for pat in patterns:
        for m in re.finditer(pat, html):
            url = m.group(1)
            if url not in urls and (
                "Sign" in url
                or "sign" in url
                or "summary" in url
                or "Summary" in url
                or "qd" in url
                or "qt" in url
            ):
                urls.append(url)
    return urls


# ════════════════════ 签到/签退/总结提交 ════════════════════


def submit_activity_sign_in(
    sess,
    activity_id: str,
    *,
    latitude: str = "",
    longitude: str = "",
    endpoint: str = "",
) -> dict:
    """提交活动签到。"""
    if not endpoint:
        cfg = _load_config()
        endpoint = cfg.get("endpoint_签到", "")

    if not endpoint:
        return {"success": False, "message": "签到端点未配置，请先运行 probe 命令"}

    base = secondclass_tool.BASE_URL
    url = f"{base}{endpoint}" if not endpoint.startswith("http") else endpoint

    data = {"activityID": activity_id}
    if latitude and longitude:
        data["latitude"] = latitude
        data["longitude"] = longitude

    try:
        r = sess.post(
            url,
            data=data,
            timeout=15,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": f"{base}/Student/My/myActivity.html",
            },
        )
        r.raise_for_status()
        result = r.json()
        success = bool(
            result.get("success")
            or result.get("result") == "1"
            or str(result.get("status")) == "1"
        )
        return {
            "success": success,
            "message": result.get("message", "签到" + ("成功" if success else "失败")),
        }
    except Exception as e:
        return {"success": False, "message": f"签到请求失败: {e}"}


def submit_activity_sign_out(
    sess,
    activity_id: str,
    *,
    latitude: str = "",
    longitude: str = "",
    endpoint: str = "",
) -> dict:
    """提交活动签退。"""
    if not endpoint:
        cfg = _load_config()
        endpoint = cfg.get("endpoint_签退", "")

    if not endpoint:
        return {"success": False, "message": "签退端点未配置，请先运行 probe 命令"}

    base = secondclass_tool.BASE_URL
    url = f"{base}{endpoint}" if not endpoint.startswith("http") else endpoint

    data = {"activityID": activity_id}
    if latitude and longitude:
        data["latitude"] = latitude
        data["longitude"] = longitude

    try:
        r = sess.post(
            url,
            data=data,
            timeout=15,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": f"{base}/Student/My/myActivity.html",
            },
        )
        r.raise_for_status()
        result = r.json()
        success = bool(
            result.get("success")
            or result.get("result") == "1"
            or str(result.get("status")) == "1"
        )
        return {
            "success": success,
            "message": result.get("message", "签退" + ("成功" if success else "失败")),
        }
    except Exception as e:
        return {"success": False, "message": f"签退请求失败: {e}"}


def submit_activity_summary(sess, activity_id: str, summary_text: str) -> dict:
    """提交活动总结。

    已确认端点: POST /Student/My/myActivitySummary.html
    需要先GET页面再POST（服务端session校验）。
    表单: multipart/form-data，字段: activityID, content, stars
    """
    base = secondclass_tool.BASE_URL
    ret_url = "JTJGU3R1ZGVudCUyRk15JTJGbXlBY3Rpdml0eS5odG1s"
    page_url = f"{base}/Student/My/myActivitySummary.html?activityID={activity_id}&retUrl={ret_url}"
    post_url = f"{base}/Student/My/myActivitySummary.html?retUrl={ret_url}"

    try:
        # 步骤1：GET页面（获取session状态）
        r1 = sess.get(
            page_url,
            timeout=15,
            headers={
                "Referer": f"{base}/Student/My/myActivity.html",
            },
        )
        if "请在活动结束后" in r1.text:
            return {"success": False, "message": "活动未结束，不能提交总结"}
        if "top.location.href" in r1.text:
            return {"success": False, "message": "登录已过期"}

        # 步骤2：POST提交（multipart/form-data）
        r2 = sess.post(
            post_url,
            timeout=15,
            headers={
                "Origin": base,
                "Referer": page_url,
            },
            files={
                "activityID": (None, activity_id),
                "content": (None, summary_text),
                "stars": (None, ""),
            },
        )
        if "成功" in r2.text:
            return {"success": True, "message": "总结提交成功"}
        if "出错" in r2.text or "错误" in r2.text:
            import re

            m = re.search(r'class="error">(.*?)</p>', r2.text)
            msg = m.group(1) if m else "提交失败"
            return {"success": False, "message": msg}
        if "top.location.href" in r2.text:
            return {"success": False, "message": "登录已过期"}
        return {"success": True, "message": "已提交（响应未明确标识成功）"}

    except Exception as e:
        return {"success": False, "message": f"总结提交失败: {e}"}


def fetch_my_activities_needing_action(sess) -> dict:
    """获取需要操作的活动列表。"""
    result = {"need_sign_in": [], "need_sign_out": [], "need_summary": []}

    # 使用专用的API获取未签到活动
    base = secondclass_tool.BASE_URL
    try:
        r = sess.post(
            f"{base}/Student/My/getActivityNoSign.html",
            data={"p": "1", "maxActivityID": ""},
            timeout=15,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": f"{base}/Student/My/activityNoSign.html",
            },
        )
        data = r.json()
        if isinstance(data, list) and len(data) >= 2:
            for a in data[1]:
                entry = {
                    "activity_id": str(a.get("activityID", "")),
                    "activity_name": a.get("activityName", ""),
                    "score": float(a.get("score", 0)),
                    "module_name": a.get("moduleName", ""),
                    "need_sign_out": a.get("needSignOut", "0"),
                    "gps": a.get("gps", "0"),
                    "summary_required": a.get("summaryRequired", "0"),
                }
                result["need_sign_in"].append(entry)
                if a.get("needSignOut") == "1":
                    result["need_sign_out"].append(entry)
    except Exception as e:
        log.warning("获取未签到活动失败: %s", e)

    # 获取需要总结的活动
    try:
        r = sess.post(
            f"{base}/Student/My/getActivityNoSummary.html",
            data={"p": "1", "maxActivityID": ""},
            timeout=15,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": f"{base}/Student/My/activityNoSummary.html",
            },
        )
        data = r.json()
        if isinstance(data, list) and len(data) >= 2:
            for a in data[1]:
                # 只加入已签到的（signDate不为None的）
                if a.get("signDate"):
                    entry = {
                        "activity_id": str(a.get("activityID", "")),
                        "activity_name": a.get("activityName", ""),
                        "score": float(a.get("score", 0)),
                        "module_name": a.get("moduleName", ""),
                    }
                    result["need_summary"].append(entry)
    except Exception as e:
        log.warning("获取未总结活动失败: %s", e)

    return result


# ════════════════════ 自动总结生成 ════════════════════


def generate_activity_summary(activity: dict) -> str:
    """根据活动信息生成总结文本。"""
    name = activity.get("activity_name", activity.get("activityName", "本次活动"))
    module = activity.get("module_name", activity.get("moduleName", ""))
    organizer = activity.get("organizer", activity.get("organizerName", ""))

    templates = [
        f"通过参加{module}模块的「{name}」活动，我收获颇丰。"
        f"本次活动由{organizer}举办，让我对该领域有了更深入的认识和理解。"
        f"在活动过程中，我认真聆听、积极参与，不仅拓宽了视野，也提升了自身的综合素质。"
        f"希望今后能有更多这样的学习机会，不断充实和完善自我。",
        f"在「{name}」活动中，我深刻体会到了{module}的重要性。"
        f"通过实践与交流，我加深了对相关知识的理解，也认识到了自身需要改进的地方。"
        f"感谢{organizer}的精心组织，活动安排合理、内容充实，让我受益匪浅。"
        f"今后我将继续努力，在实践中不断成长。",
        f"本次参加「{name}」活动，是一次非常有意义的体验。"
        f"活动内容丰富，形式多样，让我在{module}方面有了新的感悟和收获。"
        f"通过与同学们的交流互动，我不仅学到了新知识，也增强了团队协作能力。"
        f"感谢{organizer}提供这样的平台，期待未来参与更多类似活动。",
    ]
    return random.choice(templates)


# ════════════════════ 监控循环 ════════════════════


class ActivityMonitor:
    """活动监控器：自动签到、签退、提交总结。"""

    def __init__(self, sess, user: dict, cfg: dict):
        self.sess = sess
        self.user = user
        self.cfg = cfg
        self.state = _load_state()
        self._running = False

    def run(self) -> None:
        """启动监控循环。"""
        self._running = True
        interval = self.cfg.get("poll_interval_seconds", 60)

        print(f"监控已启动 (间隔 {interval}s)，按 Ctrl+C 停止")
        while self._running:
            try:
                self._tick()
            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error("监控tick异常: %s", e)

            time.sleep(interval)

        print("监控已停止")

    def stop(self) -> None:
        self._running = False

    def _tick(self) -> None:
        """单次检查循环。"""
        now = datetime.now()
        print(f"\n[{now.strftime('%H:%M:%S')}] 检查活动状态...")

        try:
            actions = fetch_my_activities_needing_action(self.sess)
        except secondclass_tool.SecondClassAuthError:
            print("认证过期，尝试重新登录...")
            try:
                self.sess = secondclass_tool.obtain_secondclass_session_from_user(
                    self.user
                )
                actions = fetch_my_activities_needing_action(self.sess)
            except Exception as e:
                print(f"重新认证失败: {e}")
                return
        except Exception as e:
            print(f"获取活动状态失败: {e}")
            return

        # 签到 — 目前需要扫码，只能提示
        sign_in_count = len(actions["need_sign_in"])
        if sign_in_count > 0:
            print(f"  未签到活动: {sign_in_count} 个（需扫码签到，暂无法自动处理）")
            for act in actions["need_sign_in"][:5]:
                name = act.get("activity_name", "?")
                gps = act.get("gps", "0")
                gps_note = " [GPS可签到]" if gps == "1" else ""
                print(f"    - {name}{gps_note}")

        # GPS签到
        for act in actions["need_sign_in"]:
            if act.get("gps") == "1" and not self.cfg.get("skip_location_sign_in"):
                aid = act.get("activity_id", "")
                name = act.get("activity_name", "?")
                print(f"  GPS签到: {name} (ID: {aid})")
                lat = self.cfg.get("campus_latitude", "")
                lon = self.cfg.get("campus_longitude", "")
                result = submit_activity_sign_in(
                    self.sess, aid, latitude=lat, longitude=lon
                )
                print(
                    f"    {'OK' if result['success'] else 'FAIL'} {result['message']}"
                )
                time.sleep(random.uniform(1, 3))

        # 签退 — 类似签到
        sign_out_count = len(actions["need_sign_out"])
        if sign_out_count > 0:
            print(f"  需签退活动: {sign_out_count} 个")

        # 提交总结
        for act in actions["need_summary"]:
            aid = act.get("activity_id", "")
            name = act.get("activity_name", "?")
            summary = generate_activity_summary(act)
            print(f"  提交总结: {name} (ID: {aid})")

            result = submit_activity_summary(self.sess, aid, summary)
            print(f"    {'OK' if result['success'] else 'FAIL'} {result['message']}")
            time.sleep(random.uniform(1, 3))

        # 更新状态
        self.state["last_full_scan"] = now.isoformat(timespec="seconds")
        _save_state(self.state)

        total_actions = sign_in_count + sign_out_count + len(actions["need_summary"])
        if total_actions == 0:
            print("  所有可自动处理的活动均已完成")


def cmd_monitor(args) -> None:
    """启动监控循环。"""
    sess, user = _get_session()
    cfg = _load_config()
    monitor = ActivityMonitor(sess, user, cfg)
    try:
        monitor.run()
    except KeyboardInterrupt:
        monitor.stop()


def cmd_run(args) -> None:
    """全自动模式：先报名，再监控。"""
    sess, user = _get_session()
    cfg = _load_config()

    # 先批量报名
    print("=" * 50)
    print("  第一步：批量报名")
    print("=" * 50)
    signup_args = argparse.Namespace(dry_run=False, max=args.max_signup)
    cmd_signup(signup_args)

    # 然后启动监控
    print("\n" + "=" * 50)
    print("  第二步：启动监控")
    print("=" * 50)

    # 重新获取session（报名可能更新了cookie）
    try:
        sess, user = _get_session()
    except SystemExit:
        return

    monitor = ActivityMonitor(sess, user, cfg)
    try:
        monitor.run()
    except KeyboardInterrupt:
        monitor.stop()


# ════════════════════ 扫描新活动 ════════════════════


def cmd_scan(args) -> None:
    """扫描发现新活动（包括报名未开始的活动）。"""
    sess, user = _get_session()
    student_id = user.get("student_id", "")
    qq = user.get("qq", 0)
    scan_range = args.range

    print(f"正在扫描新活动 (范围: {scan_range} 个)...")
    result = secondclass_tool.discover_new_activities(
        sess, student_id=student_id, qq=qq,
        scan_range=scan_range, delay=0.3,
    )
    print(f"\n扫描完成:")
    print(f"  发现新活动: {result['discovered']} 个")
    print(f"  报名未开始: {result['upcoming']} 个")


# ════════════════════ 门票 → SSID ════════════════════


def cmd_ticket(args) -> None:
    """门票/令牌 → SSID cookie 转换。

    支持：
      --ticket     portal_ticket 值
      --token      access_token 值
      --json       凭证 JSON 文件路径
      --student-id 学号（可选，用于缓存）
    """
    from secondclass.secondclass_tool import convert_to_ssid

    access_token = args.token or os.environ.get("SSO_ACCESS_TOKEN", "")
    portal_ticket = args.ticket or os.environ.get("SSO_PORTAL_TICKET", "")
    student_id = args.student_id or os.environ.get("SSO_STUDENT_ID", "")

    # 从 JSON 文件读取
    if args.json:
        import json as _json
        from pathlib import Path

        path = Path(args.json)
        if not path.exists():
            print(f"错误: 文件不存在: {path}", file=sys.stderr)
            sys.exit(1)
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"错误: JSON 解析失败: {e}", file=sys.stderr)
            sys.exit(1)

        if "accounts" in data:
            acc = data["accounts"][0] if data["accounts"] else {}
            access_token = access_token or acc.get("access_token", "")
            portal_ticket = portal_ticket or acc.get("portal_ticket", "")
            student_id = student_id or acc.get("student_id", "")
        else:
            access_token = access_token or data.get("access_token", "")
            portal_ticket = portal_ticket or data.get("portal_ticket", "")
            student_id = student_id or data.get("student_id", "")

    if not access_token and not portal_ticket:
        print("错误: 缺少凭证", file=sys.stderr)
        print("用法: python secondclass_auto_score.py ticket --ticket PORTAL_TICKET", file=sys.stderr)
        print("      python secondclass_auto_score.py ticket --token ACCESS_TOKEN", file=sys.stderr)
        print("      python secondclass_auto_score.py ticket --json accounts.json", file=sys.stderr)
        sys.exit(1)

    print("-" * 50)
    print("  二课 SSID 转换")
    print("-" * 50)
    if student_id:
        print(f"  学号: {student_id}")
    if access_token:
        print(f"  access_token: {access_token[:12]}…")
    if portal_ticket:
        print(f"  portal_ticket: {portal_ticket[:12]}…")

    print("\n  正在桥接获取 SSID…")
    ssid = convert_to_ssid(
        access_token=access_token,
        portal_ticket=portal_ticket,
        student_id=student_id,
    )

    if ssid:
        print(f"\n  ✅ 成功获取 SSID:")
        print(f"  ┌────────────────────────────────────────────────┐")
        print(f"  │ {ssid:<46s} │")
        print(f"  └────────────────────────────────────────────────┘")
        print(f"\n  环境变量:  set AUTO_SCORE_SSID={ssid}")
        print(f"  命令行:   --ssid {ssid}")
    else:
        print(f"\n  ❌ 获取 SSID 失败")
        print(f"  请检查凭证是否有效（portal_ticket 可能已过期）")
        print(f"  或使用 access_token 自动刷新：--token ACCESS_TOKEN --ticket PORTAL_TICKET")
        sys.exit(1)


# ════════════════════ CLI入口 ════════════════════


def main():
    # Windows 终端 UTF-8 输出
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="二课自动积分工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
        "  python secondclass_auto_score.py status                     # 查看积分\n"
        "  python secondclass_auto_score.py signup --dry-run           # 预览可报名活动\n"
        "  python secondclass_auto_score.py signup --max 5             # 报名5个活动\n"
        "  python secondclass_auto_score.py probe                      # 探测签到端点\n"
        "  python secondclass_auto_score.py monitor                    # 监控签到/签退\n"
        "  python secondclass_auto_score.py run                        # 全自动模式\n"
        "  python secondclass_auto_score.py ticket --ticket PORTAL_TICKET  # 门票→SSID\n"
        "  python secondclass_auto_score.py ticket --token ACCESS_TOKEN    # 令牌→SSID\n"
        "  python secondclass_auto_score.py ticket --json accounts.json   # 文件→SSID\n"
        "\n环境变量:\n"
        "  AUTO_SCORE_SSID=xxx   直接使用SSID cookie（无需本地凭证）\n"
        "  SSO_ACCESS_TOKEN=xxx  access_token（ticket子命令使用）\n"
        "  SSO_PORTAL_TICKET=xxx portal_ticket（ticket子命令使用）\n",
    )
    parser.add_argument("--ssid", help="直接使用SSID cookie（无需本地凭证）")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="查看积分仪表盘")

    signup_p = sub.add_parser("signup", help="批量报名活动")
    signup_p.add_argument("--dry-run", action="store_true", help="仅显示，不实际报名")
    signup_p.add_argument("--max", type=int, default=10, help="最多报名数量 (默认10)")

    sub.add_parser("probe", help="探测签到/签退/总结端点")

    summary_p = sub.add_parser("summary", help="批量提交活动总结")
    summary_p.add_argument("--dry-run", action="store_true", help="仅显示，不实际提交")
    summary_p.add_argument("--max", type=int, default=10, help="最多提交数量")

    sub.add_parser("monitor", help="启动签到/签退/总结监控")

    scan_p = sub.add_parser("scan", help="扫描发现新活动（含报名未开始）")
    scan_p.add_argument("--range", type=int, default=50, help="扫描ID数量")

    run_p = sub.add_parser("run", help="全自动模式(报名+监控)")
    run_p.add_argument("--max-signup", type=int, default=10, help="最多报名数量")

    # ── ticket ──
    ticket_p = sub.add_parser("ticket", help="门票/令牌 → SSID cookie 转换")
    ticket_p.add_argument("--ticket", type=str, default=None,
                          help="portal_ticket 值，从SSO登录获得")
    ticket_p.add_argument("--token", type=str, default=None,
                          help="access_token 值，从SSO登录获得")
    ticket_p.add_argument("--json", type=str, default=None,
                          help="读取凭证 JSON 文件（含 access_token/portal_ticket）")
    ticket_p.add_argument("--student-id", type=str, default=None,
                          help="学号（可选，用于SSID缓存）")

    args = parser.parse_args()

    # 处理 --ssid
    if args.ssid:
        _get_session._ssid_override = args.ssid
        os.environ["AUTO_SCORE_SSID"] = args.ssid

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)-5s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    commands = {
        "status": cmd_status,
        "signup": cmd_signup,
        "probe": cmd_probe,
        "summary": cmd_summary,
        "scan": cmd_scan,
        "monitor": cmd_monitor,
        "run": cmd_run,
        "ticket": cmd_ticket,
    }

    cmd = commands.get(args.command)
    if not cmd:
        parser.print_help()
        sys.exit(1)

    cmd(args)


if __name__ == "__main__":
    main()
