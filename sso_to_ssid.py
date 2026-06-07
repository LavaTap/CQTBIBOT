"""SSO 凭证 → 二课 SSID cookie 转换工具。

读取 access_token + portal_ticket，通过 SSO 桥接获取二课系统的 SSID cookie。

用法:
  # 从 JSON 文件读取
  python sso_to_ssid.py accounts.json

  # 从 stdin 读取 JSON
  echo '{"access_token":"...","portal_ticket":"..."}' | python sso_to_ssid.py

  # 从环境变量读取
  set SSO_ACCESS_TOKEN=xxx & set SSO_PORTAL_TICKET=xxx
  python sso_to_ssid.py

  # 提取指定 QQ 的凭证并转换
  python sso_to_ssid.py accounts.json --qq 3200418862

输出：
  SSID=e8f3a1b2c4d5...    (成功)
  错误: ...                  (失败)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

# 确保项目根目录在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from secondclass.secondclass_tool import convert_to_ssid, SecondClassAuthError

log = logging.getLogger("sso_to_ssid")


def load_credentials(source: str | None, qq: str | None = None) -> dict:
    """从文件、stdin 或环境变量加载凭证。

    Args:
        source: JSON 文件路径，或 None（从 stdin/环境变量）。
        qq: 指定 QQ 号提取。

    Returns:
        {"access_token": str, "portal_ticket": str, "student_id": str}
    """

    def _pick_account(accounts: list[dict]) -> dict | None:
        if qq:
            for acc in accounts:
                if str(acc.get("qq", "")) == str(qq):
                    return acc
            log.error("未找到 QQ=%s 的账号", qq)
            return None
        # 取第一个非空的
        for acc in accounts:
            if acc.get("access_token") or acc.get("portal_ticket"):
                return acc
        return None

    # 1. 从文件读取
    if source:
        path = Path(source)
        if not path.exists():
            log.error("文件不存在: %s", path)
            sys.exit(1)
        raw = json.loads(path.read_text(encoding="utf-8"))
        if "accounts" in raw:
            acc = _pick_account(raw["accounts"])
            if not acc:
                log.error("accounts.json 中没有可用账号")
                sys.exit(1)
            return acc
        return dict(raw)

    # 2. 从 stdin 读取
    if not sys.stdin.isatty():
        try:
            raw = json.loads(sys.stdin.read())
            if "accounts" in raw:
                acc = _pick_account(raw["accounts"])
                return acc if acc else raw.get("__default__", {})
            return dict(raw)
        except json.JSONDecodeError as e:
            log.error("stdin JSON 解析失败: %s", e)
        except Exception as e:
            log.error("读取 stdin 失败: %s", e)

    # 3. 从环境变量读取
    access_token = os.environ.get("SSO_ACCESS_TOKEN") or ""
    portal_ticket = os.environ.get("SSO_PORTAL_TICKET") or ""
    student_id = os.environ.get("SSO_STUDENT_ID") or ""
    if access_token or portal_ticket:
        return {
            "access_token": access_token,
            "portal_ticket": portal_ticket,
            "student_id": student_id,
        }

    log.error(
        "没有可用的凭证。请提供 JSON 文件、通过 stdin 传入 JSON，"
        "或设置环境变量 SSO_ACCESS_TOKEN / SSO_PORTAL_TICKET"
    )
    sys.exit(1)


def main() -> None:
    # Windows 终端 UTF-8 输出
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    import argparse

    parser = argparse.ArgumentParser(
        description="SSO 凭证 → 二课 SSID cookie 转换工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("file", nargs="?", default=None,
                        help="accounts.json 文件路径（可选）")
    parser.add_argument("--qq", type=str, default=None,
                        help="指定 QQ 号（从 accounts.json 中提取）")
    parser.add_argument("--json", type=str, default=None,
                        help="直接传入 JSON 字符串")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="输出详细日志")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)-5s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    # 加载凭证
    if args.json:
        # 直接从 --json 参数解析
        raw = json.loads(args.json)
        if "accounts" in raw:
            accounts = raw["accounts"]
            target = None
            if args.qq:
                for a in accounts:
                    if str(a.get("qq", "")) == str(args.qq):
                        target = a
                        break
                if not target:
                    log.error("未找到 QQ=%s 的账号", args.qq)
                    sys.exit(1)
            else:
                target = accounts[0] if accounts else {}
            creds = target
        else:
            creds = raw
    else:
        creds = load_credentials(args.file, qq=args.qq)

    access_token = creds.get("access_token") or creds.get("cred_access_token", "")
    portal_ticket = creds.get("portal_ticket") or creds.get("cred_portal_ticket", "")
    student_id = (
        creds.get("student_id")
        or creds.get("stu_id", "")
    )

    if not access_token and not portal_ticket:
        log.error("凭证中缺少 access_token 和 portal_ticket")
        print(f"错误: 凭证数据: {json.dumps(creds, ensure_ascii=False)[:200]}", file=sys.stderr)
        sys.exit(1)

    # 执行转换
    print(f"student_id={student_id}", file=sys.stderr)
    if access_token:
        print(f"access_token={access_token[:8]}…", file=sys.stderr)
    if portal_ticket:
        print(f"portal_ticket={portal_ticket[:8]}…", file=sys.stderr)

    ssid = convert_to_ssid(
        access_token=access_token,
        portal_ticket=portal_ticket,
        student_id=student_id,
    )

    if ssid:
        # 成功：输出 SSID（可被脚本捕获）
        print(f"SSID={ssid}", flush=True)
        # 同时输出方便复制到剪贴板的纯值
        print(f"\nSSID 值:\n{ssid}", file=sys.stderr)
    else:
        print("错误: 获取 SSID 失败，请检查凭证是否有效", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
