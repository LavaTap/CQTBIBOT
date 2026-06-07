"""课表工具 —— 主入口。

向后兼容 re-export + GUI 应用。
"""
from __future__ import annotations

import io
import logging
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

import urllib3
from PIL import Image, ImageTk

from sso.sso_common import load_data, save_data, setup_logging

# ── 从子模块 re-export 公开符号（向后兼容）──
from schedule.models import Course, Schedule, ScheduleError           # noqa: F401
from schedule.parser import ScheduleParser                             # noqa: F401
from schedule.jwgl_client import JWGLClient                            # noqa: F401
from schedule.io import _schedule_path, _load_schedule_from_json, SCHEDULE_DIR  # noqa: F401
from schedule.io import _migrate_schedule_files                        # noqa: F401

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger("schedule")


# ════════════════════ 对话框 ════════════════════

class _WeekRangeDialog(tk.Toplevel):
    """导出周次区间选择对话框。"""

    def __init__(self, parent: tk.Tk, max_week: int) -> None:
        super().__init__(parent)
        self.title("导出周次区间")
        self.resizable(False, False)
        self.grab_set()
        self.confirmed = False
        self.week_start = 1
        self.week_end = max_week

        pad = {"padx": 8, "pady": 4}
        vals = [str(i) for i in range(1, max_week + 1)]

        ttk.Label(self, text="从第").grid(row=0, column=0, **pad)
        self.start_var = tk.StringVar(value="1")
        self.start_cb = ttk.Combobox(self, textvariable=self.start_var, values=vals, width=6, state="readonly")
        self.start_cb.grid(row=0, column=1, **pad)
        ttk.Label(self, text="周到第").grid(row=0, column=2, **pad)
        self.end_var = tk.StringVar(value=str(max_week))
        self.end_cb = ttk.Combobox(self, textvariable=self.end_var, values=vals, width=6, state="readonly")
        self.end_cb.grid(row=0, column=3, **pad)
        ttk.Label(self, text="周").grid(row=0, column=4, **pad)

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=1, column=0, columnspan=5, pady=8)
        ttk.Button(btn_frame, text="确定", command=self._confirm).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="取消", command=self.destroy).pack(side="left", padx=6)

    def _confirm(self) -> None:
        try:
            self.week_start = int(self.start_var.get())
            self.week_end = int(self.end_var.get())
            if self.week_start > self.week_end:
                self.week_start, self.week_end = self.week_end, self.week_start
            self.confirmed = True
            self.destroy()
        except ValueError:
            pass


class _DiffDialog(tk.Toplevel):
    """课表差异对比对话框。"""

    def __init__(self, parent: tk.Tk, old: Schedule, new: Schedule,
                 added: set, removed: set) -> None:
        super().__init__(parent)
        self.title("课表变动对比")
        self.geometry("600x400")
        self.grab_set()
        self.transient(parent)

        pad = {"padx": 8, "pady": 4}
        ttk.Label(self, text="课表变动对比", font=("", 12, "bold")).pack(**pad)

        if added:
            ttk.Label(self, text=f"新增课程（{len(added)} 门）:", foreground="#070").pack(**pad)
            text_a = tk.Text(self, height=6, font=("Consolas", 9), fg="#070")
            for c in sorted(added):
                text_a.insert("end", " +  " + " / ".join(str(x) for x in c) + "\n")
            text_a.configure(state="disabled")
            text_a.pack(fill="x", padx=12, pady=2)

        if removed:
            ttk.Label(self, text=f"移除课程（{len(removed)} 门）:", foreground="#c00").pack(**pad)
            text_r = tk.Text(self, height=6, font=("Consolas", 9), fg="#c00")
            for c in sorted(removed):
                text_r.insert("end", " -  " + " / ".join(str(x) for x in c) + "\n")
            text_r.configure(state="disabled")
            text_r.pack(fill="x", padx=12, pady=2)

        ttk.Button(self, text="关闭", command=self.destroy).pack(pady=8)


# ════════════════════ GUI 主应用 ════════════════════

class App(tk.Tk):
    """课表工具 GUI 主窗口。"""

    REFRESH_INTERVAL = 3600  # 1 小时

    def __init__(self) -> None:
        super().__init__()
        self.title("课表工具")
        self.geometry("960x700")
        self.resizable(True, True)

        self.data = load_data()
        self.client = JWGLClient()
        self.schedule: Schedule | None = None
        self.captcha_image: ImageTk.PhotoImage | None = None
        self._busy = False
        self._refresh_timer_id: str | None = None

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if self.data["accounts"]:
            self.after(200, self._auto_start)

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        acc_frame = ttk.LabelFrame(self, text="SSO 登录")
        acc_frame.pack(fill="x", **pad)

        row1 = ttk.Frame(acc_frame)
        row1.pack(fill="x", padx=6, pady=4)
        ttk.Label(row1, text="已存账号:").pack(side="left")
        self.account_var = tk.StringVar()
        self.account_combo = ttk.Combobox(
            row1, textvariable=self.account_var, state="readonly", width=18,
        )
        self.account_combo.pack(side="left", padx=4)
        self.account_combo.bind("<<ComboboxSelected>>", self._on_pick_account)

        row2 = ttk.Frame(acc_frame)
        row2.pack(fill="x", padx=6, pady=4)
        ttk.Label(row2, text="学号:").pack(side="left")
        self.ucode_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.ucode_var, width=14).pack(side="left", padx=4)
        ttk.Label(row2, text="密码:").pack(side="left")
        self.pwd_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.pwd_var, width=18, show="*").pack(side="left", padx=4)

        row3 = ttk.Frame(acc_frame)
        row3.pack(fill="x", padx=6, pady=4)
        ttk.Label(row3, text="验证码:").pack(side="left")
        self.captcha_label = ttk.Label(row3, text="(加载中…)", width=12)
        self.captcha_label.pack(side="left", padx=4)
        self.captcha_label.bind("<Button-1>", lambda e: self._refresh_captcha())
        self.rcode_var = tk.StringVar()
        ttk.Entry(row3, textvariable=self.rcode_var, width=8).pack(side="left", padx=4)
        ttk.Button(row3, text="刷新验证码", command=self._refresh_captcha).pack(side="left", padx=4)

        row4 = ttk.Frame(acc_frame)
        row4.pack(fill="x", padx=6, pady=4)
        self.login_btn = ttk.Button(row4, text="SSO 登录 + 获取课表", command=self._do_login_and_fetch)
        self.login_btn.pack(side="left", padx=4)
        self.refresh_btn = ttk.Button(row4, text="刷新课表", command=self._do_refresh)
        self.refresh_btn.pack(side="left", padx=4)
        ttk.Label(row4, text="(自动每小时刷新)", foreground="#888").pack(side="left", padx=4)

        sched_frame = ttk.LabelFrame(self, text="课表")
        sched_frame.pack(fill="both", expand=True, **pad)

        sem_row = ttk.Frame(sched_frame)
        sem_row.pack(fill="x", padx=6, pady=2)
        ttk.Label(sem_row, text="学期:").pack(side="left")
        self.semester_var = tk.StringVar(value="2025-2026-2")
        ttk.Entry(sem_row, textvariable=self.semester_var, width=14).pack(side="left", padx=4)
        ttk.Separator(sem_row, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(sem_row, text="周次:").pack(side="left")
        self.week_prev_btn = ttk.Button(sem_row, text="<", width=3, command=self._week_prev)
        self.week_prev_btn.pack(side="left", padx=2)
        self.week_var = tk.StringVar(value="全部")
        self.week_combo = ttk.Combobox(
            sem_row, textvariable=self.week_var,
            values=["全部"] + [str(i) for i in range(1, 21)],
            state="readonly", width=6,
        )
        self.week_combo.pack(side="left", padx=2)
        self.week_combo.bind("<<ComboboxSelected>>", lambda e: self._update_tree())
        self.week_next_btn = ttk.Button(sem_row, text=">", width=3, command=self._week_next)
        self.week_next_btn.pack(side="left", padx=2)
        ttk.Separator(sem_row, orient="vertical").pack(side="left", fill="y", padx=8)
        self.fetched_time_var = tk.StringVar(value="")
        self.fetched_time_label = ttk.Label(sem_row, textvariable=self.fetched_time_var, foreground="#888")
        self.fetched_time_label.pack(side="left", padx=4)

        columns = ("节次", "周一", "周二", "周三", "周四", "周五", "周六", "周日")
        self.tree = ttk.Treeview(sched_frame, columns=columns, show="headings", height=8)
        for col in columns:
            self.tree.heading(col, text=col)
            width = 10 if col == "节次" else 16
            self.tree.column(col, width=width * 7, minwidth=60)
        scrollbar = ttk.Scrollbar(sched_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=6, pady=4)
        scrollbar.pack(side="right", fill="y", pady=4)

        bot_frame = ttk.Frame(self)
        bot_frame.pack(fill="x", **pad)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bot_frame, textvariable=self.status_var, foreground="#0a0").pack(side="left", padx=4)

        btns = ttk.Frame(bot_frame)
        btns.pack(side="right")
        ttk.Button(btns, text="保存 JSON", command=self._save_json).pack(side="left", padx=2)
        excel_btn = ttk.Menubutton(btns, text="保存 Excel ▼")
        excel_menu = tk.Menu(excel_btn, tearoff=False)
        excel_menu.add_command(label="导出全部周", command=self._save_excel_all)
        excel_menu.add_command(label="导出周次区间…", command=self._save_excel_range)
        excel_menu.add_separator()
        excel_menu.add_command(label="对比旧课表变动…", command=self._diff_excel)
        excel_btn["menu"] = excel_menu
        excel_btn.pack(side="left", padx=2)

        self._refresh_account_combo()
        self._try_load_cached_schedule()

    def _refresh_account_combo(self) -> None:
        ids = [a["student_id"] for a in self.data["accounts"]]
        self.account_combo["values"] = ids

    def _on_pick_account(self, _e=None) -> None:
        sid = self.account_var.get()
        for a in self.data["accounts"]:
            if a["student_id"] == sid:
                self.ucode_var.set(sid)
                self.pwd_var.set(a.get("password", ""))
                return

    def _try_load_cached_schedule(self) -> None:
        sid = self.ucode_var.get().strip()
        sem = self.semester_var.get().strip()
        if not sid or not sem:
            return
        path = _schedule_path("json", sid, sem)
        if not path.exists():
            return
        cached = _load_schedule_from_json(path)
        if cached and cached.courses:
            self.schedule = cached
            self._update_tree()
            self._set_busy(False, "已加载缓存课表")

    def _load_captcha_session(self) -> None:
        self._set_busy(True, "拉取验证码…")
        threading.Thread(target=self._bg_begin_sso, daemon=True).start()

    def _bg_begin_sso(self) -> None:
        try:
            img_bytes = self.client.begin_sso()
        except Exception as e:
            log.exception("begin_sso 失败")
            self.after(0, lambda: self._on_captcha_error(str(e)))
            return
        self.after(0, lambda: self._render_captcha(img_bytes))

    def _refresh_captcha(self) -> None:
        if not self.client.acc_key:
            self._load_captcha_session()
            return
        threading.Thread(target=self._bg_refresh_captcha, daemon=True).start()

    def _bg_refresh_captcha(self) -> None:
        try:
            img_bytes = self.client.refresh_captcha()
        except Exception as e:
            log.exception("refresh_captcha 失败")
            self.after(0, lambda: self._on_captcha_error(str(e)))
            return
        self.after(0, lambda: self._render_captcha(img_bytes))

    def _render_captcha(self, img_bytes: bytes) -> None:
        try:
            img = Image.open(io.BytesIO(img_bytes))
            w, h = img.size
            img = img.resize((int(w * 1.5), int(h * 1.5)), Image.LANCZOS)
            self.captcha_image = ImageTk.PhotoImage(img)
            self.captcha_label.configure(image=self.captcha_image, text="")
        except Exception as e:
            self.captcha_label.configure(image="", text=f"图片解析失败: {e}")
        self.rcode_var.set("")
        self._set_busy(False, f"验证码就绪 (accKey={self.client.acc_key})")

    def _on_captcha_error(self, msg: str) -> None:
        self.captcha_label.configure(image="", text=f"加载失败: {msg}")
        self._set_busy(False, f"验证码失败: {msg}")

    def _do_login_and_fetch(self) -> None:
        if self._busy:
            return
        ucode = self.ucode_var.get().strip()
        pwd = self.pwd_var.get()
        rcode = self.rcode_var.get().strip()
        if not ucode or not pwd or not rcode:
            messagebox.showwarning("缺参数", "学号 / 密码 / 验证码都不能空")
            return
        if not self.client.acc_key:
            messagebox.showwarning("等等", "验证码还没就绪")
            return
        self._set_busy(True, "SSO 登录中…")
        threading.Thread(
            target=self._bg_login_and_fetch, args=(ucode, pwd, rcode), daemon=True,
        ).start()

    def _bg_login_and_fetch(self, ucode: str, pwd: str, rcode: str) -> None:
        try:
            self.client.sso_login(ucode, pwd, rcode)
            self.after(0, lambda: self._set_busy(True, "SSO 登录成功，正在桥接 JWGL…"))
            self.client.portal_login()
            self.after(0, lambda: self._set_busy(True, "正在桥接 JWGL…"))
            self.client.bridge()
            self.after(0, lambda: self._set_busy(True, "正在获取课表…"))
            semester = self.semester_var.get().strip()
            schedule = self.client.get_schedule(semester=semester, student_id=ucode)
            self.schedule = schedule
            self.after(0, lambda: self._on_fetch_ok(ucode, pwd))
        except ScheduleError as e:
            self.after(0, lambda: self._on_fetch_failed(str(e)))
        except Exception as e:
            log.exception("登录+获取过程异常")
            self.after(0, lambda: self._on_fetch_failed(f"异常: {e}"))

    def _on_fetch_ok(self, ucode: str, pwd: str) -> None:
        self._update_tree()
        self._set_busy(False, "课表已获取 ✓")
        for a in self.data["accounts"]:
            if a["student_id"] == ucode:
                a["access_token"] = self.client.access_token or a.get("access_token", "")
                a["portal_ticket"] = self.client.portal_ticket or a.get("portal_ticket", "")
                a["last_login"] = datetime.now().isoformat(timespec="seconds")
                break
        else:
            self.data["accounts"].append({
                "student_id": ucode,
                "password": pwd,
                "access_token": self.client.access_token or "",
                "portal_ticket": self.client.portal_ticket or "",
                "expires_at": 0,
                "last_login": datetime.now().isoformat(timespec="seconds"),
            })
        save_data(self.data)
        self._start_refresh_timer()
        self._load_captcha_session()

    def _on_fetch_failed(self, msg: str) -> None:
        messagebox.showerror("失败", msg)
        self._set_busy(False, f"失败: {msg}")
        self._load_captcha_session()

    def _do_refresh(self) -> None:
        if self._busy:
            return
        if not self.client._bridged:
            messagebox.showinfo("提示", "请先 SSO 登录获取课表")
            return
        self._set_busy(True, "刷新课表…")
        threading.Thread(target=self._bg_refresh, daemon=True).start()

    def _bg_refresh(self) -> None:
        try:
            if not self.client.is_jwgl_valid():
                log.info("JWGL session 失效，尝试重新桥接")
                if self.client.portal_ticket:
                    self.client.bridge()
                else:
                    self.after(0, lambda: self._on_refresh_need_login("JWGL 会话已过期，请重新 SSO 登录"))
                    return
            semester = self.semester_var.get().strip()
            schedule = self.client.get_schedule(
                semester=semester, student_id=self.ucode_var.get().strip(),
            )
            self.schedule = schedule
            self.after(0, self._on_refresh_ok)
        except ScheduleError as e:
            self.after(0, lambda: self._on_refresh_need_login(str(e)))
        except Exception as e:
            log.exception("刷新课表异常")
            self.after(0, lambda: self._on_refresh_need_login(f"异常: {e}"))

    def _on_refresh_ok(self) -> None:
        self._update_tree()
        self._set_busy(False, "课表已刷新 ✓")
        self._start_refresh_timer()

    def _on_refresh_need_login(self, msg: str) -> None:
        self._set_busy(False, f"需重新登录: {msg}")
        messagebox.showwarning("会话过期", f"{msg}\n请重新输入验证码登录。")
        self._load_captcha_session()

    def _start_refresh_timer(self) -> None:
        if self._refresh_timer_id:
            self.after_cancel(self._refresh_timer_id)
        self._refresh_timer_id = self.after(
            self.REFRESH_INTERVAL * 1000, self._on_refresh_timer,
        )
        log.info("定时刷新已启动，间隔 %ds", self.REFRESH_INTERVAL)

    def _on_refresh_timer(self) -> None:
        log.info("定时刷新触发")
        self._do_refresh()

    def _get_selected_week(self) -> int:
        v = self.week_var.get()
        if v == "全部" or not v:
            return 0
        try:
            return int(v)
        except ValueError:
            return 0

    def _week_prev(self) -> None:
        w = self._get_selected_week()
        if w <= 1:
            self.week_var.set("全部")
        elif w == 0:
            return
        else:
            self.week_var.set(str(w - 1))
        self._update_tree()

    def _week_next(self) -> None:
        w = self._get_selected_week()
        if w == 0:
            self.week_var.set("1")
        elif w >= 20:
            return
        else:
            self.week_var.set(str(w + 1))
        self._update_tree()

    def _update_fetched_time(self) -> None:
        if self.schedule and self.schedule.fetched_at:
            try:
                dt = datetime.fromisoformat(self.schedule.fetched_at)
                self.fetched_time_var.set(f"获取时间: {dt:%m-%d %H:%M}")
            except (ValueError, OSError):
                self.fetched_time_var.set(f"获取时间: {self.schedule.fetched_at}")
        else:
            self.fetched_time_var.set("")

    def _update_tree(self) -> None:
        self._update_fetched_time()
        self.tree.delete(*self.tree.get_children())
        if not self.schedule:
            return
        week = self._get_selected_week()
        period_labels = [
            (1, 3, "一大节\n1-3节"),
            (4, 5, "二大节\n4-5节"),
            (6, 8, "三大节\n6-8节"),
            (9, 10, "四大节\n9-10节"),
            (11, 12, "五大节\n11-12节"),
        ]
        for ps, pe, label in period_labels:
            row_data = [label]
            for day in range(1, 8):
                matches = [
                    c for c in self.schedule.courses
                    if c.day == day and c.period_start <= pe and c.period_end >= ps
                    and (week == 0 or c.week_matches(week))
                ]
                if matches:
                    parts = []
                    for c in matches:
                        line = c.name
                        if c.teacher:
                            line += f"\n{c.teacher}"
                        if c.room:
                            line += f"\n{c.room}"
                        if c.weeks:
                            line += f"\n{c.weeks}(周)"
                        parts.append(line)
                    row_data.append("\n---\n".join(parts))
                else:
                    row_data.append("")
            self.tree.insert("", "end", values=row_data)

    def _save_json(self) -> None:
        if not self.schedule:
            messagebox.showwarning("无数据", "还没获取课表")
            return
        sid = self.schedule.student_id or self.ucode_var.get().strip()
        sem = self.schedule.semester or self.semester_var.get().strip()
        path = _schedule_path("json", sid, sem)
        self.schedule.save_json(path)
        messagebox.showinfo("已保存", f"课表已保存到\n{path}")

    def _save_excel_all(self) -> None:
        if not self.schedule:
            messagebox.showwarning("无数据", "还没获取课表")
            return
        sid = self.schedule.student_id or self.ucode_var.get().strip()
        sem = self.schedule.semester or self.semester_var.get().strip()
        json_path = _schedule_path("json", sid, sem)
        xlsx_path = _schedule_path("xlsx", sid, sem)
        has_old = json_path.exists()
        if has_old:
            old_schedule = _load_schedule_from_json(json_path)
        self.schedule.save_json(json_path)
        self.schedule.save_excel(xlsx_path)
        self.status_var.set(f"已保存 → {xlsx_path.name}")
        if has_old and old_schedule:
            self._show_diff(old_schedule, self.schedule)

    def _save_excel_range(self) -> None:
        if not self.schedule:
            messagebox.showwarning("无数据", "还没获取课表")
            return
        max_w = self.schedule._max_week()
        dlg = _WeekRangeDialog(self, max_week=max_w)
        self.wait_window(dlg)
        if not dlg.confirmed:
            return
        ws, we = dlg.week_start, dlg.week_end
        sid = self.schedule.student_id or self.ucode_var.get().strip()
        sem = self.schedule.semester or self.semester_var.get().strip()
        json_path = _schedule_path("json", sid, sem)
        xlsx_path = _schedule_path("xlsx", sid, sem)
        has_old = json_path.exists()
        if has_old:
            old_schedule = _load_schedule_from_json(json_path)
        self.schedule.save_json(json_path)
        self.schedule.save_excel(xlsx_path, week_start=ws, week_end=we)
        self.status_var.set(f"已保存 (第{ws}-{we}周) → {xlsx_path.name}")
        if has_old and old_schedule:
            self._show_diff(old_schedule, self.schedule)

    def _diff_excel(self) -> None:
        if not self.schedule:
            messagebox.showwarning("无数据", "还没获取课表")
            return
        sid = self.schedule.student_id or self.ucode_var.get().strip()
        sem = self.schedule.semester or self.semester_var.get().strip()
        json_path = _schedule_path("json", sid, sem)
        old_schedule = _load_schedule_from_json(json_path)
        if old_schedule is None:
            messagebox.showwarning("无旧数据", f"未找到旧课表 JSON:\n{json_path}\n\n请先导出一次课表。")
            return
        self._show_diff(old_schedule, self.schedule)

    def _show_diff(self, old: Schedule, new: Schedule) -> None:
        old_set = {(c.name, c.teacher, c.weeks, c.day, c.period_start, c.period_end, c.room) for c in old.courses}
        new_set = {(c.name, c.teacher, c.weeks, c.day, c.period_start, c.period_end, c.room) for c in new.courses}
        added = new_set - old_set
        removed = old_set - new_set
        if not added and not removed:
            messagebox.showinfo("无变动", "课表与上次导出完全一致，没有变化。")
            return
        _DiffDialog(self, old, new, added, removed)

    def _auto_start(self) -> None:
        if self.data["accounts"]:
            acc = self.data["accounts"][0]
            self.ucode_var.set(acc["student_id"])
            self.pwd_var.set(acc.get("password", ""))
            self.account_var.set(acc["student_id"])
            pt = acc.get("portal_ticket")
            if pt:
                self.client.portal_ticket = pt
                self._set_busy(True, "尝试用已有 ticket 桥接 JWGL…")
                threading.Thread(target=self._bg_try_bridge, daemon=True).start()
                return
        self._load_captcha_session()

    def _bg_try_bridge(self) -> None:
        try:
            self.client.bridge()
            semester = self.semester_var.get().strip()
            schedule = self.client.get_schedule(
                semester=semester, student_id=self.ucode_var.get().strip(),
            )
            self.schedule = schedule
            self.after(0, self._on_auto_bridge_ok)
        except Exception:
            log.info("自动桥接失败，需要重新 SSO 登录")
            self.client.portal_ticket = None
            self.client._bridged = False
            self.after(0, self._load_captcha_session)

    def _on_auto_bridge_ok(self) -> None:
        self._update_tree()
        self._set_busy(False, "用已有 ticket 成功获取课表 ✓")
        self._start_refresh_timer()
        self._load_captcha_session()

    def _set_busy(self, busy: bool, status: str = "") -> None:
        self._busy = busy
        if status:
            self.status_var.set(status)
        try:
            state = "disabled" if busy else "normal"
            self.login_btn.configure(state=state)
            self.refresh_btn.configure(state=state)
        except tk.TclError:
            pass

    def _on_close(self) -> None:
        save_data(self.data)
        self.destroy()


# ════════════════════ 入口 ════════════════════

def main() -> None:
    setup_logging()
    log.info("schedule_tool 启动")
    App().mainloop()
    log.info("schedule_tool 退出")


if __name__ == "__main__":
    main()
