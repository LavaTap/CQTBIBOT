"""NapCat 掉线监控与自动重启。

监听独立 OneBot WebSocket（仅用于心跳/连接状态），检测到掉线后：
  1. 杀掉 NapCatWinBootMain.exe 与 QQ.exe
  2. 删除旧二维码缓存文件
  3. 重新启动 napcat.bat
  4. 用 watchdog 等待新的 qrcode.png 出现
  5. 等主转发 OneBot 连接重连成功后，把二维码以图片消息私聊给管理员 QQ
"""
from __future__ import annotations

import base64
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

import websocket
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

HEARTBEAT_TIMEOUT_SEC = 65          # 心跳超时阈值（NapCat 默认 heartInterval=30s）
RESTART_COOLDOWN_SEC = 60           # 重启冷却窗口
QRCODE_WAIT_TIMEOUT_SEC = 90        # 等待二维码文件出现超时
MAIN_RECONNECT_WAIT_SEC = 30        # 等待主 client 重连成功超时
QRCODE_STABILIZE_SEC = 1.0          # 文件写入后稳定窗口

# 监控目标端口（3001 实例）与必须保护的"另一个"实例端口
TARGET_PORT = 3001
PROTECTED_PORT = 3002

# 状态枚举（字符串简单，避免引 Enum）
STATE_IDLE = "未启动"
STATE_MONITORING = "监控中"
STATE_OFFLINE_DETECTED = "检测到掉线"
STATE_RESTARTING = "重启中"
STATE_WAITING_QRCODE = "等待扫码"
STATE_QRCODE_SENT = "二维码已发送"
STATE_ERROR = "错误"


class _QrcodeWaiter(FileSystemEventHandler):
    """watchdog 处理器：等待 qrcode.png 创建/修改后给 Event 信号。"""

    def __init__(self, target_filename: str, evt: threading.Event) -> None:
        self._target = target_filename.lower()
        self._evt = evt

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if Path(event.src_path).name.lower() == self._target:
            self._evt.set()

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if Path(event.src_path).name.lower() == self._target:
            self._evt.set()


class NapCatMonitor:
    """NapCat 心跳监控 + 掉线自愈。

    与主 OneBotClient 解耦：独立的 ws 客户端只观察 lifecycle/heartbeat 和连接状态，
    不参与业务消息处理。
    """

    def __init__(
        self,
        ws_url: str,
        access_token: str,
        napcat_bat_path: Path,
        qrcode_path: Path,
        get_admin_qq: Callable[[], int],
        send_private_msg: Callable[[int, list], None],
        is_main_connected: Callable[[], bool],
        log_callback: Callable[[str, str], None],
        on_state_change: Callable[[str], None] | None = None,
        on_qrcode_ready: Callable[[bytes], None] | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._access_token = access_token
        self._napcat_bat = napcat_bat_path
        self._qrcode_path = qrcode_path
        self._get_admin_qq = get_admin_qq
        self._send_private_msg = send_private_msg
        self._is_main_connected = is_main_connected
        self._log = log_callback
        self._on_state_change = on_state_change
        self._on_qrcode_ready = on_qrcode_ready

        self._ws: websocket.WebSocketApp | None = None
        self._ws_thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._running = False
        self._last_heartbeat = 0.0
        self._last_restart_ts = 0.0
        self._restart_lock = threading.Lock()
        self._state = STATE_IDLE

    # ---------- 公共 API ----------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._last_heartbeat = time.time()
        self._open_ws()
        self._watchdog_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._watchdog_thread.start()
        self._set_state(STATE_MONITORING)
        self._log("info", "NapCat 监控已启动")

    def stop(self) -> None:
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._ws = None
        self._set_state(STATE_IDLE)
        self._log("info", "NapCat 监控已停止")

    def trigger_restart(self, reason: str = "手动触发") -> None:
        """供 UI 按钮调用：在后台线程执行重启流程。"""
        threading.Thread(target=self._do_restart, args=(reason,), daemon=True).start()

    @property
    def state(self) -> str:
        return self._state

    # ---------- 心跳 ws ----------

    def _open_ws(self) -> None:
        url = self._ws_url
        if self._access_token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}access_token={self._access_token}"
        self._ws = websocket.WebSocketApp(
            url,
            on_open=self._on_ws_open,
            on_message=self._on_ws_message,
            on_close=self._on_ws_close,
            on_error=self._on_ws_error,
        )
        self._ws_thread = threading.Thread(
            target=self._ws.run_forever,
            kwargs={"ping_interval": 30, "ping_timeout": 10},
            daemon=True,
        )
        self._ws_thread.start()

    def _on_ws_open(self, ws: websocket.WebSocketApp) -> None:
        self._last_heartbeat = time.time()
        self._log("info", "[monitor] 心跳 WS 已连接")
        # 连接成功后回到监控中状态（覆盖之前的"检测到掉线"）
        if self._state in (STATE_OFFLINE_DETECTED, STATE_QRCODE_SENT):
            self._set_state(STATE_MONITORING)

    def _on_ws_message(self, ws: websocket.WebSocketApp, raw: str) -> None:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return
        if event.get("post_type") == "meta_event":
            mt = event.get("meta_event_type")
            if mt in ("heartbeat", "lifecycle"):
                self._last_heartbeat = time.time()

    def _on_ws_close(self, ws: websocket.WebSocketApp, code, reason) -> None:
        self._log("warn", f"[monitor] 心跳 WS 已断开 code={code}")
        if self._running and self._state == STATE_MONITORING:
            self._set_state(STATE_OFFLINE_DETECTED)
            self._log("warn", "[monitor] 检测到掉线（WS 断开），准备重启 NapCat")
            threading.Thread(target=self._do_restart, args=("WS 断开",), daemon=True).start()

    def _on_ws_error(self, ws: websocket.WebSocketApp, exc: Exception) -> None:
        # 仅记录，不直接触发重启（让 on_close 来判定）
        self._log("warn", f"[monitor] 心跳 WS 错误: {exc}")

    def _heartbeat_loop(self) -> None:
        """每 5s 检查一次心跳是否超时。"""
        while self._running:
            time.sleep(5)
            if not self._running:
                break
            if self._state != STATE_MONITORING:
                continue
            elapsed = time.time() - self._last_heartbeat
            if elapsed > HEARTBEAT_TIMEOUT_SEC:
                self._log(
                    "warn",
                    f"[monitor] 心跳超时 {elapsed:.0f}s（阈值 {HEARTBEAT_TIMEOUT_SEC}s），判定掉线",
                )
                self._set_state(STATE_OFFLINE_DETECTED)
                threading.Thread(
                    target=self._do_restart, args=("心跳超时",), daemon=True,
                ).start()

    # ---------- 重启流程 ----------

    def _do_restart(self, reason: str) -> None:
        # 冷却窗口
        with self._restart_lock:
            now = time.time()
            if now - self._last_restart_ts < RESTART_COOLDOWN_SEC:
                self._log(
                    "info",
                    f"[monitor] 距上次重启 {now - self._last_restart_ts:.0f}s，"
                    f"未超过冷却 {RESTART_COOLDOWN_SEC}s，跳过本次（原因={reason}）",
                )
                return
            self._last_restart_ts = now

        # 预检：日志记录两个端口当前状态
        p3001 = self._pids_listening_on(TARGET_PORT)
        p3002 = self._pids_listening_on(PROTECTED_PORT)
        self._log(
            "info",
            f"[monitor] 端口状态 3001={sorted(p3001) or '空'} 3002={sorted(p3002) or '空'}",
        )

        self._set_state(STATE_RESTARTING)
        self._log("info", f"[monitor] 开始重启 NapCat 3001 实例（原因={reason}）")

        try:
            self._kill_processes()
            self._remove_old_qrcode()
            self._launch_napcat()
            self._set_state(STATE_WAITING_QRCODE)

            qrcode_bytes = self._wait_qrcode()
            if not qrcode_bytes:
                self._log("error", "[monitor] 等待二维码超时")
                self._set_state(STATE_ERROR)
                return

            if self._on_qrcode_ready:
                try:
                    self._on_qrcode_ready(qrcode_bytes)
                except Exception as e:
                    self._log("warn", f"[monitor] UI 预览回调失败: {e}")

            # 重新打开心跳 ws（旧的可能已经关闭）
            if not self._ws or not getattr(self._ws, "sock", None):
                self._open_ws()

            # 等主 client 重连后再发图
            sent = self._send_qrcode_when_ready(qrcode_bytes)
            if sent:
                self._set_state(STATE_QRCODE_SENT)
                self._log("info", "[monitor] 二维码已私聊发送给管理员")
            else:
                self._set_state(STATE_ERROR)
                self._log("error", "[monitor] 主连接未在窗口内恢复，二维码未发送")
        except Exception as e:
            self._log("error", f"[monitor] 重启流程异常: {e}")
            self._set_state(STATE_ERROR)

    def _kill_processes(self) -> None:
        """只 kill 占用 3001 端口的 NapCat 实例，保护 3002 实例。

        策略：直接 kill 占用 3001 端口的进程及其进程树（/T），
        3002 端口对应的 PID 作为白名单严格拒绝命中。
        """
        protected_pids = self._pids_listening_on(PROTECTED_PORT)
        if protected_pids:
            self._log(
                "info",
                f"[monitor] 检测到 3002 实例 PID={sorted(protected_pids)}，将保护",
            )

        target_pids = self._pids_listening_on(TARGET_PORT)
        if not target_pids:
            self._log(
                "warn",
                f"[monitor] 当前 {TARGET_PORT} 端口无 LISTENING 进程，跳过 kill",
            )
            return

        kill_pids = target_pids - protected_pids
        if not kill_pids:
            self._log(
                "error",
                "[monitor] 3001 与 3002 命中相同 PID，拒绝 kill（实例共享同一进程？）",
            )
            return

        for pid in kill_pids:
            try:
                result = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    self._log("info", f"[monitor] 已结束 3001 实例进程树 PID={pid}")
                else:
                    self._log(
                        "warn",
                        f"[monitor] taskkill PID={pid}: "
                        f"{(result.stdout or result.stderr).strip()}",
                    )
            except Exception as e:
                self._log("warn", f"[monitor] taskkill PID={pid} 失败: {e}")

        # 给 OS 一点时间释放端口
        time.sleep(2)

        # 校验：3002 应仍 LISTENING；3001 应已释放
        if protected_pids:
            still = self._pids_listening_on(PROTECTED_PORT)
            if not still:
                self._log("error", "[monitor] 警告：操作后 3002 端口已无 LISTENING，可能误伤")
            else:
                self._log("info", f"[monitor] 校验：3002 实例仍存活 PID={sorted(still)}")
        remaining_3001 = self._pids_listening_on(TARGET_PORT)
        if remaining_3001:
            self._log(
                "warn",
                f"[monitor] 3001 仍被占用 PID={sorted(remaining_3001)}（可能需要管理员权限）",
            )

    @staticmethod
    def _pids_listening_on(port: int) -> set[int]:
        """用 netstat -ano 找在指定端口 LISTENING 的 PID 集合。"""
        try:
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:
            return set()
        pids: set[int] = set()
        suffix = f":{port}"
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            if parts[0].upper() != "TCP":
                continue
            local, _state = parts[1], parts[3]
            if _state.upper() != "LISTENING":
                continue
            if not local.endswith(suffix):
                continue
            try:
                pids.add(int(parts[4]))
            except ValueError:
                continue
        return pids

    def _remove_old_qrcode(self) -> None:
        try:
            if self._qrcode_path.exists():
                self._qrcode_path.unlink()
                self._log("info", f"[monitor] 已删除旧二维码 {self._qrcode_path.name}")
        except Exception as e:
            self._log("warn", f"[monitor] 删除旧二维码失败: {e}")

    def _launch_napcat(self) -> None:
        if not self._napcat_bat.exists():
            raise FileNotFoundError(f"napcat.bat 不存在: {self._napcat_bat}")
        # 用 start 新开一个独立窗口运行 napcat.bat
        subprocess.Popen(
            ["cmd", "/c", "start", "", str(self._napcat_bat)],
            cwd=str(self._napcat_bat.parent),
            shell=False,
        )
        self._log("info", f"[monitor] 已启动 napcat.bat ({self._napcat_bat})")

    def _wait_qrcode(self) -> bytes | None:
        cache_dir = self._qrcode_path.parent
        cache_dir.mkdir(parents=True, exist_ok=True)
        evt = threading.Event()
        handler = _QrcodeWaiter(self._qrcode_path.name, evt)
        observer = Observer()
        observer.schedule(handler, str(cache_dir), recursive=False)
        observer.start()
        try:
            # 边等 watchdog 边轮询（防止启动瞬间已生成、错过事件）
            deadline = time.time() + QRCODE_WAIT_TIMEOUT_SEC
            while time.time() < deadline:
                if self._qrcode_path.exists():
                    # 等文件写完
                    time.sleep(QRCODE_STABILIZE_SEC)
                    try:
                        return self._qrcode_path.read_bytes()
                    except Exception:
                        continue
                if evt.wait(timeout=1.0):
                    evt.clear()
                    if self._qrcode_path.exists():
                        time.sleep(QRCODE_STABILIZE_SEC)
                        try:
                            return self._qrcode_path.read_bytes()
                        except Exception:
                            continue
            return None
        finally:
            observer.stop()
            observer.join(timeout=2)

    def _send_qrcode_when_ready(self, qrcode_bytes: bytes) -> bool:
        admin_qq = self._get_admin_qq()
        if not admin_qq:
            self._log("error", "[monitor] 未配置管理员 QQ，无法发送二维码")
            return False

        deadline = time.time() + MAIN_RECONNECT_WAIT_SEC
        while time.time() < deadline:
            if self._is_main_connected():
                break
            time.sleep(1)
        else:
            return False

        img_b64 = base64.b64encode(qrcode_bytes).decode("ascii")
        message = [
            {"type": "text", "data": {"text": "NapCat 已重启，请扫码登录："}},
            {"type": "image", "data": {"file": f"base64://{img_b64}"}},
        ]
        try:
            self._send_private_msg(admin_qq, message)
            return True
        except Exception as e:
            self._log("error", f"[monitor] 发送二维码私聊失败: {e}")
            return False

    # ---------- 状态 ----------

    def _set_state(self, state: str) -> None:
        self._state = state
        if self._on_state_change:
            try:
                self._on_state_change(state)
            except Exception:
                pass
