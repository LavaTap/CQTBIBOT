"""QQ群消息转发工具

基于 OneBot v11 协议（NapCatQQ）方案：通过 WebSocket 连接 NapCat，
监听源群消息事件，自动转发到目标群。支持文本、图片等富媒体转发。

功能：
  - 事件驱动，消息即时转发，无延迟
  - 支持文本、图片、表情等富媒体消息转发
  - 基于 message_id 去重，防止重复转发
  - 自动重连，可长期稳定运行

使用前：
  1. 安装并启动 NapCatQQ，配置正向 WebSocket 端口（默认 3001）
  2. 运行本脚本，填写 ws 地址、源群号、目标群号
  3. 点击"启动监控"

依赖：pip install websocket-client
"""
from __future__ import annotations

import base64
import json
import logging
import re
import sqlite3
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from secondclass import secondclass_tool
from qq.help_image import get_help_image
from core.onebot_client import OneBotClient, OneBotError

# ---------- 常量 ----------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "forward_config.json"
USER_DB_FILE = PROJECT_ROOT / "users.db"
ACCESS_USER_URL = "http://szxy.cqtbi.edu.cn/oauth2/v1/access_user"


# ---------- 用户数据库 ----------
class UserDB:
    """SQLite 用户数据库，存储 QQ 号与 SSO 账号绑定关系。"""

    def __init__(self, db_path: Path = USER_DB_FILE) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    qq INTEGER PRIMARY KEY,
                    student_id TEXT NOT NULL,
                    realname TEXT DEFAULT '',
                    dept_id TEXT DEFAULT '',
                    dept_name TEXT DEFAULT '',
                    is_teacher INTEGER DEFAULT 0,
                    access_token TEXT DEFAULT '',
                    portal_ticket TEXT DEFAULT '',
                    expires_at INTEGER DEFAULT 0,
                    last_login TEXT DEFAULT '',
                    created_at TEXT DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS login_creds (
                    qq INTEGER PRIMARY KEY,
                    student_id TEXT NOT NULL,
                    password TEXT DEFAULT '',
                    created_at TEXT DEFAULT ''
                )
            """)
            conn.commit()
            conn.close()

    def upsert(self, qq: int, student_id: str, realname: str = "",
               dept_id: str = "", dept_name: str = "", is_teacher: bool = False,
               access_token: str = "", portal_ticket: str = "",
               expires_at: int = 0, last_login: str = "") -> None:
        with self._lock:
            conn = self._connect()
            now_iso = datetime.now().isoformat(timespec="seconds")
            conn.execute("""
                INSERT INTO users (qq, student_id, realname, dept_id, dept_name,
                                   is_teacher, access_token, portal_ticket,
                                   expires_at, last_login, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(qq) DO UPDATE SET
                    student_id=CASE WHEN excluded.student_id!='' THEN excluded.student_id ELSE users.student_id END,
                    realname=CASE WHEN excluded.realname!='' THEN excluded.realname ELSE users.realname END,
                    dept_id=CASE WHEN excluded.dept_id!='' THEN excluded.dept_id ELSE users.dept_id END,
                    dept_name=CASE WHEN excluded.dept_name!='' THEN excluded.dept_name ELSE users.dept_name END,
                    is_teacher=excluded.is_teacher,
                    access_token=CASE WHEN excluded.access_token!='' THEN excluded.access_token ELSE users.access_token END,
                    portal_ticket=CASE WHEN excluded.portal_ticket!='' THEN excluded.portal_ticket ELSE users.portal_ticket END,
                    expires_at=excluded.expires_at,
                    last_login=excluded.last_login
            """, (qq, student_id, realname, dept_id, dept_name,
                  1 if is_teacher else 0, access_token, portal_ticket,
                  expires_at, last_login, now_iso))
            conn.commit()
            conn.close()

    def get_by_qq(self, qq: int) -> dict | None:
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT * FROM users WHERE qq = ?", (qq,)).fetchone()
            conn.close()
            return dict(row) if row else None

    def get_all(self) -> list[dict]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute("SELECT * FROM users ORDER BY qq").fetchall()
            conn.close()
            return [dict(r) for r in rows]

    def upsert_creds(self, qq: int, student_id: str, password: str) -> None:
        """插入或更新登录凭据（QQ -> 学号/密码）。"""
        with self._lock:
            conn = self._connect()
            now_iso = datetime.now().isoformat(timespec="seconds")
            conn.execute("""
                INSERT INTO login_creds (qq, student_id, password, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(qq) DO UPDATE SET
                    student_id=excluded.student_id,
                    password=excluded.password
            """, (qq, student_id, password, now_iso))
            conn.commit()
            conn.close()
            log.info("登录凭据已保存: qq=%s student_id=%s", qq, student_id)

    def get_creds(self, qq: int) -> tuple[str, str] | None:
        """查询登录凭据，返回 (student_id, password) 或 None。"""
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT student_id, password FROM login_creds WHERE qq = ?",
                (qq,)
            ).fetchone()
            conn.close()
            if row:
                return (row[0], row[1])
            return None

# ---------- 日志 ----------
log = logging.getLogger("qq_forward")


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s | %(message)s", datefmt="%H:%M:%S"))
    root.addHandler(h)


# ---------- 配置 ----------
@dataclass
class ForwardConfig:
    ws_url: str = "ws://127.0.0.1:3001"
    access_token: str = ""
    source_groups: list[int] = None
    target_groups: list[int] = None
    admin_qq: int = 3602653998
    command_enabled: bool = True

    def __post_init__(self) -> None:
        if self.source_groups is None:
            self.source_groups = []
        if self.target_groups is None:
            self.target_groups = []

    def save(self) -> None:
        CONFIG_FILE.write_text(
            json.dumps(self._to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls) -> ForwardConfig:
        if not CONFIG_FILE.exists():
            return cls()
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            raw_src = data.get("source_groups") or data.get("source_group", 0)
            raw_tgt = data.get("target_groups") or data.get("target_group", 0)
            src_list = _ensure_list(raw_src)
            tgt_list = _ensure_list(raw_tgt)
            return cls(
                ws_url=data.get("ws_url", "ws://127.0.0.1:3001"),
                access_token=data.get("access_token", ""),
                source_groups=src_list,
                target_groups=tgt_list,
                admin_qq=int(data.get("admin_qq", 3602653998)),
                command_enabled=bool(data.get("command_enabled", True)),
            )
        except (OSError, json.JSONDecodeError, ValueError):
            return cls()

    def _to_dict(self) -> dict:
        return {
            "ws_url": self.ws_url,
            "access_token": self.access_token,
            "source_groups": self.source_groups,
            "target_groups": self.target_groups,
            "admin_qq": self.admin_qq,
            "command_enabled": self.command_enabled,
        }


def _ensure_list(val: int | list[int] | None) -> list[int]:
    """兼容旧配置：单个 int 转 list；空则返回空列表。"""
    if isinstance(val, list):
        return [v for v in val if v > 0]
    v = int(val) if val else 0
    return [v] if v > 0 else []


# ---------- 登录会话 ----------
class _LoginState:
    IDLE = "idle"
    WAIT_UCODE = "wait_ucode"
    WAIT_PASSWORD = "wait_password"
    CAPTCHA_PENDING = "captcha_pending"
    WAIT_CAPTCHA = "wait_captcha"
    LOGGING_IN = "logging_in"
    PASSWORD_UPDATE_CAPTCHA = "password_update_captcha"
    WAIT_APPLY_ACTIVITY_ID = "wait_apply_activity_id"
    WAIT_APPLY_CAPTCHA = "wait_apply_captcha"
    WAIT_SIGN_QR_IMAGE = "wait_sign_qr_image"


@dataclass
class _LoginSession:
    state: str = _LoginState.IDLE
    msg_type: str = ""
    group_id: int = 0
    user_id: int = 0
    ucode: str = ""
    password: str = ""
    jwgl_client: object | None = None
    last_activity: float = 0.0
    # #报名 流程字段
    activity_id: str = ""
    apply_data: dict = field(default_factory=dict)
    sc_session: object | None = None  # secondclass requests.Session


# ---------- 指令处理 ----------
class CommandHandler:
    """识别并执行 # 开头的管理员指令。#帮助 对所有人开放，其余仅管理员可用。"""

    HELP_CMD = "#帮助"
    UPDATE_SCHEDULE_CMD = "#更新模板课表"
    UPDATE_MY_SCHEDULE_CMD = "#更新课表"
    EXPORT_SCHEDULE_CMD = "#导出课表"
    WEEK_SCHEDULE_CMD = "#本周课表"
    TODAY_SCHEDULE_CMD = "#今日课表"
    TOMORROW_SCHEDULE_CMD = "#明天课表"
    LOGIN_CMD = "#登录"
    QR_LOGIN_CMD = "#扫码登录"
    CANCEL_CMD = "#取消"
    QUERY_USERS_CMD = "#查询用户"
    SECOND_CLASS_INFO_CMD = "#二课信息"
    SECOND_CLASS_CHART_CMD = "#二课图表"
    SECOND_CLASS_LIST_CMD = "#二课列表"
    MY_ER_CMD = "#我的二课"
    UPDATE_CMD = "#更新"
    UPDATE_DEBUG_CMD = "#更新调试"
    PASSWORD_UPDATE_CMD = "#密码更新"
    APPLY_CMD = "#报名"
    RESERVE_CMD = "#预约报名"
    MY_RESERVATIONS_CMD = "#我的预约"
    SIGN_IN_CMD = "#签到"
    SIGN_OUT_CMD = "#签退"
    SCAN_SIGN_CMD = "#扫码签到"
    LOGIN_SESSION_TIMEOUT = 120  # 秒

    def __init__(self, config: ForwardConfig, client: OneBotClient,
                 log_callback: Callable[[str, str], None]) -> None:
        self.config = config
        self._client = client
        self._log = log_callback
        self._sessions: dict[tuple[int, str, int], _LoginSession] = {}
        self._session_lock = threading.Lock()
        # 启动会话过期守护线程
        threading.Thread(target=self._session_watchdog, daemon=True).start()

    # ---- 会话管理 ----

    def _get_session(self, key: tuple[int, str, int]) -> _LoginSession | None:
        with self._session_lock:
            return self._sessions.get(key)

    def _set_session(self, key: tuple[int, str, int], session: _LoginSession) -> None:
        with self._session_lock:
            self._sessions[key] = session

    def _remove_session(self, key: tuple[int, str, int]) -> None:
        with self._session_lock:
            self._sessions.pop(key, None)

    def _session_watchdog(self) -> None:
        """定期清理超时的登录会话。"""
        while True:
            threading.Event().wait(30)
            now = time.time()
            expired_keys = []
            with self._session_lock:
                for key, session in self._sessions.items():
                    if session.state != _LoginState.IDLE and (now - session.last_activity) > self.LOGIN_SESSION_TIMEOUT:
                        expired_keys.append(key)
            for key in expired_keys:
                session = self._get_session(key)
                if session:
                    self._log("info", f"登录会话超时: user={session.user_id}")
                    self._reply(session.msg_type, session.group_id, session.user_id, "登录会话已超时，请重新 #登录")
                    self._remove_session(key)

    # ---- 指令入口 ----

    def _run_async(self, target: Callable, *args: object) -> None:
        """以守护线程跑后台任务（统一封装，避免到处写 threading.Thread）。"""
        threading.Thread(target=target, args=args, daemon=True).start()

    def _hit_log(self, text: str, user_id: int, group_id: int) -> None:
        """统一打 “指令命中” 日志。"""
        self._log("info", f"指令命中: {text} from={user_id} group={group_id}")

    def try_handle(self, event: dict) -> bool:
        """尝试处理指令。命中返回 True，调用方应跳过转发。"""
        if not self.config.command_enabled:
            return False
        if event.get("post_type") != "message":
            return False

        msg_type = event.get("message_type")
        if msg_type not in ("group", "private"):
            return False

        text = (event.get("raw_message") or "").strip()
        user_id = event.get("user_id", 0)
        group_id = event.get("group_id", 0) if msg_type == "group" else 0
        session_key = (user_id, msg_type, group_id)

        # 登录会话拦截：活跃会话中的非 # 消息由会话处理
        session = self._get_session(session_key)
        if session and session.state != _LoginState.IDLE:
            if time.time() - session.last_activity > self.LOGIN_SESSION_TIMEOUT:
                self._expire_session(session_key, session)
            elif not text.startswith("#"):
                self._handle_session_input(session, text, msg_type, group_id, user_id, event)
                return True
            # # 开头的消息走正常指令分发，会话保持

        # 必须以 # 开头
        if not text.startswith("#"):
            return False

        # ===== 帮助 =====
        if text == self.HELP_CMD:
            self._hit_log(text, user_id, group_id)
            is_admin = user_id == self.config.admin_qq
            try:
                img_path = get_help_image(is_admin=is_admin)
                self._reply_image(msg_type, group_id, user_id, img_path.read_bytes())
            except FileNotFoundError as e:
                self._log("error", f"帮助图片加载失败: {e}")
                self._reply(msg_type, group_id, user_id, "帮助图片加载失败，请查看日志")
            return True

        # ===== 登录类 =====
        if text == self.LOGIN_CMD:
            self._start_login_session(msg_type, group_id, user_id)
            return True
        if text == self.QR_LOGIN_CMD:
            self._hit_log(text, user_id, group_id)
            self._reply(msg_type, group_id, user_id, "正在获取登录二维码…")
            self._run_async(self._cmd_qr_login, msg_type, group_id, user_id)
            return True
        if text == self.CANCEL_CMD:
            session = self._get_session(session_key)
            if session and session.state != _LoginState.IDLE:
                self._cancel_session(session_key, msg_type, group_id, user_id)
                return True
            return False

        # ===== 课表类 =====
        if text == self.UPDATE_SCHEDULE_CMD:
            self._hit_log(text, user_id, group_id)
            self._cmd_send_template_schedule(msg_type, group_id, user_id)
            return True
        if text == self.UPDATE_MY_SCHEDULE_CMD:
            self._hit_log(text, user_id, group_id)
            self._reply(msg_type, group_id, user_id, "正在拉取课表…")
            self._run_async(self._cmd_update_my_schedule, msg_type, group_id, user_id)
            return True
        if text == self.EXPORT_SCHEDULE_CMD:
            self._hit_log(text, user_id, group_id)
            self._run_async(self._cmd_export_schedule, msg_type, group_id, user_id)
            return True
        if text == self.WEEK_SCHEDULE_CMD:
            self._hit_log(text, user_id, group_id)
            self._run_async(self._cmd_week_schedule, msg_type, group_id, user_id, None)
            return True
        if text == self.TODAY_SCHEDULE_CMD:
            self._hit_log(text, user_id, group_id)
            self._run_async(self._cmd_day_schedule, msg_type, group_id, user_id, 0)
            return True
        if text == self.TOMORROW_SCHEDULE_CMD:
            self._hit_log(text, user_id, group_id)
            self._run_async(self._cmd_day_schedule, msg_type, group_id, user_id, 1)
            return True
        m_week = re.match(r"#第(\d+)周课表", text)
        if m_week:
            week_num = int(m_week.group(1))
            self._hit_log(f"#第{week_num}周课表", user_id, group_id)
            self._run_async(self._cmd_week_schedule, msg_type, group_id, user_id, week_num)
            return True

        # ===== 凭证更新类 =====
        if text == self.UPDATE_CMD:
            self._hit_log(text, user_id, group_id)
            self._run_async(self._cmd_update, msg_type, group_id, user_id)
            return True
        if text == self.PASSWORD_UPDATE_CMD:
            self._hit_log(text, user_id, group_id)
            self._run_async(self._cmd_password_update, msg_type, group_id, user_id)
            return True

        # ===== 二课类 =====
        if text == self.SECOND_CLASS_INFO_CMD:
            self._hit_log(text, user_id, group_id)
            self._reply(msg_type, group_id, user_id, "正在查询第二课堂信息…")
            self._run_async(self._cmd_secondclass_info, msg_type, group_id, user_id)
            return True
        if text == self.SECOND_CLASS_CHART_CMD:
            self._hit_log(text, user_id, group_id)
            self._reply(msg_type, group_id, user_id, "正在生成第二课堂信息图表…")
            self._run_async(self._cmd_secondclass_chart, msg_type, group_id, user_id)
            return True
        # ===== 报名类 =====
        if text == self.APPLY_CMD or text.startswith(self.APPLY_CMD + " "):
            m_apply = re.match(r"^#报名\s+(\d{4,})\s*$", text)
            if m_apply:
                activity_id = m_apply.group(1)
                self._hit_log(f"#报名 activity_id={activity_id}", user_id, group_id)
                self._run_async(self._cmd_apply, msg_type, group_id, user_id, activity_id)
                return True
            if text == self.APPLY_CMD:
                # 未带 ID → 进入等待活动 ID 的对话状态
                self._hit_log("#报名（等待活动 ID）", user_id, group_id)
                self._set_session(session_key, _LoginSession(
                    state=_LoginState.WAIT_APPLY_ACTIVITY_ID,
                    msg_type=msg_type, group_id=group_id, user_id=user_id,
                    last_activity=time.time(),
                ))
                self._reply(msg_type, group_id, user_id,
                            "请输入要报名的活动 ID（4 位以上数字），发送 #取消 可取消。")
                return True
            self._reply(msg_type, group_id, user_id,
                        "格式：#报名 <活动ID>\n例：#报名 121499")
            return True

        # ---- #预约报名 <活动ID> ----
        if text == self.RESERVE_CMD or text.startswith(self.RESERVE_CMD + " "):
            m_resv = re.match(r"^#预约报名\s+(\d{4,})\s*$", text)
            if m_resv:
                activity_id = m_resv.group(1)
                self._hit_log(f"#预约报名 activity_id={activity_id}", user_id, group_id)
                self._run_async(self._cmd_reserve, msg_type, group_id, user_id, activity_id)
                return True
            self._reply(msg_type, group_id, user_id,
                        "格式：#预约报名 <活动ID>\n例：#预约报名 121499\n"
                        "（仅可预约状态为「报名未开始」的活动）")
            return True

        # ---- #我的预约 ----
        if text == self.MY_RESERVATIONS_CMD:
            self._hit_log(text, user_id, group_id)
            self._run_async(self._cmd_my_reservations, msg_type, group_id, user_id)
            return True

        # ---- #签到 / #签退 <活动ID> [渠道] ----
        for cmd_text, sign_out in (
            (self.SIGN_IN_CMD, False), (self.SIGN_OUT_CMD, True),
        ):
            if text == cmd_text or text.startswith(cmd_text + " "):
                m = re.match(rf"^{cmd_text}\s+(\d{{4,}})(?:\s+(\d+))?\s*$", text)
                if m:
                    activity_id = m.group(1)
                    channel_id = int(m.group(2)) if m.group(2) else 5
                    self._hit_log(
                        f"{cmd_text} aid={activity_id} ch={channel_id}",
                        user_id, group_id,
                    )
                    self._run_async(
                        self._cmd_sign, msg_type, group_id, user_id,
                        activity_id, sign_out, channel_id,
                    )
                    return True
                self._reply(msg_type, group_id, user_id,
                            f"格式：{cmd_text} <活动ID> [渠道]\n"
                            f"例：{cmd_text} 121582  （默认 渠道=5）")
                return True

        # ---- #扫码签到 ----
        if text == self.SCAN_SIGN_CMD:
            self._hit_log(text, user_id, group_id)
            self._set_session(session_key, _LoginSession(
                state=_LoginState.WAIT_SIGN_QR_IMAGE,
                msg_type=msg_type, group_id=group_id, user_id=user_id,
                last_activity=time.time(),
            ))
            self._reply(msg_type, group_id, user_id,
                        "请发送签到二维码图片（在大屏上拍一下二维码发过来）")
            return True

        # ===== 活动详情（#<activity_id>） =====
        m_id = re.match(r"#(\d{4,})$", text)
        if m_id:
            activity_id = m_id.group(1)
            self._hit_log(f"#二课详情 activity_id={activity_id}", user_id, group_id)
            self._reply(msg_type, group_id, user_id, f"正在查询活动 {activity_id} 详情…")
            self._run_async(self._cmd_activity_detail, msg_type, group_id, user_id, activity_id)
            return True

        # ===== 管理员专用 =====
        if user_id != self.config.admin_qq:
            # 管理员指令需先过权限判断；非管理员到此即未命中
            if text == self.UPDATE_DEBUG_CMD:
                self._reply(msg_type, group_id, user_id, "无权执行此指令")
                return True
            if text == self.SECOND_CLASS_LIST_CMD:
                self._reply(msg_type, group_id, user_id, "无权执行此指令")
                return True
            if text == self.MY_ER_CMD:
                self._reply(msg_type, group_id, user_id, "无权执行此指令")
                return True
            return False
        if text == self.UPDATE_DEBUG_CMD:
            self._hit_log(text, user_id, group_id)
            self._reply(msg_type, group_id, user_id, "正在尝试更新课表…")
            self._run_async(self._cmd_update_debug, msg_type, group_id, user_id)
            return True
        if text == self.SECOND_CLASS_LIST_CMD:
            self._hit_log(text, user_id, group_id)
            self._reply(msg_type, group_id, user_id, "正在渲染二课活动列表…")
            self._run_async(self._cmd_secondclass_list, msg_type, group_id, user_id)
            return True
        if text == self.MY_ER_CMD:
            self._hit_log(text, user_id, group_id)
            self._reply(msg_type, group_id, user_id, "正在查询您的未结束活动…")
            self._run_async(self._cmd_my_er, msg_type, group_id, user_id)
            return True
        if text == self.QUERY_USERS_CMD:
            self._hit_log(text, user_id, group_id)
            self._reply(msg_type, group_id, user_id, "正在导出用户表…")
            self._run_async(self._cmd_query_users, msg_type, group_id, user_id)
            return True

        return False

    # ---- 帮助 ----

    @staticmethod
    def _help_text() -> str:
        return (
            "支持的 #指令：\n"
            "#帮助 — 查看所有指令（所有人可用）\n"
            "#扫码登录 — 扫码安全登录（所有人可用，推荐）\n"
            "#登录 — 账号密码登录（所有人可用）\n"
            "#更新 — 更新登录凭证（所有人可用，需先扫码登录）\n"
            "#更新课表 — 拉取并更新个人课表，自动渲染本周课表图片（所有人，需先登录）\n"
            "#导出课表 — 导出个人课表为Excel文件（所有人，需先更新课表）\n"
            "#本周课表 — 查看本周课表图片（需先更新课表）\n"
            "#今日课表 — 查看今日课表卡片（需先更新课表）\n"
            "#明天课表 — 查看明天课表卡片（需先更新课表）\n"
            "#第N周课表 — 查看指定周次课表图片（如 #第17周课表）\n"
            "#二课信息 — 查询第二课堂活动与积分（所有人可用，需先登录）\n"
            "#二课图表 — 生成第二课堂信息图表（所有人可用，需先登录）\n"
            "#我的二课 — 查询您的未结束活动卡片（报名中/活动中/未开始，所有人，需先登录）\n"
            "#报名 [活动ID] — 二课活动报名，不带 ID 时 bot 会询问（所有人，需先登录）\n"
            "#预约报名 <活动ID> — 预约「报名未开始」的活动，到点 @ 提醒你（不会自动报名）\n"
            "#我的预约 — 查看当前所有预约报名（所有人）\n"
            "#密码更新 — 用保存的密码重新登录并更新课表（所有人，需先密码登录过）\n"
            "#更新模板课表 — 获取模板课表（所有人可用）\n"
            "#查询用户 — 导出用户表（仅管理员）\n"
            "#取消 — 取消登录流程"
        )

    # ---- 回复与文件发送 ----

    def _send(self, msg_type: str, group_id: int, user_id: int,
              message: list[dict] | str, *, forward: bool = False) -> None:
        """统一封装群/私聊与普通/合并转发四种发送场景。失败仅记日志。"""
        try:
            if forward:
                if msg_type == "group":
                    self._client.send_group_forward_msg(group_id, message)
                else:
                    self._client.send_private_forward_msg(user_id, message)
            else:
                if msg_type == "group":
                    self._client.send_group_msg(group_id, message)
                else:
                    self._client.send_private_msg(user_id, message)
        except Exception as e:
            self._log("error", f"消息发送失败 (msg_type={msg_type}, forward={forward}): {e}")

    def _reply(self, msg_type: str, group_id: int, user_id: int, text: str) -> None:
        """发送纯文本回复。"""
        self._send(msg_type, group_id, user_id, text)

    def _reply_image(self, msg_type: str, group_id: int, user_id: int,
                     img_bytes: bytes, caption: str = "") -> None:
        """发送 base64 图片 + 可选文字。"""
        img_b64 = base64.b64encode(img_bytes).decode("ascii")
        message: list[dict] = [{"type": "image", "data": {"file": f"base64://{img_b64}"}}]
        if caption:
            message.append({"type": "text", "data": {"text": caption}})
        self._send(msg_type, group_id, user_id, message)

    def _send_log_as_forward(self, msg_type: str, group_id: int,
                             user_id: int, title: str, log_text: str) -> None:
        """将日志文本以合并转发形式发送给用户。"""
        nodes = [{
            "type": "node",
            "data": {
                "name": "系统日志",
                "uin": str(self.config.admin_qq),
                "content": [
                    {"type": "text", "data": {"text": f"{title}\n\n{log_text}"}},
                ],
            },
        }]
        self._send(msg_type, group_id, user_id, nodes, forward=True)

    def _forward_files(self, msg_type: str, group_id: int, user_id: int,
                       files: list[Path], sender_name: str) -> None:
        """以合并转发形式发送多个本地文件给触发方。"""
        nodes: list[dict] = []
        for fp in files:
            if not fp.exists():
                continue
            nodes.append({
                "type": "node",
                "data": {
                    "name": sender_name,
                    "uin": str(self.config.admin_qq),
                    "content": [
                        {"type": "file",
                         "data": {"file": fp.resolve().as_uri(), "name": fp.name}},
                    ],
                },
            })
        if not nodes:
            return
        self._send(msg_type, group_id, user_id, nodes, forward=True)
        self._log("info", f"合并转发课表文件成功: {len(nodes)} 个")

    def _forward_error(self, msg_type: str, group_id: int, user_id: int,
                       error_text: str) -> None:
        """以合并转发形式发送错误日志。"""
        nodes = [{
            "type": "node",
            "data": {
                "name": "登录错误",
                "uin": str(self.config.admin_qq),
                "content": [{"type": "text", "data": {"text": error_text}}],
            },
        }]
        self._send(msg_type, group_id, user_id, nodes, forward=True)

    # ---- 登录会话 ----

    def _start_login_session(self, msg_type: str, group_id: int, user_id: int) -> None:
        session_key = (user_id, msg_type, group_id)
        old = self._get_session(session_key)
        if old and old.state != _LoginState.IDLE:
            self._reply(msg_type, group_id, user_id, "已有登录流程进行中，发送 #取消 可终止")
            return

        self._log("info", f"#登录 开始: user={user_id} group={group_id}")
        session = _LoginSession(
            state=_LoginState.WAIT_UCODE,
            msg_type=msg_type,
            group_id=group_id,
            user_id=user_id,
            last_activity=time.time(),
        )
        self._set_session(session_key, session)
        self._reply(msg_type, group_id, user_id,
                     "即将开始登录流程。注意：学号和密码将以明文出现在聊天记录中。\n"
                     "推荐使用 #扫码登录 更安全方便！\n"
                     "发送 #取消 可随时取消。\n\n请发送学号")

    def _handle_session_input(self, session: _LoginSession, text: str,
                              msg_type: str, group_id: int, user_id: int,
                              event: dict) -> None:
        session.last_activity = time.time()
        state = session.state

        if state == _LoginState.WAIT_UCODE:
            session.ucode = text.strip()
            session.state = _LoginState.WAIT_PASSWORD
            self._set_session((user_id, msg_type, group_id), session)
            self._reply(msg_type, group_id, user_id, "请发送密码")

        elif state == _LoginState.WAIT_PASSWORD:
            session.password = text.strip()
            session.state = _LoginState.CAPTCHA_PENDING
            self._set_session((user_id, msg_type, group_id), session)
            # 尝试撤回密码消息
            message_id = event.get("message_id")
            if message_id:
                try:
                    self._client.delete_msg(message_id)
                    self._log("info", "已撤回密码消息")
                except Exception:
                    self._log("info", "撤回密码消息失败（可能权限不足）")
            self._reply(msg_type, group_id, user_id, "正在获取验证码…")
            threading.Thread(
                target=self._bg_begin_sso,
                args=(user_id, msg_type, group_id),
                daemon=True,
            ).start()

        elif state == _LoginState.CAPTCHA_PENDING:
            self._reply(msg_type, group_id, user_id, "验证码加载中，请稍候…")

        elif state == _LoginState.WAIT_CAPTCHA:
            rcode = text.strip()
            session.state = _LoginState.LOGGING_IN
            self._set_session((user_id, msg_type, group_id), session)
            self._reply(msg_type, group_id, user_id, "正在登录…")
            threading.Thread(
                target=self._bg_sso_login,
                args=(user_id, msg_type, group_id, rcode),
                daemon=True,
            ).start()

        # elif state == _LoginState.LOGGING_IN:
        #     self._reply(msg_type, group_id, user_id, "登录中，请等待…")

        elif state == _LoginState.PASSWORD_UPDATE_CAPTCHA:
            rcode = text.strip()
            if not rcode:
                self._reply(msg_type, group_id, user_id, "验证码不能为空")
                return
            threading.Thread(
                target=self._bg_password_update_do_login,
                args=(user_id, msg_type, group_id, rcode),
                daemon=True,
            ).start()

        # #报名 流程：等待用户输入活动 ID
        elif state == _LoginState.WAIT_APPLY_ACTIVITY_ID:
            activity_id = text.strip()
            if not re.match(r"^\d{4,}$", activity_id):
                self._reply(msg_type, group_id, user_id,
                            "活动 ID 格式不正确，请输入 4 位以上数字，或发送 #取消")
                return
            # 清除等待状态，转入 _cmd_apply 由其自行管理新会话
            self._remove_session((user_id, msg_type, group_id))
            threading.Thread(
                target=self._cmd_apply,
                args=(msg_type, group_id, user_id, activity_id),
                daemon=True,
            ).start()

        # #扫码签到 流程：等待用户发送二维码图片
        elif state == _LoginState.WAIT_SIGN_QR_IMAGE:
            from qq.image_intake import extract_image_urls
            urls = extract_image_urls(event)
            if not urls:
                self._reply(msg_type, group_id, user_id,
                            "未检测到图片，请直接发送二维码图片（或发送 #取消）")
                return
            self._remove_session((user_id, msg_type, group_id))
            threading.Thread(
                target=self._cmd_scan_sign,
                args=(msg_type, group_id, user_id, urls[0]),
                daemon=True,
            ).start()

        # #报名 流程：等待用户输入报名验证码
        elif state == _LoginState.WAIT_APPLY_CAPTCHA:
            rcode = text.strip()
            if not rcode:
                self._reply(msg_type, group_id, user_id, "验证码不能为空")
                return
            threading.Thread(
                target=self._bg_apply_submit,
                args=(user_id, msg_type, group_id, rcode),
                daemon=True,
            ).start()

    def _expire_session(self, key: tuple[int, str, int], session: _LoginSession) -> None:
        self._log("info", f"登录会话超时: user={session.user_id}")
        self._reply(session.msg_type, session.group_id, session.user_id, "登录会话已超时，请重新 #登录")
        self._remove_session(key)

    def _cancel_session(self, key: tuple[int, str, int],
                        msg_type: str, group_id: int, user_id: int) -> None:
        self._log("info", f"登录会话取消: user={user_id}")
        self._reply(msg_type, group_id, user_id, "已取消登录流程")
        self._remove_session(key)

    # ---- 用户信息 ----

    @staticmethod
    def _fetch_user_info(access_token: str) -> dict | None:
        """调用 access_user 接口获取用户信息。"""
        if not access_token:
            return None
        try:
            import requests
            r = requests.post(ACCESS_USER_URL, data={"access_token": access_token}, timeout=10)
            r.raise_for_status()
            data = r.json()
            if data.get("success"):
                return data
            log.warning("access_user 返回失败: %s", data)
        except Exception as e:
            log.warning("获取用户信息失败: %s", e)
        return None

    # ---- 后台任务 ----

    def _bg_begin_sso(self, user_id: int, msg_type: str, group_id: int) -> None:
        """后台：获取 accKey + 验证码图片，发送给用户。"""
        session_key = (user_id, msg_type, group_id)
        try:
            from schedule import JWGLClient
            client = JWGLClient()
            captcha_bytes = client.begin_sso()

            session = self._get_session(session_key)
            if not session or session.state != _LoginState.CAPTCHA_PENDING:
                return
            session.jwgl_client = client
            session.state = _LoginState.WAIT_CAPTCHA
            session.last_activity = time.time()
            self._set_session(session_key, session)

            self._reply_image(msg_type, group_id, user_id, captcha_bytes, "\n请输入验证码")
        except Exception as e:
            self._log("error", f"获取验证码失败: {e}")
            self._reply(msg_type, group_id, user_id, f"获取验证码失败: {e}")
            self._remove_session(session_key)

    def _bg_sso_login(self, user_id: int, msg_type: str, group_id: int, rcode: str) -> None:
        """后台：执行 SSO 登录，成功后自动更新课表。"""
        session_key = (user_id, msg_type, group_id)
        try:
            session = self._get_session(session_key)
            if not session or not session.jwgl_client:
                raise RuntimeError("登录会话已丢失")

            jwgl = session.jwgl_client
            result = jwgl.sso_login(session.ucode, session.password, rcode)

            # 保存账号到 accounts.json
            from sso.sso_common import load_data, save_data
            data = load_data()
            accounts = data.setdefault("accounts", [])
            portal_ticket = jwgl.portal_ticket or ""
            access_token = jwgl.access_token or ""
            expires_in = int(result.get("expires_in") or 0)
            now_iso = datetime.now().isoformat(timespec="seconds")
            expires_at = int(datetime.now().timestamp()) + expires_in if expires_in else 0
            found = False
            for a in accounts:
                if a.get("student_id") == session.ucode:
                    a["qq"] = user_id
                    a["password"] = session.password
                    a["access_token"] = access_token
                    a["portal_ticket"] = portal_ticket
                    a["expires_at"] = expires_at
                    a["last_login"] = now_iso
                    found = True
                    break
            if not found:
                accounts.append({
                    "qq": user_id,
                    "student_id": session.ucode,
                    "password": session.password,
                    "access_token": access_token,
                    "portal_ticket": portal_ticket,
                    "expires_at": expires_at,
                    "last_login": now_iso,
                })
            save_data(data)
            self._log("info", f"SSO 登录成功: ucode={session.ucode} ticket={portal_ticket[:8]}…")

            # 保存登录凭据到 login_creds（供 #更新 自动重登使用）
            UserDB().upsert_creds(user_id, session.ucode, session.password)

            # 获取用户信息并存入数据库
            user_info = self._fetch_user_info(access_token)
            if user_info:
                UserDB().upsert(
                    qq=user_id,
                    student_id=user_info.get("userloginid", session.ucode),
                    realname=user_info.get("userrealname", ""),
                    dept_id=user_info.get("userdeptid", ""),
                    dept_name=user_info.get("userdepaname", ""),
                    is_teacher=user_info.get("teacher") == "1",
                    access_token=access_token,
                    portal_ticket=portal_ticket,
                    expires_at=expires_at,
                    last_login=now_iso,
                )
                self._log("info", f"用户信息已存库: qq={user_id} name={user_info.get('userrealname', '?')}")
            else:
                # 获取用户信息失败，仍用学号存库
                UserDB().upsert(
                    qq=user_id,
                    student_id=session.ucode,
                    access_token=access_token,
                    portal_ticket=portal_ticket,
                    expires_at=expires_at,
                    last_login=now_iso,
                )
                self._log("info", f"用户信息存库（无详细）: qq={user_id}")

            self._reply(msg_type, group_id, user_id, "登录成功，正在拉取课表…")

            # 自动更新课表
            self._bg_update_after_login(jwgl, session.ucode, msg_type, group_id, user_id)

        except Exception as e:
            self._log("error", f"SSO 登录失败: {e}")
            self._forward_error(msg_type, group_id, user_id, f"登录失败: {e}")
            self._reply(msg_type, group_id, user_id, "登录失败，请重新 #登录")
            self._remove_session(session_key)

    def _bg_update_after_login(self, jwgl: object, student_id: str,
                               msg_type: str, group_id: int, user_id: int) -> None:
        """登录成功后自动拉取课表。"""
        session_key = (user_id, msg_type, group_id)
        try:
            from schedule import ScheduleError, _schedule_path

            jwgl.bridge()
            schedule = jwgl.get_schedule(student_id=student_id)

            if not schedule.courses:
                raise RuntimeError("课表为空")

            json_path = _schedule_path("json", student_id=student_id, semester=schedule.semester)
            schedule.save_json(json_path)

            self._log("info", f"课表拉取成功: {len(schedule.courses)} 门课 → {json_path.name}")
            self._reply(msg_type, group_id, user_id, f"登录并拉取课表成功，共 {len(schedule.courses)} 门课")

            # 自动渲染本周课表
            self._cmd_week_schedule(msg_type, group_id, user_id, None)

            # 自动更新二课信息
            self._cmd_secondclass_info(msg_type, group_id, user_id)
        except Exception as e:
            self._log("error", f"课表拉取失败: {e}")
            self._reply(msg_type, group_id, user_id,
                        f"登录成功但课表拉取失败: {e}")
        finally:
            self._remove_session(session_key)

    # ---- #更新模板课表 ----

    def _cmd_send_template_schedule(self, msg_type: str, group_id: int, user_id: int) -> None:
        """更新 2403740(QQ:3200418862) 课表并发送模板课表 Excel。"""
        TEMPLATE_QQ = 3200418862
        TEMPLATE_STUDENT_ID = "2403740"
        try:
            # 1. 查找模板账号（与 #更新课表 同样方式）
            user = UserDB().get_by_qq(TEMPLATE_QQ)
            if not user:
                from core.account_store import find_account_by_qq
                acc = find_account_by_qq(TEMPLATE_QQ)
                if not acc:
                    self._reply(msg_type, group_id, user_id,
                                "模板账号(QQ:3200418862)未绑定，请先登录")
                    return
                user = {
                    "qq": TEMPLATE_QQ,
                    "student_id": acc.get("student_id", ""),
                    "portal_ticket": acc.get("portal_ticket", ""),
                    "access_token": acc.get("access_token", ""),
                    "expires_at": acc.get("expires_at", 0),
                }

            portal_ticket = user.get("portal_ticket", "")
            access_token = user.get("access_token", "")
            student_id = user.get("student_id", "")
            expires_at = int(user.get("expires_at") or 0)

            if expires_at and expires_at < int(time.time()):
                self._reply(msg_type, group_id, user_id, "模板账号登录已过期，请重新 #扫码登录")
                return

            if not portal_ticket and access_token:
                from secondclass.secondclass_tool import _obtain_portal_ticket
                portal_ticket = _obtain_portal_ticket(access_token)
                if portal_ticket:
                    UserDB().upsert(qq=TEMPLATE_QQ, portal_ticket=portal_ticket)

            if not portal_ticket:
                self._reply(msg_type, group_id, user_id, "模板账号缺少门户票据，请重新 #扫码登录")
                return

            self._reply(msg_type, group_id, user_id, "正在更新模板课表(2403740)…")

            # 2. 桥接 JWGL 获取课表（与 #更新课表 相同逻辑）
            from schedule import JWGLClient, _schedule_path

            client = JWGLClient()
            client.portal_ticket = portal_ticket
            client.bridge()
            schedule = client.get_schedule(student_id=TEMPLATE_STUDENT_ID)

            if not schedule or not schedule.courses:
                raise RuntimeError("课表为空，可能 ticket 已失效")

            # 3. 保存 JSON + 导出 Excel 并发送
            json_path = _schedule_path("json", student_id=TEMPLATE_STUDENT_ID,
                                       semester=schedule.semester)
            schedule.save_json(json_path)
            xlsx_path = _schedule_path("xlsx", student_id=TEMPLATE_STUDENT_ID,
                                       semester=schedule.semester)
            schedule.save_excel(xlsx_path)

            self._log("info", "模板课表更新成功: %s 门课 → %s",
                      len(schedule.courses), xlsx_path.name)
            self._forward_files(msg_type, group_id, user_id,
                                [xlsx_path], "模板课表(2403740)")
        except Exception as e:
            self._log("error", f"发送模板课表失败: {e}")
            self._reply(msg_type, group_id, user_id, f"发送模板课表失败: {e}")

    # ---- #密码更新（用密码重登刷新凭证）----

    def _cmd_password_update(self, msg_type: str, group_id: int, user_id: int) -> None:
        """#密码更新：用保存的学号+密码重新 SSO 登录，仅更新 token/ticket。"""
        from schedule import JWGLClient
        try:
            # 1. 查用户
            user = UserDB().get_by_qq(user_id)
            if not user:
                from core.account_store import find_account_by_qq
                acc = find_account_by_qq(user_id)
                if not acc:
                    self._reply(msg_type, group_id, user_id,
                                "未找到您的账号信息，请先使用 #登录 或 #扫码登录")
                    return
                student_id = acc.get("student_id", "")
            else:
                student_id = user.get("student_id", "")

            if not student_id:
                self._reply(msg_type, group_id, user_id, "未关联学号，请先使用 #登录")
                return

            # 2. 查密码
            creds = UserDB().get_creds(user_id)
            if not creds:
                self._reply(msg_type, group_id, user_id,
                            "未找到您的密码，请先使用 #登录 登录一次")
                return
            ucode, pwd = creds

            # 3. 获取验证码
            client = JWGLClient()
            captcha_bytes = client.begin_sso()
            session_key = (user_id, msg_type, group_id)
            session = _LoginSession(
                state=_LoginState.PASSWORD_UPDATE_CAPTCHA,
                msg_type=msg_type, group_id=group_id, user_id=user_id,
                ucode=ucode, password=pwd, jwgl_client=client,
                last_activity=time.time(),
            )
            self._set_session(session_key, session)
            self._reply_image(msg_type, group_id, user_id, captcha_bytes,
                              f"\n正在为 {student_id} 重新登录，请输入验证码")

        except Exception as e:
            self._log("error", f"#密码更新 异常: {e}")
            self._reply(msg_type, group_id, user_id, f"密码更新失败: {e}")

    def _bg_password_update_do_login(self, user_id: int, msg_type: str,
                                     group_id: int, rcode: str) -> None:
        """#密码更新 验证码提交后：SSO 重登 → 保存 token/ticket → 回复。"""
        from schedule import ScheduleError
        session_key = (user_id, msg_type, group_id)
        try:
            session = self._get_session(session_key)
            if not session or not session.jwgl_client:
                raise RuntimeError("登录会话已丢失")

            client = session.jwgl_client
            result = client.sso_login(session.ucode, session.password, rcode)
            portal_ticket = client.portal_ticket
            access_token = client.access_token

            # 保存到 accounts.json
            from sso.sso_common import load_data, save_data
            data = load_data()
            accounts = data.setdefault("accounts", [])
            from datetime import datetime
            now_iso = datetime.now().isoformat(timespec="seconds")
            expires_in = int(result.get("expires_in") or 0)
            expires_at = int(datetime.now().timestamp()) + expires_in if expires_in else 0
            found = False
            for a in accounts:
                if a.get("student_id") == session.ucode:
                    a["access_token"] = access_token
                    a["portal_ticket"] = portal_ticket
                    a["expires_at"] = expires_at
                    a["last_login"] = now_iso
                    found = True
                    break
            if not found:
                accounts.append({
                    "qq": user_id,
                    "student_id": session.ucode,
                    "access_token": access_token,
                    "portal_ticket": portal_ticket,
                    "expires_at": expires_at,
                    "last_login": now_iso,
                })
            save_data(data)

            # 同步到 users.db
            UserDB().upsert(
                qq=user_id,
                student_id=session.ucode,
                access_token=access_token,
                portal_ticket=portal_ticket,
                expires_at=expires_at,
                last_login=now_iso,
            )

            self._reply(msg_type, group_id, user_id,
                        f"更新成功！学号：{session.ucode}")
            self._log("info", f"#密码更新 成功: qq={user_id} sid={session.ucode}")

        except ScheduleError as e:
            self._log("error", f"#密码更新 登录失败: {e}")
            self._reply(msg_type, group_id, user_id,
                        f"登录失败: {e}，请重新 #登录")
        except Exception as e:
            self._log("error", f"#密码更新 异常: {e}")
            self._reply(msg_type, group_id, user_id,
                        f"密码更新失败: {e}，请重新 #登录")
        finally:
            self._remove_session(session_key)

    # ---- #更新（验证过期间+状态消息+静默更新）----

    def _cmd_update(self, msg_type: str, group_id: int, user_id: int) -> None:
        """#更新：验证 token 时效 → 发送状态消息 → 静默更新课表+二课。"""
        from datetime import datetime
        from schedule import ScheduleError, _schedule_path

        try:
            # 1. 查用户
            user = UserDB().get_by_qq(user_id)
            if not user:
                from core.account_store import find_account_by_qq
                acc = find_account_by_qq(user_id)
                if not acc:
                    self._reply(msg_type, group_id, user_id,
                                "未找到您的账号信息，请先使用 #登录 或 #扫码登录")
                    return
                user = {
                    "qq": user_id,
                    "student_id": acc.get("student_id", ""),
                    "portal_ticket": acc.get("portal_ticket", ""),
                    "access_token": acc.get("access_token", ""),
                    "expires_at": acc.get("expires_at", 0),
                    "last_login": acc.get("last_login", ""),
                }

            student_id = user.get("student_id", "")
            access_token = user.get("access_token", "")
            portal_ticket = user.get("portal_ticket", "")
            last_login = user.get("last_login", "")
            expires_at = int(user.get("expires_at") or 0)

            if not student_id:
                self._reply(msg_type, group_id, user_id, "未关联学号，请先使用 #登录")
                return

            # 2. 格式化 token 更新时间
            login_time = last_login
            if last_login:
                try:
                    dt = datetime.fromisoformat(last_login)
                    login_time = dt.strftime("%Y-%m-%d %H:%M")
                except (ValueError, OSError):
                    pass
            elif expires_at:
                try:
                    dt = datetime.fromtimestamp(expires_at)
                    login_time = dt.strftime("%Y-%m-%d %H:%M")
                except (ValueError, OSError):
                    pass
            else:
                login_time = "未知"

            self._reply(msg_type, group_id, user_id,
                        f"学号：{student_id}  token更新时间：{login_time}")

            # 3. 检查是否过期
            now_ts = int(time.time())
            if expires_at and expires_at < now_ts:
                self._log("info", f"#更新 token 已过期，静默跳过: qq={user_id}")
                return
            if not access_token and not portal_ticket:
                self._log("info", f"#更新 无凭证，静默跳过: qq={user_id}")
                return

            # 4. 静默更新课表（仅存 JSON，不发消息）
            if portal_ticket:
                try:
                    from schedule import JWGLClient
                    client = JWGLClient()
                    client.portal_ticket = portal_ticket
                    client.bridge()
                    schedule = client.get_schedule(student_id=student_id)
                    if schedule and schedule.courses:
                        json_path = _schedule_path("json", student_id=student_id, semester=schedule.semester)
                        schedule.save_json(json_path)
                        self._log("info", f"#更新 课表静默更新: {student_id} {len(schedule.courses)}门课")
                except Exception as e:
                    self._log("warning", f"#更新 课表静默更新失败: {e}")

            # 5. 静默更新二课（仅存 DB，不发消息）
            if access_token:
                try:
                    from secondclass.secondclass_tool import (
                        obtain_secondclass_session_from_user,
                        fetch_all_with_session, SecondClassDB,
                    )
                    sess = obtain_secondclass_session_from_user({
                        "student_id": student_id,
                        "portal_ticket": portal_ticket or "",
                        "access_token": access_token,
                        "expires_at": expires_at,
                    })
                    data = fetch_all_with_session(
                        sess,
                        student_id=student_id,
                        realname=user.get("realname", ""),
                        deptname=user.get("dept_name", ""),
                    )
                    sid = data.get("student_id") or student_id
                    data.pop("student_id", None)
                    SecondClassDB().upsert(student_id=sid, qq=user_id, **data)
                    # 静默更新图表
                    try:
                        data["student_id"] = sid
                        from secondclass.secondclass_image import render_secondclass_chart
                        render_secondclass_chart(data)
                    except Exception:
                        pass
                    self._log("info", f"#更新 二课静默更新: {student_id}")
                except Exception as e:
                    self._log("warning", f"#更新 二课静默更新失败: {e}")

        except Exception as e:
            self._log("error", f"#更新 异常: {e}")
            self._reply(msg_type, group_id, user_id, f"更新失败: {e}")

    # ---- #更新调试 ----

    def _cmd_update_debug(self, msg_type: str, group_id: int, user_id: int) -> None:
        """#更新调试：尝试 token 更新课表，失败自动重登（仅需验证码）。"""
        try:
            user = UserDB().get_by_qq(user_id)
            if not user:
                self._reply(msg_type, group_id, user_id, "你还未登录，请先使用 #扫码登录 或 #登录")
                return

            access_token = user.get("access_token", "")
            portal_ticket = user.get("portal_ticket", "")
            student_id = user.get("student_id", "")
            expires_at = user.get("expires_at", 0)

            if expires_at and expires_at < int(time.time()):
                self._reply(msg_type, group_id, user_id, "登录已过期，请重新 #扫码登录")
                return

            # Step 1: 先尝试用现有 portal_ticket 更新课表
            from schedule import JWGLClient, _schedule_path, ScheduleError

            update_success = False
            schedule = None
            json_path = None
            xlsx_path = None

            if portal_ticket:
                try:
                    client = JWGLClient()
                    client.portal_ticket = portal_ticket
                    client.bridge()
                    schedule = client.get_schedule(student_id=student_id)
                    if schedule and schedule.courses:
                        json_path = _schedule_path("json", student_id=student_id, semester=schedule.semester)
                        schedule.save_json(json_path)
                        update_success = True
                        self._log("info", f"#更新 token 更新成功: qq={user_id} {len(schedule.courses)} 门课")
                except ScheduleError as e:
                    self._log("info", f"#更新 token 更新失败（准备自动重登）: {e}")
                except Exception as e:
                    self._log("info", f"#更新 token 更新失败（准备自动重登）: {e}")

            if update_success and schedule:
                self._reply(msg_type, group_id, user_id, f"课表更新成功，{len(schedule.courses)} 门课")

                # 自动渲染本周课表
                self._cmd_week_schedule(msg_type, group_id, user_id, None)

                # 自动更新二课信息
                self._cmd_secondclass_info(msg_type, group_id, user_id)

                # 调试 #我的二课、#二课列表
                self._cmd_my_er(msg_type, group_id, user_id)
                self._cmd_secondclass_list(msg_type, group_id, user_id)
                return

            # Step 2: token 更新失败，查 login_creds 获取存储的学号密码
            creds = UserDB().get_creds(user_id)
            if creds:
                # 有存储凭据，直接自动获取验证码
                ucode, pwd = creds
                self._log("info", f"#更新 使用存储凭据: qq={user_id} ucode={ucode[:4]}…")
                self._bg_auto_login(msg_type, group_id, user_id, ucode, pwd)
                return

            # 没有存储凭据，启动交互式登录流程（复用 #登录）
            self._log("info", f"#更新 无存储凭据，启动交互式登录: qq={user_id}")
            self._start_login_session(msg_type, group_id, user_id)
            self._reply(msg_type, group_id, user_id, "请输入学号")

        except Exception as e:
            from sso.sso_common import redact
            self._log("error", f"#更新 失败: {redact(str(e))}")
            self._reply(msg_type, group_id, user_id, f"更新失败: {e}")

    def _bg_auto_login(self, msg_type: str, group_id: int, user_id: int,
                       ucode: str, password: str) -> None:
        """后台：已有学号密码，自动获取验证码并发送给用户输入。"""
        session_key = (user_id, msg_type, group_id)
        try:
            from schedule import JWGLClient

            # 创建客户端并开始 SSO 获取验证码
            client = JWGLClient()
            captcha_bytes = client.begin_sso()

            # 初始化会话，直接进入 WAIT_CAPTCHA 状态
            session = _LoginSession(
                state=_LoginState.WAIT_CAPTCHA,
                msg_type=msg_type,
                group_id=group_id,
                user_id=user_id,
                ucode=ucode,
                password=password,
                jwgl_client=client,
                last_activity=time.time(),
            )
            self._set_session(session_key, session)

            # 发送验证码图片给用户
            self._reply_image(msg_type, group_id, user_id, captcha_bytes,
                              "\n请输入验证码（token 更新失败，自动重登）")
            self._log("info", f"#更新 验证码已发送: qq={user_id}")
        except Exception as e:
            self._log("error", f"#更新 获取验证码失败: {e}")
            self._reply(msg_type, group_id, user_id, f"获取验证码失败: {e}")
            self._remove_session(session_key)

    # ---- #更新课表（个人） ----

    def _cmd_update_my_schedule(self, msg_type: str, group_id: int, user_id: int) -> None:
        """按 QQ 号查 UserDB，用该用户的 token 拉取个人课表。"""
        try:
            user = UserDB().get_by_qq(user_id)
            if not user:
                self._reply(msg_type, group_id, user_id, "你还未登录，请先使用 #扫码登录 或 #登录")
                return

            access_token = user.get("access_token", "")
            portal_ticket = user.get("portal_ticket", "")
            student_id = user.get("student_id", "")
            expires_at = user.get("expires_at", 0)

            if expires_at and expires_at < int(time.time()):
                self._reply(msg_type, group_id, user_id, "登录已过期，请重新 #扫码登录")
                return

            # 若 portal_ticket 缺失但有 access_token，尝试自动获取
            if not portal_ticket and access_token:
                from secondclass.secondclass_tool import _obtain_portal_ticket
                portal_ticket = _obtain_portal_ticket(access_token)
                if portal_ticket:
                    UserDB().upsert(qq=user_id, portal_ticket=portal_ticket)
                    self._log("info", f"自动获取 portal_ticket 成功: qq={user_id}")

            if not portal_ticket:
                self._reply(msg_type, group_id, user_id, "缺少门户票据，请重新 #扫码登录")
                return

            from schedule import JWGLClient, _schedule_path

            client = JWGLClient()
            client.portal_ticket = portal_ticket
            client.bridge()
            schedule = client.get_schedule(student_id=student_id)

            if not schedule.courses:
                raise RuntimeError("课表为空，可能 ticket 已失效")

            json_path = _schedule_path("json", student_id=student_id, semester=schedule.semester)
            schedule.save_json(json_path)

            self._log("info", f"个人课表拉取成功: qq={user_id} {len(schedule.courses)} 门课")
            self._reply(msg_type, group_id, user_id, f"课表更新成功！共 {len(schedule.courses)} 门课")

            # 自动渲染并发送本周课表
            self._cmd_week_schedule(msg_type, group_id, user_id, None)
        except Exception as e:
            self._log("error", f"个人课表拉取失败: {e}")
            self._reply(msg_type, group_id, user_id, f"拉取失败: {e}")

    # ---- #导出课表 ----
    def _cmd_export_schedule(self, msg_type: str, group_id: int, user_id: int) -> None:
        """导出最新课表为Excel文件并发送。"""
        try:
            user = UserDB().get_by_qq(user_id)
            if not user:
                # 兜底查 accounts.json
                from core.account_store import find_account_by_qq
                acc = find_account_by_qq(user_id)
                if not acc:
                    self._reply(msg_type, group_id, user_id,
                                "未找到您的账号信息，请先使用 #登录 或 #扫码登录")
                    return
                student_id = acc.get("student_id", "")
            else:
                student_id = user.get("student_id", "")

            if not student_id:
                self._reply(msg_type, group_id, user_id,
                            "未关联学号，请先使用 #登录")
                return

            from schedule import _schedule_path, _load_schedule_from_json
            sid_dir = PROJECT_ROOT / "schedules" / student_id
            if not sid_dir.is_dir():
                self._reply(msg_type, group_id, user_id,
                            "尚无课表数据，请先使用 #更新课表")
                return

            json_files = sorted(sid_dir.glob("*.json"))
            if not json_files:
                self._reply(msg_type, group_id, user_id,
                            "尚无课表数据，请先使用 #更新课表")
                return

            schedule = _load_schedule_from_json(json_files[-1])
            if not schedule or not schedule.courses:
                self._reply(msg_type, group_id, user_id,
                            "课表数据为空，请先使用 #更新课表")
                return

            # 导出Excel
            self._reply(msg_type, group_id, user_id, "正在导出课表Excel…")
            xlsx_path = _schedule_path("xlsx", student_id=student_id, semester=schedule.semester)
            try:
                schedule.save_excel(xlsx_path)
            except Exception as e:
                self._log("error", f"导出 Excel 失败: {e}")
                self._reply(msg_type, group_id, user_id,
                            f"导出Excel失败: {e}")
                return

            # 发送文件
            if xlsx_path.exists():
                sender_name = schedule.student_name or student_id or "课表"
                self._forward_files(msg_type, group_id, user_id, [xlsx_path], sender_name)
            else:
                self._reply(msg_type, group_id, user_id, "导出失败，文件不存在")

        except Exception as e:
            self._log("error", f"导出课表异常: {e}")
            self._reply(msg_type, group_id, user_id, "导出课表过程中出现异常")

    # ---- 课表图片 ----

    def _load_user_schedule(self, msg_type: str, group_id: int, user_id: int):
        """根据 QQ 号查找学号并加载本地 JSON 课表。返回 (Schedule, student_id) 或 None。"""
        try:
            user = UserDB().get_by_qq(user_id)
            if not user:
                from core.account_store import find_account_by_qq
                acc = find_account_by_qq(user_id)
                if not acc:
                    self._reply(msg_type, group_id, user_id,
                                "未找到您的账号信息，请先使用 #登录 或 #扫码登录")
                    return None
                student_id = acc.get("student_id", "")
            else:
                student_id = user.get("student_id", "")

            if not student_id:
                self._reply(msg_type, group_id, user_id,
                            "未关联学号，请先使用 #登录")
                return None

            from schedule import _load_schedule_from_json
            sid_dir = PROJECT_ROOT / "schedules" / student_id
            if not sid_dir.is_dir():
                self._reply(msg_type, group_id, user_id,
                            "尚未下载课表，请先使用 #更新课表")
                return None

            json_files = sorted(sid_dir.glob("*.json"))
            if not json_files:
                self._reply(msg_type, group_id, user_id,
                            "尚未下载课表，请先使用 #更新课表")
                return None

            schedule = _load_schedule_from_json(json_files[-1])
            if not schedule or not schedule.courses:
                self._reply(msg_type, group_id, user_id,
                            "课表数据为空，请先使用 #更新课表")
                return None

            return schedule, student_id
        except Exception as e:
            self._log("error", f"_load_user_schedule 异常: {e}")
            self._reply(msg_type, group_id, user_id,
                        f"加载课表数据失败: {e}")
            return None

    def _cmd_week_schedule(self, msg_type: str, group_id: int,
                           user_id: int, week_num: int | None) -> None:
        """生成并发送周课表图片。"""
        try:
            from schedule.schedule_image import (
                get_current_week_num, get_cached_image_path,
                is_cache_valid, render_schedule_image,
            )

            result = self._load_user_schedule(msg_type, group_id, user_id)
            if not result:
                return
            schedule, student_id = result

            semester = schedule.semester
            if week_num is None:
                week_num = get_current_week_num(semester)
                if week_num == 0:
                    self._reply(msg_type, group_id, user_id,
                                "无法自动计算当前周次，请使用 #第N周课表（如 #第17周课表）")
                    return

            if is_cache_valid(student_id, semester, week_num):
                png_path = get_cached_image_path(student_id, week_num)
                img_bytes = png_path.read_bytes()
            else:
                png_path = render_schedule_image(schedule, week_num, student_id)
                img_bytes = png_path.read_bytes()
            self._reply_image(msg_type, group_id, user_id, img_bytes,
                              f"第{week_num}周课表")
        except Exception as e:
            self._log("error", f"_cmd_week_schedule 异常: {e}")
            self._reply(msg_type, group_id, user_id,
                        f"课表图片生成失败: {e}")

    def _cmd_day_schedule(self, msg_type: str, group_id: int,
                          user_id: int, day_offset: int) -> None:
        """生成并发送单日课表卡片。"""
        try:
            from schedule.schedule_image import get_current_week_num, render_day_image

            result = self._load_user_schedule(msg_type, group_id, user_id)
            if not result:
                return
            schedule, student_id = result

            semester = schedule.semester
            week_num = get_current_week_num(semester)
            if week_num == 0:
                self._reply(msg_type, group_id, user_id,
                            "无法自动计算当前周次，请先使用 #更新课表")
                return

            from datetime import date, timedelta
            today = date.today()
            target = today + timedelta(days=day_offset)
            day = target.isoweekday()

            if day_offset > 0 and target.isocalendar()[1] != today.isocalendar()[1]:
                week_num += 1

            label = "今日" if day_offset == 0 else "明日"
            png_path = render_day_image(schedule, week_num, day, student_id)
            img_bytes = png_path.read_bytes()
            self._reply_image(msg_type, group_id, user_id, img_bytes,
                              f"{label}课表")
        except Exception as e:
            self._log("error", f"_cmd_day_schedule 异常: {e}")
            self._reply(msg_type, group_id, user_id,
                        f"课表图片生成失败: {e}")

    # ---- #二课信息 ----

    def _cmd_secondclass_info(self, msg_type: str, group_id: int, user_id: int) -> None:
        """按 QQ 号查 UserDB，用统一入口查询第二课堂信息。"""
        try:
            user = UserDB().get_by_qq(user_id)
            if not user:
                self._reply(msg_type, group_id, user_id,
                            "你还未登录，请先使用 #扫码登录 或 #登录")
                return

            data = secondclass_tool.fetch_and_save_secondclass_info(
                user_id, user)

            self._reply(msg_type, group_id, user_id,
                        secondclass_tool.format_secondclass_summary(data))

            # 同时生成并发送图表
            try:
                from secondclass.secondclass_image import render_secondclass_chart
                chart_path = render_secondclass_chart(data)
                img_bytes = chart_path.read_bytes()
                self._reply_image(msg_type, group_id, user_id, img_bytes)
            except Exception as chart_e:
                self._log("warning", f"二课图表生成失败（不影响文字查询）: {chart_e}")

            self._log("info", f"二课信息查询成功: qq={user_id}")
        except secondclass_tool.SecondClassAuthError as e:
            self._reply(msg_type, group_id, user_id, str(e))
        except Exception as e:
            from sso.sso_common import redact
            self._log("error", f"二课信息查询失败: {redact(str(e))}")
            self._reply(msg_type, group_id, user_id,
                        "查询二课信息失败，请稍后重试或重新登录")

    # ---- #二课图表 ----
    def _cmd_secondclass_chart(self, msg_type: str, group_id: int, user_id: int) -> None:
        """#二课图表：直接从 DB 缓存读取数据，渲染图表发送，不请求 API。"""
        try:
            from secondclass.secondclass_tool import SecondClassDB
            from secondclass.secondclass_image import render_secondclass_chart

            data = SecondClassDB().get_by_qq(user_id)
            if not data:
                self._reply(msg_type, group_id, user_id,
                            "暂无二课缓存数据，请先使用 #二课信息 查询")
                return

            chart_path = render_secondclass_chart(dict(data))
            img_bytes = chart_path.read_bytes()
            self._reply_image(msg_type, group_id, user_id, img_bytes,
                              "📊 你的第二课堂信息图表")
            self._log("info", f"二课图表生成成功: qq={user_id}")
        except Exception as e:
            from sso.sso_common import redact
            self._log("error", f"二课图表生成失败: {redact(str(e))}")
            self._reply(msg_type, group_id, user_id,
                        "生成二课图表失败，请稍后重试或重新登录")

    # ---- #二课列表 ----

    def _cmd_secondclass_list(self, msg_type: str, group_id: int, user_id: int) -> None:
        """#二课列表：渲染 master 表活动分页列表，每图最多10个，合并转发。"""
        from pathlib import Path
        import base64
        try:
            # ── 查找 student_id ──
            from secondclass.secondclass_tool import SecondClassDB, SecondClassMasterDB
            from core.account_store import find_account_by_qq
            sc_data = SecondClassDB().get_by_qq(user_id)
            student_id = ""
            if sc_data:
                student_id = sc_data.get("student_id", "")
            if not student_id:
                acc = find_account_by_qq(user_id)
                if acc:
                    student_id = acc.get("student_id", "")
            if not student_id:
                self._reply(msg_type, group_id, user_id,
                            "未关联学号，请先使用 #登录 或 #扫码登录")
                return

            # ── 从 master 表读取活动 ──
            master_db = SecondClassMasterDB()
            activities = master_db.get_by_student_id(student_id)
            if not activities:
                self._reply(msg_type, group_id, user_id, "您没有二课活动记录")
                return

            realname = sc_data.get("realname", "") if sc_data else ""
            student_info = "%s(%s)" % (realname, student_id) if realname else student_id
            total = len(activities)
            pages = total // 10 + (1 if total % 10 else 0)
            if pages > 10:
                self._reply(msg_type, group_id, user_id,
                            "活动较多(%d个，共%d页)，渲染可能需要较长时间…" % (total, pages))
            else:
                self._reply(msg_type, group_id, user_id,
                            "正在渲染 %s 的二课活动列表(%d个，共%d页)…" % (student_info, total, pages))

            # ── 分页渲染（每页最多10个）：存到 schedules/<student_id>/list/ ──
            from secondclass.secondclass_activity_chart import render_all_activity_lists
            chart_dir = Path(__file__).resolve().parent.parent / "schedules" / student_id / "list"
            chart_dir.mkdir(parents=True, exist_ok=True)

            paths = render_all_activity_lists(activities, student_info=student_info, output_dir=chart_dir)

            # ── 构建合并转发节点 ──
            sender_name = "二课活动列表"
            admin_qq = str(self.config.admin_qq)
            nodes: list[dict] = []
            for fp in paths:
                if not fp.exists():
                    continue
                file_uri = fp.resolve().as_uri()
                nodes.append({
                    "type": "node",
                    "data": {
                        "name": sender_name,
                        "uin": admin_qq,
                        "content": [
                            {"type": "image", "data": {"file": file_uri}},
                        ],
                    },
                })
            if not nodes:
                self._reply(msg_type, group_id, user_id, "渲染图片为空")
                return

            if msg_type == "group":
                self._client.send_group_forward_msg(group_id, nodes)
            else:
                self._client.send_private_forward_msg(user_id, nodes)

            self._log("info", "#二课列表 完成: qq=%s student_id=%s 活动=%d 页=%d" % (
                user_id, student_id, total, pages))

        except Exception as e:
            self._log("error", "#二课列表 异常: %s" % e)
            import traceback
            tb = traceback.format_exc()
            self._reply(msg_type, group_id, user_id, "生成二课列表失败")
            self._send_log_as_forward(msg_type, group_id, user_id, "#二课列表 错误日志", tb)

    # ---- #我的二课 ----

    def _cmd_my_er(self, msg_type: str, group_id: int, user_id: int) -> None:
        """#我的二课：从独立表读取未结束活动ID，逐张渲染卡片后合并转发。"""
        from pathlib import Path
        try:
            # ── 1. 查找 student_id ──
            from secondclass.secondclass_tool import SecondClassDB
            from core.account_store import find_account_by_qq
            sc_data = SecondClassDB().get_by_qq(user_id)
            student_id = ""
            if sc_data:
                student_id = sc_data.get("student_id", "")
            if not student_id:
                acc = find_account_by_qq(user_id)
                if acc:
                    student_id = acc.get("student_id", "")
            if not student_id:
                self._reply(msg_type, group_id, user_id,
                            "未关联学号，请先使用 #登录 或 #扫码登录")
                return

            realname = sc_data.get("realname", "") if sc_data else ""

            # ── 2. 从独立表读取未结束活动ID ──
            from secondclass.secondclass_tool import SecondClassUserActivityDB
            user_act_db = SecondClassUserActivityDB()
            activity_ids = user_act_db.get_activity_ids_by_student_id(student_id)

            # 缓存未命中 → 现场拉取一次再读
            if not activity_ids:
                user = UserDB().get_by_qq(user_id)
                if user:
                    self._reply(msg_type, group_id, user_id,
                                "未结束活动缓存为空，正在拉取…")
                    try:
                        from secondclass.secondclass_tool import (
                            fetch_and_store_my_unfinished_activities,
                            obtain_secondclass_session_from_user,
                        )
                        sess = obtain_secondclass_session_from_user(user)
                        if sess:
                            fetch_and_store_my_unfinished_activities(
                                sess, student_id, qq=user_id,
                            )
                            activity_ids = user_act_db.get_activity_ids_by_student_id(student_id)
                    except Exception as e:
                        self._log("warning", f"#我的二课 现场拉取失败: {e}")

            if not activity_ids:
                self._reply(msg_type, group_id, user_id,
                            "您当前没有未结束的活动（报名中/活动中/未开始）")
                return

            total = len(activity_ids)
            self._reply(msg_type, group_id, user_id,
                        "正在渲染 %s 的未结束活动(%d个)，请稍候…" % (
                            f"{realname}({student_id})" if realname else student_id, total))

            # ── 3. 逐张渲染活动卡片（缓存命中则跳过）──
            from secondclass.secondclass_activity_chart import (
                render_activity_card, OUTPUT_DIR as CARD_DIR,
            )
            from secondclass.secondclass_tool import (
                SecondClassActivityDetailDB, get_all_user_credentials,
                obtain_secondclass_session_from_user,
                fetch_activity_detail_page, fetch_and_save_activity_detail,
            )
            CARD_DIR.mkdir(parents=True, exist_ok=True)
            detail_db = SecondClassActivityDetailDB()

            card_paths: list[Path] = []
            need_session = True  # 懒获取 sess（只在首次缓存未命中时获取）
            sess = None

            for aid in activity_ids:
                try:
                    cache_path = CARD_DIR / f"{aid}.png"
                    if cache_path.exists():
                        card_paths.append(cache_path)
                        continue

                    # 缓存未命中 → 查 DetailDB
                    detail = detail_db.get_by_activity_id(aid)
                    has_cached = detail and detail.get("activity_name") and "跳转提示" not in str(detail.get("activity_name", ""))

                    if not has_cached:
                        # 实时拉取
                        if need_session:
                            users = [u for u in get_all_user_credentials() if u.get("student_id")]
                            if users:
                                for u in users:
                                    try:
                                        sess = obtain_secondclass_session_from_user(u)
                                        if sess:
                                            break
                                    except Exception:
                                        continue
                            need_session = False

                        if not sess:
                            self._reply(msg_type, group_id, user_id,
                                        "会话已过期，未结束活动部分渲染可能不完整")
                            break

                        detail = fetch_activity_detail_page(sess, aid)
                        aname = (detail.get("activity_name") or "") if detail else ""
                        if not aname or "跳转提示" in aname:
                            continue
                        fetch_and_save_activity_detail(sess, student_id, aid)
                        detail = detail_db.get_by_activity_id(aid)

                    if detail and detail.get("activity_name"):
                        card_path = render_activity_card(dict(detail), output_dir=CARD_DIR)
                        card_paths.append(card_path)
                except Exception:
                    continue

            if not card_paths:
                self._reply(msg_type, group_id, user_id, "没有可展示的活动卡片")
                return

            # ── 4. 合并转发 ──
            sender_name = "我的二课"
            admin_qq = str(self.config.admin_qq)
            nodes: list[dict] = []
            for fp in card_paths:
                if not fp.exists():
                    continue
                file_uri = fp.resolve().as_uri()
                nodes.append({
                    "type": "node",
                    "data": {
                        "name": sender_name,
                        "uin": admin_qq,
                        "content": [
                            {"type": "image", "data": {"file": file_uri}},
                        ],
                    },
                })

            if not nodes:
                self._reply(msg_type, group_id, user_id, "渲染图片为空")
                return

            if msg_type == "group":
                self._client.send_group_forward_msg(group_id, nodes)
            else:
                self._client.send_private_forward_msg(user_id, nodes)

            self._log("info", "#我的二课 完成: qq=%s student_id=%s 活动=%d 卡片=%d" % (
                user_id, student_id, total, len(card_paths)))

        except Exception as e:
            self._log("error", "#我的二课 异常: %s" % e)
            import traceback
            tb = traceback.format_exc()
            self._reply(msg_type, group_id, user_id, "查询我的二课失败")
            self._send_log_as_forward(msg_type, group_id, user_id, "#我的二课 错误日志", tb)

    # ---- #<活动ID> 活动详情 ----

    def _cmd_activity_detail(self, msg_type: str, group_id: int, user_id: int,
                              activity_id: str) -> None:
        """#<activity_id>：从细节表读数据，渲染卡片图片后发送。"""
        try:
            # ── 1. 查缓存 ──
            from secondclass.secondclass_tool import SecondClassActivityDetailDB
            detail_db = SecondClassActivityDetailDB()
            detail = detail_db.get_by_activity_id(activity_id)
            if not detail or not detail.get("activity_name"):
                # ── 2. 缓存未命中，实时拉取 ──
                from secondclass.secondclass_tool import (
                    get_all_user_credentials, obtain_secondclass_session_from_user,
                    fetch_activity_detail_page, fetch_and_save_activity_detail,
                )
                users = [u for u in get_all_user_credentials() if u.get("student_id")]
                if not users:
                    self._reply(msg_type, group_id, user_id,
                                "无可用凭证，请先使用 #扫码登录 或 #登录")
                    return
                sess = None
                for user in users:
                    try:
                        sess = obtain_secondclass_session_from_user(user)
                        if sess:
                            break
                    except Exception:
                        continue
                if not sess:
                    self._reply(msg_type, group_id, user_id,
                                "所有用户凭证均已过期，请重新 #扫码登录")
                    return
                detail = fetch_activity_detail_page(sess, activity_id)
                if not detail or not detail.get("activity_name"):
                    self._reply(msg_type, group_id, user_id,
                                f"活动 {activity_id} 不存在或获取失败")
                    return
                fetch_and_save_activity_detail(sess, users[0].get("student_id", ""),
                                               activity_id)
                detail = detail_db.get_by_activity_id(activity_id)
                if not detail:
                    self._reply(msg_type, group_id, user_id, "保存活动详情失败")
                    return

            # ── 3. 渲染卡片图片并发送 ──
            from secondclass.secondclass_activity_chart import render_activity_card
            from secondclass.secondclass_activity_chart import OUTPUT_DIR as CHART_DIR
            card_path = render_activity_card(dict(detail), output_dir=CHART_DIR)
            img_bytes = card_path.read_bytes()
            self._reply_image(msg_type, group_id, user_id, img_bytes,
                              f"📋 活动详情 #{activity_id}")

        except Exception as e:
            self._log("error", "#活动详情 异常: %s" % e)
            self._reply(msg_type, group_id, user_id,
                        f"查询活动 {activity_id} 详情失败，请稍后重试")

    # ---- #报名 ----

    def _cmd_apply(self, msg_type: str, group_id: int, user_id: int,
                   activity_id: str) -> None:
        """#报名 <活动ID>：二课活动报名流程。

        步骤：查询用户凭证 → 获取二课 session → 解析 apply.html 抽取 s1/s2 →
        发送验证码图片 → 等待用户输入验证码 → 提交报名。
        """
        try:
            # 1) 查找用户凭证
            user = UserDB().get_by_qq(user_id)
            if not user or not user.get("student_id"):
                self._reply(msg_type, group_id, user_id,
                            "未找到您的登录凭证，请先 #扫码登录 或 #登录")
                return

            self._reply(msg_type, group_id, user_id,
                        f"正在准备报名活动 {activity_id}…")

            # 2) 获取二课 session
            from secondclass.secondclass_tool import (
                fetch_apply_page, fetch_verifycode_image,
                obtain_secondclass_session_from_user, submit_activity_apply,
            )
            sess = obtain_secondclass_session_from_user(user)
            if not sess:
                self._reply(msg_type, group_id, user_id,
                            "二课凭证已过期，请重新 #扫码登录")
                return

            # 3) 解析报名页（抽取 s1/s2，检测状态）
            apply_data = fetch_apply_page(sess, activity_id)
            activity_name = apply_data.get("activity_name", activity_id)

            if not apply_data.get("s1") or not apply_data.get("s2"):
                self._reply(msg_type, group_id, user_id,
                            f"活动「{activity_name}」报名页面解析失败，无法获取隐藏字段")
                return

            # 4) 不需要验证码 → 直接提交
            if not apply_data.get("need_captcha", True):
                result = submit_activity_apply(
                    sess, activity_id, "", apply_data["s1"], apply_data["s2"],
                )
                if result["success"]:
                    self._reply(msg_type, group_id, user_id,
                                f"报名成功！活动：{activity_name}")
                else:
                    self._reply(msg_type, group_id, user_id,
                                f"报名失败：{result['message']}")
                return

            # 5) 需要验证码 → 拉取图片，进入等待状态
            self._reply(msg_type, group_id, user_id,
                        f"活动：{activity_name}\n正在获取验证码…")
            img_bytes = fetch_verifycode_image(sess)

            session_key = (user_id, msg_type, group_id)
            apply_session = _LoginSession(
                state=_LoginState.WAIT_APPLY_CAPTCHA,
                msg_type=msg_type, group_id=group_id, user_id=user_id,
                last_activity=time.time(),
                activity_id=activity_id,
                apply_data=apply_data,
                sc_session=sess,
            )
            self._set_session(session_key, apply_session)
            self._reply_image(msg_type, group_id, user_id, img_bytes,
                              f"\n活动 {activity_id} 报名验证码\n请输入验证码，发送 #取消 可取消")

        except Exception as e:
            # 区分已知业务异常 vs 未知异常
            from secondclass.secondclass_tool import (
                ActivityApplyError, SecondClassAuthError,
            )
            if isinstance(e, (ActivityApplyError, SecondClassAuthError)):
                self._log("warning",
                          f"#报名 业务异常: activity_id={activity_id} "
                          f"type={type(e).__name__} msg={e}")
                self._reply(msg_type, group_id, user_id, str(e))
            else:
                import traceback
                self._log("error",
                          f"#报名 异常: activity_id={activity_id} {type(e).__name__}: {e}\n"
                          f"{traceback.format_exc()}")
                self._reply(msg_type, group_id, user_id, f"报名过程出现异常: {e}")

    def _bg_apply_submit(self, user_id: int, msg_type: str,
                         group_id: int, rcode: str) -> None:
        """后台：提交活动报名。"""
        session_key = (user_id, msg_type, group_id)
        session = self._get_session(session_key)
        if not session or session.state != _LoginState.WAIT_APPLY_CAPTCHA:
            return

        sess = session.sc_session
        activity_id = session.activity_id
        apply_data = session.apply_data or {}
        activity_name = apply_data.get("activity_name", activity_id)

        try:
            from secondclass.secondclass_tool import (
                ActivityApplyError, SecondClassAuthError, submit_activity_apply,
            )
            result = submit_activity_apply(
                sess, activity_id, rcode,
                apply_data.get("s1", ""), apply_data.get("s2", ""),
            )
            if result["success"]:
                self._reply(msg_type, group_id, user_id,
                            f"报名成功！活动：{activity_name}")
            else:
                self._reply(msg_type, group_id, user_id,
                            f"报名失败：{result['message']}\n可重新发送 #报名 {activity_id}")
        except SecondClassAuthError as e:
            self._reply(msg_type, group_id, user_id, str(e))
        except ActivityApplyError as e:
            self._reply(msg_type, group_id, user_id, str(e))
        except Exception as e:
            self._log("error", f"提交报名异常: {e}")
            self._reply(msg_type, group_id, user_id, f"报名提交异常: {e}")
        finally:
            self._remove_session(session_key)

    # ---- #预约报名 / #我的预约（业务实现见 qq/reservation/）----

    def _reservation_commands(self) -> "ReservationCommands":
        from qq.reservation import ReservationCommands
        return ReservationCommands(
            reply=lambda mt, gid, uid, text: self._reply(mt, gid, uid, text),
            student_lookup=self._lookup_student_for_reservation,
        )

    @staticmethod
    def _lookup_student_for_reservation(qq: int) -> tuple[str, dict | None]:
        user = UserDB().get_by_qq(qq)
        if user and user.get("student_id"):
            return str(user["student_id"]), user
        return "", user

    def _cmd_reserve(self, msg_type: str, group_id: int, user_id: int,
                     activity_id: str) -> None:
        self._reservation_commands().handle_reserve(msg_type, group_id, user_id, activity_id)

    def _cmd_my_reservations(self, msg_type: str, group_id: int, user_id: int) -> None:
        self._reservation_commands().handle_my_reservations(msg_type, group_id, user_id)

    # ---- #签到 / #签退 ----

    def _sign_commands(self) -> "SignCommands":
        from qq.sign_commands import SignCommands
        return SignCommands(
            reply=lambda mt, gid, uid, text: self._reply(mt, gid, uid, text),
            student_lookup=self._lookup_student_for_reservation,
        )

    def _cmd_sign(self, msg_type: str, group_id: int, user_id: int,
                  activity_id: str, sign_out: bool, channel_id: int) -> None:
        sc = self._sign_commands()
        if sign_out:
            sc.handle_sign_out(msg_type, group_id, user_id, activity_id, channel_id)
        else:
            sc.handle_sign_in(msg_type, group_id, user_id, activity_id, channel_id)

    def _cmd_scan_sign(self, msg_type: str, group_id: int, user_id: int,
                       image_url: str) -> None:
        from qq.image_intake import download_image
        img_bytes = download_image(image_url)
        if not img_bytes:
            self._reply(msg_type, group_id, user_id,
                        "图片下载失败，请重新发送 #扫码签到 重试")
            return
        self._sign_commands().handle_scan_qr_image(
            msg_type, group_id, user_id, img_bytes,
        )

    # ---- #扫码登录 ----

    def _cmd_qr_login(self, msg_type: str, group_id: int, user_id: int) -> None:
        """二维码扫码登录：获取二维码→发送→等待扫码→Portal验证→获取PORTAL_TICKET。"""
        # 防止重复：已有活跃登录会话则拒绝
        session_key = (user_id, msg_type, group_id)
        old = self._get_session(session_key)
        if old and old.state != _LoginState.IDLE:
            self._reply(msg_type, group_id, user_id, "已有登录流程进行中，发送 #取消 可终止")
            return

        import requests as req
        from sso.sso_common import APPID, AUTH_URL_TEMPLATE, DEFAULT_REDIRECT
        from urllib.parse import parse_qs, urlparse

        # 创建会话用于取消检测
        login_session = _LoginSession(
            state=_LoginState.LOGGING_IN,
            msg_type=msg_type, group_id=group_id, user_id=user_id,
            last_activity=time.time(),
        )
        self._set_session(session_key, login_session)

        try:
            # ---- 1. 建立 OAuth2 会话 + 获 accKey ----
            sess = req.Session()
            sess.trust_env = False
            sess.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            })

            auth_url = AUTH_URL_TEMPLATE.format(appid=APPID, redirect_uri=DEFAULT_REDIRECT)
            r = sess.get(auth_url, allow_redirects=False, timeout=15)
            loc = r.headers.get("Location") or ""
            acc_key = (parse_qs(urlparse(loc).query).get("accKey") or [""])[0]
            if not acc_key:
                raise RuntimeError(f"auth2orize 未返回 accKey: status={r.status_code}")

            # ---- 2. 获取二维码（支持 JSON 和二进制两种响应）----
            erm_url = f"http://szxy.cqtbi.edu.cn/oauth2/v1/createErm?seq=99&v={acc_key}"
            r = sess.get(erm_url, timeout=15)
            r.raise_for_status()

            qr_img_bytes = b""
            ct = (r.headers.get("Content-Type") or "").lower()

            if "json" in ct:
                try:
                    erm_data = r.json()
                    self._log("info", f"createErm JSON 响应: {str(erm_data)[:200]}")
                    img_field = erm_data.get("img") or erm_data.get("image") or ""
                    if img_field:
                        if "base64," in img_field:
                            img_b64 = img_field.split("base64,")[-1]
                        else:
                            img_b64 = img_field
                        qr_img_bytes = base64.b64decode(img_b64)
                except Exception as e:
                    self._log("warning", f"createErm JSON 解析失败: {e}，尝试二进制回退")
                    qr_img_bytes = r.content

            if not qr_img_bytes:
                qr_img_bytes = r.content

            log.info("qr_login: 二维码 %dB", len(qr_img_bytes))

            if len(qr_img_bytes) < 100:
                raise RuntimeError(f"二维码数据异常: {len(qr_img_bytes)}B")

            # ---- 3. 发送二维码到 QQ ----
            self._reply_image(msg_type, group_id, user_id, qr_img_bytes,
                              "\n请扫码登录（2分钟内有效）\n发送 #取消 可取消等待")
            self._log("info", f"二维码已发送: user={user_id} accKey={acc_key[:8]}…")

            # ---- 4. 轮询扫码状态（oauth2ewmSqCheck）----
            login_ok = False
            poll_url = "http://szxy.cqtbi.edu.cn/oauth2/v1/oauth2ewmSqCheck"
            deadline = time.time() + 120
            redirect_url = ""
            ticket = ""

            while time.time() < deadline:
                if not self._get_session(session_key):
                    self._reply(msg_type, group_id, user_id, "已取消扫码登录")
                    return

                time.sleep(3)
                try:
                    pr = sess.post(
                        poll_url,
                        data={"accKey": acc_key},
                        timeout=10,
                        headers={
                            "X-Requested-With": "XMLHttpRequest",
                            "Referer": f"http://szxy.cqtbi.edu.cn/oauth2/Login.html?accKey={acc_key}",
                            "Origin": "http://szxy.cqtbi.edu.cn",
                        },
                    )
                    pr.raise_for_status()
                    pdata = pr.json()
                    self._log("info", f"qr 轮询: status={pdata.get('status')} data={str(pdata)[:200]}")

                    if pdata.get("success") or str(pdata.get("status")) == "1":
                        login_ok = True
                        redirect_url = pdata.get("redirect_url") or ""
                        ticket = pdata.get("ticket") or pdata.get("portal_ticket") or ""
                        break

                    if str(pdata.get("status")) == "2":
                        self._reply(msg_type, group_id, user_id, "已扫码，请在手机确认登录…")
                except Exception as e:
                    self._log("info", f"轮询扫码状态异常（继续）: {e}")

            if not login_ok:
                self._reply(msg_type, group_id, user_id, "扫码登录超时，请重新 #扫码登录")
                return

            # ---- 5. 扫码成功 → 换 access_token → 获取用户信息 ----
            code = (parse_qs(urlparse(redirect_url).query).get("code") or [""])[0]
            portal_ticket_final = ticket
            access_token = ""

            # 5a. 跟随 redirect_url 触发 PORTAL_TICKET cookie 设置并提取
            if redirect_url:
                try:
                    self._log("info", "跟随扫码重定向以获取 PORTAL_TICKET")
                    sess.get(
                        redirect_url, timeout=15,
                        headers={"Referer": f"http://szxy.cqtbi.edu.cn/oauth2/Login.html?accKey={acc_key}"},
                        allow_redirects=True,
                    )
                    # 从门户域 cookie 中提取 PORTAL_TICKET
                    pt_cookie = sess.cookies.get("PORTAL_TICKET")
                    if pt_cookie:
                        ticket = ticket or pt_cookie
                        portal_ticket_final = ticket
                        self._log("info", f"从 redirect_url cookie 获取 PORTAL_TICKET: {pt_cookie[:8]}…")
                except Exception as e:
                    self._log("warning", f"扫码重定向失败（可忽略）: {e}")

                # 若仍无 PORTAL_TICKET，再走一次 auth2orize（利用已建立的 SSO 会话）
                if not portal_ticket_final:
                    try:
                        self._log("info", "重走 auth2orize 以获取 PORTAL_TICKET")
                        r2 = sess.get(auth_url, allow_redirects=False, timeout=15)
                        if r2.status_code in (301, 302, 303, 307):
                            loc2 = r2.headers.get("Location", "")
                            if loc2:
                                sess.get(loc2, timeout=15, allow_redirects=True)
                        pt_cookie2 = sess.cookies.get("PORTAL_TICKET")
                        if pt_cookie2:
                            ticket = pt_cookie2
                            portal_ticket_final = ticket
                            self._log("info", f"从 auth2orize cookie 获取 PORTAL_TICKET: {pt_cookie2[:8]}…")
                    except Exception as e:
                        self._log("warning", f"auth2orize 重走失败（可忽略）: {e}")

            # 5b. 用 code 换 access_token
            if code:
                try:
                    from sso.sso_common import exchange_code_for_token
                    token_result = exchange_code_for_token(code)
                    access_token = token_result.get("access_token", "")
                    expires_in = token_result.get("expires_in", 3600)
                    self._log("info", f"access_token 获取: {'成功' if access_token else '失败'}")
                except Exception as e:
                    self._log("warning", f"code 换 token 失败: {e}")
                    expires_in = 3600
            else:
                expires_in = 3600

            # ---- 6. 用 access_token 获取用户信息并保存 ----
            now_iso = datetime.now().isoformat(timespec="seconds")
            expires_at = int(time.time()) + expires_in if access_token else 0

            # 优先用 access_token + access_user 获取用户信息
            user_info = self._fetch_user_info(access_token) if access_token else None
            if user_info:
                student_id = user_info.get("userloginid", "")
                realname = user_info.get("userrealname", "")
                UserDB().upsert(
                    qq=user_id,
                    student_id=student_id,
                    realname=realname,
                    dept_id=user_info.get("userdeptid", ""),
                    dept_name=user_info.get("userdepaname", ""),
                    is_teacher=user_info.get("teacher") == "1",
                    access_token=access_token,
                    portal_ticket=ticket,
                    expires_at=expires_at,
                    last_login=now_iso,
                )
                self._reply(msg_type, group_id, user_id,
                            f"登录成功！\n姓名：{realname}\n学号：{student_id}")
                self._log("info", f"扫码登录成功: qq={user_id} sid={student_id} ticket={'有' if ticket else '无'}")
            else:
                UserDB().upsert(
                    qq=user_id,
                    student_id="",
                    access_token=access_token,
                    portal_ticket=ticket,
                    expires_at=expires_at,
                    last_login=now_iso,
                )
                self._reply(msg_type, group_id, user_id, "登录成功！（未获取到用户详情）")

        except Exception as e:
            self._log("error", f"扫码登录失败: {e}")
            self._reply(msg_type, group_id, user_id, f"扫码登录失败: {e}")
        finally:
            self._remove_session(session_key)

    # ---- #查询用户 ----

    def _cmd_query_users(self, msg_type: str, group_id: int, user_id: int) -> None:
        """查询所有用户并导出 Excel。"""
        try:
            users = UserDB().get_all()
            if not users:
                self._reply(msg_type, group_id, user_id, "暂无用户数据")
                return

            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "用户列表"
            headers = ["QQ号", "学号", "姓名", "部门编号", "部门名称",
                       "教师(1)/学生(0)", "access_token", "portal_ticket",
                       "过期时间戳", "最近登录", "注册时间"]
            ws.append(headers)
            for u in users:
                ws.append([
                    u.get("qq", ""),
                    u.get("student_id", ""),
                    u.get("realname", ""),
                    u.get("dept_id", ""),
                    u.get("dept_name", ""),
                    u.get("is_teacher", 0),
                    u.get("access_token", ""),
                    u.get("portal_ticket", ""),
                    u.get("expires_at", 0),
                    u.get("last_login", ""),
                    u.get("created_at", ""),
                ])
            # 自动列宽
            for col in ws.columns:
                max_len = max(len(str(c.value or "")) for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 40)

            xlsx_path = PROJECT_ROOT / "schedules" / "users.xlsx"
            xlsx_path.parent.mkdir(exist_ok=True)
            wb.save(str(xlsx_path))

            self._log("info", f"用户表导出成功: {len(users)} 条 → {xlsx_path.name}")
            self._reply(msg_type, group_id, user_id, f"共 {len(users)} 名用户")
            self._forward_files(msg_type, group_id, user_id, [xlsx_path], "用户列表")
        except Exception as e:
            self._log("error", f"查询用户失败: {e}")
            self._reply(msg_type, group_id, user_id, f"查询用户失败: {e}")


# ---------- 转发器 ----------
class QQForwarder:
    """监听源群消息并转发到目标群。"""

    MAX_ERRORS = 5

    def __init__(self, config: ForwardConfig, log_callback: Callable[[str, str], None]) -> None:
        self.config = config
        self._log = log_callback
        self._client: OneBotClient | None = None
        self._command_handler: CommandHandler | None = None
        self._running = False
        self._seen_ids: set[int] = set()
        self._consecutive_errors = 0
        self._reconnect_timer: threading.Timer | None = None
        self._current_message_id: int | None = None

    def start(self) -> None:
        self._client = OneBotClient(self.config.ws_url, self._on_event, self.config.access_token)
        self._client.set_log_callback(self._log)
        self._command_handler = CommandHandler(self.config, self._client, self._log)
        self._client.connect()
        self._running = True

        # 启动重连守护线程
        t = threading.Thread(target=self._reconnect_watchdog, daemon=True)
        t.start()

        # 启动预约提醒调度器
        from qq.reservation import ReservationScheduler
        self._reservation_scheduler = ReservationScheduler(
            send_callback=self._send_reservation_reminder,
            log_callback=self._log,
        )
        self._reservation_scheduler.start()

    def _send_reservation_reminder(
        self, msg_type: str, group_id: int, qq: int, segments: list[dict],
    ) -> None:
        """预约调度器回调：群里 @ 提醒；私聊直接发文本。"""
        try:
            if msg_type == "group" and group_id:
                self._client.send_group_msg(group_id, segments)
            else:
                self._client.send_private_msg(qq, segments)
        except Exception as e:
            self._log("error", f"预约提醒发送失败: {e}")

    def stop(self) -> None:
        self._running = False
        if self._reconnect_timer:
            self._reconnect_timer.cancel()
        if self._client:
            self._client.disconnect()
        sched = getattr(self, "_reservation_scheduler", None)
        if sched:
            sched.stop()
        self._log("info", "已停止监听")

    @property
    def is_running(self) -> bool:
        return self._running

    def _on_event(self, event: dict) -> None:
        if not self._running:
            return

        # 先让指令系统处理（私聊或群聊）
        if event.get("post_type") == "message":
            message_id = event.get("message_id")
            if message_id and message_id in self._seen_ids:
                return
            try:
                if self._command_handler.try_handle(event):
                    if message_id:
                        self._seen_ids.add(message_id)
                    return
            except Exception as e:
                self._log("error", f"指令处理异常: {e}")

        # 只处理群消息（转发）
        if event.get("post_type") != "message":
            return
        if event.get("message_type") != "group":
            return

        group_id = event.get("group_id", 0)
        if group_id not in self.config.source_groups:
            self._log("info", f"忽略非源群消息 group={group_id}（源群列表={self.config.source_groups}）")
            return

        message_id = event.get("message_id")
        if message_id in self._seen_ids:
            return
        self._seen_ids.add(message_id)

        # 限制去重集合大小
        if len(self._seen_ids) > 10000:
            self._seen_ids = set(list(self._seen_ids)[-5000:])

        user_id = event.get("user_id", 0)
        sender = event.get("sender", {})
        nickname = sender.get("nickname", str(user_id))
        message = event.get("message", [])
        raw_message = event.get("raw_message", "")

        self._log("forward", f"[{group_id}] {nickname}: {raw_message[:80]}")

        try:
            self._current_message_id = message_id
            self._forward_message(nickname, user_id, message)
            self._consecutive_errors = 0
        except OneBotError as e:
            self._consecutive_errors += 1
            self._log("error", f"转发失败 ({self._consecutive_errors}/{self.MAX_ERRORS}): {e}")
            if self._consecutive_errors >= self.MAX_ERRORS:
                self._log("error", "连续错误过多，监控已停止")
                self._running = False

    def _forward_message(self, nickname: str, user_id: int, message: list[dict]) -> None:
        """转发消息到所有目标群：先发送发送者标识，再原生转发内容。"""
        if not self.config.target_groups:
            return
        source_groups_str = ",".join(str(g) for g in self.config.source_groups)
        sender_label = f"【{nickname}(QQ: {user_id})】\n来自群 {source_groups_str}"
        message_id = self._current_message_id

        for tgt in self.config.target_groups:
            # 1. 先发送发送者标识
            try:
                self._client.send_group_msg(tgt, [
                    {"type": "text", "data": {"text": sender_label}},
                ])
            except Exception:
                pass

            # 2. 原生转发原消息内容
            if message_id:
                try:
                    self._client.forward_group_single_msg(tgt, message_id)
                    self._log("info", f"原生转发到 {tgt} (mid={message_id}, 来自={nickname})")
                    continue
                except Exception:
                    self._log("info", f"原生转发到 {tgt} 失败，降级")

            # 降级：直接发送内容
            forward_content = [
                {"type": "text", "data": {
                    "text": f"【{nickname}(QQ: {user_id})】\n来自群 {source_groups_str}\n━━━━━━━━━━━━━━\n"
                }},
            ]
            forward_content.extend(message)
            self._client.send_group_msg(tgt, forward_content)

    def _reconnect_watchdog(self) -> None:
        """定期检查连接状态，断线自动重连。"""
        while self._running:
            if self._client and not self._client.is_connected:
                self._log("info", "连接已断开，尝试重连…")
                try:
                    self._client.disconnect()
                    self._client.connect()
                    self._log("info", "重连成功")
                except Exception as e:
                    self._log("error", f"重连失败: {e}，5 秒后重试")
            threading.Event().wait(5)


# ---------- GUI ----------
class ForwardApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("QQ 消息转发工具")
        self.geometry("640x560")
        self.resizable(True, True)

        self.config = ForwardConfig.load()
        self.forwarder: QQForwarder | None = None
        self._busy = False

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        # --- 配置区 ---
        cfg_frame = ttk.LabelFrame(self, text="连接配置")
        cfg_frame.pack(fill="x", **pad)

        row0 = ttk.Frame(cfg_frame)
        row0.pack(fill="x", padx=6, pady=4)
        ttk.Label(row0, text="OneBot WS:").pack(side="left")
        self.ws_var = tk.StringVar(value=self.config.ws_url)
        ttk.Entry(row0, textvariable=self.ws_var, width=30).pack(side="left", padx=4)

        row_token = ttk.Frame(cfg_frame)
        row_token.pack(fill="x", padx=6, pady=4)
        ttk.Label(row_token, text="Token:").pack(side="left")
        self.token_var = tk.StringVar(value=self.config.access_token)
        ttk.Entry(row_token, textvariable=self.token_var, width=30).pack(side="left", padx=4)

        # --- 源群 / 目标群 多行列表 ---
        glist_frame = ttk.Frame(cfg_frame)
        glist_frame.pack(fill="x", padx=6, pady=4)

        # 源群列
        src_frame = ttk.LabelFrame(glist_frame, text="源群列表（每行一个群号）")
        src_frame.pack(side="left", fill="both", expand=True, padx=(0, 4))
        self.src_text = tk.Text(src_frame, height=4, width=18, font=("Consolas", 9))
        self.src_text.insert("1.0", "\n".join(str(g) for g in self.config.source_groups))
        self.src_text.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        src_scroll = ttk.Scrollbar(src_frame, orient="vertical", command=self.src_text.yview)
        self.src_text.configure(yscrollcommand=src_scroll.set)
        src_scroll.pack(side="right", fill="y", pady=4)

        # 目标群列
        tgt_frame = ttk.LabelFrame(glist_frame, text="目标群列表（每行一个群号）")
        tgt_frame.pack(side="left", fill="both", expand=True, padx=(4, 0))
        self.tgt_text = tk.Text(tgt_frame, height=4, width=18, font=("Consolas", 9))
        self.tgt_text.insert("1.0", "\n".join(str(g) for g in self.config.target_groups))
        self.tgt_text.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        tgt_scroll = ttk.Scrollbar(tgt_frame, orient="vertical", command=self.tgt_text.yview)
        self.tgt_text.configure(yscrollcommand=tgt_scroll.set)
        tgt_scroll.pack(side="right", fill="y", pady=4)

        ttk.Button(cfg_frame, text="保存配置", command=self._save_config).pack(pady=4)

        # --- 指令系统配置 ---
        cmd_frame = ttk.LabelFrame(self, text="#指令系统")
        cmd_frame.pack(fill="x", **pad)

        row_admin = ttk.Frame(cmd_frame)
        row_admin.pack(fill="x", padx=6, pady=4)
        ttk.Label(row_admin, text="管理员QQ:").pack(side="left")
        self.admin_var = tk.StringVar(value=str(self.config.admin_qq))
        ttk.Entry(row_admin, textvariable=self.admin_var, width=15).pack(side="left", padx=4)

        self.command_enabled_var = tk.BooleanVar(value=self.config.command_enabled)
        ttk.Checkbutton(
            row_admin, text="启用 #指令", variable=self.command_enabled_var,
        ).pack(side="left", padx=12)

        ttk.Label(
            cmd_frame,
            text="支持指令: #帮助 #扫码登录 #登录 #更新课表 #更新模板课表 #取消（所有人）| #查询用户（仅管理员）",
            foreground="#888",
        ).pack(fill="x", padx=10, pady=2)

        # --- 控制区 ---
        ctrl_frame = ttk.Frame(self)
        ctrl_frame.pack(fill="x", padx=8, pady=2)
        self.start_btn = ttk.Button(ctrl_frame, text="启动监控", command=self._start_monitor)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(ctrl_frame, text="停止监控", command=self._stop_monitor, state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        self.status_var = tk.StringVar(value="就绪 — 请先启动 NapCat 并配置连接")
        ttk.Label(ctrl_frame, textvariable=self.status_var, foreground="#0a0").pack(side="right", padx=4)

        # --- 提示 ---
        tip = ttk.Label(
            self,
            text="提示：需先启动 NapCatQQ 并配置正向 WebSocket。点击停止即可断开连接。",
            foreground="#888",
        )
        tip.pack(fill="x", padx=14, pady=2)

        # --- 日志区 ---
        log_frame = ttk.LabelFrame(self, text="运行日志")
        log_frame.pack(fill="both", expand=True, **pad)

        self.log_text = tk.Text(log_frame, wrap="word", state="disabled", font=("Consolas", 9))
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=4)
        scrollbar.pack(side="right", fill="y", pady=4, padx=(0, 6))

        self.log_text.tag_configure("info", foreground="#333")
        self.log_text.tag_configure("forward", foreground="#0066cc")
        self.log_text.tag_configure("error", foreground="#cc0000")

    @staticmethod
    def _parse_group_text(text: str) -> list[int]:
        """从多行文本框解析群号列表。"""
        ids: list[int] = []
        for line in text.splitlines():
            line = line.strip()
            if line and line.isdigit():
                ids.append(int(line))
        return ids

    def _save_config(self) -> None:
        self.config.ws_url = self.ws_var.get().strip()
        self.config.access_token = self.token_var.get().strip()
        self.config.source_groups = self._parse_group_text(self.src_text.get("1.0", "end-1c"))
        self.config.target_groups = self._parse_group_text(self.tgt_text.get("1.0", "end-1c"))
        try:
            self.config.admin_qq = int(self.admin_var.get().strip())
        except ValueError:
            self.config.admin_qq = 0
        self.config.command_enabled = bool(self.command_enabled_var.get())
        self.config.save()
        self.status_var.set("配置已保存")

    def _start_monitor(self) -> None:
        if self._busy:
            return

        ws_url = self.ws_var.get().strip()
        if not ws_url:
            messagebox.showwarning("缺参数", "WS 地址不能为空")
            return

        cmd_enabled = bool(self.command_enabled_var.get())
        src_list = self._parse_group_text(self.src_text.get("1.0", "end-1c"))
        tgt_list = self._parse_group_text(self.tgt_text.get("1.0", "end-1c"))

        # 转发和指令至少启用一项
        need_forward = bool(src_list and tgt_list)
        if not need_forward and not cmd_enabled:
            messagebox.showwarning("缺参数", "请至少填一组源群+目标群，或启用 #指令功能")
            return

        # 更新配置
        self.config.ws_url = ws_url
        self.config.access_token = self.token_var.get().strip()
        self.config.source_groups = src_list
        self.config.target_groups = tgt_list
        try:
            self.config.admin_qq = int(self.admin_var.get().strip())
        except ValueError:
            self.config.admin_qq = 0
        self.config.command_enabled = bool(self.command_enabled_var.get())
        self.config.save()

        self._set_busy(True, "正在连接…")
        threading.Thread(target=self._bg_start, daemon=True).start()

    def _bg_start(self) -> None:
        try:
            self.forwarder = QQForwarder(self.config, self._log_message)
            self.forwarder.start()
            # 等待一小段时间确认连接
            threading.Event().wait(1)
            if self.forwarder.is_running:
                self.after(0, lambda: self._on_start_ok())
            else:
                self.after(0, lambda: self._on_start_failed("连接失败，请检查 NapCat 是否启动"))
        except OneBotError as e:
            self.after(0, lambda: self._on_start_failed(str(e)))
        except Exception as e:
            log.exception("启动监控失败")
            self.after(0, lambda: self._on_start_failed(f"异常: {e}"))

    def _on_start_ok(self) -> None:
        self.status_var.set("监控中…")

    def _on_start_failed(self, msg: str) -> None:
        self._insert_log("error", f"启动失败: {msg}\n")
        self.status_var.set(f"启动失败: {msg}")
        self._set_busy(False)

    def _stop_monitor(self) -> None:
        if self.forwarder:
            self.forwarder.stop()
        self._set_busy(False, "已停止")

    def _log_message(self, level: str, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}\n"
        self.after(0, lambda: self._insert_log(level, line))

    def _insert_log(self, level: str, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line, (level,))
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_busy(self, busy: bool, status: str = "") -> None:
        self._busy = busy
        if status:
            self.status_var.set(status)
        try:
            self.start_btn.configure(state="disabled" if busy else "normal")
            self.stop_btn.configure(state="normal" if busy else "disabled")
        except tk.TclError:
            pass

    def _on_close(self) -> None:
        if self.forwarder and self.forwarder.is_running:
            self.forwarder.stop()
        self._save_config()
        self.destroy()


# ---------- 入口 ----------
def main() -> None:
    setup_logging()
    log.info("QQ消息转发工具启动")
    ForwardApp().mainloop()
    log.info("QQ消息转发工具退出")


if __name__ == "__main__":
    main()
