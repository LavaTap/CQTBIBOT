"""二课总表调度器 GUI 工具。

提供图形界面配置和运行二课总表自动调度，支持：
  - 查看已登录用户
  - 手动单次拉取
  - 启动/停止自动调度
  - 实时查看日志和状态
  - 调度策略指示器
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from tkinter import ttk
import tkinter as tk

from secondclass import secondclass_tool
from secondclass import secondclass_scheduler

log = logging.getLogger("sc_gui")


class _LogHandler(logging.Handler):
    """捕获日志到队列，供 GUI 显示。"""

    def __init__(self, log_queue: queue.Queue):
        super().__init__(level=logging.INFO)
        self.log_queue = log_queue

    def emit(self, record):
        self.log_queue.put(self.format(record))


class App(tk.Tk):
    WIDTH = 800
    HEIGHT = 680

    def __init__(self):
        super().__init__()
        self.title("二课总表调度器")

        # 窗口大小与位置
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = (sw - self.WIDTH) // 2
        y = (sh - self.HEIGHT) // 2
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── 状态变量 ──
        self.scheduler = secondclass_scheduler.SecondClassScheduler()
        self.status_var = tk.StringVar(value="就绪")
        self.schedule_desc_var = tk.StringVar(value="等待启动")
        self.users_var = tk.StringVar(value="加载中…")
        self._busy = False
        self._users_data: list[dict] = []  # 当前用户列表数据

        # ── 日志队列 ──
        self._log_queue: queue.Queue = queue.Queue()
        self._setup_logging()
        self._scheduler_running = False
        self._poll_timer_id: str | None = None

        # ── 构建 UI ──
        self._build_ui()

        # ── 启动时加载用户信息 ──
        self.after(100, self._refresh_users)

    # ── 日志设置 ──
    def _setup_logging(self):
        fmt = logging.Formatter("%(asctime)s %(levelname)-5s | %(message)s",
                                datefmt="%H:%M:%S")
        handler = _LogHandler(self._log_queue)
        handler.setFormatter(fmt)
        logging.getLogger("secondclass").addHandler(handler)
        logging.getLogger("scheduler").addHandler(handler)

    # ── UI 构建 ──
    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}

        # ════════ 顶部：调度策略指示器 ════════
        strategy_frame = ttk.LabelFrame(self, text="调度策略", padding=6)
        strategy_frame.pack(fill="x", **pad)

        self.strategy_text = tk.Text(
            strategy_frame, height=4, wrap="word",
            font=("Consolas", 10), fg="#333",
            relief="flat", bg="#f5f5f5",
        )
        self.strategy_text.pack(fill="x", padx=4, pady=2)
        self.strategy_text.insert("1.0", self._strategy_doc())
        self.strategy_text.configure(state="disabled")

        # ════════ 用户列表 ════════
        user_frame = ttk.LabelFrame(self, text="已登录用户", padding=6)
        user_frame.pack(fill="x", **pad)

        self.user_listbox = tk.Listbox(
            user_frame, height=4, font=("Consolas", 10),
            selectmode=tk.MULTIPLE, exportselection=False,
        )
        self.user_listbox.pack(fill="x", padx=4, pady=2)

        # 用户选择提示
        hint_row = ttk.Frame(user_frame)
        hint_row.pack(fill="x", padx=4, pady=(0, 2))
        ttk.Label(
            hint_row, text="💡 点击选中要拉取的用户（按住 Ctrl 多选，不选则拉取全部）",
            foreground="#888", font=("", 8),
        ).pack(side="left")
        self.select_all_btn = ttk.Button(
            hint_row, text="全选", width=5, command=self._select_all_users,
        )
        self.select_all_btn.pack(side="right", padx=2)
        self.deselect_all_btn = ttk.Button(
            hint_row, text="取消全选", width=8, command=self._deselect_all_users,
        )
        self.deselect_all_btn.pack(side="right", padx=2)

        # ════════ SSID 转换 ════════
        ssid_frame = ttk.LabelFrame(self, text="SSID 转换（门票/令牌 → SSID cookie）", padding=6)
        ssid_frame.pack(fill="x", **pad)

        # 输入行
        input_row = ttk.Frame(ssid_frame)
        input_row.pack(fill="x", pady=2)

        ttk.Label(input_row, text="access_token:", width=12).pack(side="left")
        self.token_entry = ttk.Entry(input_row, width=30)
        self.token_entry.pack(side="left", padx=4)
        self.token_entry.insert(0, "")
        ttk.Label(input_row, text="portal_ticket:", width=12).pack(side="left", padx=(10, 0))
        self.ticket_entry = ttk.Entry(input_row, width=30)
        self.ticket_entry.pack(side="left", padx=4)

        # 第二行：学号 + 按钮
        row2 = ttk.Frame(ssid_frame)
        row2.pack(fill="x", pady=2)

        ttk.Label(row2, text="学号（可选）:", width=12).pack(side="left")
        self.sid_entry = ttk.Entry(row2, width=20)
        self.sid_entry.pack(side="left", padx=4)

        self.convert_btn = ttk.Button(
            row2, text="开始转换 → SSID", command=self._convert_ssid,
            width=18,
        )
        self.convert_btn.pack(side="left", padx=10)

        # 结果行
        result_row = ttk.Frame(ssid_frame)
        result_row.pack(fill="x", pady=2)

        self.ssid_result_var = tk.StringVar(value="")
        self.ssid_result_entry = ttk.Entry(
            result_row, textvariable=self.ssid_result_var,
            font=("Consolas", 10), state="readonly", width=60,
        )
        self.ssid_result_entry.pack(side="left", padx=4, fill="x", expand=True)

        self.copy_btn = ttk.Button(
            result_row, text="复制", command=self._copy_ssid,
            width=6, state="disabled",
        )
        self.copy_btn.pack(side="left", padx=2)

        # ════════ 控制区 ════════
        ctrl_frame = ttk.LabelFrame(self, text="调度控制", padding=6)
        ctrl_frame.pack(fill="x", **pad)

        btn_row = ttk.Frame(ctrl_frame)
        btn_row.pack(fill="x", pady=2)

        self.run_once_btn = ttk.Button(
            btn_row, text="运行一次", command=self._run_once,
            width=12,
        )
        self.run_once_btn.pack(side="left", padx=4)

        self.start_btn = ttk.Button(
            btn_row, text="启动调度", command=self._start_scheduler,
            width=12,
        )
        self.start_btn.pack(side="left", padx=4)

        self.stop_btn = ttk.Button(
            btn_row, text="停止调度", command=self._stop_scheduler,
            width=12, state="disabled",
        )
        self.stop_btn.pack(side="left", padx=4)

        # ════════ 状态面板 ════════
        status_frame = ttk.LabelFrame(self, text="运行状态", padding=6)
        status_frame.pack(fill="x", **pad)

        # 状态指示行
        status_row = ttk.Frame(status_frame)
        status_row.pack(fill="x", pady=2)

        ttk.Label(status_row, text="状态:").pack(side="left")
        self.status_indicator = ttk.Label(
            status_row, textvariable=self.status_var,
            foreground="#0a0", font=("", 10, "bold"),
        )
        self.status_indicator.pack(side="left", padx=8)

        ttk.Label(status_row, text="策略:").pack(side="left", padx=(20, 0))
        self.schedule_label = ttk.Label(
            status_row, textvariable=self.schedule_desc_var,
            foreground="#555", font=("", 9),
        )
        self.schedule_label.pack(side="left", padx=8)

        # 统计数据行
        stat_row = ttk.Frame(status_frame)
        stat_row.pack(fill="x", pady=2)

        self.stat_var = tk.StringVar(value="运行: 0 次  |  成功: 0  |  失败: 0  |  活动总数: 0")
        ttk.Label(stat_row, textvariable=self.stat_var,
                  font=("Consolas", 9), foreground="#666").pack(side="left")

        self.expired_var = tk.StringVar(value="")
        self.expired_label = ttk.Label(
            stat_row, textvariable=self.expired_var,
            foreground="#c00", font=("", 9, "bold"),
        )
        self.expired_label.pack(side="right", padx=8)

        # ════════ 日志区 ════════
        log_frame = ttk.LabelFrame(self, text="运行日志", padding=6)
        log_frame.pack(fill="both", expand=True, **pad)

        log_row = ttk.Frame(log_frame)
        log_row.pack(fill="both", expand=True)

        self.log_text = tk.Text(
            log_row, height=12, wrap="word",
            font=("Consolas", 9), relief="sunken", bd=1,
        )
        self.log_text.pack(side="left", fill="both", expand=True)

        log_scroll = ttk.Scrollbar(log_row, orient="vertical",
                                   command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        # 底部
        bot_row = ttk.Frame(self)
        bot_row.pack(fill="x", **{"padx": 10, "pady": (0, 8)})

        ttk.Button(bot_row, text="清空日志",
                   command=self._clear_log, width=10).pack(side="left")

        ttk.Label(bot_row, text="二课总表调度器 v1.0",
                  foreground="#aaa", font=("", 8)).pack(side="right")

        # ── 启动日志轮询 ──
        self._poll_log()

    # ── 调度策略文档 ──
    @staticmethod
    def _strategy_doc() -> str:
        return (
            "▸ 高峰期(15分钟)  — 周二 06:00~22:00, 周三 06:00~17:00\n"
            "▸ 非峰期(30分钟)  — 周二至周三其余时间\n"
            "▸ 低峰期(3小时)   — 周一/周四~周日\n"
            "▸ 自动停止         — 所有用户 SSID 过期后停止维护"
        )

    # ── 刷新用户列表 ──
    def _refresh_users(self):
        users = secondclass_tool.get_all_user_credentials()
        self._users_data = users  # 保存数据供选中映射
        self.user_listbox.delete(0, "end")
        if not users:
            self.user_listbox.insert("end", "（无已登录用户，请先使用 #扫码登录）")
            self.users_var.set("无用户")
        else:
            for u in users:
                sid = u.get("student_id", "?")
                name = u.get("realname", "")
                has_pt = "✓" if u.get("portal_ticket") else "✗"
                has_at = "✓" if u.get("access_token") else "✗"
                expire = u.get("expires_at", 0)
                status = "有效" if (expire and expire > time.time()) else "已过期"
                line = f"{sid} {name:8s}  票据:{has_pt} Token:{has_at}  {status}"
                self.user_listbox.insert("end", line)
            self.users_var.set(f"{len(users)} 个用户")
            # 默认全选
            self.user_listbox.selection_set(0, "end")
        self.after(30000, self._refresh_users)  # 每 30 秒刷新

    # ── 全选 / 取消全选 ──
    def _select_all_users(self):
        self.user_listbox.selection_set(0, "end")

    def _deselect_all_users(self):
        self.user_listbox.selection_clear(0, "end")

    def _get_selected_users(self) -> list[dict] | None:
        """获取 Listbox 中选中的用户列表，无选中则返回 None（拉取全部）。"""
        sel = self.user_listbox.curselection()
        if not sel:
            return None  # 未选择 → 拉取全部
        return [self._users_data[i] for i in sel if i < len(self._users_data)]

    # ── 运行一次 ──
    def _run_once(self):
        if self._busy:
            return
        selected = self._get_selected_users()
        if selected is not None:
            desc = f"选中 {len(selected)} 个用户"
        else:
            desc = "全部用户"
        self._set_busy(True, f"正在拉取 ({desc})…")
        self.log("━" * 50)
        self.log(f"手动触发一次拉取 ({desc})")
        # 传递给调度器
        self.scheduler.set_users(selected)
        threading.Thread(target=self._bg_run_once, daemon=True).start()

    def _bg_run_once(self):
        try:
            stats = self.scheduler.run_once()
            self.after(0, lambda: self._on_run_done(stats))
        except Exception as e:
            self.after(0, lambda: self._on_run_error(str(e)))

    def _on_run_done(self, stats: dict):
        self._update_stats(stats)
        self._set_busy(False, "拉取完成")
        self.log("手动拉取完成")

    def _on_run_error(self, err: str):
        self.log(f"错误: {err}")
        self._set_busy(False, f"失败: {err}")

    # ── 启动调度 ──
    def _start_scheduler(self):
        if self._scheduler_running:
            return
        self._scheduler_running = True
        self.scheduler = secondclass_scheduler.SecondClassScheduler()
        # 传递选中的用户
        selected = self._get_selected_users()
        self.scheduler.set_users(selected)
        desc = f"选中 {len(selected)} 个用户" if selected is not None else "全部用户"
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.run_once_btn.configure(state="disabled")
        self._set_busy(True, f"调度运行中 ({desc})…", color="#060")

        self.log("━" * 50)
        self.log(f"启动自动调度 ({desc})")

        threading.Thread(target=self._bg_scheduler_loop, daemon=True).start()
        self._poll_scheduler()

    def _bg_scheduler_loop(self):
        """调度循环（在后台线程运行）。"""
        # 首次立即拉取
        try:
            stats = self.scheduler.run_once()
            self.after(0, lambda: self._update_stats(stats))
        except Exception as e:
            self.after(0, lambda: self.log(f"首次拉取异常: {e}"))

        while self._scheduler_running and not self.scheduler.is_all_expired:
            interval = secondclass_tool.get_schedule_interval()
            desc = secondclass_scheduler._describe_schedule()
            self.after(0, lambda d=desc: self.schedule_desc_var.set(d))
            self.after(0, lambda i=interval: self.log(
                f"下次拉取: {interval // 60} 分钟后 ({desc})"))

            # 分段等待，可被停止
            waited = 0
            while waited < interval and self._scheduler_running:
                time.sleep(5)
                waited += 5

            if not self._scheduler_running:
                break

            try:
                stats = self.scheduler.run_once()
                self.after(0, lambda s=stats: self._update_stats(s))
            except Exception as e:
                self.after(0, lambda e=e: self.log(f"调度异常: {e}"))

        # 循环退出
        if self.scheduler.is_all_expired:
            self.after(0, self._on_all_expired)
        else:
            self.after(0, self._on_scheduler_stopped)

    def _poll_scheduler(self):
        """定期检查调度器状态。"""
        if not self._scheduler_running:
            return
        self._update_schedule_desc()
        if self.scheduler.is_all_expired:
            self._on_all_expired()
            return
        self._poll_timer_id = self.after(5000, self._poll_scheduler)

    def _update_schedule_desc(self):
        desc = secondclass_scheduler._describe_schedule()
        self.schedule_desc_var.set(desc)

    # ── 停止调度 ──
    def _stop_scheduler(self):
        self._scheduler_running = False
        self.scheduler.stop()
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.run_once_btn.configure(state="normal")
        self._set_busy(False, "调度已停止")
        self.schedule_desc_var.set("已停止")
        self.log("自动调度已停止")

        if self._poll_timer_id:
            self.after_cancel(self._poll_timer_id)
            self._poll_timer_id = None

    def _on_scheduler_stopped(self):
        self._stop_scheduler()

    def _on_all_expired(self):
        self._scheduler_running = False
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.run_once_btn.configure(state="normal")
        self._set_busy(False, "所有用户过期，调度停止")
        self.expired_var.set("⚠ 所有用户 SSID 已过期")
        self.log("所有用户 SSID 已过期，调度已停止")
        if self._poll_timer_id:
            self.after_cancel(self._poll_timer_id)
            self._poll_timer_id = None

    # ── 重置过期 ──
    def _reset_expired(self):
        self.scheduler._all_expired = False
        self.expired_var.set("")
        self.log("过期状态已重置")

    # ── SSID 转换 ──
    def _convert_ssid(self):
        if self._busy:
            return
        token = self.token_entry.get().strip()
        ticket = self.ticket_entry.get().strip()
        sid = self.sid_entry.get().strip()

        if not token and not ticket:
            self.log("错误: 至少需要 access_token 或 portal_ticket")
            return

        self.convert_btn.configure(state="disabled", text="转换中…")
        self.ssid_result_var.set("正在桥接获取 SSID…")
        self._set_busy(True, "SSID 转换中…")

        threading.Thread(
            target=self._bg_convert_ssid,
            args=(token, ticket, sid),
            daemon=True,
        ).start()

    def _bg_convert_ssid(self, token: str, ticket: str, sid: str):
        from secondclass.secondclass_tool import convert_to_ssid

        try:
            ssid = convert_to_ssid(
                access_token=token,
                portal_ticket=ticket,
                student_id=sid,
            )
            self.after(0, lambda: self._on_ssid_done(ssid))
        except Exception as e:
            self.after(0, lambda: self._on_ssid_error(str(e)))

    def _on_ssid_done(self, ssid: str):
        self._set_busy(False, "SSID 转换完成")
        self.convert_btn.configure(state="normal", text="开始转换 → SSID")
        if ssid:
            self.ssid_result_var.set(ssid)
            self.copy_btn.configure(state="normal")
            self.log(f"✅ SSID 转换成功: {ssid[:8]}…")
        else:
            self.ssid_result_var.set("❌ 获取 SSID 失败，请检查凭证")
            self.copy_btn.configure(state="disabled")
            self.log("❌ SSID 转换失败: 凭证无效或已过期")

    def _on_ssid_error(self, err: str):
        self._set_busy(False, "SSID 转换失败")
        self.convert_btn.configure(state="normal", text="开始转换 → SSID")
        self.ssid_result_var.set(f"❌ 错误: {err}")
        self.copy_btn.configure(state="disabled")
        self.log(f"❌ SSID 转换异常: {err}")

    def _copy_ssid(self):
        val = self.ssid_result_var.get()
        if val and not val.startswith("❌"):
            self.clipboard_clear()
            self.clipboard_append(val)
            self.log(f"📋 SSID 已复制到剪贴板")

    # ── 更新统计 ──
    def _update_stats(self, stats: dict):
        s = self.scheduler.stats
        total = stats.get("total_count", 0)
        self.stat_var.set(
            f"运行: {s['runs']} 次  |  成功: {s['successful']}  "
            f"|  失败: {s['failed']}  |  活动总数: {self._last_activity_count or total}"
        )
        if stats.get("total_count", 0) > 0:
            self._last_activity_count = stats["total_count"]

    # ── 日志 ──
    def log(self, msg: str):
        self._log_queue.put(msg)

    def _poll_log(self):
        """从日志队列读取并显示。"""
        while not self._log_queue.empty():
            msg = self._log_queue.get_nowait()
            self.log_text.insert("end", str(msg) + "\n")
            self.log_text.see("end")
        self.after(200, self._poll_log)

    def _clear_log(self):
        self.log_text.delete("1.0", "end")

    # ── 忙状态 ──
    def _set_busy(self, busy: bool, status: str = "", color: str = ""):
        self._busy = busy
        if status:
            self.status_var.set(status)
        if color:
            self.status_indicator.configure(foreground=color)
        elif not busy:
            self.status_indicator.configure(foreground="#0a0")
        else:
            self.status_indicator.configure(foreground="#060")

        state = "disabled" if busy else "normal"
        try:
            if not self._scheduler_running:
                self.run_once_btn.configure(state=state)
        except tk.TclError:
            pass

    # ── 关闭 ──
    def _on_close(self):
        if self._scheduler_running:
            self._stop_scheduler()
        self.destroy()

    @property
    def _last_activity_count(self):
        return getattr(self, "__last_act", 0)

    @_last_activity_count.setter
    def _last_activity_count(self, val):
        self.__last_act = val


def main():
    logging.basicConfig(level=logging.WARNING)
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
