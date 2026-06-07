"""二课自动积分工具 — 图形界面。

基于 Tkinter 的 GUI，集成积分仪表盘、批量报名、总结提交、监控等功能。
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from secondclass import secondclass_tool  # noqa: E402

log = logging.getLogger("auto_score_gui")

# ── 颜色主题 ──
COLORS = {
    "bg": "#f0f2f5",
    "card": "#ffffff",
    "primary": "#4a6cf7",
    "success": "#10b981",
    "warning": "#f59e0b",
    "danger": "#ef4444",
    "text": "#1f2937",
    "text_secondary": "#6b7280",
    "border": "#e5e7eb",
    "header_bg": "#4a6cf7",
    "header_fg": "#ffffff",
}


class _LogHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue):
        super().__init__(level=logging.INFO)
        self.log_queue = log_queue

    def emit(self, record):
        self.log_queue.put(self.format(record))


class CaptchaDialog:
    """验证码输入弹窗 — 在 GUI 中显示验证码图片并让用户手动输入。"""

    def __init__(self, parent: tk.Tk, img_bytes: bytes, activity_name: str = ""):
        self.parent = parent
        self.img_bytes = img_bytes
        self.activity_name = activity_name
        self.result: str | None = None
        self._event = threading.Event()

    def show(self):
        """在主线程中弹出验证码对话框（非阻塞）。"""
        self.parent.after(0, self._show_dialog)

    def wait(self, timeout: int = 300) -> str | None:
        """等待用户输入完成后返回验证码字符串；取消或超时返回 None。"""
        self._event.wait(timeout=timeout)
        return self.result

    def _show_dialog(self):
        dialog = tk.Toplevel(self.parent)
        dialog.title("验证码输入")
        dialog.geometry("340x300")
        dialog.resizable(False, False)
        dialog.transient(self.parent)
        dialog.grab_set()
        dialog.focus_set()

        # 标题
        if self.activity_name:
            tk.Label(
                dialog,
                text=f"活动: {self.activity_name[:30]}",
                font=("Microsoft YaHei UI", 10),
            ).pack(pady=(12, 4))

        # 验证码图片
        try:
            from PIL import Image, ImageTk
            import io

            img = Image.open(io.BytesIO(self.img_bytes)).convert("RGB")
            max_w, max_h = 220, 90
            if img.width > max_w or img.height > max_h:
                ratio = min(max_w / img.width, max_h / img.height)
                img = img.resize(
                    (int(img.width * ratio), int(img.height * ratio)),
                    Image.LANCZOS,
                )
            photo = ImageTk.PhotoImage(img)
            img_label = tk.Label(dialog, image=photo, relief="solid", bd=1)
            img_label.image = photo
            img_label.pack(pady=8)
        except ImportError:
            tk.Label(
                dialog,
                text="(无法渲染验证码图片)\n请查看 _temp 目录下的 captcha_*.jpg",
                font=("Microsoft YaHei UI", 9),
                fg="gray",
            ).pack(pady=12)

        # 提示 + 输入框
        tk.Label(
            dialog,
            text="请输入上方图片中的验证码:",
            font=("Microsoft YaHei UI", 9),
        ).pack(pady=(4, 2))

        code_var = tk.StringVar()
        code_entry = ttk.Entry(dialog, textvariable=code_var, width=24, font=("Consolas", 14))
        code_entry.pack(pady=6)
        code_entry.focus_set()
        code_entry.select_range(0, "end")

        # 按钮
        btn_frame = tk.Frame(dialog)
        btn_frame.pack(pady=(8, 12))

        def on_ok():
            self.result = code_var.get().strip()
            dialog.destroy()
            self._event.set()

        def on_cancel():
            self.result = None
            dialog.destroy()
            self._event.set()

        code_entry.bind("<Return>", lambda e: on_ok())
        dialog.protocol("WM_DELETE_WINDOW", on_cancel)

        ttk.Button(btn_frame, text="确定", command=on_ok, width=8).pack(side="left", padx=8)
        ttk.Button(btn_frame, text="取消", command=on_cancel, width=8).pack(side="left", padx=8)


class App(tk.Tk):
    WIDTH = 960
    HEIGHT = 720

    def __init__(self):
        super().__init__()
        self.title("二课自动积分工具")
        self.configure(bg=COLORS["bg"])

        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = (sw - self.WIDTH) // 2
        y = (sh - self.HEIGHT) // 2
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")
        self.minsize(800, 600)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._sess = None
        self._user = {}
        self._ssid_var = tk.StringVar()
        self._busy = False
        self._monitor_running = False
        self._monitor_thread = None

        self._log_queue: queue.Queue = queue.Queue()
        self._setup_logging()

        self._build_ui()

    def _setup_logging(self):
        fmt = logging.Formatter(
            "%(asctime)s %(levelname)-5s | %(message)s", datefmt="%H:%M:%S"
        )
        handler = _LogHandler(self._log_queue)
        handler.setFormatter(fmt)
        logging.getLogger("secondclass").addHandler(handler)
        logging.getLogger("auto_score").addHandler(handler)
        logging.getLogger("auto_score_gui").addHandler(handler)

    # ════════════════════ UI 构建 ════════════════════

    def _build_ui(self):
        # ── 顶部标题栏 ──
        header = tk.Frame(self, bg=COLORS["header_bg"], height=48)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(
            header,
            text="  二课自动积分工具",
            bg=COLORS["header_bg"],
            fg=COLORS["header_fg"],
            font=("Microsoft YaHei UI", 14, "bold"),
            anchor="w",
        ).pack(side="left", padx=16, fill="y")

        # ── SSID 输入栏 ──
        token_frame = tk.Frame(self, bg=COLORS["card"], pady=6, padx=12)
        token_frame.pack(fill="x", padx=12, pady=(8, 0))

        tk.Label(
            token_frame,
            text="SSID:",
            bg=COLORS["card"],
            font=("Consolas", 10),
        ).pack(side="left")
        token_entry = ttk.Entry(
            token_frame,
            textvariable=self._ssid_var,
            width=48,
            font=("Consolas", 10),
        )
        token_entry.pack(side="left", padx=8)
        ttk.Button(token_frame, text="连接", command=self._connect, width=8).pack(
            side="left", padx=4
        )
        self._status_label = tk.Label(
            token_frame,
            text="未连接",
            bg=COLORS["card"],
            fg=COLORS["danger"],
            font=("Microsoft YaHei UI", 9),
        )
        self._status_label.pack(side="left", padx=12)

        # ── 主体区域：左侧导航 + 右侧内容 ──
        body = tk.Frame(self, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=12, pady=8)

        # 左侧导航
        nav = tk.Frame(body, bg=COLORS["card"], width=160, relief="flat", bd=0)
        nav.pack(side="left", fill="y", padx=(0, 8))
        nav.pack_propagate(False)

        self._nav_buttons = []
        nav_items = [
            ("dashboard", "积分仪表盘"),
            ("signup", "批量报名"),
            ("summary", "提交总结"),
            ("monitor", "自动监控"),
            ("score_edit", "分数编辑"),
            ("log", "运行日志"),
        ]
        for i, (key, label) in enumerate(nav_items):
            btn = tk.Label(
                nav,
                text=f"  {label}",
                bg=COLORS["card"],
                fg=COLORS["text"],
                font=("Microsoft YaHei UI", 11),
                anchor="w",
                cursor="hand2",
                padx=16,
                pady=10,
            )
            btn.pack(fill="x", pady=(2 if i > 0 else 8, 2))
            btn.bind("<Button-1>", lambda e, k=key: self._switch_tab(k))
            btn.bind("<Enter>", lambda e, b=btn: self._nav_hover(b, True))
            btn.bind("<Leave>", lambda e, b=btn: self._nav_hover(b, False))
            self._nav_buttons.append((key, btn))

        # 右侧内容区
        self._content = tk.Frame(body, bg=COLORS["card"])
        self._content.pack(side="left", fill="both", expand=True)

        # 创建各标签页
        self._pages = {}
        self._build_dashboard_page()
        self._build_signup_page()
        self._build_summary_page()
        self._build_monitor_page()
        self._build_score_edit_page()
        self._build_log_page()

        self._switch_tab("dashboard")

        # 启动日志轮询
        self._poll_log()

    def _nav_hover(self, btn, entering):
        if entering:
            btn.configure(bg="#eef2ff")
        else:
            current_tab = getattr(self, "_current_tab", "")
            key = next((k for k, b in self._nav_buttons if b is btn), "")
            if key == current_tab:
                btn.configure(bg="#eef2ff")
            else:
                btn.configure(bg=COLORS["card"])

    def _switch_tab(self, key):
        self._current_tab = key
        for k, btn in self._nav_buttons:
            if k == key:
                btn.configure(
                    bg="#eef2ff",
                    fg=COLORS["primary"],
                    font=("Microsoft YaHei UI", 11, "bold"),
                )
            else:
                btn.configure(
                    bg=COLORS["card"],
                    fg=COLORS["text"],
                    font=("Microsoft YaHei UI", 11),
                )
        for k, page in self._pages.items():
            if k == key:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()

    # ── 通用卡片 ──
    def _card(self, parent, title=""):
        frame = tk.LabelFrame(
            parent,
            text=f"  {title}" if title else "",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Microsoft YaHei UI", 10, "bold"),
            padx=12,
            pady=8,
            relief="groove",
            bd=1,
        )
        return frame

    # ════════════════════ 积分仪表盘 ════════════════════

    def _build_dashboard_page(self):
        page = tk.Frame(self._content, bg=COLORS["card"])
        self._pages["dashboard"] = page

        # 学生信息卡片
        info_card = self._card(page, "学生信息")
        info_card.pack(fill="x", padx=16, pady=(16, 8))
        self._info_labels = {}
        for field in ["姓名", "学号", "班级", "学院", "专业"]:
            row = tk.Frame(info_card, bg=COLORS["card"])
            row.pack(fill="x", pady=1)
            tk.Label(
                row,
                text=f"{field}:",
                width=6,
                anchor="e",
                bg=COLORS["card"],
                fg=COLORS["text_secondary"],
                font=("Microsoft YaHei UI", 10),
            ).pack(side="left")
            lbl = tk.Label(
                row,
                text="--",
                anchor="w",
                bg=COLORS["card"],
                fg=COLORS["text"],
                font=("Microsoft YaHei UI", 10, "bold"),
            )
            lbl.pack(side="left", padx=8)
            self._info_labels[field] = lbl

        # 积分总览卡片
        score_card = self._card(page, "积分总览")
        score_card.pack(fill="x", padx=16, pady=8)

        self._total_score_var = tk.StringVar(value="--")
        self._total_limit_var = tk.StringVar(value="--")
        self._hours_var = tk.StringVar(value="--")

        total_row = tk.Frame(score_card, bg=COLORS["card"])
        total_row.pack(fill="x", pady=4)

        for label, var, color in [
            ("总积分", self._total_score_var, COLORS["primary"]),
            ("需求积分", self._total_limit_var, COLORS["text_secondary"]),
            ("总时长", self._hours_var, COLORS["text_secondary"]),
        ]:
            box = tk.Frame(total_row, bg=COLORS["card"])
            box.pack(side="left", expand=True, padx=8)
            tk.Label(
                box,
                text=label,
                bg=COLORS["card"],
                fg=COLORS["text_secondary"],
                font=("Microsoft YaHei UI", 9),
            ).pack()
            tk.Label(
                box,
                textvariable=var,
                bg=COLORS["card"],
                fg=color,
                font=("Consolas", 20, "bold"),
            ).pack()

        # 模块积分表格
        module_card = self._card(page, "模块积分详情")
        module_card.pack(fill="x", padx=16, pady=8)

        cols = ("module", "current", "required", "gap")
        self._module_tree = ttk.Treeview(
            module_card, columns=cols, show="headings", height=5
        )
        self._module_tree.heading("module", text="模块")
        self._module_tree.heading("current", text="当前积分")
        self._module_tree.heading("required", text="需求积分")
        self._module_tree.heading("gap", text="差距")
        self._module_tree.column("module", width=200, anchor="w")
        self._module_tree.column("current", width=100, anchor="center")
        self._module_tree.column("required", width=100, anchor="center")
        self._module_tree.column("gap", width=100, anchor="center")
        self._module_tree.pack(fill="x", padx=4, pady=4)

        # 首页计数
        count_card = self._card(page, "活动计数")
        count_card.pack(fill="x", padx=16, pady=(8, 16))
        self._count_vars = {}
        count_row = tk.Frame(count_card, bg=COLORS["card"])
        count_row.pack(fill="x", pady=4)
        for label in ["我的活动", "未签到", "未总结", "社团"]:
            box = tk.Frame(count_row, bg=COLORS["card"])
            box.pack(side="left", expand=True, padx=8)
            tk.Label(
                box,
                text=label,
                bg=COLORS["card"],
                fg=COLORS["text_secondary"],
                font=("Microsoft YaHei UI", 9),
            ).pack()
            var = tk.StringVar(value="--")
            tk.Label(
                box,
                textvariable=var,
                bg=COLORS["card"],
                fg=COLORS["text"],
                font=("Consolas", 16, "bold"),
            ).pack()
            self._count_vars[label] = var

        # 刷新按钮
        btn_row = tk.Frame(page, bg=COLORS["card"])
        btn_row.pack(fill="x", padx=16, pady=(0, 16))
        ttk.Button(
            btn_row, text="刷新积分", command=self._refresh_dashboard, width=12
        ).pack(side="left")

    # ════════════════════ 批量报名 ════════════════════

    def _build_signup_page(self):
        page = tk.Frame(self._content, bg=COLORS["card"])
        self._pages["signup"] = page

        # 配置区
        cfg_card = self._card(page, "报名配置")
        cfg_card.pack(fill="x", padx=16, pady=(16, 8))

        row = tk.Frame(cfg_card, bg=COLORS["card"])
        row.pack(fill="x", pady=4)

        tk.Label(
            row, text="最多报名:", bg=COLORS["card"], font=("Microsoft YaHei UI", 10)
        ).pack(side="left")
        self._signup_max_var = tk.StringVar(value="5")
        ttk.Spinbox(
            row, from_=1, to=50, textvariable=self._signup_max_var, width=6
        ).pack(side="left", padx=4)

        self._skip_captcha_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row, text="跳过需要验证码的活动", variable=self._skip_captcha_var
        ).pack(side="left", padx=16)

        # 活动列表
        list_card = self._card(page, "可报名活动")
        list_card.pack(fill="both", expand=True, padx=16, pady=8)

        cols = ("idx", "score", "name", "module", "time", "id")
        self._signup_tree = ttk.Treeview(
            list_card, columns=cols, show="headings", height=10
        )
        self._signup_tree.heading("idx", text="#")
        self._signup_tree.heading("score", text="积分")
        self._signup_tree.heading("name", text="活动名称")
        self._signup_tree.heading("module", text="模块")
        self._signup_tree.heading("time", text="开始时间")
        self._signup_tree.heading("id", text="ID")
        self._signup_tree.column("idx", width=30, anchor="center")
        self._signup_tree.column("score", width=50, anchor="center")
        self._signup_tree.column("name", width=250, anchor="w")
        self._signup_tree.column("module", width=160, anchor="w")
        self._signup_tree.column("time", width=100, anchor="center")
        self._signup_tree.column("id", width=70, anchor="center")
        self._signup_tree.pack(fill="both", expand=True, padx=4, pady=4)

        scrollbar = ttk.Scrollbar(
            list_card, orient="vertical", command=self._signup_tree.yview
        )
        self._signup_tree.configure(yscrollcommand=scrollbar.set)

        # 按钮区
        btn_row = tk.Frame(page, bg=COLORS["card"])
        btn_row.pack(fill="x", padx=16, pady=(0, 16))
        ttk.Button(
            btn_row, text="刷新活动列表", command=self._refresh_signup_list, width=14
        ).pack(side="left", padx=4)
        ttk.Button(btn_row, text="批量报名", command=self._do_signup, width=12).pack(
            side="left", padx=4
        )
        ttk.Button(
            btn_row, text="刷验证码报名", command=self._single_captcha_signup, width=14
        ).pack(side="left", padx=4)
        ttk.Button(
            btn_row, text="扫描新活动", command=self._scan_new_activities, width=12
        ).pack(side="left", padx=4)

        self._signup_progress = tk.Label(
            btn_row,
            text="",
            bg=COLORS["card"],
            fg=COLORS["text_secondary"],
            font=("Microsoft YaHei UI", 9),
        )
        self._signup_progress.pack(side="left", padx=16)

    # ════════════════════ 提交总结 ════════════════════

    def _build_summary_page(self):
        page = tk.Frame(self._content, bg=COLORS["card"])
        self._pages["summary"] = page

        # 总结列表
        list_card = self._card(page, "需要总结的活动（已签到+已结束）")
        list_card.pack(fill="both", expand=True, padx=16, pady=(16, 8))

        cols = ("idx", "score", "name", "module", "id")
        self._summary_tree = ttk.Treeview(
            list_card, columns=cols, show="headings", height=10
        )
        self._summary_tree.heading("idx", text="#")
        self._summary_tree.heading("score", text="积分")
        self._summary_tree.heading("name", text="活动名称")
        self._summary_tree.heading("module", text="模块")
        self._summary_tree.heading("id", text="ID")
        self._summary_tree.column("idx", width=30, anchor="center")
        self._summary_tree.column("score", width=50, anchor="center")
        self._summary_tree.column("name", width=350, anchor="w")
        self._summary_tree.column("module", width=180, anchor="w")
        self._summary_tree.column("id", width=70, anchor="center")
        self._summary_tree.pack(fill="both", expand=True, padx=4, pady=4)

        # 按钮区
        btn_row = tk.Frame(page, bg=COLORS["card"])
        btn_row.pack(fill="x", padx=16, pady=(0, 16))
        ttk.Button(
            btn_row, text="刷新列表", command=self._refresh_summary_list, width=12
        ).pack(side="left", padx=4)
        ttk.Button(
            btn_row, text="全部提交总结", command=self._do_summary, width=14
        ).pack(side="left", padx=4)

        self._summary_progress = tk.Label(
            btn_row,
            text="",
            bg=COLORS["card"],
            fg=COLORS["text_secondary"],
            font=("Microsoft YaHei UI", 9),
        )
        self._summary_progress.pack(side="left", padx=16)

    # ════════════════════ 自动监控 ════════════════════

    def _build_monitor_page(self):
        page = tk.Frame(self._content, bg=COLORS["card"])
        self._pages["monitor"] = page

        # 状态面板
        status_card = self._card(page, "监控状态")
        status_card.pack(fill="x", padx=16, pady=(16, 8))

        self._monitor_status_var = tk.StringVar(value="未启动")
        self._monitor_info_var = tk.StringVar(value="")

        row = tk.Frame(status_card, bg=COLORS["card"])
        row.pack(fill="x", pady=4)
        tk.Label(
            row,
            text="状态:",
            bg=COLORS["card"],
            fg=COLORS["text_secondary"],
            font=("Microsoft YaHei UI", 10),
        ).pack(side="left")
        self._monitor_status_label = tk.Label(
            row,
            textvariable=self._monitor_status_var,
            bg=COLORS["card"],
            fg=COLORS["danger"],
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self._monitor_status_label.pack(side="left", padx=8)
        tk.Label(
            row,
            textvariable=self._monitor_info_var,
            bg=COLORS["card"],
            fg=COLORS["text_secondary"],
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=16)

        # 说明
        info_card = self._card(page, "监控说明")
        info_card.pack(fill="x", padx=16, pady=8)
        info_text = (
            "自动监控会定期检查以下内容：\n"
            "  - 未签到活动（扫码签到暂无法自动处理，仅提示）\n"
            "  - GPS签到活动（需活动开启定位签到，自动使用配置的校园坐标）\n"
            "  - 需要签退的活动\n"
            "  - 已签到已结束需要总结的活动（自动生成并提交总结）\n\n"
            "监控间隔: 60秒 | 签到提前: 30秒 | 签退延迟: 60秒"
        )
        tk.Label(
            info_card,
            text=info_text,
            bg=COLORS["card"],
            fg=COLORS["text_secondary"],
            font=("Microsoft YaHei UI", 9),
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=4, pady=4)

        # 配置
        cfg_card = self._card(page, "监控配置")
        cfg_card.pack(fill="x", padx=16, pady=8)

        cfg_row = tk.Frame(cfg_card, bg=COLORS["card"])
        cfg_row.pack(fill="x", pady=4)
        self._monitor_interval_var = tk.StringVar(value="60")
        tk.Label(
            cfg_row,
            text="检查间隔(秒):",
            bg=COLORS["card"],
            font=("Microsoft YaHei UI", 10),
        ).pack(side="left")
        ttk.Entry(cfg_row, textvariable=self._monitor_interval_var, width=8).pack(
            side="left", padx=4
        )

        self._gps_sign_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            cfg_row, text="启用GPS签到（使用校园坐标）", variable=self._gps_sign_var
        ).pack(side="left", padx=16)

        # 监控日志
        log_card = self._card(page, "监控日志")
        log_card.pack(fill="both", expand=True, padx=16, pady=8)
        self._monitor_log = scrolledtext.ScrolledText(
            log_card,
            height=8,
            font=("Consolas", 9),
            wrap="word",
            relief="flat",
            bg="#fafafa",
        )
        self._monitor_log.pack(fill="both", expand=True, padx=4, pady=4)

        # 按钮区
        btn_row = tk.Frame(page, bg=COLORS["card"])
        btn_row.pack(fill="x", padx=16, pady=(0, 16))
        self._start_monitor_btn = ttk.Button(
            btn_row, text="启动监控", command=self._start_monitor, width=12
        )
        self._start_monitor_btn.pack(side="left", padx=4)
        self._stop_monitor_btn = ttk.Button(
            btn_row,
            text="停止监控",
            command=self._stop_monitor,
            width=12,
            state="disabled",
        )
        self._stop_monitor_btn.pack(side="left", padx=4)

    # ════════════════════ 分数编辑 ════════════════════

    _SCORE_EDIT_TREE_COLS = ("category", "item_name", "score", "limit", "item_id")

    def _build_score_edit_page(self):
        page = tk.Frame(self._content, bg=COLORS["card"])
        self._pages["score_edit"] = page

        self._score_year_var = tk.StringVar(value="20252026")
        self._score_term_var = tk.StringVar(value="2")
        self._online_save = False
        self._score_selected_item: dict | None = None
        self._score_edit_new_var = tk.StringVar(value="")

        # ── 学期选择 + 探测 ──
        sel_card = self._card(page, "学期选择 / 写入模式")
        sel_card.pack(fill="x", padx=16, pady=(16, 8))
        row1 = tk.Frame(sel_card, bg=COLORS["card"])
        row1.pack(fill="x", pady=4)
        tk.Label(
            row1,
            text="学年:",
            bg=COLORS["card"],
            fg=COLORS["text_secondary"],
            font=("Microsoft YaHei UI", 10),
        ).pack(side="left")
        ttk.Combobox(
            row1,
            textvariable=self._score_year_var,
            values=["20252026", "20242025"],
            width=10,
            state="readonly",
        ).pack(side="left", padx=6)
        tk.Label(
            row1,
            text="学期:",
            bg=COLORS["card"],
            fg=COLORS["text_secondary"],
            font=("Microsoft YaHei UI", 10),
        ).pack(side="left", padx=(12, 0))
        ttk.Combobox(
            row1,
            textvariable=self._score_term_var,
            values=["1", "2", ""],
            width=4,
            state="readonly",
        ).pack(side="left", padx=6)
        ttk.Button(
            row1, text="刷新明细", command=self._refresh_score_items, width=10
        ).pack(side="left", padx=8)
        ttk.Button(
            row1,
            text="探测写入端点",
            command=self._probe_score_endpoints,
            width=14,
        ).pack(side="left", padx=4)
        self._score_edit_mode_label = tk.Label(
            row1,
            text="模式: 未探测",
            bg=COLORS["card"],
            fg=COLORS["text_secondary"],
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        self._score_edit_mode_label.pack(side="left", padx=12)

        # ── 分数明细 Treeview ──
        list_card = self._card(page, "分数明细（双击行编辑）")
        list_card.pack(fill="both", expand=True, padx=16, pady=8)
        cols = self._SCORE_EDIT_TREE_COLS
        self._score_edit_tree = ttk.Treeview(
            list_card, columns=cols, show="headings", height=12
        )
        headings = {
            "category": ("分类", 140),
            "item_name": ("项目名称", 360),
            "score": ("当前分数", 90),
            "limit": ("上限", 70),
            "item_id": ("ID", 70),
        }
        for col, (label, width) in headings.items():
            self._score_edit_tree.heading(col, text=label)
            self._score_edit_tree.column(col, width=width, anchor="w")
        self._score_edit_tree.pack(fill="both", expand=True, padx=4, pady=4)
        self._score_edit_tree.bind("<<TreeviewSelect>>", self._on_score_row_select)
        self._score_edit_tree.bind("<Double-1>", lambda e: self._open_score_editor())

        # ── 编辑面板 ──
        edit_card = self._card(page, "编辑")
        edit_card.pack(fill="x", padx=16, pady=8)
        row2 = tk.Frame(edit_card, bg=COLORS["card"])
        row2.pack(fill="x", pady=4)
        tk.Label(
            row2,
            text="选中项:",
            bg=COLORS["card"],
            fg=COLORS["text_secondary"],
            font=("Microsoft YaHei UI", 10),
        ).pack(side="left")
        self._score_edit_selected_label = tk.Label(
            row2,
            text="（未选中）",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Microsoft YaHei UI", 10),
        )
        self._score_edit_selected_label.pack(side="left", padx=8)

        row3 = tk.Frame(edit_card, bg=COLORS["card"])
        row3.pack(fill="x", pady=4)
        tk.Label(
            row3,
            text="新分数:",
            bg=COLORS["card"],
            fg=COLORS["text_secondary"],
            font=("Microsoft YaHei UI", 10),
        ).pack(side="left")
        ttk.Entry(
            row3, textvariable=self._score_edit_new_var, width=10
        ).pack(side="left", padx=6)
        ttk.Button(
            row3,
            text="保存到服务器",
            command=lambda: self._save_score_item(force_local=False),
            width=14,
        ).pack(side="left", padx=4)
        ttk.Button(
            row3,
            text="仅写本地 DB",
            command=lambda: self._save_score_item(force_local=True),
            width=12,
        ).pack(side="left", padx=4)

        self._score_edit_status_label = tk.Label(
            edit_card,
            text="就绪",
            bg=COLORS["card"],
            fg=COLORS["text_secondary"],
            font=("Microsoft YaHei UI", 9),
        )
        self._score_edit_status_label.pack(anchor="w", padx=4, pady=(4, 0))

    def _refresh_score_items(self):
        if not self._ensure_session():
            return
        if self._busy:
            return
        self._busy = True
        self._score_edit_status_label.configure(
            text="加载中...", fg=COLORS["warning"]
        )
        threading.Thread(
            target=self._refresh_score_items_worker, daemon=True
        ).start()

    def _refresh_score_items_worker(self):
        try:
            items = secondclass_tool.fetch_score_items_with_session(
                self._sess,
                year_id=self._score_year_var.get(),
                term_id=self._score_term_var.get(),
            )
            self.after(0, self._update_score_tree, items)
        except Exception as e:
            log.error("加载分数明细失败: %s", e)
            self.after(
                0,
                lambda: self._score_edit_status_label.configure(
                    text=f"失败: {e}", fg=COLORS["danger"]
                ),
            )
        finally:
            self._busy = False

    def _update_score_tree(self, items):
        for iid in self._score_edit_tree.get_children():
            self._score_edit_tree.delete(iid)
        for i, it in enumerate(items):
            self._score_edit_tree.insert(
                "",
                "end",
                iid=f"score_{i}",
                values=(
                    it.get("category", ""),
                    it.get("item_name", ""),
                    it.get("score", 0),
                    it.get("limit", 0),
                    it.get("item_id", ""),
                ),
            )
        self._score_edit_status_label.configure(
            text=f"共 {len(items)} 项", fg=COLORS["success"]
        )

    def _on_score_row_select(self, _event=None):
        sel = self._score_edit_tree.selection()
        if not sel:
            return
        values = self._score_edit_tree.item(sel[0], "values")
        if not values or len(values) < 5:
            return
        self._score_selected_item = {
            "category": values[0],
            "item_name": values[1],
            "score": float(values[2] or 0),
            "limit": float(values[3] or 0),
            "item_id": values[4],
        }
        self._score_edit_selected_label.configure(
            text=f"{values[0]} / {values[1]}（当前 {values[2]}）"
        )
        self._score_edit_new_var.set(str(values[2]))

    def _open_score_editor(self):
        if not self._score_selected_item:
            messagebox.showinfo("提示", "请先选中一行")
            return
        # 双击就是直接进入编辑态：聚焦到输入框即可（已在上方）
        self._score_edit_status_label.configure(
            text="请在上方修改分数后点保存", fg=COLORS["warning"]
        )

    def _probe_score_endpoints(self):
        if not self._ensure_session():
            return
        self._score_edit_mode_label.configure(text="探测中...", fg=COLORS["warning"])
        threading.Thread(
            target=self._probe_score_endpoints_worker, daemon=True
        ).start()

    def _probe_score_endpoints_worker(self):
        try:
            result = secondclass_tool.probe_score_edit_endpoints(self._sess)
            self.after(0, self._apply_probe_result, result)
        except Exception as e:
            log.error("探测分数端点失败: %s", e)
            self.after(
                0,
                lambda: self._score_edit_mode_label.configure(
                    text=f"探测失败: {e}", fg=COLORS["danger"]
                ),
            )

    def _apply_probe_result(self, result: dict):
        online = bool(result.get("online_available"))
        self._online_save = online
        if online:
            self._score_edit_mode_label.configure(
                text=f"模式: 在线 ({result.get('save_endpoint') or result.get('edit_page')})",
                fg=COLORS["success"],
            )
        else:
            self._score_edit_mode_label.configure(
                text="模式: 仅本地（服务器未开放写入）",
                fg=COLORS["warning"],
            )

    def _save_score_item(self, force_local: bool = False):
        if not self._ensure_session():
            return
        if not self._score_selected_item:
            messagebox.showwarning("提示", "请先选中一行")
            return
        raw = self._score_edit_new_var.get().strip()
        try:
            new_score = float(raw)
        except ValueError:
            messagebox.showwarning("提示", f"分数必须是数字: {raw!r}")
            return

        item = dict(self._score_selected_item)
        student_id = (self._user or {}).get("student_id", "")
        if not student_id:
            # 兜底：从 GUI 顶部信息标签拿
            student_id = self._info_labels.get(
                "学号", FakeNone()
            ).cget("text") if hasattr(self, "_info_labels") else ""
        student_id = str(student_id or "2403740").strip()

        self._score_edit_status_label.configure(
            text="保存中...", fg=COLORS["warning"]
        )

        threading.Thread(
            target=self._save_score_item_worker,
            args=(student_id, item, new_score, force_local),
            daemon=True,
        ).start()

    def _save_score_item_worker(
        self, student_id: str, item: dict, new_score: float, force_local: bool
    ):
        try:
            use_online = self._online_save and not force_local
            if use_online:
                # 服务器无写入端点：直接 fallback
                result = {
                    "success": False,
                    "mode": "online",
                    "message": "服务器未开放写入端点",
                }
            else:
                result = secondclass_tool.save_score_item_local(
                    student_id=student_id, item=item, new_score=new_score
                )
            self.after(0, self._on_save_done, result)
        except Exception as e:
            log.error("保存分数失败: %s", e)
            self.after(
                0,
                lambda: self._score_edit_status_label.configure(
                    text=f"保存失败: {e}", fg=COLORS["danger"]
                ),
            )

    def _on_save_done(self, result: dict):
        if result.get("success"):
            old = result.get("old_score", 0)
            new = result.get("new_score", 0)
            field = result.get("field", "")
            mode = result.get("mode", "")
            self._score_edit_status_label.configure(
                text=f"✓ {mode} {field}: {old} → {new}",
                fg=COLORS["success"],
            )
            self.after(300, self._refresh_score_items)
        else:
            msg = result.get("message", "未知错误")
            self._score_edit_status_label.configure(
                text=f"✗ 保存失败: {msg}", fg=COLORS["danger"]
            )

    # ════════════════════ 运行日志 ════════════════════

    def _build_log_page(self):
        page = tk.Frame(self._content, bg=COLORS["card"])
        self._pages["log"] = page

        log_card = self._card(page, "运行日志")
        log_card.pack(fill="both", expand=True, padx=16, pady=16)

        self._log_text = scrolledtext.ScrolledText(
            log_card, font=("Consolas", 9), wrap="word", relief="flat", bg="#fafafa"
        )
        self._log_text.pack(fill="both", expand=True, padx=4, pady=4)

        btn_row = tk.Frame(page, bg=COLORS["card"])
        btn_row.pack(fill="x", padx=16, pady=(0, 16))
        ttk.Button(
            btn_row,
            text="清空日志",
            command=lambda: self._log_text.delete("1.0", "end"),
            width=10,
        ).pack(side="left")

    # ════════════════════ 连接 ════════════════════

    def _connect(self):
        ssid = self._ssid_var.get().strip()
        if not ssid:
            messagebox.showwarning("提示", "请输入SSID")
            return

        self._status_label.configure(text="连接中...", fg=COLORS["warning"])
        self.update_idletasks()

        sess = secondclass_tool._session(ssid)
        try:
            r = sess.get(
                f"{secondclass_tool.BASE_URL}/Student/My/index.html", timeout=10
            )
            if r.status_code == 200 and "top.location.href" not in r.text:
                self._sess = sess
                self._user = {"student_id": "", "realname": ""}
                self._status_label.configure(text="已连接", fg=COLORS["success"])
                self.after(100, self._refresh_dashboard)
            else:
                self._status_label.configure(text="SSID已过期", fg=COLORS["danger"])
        except Exception as e:
            self._status_label.configure(text="连接失败", fg=COLORS["danger"])
            log.error("连接失败: %s", e)

    def _ensure_session(self) -> bool:
        if self._sess is not None:
            return True
        messagebox.showwarning("提示", "请先输入SSID并点击连接")
        return False

    # ════════════════════ 仪表盘逻辑 ════════════════════

    def _refresh_dashboard(self):
        if not self._ensure_session():
            return
        self._busy = True
        threading.Thread(target=self._refresh_dashboard_worker, daemon=True).start()

    def _refresh_dashboard_worker(self):
        try:
            import os

            os.environ["AUTO_SCORE_SSID"] = ""
            cfg = {
                "year_term": "20252026-2",
            }

            data = secondclass_tool.fetch_score_data_json(
                self._sess, year_term=cfg["year_term"]
            )

            student = data.get("dataStudent") or {}
            total = float(data.get("scoreTotal", 0))
            limit = float(data.get("scoreTotalLimit", 0))
            modules = data.get("modules", [])

            counts = secondclass_tool.fetch_index_counts_with_session(self._sess)

            self.after(
                0, self._update_dashboard_ui, student, total, limit, modules, counts
            )
        except Exception as e:
            log.error("刷新仪表盘失败: %s", e)
            self.after(
                0,
                lambda: self._status_label.configure(
                    text="刷新失败", fg=COLORS["danger"]
                ),
            )
        finally:
            self._busy = False

    def _update_dashboard_ui(self, student, total, limit, modules, counts):
        self._info_labels["姓名"].configure(text=student.get("studentName", "--"))
        self._info_labels["学号"].configure(text=student.get("studentID", "--"))
        self._info_labels["班级"].configure(text=student.get("className", "--"))
        self._info_labels["学院"].configure(text=student.get("collegeName", "--"))
        self._info_labels["专业"].configure(text=student.get("major", "--"))

        self._total_score_var.set(f"{total:.1f}")
        self._total_limit_var.set(f"{limit:.1f}")
        self._hours_var.set(f"{counts.get('hoursTotal', 0)}h" if counts else "--")

        for item in self._module_tree.get_children():
            self._module_tree.delete(item)

        for m in modules:
            name = m.get("name", "")
            zf = float(m.get("zf") or 0)
            avg = float(m.get("avg") or 0)
            gap = zf - avg
            gap_str = f"{gap:+.1f}" if gap < 0 else "OK"
            tag = "ok" if gap >= 0 else "gap"
            self._module_tree.insert(
                "",
                "end",
                values=(name, f"{zf:.1f}", f"{avg:.1f}", gap_str),
                tags=(tag,),
            )

        self._module_tree.tag_configure("gap", foreground=COLORS["danger"])
        self._module_tree.tag_configure("ok", foreground=COLORS["success"])

        if counts:
            self._count_vars["我的活动"].set(str(int(counts.get("activity_count", 0))))
            self._count_vars["未签到"].set(str(int(counts.get("unsigned_count", 0))))
            self._count_vars["未总结"].set(str(int(counts.get("unfinished_count", 0))))
            self._count_vars["社团"].set(str(int(counts.get("club_count", 0))))

    # ════════════════════ 报名逻辑 ════════════════════

    def _refresh_signup_list(self):
        if not self._ensure_session():
            return
        self._busy = True
        self._signup_progress.configure(text="正在加载活动列表...")
        threading.Thread(target=self._refresh_signup_list_worker, daemon=True).start()

    def _refresh_signup_list_worker(self):
        try:
            activities = secondclass_tool.fetch_activities_can_apply(
                self._sess, sort_by_score="desc", max_results=200
            )
            my_activities = secondclass_tool.fetch_all_my_activities(self._sess)
            signed_ids = set()
            for tab_acts in my_activities.values():
                for act in tab_acts:
                    aid = str(act.get("activity_id", act.get("activityID", "")))
                    if aid:
                        signed_ids.add(aid)

            from secondclass_auto_score import filter_eligible_activities

            eligible = filter_eligible_activities(activities, exclude_ids=signed_ids)

            self.after(0, self._update_signup_list, eligible)
        except Exception as e:
            log.error("刷新活动列表失败: %s", e)
            self.after(0, lambda: self._signup_progress.configure(text="加载失败"))
        finally:
            self._busy = False

    def _update_signup_list(self, eligible):
        for item in self._signup_tree.get_children():
            self._signup_tree.delete(item)

        for i, act in enumerate(eligible, 1):
            aid = act.get("activity_id", act.get("activityID", ""))
            name = act.get("activity_name", act.get("activityName", "?"))
            score = act.get("score", 0)
            module = act.get("module_name", act.get("moduleName", ""))
            start = act.get("start_date", act.get("startDate", ""))
            if isinstance(start, int) and start > 0:
                start = datetime.fromtimestamp(start).strftime("%m-%d %H:%M")
            else:
                start = str(start)[:12] if start else "--"
            self._signup_tree.insert(
                "", "end", values=(i, f"{score:.1f}", name, module, start, aid)
            )

        self._signup_progress.configure(text=f"共 {len(eligible)} 个可报名活动")

    def _do_signup(self):
        if not self._ensure_session():
            return
        items = self._signup_tree.get_children()
        if not items:
            messagebox.showinfo("提示", "请先刷新活动列表")
            return

        max_n = int(self._signup_max_var.get() or 5)
        count = min(len(items), max_n)
        if not messagebox.askyesno("确认", f"即将报名 {count} 个活动，确认继续？"):
            return

        self._busy = True
        self._signup_progress.configure(text=f"报名中 (0/{count})...")
        threading.Thread(
            target=self._do_signup_worker, args=(count,), daemon=True
        ).start()

    def _single_captcha_signup(self):
        """刷验证码报名：选中单个活动，刷验证码弹窗，用户输入后报名。"""
        if not self._ensure_session():
            return
        sel = self._signup_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先在列表中点击选中一个活动")
            return
        values = self._signup_tree.item(sel[0], "values")
        aid = str(values[5])
        name = str(values[2])
        if not messagebox.askyesno("确认", f"即将刷验证码报名:\n{name}\n确定继续？"):
            return
        self._busy = True
        self._signup_progress.configure(text=f"刷码报名: {name[:20]}...")
        threading.Thread(
            target=self._do_single_captcha_signup_worker,
            args=(aid, name),
            daemon=True,
        ).start()

    def _do_single_captcha_signup_worker(self, activity_id: str, activity_name: str):
        """刷验证码报名的工作线程：取页面 → 取验证码 → 弹窗 → 提交。"""
        result = self._signup_single_activity(activity_id, activity_name, skip_captcha=False)
        msg = result.get("message", "")
        if result.get("success"):
            log.info("刷码报名成功: %s - %s", activity_name, msg)
        else:
            log.warning("刷码报名失败: %s - %s", activity_name, msg)
        self.after(
            0,
            lambda: self._signup_progress.configure(
                text=f"刷码报名 {'成功' if result['success'] else '失败'}: {msg}"
            ),
        )
        self._busy = False

    def _scan_new_activities(self):
        """扫描发现新活动（含报名未开始）。"""
        if not self._ensure_session():
            return
        self._busy = True
        self._signup_progress.configure(text="正在扫描新活动...")
        threading.Thread(target=self._scan_new_activities_worker, daemon=True).start()

    def _scan_new_activities_worker(self):
        try:
            student_id = self._user.get("student_id", "")
            result = secondclass_tool.discover_new_activities(
                self._sess, student_id=student_id,
                scan_range=50, delay=0.3,
            )
            d = result.get("discovered", 0)
            u = result.get("upcoming", 0)
            log.info("扫描新活动完成: 发现 %d 个, 报名未开始 %d 个", d, u)
            self.after(
                0,
                lambda: self._signup_progress.configure(
                    text=f"扫描完成: 发现 {d} 个, 报名未开始 {u} 个"
                ),
            )
        except Exception as e:
            log.warning("扫描新活动失败: %s", e)
            self.after(
                0,
                lambda: self._signup_progress.configure(text=f"扫描失败: {e}"),
            )
        finally:
            self._busy = False

    def _do_signup_worker(self, count):
        import random

        items = self._signup_tree.get_children()
        success = 0
        fail = 0
        skip_captcha = self._skip_captcha_var.get()

        for i, item in enumerate(items[:count]):
            values = self._signup_tree.item(item, "values")
            aid = str(values[5])
            name = str(values[2])
            self.after(
                0,
                lambda n=name: self._signup_progress.configure(
                    text=f"报名: {n[:20]}..."
                ),
            )

            result = self._signup_single_activity(aid, name, skip_captcha)
            if result.get("success"):
                success += 1
                log.info("报名成功: %s", name)
            else:
                fail += 1
                log.warning("报名失败: %s - %s", name, result.get("message", ""))

            self.after(
                0,
                lambda s=success, f=fail, c=count: self._signup_progress.configure(
                    text=f"报名中 ({s + f}/{c}) 成功:{s} 失败:{f}"
                ),
            )
            time.sleep(random.uniform(2, 4))

        self.after(
            0,
            lambda: self._signup_progress.configure(
                text=f"报名完成: 成功 {success}, 失败 {fail}"
            ),
        )
        self._busy = False

    def _reauthenticate(self) -> bool:
        """尝试重新获取 session：
           1. 用 GUI 中已输入的 SSID 重新验证
           2. 从本地存储的凭证重新获取
        """
        saved_ssid = self._ssid_var.get().strip()
        if saved_ssid:
            log.info("尝试用已保存的 SSID 重新连接...")
            sess = secondclass_tool._session(saved_ssid)
            try:
                r = sess.get(
                    f"{secondclass_tool.BASE_URL}/Student/My/index.html", timeout=10
                )
                if r.status_code == 200 and "top.location.href" not in r.text:
                    self._sess = sess
                    log.info("重新认证成功（使用 SSID）")
                    return True
            except Exception:
                pass
            log.info("已保存的 SSID 也已过期")

        log.info("尝试从本地存储的凭证重新认证...")
        try:
            users = secondclass_tool.get_all_user_credentials()
            if not users:
                log.warning("未找到本地存储的用户凭证")
                return False
            user = users[0]
            self._sess = secondclass_tool.obtain_secondclass_session_from_user(user)
            log.info("重新认证成功（使用本地凭证）")
            return True
        except Exception as e:
            log.warning("本地凭证认证失败: %s", e)
            return False

    def _signup_single_activity(
        self, activity_id: str, activity_name: str, skip_captcha: bool
    ) -> dict:
        """在 GUI 中报名单个活动（含验证码弹窗 + 自动重连）。"""
        for attempt in range(2):  # 第 0 次正常请求，第 1 次重试（重新认证后）
            try:
                apply_data = secondclass_tool.fetch_apply_page(self._sess, activity_id)
                break  # 成功则跳出重试循环
            except secondclass_tool.SecondClassAuthError:
                if attempt == 0:
                    log.warning("二课登录已过期，尝试自动重新认证...")
                    if self._reauthenticate():
                        continue  # 重试
                return {
                    "success": False,
                    "message": "二课登录已过期，请重新输入 SSID 连接",
                }
            except Exception as e:
                return {"success": False, "message": f"获取报名页面失败: {e}"}

        if not apply_data.get("s1") or not apply_data.get("s2"):
            return {"success": False, "message": "报名页面缺少必要字段"}

        captcha_code = ""
        if apply_data.get("need_captcha") and not skip_captcha:
            try:
                img_bytes = secondclass_tool.fetch_verifycode_image(self._sess)
            except Exception as e:
                return {"success": False, "message": f"获取验证码失败: {e}"}

            dlg = CaptchaDialog(self, img_bytes, activity_name)
            dlg.show()
            code = dlg.wait(timeout=300)
            if not code:
                return {"success": False, "message": "用户取消或验证码输入超时"}
            captcha_code = code
        elif apply_data.get("need_captcha") and skip_captcha:
            return {"success": False, "message": "需要验证码，已跳过"}

        try:
            result = secondclass_tool.submit_activity_apply(
                self._sess,
                activity_id,
                captcha_code,
                apply_data["s1"],
                apply_data["s2"],
            )
            return result
        except Exception as e:
            return {"success": False, "message": f"报名请求失败: {e}"}

    # ════════════════════ 总结逻辑 ════════════════════

    def _refresh_summary_list(self):
        if not self._ensure_session():
            return
        self._busy = True
        self._summary_progress.configure(text="正在加载...")
        threading.Thread(target=self._refresh_summary_list_worker, daemon=True).start()

    def _refresh_summary_list_worker(self):
        try:
            from secondclass_auto_score import fetch_my_activities_needing_action

            actions = fetch_my_activities_needing_action(self._sess)
            self.after(0, self._update_summary_list, actions["need_summary"])
        except Exception as e:
            log.error("刷新总结列表失败: %s", e)
            self.after(0, lambda: self._summary_progress.configure(text="加载失败"))
        finally:
            self._busy = False

    def _update_summary_list(self, need_summary):
        for item in self._summary_tree.get_children():
            self._summary_tree.delete(item)

        for i, act in enumerate(need_summary, 1):
            aid = act.get("activity_id", "")
            name = act.get("activity_name", "?")
            score = act.get("score", 0)
            module = act.get("module_name", "")
            self._summary_tree.insert(
                "", "end", values=(i, f"{score:.1f}", name, module, aid)
            )

        self._summary_progress.configure(text=f"共 {len(need_summary)} 个需要总结")

    def _do_summary(self):
        if not self._ensure_session():
            return
        items = self._summary_tree.get_children()
        if not items:
            messagebox.showinfo("提示", "请先刷新列表，或当前没有需要总结的活动")
            return

        count = len(items)
        if not messagebox.askyesno(
            "确认", f"即将为 {count} 个活动提交总结，确认继续？"
        ):
            return

        self._busy = True
        threading.Thread(
            target=self._do_summary_worker, args=(count,), daemon=True
        ).start()

    def _do_summary_worker(self, count):
        from secondclass_auto_score import (
            submit_activity_summary,
            generate_activity_summary,
        )
        import random

        items = self._summary_tree.get_children()
        success = 0
        fail = 0

        for i, item in enumerate(items[:count]):
            values = self._summary_tree.item(item, "values")
            aid = str(values[4])
            name = str(values[2])
            summary = generate_activity_summary({"activity_name": name})

            self.after(
                0,
                lambda n=name: self._summary_progress.configure(
                    text=f"提交: {n[:20]}..."
                ),
            )

            result = submit_activity_summary(self._sess, aid, summary)
            if result.get("success"):
                success += 1
                log.info("总结提交成功: %s", name)
            else:
                fail += 1
                log.warning("总结提交失败: %s - %s", name, result.get("message", ""))

            self.after(
                0,
                lambda s=success, f=fail: self._summary_progress.configure(
                    text=f"提交中 ({s + f}/{count}) 成功:{s} 失败:{f}"
                ),
            )
            time.sleep(random.uniform(2, 4))

        self.after(
            0,
            lambda: self._summary_progress.configure(
                text=f"提交完成: 成功 {success}, 失败 {fail}"
            ),
        )
        self._busy = False

    # ════════════════════ 监控逻辑 ════════════════════

    def _start_monitor(self):
        if not self._ensure_session():
            return
        self._monitor_running = True
        self._start_monitor_btn.configure(state="disabled")
        self._stop_monitor_btn.configure(state="normal")
        self._monitor_status_var.set("运行中")
        self._monitor_status_label.configure(fg=COLORS["success"])

        interval = int(self._monitor_interval_var.get() or 60)
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, args=(interval,), daemon=True
        )
        self._monitor_thread.start()

    def _stop_monitor(self):
        self._monitor_running = False
        self._start_monitor_btn.configure(state="normal")
        self._stop_monitor_btn.configure(state="disabled")
        self._monitor_status_var.set("已停止")
        self._monitor_status_label.configure(fg=COLORS["danger"])

    def _monitor_loop(self, interval):
        from secondclass_auto_score import (
            fetch_my_activities_needing_action,
            submit_activity_sign_in,
            submit_activity_summary,
            generate_activity_summary,
        )
        import random

        while self._monitor_running:
            now = datetime.now()
            self.after(
                0,
                lambda: self._monitor_info_var.set(
                    f"上次检查: {now.strftime('%H:%M:%S')}"
                ),
            )
            self.after(
                0,
                lambda: self._monitor_log.insert(
                    "end", f"\n[{now.strftime('%H:%M:%S')}] 检查活动状态...\n"
                ),
            )

            try:
                actions = fetch_my_activities_needing_action(self._sess)

                # 签到
                sign_in_count = len(actions["need_sign_in"])
                if sign_in_count > 0:
                    msg = f"  未签到: {sign_in_count} 个\n"
                    for act in actions["need_sign_in"][:5]:
                        name = act.get("activity_name", "?")
                        gps = act.get("gps", "0")
                        gps_note = " [GPS]" if gps == "1" else ""
                        msg += f"    - {name}{gps_note}\n"
                    self.after(0, lambda m=msg: self._monitor_log.insert("end", m))

                # GPS签到
                if self._gps_sign_var.get():
                    for act in actions["need_sign_in"]:
                        if act.get("gps") == "1":
                            aid = act.get("activity_id", "")
                            name = act.get("activity_name", "?")
                            result = submit_activity_sign_in(self._sess, aid)
                            status = "OK" if result["success"] else "FAIL"
                            self.after(
                                0,
                                lambda n=name, s=status, m=result["message"]: (
                                    self._monitor_log.insert(
                                        "end", f"  GPS签到: {n} -> {s} {m}\n"
                                    )
                                ),
                            )
                            time.sleep(random.uniform(1, 3))

                # 总结
                for act in actions["need_summary"]:
                    aid = act.get("activity_id", "")
                    name = act.get("activity_name", "?")
                    summary = generate_activity_summary(act)
                    result = submit_activity_summary(self._sess, aid, summary)
                    status = "OK" if result["success"] else "FAIL"
                    self.after(
                        0,
                        lambda n=name, s=status, m=result["message"]: (
                            self._monitor_log.insert("end", f"  总结: {n} -> {s} {m}\n")
                        ),
                    )
                    time.sleep(random.uniform(2, 4))

                if sign_in_count == 0 and not actions["need_summary"]:
                    self.after(
                        0,
                        lambda: self._monitor_log.insert(
                            "end", "  所有可自动处理的活动均已完成\n"
                        ),
                    )

            except Exception as e:
                log.error("监控检查异常: %s", e)

            self.after(0, lambda: self._monitor_log.see("end"))

            time.sleep(interval)

    # ════════════════════ 日志轮询 ════════════════════

    def _poll_log(self):
        while not self._log_queue.empty():
            try:
                msg = self._log_queue.get_nowait()
                self._log_text.insert("end", msg + "\n")
                self._log_text.see("end")
            except Exception:
                break
        self.after(200, self._poll_log)

    def _on_close(self):
        self._monitor_running = False
        self.destroy()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)-5s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
