"""NapCat 监控/自动重启模块。

独立于 OneBotClient 的 WS 监控客户端，专门检测指定端口 NapCat 实例的存活状态。
当检测到掉线（WS 断开或心跳超时 65s），自动：
  1. 用 netstat 查找目标端口对应的 PID
  2. kill 该进程链（不影响其他端口上的 NapCat）
  3. 重启 napcat.bat

注：仅负责重启，不再处理二维码。NapCat 重启后需手动扫码登录。
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# ---------- 常量 ----------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NAPCAT_SHELL_DIR = PROJECT_ROOT / "NapCat.Shell.Windows.OneKey" / "NapCat.44498.Shell"
NAPCAT_BAT = NAPCAT_SHELL_DIR / "napcat.bat"
HEARTBEAT_TIMEOUT = 65  # heartInterval * 2 + 5 = 65s
RESTART_COOLDOWN = 60   # 冷却秒数
CONNECT_BACKOFF_BASE = 5   # 连接失败首次退避
CONNECT_BACKOFF_MAX = 60   # 退避上限

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
        log_cb: Callable[[str, str], None],
        is_main_connected: Callable[[], bool],
        on_state_change: Callable[[str], None] | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._access_token = access_token
        self._log = log_cb
        self._is_main_connected = is_main_connected
        self._on_state_change = on_state_change

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_heartbeat: float = 0.0
        self._last_restart_at: float = 0.0
        self._state: str = STATE_IDLE
        self._established: bool = False  # 当前 WS 是否曾握手成功
        self._connect_fail_count: int = 0  # 连续连接失败次数（用于退避）
        self._target_port: int = self._parse_port(ws_url)

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

    def _set_state(self, s: str) -> None:
        self._state = s
        if self._on_state_change:
            self._on_state_change(s)

    @staticmethod
    def _parse_port(ws_url: str) -> int:
        """从 ws://host:port/... 解析端口，失败回退 3001。"""
        try:
            return urlparse(ws_url).port or 3001
        except Exception:
            return 3001

    # ── 主循环 ──

    def _run(self) -> None:
        import websocket as _ws

        self._log("info", f"NapCat 监控已启动（目标端口 {self._target_port}）")
        self._set_state(STATE_MONITORING)

        while not self._stop.is_set():
            try:
                self._monitor_loop()
            except Exception as e:
                self._log("error", f"监控循环异常: {e}")
            # 连接失败用指数退避，避免疯狂刷屏
            if not self._established and self._connect_fail_count > 0:
                wait = min(
                    CONNECT_BACKOFF_BASE * (2 ** (self._connect_fail_count - 1)),
                    CONNECT_BACKOFF_MAX,
                )
            else:
                wait = 5
            self._stop.wait(wait)

        self._log("info", "NapCat 监控已停止")

    def _monitor_loop(self) -> None:
        import websocket as _ws

        # 进入新的一次连接：重置 established 标志
        self._established = False

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

        # 主线程负责心跳超时检测（仅在已建立连接后才有意义）
        while not self._stop.is_set() and (ws.sock and ws.sock.connected):
            if self._established and time.time() - self._last_heartbeat > HEARTBEAT_TIMEOUT:
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
        self._log("info", f"监控 WS 已连接（{self._ws_url}）")
        self._last_heartbeat = time.time()
        self._established = True
        self._connect_fail_count = 0

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
        if self._stop.is_set():
            return
        if not self._established:
            # 从未握手成功 —— 视为目标 NapCat 未启动 / 端口未开
            self._connect_fail_count += 1
            wait = min(
                CONNECT_BACKOFF_BASE * (2 ** (self._connect_fail_count - 1)),
                CONNECT_BACKOFF_MAX,
            )
            self._log(
                "info",
                f"监控目标未响应（第 {self._connect_fail_count} 次），{wait}s 后重试",
            )
            return
        self._log("warn", f"监控 WS 断开 (code={close_status_code})")
        self._on_disconnected()

    def _on_ws_error(self, ws, error) -> None:
        # 未握手前的 sock=None / 连接拒绝是常见噪音，降级到 debug
        if not self._established:
            log.debug("监控 WS 连接失败: %s", error)
            return
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
            self._kill_target_process()
            self._log("info", "等待进程清理完毕…")
            time.sleep(2)

            # 启动 napcat.bat
            self._set_state(STATE_RESTARTING)
            self._log("info", "启动 napcat.bat…")
            subprocess.Popen(
                ["cmd", "/c", "start", "", str(NAPCAT_BAT)],
                cwd=str(NAPCAT_SHELL_DIR),
                shell=True,
            )
            self._log("info", "NapCat 已重启，请手动扫码登录")
            self._set_state(STATE_MONITORING)

        except Exception as e:
            self._log("error", f"重启流程异常: {e}")
            self._set_state(STATE_ERROR)

    # ── 步骤 1: kill 监控目标端口对应的进程 ──

    def _kill_target_process(self) -> None:
        """用 netstat 找 target_port 对应的 PID，只 kill 那条进程链。"""
        port = self._target_port
        pid = self._find_pid_by_port(port)
        if not pid:
            self._log("info", f"{port} 端口无 LISTENING 进程，无需 kill")
            return

        self._log("info", f"{port} 端口 PID={pid}，正在终止…")

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

        self._log("info", f"{port} 进程已终止")

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

    # ── 步骤 2: 结束 ──

