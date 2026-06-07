"""二课总表自动调度器。

SSID 生命周期：
  access_token (OAuth2 令牌, 有有效期)
      ↓ GET /v1/auth2orize (自动授权, 需要活跃 SSO 会话)
  portal_ticket (门户票据, 单次会话)
      ↓ POST /cqdddt/dtLog!log.action (门户登录)
      ↓ GET /Admin/Index/cqtbiSSO?PORTAL_TICKET=xxx (二课桥接)
  SSID cookie (二课会话, 用于后续所有二课 API 调用)

调度策略（由用户指定）：
  - 周二 06:00-22:00, 周三 06:00-17:00 → 每15分钟拉一次
  - 周二到周三其余时间 → 每30分钟拉一次
  - 其它日子 → 每3小时拉一次
  - 所有用户 SSID 过期 → 停止维护

用法：
  python secondclass_scheduler.py          # 启动调度
  python secondclass_scheduler.py --once   # 只拉一次
  python secondclass_scheduler.py --daemon # 后台运行
"""

from __future__ import annotations

import logging
import sys
import time
import threading
from datetime import datetime
from pathlib import Path

from secondclass import secondclass_tool

log = logging.getLogger("scheduler")

# ── 日志 ──
def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)-5s | %(message)s",
        datefmt="%m-%d %H:%M:%S",
        stream=sys.stderr,
    )


# ── 核心调度 ──

class SecondClassScheduler:
    """二课总表自动调度器。"""

    def __init__(self):
        self._running = False
        self._thread: threading.Thread | None = None
        self._stats = {"runs": 0, "successful": 0, "failed": 0}
        self._all_expired = False
        self._consecutive_failures = 0
        self._users_override: list[dict] | None = None  # GUI 设置的用户子集

    def set_users(self, users: list[dict] | None):
        """设置要拉取的用户列表（None = 拉取所有用户）。"""
        self._users_override = users

    def run_once(self, users_override: list[dict] | None = None) -> dict:
        """执行一次二课总表拉取，返回统计信息。

        参数：
            users_override: 可选，指定要拉取的用户列表（不传则拉取所有用户）
        """
        log.info("=" * 50)
        log.info("二课总表拉取开始")
        stats: dict = {"total_activities": 0, "users": 0,
                       "failed_users": 0, "expired_users": 0}

        users = (users_override if users_override is not None
                 else self._users_override
                 if self._users_override is not None
                 else secondclass_tool.get_all_user_credentials())
        if not users:
            log.warning("没有找到任何用户凭证，跳过拉取")
            self._all_expired = True
            return stats

        active_count = 0
        expired_count = 0
        failed_count = 0

        for user in users:
            qq = user.get("qq", 0)
            student_id = user.get("student_id", "")
            realname = user.get("realname", "")
            try:
                # 尝试获取 SSID 会话
                sess = secondclass_tool.obtain_secondclass_session_from_user(user)

                # 拉取并存储总表数据（以 student_id 为主键）
                result = secondclass_tool.fetch_and_store_master_data(
                    sess, student_id=student_id, qq=qq,
                    fetch_details=True, max_detail_activities=10,
                    include_reparse=True, scan_new=True, scan_range=50)
                stats["total_activities"] += result.get("total_count", 0)
                active_count += 1
                log.info("用户 %s(%s) 拉取成功 ✓  活动=%s 详情=%s",
                         student_id, realname or "?",
                         result.get("total_count", 0),
                         result.get("detail_fetched", 0))

                # 拉取未结束活动（报名中+活动中+未开始）写入独立表
                try:
                    unfinished_result = secondclass_tool.fetch_and_store_my_unfinished_activities(
                        sess, student_id=student_id, qq=qq,
                    )
                    log.info("用户 %s(%s) 未结束活动: %s",
                             student_id, realname or "?",
                             unfinished_result.get("total", 0))
                except Exception as e:
                    log.warning("用户 %s(%s) 未结束活动拉取失败: %s",
                                student_id, realname or "?", e)

            except secondclass_tool.SecondClassAuthError as e:
                log.warning("用户 %s(%s) 认证失败: %s", student_id, realname or "?", e)
                expired_count += 1
            except Exception as e:
                log.warning("用户 %s(%s) 拉取异常: %s", student_id, realname or "?", e)
                failed_count += 1

        stats["users"] = active_count
        stats["failed_users"] = failed_count
        stats["expired_users"] = expired_count

        # 所有用户过期 → 累积失败计数，但不停机（等待重试）
        if active_count == 0 and expired_count > 0:
            self._consecutive_failures += 1
            log.warning("所有用户 SSID 过期(连续%d次)，将在下次调度窗口重试",
                        self._consecutive_failures)
        else:
            self._consecutive_failures = 0
            self._all_expired = False

        self._stats["runs"] += 1
        if active_count > 0:
            self._stats["successful"] += 1
        else:
            self._stats["failed"] += 1

        log.info("二课总表拉取完成: 活跃=%d 过期=%d 失败=%d 活动=%d",
                 active_count, expired_count, failed_count,
                 stats["total_activities"])
        log.info("=" * 50)
        return stats

    def start(self):
        """启动调度循环。"""
        if self._running:
            log.warning("调度器已在运行")
            return

        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("二课总表调度器已启动")

    def stop(self):
        """停止调度。"""
        self._running = False
        log.info("二课总表调度器已停止")

    def _loop(self):
        """调度主循环。"""
        if self._running:
            self.run_once()

        while self._running:
            # 计算下次拉取间隔（连续失败则用非峰期间隔，不再缩短）
            interval = secondclass_tool.get_schedule_interval()
            if self._consecutive_failures > 0:
                # 连续失败时使用30分钟间隔重试
                interval = max(interval, 30 * 60)
            interval_min = interval // 60
            log.info("下次拉取在 %d 分钟后（%s）",
                     interval_min, _describe_schedule())

            waited = 0
            while waited < interval and self._running:
                time.sleep(min(60, interval - waited))
                waited += 60

            if self._running:
                try:
                    self.run_once()
                except Exception as e:
                    log.error("调度循环异常: %s", e)
                    self._stats["failed"] += 1

        log.info("二课总表调度器已退出")

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_all_expired(self) -> bool:
        return self._all_expired


def _describe_schedule() -> str:
    """返回当前调度策略的文本描述。"""
    now = datetime.now()
    weekday = now.weekday()
    hour = now.hour
    minute = now.minute
    time_minutes = hour * 60 + minute

    day_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    if weekday == 1:
        if 360 <= time_minutes < 1320:
            return f"{day_names[weekday]}高峰期(15分钟间隔)"
        return f"{day_names[weekday]}非峰期(30分钟间隔)"
    if weekday == 2:
        if 360 <= time_minutes < 1020:
            return f"{day_names[weekday]}高峰期(15分钟间隔)"
        return f"{day_names[weekday]}非峰期(30分钟间隔)"
    return f"{day_names[weekday]}低峰期(3小时间隔)"


# ════════════════════ CLI ════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="二课总表自动调度器")
    parser.add_argument("--once", action="store_true",
                        help="只执行一次拉取，不启动调度循环")
    parser.add_argument("--daemon", action="store_true",
                        help="后台模式运行（不输出调度状态到 stderr）")
    args = parser.parse_args()

    _setup_logging()
    if args.daemon:
        logging.getLogger().setLevel(logging.WARNING)

    scheduler = SecondClassScheduler()

    if args.once:
        stats = scheduler.run_once()
        log.info("单次拉取完成: %s", stats)
        return

    # 启动调度循环
    scheduler.start()
    log.info("二课总表调度器运行中（Ctrl+C 停止）")

    try:
        while scheduler.is_running:
            time.sleep(10)
    except KeyboardInterrupt:
        log.info("收到中断信号")
    finally:
        scheduler.stop()
        log.info("二课总表调度器已退出")


if __name__ == "__main__":
    main()
