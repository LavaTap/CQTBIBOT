"""NapCat 监控/自动重启模块。

独立于 OneBotClient 的 WS 监控客户端，专门检测 3001 端口 NapCat 实例的存活状态。
当检测到掉线（WS 断开或心跳超时 65s），自动：
  1. 用 netstat 查找 3001 端口对应的 PID
  2. 只 kill 3001 的进程链（不动 3002）
  3. 重启 napcat.bat
  4. 等待新二维码生成
  5. 待主 OneBotClient 重连后，推送二维码给管理员
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# ---------- 常量 ----------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NAPCAT_SHELL_DIR = PROJECT_ROOT / "NapCat.Shell.Windows.OneKey" / "NapCat.44498.Shell"
NAPCAT_BAT = NAPCAT_SHELL_DIR / "napcat.bat"
NAPCAT_QRCODE_PATH = (
    NAPCAT_SHELL_DIR
    / "versions" / "9.9.26-44498" / "resources" / "app" / "napcat" / "cache"
    / "qrcode.png"
)
HEARTBEAT_TIMEOUT = 65  # heartInterval * 2 + 5 = 65s
RESTART_COOLDOWN = 60   # 冷却秒数
QR_WATCH_TIMEOUT = 90   # 等待二维码超时
RECONNECT_WAIT = 30     # 等待主客户端重连超时

State = str
STATE_IDLE = "空闲"
STATE_MONITORING = "监控中"
STATE_DISCONNECTED = "检测到掉线"
STATE_RESTARTING = "重启中"
STATE_WAITING_QR = "等待二维码"
STATE_QR_SENT = "二维码已发送"
STATE_ERROR = "出错"


class NapCatMonitor:
    """NapCat 3001 实例监控器。"""

    def __init__(
        self,
        *,
        ws_url: str,
        access_token: str,
        admin_qq: int,
        log_cb: Callable[[str, str], None],
        send_cb: Callable[[int, list[dict] | str], None],
        is_main_connected: Callable[[], bool],
        on_state_change: Callable[[State], None] | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._access_token = access_token
        self._admin_qq = admin_qq
        self._log = log_cb
        self._send = send_cb
        self._is_main_connected = is_main_connected
        self._on_state_change = on_state_change

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_heartbeat: float = 0.0
        self._last_restart_at: float = 0.0
        self._state: State = STATE_IDLE

    # ── 公开方法 ──

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="NapCatMonitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._set_state(STATE_IDLE)

    def trigger_restart(self) -> None:
        """手动触发重启（供 UI 按钮调用）。"""
        threading.Thread(target=self._restart_flow, daemon=True).start()

    # ── 状态管理 ──

    def _set_state(self, s: State) -> None:
        self._state = s
        if self._on_state_change:
            self._on_state_change(s)

    # ── 主循环 ──

    def _run(self) -> None:
        import websocket as _ws

        self._log("info", "NapCat 监控已启动")
        self._set_state(STATE_MONITORING)

        while not self._stop.is_set():
            try:
                self._monitor_loop()
            except Exception as e:
                self._log("error", f"监控循环异常: {e}")
            self._stop.wait(5)

        self._log("info", "NapCat 监控已停止")

    def _monitor_loop(self) -> None:
        import websocket as _ws

        ws = _ws.WebSocketApp(
            self._ws_url,
            header={"Authorization": f"Bearer {self._access_token}"}
            if self._access_token
            else {},
            on_open=self._on_ws_open,
            on_message=self._on_ws_message,
            on_close=self._on_ws_close,
            on_error=self._on_ws_error,
        )

        self._last_heartbeat = time.time()
        self._set_state(STATE_MONITORING)

        # 在独立线程中运行 WS
        t = threading.Thread(
            target=ws.run_forever, kwargs={"ping_interval": 10, "ping_timeout": 5},
            daemon=True,
        )
        t.start()

        # 主线程负责心跳超时检测
        while not self._stop.is_set() and (ws.sock and ws.sock.connected):
            if time.time() - self._last_heartbeat > HEARTBEAT_TIMEOUT:
                self._log("warn", f"心跳超时（{HEARTBEAT_TIMEOUT}s 无消息），判定掉线")
                self._on_disconnected()
                break
            self._stop.wait(5)

        # 关闭 WS
        try:
            ws.close()
        except Exception:
            pass
        t.join(timeout=3)

    # ── WS 回调 ──

    def _on_ws_open(self, ws) -> None:
        self._log("info", "监控 WS 已连接")
        self._last_heartbeat = time.time()

    def _on_ws_message(self, ws, raw: str) -> None:
        self._last_heartbeat = time.time()
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return
        # 心跳事件刷新 last_heartbeat
        if event.get("post_type") == "meta_event":
            return

    def _on_ws_close(self, ws, close_status_code, close_msg) -> None:
        self._log("warn", f"监控 WS 断开 (code={close_status_code})")
        if not self._stop.is_set():
            self._on_disconnected()

    def _on_ws_error(self, ws, error) -> None:
        self._log("error", f"监控 WS 错误: {error}")

    # ── 掉线处理 ──

    def _on_disconnected(self) -> None:
        now = time.time()
        if now - self._last_restart_at < RESTART_COOLDOWN:
            self._log("info", f"冷却中（{RESTART_COOLDOWN}s 内已重启过），跳过")
            return

        self._set_state(STATE_DISCONNECTED)
        self._log("warn", "触发重启流程")
        self._restart_flow()

    def _restart_flow(self) -> None:
        self._last_restart_at = time.time()
        try:
            self._kill_3001_process()
            self._log("info", "等待进程清理完毕…")
            time.sleep(2)

            # 删除旧二维码
            self._delete_old_qrcode()

            # 启动 napcat.bat
            self._set_state(STATE_RESTARTING)
            self._log("info", "启动 napcat.bat…")
            subprocess.Popen(
                ["cmd", "/c", "start", "", str(NAPCAT_BAT)],
                cwd=str(NAPCAT_SHELL_DIR),
                shell=True,
            )

            # 等待二维码
            self._set_state(STATE_WAITING_QR)
            qr_path = self._wait_qrcode()
            if not qr_path:
                self._log("error", "等待二维码超时（90s），请手动重启 NapCat")
                self._set_state(STATE_ERROR)
                return

            self._log("info", f"二维码已生成: {qr_path.name}")

            # 等待主客户端重连后发送
            self._set_state(STATE_QR_SENT)
            self._send_qrcode(qr_path)

        except Exception as e:
            self._log("error", f"重启流程异常: {e}")
            self._set_state(STATE_ERROR)

    # ── 步骤 1: kill 3001 进程 ──

    def _kill_3001_process(self) -> None:
        """用 netstat 找 3001 端口的 PID，只 kill 那条进程链。"""
        pid = self._find_pid_by_port(3001)
        if not pid:
            self._log("info", "3001 端口无 LISTENING 进程，无需 kill")
            return

        self._log("info", f"3001 端口 PID={pid}，正在终止…")

        # 先杀子进程，再杀父进程
        for flag in ("/T", ""):
            try:
                subprocess.run(
                    ["taskkill", "/F", flag, "/PID", str(pid)],
                    capture_output=True, timeout=10,
                )
            except subprocess.TimeoutExpired:
                pass
            except Exception:
                pass

        # 兜底：按进程名杀
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "NapCatWinBootMain.exe"],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass

        self._log("info", "3001 进程已终止")

    @staticmethod
    def _find_pid_by_port(port: int) -> int | None:
        """通过 netstat -ano 查找端口对应的 LISTENING PID。"""
        try:
            r = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=10,
            )
            for line in r.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.strip().split()
                    if parts:
                        try:
                            return int(parts[-1])
                        except (ValueError, IndexError):
                            pass
        except Exception:
            pass
        return None

    # ── 步骤 2: 删除旧二维码 ──

    def _delete_old_qrcode(self) -> None:
        qr = Path(NAPCAT_QRCODE_PATH)
        if qr.exists():
            try:
                qr.unlink()
                self._log("info", "已删除旧二维码")
            except OSError as e:
                self._log("warn", f"删除旧二维码失败: {e}")

    # ── 步骤 3: 等待新二维码 ──

    def _wait_qrcode(self) -> Path | None:
        """用 watchdog 监听 cache 目录，等待 qrcode.png 被创建。"""
        qr = Path(NAPCAT_QRCODE_PATH)
        cache_dir = qr.parent

        # 确保目录存在
        cache_dir.mkdir(parents=True, exist_ok=True)

        # 先检查是否已存在（NapCat 可能已生成）
        if qr.exists():
            return qr

        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            self._log("error", "缺少 watchdog 库，请 pip install watchdog")
            # 降级：轮询
            return self._poll_qrcode(qr)

        event = threading.Event()
        qr_found: list[Path] = []

        class _Handler(FileSystemEventHandler):
            def on_created(self, ev):
                if Path(ev.src_path).name == qr.name:
                    qr_found.append(Path(ev.src_path))
                    event.set()

        observer = Observer()
        observer.schedule(_Handler(), str(cache_dir), recursive=False)
        observer.start()

        try:
            event.wait(timeout=QR_WATCH_TIMEOUT)
        finally:
            observer.stop()
            observer.join(timeout=3)

        return qr_found[0] if qr_found else None

    def _poll_qrcode(self, qr: Path) -> Path | None:
        """watchdog 不可用时的轮询降级方案。"""
        deadline = time.time() + QR_WATCH_TIMEOUT
        while time.time() < deadline and not self._stop.is_set():
            if qr.exists():
                return qr
            time.sleep(2)
        return qr if qr.exists() else None

    # ── 步骤 4: 发送二维码给管理员 ──

    def _send_qrcode(self, qr_path: Path) -> None:
        """等待主客户端重连后推送二维码。"""
        deadline = time.time() + RECONNECT_WAIT
        while time.time() < deadline and not self._stop.is_set():
            if self._is_main_connected():
                break
            time.sleep(2)
        else:
            self._log("error", f"主客户端 {RECONNECT_WAIT}s 内未重连，二维码未发送")
            return

        try:
            img_bytes = qr_path.read_bytes()
            img_b64 = base64.b64encode(img_bytes).decode("ascii")
            message = [
                {"type": "text", "data": {"text": "NapCat 已重启，请扫码登录\n"}},
                {"type": "image", "data": {"file": f"base64://{img_b64}"}},
            ]
            self._send(self._admin_qq, message)
            self._log("info", f"二维码已发送给管理员 QQ({self._admin_qq})")
            self._set_state(STATE_QR_SENT)
        except Exception as e:
            self._log("error", f"发送二维码失败: {e}")
            self._set_state(STATE_ERROR)
