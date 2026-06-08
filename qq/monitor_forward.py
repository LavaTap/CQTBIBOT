"""QQ群消息监控转发脚本（无头版）- 支持 #指令系统

功能：
  - 从 forward_config.json 自动加载配置
  - 连接 NapCat WebSocket，监控源群消息并转发到目标群
  - 集成 #指令系统（#更新模板课表 / #登录 / #扫码登录 / #更新课表）
  - 自动重连，无需 GUI 操作

使用：
  确保 NapCatQQ 已启动，正向 WebSocket 端口（默认 3001）
  运行：python monitor_forward.py
"""
from __future__ import annotations

import json
import logging
import re
import signal
import sqlite3
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

import websocket

from qq.help_image import get_help_image

# ---------- 配置 ----------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "forward_config.json"
USER_DB_FILE = PROJECT_ROOT / "users.db"
TEMP_CAPTCHA_DIR = PROJECT_ROOT / "_temp"
TEMP_CAPTCHA_DIR.mkdir(exist_ok=True)

# ---------- 日志 ----------
log = logging.getLogger("monitor_forward")


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-5s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(h)


# ---------- 配置加载 ----------
def load_config() -> dict:
    """从 forward_config.json 加载配置。"""
    if not CONFIG_FILE.exists():
        log.error("配置文件 %s 不存在", CONFIG_FILE)
        sys.exit(1)
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return {
            "ws_url": data.get("ws_url", "ws://127.0.0.1:3001"),
            "access_token": data.get("access_token", ""),
            "source_groups": data.get("source_groups", []),
            "target_groups": data.get("target_groups", []),
            "admin_qq": int(data.get("admin_qq", 0)),
            "command_enabled": bool(data.get("command_enabled", True)),
        }
    except (OSError, json.JSONDecodeError, ValueError) as e:
        log.error("加载配置失败: %s", e)
        sys.exit(1)


# ---------- OneBot 错误 ----------
from core.onebot_client import OneBotError


# ---------- login_creds 表：存储 QQ → 学号/密码（用于 #更新 自动重登录）----------
_LOGIN_CREDS_LOCK = threading.Lock()


def _init_login_creds_table() -> None:
    """确保 login_creds 表存在。"""
    with _LOGIN_CREDS_LOCK:
        conn = sqlite3.connect(str(USER_DB_FILE))
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


def save_login_creds(qq: int, student_id: str, password: str) -> None:
    """保存或更新用户的登录凭证（qq → student_id + password）。"""
    _init_login_creds_table()
    from datetime import datetime
    now_iso = datetime.now().isoformat(timespec="seconds")
    with _LOGIN_CREDS_LOCK:
        conn = sqlite3.connect(str(USER_DB_FILE))
        conn.execute("""
            INSERT INTO login_creds (qq, student_id, password, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(qq) DO UPDATE SET
                student_id=CASE WHEN excluded.student_id!='' THEN excluded.student_id ELSE login_creds.student_id END,
                password=CASE WHEN excluded.password!='' THEN excluded.password ELSE login_creds.password END,
                created_at=CASE WHEN excluded.created_at!='' THEN excluded.created_at ELSE login_creds.created_at END
        """, (qq, student_id, password, now_iso))
        conn.commit()
        conn.close()


def get_login_creds(qq: int) -> dict | None:
    """查询用户的登录凭证（student_id, password）。"""
    _init_login_creds_table()
    with _LOGIN_CREDS_LOCK:
        conn = sqlite3.connect(str(USER_DB_FILE))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM login_creds WHERE qq = ?", (qq,)).fetchone()
        conn.close()
        return dict(row) if row else None


def get_user_from_users_db(qq: int) -> dict | None:
    """从 users.db 的 users 表查询用户 token 信息。"""
    _init_login_creds_table()
    with _LOGIN_CREDS_LOCK:
        conn = sqlite3.connect(str(USER_DB_FILE))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM users WHERE qq = ?", (qq,)).fetchone()
        conn.close()
        return dict(row) if row else None


# ---------- OAuth2 常量 ----------
OAUTH2_BASE = "http://szxy.cqtbi.edu.cn/oauth2/v1"
USER_DATA_URL = f"{OAUTH2_BASE}/getUserDataByTicket"
ACCESS_USER_URL = f"{OAUTH2_BASE}/access_user"


# ---------- 指令处理 ----------
class CommandHandler:
    """识别并执行 # 开头的指令（所有QQ用户均可使用）。"""

    CMD_TEMPLATE_SCHEDULE = "#更新模板课表"
    CMD_LOGIN = "#登录"
    CMD_QR_LOGIN = "#扫码登录"
    CMD_UPDATE = "#更新"
    CMD_UPDATE_DEBUG = "#更新调试"
    CMD_UPDATE_SCHEDULE = "#更新课表"
    CMD_EXPORT_SCHEDULE = "#导出课表"
    CMD_WEEK_SCHEDULE = "#本周课表"
    CMD_TODAY_SCHEDULE = "#今日课表"
    CMD_TOMORROW_SCHEDULE = "#明天课表"
    CMD_ER_INFO = "#二课信息"
    CMD_ER_CHART = "#二课图表"
    CMD_VIEW_ER = "#查看二课"
    CMD_ER_LIST = "#二课列表"
    CMD_MY_ER = "#我的二课"
    CMD_PASSWORD_UPDATE = "#密码更新"
    CMD_CANCEL = "#取消"
    CMD_REFRESH_CAPTCHA = "#刷新验证码"
    CMD_QUERY_USERS = "#查询用户"
    CMD_HELP = "#帮助"
    CMD_APPLY = "#报名"
    CMD_RESERVE = "#预约报名"
    CMD_MY_RESERVATIONS = "#我的预约"
    CMD_SIGN_IN = "#签到"
    CMD_SIGN_OUT = "#签退"
    CMD_SCAN_SIGN = "#扫码签到"

    def __init__(self, forwarder: "MonitorForwarder") -> None:
        self._forwarder = forwarder
        self._config = forwarder._config

    def try_handle(self, event: dict) -> bool:
        """尝试处理指令或会话续接。命中返回 True，调用方应跳过转发。"""
        if not self._config["command_enabled"]:
            return False
        if event.get("post_type") != "message":
            return False

        msg_type = event.get("message_type")
        if msg_type not in ("group", "private"):
            return False

        user_id = event.get("user_id", 0)
        group_id = event.get("group_id", 0) if msg_type == "group" else 0

        # 从 message 段提取纯文本（跳过 [CQ:reply],[CQ:at] 等非文本段）
        # 回复/引用消息时 raw_message 会带 [CQ:reply,id=xxx] 前缀，
        # 导致 text.startswith("#") 失败，指令无法识别。
        segments = event.get("message", [])
        if isinstance(segments, list) and segments:
            text = "".join(
                seg.get("data", {}).get("text", "")
                for seg in segments
                if isinstance(seg, dict) and seg.get("type") == "text"
            ).strip()
        else:
            text = (event.get("raw_message") or "").strip()

        # 支持 录/陆 互换（部分用户习惯写 "登陆"）
        normalized = text.replace("登陆", "登录")

        # ---- 1. 检查是否有活跃的会话（多步对话续接）----
        # 1a. 图片消息：仅在等待签到二维码状态下处理
        from core.user_session import SessionStep, get_session
        active = get_session(user_id)
        if active and active.is_active and active.step == SessionStep.WAITING_SIGN_QR_IMAGE:
            from qq.image_intake import extract_image_urls
            urls = extract_image_urls(event)
            if urls:
                from core.user_session import remove_session
                remove_session(user_id)
                log.info("会话续接(图片): user=%s step=WAITING_SIGN_QR_IMAGE url=%s",
                         user_id, urls[0][:80])
                threading.Thread(
                    target=self._cmd_scan_sign,
                    args=(msg_type, group_id, user_id, urls[0]),
                    daemon=True,
                ).start()
                return True

        # 1b. 文本会话续接
        if text and not text.startswith("#"):
            session = get_session(user_id)
            if session and session.is_active:
                log.info("会话续接: user=%s step=%s text=%s", user_id, session.step, text[:20])
                return self._handle_session_input(msg_type, group_id, user_id, text, session)

        # ---- 2. 指令匹配（取 # 开头第一段，兼容 @ 和多余文本）----
        if not text.startswith("#"):
            return False

        # 提取第一个 # 开头的完整指令
        m = re.match(r"(#[^\s]+)", normalized)
        cmd = m.group(1) if m else normalized
        log.info("指令匹配: %s from=%s group=%s", cmd, user_id, group_id)

        # 简单 (cmd → method, extra_args) 路由表：所有人均可使用
        simple_routes: dict[str, tuple[Callable, tuple]] = {
            self.CMD_TEMPLATE_SCHEDULE:  (self._cmd_template_schedule, ()),
            self.CMD_LOGIN:              (self._cmd_login, ()),
            self.CMD_QR_LOGIN:           (self._cmd_qr_login, ()),
            self.CMD_UPDATE:             (self._cmd_update, ()),
            self.CMD_UPDATE_DEBUG:       (self._cmd_update_debug, ()),
            self.CMD_UPDATE_SCHEDULE:    (self._cmd_update_schedule, ()),
            self.CMD_EXPORT_SCHEDULE:    (self._cmd_export_schedule, ()),
            self.CMD_WEEK_SCHEDULE:      (self._cmd_week_schedule, (None,)),
            self.CMD_TODAY_SCHEDULE:     (self._cmd_day_schedule, (0,)),
            self.CMD_TOMORROW_SCHEDULE:  (self._cmd_day_schedule, (1,)),
            self.CMD_ER_INFO:            (self._cmd_er_info, ()),
            self.CMD_ER_CHART:           (self._cmd_er_chart, ()),
            self.CMD_MY_RESERVATIONS:    (self._cmd_my_reservations, ()),
            self.CMD_PASSWORD_UPDATE:    (self._cmd_password_update, ()),
            self.CMD_CANCEL:             (self._cmd_cancel, ()),
        }
        if cmd in simple_routes:
            target, extra = simple_routes[cmd]
            threading.Thread(
                target=target,
                args=(msg_type, group_id, user_id, *extra), daemon=True,
            ).start()
            return True

        # ===== 同步指令 =====
        if cmd == self.CMD_HELP:
            self._show_help(msg_type, group_id, user_id)
            return True

        # ===== 报名（带参数 / 进入对话） =====
        if cmd == self.CMD_APPLY:
            m_apply = re.match(r"#报名\s*(\d{4,})", normalized)
            if m_apply:
                activity_id = m_apply.group(1)
                threading.Thread(
                    target=self._cmd_apply,
                    args=(msg_type, group_id, user_id, activity_id), daemon=True,
                ).start()
                return True
            # 未带 ID → 进入等待活动 ID 的对话状态
            from core.user_session import SessionStep, create_or_get_session
            session = create_or_get_session(user_id, msg_type, group_id)
            session.step = SessionStep.WAITING_APPLY_ACTIVITY_ID
            session.touch()
            self._reply(msg_type, group_id, user_id,
                        "请输入要报名的活动 ID（4 位以上数字），发送 #取消 可取消。")
            return True

        # ===== 预约报名（带参数） =====
        if cmd == self.CMD_RESERVE:
            m_resv = re.match(r"#预约报名\s+(\d{4,})", normalized)
            if m_resv:
                activity_id = m_resv.group(1)
                threading.Thread(
                    target=self._cmd_reserve,
                    args=(msg_type, group_id, user_id, activity_id), daemon=True,
                ).start()
                return True
            self._reply(msg_type, group_id, user_id,
                        "格式：#预约报名 <活动ID>\n例：#预约报名 121499\n"
                        "（仅可预约状态为「报名未开始」的活动）")
            return True

        # ===== 签到 / 签退 =====
        for cmd_text, sign_out in (
            (self.CMD_SIGN_IN, False), (self.CMD_SIGN_OUT, True),
        ):
            if cmd == cmd_text:
                m = re.match(rf"{cmd_text}\s+(\d{{4,}})(?:\s+(\d+))?",
                             normalized)
                if m:
                    activity_id = m.group(1)
                    channel_id = int(m.group(2)) if m.group(2) else 5
                    threading.Thread(
                        target=self._cmd_sign,
                        args=(msg_type, group_id, user_id,
                              activity_id, sign_out, channel_id),
                        daemon=True,
                    ).start()
                    return True
                self._reply(msg_type, group_id, user_id,
                            f"格式：{cmd_text} <活动ID> [渠道]\n"
                            f"例：{cmd_text} 121582  （默认 渠道=5）")
                return True

        # ===== 扫码签到（等待图片）=====
        if cmd == self.CMD_SCAN_SIGN:
            from core.user_session import SessionStep, create_or_get_session
            session = create_or_get_session(user_id, msg_type, group_id)
            session.step = SessionStep.WAITING_SIGN_QR_IMAGE
            session.touch()
            self._reply(msg_type, group_id, user_id,
                        "请发送签到二维码图片（在大屏上拍一下二维码发过来）")
            return True

        # ===== 刷新验证码（按会话状态判断） =====
        if cmd == self.CMD_REFRESH_CAPTCHA:
            from core.user_session import SessionStep, get_session
            session = get_session(user_id)
            if session and session.step in (
                SessionStep.WAITING_CAPTCHA,
                SessionStep.WAITING_APPLY_CAPTCHA,
                SessionStep.WAITING_UPDATE_CAPTCHA,
                SessionStep.WAITING_PASSWORD_UPDATE_CAPTCHA,
            ):
                threading.Thread(
                    target=self._cmd_refresh_captcha,
                    args=(user_id,), daemon=True,
                ).start()
            else:
                self._reply(msg_type, group_id, user_id,
                            "当前没有等待验证码的流程")
            return True

        # ===== 管理员专用 =====
        if cmd == self.CMD_QUERY_USERS:
            if user_id != self._config.get("admin_qq", 0):
                self._reply(msg_type, group_id, user_id, "无权执行此指令")
                return True
            threading.Thread(
                target=self._cmd_query_users,
                args=(msg_type, group_id, user_id), daemon=True,
            ).start()
            return True

        # ===== 管理员专用：二课指令 =====
        if cmd in (self.CMD_VIEW_ER, self.CMD_ER_LIST, self.CMD_MY_ER):
            if user_id != self._config.get("admin_qq", 0):
                self._reply(msg_type, group_id, user_id, "无权执行此指令")
                return True
            targets = {
                self.CMD_VIEW_ER: (self._cmd_view_er, ()),
                self.CMD_ER_LIST: (self._cmd_er_list, ()),
                self.CMD_MY_ER:   (self._cmd_my_er, ()),
            }
            target, extra = targets[cmd]
            threading.Thread(
                target=target,
                args=(msg_type, group_id, user_id, *extra), daemon=True,
            ).start()
            return True

        # ===== 正则匹配指令 =====
        m_week = re.match(r"#第(\d+)周课表", cmd)
        if m_week:
            week_num = int(m_week.group(1))
            threading.Thread(
                target=self._cmd_week_schedule,
                args=(msg_type, group_id, user_id, week_num), daemon=True,
            ).start()
            return True

        m_id = re.match(r"#(\d{4,})$", cmd)
        if m_id:
            activity_id = m_id.group(1)
            threading.Thread(
                target=self._cmd_activity_detail,
                args=(msg_type, group_id, user_id, activity_id), daemon=True,
            ).start()
            return True

        # ---- 3. 未知指令，友好提示 ----
        known = [
            self.CMD_TEMPLATE_SCHEDULE,
            self.CMD_LOGIN,
            self.CMD_QR_LOGIN,
            self.CMD_UPDATE,
            self.CMD_UPDATE_SCHEDULE,
            self.CMD_WEEK_SCHEDULE,
            self.CMD_TODAY_SCHEDULE,
            self.CMD_TOMORROW_SCHEDULE,
            self.CMD_ER_INFO,
            self.CMD_ER_CHART,
            self.CMD_PASSWORD_UPDATE,
            self.CMD_CANCEL,
            self.CMD_REFRESH_CAPTCHA,
            self.CMD_APPLY,
        ]
        if user_id == self._config.get("admin_qq", 0):
            known.append(self.CMD_UPDATE_DEBUG)
            known.append(self.CMD_QUERY_USERS)
            known.append(self.CMD_VIEW_ER)
            known.append(self.CMD_ER_LIST)
            known.append(self.CMD_MY_ER)
        self._reply(msg_type, group_id, user_id,
                    f"未知指令: {cmd}\n\n可用指令: {'、'.join(known)}\n发送 #帮助 查看详情")
        return True

    def _show_help(self, msg_type: str, group_id: int, user_id: int) -> None:
        is_admin = user_id == self._config.get("admin_qq", 0)
        try:
            img_path = get_help_image(is_admin=is_admin)
            self._reply_image(msg_type, group_id, user_id, img_path)
        except FileNotFoundError as e:
            log.error("帮助图片加载失败: %s", e)
            self._reply(msg_type, group_id, user_id, "帮助图片加载失败，请查看日志")

    # ======================== 消息发送工具 ========================

    def _send(self, msg_type: str, group_id: int, user_id: int,
              message: list[dict] | str, *, forward: bool = False) -> None:
        """统一封装群/私聊与普通/合并转发的四种发送场景。失败仅记日志。"""
        try:
            if forward:
                if msg_type == "group":
                    self._forwarder.send_group_forward_msg(group_id, message)
                else:
                    self._forwarder.send_private_forward_msg(user_id, message)
            else:
                if msg_type == "group":
                    self._forwarder.send_group_msg(group_id, message)
                else:
                    self._forwarder.send_private_msg(user_id, message)
        except Exception as e:
            log.error("消息发送失败 (msg_type=%s, forward=%s): %s", msg_type, forward, e)

    def _reply(self, msg_type: str, group_id: int, user_id: int, text: str) -> None:
        """发送纯文本回复。"""
        self._send(msg_type, group_id, user_id, text)

    def _reply_image(self, msg_type: str, group_id: int, user_id: int,
                     image_path: Path, text: str = "") -> None:
        """发送图片消息（可选附带文字）。"""
        if not image_path.exists():
            self._reply(msg_type, group_id, user_id, f"图片文件不存在: {image_path.name}")
            return
        file_uri = image_path.resolve().as_uri()
        parts: list[dict] = []
        if text:
            parts.append({"type": "text", "data": {"text": text + "\n"}})
        parts.append({"type": "image", "data": {"file": file_uri}})
        self._send(msg_type, group_id, user_id, parts)

    def _forward_files(self, msg_type: str, group_id: int, user_id: int,
                       files: list[Path], sender_name: str) -> None:
        """逐个发送本地文件给触发方（不使用合并转发，避免文件内容丢失）。"""
        sent = 0
        for fp in files:
            if not fp.exists():
                log.warning("文件不存在，跳过: %s", fp)
                continue
            msg = [{"type": "file",
                    "data": {"file": fp.resolve().as_uri(), "name": fp.name}}]
            self._send(msg_type, group_id, user_id, msg)
            sent += 1
        if sent == 0:
            self._reply(msg_type, group_id, user_id, "没有可发送的文件")
        else:
            log.info("文件发送成功: %s 个", sent)

    def _send_log_as_forward(self, msg_type: str, group_id: int,
                             user_id: int, title: str, log_text: str) -> None:
        """将日志文本以合并转发形式发送给用户。"""
        nodes = [{
            "type": "node",
            "data": {
                "name": "系统日志",
                "uin": str(self._config["admin_qq"]),
                "content": [
                    {"type": "text", "data": {"text": f"{title}\n\n{log_text}"}},
                ],
            },
        }]
        self._send(msg_type, group_id, user_id, nodes, forward=True)

    # ======================== 指令: #更新模板课表 ========================

    def _cmd_template_schedule(self, msg_type: str, group_id: int, user_id: int) -> None:
        """更新 2403740(QQ:3200418862) 课表并转发模板课表 Excel。"""
        TEMPLATE_QQ = 3200418862
        TEMPLATE_STUDENT_ID = "2403740"
        from core.account_store import find_account_by_qq
        from schedule import JWGLClient, _schedule_path

        try:
            # 1. 查找模板账号（与 #更新课表 同样方式）
            acc = find_account_by_qq(TEMPLATE_QQ)
            if not acc:
                self._reply(msg_type, group_id, user_id,
                            "模板账号(QQ:3200418862)未绑定，请先登录")
                return

            portal_ticket = acc.get("portal_ticket", "")
            if not portal_ticket:
                self._reply(msg_type, group_id, user_id,
                            "模板账号无登录凭证，请重新 #登录")
                return

            self._reply(msg_type, group_id, user_id,
                        "正在更新模板课表(2403740)…")

            # 2. 桥接 JWGL 获取课表（与 #更新课表 相同逻辑）
            client = JWGLClient()
            client.portal_ticket = portal_ticket
            client.bridge()
            schedule = client.get_schedule(student_id=TEMPLATE_STUDENT_ID)

            if not schedule or not schedule.courses:
                self._reply(msg_type, group_id, user_id,
                            "课表为空，可能 ticket 已失效")
                return

            # 3. 保存 JSON + 导出 Excel 并发送
            json_path = _schedule_path("json", student_id=TEMPLATE_STUDENT_ID,
                                       semester=schedule.semester)
            schedule.save_json(json_path)
            xlsx_path = _schedule_path("xlsx", student_id=TEMPLATE_STUDENT_ID,
                                       semester=schedule.semester)
            schedule.save_excel(xlsx_path)

            log.info("模板课表更新成功: %s 门课 → %s",
                     len(schedule.courses), xlsx_path.name)
            self._reply(msg_type, group_id, user_id, "正在发送模板课表…")
            self._forward_files(msg_type, group_id, user_id,
                                [xlsx_path], "模板课表(2403740)")
        except Exception as e:
            log.error("模板课表更新异常: %s", e)
            self._reply(msg_type, group_id, user_id,
                        "更新模板课表过程中出现异常")
            tb = traceback.format_exc()
            self._send_log_as_forward(msg_type, group_id, user_id,
                                      "模板课表错误日志", tb)

    # ======================== 指令: #登录 (SSO 验证码登录) ========================

    def _cmd_login(self, msg_type: str, group_id: int, user_id: int) -> None:
        """多步对话：学号 → 密码 → 验证码 → SSO 登录 → 保存账号 → 自动更新课表。"""
        from core.user_session import SessionStep, create_or_get_session
        session = create_or_get_session(user_id, msg_type, group_id)
        session.step = SessionStep.WAITING_STUDENT_ID
        session.touch()
        self._reply(msg_type, group_id, user_id,
                    "请输入学号（如 2403740）\n\n提示：推荐使用 #扫码登录 更方便")

    def _handle_session_input(self, msg_type: str, group_id: int,
                              user_id: int, text: str, session) -> bool:
        """处理多步对话的用户输入。"""
        from core.user_session import SessionStep

        session.touch()
        step = session.step

        # ---- #登录 流程：学号 → 密码 → 验证码 ----
        if step == SessionStep.WAITING_STUDENT_ID:
            sid = text.strip()
            if not sid.isdigit() or len(sid) < 4:
                self._reply(msg_type, group_id, user_id, "学号格式不正确，请重新输入学号")
                return True
            session.student_id = sid
            session.step = SessionStep.WAITING_PASSWORD
            self._reply(msg_type, group_id, user_id,
                        f"学号已记录: {sid}\n请输入密码（密码不会记录在聊天记录中）")
            return True

        if step == SessionStep.WAITING_PASSWORD:
            if not text:
                self._reply(msg_type, group_id, user_id, "密码不能为空，请重新输入密码")
                return True
            session.password = text
            session.step = SessionStep.WAITING_CAPTCHA
            self._reply(msg_type, group_id, user_id, "正在获取验证码…")
            threading.Thread(target=self._bg_login_captcha,
                             args=(user_id,), daemon=True).start()
            return True

        # ---- 等待验证码的 4 个流程：统一分发到对应后台任务 ----
        captcha_handlers: dict = {
            SessionStep.WAITING_CAPTCHA:                  self._bg_sso_login,
            SessionStep.WAITING_UPDATE_CAPTCHA:           self._bg_update_do_login,
            SessionStep.WAITING_PASSWORD_UPDATE_CAPTCHA:  self._bg_password_update_do_login,
            SessionStep.WAITING_APPLY_CAPTCHA:            self._bg_apply_submit,
        }
        if step in captcha_handlers:
            rcode = text.strip()
            if not rcode:
                self._reply(msg_type, group_id, user_id, "验证码不能为空")
                return True
            threading.Thread(target=captcha_handlers[step],
                             args=(user_id, rcode), daemon=True).start()
            return True

        # ---- #报名 流程：先收活动 ID，再转入正式报名流程 ----
        if step == SessionStep.WAITING_APPLY_ACTIVITY_ID:
            activity_id = text.strip()
            if not re.match(r"^\d{4,}$", activity_id):
                self._reply(msg_type, group_id, user_id,
                            "活动 ID 不正确，请输入 4 位以上的数字，或发送 #取消 取消")
                return True
            # 清除等待状态，避免在 _cmd_apply 内创建会话时冲突
            from core.user_session import remove_session
            remove_session(user_id)
            threading.Thread(target=self._cmd_apply,
                             args=(msg_type, group_id, user_id, activity_id),
                             daemon=True).start()
            return True

        return False

    def _bg_login_captcha(self, user_id: int) -> None:
        """后台：拉取验证码并发送图片到 QQ。"""
        from schedule import JWGLClient
        from core.user_session import get_session
        session = get_session(user_id)
        if not session:
            return

        try:
            client = JWGLClient()
            captcha_bytes = client.begin_sso()
            session.captcha_client = client
            session.acc_key = client.acc_key
            # 保存验证码图片到临时文件
            img_path = TEMP_CAPTCHA_DIR / f"captcha_{user_id}.jpg"
            img_path.write_bytes(captcha_bytes)
            self._reply_image(
                session.msg_type, session.group_id, user_id, img_path,
                "请输入验证码（4位数字），如需刷新请发送 #刷新验证码",
            )
        except Exception as e:
            log.error("获取验证码失败: %s", e)
            self._reply(session.msg_type, session.group_id, user_id,
                        f"获取验证码失败: {e}\n请重新输入 #登录 开始")
            from core.user_session import remove_session
            remove_session(user_id)

    def _bg_sso_login(self, user_id: int, rcode: str) -> None:
        """后台：提交 SSO 登录、门户登录、桥接 JWGL、保存账号、自动更新课表。"""
        from schedule import ScheduleError, _schedule_path
        from core.user_session import get_session, remove_session
        session = get_session(user_id)
        if not session or not session.captcha_client:
            return

        client = session.captcha_client
        msg_type, gid, uid = session.msg_type, session.group_id, user_id
        sid = session.student_id
        pwd = session.password

        try:
            # 1. SSO 登录
            login_result = client.sso_login(sid, pwd, rcode)
            ticket = client.portal_ticket
            access_token = client.access_token
            log.info("SSO 登录成功: %s", sid)

            # 2. 门户登录
            client.portal_login()
            portal_ticket = client.portal_ticket

            # 3. 桥接 JWGL
            client.bridge()

            # 4. 保存账号
            from core.account_store import ensure_account
            ensure_account(
                qq=uid,
                student_id=sid,
                password=pwd,
                access_token=access_token or "",
                portal_ticket=portal_ticket or "",
            )
            # 同步保存密码到 login_creds 表，供 #更新 使用
            save_login_creds(uid, sid, pwd)

            self._reply(msg_type, gid, uid, "登录成功！正在自动更新课表…")

            # 5. 自动获取课表
            try:
                schedule = client.get_schedule(student_id=sid)
                if schedule.courses:
                    json_path = _schedule_path("json", student_id=sid, semester=schedule.semester)
                    xlsx_path = _schedule_path("xlsx", student_id=sid, semester=schedule.semester)
                    schedule.save_json(json_path)
                    try:
                        schedule.save_excel(xlsx_path)
                    except Exception as e:
                        log.error("保存 Excel 失败: %s", e)
                    self._reply(msg_type, gid, uid,
                                f"课表更新成功！共 {len(schedule.courses)} 门课")
                    self._forward_files(msg_type, gid, uid, [xlsx_path],
                                        schedule.student_name or sid)
                else:
                    self._reply(msg_type, gid, uid, "课表为空，请稍后重试")
            except Exception as e:
                log.error("自动更新课表失败: %s", e)
                self._reply(msg_type, gid, uid, f"登录成功，但课表更新失败: {e}")

            log.info("SSO 登录流程完成: qq=%s sid=%s", uid, sid)

        except ScheduleError as e:
            log.error("登录失败: %s", e)
            error_text = f"登录失败: {e}\n\n建议使用 #扫码登录 更便捷"
            self._reply(msg_type, gid, uid, error_text)
        except Exception as e:
            log.error("登录异常: %s", e)
            tb = traceback.format_exc()
            self._reply(msg_type, gid, uid, "登录过程中出现异常，请查看日志")
            self._send_log_as_forward(msg_type, gid, uid, "登录错误日志", tb)
        finally:
            remove_session(user_id)

    # ======================== 指令: #扫码登录 ========================

    def _cmd_qr_login(self, msg_type: str, group_id: int, user_id: int) -> None:
        """生成登录二维码并发送给用户，后台轮询扫码结果。"""
        from core.user_session import SessionStep, create_or_get_session
        session = create_or_get_session(user_id, msg_type, group_id)
        session.step = SessionStep.WAITING_QR_SCAN
        session.touch()

        self._reply(msg_type, group_id, user_id, "正在获取登录二维码…")

        threading.Thread(
            target=self._bg_qr_login,
            args=(user_id,), daemon=True,
        ).start()

    def _bg_qr_login(self, user_id: int) -> None:
        """后台：获取二维码 → 发送 QQ → 轮询扫码 → Portal验证 → 获取PORTAL_TICKET。"""
        from core.user_session import get_session, remove_session
        session = get_session(user_id)
        if not session:
            return

        msg_type, gid, uid = session.msg_type, session.group_id, user_id

        try:
            # 1. 建立 SSO 会话 + 获取 accKey
            from schedule import JWGLClient, ScheduleError
            client = JWGLClient()
            from sso.sso_common import AUTH_URL_TEMPLATE, APPID, DEFAULT_REDIRECT
            auth_url = AUTH_URL_TEMPLATE.format(appid=APPID, redirect_uri=DEFAULT_REDIRECT)
            r = client.session.get(auth_url, allow_redirects=False, timeout=15)
            loc = r.headers.get("Location") or ""
            acc_key = (parse_qs(urlparse(loc).query).get("accKey") or [""])[0]
            if not acc_key:
                raise ScheduleError("auth2orize 未返回 accKey")
            client.acc_key = acc_key
            log.info("qr_login: accKey=%s", acc_key)

            # 2. 获取二维码（支持 JSON 和二进制两种响应）
            erm_url = f"{OAUTH2_BASE}/createErm?seq=99&v={acc_key}"
            r = client.session.get(erm_url, timeout=15)
            r.raise_for_status()

            import base64 as b64mod
            qr_img_bytes = b""
            ct = (r.headers.get("Content-Type") or "").lower()

            if "json" in ct:
                try:
                    erm_data = r.json()
                    log.info("createErm JSON 响应: %s", str(erm_data)[:200])
                    img_field = erm_data.get("img") or erm_data.get("image") or ""
                    if img_field:
                        if "base64," in img_field:
                            img_b64 = img_field.split("base64,")[-1]
                        else:
                            img_b64 = img_field
                        qr_img_bytes = b64mod.b64decode(img_b64)
                except Exception as e:
                    log.warning("createErm JSON 解析失败: %s，二进制回退", e)
                    qr_img_bytes = r.content

            if not qr_img_bytes:
                qr_img_bytes = r.content

            log.info("qr_login: 二维码 %dB", len(qr_img_bytes))

            if len(qr_img_bytes) < 100:
                raise ScheduleError(f"二维码数据异常: {len(qr_img_bytes)}B")

            # 发送二维码图片到 QQ
            img_path = TEMP_CAPTCHA_DIR / f"qr_{user_id}.jpg"
            img_path.write_bytes(qr_img_bytes)
            self._reply_image(msg_type, gid, uid, img_path,
                              "请扫码登录（2分钟内有效）\n发送 #取消 可取消等待")

            # 3. 轮询扫码状态（oauth2ewmSqCheck）
            poll_url = f"{OAUTH2_BASE}/oauth2ewmSqCheck"
            deadline = time.time() + 120
            login_ok = False
            redirect_url = ""
            ticket = ""

            while time.time() < deadline:
                time.sleep(3)
                try:
                    pr = client.session.post(
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
                    log.info("qr_login 轮询: status=%s data=%s",
                             pdata.get("status"), pdata)

                    if pdata.get("success") or str(pdata.get("status")) == "1":
                        login_ok = True
                        redirect_url = pdata.get("redirect_url") or ""
                        ticket = pdata.get("ticket") or pdata.get("portal_ticket") or ""
                        break

                    if str(pdata.get("status")) == "2":
                        self._reply(msg_type, gid, uid, "已扫码，请在手机确认登录…")
                except Exception as e:
                    log.info("轮询扫码状态异常（继续）: %s", e)

            if not login_ok:
                self._reply(msg_type, gid, uid, "扫码登录超时，请重新 #扫码登录")
                return

            # 4. 扫码成功 → 换 access_token → 获取用户信息
            code = (parse_qs(urlparse(redirect_url).query).get("code") or [""])[0]
            portal_ticket_final = ticket
            access_token = ""

            # 4a. 跟随 redirect_url 触发 cookie 设置（非必需）
            if redirect_url:
                try:
                    log.info("跟随扫码重定向: %s", redirect_url[:120])
                    client.session.get(
                        redirect_url, timeout=15,
                        headers={"Referer": f"http://szxy.cqtbi.edu.cn/oauth2/Login.html?accKey={acc_key}"},
                        allow_redirects=True,
                    )
                except Exception as e:
                    log.warning("扫码重定向失败（可忽略）: %s", e)

            # 4b. 用 code 换 access_token
            expires_in = 3600
            if code:
                try:
                    from sso.sso_common import exchange_code_for_token
                    token_result = exchange_code_for_token(code)
                    access_token = token_result.get("access_token", "")
                    expires_in = token_result.get("expires_in", 3600)
                    log.info("access_token 获取: %s", "成功" if access_token else "失败")
                except Exception as e:
                    log.warning("code 换 token 失败: %s", e)

            # 5. 用 access_token 获取用户信息 + 保存 + 自动更新课表
            now_iso = datetime.now().isoformat(timespec="seconds")
            expires_at = int(time.time()) + expires_in if access_token else 0

            user_info = self._fetch_user_info(access_token) if access_token else None
            if user_info:
                student_id = user_info.get("userloginid", "")
                realname = user_info.get("userrealname", "")
                depaname = user_info.get("userdepaname", "")
                dept_id = user_info.get("userdeptid", "")
                is_teacher = user_info.get("teacher") == "1"
                info_text = (
                    f"扫码登录成功！\n"
                    f"姓名: {realname}\n"
                    f"学号/工号: {student_id}\n"
                    f"部门: {depaname}\n"
                    f"身份: {'教师' if is_teacher else '学生'}"
                )
                self._reply(msg_type, gid, uid, info_text)
                from core.account_store import ensure_account
                ensure_account(
                    qq=uid, student_id=student_id, password="",
                    access_token=access_token, portal_ticket=portal_ticket_final, expires_at=expires_at,
                )
                log.info("扫码登录成功: qq=%s sid=%s", uid, student_id)
                self._reply(msg_type, gid, uid, "正在自动更新课表…")
                self._auto_update_schedule_after_login(
                    msg_type, gid, uid, client, portal_ticket_final, student_id,
                )
            else:
                self._reply(msg_type, gid, uid, "登录成功！（未获取到详细用户信息）")
                from core.account_store import ensure_account
                ensure_account(
                    qq=uid, student_id="", password="",
                    access_token=access_token, portal_ticket=portal_ticket_final, expires_at=expires_at,
                )

        except ScheduleError as e:
            log.error("qr_login 失败: %s", e)
            self._reply(msg_type, gid, uid, f"扫码登录失败: {e}")
        except Exception as e:
            log.error("qr_login 异常: %s", e)
            tb = traceback.format_exc()
            self._reply(msg_type, gid, uid, f"扫码登录异常: {e}")
            self._send_log_as_forward(msg_type, gid, uid, "扫码登录错误日志", tb)
        finally:
            remove_session(user_id)

    # ---- 辅助：access_user 获取用户信息 ----

    @staticmethod
    def _fetch_user_info(access_token: str) -> dict | None:
        """调用 access_user 接口获取用户信息。"""
        if not access_token:
            return None
        try:
            import requests
            r = requests.post(ACCESS_USER_URL,
                              data={"access_token": access_token}, timeout=10)
            r.raise_for_status()
            data = r.json()
            if data.get("success"):
                return data
            log.warning("access_user 返回失败: %s", data)
        except Exception as e:
            log.warning("获取用户信息失败: %s", e)
        return None

    # ---- 辅助：登录后自动更新课表 ----

    def _auto_update_schedule_after_login(
        self, msg_type: str, group_id: int, user_id: int,
        client, portal_ticket: str, student_id: str,
    ) -> None:
        """登录成功后自动执行课表更新逻辑。"""
        from schedule import ScheduleError, _schedule_path
        try:
            if not portal_ticket:
                self._reply(msg_type, group_id, user_id, "无登录凭证，无法更新课表")
                return
            client.portal_ticket = portal_ticket
            # 必须先 portal_login() 设置 PORTAL_TICKET cookie，否则 bridge 会失败
            client.portal_login()
            client.bridge()
            schedule = client.get_schedule(student_id=student_id)
            if schedule and schedule.courses:
                xlsx_path = _schedule_path(
                    "xlsx", student_id=student_id, semester=schedule.semester)
                schedule.save_json(_schedule_path(
                    "json", student_id=student_id, semester=schedule.semester))
                try:
                    schedule.save_excel(xlsx_path)
                except Exception:
                    pass
                self._reply(msg_type, group_id, user_id,
                            f"课表更新成功！共 {len(schedule.courses)} 门课")
                self._forward_files(msg_type, group_id, user_id, [xlsx_path],
                                    schedule.student_name or student_id)
            else:
                self._reply(msg_type, group_id, user_id, "课表为空")
        except ScheduleError as e:
            log.error("登录后更新课表失败: %s", e)
            self._reply(msg_type, group_id, user_id, f"课表更新失败: {e}")
        except Exception as e:
            log.error("登录后更新课表异常: %s", e)
            self._reply(msg_type, group_id, user_id, "课表更新异常，请稍后使用 #更新课表")

    # ======================== 指令: #密码更新（用密码重登刷新凭证）========================

    def _cmd_password_update(self, msg_type: str, group_id: int, user_id: int) -> None:
        """#密码更新：用保存的学号+密码重新 SSO 登录，仅更新 token/ticket。"""
        from schedule import JWGLClient
        from core.user_session import SessionStep, create_or_get_session

        try:
            # 1. 查用户
            user = get_user_from_users_db(user_id)
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

            # 2. 查保存的密码
            creds = get_login_creds(user_id)
            if not creds or not creds.get("password"):
                # 兜底查 accounts.json
                from core.account_store import find_account_by_qq
                acc = find_account_by_qq(user_id)
                if acc and acc.get("password"):
                    save_login_creds(user_id, acc["student_id"], acc["password"])
                    creds = {"student_id": acc["student_id"], "password": acc["password"]}
                else:
                    from sso.sso_common import load_data
                    data = load_data()
                    for a in data.get("accounts", []):
                        if a.get("student_id") == student_id and a.get("password"):
                            save_login_creds(user_id, student_id, a["password"])
                            creds = {"student_id": student_id, "password": a["password"]}
                            break
            if not creds or not creds.get("password"):
                self._reply(msg_type, group_id, user_id,
                            "未找到您的密码，请先使用 #登录（密码+验证码）登录一次")
                return

            # 3. 获取验证码
            self._reply(msg_type, group_id, user_id, "正在获取验证码，请稍后输入…")
            session = create_or_get_session(user_id, msg_type, group_id)
            session.step = SessionStep.WAITING_PASSWORD_UPDATE_CAPTCHA
            session.student_id = creds["student_id"]
            session.password = creds["password"]
            session.touch()

            captcha_client = JWGLClient()
            captcha_bytes = captcha_client.begin_sso()
            session.captcha_client = captcha_client
            session.acc_key = captcha_client.acc_key
            img_path = TEMP_CAPTCHA_DIR / f"pwd_update_{user_id}.jpg"
            img_path.write_bytes(captcha_bytes)
            self._reply_image(
                msg_type, group_id, user_id, img_path,
                f"正在为 {student_id} 重新登录，请输入验证码（4位数字）",
            )

        except Exception as e:
            log.error("#密码更新 异常: %s", e)
            self._reply(msg_type, group_id, user_id,
                        f"密码更新失败: {e}")
            from core.user_session import remove_session
            remove_session(user_id)

    def _bg_password_update_do_login(self, user_id: int, rcode: str) -> None:
        """#密码更新 验证码提交后：SSO 重登 → 保存 token/ticket → 回复。"""
        from datetime import datetime
        from schedule import ScheduleError
        from core.user_session import get_session, remove_session
        from core.account_store import ensure_account

        session = get_session(user_id)
        if not session or not session.captcha_client:
            return

        client = session.captcha_client
        msg_type, gid, uid = session.msg_type, session.group_id, user_id
        student_id = session.student_id
        password = session.password

        try:
            # 1. SSO 登录
            login_result = client.sso_login(student_id, password, rcode)
            portal_ticket = client.portal_ticket
            access_token = client.access_token
            log.info("#密码更新 重新登录成功: %s", student_id)

            # 2. 门户登录（设 PORTAL_TICKET cookie）
            client.portal_login()
            ticket = client.portal_ticket

            # 3. 保存 token/ticket 到 accounts.json
            ensure_account(
                qq=uid,
                student_id=student_id,
                password=password,
                access_token=access_token or "",
                portal_ticket=ticket or "",
            )

            # 4. 同步到 users.db
            try:
                conn = sqlite3.connect(str(USER_DB_FILE))
                now_iso = datetime.now().isoformat(timespec="seconds")
                expires_at = int(time.time()) + 3600 if access_token else 0
                conn.execute("""
                    INSERT INTO users (qq, student_id, access_token, portal_ticket,
                                       expires_at, last_login, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(qq) DO UPDATE SET
                        access_token=excluded.access_token,
                        portal_ticket=excluded.portal_ticket,
                        expires_at=excluded.expires_at,
                        last_login=excluded.last_login
                """, (uid, student_id, access_token or "", ticket or "",
                      expires_at, now_iso, now_iso))
                conn.commit()
                conn.close()
            except Exception as e:
                log.warning("写入 users.db 失败（可忽略）: %s", e)

            self._reply(msg_type, gid, uid,
                        f"更新成功！学号：{student_id}")

        except ScheduleError as e:
            log.error("#密码更新 登录失败: %s", e)
            self._reply(msg_type, gid, uid,
                        f"登录失败: {e}，请重新 #登录")
        except Exception as e:
            log.error("#密码更新 异常: %s", e)
            self._reply(msg_type, gid, uid,
                        f"密码更新失败: {e}，请重新 #登录")
        finally:
            remove_session(user_id)

    # ======================== 指令: #更新（验证过期+状态消息+静默更新）========================

    def _cmd_update(self, msg_type: str, group_id: int, user_id: int) -> None:
        """#更新：验证 token 时效 → 发送状态消息 → 静默更新课表+二课。"""
        from datetime import datetime
        from schedule import ScheduleError, _schedule_path, _load_schedule_from_json

        try:
            # 1. 查用户（users.db → accounts.json）
            user = get_user_from_users_db(user_id)
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

            # 2. 检查 token 是否过期
            now_ts = int(time.time())
            if expires_at and expires_at < now_ts:
                status = "已过期"
            elif not access_token and not portal_ticket:
                status = "无凭证"
            else:
                status = "有效"

            # 3. 格式化 last_login 时间
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

            if status != "有效":
                log.info("#更新 token 已过期，静默跳过更新: qq=%s sid=%s", user_id, student_id)
                return

            # 4. 静默更新课表（仅存 JSON，不发送任何消息）
            if portal_ticket:
                try:
                    from schedule import JWGLClient
                    client = JWGLClient()
                    client.portal_ticket = portal_ticket
                    client.portal_login()
                    client.bridge()
                    schedule = client.get_schedule(student_id=student_id)
                    if schedule and schedule.courses:
                        json_path = _schedule_path("json", student_id=student_id, semester=schedule.semester)
                        schedule.save_json(json_path)
                        log.info("#更新 课表已静默更新: %s %d门课", student_id, len(schedule.courses))
                except Exception as e:
                    log.warning("#更新 课表静默更新失败: %s", e)

            # 5. 静默更新二课信息（仅存 DB，不发送任何消息）
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
                    log.info("#更新 二课信息已静默更新: %s", student_id)
                except Exception as e:
                    log.warning("#更新 二课静默更新失败: %s", e)

        except Exception as e:
            log.error("#更新 异常: %s", e)
            self._reply(msg_type, group_id, user_id, f"更新失败: {e}")

    # ======================== 指令: #更新调试（自动重登录兜底）========================

    def _cmd_update_debug(self, msg_type: str, group_id: int, user_id: int) -> None:
        """#更新：先从 users.db 查 token → 拉课表 → 失败则自动重登录 → 再拉课表。

        流程：
          1) 读 users.db → users 表 → 取 portal_ticket
          2) 桥接 JWGL → 获取课表 → 成功则结束
          3) 失败 → 读 login_creds 表 → 取学号/密码
          4) 有凭证 → 验证码登录 → 用户输验证码 → 重新登录 → 重试课表
          5) 无凭证 → 提示用户先 #登录
        """
        from schedule import JWGLClient, ScheduleError, _schedule_path

        try:
            self._reply(msg_type, group_id, user_id, "正在查询登录凭证…")

            # 1. 先查 users.db
            user = get_user_from_users_db(user_id)
            if not user:
                # 兜底查 accounts.json
                from core.account_store import find_account_by_qq
                acc = find_account_by_qq(user_id)
                if not acc:
                    self._reply(msg_type, group_id, user_id,
                                "未找到您的账号信息，请先使用 #登录 或 #扫码登录")
                    return
                # 把 accounts.json 的数据当 user 用
                user = {
                    "qq": user_id,
                    "student_id": acc.get("student_id", ""),
                    "portal_ticket": acc.get("portal_ticket", ""),
                    "access_token": acc.get("access_token", ""),
                    "expires_at": acc.get("expires_at", 0),
                }

            student_id = user.get("student_id", "")
            portal_ticket = user.get("portal_ticket", "")
            if not student_id:
                self._reply(msg_type, group_id, user_id,
                            "未关联学号，请先使用 #登录")
                return

            self._reply(msg_type, group_id, user_id, f"正在拉取 {student_id} 的课表…")

            # 2. 尝试用已有凭证更新课表
            client = JWGLClient()
            success = self._try_update_schedule(
                msg_type, group_id, user_id, client, portal_ticket, student_id,
            )
            if success:
                # 调试 #我的二课、#二课列表、#查看二课
                threading.Thread(
                    target=self._cmd_my_er,
                    args=(msg_type, group_id, user_id), daemon=True,
                ).start()
                threading.Thread(
                    target=self._cmd_er_list,
                    args=(msg_type, group_id, user_id), daemon=True,
                ).start()
                threading.Thread(
                    target=self._cmd_view_er,
                    args=(msg_type, group_id, user_id), daemon=True,
                ).start()
                return  # 更新成功，结束

            # 3. 更新失败 → 读取 login_creds 中的密码 → 自动重登录
            self._reply(msg_type, group_id, user_id,
                        "登录凭证过期，正在尝试自动重新登录…")

            creds = get_login_creds(user_id)
            if not creds or not creds.get("password"):
                # 兜底1: 按 qq 查 accounts.json
                from core.account_store import find_account_by_qq
                acc = find_account_by_qq(user_id)
                if acc and acc.get("password"):
                    save_login_creds(user_id, acc.get("student_id", ""), acc.get("password", ""))
                    creds = {"student_id": acc.get("student_id", ""), "password": acc.get("password", "")}
                else:
                    # 兜底2: accounts.json 可能没有 qq 字段，按 student_id 交叉查找
                    from sso.sso_common import load_data
                    data = load_data()
                    sid = user.get("student_id", "")
                    for a in data.get("accounts", []):
                        if a.get("student_id") == sid and a.get("password"):
                            save_login_creds(user_id, sid, a["password"])
                            creds = {"student_id": sid, "password": a["password"]}
                            break
                if not creds or not creds.get("password"):
                    self._reply(msg_type, group_id, user_id,
                                "未找到您的登录密码。\n"
                                "请先使用 #登录（密码+验证码）登录一次，"
                                "之后 #更新 即可自动重登录。")
                    return

            # 4. 获取验证码，开始重登录流程
            self._reply(msg_type, group_id, user_id,
                        "正在获取验证码，请稍后输入…")

            from core.user_session import SessionStep, create_or_get_session, get_session
            session = create_or_get_session(user_id, msg_type, group_id)
            session.step = SessionStep.WAITING_UPDATE_CAPTCHA
            session.student_id = creds.get("student_id", "")
            session.password = creds.get("password", "")
            session.touch()

            # 拉验证码（复用 JWGLClient.begin_sso）
            try:
                captcha_client = JWGLClient()
                captcha_bytes = captcha_client.begin_sso()
                session.captcha_client = captcha_client
                session.acc_key = captcha_client.acc_key
                img_path = TEMP_CAPTCHA_DIR / f"update_{user_id}.jpg"
                img_path.write_bytes(captcha_bytes)
                self._reply_image(
                    msg_type, group_id, user_id, img_path,
                    f"正在为 {student_id} 重新登录，请输入验证码（4位数字）",
                )
            except Exception as e:
                log.error("获取验证码失败: %s", e)
                self._reply(msg_type, group_id, user_id,
                            f"获取验证码失败: {e}\n请稍后重试 #更新")
                from core.user_session import remove_session
                remove_session(user_id)

        except Exception as e:
            log.error("#更新 异常: %s", e)
            tb = traceback.format_exc()
            self._reply(msg_type, group_id, user_id, "#更新 过程中出现异常")
            self._send_log_as_forward(msg_type, group_id, user_id, "#更新 错误日志", tb)

    def _try_update_schedule(
        self, msg_type: str, group_id: int, user_id: int,
        client, portal_ticket: str, student_id: str,
    ) -> bool:
        """尝试用已有 token 更新课表。成功返回 True，失败返回 False。"""
        from schedule import ScheduleError, _schedule_path
        try:
            if not portal_ticket:
                return False
            client.portal_ticket = portal_ticket
            client.portal_login()
            client.bridge()
            schedule = client.get_schedule(student_id=student_id)
            if schedule and schedule.courses:
                # 仅更新JSON，不导出Excel
                json_path = _schedule_path(
                    "json", student_id=student_id, semester=schedule.semester)
                schedule.save_json(json_path)
                self._reply(msg_type, group_id, user_id,
                            f"课表更新成功！共 {len(schedule.courses)} 门课")

                # 自动渲染本周课表
                self._cmd_week_schedule(msg_type, group_id, user_id, None)

                # 自动更新二课信息
                self._cmd_er_info(msg_type, group_id, user_id)
                return True
            else:
                self._reply(msg_type, group_id, user_id, "课表为空")
                return True
        except ScheduleError as e:
            log.warning("凭证失效，需要重新登录: %s", e)
            return False
        except Exception as e:
            log.warning("更新课表异常（需要重新登录）: %s", e)
            return False

    def _bg_update_do_login(self, user_id: int, rcode: str) -> None:
        """#更新 流程中：验证码提交 → SSO 重新登录 → 保存 token → 再更新课表。"""
        from datetime import datetime
        from schedule import ScheduleError, _schedule_path
        from core.user_session import get_session, remove_session
        from core.account_store import ensure_account

        session = get_session(user_id)
        if not session or not session.captcha_client:
            return

        client = session.captcha_client
        msg_type, gid, uid = session.msg_type, session.group_id, user_id
        student_id = session.student_id
        password = session.password

        try:
            # 1. SSO 登录（用验证码）
            login_result = client.sso_login(student_id, password, rcode)
            portal_ticket = client.portal_ticket
            access_token = client.access_token
            log.info("#更新 重新登录成功: %s", student_id)

            # 2. 门户登录
            client.portal_login()
            ticket = client.portal_ticket

            # 3. 保存账号到 accounts.json
            ensure_account(
                qq=uid,
                student_id=student_id,
                password=password,
                access_token=access_token or "",
                portal_ticket=ticket or "",
            )

            # 4. 也保存到 users.db
            try:
                conn = sqlite3.connect(str(USER_DB_FILE))
                now_iso = datetime.now().isoformat(timespec="seconds")
                expires_at = int(time.time()) + 3600 if access_token else 0
                conn.execute("""
                    INSERT INTO users (qq, student_id, access_token, portal_ticket,
                                       expires_at, last_login, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(qq) DO UPDATE SET
                        access_token=excluded.access_token,
                        portal_ticket=excluded.portal_ticket,
                        expires_at=excluded.expires_at,
                        last_login=excluded.last_login
                """, (uid, student_id, access_token or "", ticket or "",
                      expires_at, now_iso, now_iso))
                conn.commit()
                conn.close()
            except Exception as e:
                log.warning("写入 users.db 失败（可忽略）: %s", e)

            self._reply(msg_type, gid, uid, "重新登录成功！正在更新课表…")

            # 5. 桥接 JWGL + 获取课表
            try:
                client.bridge()
                schedule = client.get_schedule(student_id=student_id)
                if schedule and schedule.courses:
                    # 仅更新JSON，不导出Excel
                    json_path = _schedule_path(
                        "json", student_id=student_id, semester=schedule.semester)
                    schedule.save_json(json_path)
                    self._reply(msg_type, gid, uid,
                                f"课表更新成功！共 {len(schedule.courses)} 门课")

                    # 自动渲染本周课表
                    self._cmd_week_schedule(msg_type, gid, uid, None)

                    # 自动更新二课信息
                    self._cmd_er_info(msg_type, gid, uid)
                else:
                    self._reply(msg_type, gid, uid, "课表为空")
            except Exception as e:
                log.error("重新登录后更新课表失败: %s", e)
                self._reply(msg_type, gid, uid,
                            f"课表更新失败: {e}\n请稍后手动执行 #更新课表")

        except ScheduleError as e:
            log.error("#更新 登录失败: %s", e)
            self._reply(msg_type, gid, uid,
                        f"重新登录失败: {e}\n请手动使用 #登录")
        except Exception as e:
            log.error("#更新 登录异常: %s", e)
            tb = traceback.format_exc()
            self._reply(msg_type, gid, uid, "#更新 登录过程出现异常")
            self._send_log_as_forward(msg_type, gid, uid, "#更新 错误日志", tb)
        finally:
            remove_session(user_id)

    # ======================== 指令: 课表图片 ========================

    def _load_user_schedule(self, msg_type: str, group_id: int, user_id: int):
        """根据 QQ 号查找学号并加载本地 JSON 课表。返回 (Schedule, student_id) 或 None。"""
        try:
            from core.account_store import find_account_by_qq
            from schedule import _load_schedule_from_json

            acc = find_account_by_qq(user_id)
            if not acc:
                self._reply(msg_type, group_id, user_id,
                            "未找到您的账号信息，请先使用 #登录 或 #扫码登录")
                return None
            student_id = acc.get("student_id", "")
            if not student_id:
                self._reply(msg_type, group_id, user_id,
                            "未关联学号，请先使用 #登录")
                return None

            # 尝试加载最新的课表 JSON
            sid_dir = Path(PROJECT_ROOT) / "schedules" / student_id
            if not sid_dir.is_dir():
                self._reply(msg_type, group_id, user_id,
                            "尚未下载课表，请先使用 #更新课表")
                return None

            json_files = sorted(sid_dir.glob("*.json"))
            if not json_files:
                self._reply(msg_type, group_id, user_id,
                            "尚未下载课表，请先使用 #更新课表")
                return None

            latest_json = json_files[-1]
            schedule = _load_schedule_from_json(latest_json)
            if not schedule or not schedule.courses:
                self._reply(msg_type, group_id, user_id,
                            "课表数据为空，请先使用 #更新课表")
                return None

            return schedule, student_id
        except Exception as e:
            log.error("_load_user_schedule 异常: %s", e)
            self._reply(msg_type, group_id, user_id,
                        f"加载课表数据失败: {e}")
            return None

    def _cmd_week_schedule(self, msg_type: str, group_id: int,
                           user_id: int, week_num: int | None) -> None:
        """生成并发送周课表图片。week_num=None 时自动计算当前周。"""
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

            # 检查缓存
            if is_cache_valid(student_id, semester, week_num):
                png_path = get_cached_image_path(student_id, week_num)
                self._reply_image(msg_type, group_id, user_id, png_path,
                                  f"第{week_num}周课表（缓存）")
                return

            # 渲染新图片
            png_path = render_schedule_image(schedule, week_num, student_id)
            self._reply_image(msg_type, group_id, user_id, png_path,
                              f"第{week_num}周课表")
        except Exception as e:
            log.error("_cmd_week_schedule 异常: %s", e)
            self._reply(msg_type, group_id, user_id,
                        f"课表图片生成失败: {e}")

    def _cmd_day_schedule(self, msg_type: str, group_id: int,
                          user_id: int, day_offset: int) -> None:
        """生成并发送单日课表卡片。day_offset=0 今天，1 明天。"""
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

            # 计算目标星期（1=周一 … 7=周日）
            from datetime import date, timedelta
            today = date.today()
            target = today + timedelta(days=day_offset)
            day = target.isoweekday()  # 1=Mon … 7=Sun

            # 如果跨周了，调整 week_num
            if day_offset > 0 and target.isocalendar()[1] != today.isocalendar()[1]:
                week_num += 1

            label = "今日" if day_offset == 0 else "明日"
            png_path = render_day_image(schedule, week_num, day, student_id)
            self._reply_image(msg_type, group_id, user_id, png_path,
                              f"{label}课表")
        except Exception as e:
            log.error("渲染单日课表卡片失败: %s", e)
            self._reply(msg_type, group_id, user_id,
                        f"课表图片生成失败: {e}")

    # ======================== 指令: #更新课表 ========================

    def _cmd_update_schedule(self, msg_type: str, group_id: int, user_id: int) -> None:
        """核对用户 QQ -> 拉取该用户的 token -> 获取课表 -> 更新 JSON -> 自动渲染本周课表。"""
        from core.account_store import find_account_by_qq
        from schedule import JWGLClient, ScheduleError, _schedule_path

        try:
            # 1. 查找该 QQ 对应的账号
            acc = find_account_by_qq(user_id)
            if not acc:
                self._reply(msg_type, group_id, user_id,
                            "未找到您的账号信息，请先使用 #登录 或 #扫码登录")
                return

            student_id = acc.get("student_id", "")
            portal_ticket = acc.get("portal_ticket", "")
            if not portal_ticket:
                self._reply(msg_type, group_id, user_id,
                            "您的账号无登录凭证，请重新使用 #登录 或 #扫码登录")
                return

            self._reply(msg_type, group_id, user_id, "正在拉取您的课表…")

            # 2. 桥接 JWGL 获取课表
            client = JWGLClient()
            client.portal_ticket = portal_ticket

            # 先 portal_login() 设 PORTAL_TICKET cookie，再 bridge
            try:
                client.portal_login()
                client.bridge()
            except Exception:
                self._reply(msg_type, group_id, user_id,
                            "登录已过期，请重新使用 #登录 或 #扫码登录")
                return

            # 3. 获取课表
            schedule = client.get_schedule(student_id=student_id)

            if not schedule or not schedule.courses:
                self._reply(msg_type, group_id, user_id, "课表为空，可能 ticket 已失效")
                return

            # 4. 仅更新JSON，不导出Excel
            json_path = _schedule_path("json", student_id=student_id, semester=schedule.semester)
            schedule.save_json(json_path)

            log.info("课表拉取成功: %s 门课 → %s", len(schedule.courses), json_path.name)
            self._reply(msg_type, group_id, user_id,
                        f"课表更新成功！共 {len(schedule.courses)} 门课")

            # 5. 自动渲染并发送本周课表
            self._cmd_week_schedule(msg_type, group_id, user_id, None)

        except ScheduleError as e:
            log.error("课表更新失败: %s", e)
            self._reply(msg_type, group_id, user_id,
                        f"课表更新失败: {e}")
        except Exception as e:
            log.error("课表更新异常: %s", e)
            tb = traceback.format_exc()
            self._reply(msg_type, group_id, user_id, "更新课表过程中出现异常")
            self._send_log_as_forward(msg_type, group_id, user_id, "课表更新错误日志", tb)


    # ======================== 指令: #导出课表 ========================

    def _cmd_export_schedule(self, msg_type: str, group_id: int, user_id: int) -> None:
        """导出最新课表为Excel文件并发送。"""
        try:
            from core.account_store import find_account_by_qq
            from schedule import _schedule_path, _load_schedule_from_json

            acc = find_account_by_qq(user_id)
            if not acc:
                self._reply(msg_type, group_id, user_id,
                            "未找到您的账号信息，请先使用 #登录 或 #扫码登录")
                return

            student_id = acc.get("student_id", "")
            if not student_id:
                self._reply(msg_type, group_id, user_id,
                            "未关联学号，请先使用 #登录")
                return

            # 读取最新JSON课表
            sid_dir = Path(PROJECT_ROOT) / "schedules" / student_id
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
                log.error("导出 Excel 失败: %s", e)
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
            log.error("导出课表异常: %s", e)
            tb = traceback.format_exc()
            self._reply(msg_type, group_id, user_id, "导出课表过程中出现异常")
            self._send_log_as_forward(msg_type, group_id, user_id, "导出课表错误日志", tb)


    # ======================== 指令: #二课信息 ========================

    def _cmd_er_info(self, msg_type: str, group_id: int, user_id: int) -> None:
        """查询第二课堂信息：活动/签到/总结/社团/积分，存入 users.db。

        优先从 accounts.json 读凭证；支持 portal_ticket/access_token 自动桥接获得 SSID。
        """
        try:
            from core.account_store import find_account_by_qq
            acc = find_account_by_qq(user_id)
            if not acc:
                self._reply(msg_type, group_id, user_id,
                            "未找到您的账号，请先使用 #登录 或 #扫码登录")
                return

            if not acc.get("portal_ticket") and not acc.get("access_token"):
                self._reply(msg_type, group_id, user_id,
                            "缺少登录凭证，请重新 #登录 或 #扫码登录")
                return

            self._reply(msg_type, group_id, user_id, "正在查询第二课堂信息…")

            from secondclass.secondclass_tool import (
                SecondClassAuthError, fetch_and_save_secondclass_info,
                format_secondclass_summary,
            )

            user = {
                "student_id": acc.get("student_id", ""),
                "realname": acc.get("realname", ""),
                "dept_name": acc.get("dept_name") or acc.get("userdepaname", ""),
                "college": acc.get("college", ""),
                "major": acc.get("major", ""),
                "portal_ticket": acc.get("portal_ticket", ""),
                "access_token": acc.get("access_token", ""),
                "expires_at": acc.get("expires_at", 0),
            }

            data = fetch_and_save_secondclass_info(user_id, user)
            self._reply(msg_type, group_id, user_id,
                        format_secondclass_summary(data))

            # 同时生成并发送图表
            try:
                from secondclass.secondclass_image import render_secondclass_chart
                chart_path = render_secondclass_chart(data)
                self._reply_image(msg_type, group_id, user_id, chart_path)
            except Exception as chart_e:
                log.warning("二课图表生成失败（不影响文字查询）: %s", chart_e)

            log.info("二课信息查询成功: qq=%s", user_id)

        except SecondClassAuthError as e:
            log.warning("二课桥接失败: %s", e)
            self._reply(msg_type, group_id, user_id, str(e))
        except Exception as e:
            log.error("二课信息查询异常: %s", e)
            tb = traceback.format_exc()
            self._reply(msg_type, group_id, user_id, "查询二课信息失败")
            self._send_log_as_forward(msg_type, group_id, user_id,
                                      "二课错误日志", tb)

    # ======================== 指令: #二课图表 ========================

    def _cmd_er_chart(self, msg_type: str, group_id: int, user_id: int) -> None:
        """#二课图表：直接从 DB 缓存读取数据，渲染图表发送，不请求 API。"""
        try:
            from secondclass.secondclass_tool import SecondClassDB
            from secondclass.secondclass_image import render_secondclass_chart

            # 从 DB 缓存读取（不调用 API）
            data = SecondClassDB().get_by_qq(user_id)
            if not data:
                self._reply(msg_type, group_id, user_id,
                            "暂无二课缓存数据，请先使用 #二课信息 查询")
                return

            chart_path = render_secondclass_chart(dict(data))
            self._reply_image(msg_type, group_id, user_id, chart_path,
                              "📊 你的第二课堂信息图表")
            log.info("二课图表生成成功: qq=%s student_id=%s", user_id, data.get("student_id", "?"))

        except Exception as e:
            log.error("二课图表生成异常: %s", e)
            self._reply(msg_type, group_id, user_id, "生成二课图表失败")

    # ======================== 指令: #查看二课 ========================

    def _cmd_view_er(self, msg_type: str, group_id: int, user_id: int) -> None:
        """#查看二课：渲染 master 表对应 QQ 的所有活动卡片并发送。"""
        from pathlib import Path
        try:
            # ── 1. 查找 student_id（优先二课DB缓存 → users.db → accounts.json）──
            from secondclass.secondclass_tool import SecondClassDB, SecondClassMasterDB, SecondClassActivityDetailDB
            sc_data = SecondClassDB().get_by_qq(user_id)
            student_id = ""
            if sc_data:
                student_id = sc_data.get("student_id", "")
            if not student_id:
                user = get_user_from_users_db(user_id)
                if user:
                    student_id = user.get("student_id", "")
            if not student_id:
                from core.account_store import find_account_by_qq
                acc = find_account_by_qq(user_id)
                if acc:
                    student_id = acc.get("student_id", "")
            if not student_id:
                self._reply(msg_type, group_id, user_id,
                            "未关联学号，请先使用 #登录 或 #扫码登录")
                return

            # ── 2. 从 master 表读取所有活动 ──
            master_db = SecondClassMasterDB()
            detail_db = SecondClassActivityDetailDB()
            activities = master_db.get_by_student_id(student_id)
            if not activities:
                self._reply(msg_type, group_id, user_id, "您没有二课活动记录")
                return

            # 2. 先通知用户数量
            total = len(activities)
            self._reply(msg_type, group_id, user_id, f"共 {total} 个活动，开始渲染卡片…")

            # 3. 批量渲染活动卡片
            from secondclass.secondclass_activity_chart import render_activity_card
            import time as _time

            # 使用固定的输出目录
            chart_dir = Path(__file__).resolve().parent.parent / "schedules" / "_activity_charts"
            chart_dir.mkdir(parents=True, exist_ok=True)

            card_paths: list[Path] = []
            for i, act in enumerate(activities):
                try:
                    # 优先从 detail 表读取详情
                    aid = act.get("activity_id", "")
                    detail = detail_db.get_by_activity_id(aid)
                    if detail:
                        card_data = dict(detail)
                    else:
                        card_data = dict(act)
                    # 确保关键字段
                    card_data["activity_id"] = aid
                    card_data["activity_name"] = card_data.get("activity_name") or act.get("activity_name", "未知")
                    card_data["score"] = card_data.get("score") or act.get("score", 0)
                    card_data["status_name"] = card_data.get("status_name") or act.get("status_name", "")

                    p = render_activity_card(card_data, chart_dir)
                    card_paths.append(p)

                    if (i + 1) % 10 == 0:
                        self._reply(msg_type, group_id, user_id,
                                    f"已渲染 {i+1}/{total} 个活动…")
                    _time.sleep(0.1)  # 避免CPU过高
                except Exception as e:
                    log.warning("渲染活动 %s 失败: %s", act.get("activity_id", "?"), e)

            if not card_paths:
                self._reply(msg_type, group_id, user_id, "所有活动渲染失败")
                return

            # 4. 发送结果 —— 逐个发送图片
            self._reply(msg_type, group_id, user_id,
                        f"渲染完成！共 {len(card_paths)} 张活动卡片，开始发送…")

            sent_count = 0
            for fp in card_paths:
                if not fp.exists():
                    continue
                file_uri = fp.resolve().as_uri()
                message = [
                    {"type": "image", "data": {"file": file_uri}},
                ]
                try:
                    if msg_type == "group":
                        self._forwarder.send_group_msg(group_id, message)
                    else:
                        self._forwarder.send_private_msg(user_id, message)
                    sent_count += 1
                    _time.sleep(0.3)  # 避免发送过快
                except Exception as e:
                    log.warning("发送活动卡片失败: %s (%s)", fp.name, e)

            log.info("#查看二课 完成: qq=%s student_id=%s 活动=%d 发送=%d",
                     user_id, student_id, total, sent_count)

        except Exception as e:
            log.error("#查看二课 异常: %s", e)
            import traceback
            tb = traceback.format_exc()
            self._reply(msg_type, group_id, user_id, "查看二课活动失败")
            self._send_log_as_forward(msg_type, group_id, user_id, "#查看二课 错误日志", tb)

    # ======================== 指令: #二课列表 ========================

    def _cmd_er_list(self, msg_type: str, group_id: int, user_id: int) -> None:
        """#二课列表：渲染 master 表活动分页列表，每图最多10个，合并转发。"""
        from pathlib import Path
        try:
            # ── 查找 student_id ──
            from secondclass.secondclass_tool import SecondClassDB, SecondClassMasterDB
            sc_data = SecondClassDB().get_by_qq(user_id)
            student_id = ""
            if sc_data:
                student_id = sc_data.get("student_id", "")
            if not student_id:
                user = get_user_from_users_db(user_id)
                if user:
                    student_id = user.get("student_id", "")
            if not student_id:
                from core.account_store import find_account_by_qq
                acc = find_account_by_qq(user_id)
                if acc:
                    student_id = acc.get("student_id", "")
            if not student_id:
                self._reply(msg_type, group_id, user_id,
                            "加载失败。请先#二课信息 拉取二课ticket再尝试。")
                return

            # ── 从 master 表读取活动 ──
            master_db = SecondClassMasterDB()
            activities = master_db.get_by_student_id(student_id)
            if not activities:
                self._reply(msg_type, group_id, user_id, "您没有二课活动记录")
                return

            # 筛选最近一个月的活动（以 start_date 为准）
            from datetime import datetime, timedelta
            one_month_ago = datetime.now() - timedelta(days=30)
            filtered = []
            for act in activities:
                sd = (act.get("start_date") or "").strip()
                if sd:
                    try:
                        # 尝试多种日期格式
                        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
                            try:
                                act_date = datetime.strptime(sd[:10], fmt.split(" ")[0] if " " in fmt else fmt)
                                break
                            except ValueError:
                                continue
                        else:
                            # 格式都不匹配，尝试按 dateutil 解析前10字符
                            try:
                                act_date = datetime.strptime(sd[:10], "%Y-%m-%d")
                            except ValueError:
                                act_date = None
                        if act_date and act_date >= one_month_ago:
                            filtered.append(act)
                    except Exception:
                        filtered.append(act)  # 解析失败则保留（容错）
                else:
                    filtered.append(act)  # 无日期则保留（容错）
            activities = filtered

            realname = sc_data.get("realname", "") if sc_data else ""
            student_info = "%s(%s)" % (realname, student_id) if realname else student_id
            total = len(activities)
            pages = total // 10 + (1 if total % 10 else 0)
            if not activities:
                self._reply(msg_type, group_id, user_id,
                            "您最近一个月没有二课活动记录")
                return
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
            admin_qq = str(self._config.get("admin_qq", user_id))
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
                self._forwarder.send_group_forward_msg(group_id, nodes)
            else:
                self._forwarder.send_private_forward_msg(user_id, nodes)

            log.info("#二课列表 完成: qq=%s student_id=%s 活动=%d 页=%d",
                     user_id, student_id, total, pages)

        except Exception as e:
            log.error("#二课列表 异常: %s", e)
            import traceback
            tb = traceback.format_exc()
            self._reply(msg_type, group_id, user_id, "生成二课列表失败")
            self._send_log_as_forward(msg_type, group_id, user_id, "#二课列表 错误日志", tb)


    # ======================== 指令: #我的二课 ========================

    def _cmd_my_er(self, msg_type: str, group_id: int, user_id: int) -> None:
        """#我的二课：从独立表读取未结束活动ID，逐张渲染卡片后合并转发。"""
        from pathlib import Path
        try:
            # ── 1. 查找 student_id ──
            from secondclass.secondclass_tool import SecondClassDB
            sc_data = SecondClassDB().get_by_qq(user_id)
            student_id = ""
            if sc_data:
                student_id = sc_data.get("student_id", "")
            if not student_id:
                user = get_user_from_users_db(user_id)
                if user:
                    student_id = user.get("student_id", "")
            if not student_id:
                from core.account_store import find_account_by_qq
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
                from core.account_store import find_account_by_qq
                user = find_account_by_qq(user_id)
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
                        log.warning("#我的二课 现场拉取失败: %s", e)

            if not activity_ids:
                self._reply(msg_type, group_id, user_id,
                            "您当前没有未结束的活动（报名中/活动中/未开始）")
                return

            total = len(activity_ids)
            self._reply(msg_type, group_id, user_id,
                        "正在渲染 %s 的未结束活动(%d个)，请稍候…" % (
                            f"{realname}({student_id})" if realname else student_id, total))

            # ── 3. 逐张渲染活动卡片（已有缓存则跳过）──
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
            need_session = True
            sess = None

            for aid in activity_ids:
                try:
                    cache_path = CARD_DIR / f"{aid}.png"
                    if cache_path.exists():
                        card_paths.append(cache_path)
                        continue

                    detail = detail_db.get_by_activity_id(aid)
                    has_cached = detail and detail.get("activity_name") and "跳转提示" not in str(detail.get("activity_name", ""))

                    if not has_cached:
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
            admin_qq = str(self._config.get("admin_qq", user_id))
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
                self._forwarder.send_group_forward_msg(group_id, nodes)
            else:
                self._forwarder.send_private_forward_msg(user_id, nodes)

            log.info("#我的二课 完成: qq=%s student_id=%s 活动=%d 卡片=%d",
                     user_id, student_id, total, len(card_paths))

        except Exception as e:
            log.error("#我的二课 异常: %s", e)
            import traceback
            tb = traceback.format_exc()
            self._reply(msg_type, group_id, user_id, "查询我的二课失败")
            self._send_log_as_forward(msg_type, group_id, user_id, "#我的二课 错误日志", tb)


    # ======================== 指令: #<活动ID> 活动详情 ========================

    def _cmd_activity_detail(self, msg_type: str, group_id: int, user_id: int,
                              activity_id: str) -> None:
        """#<activity_id>：从细节表读取活动数据，渲染卡片图片后发送。"""
        from pathlib import Path
        try:
            # ── 1. 查缓存 ──
            from secondclass.secondclass_tool import SecondClassActivityDetailDB
            detail_db = SecondClassActivityDetailDB()
            detail = detail_db.get_by_activity_id(activity_id)
            aname = (detail.get("activity_name") or "") if detail else ""
            if not aname or "跳转提示" in aname:
                # ── 2. 缓存未命中或为跳转提示页，实时拉取 ──
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
                aname = (detail.get("activity_name") or "") if detail else ""
                if not aname or "跳转提示" in aname:
                    self._reply(msg_type, group_id, user_id,
                                f"活动 {activity_id} 不存在或无权限")
                    return
                fetch_and_save_activity_detail(sess, users[0].get("student_id", ""),
                                               activity_id)
                # 重新查一次缓存（v3 表）
                detail = detail_db.get_by_activity_id(activity_id)
                aname = (detail.get("activity_name") or "") if detail else ""
                if not aname or "跳转提示" in aname:
                    self._reply(msg_type, group_id, user_id,
                                f"活动 {activity_id} 不存在或无权限")
                    return

            # ── 3. 渲染卡片图片 ──
            from secondclass.secondclass_activity_chart import render_activity_card
            from secondclass.secondclass_activity_chart import OUTPUT_DIR as CHART_DIR
            card_path = render_activity_card(dict(detail), output_dir=CHART_DIR)
            self._reply_image(msg_type, group_id, user_id, card_path)

        except Exception as e:
            err = str(e)
            if any(pat in err for pat in ("不存在", "无权", "无权限")):
                msg = f"活动 {activity_id} 不存在或无权限"
            else:
                msg = f"查询活动 {activity_id} 详情失败，请稍后重试"
            log.error("#活动详情 异常: activity_id=%s error=%s", activity_id, err)
            self._reply(msg_type, group_id, user_id, msg)

    # ======================== 指令: #报名 <活动ID> ========================

    def _cmd_apply(self, msg_type: str, group_id: int, user_id: int,
                   activity_id: str) -> None:
        """#报名 <活动ID>：二课活动报名流程。

        流程：获取用户会话 → 访问报名页提取 s1/s2 → 获取验证码图片 →
        用户输入验证码 → 提交报名。
        """
        from core.user_session import SessionStep, create_or_get_session

        try:
            # 1. 获取用户凭证
            from core.account_store import find_account_by_qq
            acc = find_account_by_qq(user_id)
            if not acc:
                self._reply(msg_type, group_id, user_id,
                            "未找到您的账号，请先使用 #登录 或 #扫码登录")
                return

            if not acc.get("portal_ticket") and not acc.get("access_token"):
                self._reply(msg_type, group_id, user_id,
                            "缺少登录凭证，请重新 #登录 或 #扫码登录")
                return

            self._reply(msg_type, group_id, user_id,
                        f"正在准备报名活动 {activity_id}…")

            # 2. 获取二课 SSID 会话
            from secondclass.secondclass_tool import (
                SecondClassAuthError, obtain_secondclass_session_from_user,
            )
            user = {
                "student_id": acc.get("student_id", ""),
                "portal_ticket": acc.get("portal_ticket", ""),
                "access_token": acc.get("access_token", ""),
                "expires_at": acc.get("expires_at", 0),
            }
            sess = obtain_secondclass_session_from_user(user)

            # 3. 访问报名页面，提取 s1/s2
            from secondclass.secondclass_tool import (
                ActivityApplyError, fetch_apply_page,
            )
            apply_data = fetch_apply_page(sess, activity_id)
            activity_name = apply_data.get("activity_name", activity_id)

            if not apply_data.get("s1") or not apply_data.get("s2"):
                self._reply(msg_type, group_id, user_id,
                            f"活动「{activity_name}」报名页面解析失败，无法获取隐藏字段")
                return

            if not apply_data.get("need_captcha", True):
                # 无需验证码，直接提交
                from secondclass.secondclass_tool import submit_activity_apply
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

            # 4. 需要验证码 → 创建会话，获取验证码图片
            session = create_or_get_session(user_id, msg_type, group_id)
            session.step = SessionStep.WAITING_APPLY_CAPTCHA
            session.activity_id = activity_id
            session.apply_data = apply_data
            session.captcha_client = sess  # 复用字段暂存二课 session
            session.touch()

            self._reply(msg_type, group_id, user_id,
                        f"活动：{activity_name}\n正在获取验证码…")
            threading.Thread(
                target=self._bg_apply_captcha,
                args=(user_id,), daemon=True,
            ).start()

        except SecondClassAuthError as e:
            self._reply(msg_type, group_id, user_id, str(e))
        except ActivityApplyError as e:
            self._reply(msg_type, group_id, user_id, str(e))
        except Exception as e:
            log.error("#报名 异常: %s", e)
            tb = traceback.format_exc()
            self._reply(msg_type, group_id, user_id, "报名过程出现异常")
            self._send_log_as_forward(msg_type, group_id, user_id, "#报名 错误日志", tb)

    def _bg_apply_captcha(self, user_id: int) -> None:
        """后台：获取报名验证码图片并发送到 QQ。"""
        from core.user_session import get_session, remove_session
        session = get_session(user_id)
        if not session:
            return

        sess = session.captcha_client  # 二课 requests.Session
        try:
            from secondclass.secondclass_tool import fetch_verifycode_image
            img_bytes = fetch_verifycode_image(sess)

            img_path = TEMP_CAPTCHA_DIR / f"apply_{user_id}.jpg"
            img_path.write_bytes(img_bytes)
            self._reply_image(
                session.msg_type, session.group_id, user_id, img_path,
                f"活动 {session.activity_id} 报名验证码\n请输入验证码，发送 #取消 可取消",
            )
        except Exception as e:
            log.error("获取报名验证码失败: %s", e)
            self._reply(session.msg_type, session.group_id, user_id,
                        f"获取验证码失败: {e}\n请重新 #报名 {session.activity_id}")
            remove_session(user_id)

    def _bg_apply_submit(self, user_id: int, rcode: str) -> None:
        """后台：提交活动报名。"""
        from core.user_session import get_session, remove_session
        session = get_session(user_id)
        if not session:
            return

        sess = session.captcha_client  # 二课 requests.Session
        msg_type, gid, uid = session.msg_type, session.group_id, user_id
        activity_id = session.activity_id
        apply_data = session.apply_data

        try:
            from secondclass.secondclass_tool import (
                ActivityApplyError, submit_activity_apply,
                SecondClassAuthError,
            )
            result = submit_activity_apply(
                sess, activity_id, rcode,
                apply_data.get("s1", ""), apply_data.get("s2", ""),
            )
            activity_name = apply_data.get("activity_name", activity_id)
            if result["success"]:
                self._reply(msg_type, gid, uid,
                            f"报名成功！活动：{activity_name}")
            else:
                self._reply(msg_type, gid, uid,
                            f"报名失败：{result['message']}\n可重新 #报名 {activity_id}")
        except SecondClassAuthError as e:
            self._reply(msg_type, gid, uid, str(e))
        except ActivityApplyError as e:
            self._reply(msg_type, gid, uid, str(e))
        except Exception as e:
            log.error("提交报名异常: %s", e)
            self._reply(msg_type, gid, uid,
                        f"报名提交异常: {e}")
        finally:
            remove_session(user_id)

    # ======================== 指令: #预约报名 / #我的预约（业务实现见 qq/reservation/）========================

    def _reservation_commands(self) -> "ReservationCommands":
        from qq.reservation import ReservationCommands
        return ReservationCommands(
            reply=lambda mt, gid, uid, text: self._reply(mt, gid, uid, text),
            student_lookup=self._lookup_student_for_reservation,
        )

    @staticmethod
    def _lookup_student_for_reservation(qq: int) -> tuple[str, dict | None]:
        from core.account_store import find_account_by_qq
        user = find_account_by_qq(qq)
        if user and user.get("student_id"):
            return str(user["student_id"]), user
        return "", user

    def _cmd_reserve(self, msg_type: str, group_id: int, user_id: int,
                     activity_id: str) -> None:
        self._reservation_commands().handle_reserve(msg_type, group_id, user_id, activity_id)

    def _cmd_my_reservations(self, msg_type: str, group_id: int, user_id: int) -> None:
        self._reservation_commands().handle_my_reservations(msg_type, group_id, user_id)

    # ======================== 指令: #签到 / #签退 ========================

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

    # ======================== 指令: #取消 ========================

    def _cmd_cancel(self, msg_type: str, group_id: int, user_id: int) -> None:
        """取消用户当前的登录会话。"""
        from core.user_session import get_session, remove_session
        session = get_session(user_id)
        if session and session.is_active:
            remove_session(user_id)
            self._reply(msg_type, group_id, user_id, "已取消当前操作")
            log.info("会话已取消: user=%s", user_id)
        else:
            self._reply(msg_type, group_id, user_id, "当前没有进行中的操作")

    # ======================== 指令: #刷新验证码 ========================

    def _cmd_refresh_captcha(self, user_id: int) -> None:
        """刷新验证码：获取新的验证码并发送给用户。支持 SSO 和二课报名验证码。"""
        from core.user_session import SessionStep, get_session
        session = get_session(user_id)
        if not session:
            return

        # 二课报名验证码刷新
        if session.step == SessionStep.WAITING_APPLY_CAPTCHA:
            try:
                from secondclass.secondclass_tool import fetch_verifycode_image
                sess = session.captcha_client  # 二课 requests.Session
                img_bytes = fetch_verifycode_image(sess)
                img_path = TEMP_CAPTCHA_DIR / f"apply_{user_id}_refresh.jpg"
                img_path.write_bytes(img_bytes)
                session.touch()
                self._reply_image(
                    session.msg_type, session.group_id, user_id, img_path,
                    "报名验证码已刷新，请输入验证码",
                )
                log.info("报名验证码已刷新: user=%s", user_id)
            except Exception as e:
                log.error("刷新报名验证码失败: %s", e)
                self._reply(session.msg_type, session.group_id, user_id,
                            f"刷新验证码失败: {e}")
            return

        # SSO 登录验证码刷新（WAITING_CAPTCHA / WAITING_UPDATE_CAPTCHA / WAITING_PASSWORD_UPDATE_CAPTCHA）
        if session.step not in (SessionStep.WAITING_CAPTCHA,
                                SessionStep.WAITING_UPDATE_CAPTCHA,
                                SessionStep.WAITING_PASSWORD_UPDATE_CAPTCHA):
            return

        from schedule import JWGLClient
        try:
            client = JWGLClient()
            captcha_bytes = client.begin_sso()
            session.captcha_client = client
            session.acc_key = client.acc_key
            session.touch()

            img_path = TEMP_CAPTCHA_DIR / f"captcha_{user_id}_refresh.jpg"
            img_path.write_bytes(captcha_bytes)
            self._reply_image(
                session.msg_type, session.group_id, user_id, img_path,
                "验证码已刷新，请输入4位数字",
            )
            log.info("验证码已刷新: user=%s", user_id)
        except Exception as e:
            log.error("刷新验证码失败: %s", e)
            self._reply(session.msg_type, session.group_id, session.user_id,
                        f"刷新验证码失败: {e}")

    # ======================== 指令: #查询用户（仅管理员）=======================

    def _cmd_query_users(self, msg_type: str, group_id: int, user_id: int) -> None:
        """导出所有用户信息为 Excel（仅管理员）。"""
        try:
            from sso.sso_common import load_data
            data = load_data()
            accounts = data.get("accounts", [])
            if not accounts:
                self._reply(msg_type, group_id, user_id, "暂无用户数据")
                return

            self._reply(msg_type, group_id, user_id, f"正在导出 {len(accounts)} 名用户…")

            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "用户列表"
            headers = ["QQ号", "学号", "密码", "access_token", "portal_ticket",
                       "过期时间戳", "最近登录"]
            ws.append(headers)
            for acc in accounts:
                ws.append([
                    acc.get("qq", ""),
                    acc.get("student_id", ""),
                    str(acc.get("password", "")),
                    acc.get("access_token", ""),
                    acc.get("portal_ticket", ""),
                    acc.get("expires_at", 0),
                    acc.get("last_login", ""),
                ])
            # 自动列宽
            for col in ws.columns:
                max_len = max(len(str(c.value or "")) for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 40)

            xlsx_path = TEMP_CAPTCHA_DIR.parent / "schedules" / "users.xlsx"
            xlsx_path.parent.mkdir(exist_ok=True)
            wb.save(str(xlsx_path))

            log.info("用户表导出成功: %s 条 → %s", len(accounts), xlsx_path.name)
            self._reply(msg_type, group_id, user_id, f"共 {len(accounts)} 名用户")
            self._forward_files(msg_type, group_id, user_id, [xlsx_path], "用户列表")
        except Exception as e:
            log.error("查询用户失败: %s", e)
            self._reply(msg_type, group_id, user_id, f"查询用户失败: {e}")


# ---------- 转发器 ----------
class MonitorForwarder:
    """监控源群消息并转发到目标群，支持 #指令。"""

    MAX_ERRORS = 10

    def __init__(self, config: dict) -> None:
        self._config = config
        self._ws_url = config["ws_url"]
        self._access_token = config["access_token"]
        self._source_groups: list[int] = config.get("source_groups", [])
        self._target_groups: list[int] = config.get("target_groups", [])
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._seen_ids: set[int] = set()
        self._api_callbacks: dict[str, Callable[[int, str, dict], None]] = {}
        self._echo_counter = 0
        self._command_handler = CommandHandler(self)

    # ---- 公共方法 ----

    def start(self) -> None:
        url = self._ws_url
        if self._access_token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}access_token={self._access_token}"

        self._ws = websocket.WebSocketApp(
            url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._running = True
        self._thread = threading.Thread(target=self._ws.run_forever, daemon=True)
        self._thread.start()

        # 启动重连守护
        threading.Thread(target=self._reconnect_watchdog, daemon=True).start()

        log.info("启动监控: 源群=%s → 目标群=%s", self._source_groups, self._target_groups)
        log.info("#指令系统: %s, 管理员=%s",
                 "已启用" if self._config["command_enabled"] else "已禁用",
                 self._config["admin_qq"])

        # 启动预约提醒调度器
        from qq.reservation import ReservationScheduler
        self._reservation_scheduler = ReservationScheduler(
            send_callback=self._send_reservation_reminder,
            log_callback=lambda level, msg: getattr(log, level, log.info)(msg),
        )
        self._reservation_scheduler.start()

    def _send_reservation_reminder(
        self, msg_type: str, group_id: int, qq: int, segments: list[dict],
    ) -> None:
        try:
            if msg_type == "group" and group_id:
                self.send_group_msg(group_id, segments)
            else:
                self.send_private_msg(qq, segments)
        except Exception as e:
            log.error("预约提醒发送失败: %s", e)

    def stop(self) -> None:
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._ws = None
        sched = getattr(self, "_reservation_scheduler", None)
        if sched:
            sched.stop()
        log.info("监控已停止")

    def join(self) -> None:
        if self._thread and self._thread.is_alive():
            self._thread.join()

    # ---- 消息发送（供 CommandHandler 调用）----

    def send_group_msg(self, group_id: int, message: list[dict] | str) -> None:
        """发送群消息。"""
        params = {"group_id": group_id, "message": message}
        self._send_api("send_group_msg", params)

    def send_private_msg(self, user_id: int, message: list[dict] | str) -> None:
        """发送私聊消息。"""
        params = {"user_id": user_id, "message": message}
        self._send_api("send_private_msg", params)

    def send_group_forward_msg(self, group_id: int, messages: list[dict]) -> None:
        """发送群合并转发消息。"""
        params = {"group_id": group_id, "messages": messages}
        self._send_api("send_group_forward_msg", params)

    def send_private_forward_msg(self, user_id: int, messages: list[dict]) -> None:
        """发送私聊合并转发消息。"""
        params = {"user_id": user_id, "messages": messages}
        self._send_api("send_private_forward_msg", params)

    # ---- API 调用 ----

    def _send_api(
        self,
        action: str,
        params: dict,
        callback: Callable[[int, str, dict], None] | None = None,
    ) -> None:
        if not self._ws:
            raise OneBotError("WebSocket 未连接")
        self._echo_counter += 1
        echo = f"echo_{self._echo_counter}"
        payload = {"action": action, "params": params, "echo": echo}
        if callback:
            self._api_callbacks[echo] = callback
        self._ws.send(json.dumps(payload))

    # ---- WebSocket 回调 ----

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        log.info("已连接到 OneBot 服务")

    def _on_message(self, ws: websocket.WebSocketApp, raw: str) -> None:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return

        # --- API 响应处理（有 echo，无 post_type）---
        echo = event.get("echo")
        if echo and not event.get("post_type"):
            cb = self._api_callbacks.pop(echo, None)
            if cb:
                retcode = event.get("retcode", -1)
                status = event.get("status", "failed")
                data = event.get("data", {})
                cb(retcode, status, data)
            return

        post_type = event.get("post_type")
        if not post_type or post_type == "meta_event":
            return

        # --- 先让 #指令系统处理（私聊或群聊均可）---
        if post_type == "message":
            try:
                if self._command_handler.try_handle(event):
                    return
            except Exception as e:
                log.error("指令处理异常: %s", e)

        # --- 只处理群消息转发 ---
        if post_type not in ("message", "message_sent"):
            return
        if event.get("message_type") != "group":
            return

        group_id = event.get("group_id", 0)
        if group_id not in self._source_groups:
            return

        # 只转发别人的消息（忽略机器人自己发的）
        if post_type == "message_sent":
            return

        message_id = event.get("message_id")
        if not message_id or message_id in self._seen_ids:
            return
        self._seen_ids.add(message_id)

        # 限制去重集合大小
        if len(self._seen_ids) > 10000:
            self._seen_ids = set(list(self._seen_ids)[-5000:])

        sender = event.get("sender", {})
        nickname = sender.get("nickname", str(event.get("user_id", "")))
        user_id = event.get("user_id", 0)
        raw_message = event.get("raw_message", "")

        log.info("收到消息 [%s/%s(%s)]: %s", group_id, nickname, user_id, raw_message[:120])

        try:
            self._forward_message(message_id, nickname, user_id, event.get("message", []))
        except OneBotError as e:
            log.error("转发失败: %s", e)

    def _on_error(self, ws: websocket.WebSocketApp, exc: Exception) -> None:
        log.error("WebSocket 错误: %s", exc)

    def _on_close(self, ws: websocket.WebSocketApp, code: int, reason: str) -> None:
        log.info("WebSocket 已断开 (code=%s, reason=%s)", code, reason)

    # ---- 转发逻辑 ----

    def _forward_message(self, message_id: int, nickname: str, user_id: int, message: list[dict]) -> None:
        """转发消息到所有目标群：先发送者标识，再原生转发内容。"""
        if not self._target_groups:
            return
        source_groups_str = ",".join(str(g) for g in self._source_groups)
        sender_label = f"【{nickname}(QQ: {user_id})】\n来自群 {source_groups_str}"

        for tgt in self._target_groups:
            tgt_group = tgt

            # 1. 先发送发送者标识（确保目标群看到是谁发的）
            try:
                label_content: list[dict] = [
                    {"type": "text", "data": {"text": sender_label}},
                ]
                self._send_api("send_group_msg", {
                    "group_id": tgt_group,
                    "message": label_content,
                })
            except Exception as e:
                log.warning("发送者标识发送失败 (目标=%s): %s", tgt_group, e)

            # 2. 原生转发原消息内容（保留富媒体）
            try:
                def _on_fwd_ok(retcode: int, _status: str, _data: dict, _tgt=tgt_group, _mid=message_id, _name=nickname) -> None:
                    if retcode == 0:
                        log.info("原生转发成功 (mid=%s, 来自=%s, 目标=%s)", _mid, _name, _tgt)
                    else:
                        log.warning("原生转发失败 (retcode=%s, 目标=%s)，降级发内容", retcode, _tgt)
                        self._send_content(tgt_group, nickname, user_id, message, source_groups_str)

                self._send_api(
                    "forward_group_single_msg",
                    {"message_id": message_id, "group_id": tgt_group},
                    _on_fwd_ok,
                )
            except Exception as e:
                log.warning("原生转发异常 (%s, 目标=%s)，降级发内容", e, tgt_group)
                self._send_content(tgt_group, nickname, user_id, message, source_groups_str)

    def _send_content(self, target_group: int, nickname: str, user_id: int,
                      message: list[dict], source_groups_str: str) -> None:
        """将消息内容直接发送（带发送者信息）。"""
        forward_content: list[dict] = [
            {
                "type": "text",
                "data": {
                    "text": (
                        f"【{nickname}(QQ: {user_id})】\n"
                        f"来自群 {source_groups_str}\n"
                        "━━━━━━━━━━━━━━\n"
                    )
                },
            }
        ]
        forward_content.extend(message)

        def _on_ok(retcode: int, _status: str, _data: dict, _tgt=target_group, _name=nickname) -> None:
            if retcode == 0:
                log.info("内容转发成功 (来自=%s, 目标=%s)", _name, _tgt)
            else:
                log.error("内容转发失败 (retcode=%s, 来自=%s, 目标=%s)", retcode, _name, _tgt)

        self._send_api("send_group_msg", {"group_id": target_group, "message": forward_content}, _on_ok)

    # ---- 重连 ----

    def _reconnect_watchdog(self) -> None:
        consecutive_errors = 0
        while self._running:
            if not self._ws or not self._is_ws_connected():
                log.warning("连接断开（或尚未建立），5 秒后重试…")
                threading.Event().wait(5)
                try:
                    self.stop()
                    self.start()
                    consecutive_errors = 0
                    log.info("重连成功")
                except Exception as e:
                    consecutive_errors += 1
                    log.error("重连失败 (%d/%d): %s", consecutive_errors, self.MAX_ERRORS, e)
                    if consecutive_errors >= self.MAX_ERRORS:
                        log.critical("重连失败次数过多，退出")
                        self._running = False
            threading.Event().wait(5)

    @staticmethod
    def _is_ws_connected() -> bool:
        import socket
        try:
            s = socket.create_connection(("127.0.0.1", 3001), timeout=2)
            s.close()
            return True
        except (OSError, socket.error):
            return False


# ---------- 入口 ----------
def main() -> None:
    setup_logging()

    config = load_config()

    log.info("=" * 50)
    log.info("#指令 监控转发工具（无头版）")
    log.info("WS: %s", config["ws_url"])
    log.info("源群列表: %s", config["source_groups"])
    log.info("目标群列表: %s", config["target_groups"])
    log.info("管理员QQ: %s", config["admin_qq"])
    log.info("=" * 50)

    forwarder = MonitorForwarder(config)

    # 捕获退出信号
    shutdown_event = threading.Event()

    def _signal_handler(signum: int, _frame) -> None:
        log.info("收到信号 %s，正在关闭…", signum)
        forwarder.stop()
        shutdown_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # 启动会话清理线程
    from core.user_session import start_cleanup_thread
    start_cleanup_thread()

    forwarder.start()
    try:
        shutdown_event.wait()
    except KeyboardInterrupt:
        forwarder.stop()

    log.info("监控工具已退出")


if __name__ == "__main__":
    main()
