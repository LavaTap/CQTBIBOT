"""二课自动积分工具 — 交互式菜单启动器。

双击 BAT 或直接运行本文件启动。所有中文由 Python 处理，无 CMD 编码问题。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)

# Windows 终端 UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    os.system("chcp 65001 >nul 2>&1")


def _run(cmd: str) -> None:
    try:
        subprocess.run(cmd, shell=True, check=False)
    except KeyboardInterrupt:
        print("\n已中断")


def _input(prompt: str, default: str = "") -> str:
    try:
        v = input(prompt).strip()
        return v if v else default
    except (EOFError, KeyboardInterrupt):
        print()
        return default


def main():
    while True:
        print()
        print("=" * 46)
        print("       二课自动积分工具 — 启动菜单")
        print("=" * 46)
        print()
        print("  1. 查看积分仪表盘    (status)")
        print("  2. 预览可报名活动    (signup --dry-run)")
        print("  3. 批量报名活动      (signup)")
        print("  4. 批量提交总结      (summary)")
        print("  5. 探测签到端点      (probe)")
        print("  6. 启动监控          (monitor)")
        print("  7. 全自动模式        (run)")
        print("  8. 启动图形界面      (GUI)")
        print("  0. 退出")
        print()

        ssid = _input("请输入SSID (直接回车使用已保存凭证): ")
        env_prefix = f"set AUTO_SCORE_SSID={ssid} && " if ssid else ""

        choice = _input("请选择操作 [0-8]: ")

        if choice == "1":
            _run(f"{env_prefix}python secondclass_auto_score.py status")
        elif choice == "2":
            max_n = _input("最多显示几个 (默认10): ", "10")
            _run(
                f"{env_prefix}python secondclass_auto_score.py signup --dry-run --max {max_n}"
            )
        elif choice == "3":
            max_n = _input("最多报名几个 (默认5): ", "5")
            _run(f"{env_prefix}python secondclass_auto_score.py signup --max {max_n}")
        elif choice == "4":
            max_n = _input("最多提交几个 (默认10): ", "10")
            _run(f"{env_prefix}python secondclass_auto_score.py summary --max {max_n}")
        elif choice == "5":
            _run(f"{env_prefix}python secondclass_auto_score.py probe")
        elif choice == "6":
            _run(f"{env_prefix}python secondclass_auto_score.py monitor")
        elif choice == "7":
            max_n = _input("最多报名几个 (默认5): ", "5")
            _run(
                f"{env_prefix}python secondclass_auto_score.py run --max-signup {max_n}"
            )
        elif choice == "8":
            _run(f"{env_prefix}python secondclass_auto_score_gui.py")
        elif choice == "0":
            break

    print("再见！")


if __name__ == "__main__":
    main()
