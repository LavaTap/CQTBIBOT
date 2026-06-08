"""QQ群历史消息拉取与合并转发工具

基于 OneBot v11 协议（NapCatQQ）：通过 WebSocket 连接 NapCat，
调用 get_group_msg_history 拉取群历史消息，构造合并转发节点，
调用 send_group_forward_msg 发送到目标群。无需额外 HTTP 服务。

使用前：
  1. 安装并启动 NapCatQQ，配置正向 WebSocket 端口（默认 3001）
  2. 运行本脚本，填写配置，点击"拉取并转发"

依赖：pip install websocket-client
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import websocket

# ---------- 常量 ----------
PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_FILE = PROJECT_ROOT / "history_config.json"

# ---------- 日志 ----------
log = logging.getLogger("qq_history")


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s | %(message)s", datefmt="%H:%M:%S"))
    root.addHandler(h)


# ---------- 配置 ----------
@dataclass
class HistoryConfig:
    """历史消息配置，支持多 WebSocket 连接。

    connections 格式:
        { "连接名": {"ws_url": "ws://...", "access_token": "..."} }
    """
    connections: dict = None
    default_connection: str = ""
    ws_url: str = "ws://127.0.0.1:3001"
    access_token: str = ""
    source_group: int = 0
    target_group: int = 0
    count: int = 20

    def __post_init__(self) -> None:
        if self.connections is None:
            self.connections = {}
            if self.ws_url:
                self.connections["main"] = {
                    "ws_url": self.ws_url,
                    "access_token": self.access_token,
                }
                self.default_connection = "main"

    @property
    def _current_conn(self) -> dict:
        conn = self.connections.get(self.default_connection) if self.default_connection else None
        if conn:
            return conn
        return {"ws_url": self.ws_url, "access_token": self.access_token}

    def save(self) -> None:
        CONFIG_FILE.write_text(
            json.dumps(self._to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls) -> HistoryConfig:
        if not CONFIG_FILE.exists():
            return cls()
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            # 解析多连接格式
            connections = data.get("connections")
            if isinstance(connections, dict) and connections:
                default_name = data.get("default_connection") or next(iter(connections))
                conn = connections.get(default_name, {})
                return cls(
                    connections=connections,
                    default_connection=default_name,
                    ws_url=conn.get("ws_url", "ws://127.0.0.1:3001"),
                    access_token=conn.get("access_token", ""),
                    source_group=int(data.get("source_group", 0)),
                    target_group=int(data.get("target_group", 0)),
                    count=int(data.get("count", 20)),
                )
            # 旧格式兼容
            return cls(
                ws_url=data.get("ws_url", "ws://127.0.0.1:3001"),
                access_token=data.get("access_token", ""),
                source_group=int(data.get("source_group", 0)),
                target_group=int(data.get("target_group", 0)),
                count=int(data.get("count", 20)),
            )
        except (OSError, json.JSONDecodeError, ValueError):
            return cls()

    def _to_dict(self) -> dict:
        return {
            "connections": self.connections or {
                self.default_connection or "main": {
                    "ws_url": self.ws_url,
                    "access_token": self.access_token,
                }
            },
            "default_connection": self.default_connection or "main",
            "source_group": self.source_group,
            "target_group": self.target_group,
            "count": self.count,
        }


# ---------- NapCat WebSocket 客户端 ----------
class NapCatWS:
    """通过 WebSocket 调用 OneBot v11 action，同步等待响应。"""

    def __init__(self, ws_url: str, access_token: str = "") -> None:
        self._ws_url = ws_url
        self._access_token = access_token
        self._ws: websocket.WebSocket | None = None
        self._echo_counter = 0
        self._pending: dict[str, threading.Event] = {}
        self._results: dict[str, dict] = {}

    def connect(self) -> None:
        url = self._ws_url
        if self._access_token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}access_token={self._access_token}"
        self._ws = websocket.create_connection(url, timeout=10)

    def close(self) -> None:
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def _next_echo(self) -> str:
        self._echo_counter += 1
        return f"hist_{self._echo_counter}"

    def _call_action(self, action: str, params: dict, timeout: float = 15.0) -> dict:
        """发送 action 请求并同步等待响应。"""
        if not self._ws:
            raise RuntimeError("WebSocket 未连接")
        echo = self._next_echo()
        ev = threading.Event()
        self._pending[echo] = ev
        payload = {"action": action, "params": params, "echo": echo}
        self._ws.send(json.dumps(payload))
        if not ev.wait(timeout):
            self._pending.pop(echo, None)
            raise TimeoutError(f"等待 {action} 响应超时")
        result = self._results.pop(echo, {})
        self._pending.pop(echo, None)
        return result

    def _recv_loop(self, echo_set: set[str]) -> None:
        """短接收循环：只匹配 echo_set 中的响应。"""
        if not self._ws:
            return
        self._ws.settimeout(15)
        try:
            while echo_set:
                raw = self._ws.recv()
                if not raw:
                    break
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                echo = data.get("echo", "")
                if echo in echo_set:
                    self._results[echo] = data
                    echo_set.discard(echo)
                    ev = self._pending.get(echo)
                    if ev:
                        ev.set()
        except websocket.WebSocketTimeoutException:
            pass

    def get_group_msg_history(self, group_id: int, count: int = 20) -> list[dict]:
        """拉取群历史消息。尝试不带 message_seq 和带 message_seq=0 两种方式。"""
        # 方式一：不带 message_seq（从最新消息向前回溯）
        result = self._try_fetch(group_id, count, message_seq=None)
        if result is not None:
            return result
        # 方式二：带 message_seq=0（从最新消息开始）
        result = self._try_fetch(group_id, count, message_seq=0)
        if result is not None:
            return result
        raise RuntimeError("get_group_msg_history 失败（两种参数组合均无有效返回）")

    def _try_fetch(self, group_id: int, count: int,
                   message_seq: int | None) -> list[dict] | None:
        """尝试一次拉取，成功返回消息列表，失败返回 None。"""
        echo = self._next_echo()
        ev = threading.Event()
        self._pending[echo] = ev
        echo_set = {echo}

        params = {"group_id": group_id, "count": count}
        if message_seq is not None:
            params["message_seq"] = message_seq

        payload = {
            "action": "get_group_msg_history",
            "params": params,
            "echo": echo,
        }
        self._ws.send(json.dumps(payload))
        self._recv_loop(echo_set)

        result = self._results.pop(echo, {})
        self._pending.pop(echo, None)

        if result.get("status") != "ok":
            raw = json.dumps(result, ensure_ascii=False)
            log.warning("get_group_msg_history 返回失败: %s", raw)
            return None
        return result.get("data", {}).get("messages", [])

    def send_group_forward_msg(self, group_id: int, messages: list[dict]) -> dict:
        """发送群合并转发消息。"""
        echo = self._next_echo()
        ev = threading.Event()
        self._pending[echo] = ev
        echo_set = {echo}

        payload = {
            "action": "send_group_forward_msg",
            "params": {"group_id": group_id, "messages": messages},
            "echo": echo,
        }
        self._ws.send(json.dumps(payload))

        self._recv_loop(echo_set)
        result = self._results.pop(echo, {})
        self._pending.pop(echo, None)

        if result.get("status") != "ok":
            wording = result.get("wording", result.get("status", "unknown"))
            raise RuntimeError(f"send_group_forward_msg 失败: {wording}")
        return result


# ---------- 消息转换 ----------
def messages_to_forward_nodes(msgs: list[dict]) -> list[dict]:
    """将 get_group_msg_history 返回的消息列表转为合并转发 node 数组。"""
    nodes: list[dict] = []
    for msg in msgs:
        sender = msg.get("sender", {})
        name = sender.get("card") or sender.get("nickname") or str(msg.get("user_id", ""))
        uin = str(msg.get("user_id", ""))
        content = msg.get("message", [])
        if not content and msg.get("raw_message"):
            content = [{"type": "text", "data": {"text": msg["raw_message"]}}]
        if not content:
            continue
        nodes.append({
            "type": "node",
            "data": {"name": name, "uin": uin, "content": content},
        })
    return nodes


# ---------- GUI ----------
class HistoryApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("QQ 群历史消息拉取与合并转发")
        self.geometry("540x460")
        self.resizable(True, True)

        self.config = HistoryConfig.load()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        # --- 连接配置 ---
        conn_frame = ttk.LabelFrame(self, text="NapCat 连接（WebSocket）")
        conn_frame.pack(fill="x", **pad)

        # 连接选择器
        row_sel = ttk.Frame(conn_frame)
        row_sel.pack(fill="x", padx=6, pady=(4, 0))
        ttk.Label(row_sel, text="连接:").pack(side="left")
        self.conn_names = list(self.config.connections.keys()) or ["main"]
        self.conn_var = tk.StringVar(value=self.config.default_connection or self.conn_names[0])
        self.conn_combo = ttk.Combobox(
            row_sel, textvariable=self.conn_var, values=self.conn_names,
            state="readonly", width=18,
        )
        self.conn_combo.pack(side="left", padx=4)
        self.conn_combo.bind("<<ComboboxSelected>>", self._on_conn_switch)
        self.add_conn_btn = ttk.Button(row_sel, text="+", width=3, command=self._add_connection)
        self.add_conn_btn.pack(side="left", padx=2)
        self.del_conn_btn = ttk.Button(row_sel, text="-", width=3, command=self._del_connection)
        self.del_conn_btn.pack(side="left", padx=2)

        row0 = ttk.Frame(conn_frame)
        row0.pack(fill="x", padx=6, pady=4)
        ttk.Label(row0, text="WS 地址:").pack(side="left")
        self.ws_var = tk.StringVar(value=self.config.ws_url)
        ttk.Entry(row0, textvariable=self.ws_var, width=30).pack(side="left", padx=4)

        row1 = ttk.Frame(conn_frame)
        row1.pack(fill="x", padx=6, pady=4)
        ttk.Label(row1, text="Token:").pack(side="left")
        self.token_var = tk.StringVar(value=self.config.access_token)
        ttk.Entry(row1, textvariable=self.token_var, width=30).pack(side="left", padx=4)

        # --- 拉取配置 ---
        fetch_frame = ttk.LabelFrame(self, text="拉取与转发配置")
        fetch_frame.pack(fill="x", **pad)

        row2 = ttk.Frame(fetch_frame)
        row2.pack(fill="x", padx=6, pady=4)
        ttk.Label(row2, text="源群号:").pack(side="left")
        self.src_var = tk.StringVar(value=str(self.config.source_group) if self.config.source_group else "")
        ttk.Entry(row2, textvariable=self.src_var, width=15).pack(side="left", padx=4)
        tk.Label(row2, text="(拉取此群的历史消息)", fg="#888").pack(side="left")

        row3 = ttk.Frame(fetch_frame)
        row3.pack(fill="x", padx=6, pady=4)
        ttk.Label(row3, text="目标群号:").pack(side="left")
        self.tgt_var = tk.StringVar(value=str(self.config.target_group) if self.config.target_group else "")
        ttk.Entry(row3, textvariable=self.tgt_var, width=15).pack(side="left", padx=4)
        tk.Label(row3, text="(合并转发到此群)", fg="#888").pack(side="left")

        row4 = ttk.Frame(fetch_frame)
        row4.pack(fill="x", padx=6, pady=4)
        ttk.Label(row4, text="消息条数:").pack(side="left")
        self.count_var = tk.StringVar(value=str(self.config.count))
        ttk.Entry(row4, textvariable=self.count_var, width=6).pack(side="left", padx=4)
        tk.Label(row4, text="范围 1-100，默认 20", fg="#888").pack(side="left")

        # --- 控制区 ---
        ctrl_frame = ttk.Frame(self)
        ctrl_frame.pack(fill="x", padx=8, pady=4)
        self.action_btn = ttk.Button(ctrl_frame, text="拉取并转发", command=self._do_action)
        self.action_btn.pack(side="left", padx=4)
        self.status_var = tk.StringVar(value="就绪")
        tk.Label(ctrl_frame, textvariable=self.status_var, fg="#0a0").pack(side="right", padx=4)

        # --- 提示 ---
        tk.Label(
            self,
            text="提示：复用 NapCat WebSocket 服务（与 QQ 转发工具共用），无需额外配置 HTTP",
            fg="#888",
        ).pack(fill="x", padx=14, pady=2)

        # --- 日志区 ---
        log_frame = ttk.LabelFrame(self, text="运行日志")
        log_frame.pack(fill="both", expand=True, **pad)

        self.log_text = tk.Text(log_frame, wrap="word", state="disabled", font=("Consolas", 9))
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=4)
        scrollbar.pack(side="right", fill="y", pady=4, padx=(0, 6))

        self.log_text.tag_configure("info", foreground="#333")
        self.log_text.tag_configure("error", foreground="#cc0000")
        self.log_text.tag_configure("success", foreground="#006600")

    def _log(self, level: str, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line, (level,))
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ======================== 连接管理 ========================

    def _sync_conn_to_config(self) -> None:
        """将当前 UI 的 ws_url/token 同步到 config.connections 中的当前连接。"""
        name = self.conn_var.get()
        if not name:
            return
        self.config.connections[name] = {
            "ws_url": self.ws_var.get().strip(),
            "access_token": self.token_var.get().strip(),
        }
        self.config.default_connection = name

    def _refresh_conn_combo(self) -> None:
        names = list(self.config.connections.keys())
        self.conn_combo["values"] = names
        if self.conn_var.get() not in names:
            self.conn_var.set(names[0] if names else "main")

    def _on_conn_switch(self, event=None) -> None:
        old_name = self.config.default_connection
        if old_name:
            self.config.connections[old_name] = {
                "ws_url": self.ws_var.get().strip(),
                "access_token": self.token_var.get().strip(),
            }
        new_name = self.conn_var.get()
        conn = self.config.connections.get(new_name, {})
        self.config.default_connection = new_name
        self.config.ws_url = conn.get("ws_url", "ws://127.0.0.1:3001")
        self.config.access_token = conn.get("access_token", "")
        self.ws_var.set(self.config.ws_url)
        self.token_var.set(self.config.access_token)

    def _add_connection(self) -> None:
        import tkinter.simpledialog as sd
        name = sd.askstring("新建连接", "请输入连接名称:", parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        if name in self.config.connections:
            messagebox.showwarning("重复", f"连接名 '{name}' 已存在")
            return
        self.config.connections[name] = {
            "ws_url": "ws://127.0.0.1:3001",
            "access_token": "",
        }
        self.conn_var.set(name)
        self._on_conn_switch()
        self._refresh_conn_combo()

    def _del_connection(self) -> None:
        name = self.conn_var.get()
        if len(self.config.connections) <= 1:
            messagebox.showwarning("禁止删除", "至少保留一个连接")
            return
        if not messagebox.askyesno("确认删除", f"确定删除连接 '{name}'?"):
            return
        del self.config.connections[name]
        new_name = next(iter(self.config.connections))
        self.conn_var.set(new_name)
        self._on_conn_switch()
        self._refresh_conn_combo()

    def _save_config(self) -> None:
        self._sync_conn_to_config()
        try:
            self.config.source_group = int(self.src_var.get().strip())
        except ValueError:
            self.config.source_group = 0
        try:
            self.config.target_group = int(self.tgt_var.get().strip())
        except ValueError:
            self.config.target_group = 0
        try:
            self.config.count = max(1, min(100, int(self.count_var.get().strip())))
        except ValueError:
            self.config.count = 20
        self.config.save()

    def _do_action(self) -> None:
        try:
            src = int(self.src_var.get().strip())
        except ValueError:
            messagebox.showwarning("参数错误", "源群号必须是数字")
            return
        try:
            tgt = int(self.tgt_var.get().strip())
        except ValueError:
            messagebox.showwarning("参数错误", "目标群号必须是数字")
            return
        try:
            count = max(1, min(100, int(self.count_var.get().strip())))
        except ValueError:
            messagebox.showwarning("参数错误", "消息条数必须是 1-100 的数字")
            return
        if src == 0 or tgt == 0:
            messagebox.showwarning("缺参数", "源群号和目标群号不能为空")
            return

        self._save_config()
        self.action_btn.configure(state="disabled")
        self.status_var.set("拉取中…")
        threading.Thread(target=self._bg_action, args=(src, tgt, count), daemon=True).start()

    def _bg_action(self, src: int, tgt: int, count: int) -> None:
        client = NapCatWS(self.config.ws_url, self.config.access_token)
        try:
            # 1. 连接
            self.after(0, lambda: self._log("info", "正在连接 NapCat…"))
            try:
                client.connect()
            except Exception as e:
                self.after(0, lambda: self._log("error", f"连接失败: {e}"))
                self.after(0, lambda: self._on_done("连接失败"))
                return
            self.after(0, lambda: self._log("info", "已连接"))

            # 2. 拉取历史消息
            self.after(0, lambda: self._log("info", f"正在拉取群 {src} 的最近 {count} 条消息…"))
            msgs = client.get_group_msg_history(src, count)
            self.after(0, lambda: self._log("info", f"拉取到 {len(msgs)} 条消息"))

            if not msgs:
                self.after(0, lambda: self._log("info", "无消息可转发"))
                self.after(0, lambda: self._on_done("无消息"))
                return

            # 3. 构造合并转发节点
            nodes = messages_to_forward_nodes(msgs)
            self.after(0, lambda: self._log("info", f"构造 {len(nodes)} 个转发节点"))

            if not nodes:
                self.after(0, lambda: self._log("error", "消息内容均为空，无法转发"))
                self.after(0, lambda: self._on_done("失败"))
                return

            # 4. 合并转发到目标群
            self.after(0, lambda: self._log("info", f"正在合并转发到群 {tgt}…"))
            client.send_group_forward_msg(tgt, nodes)
            self.after(0, lambda: self._log("success", f"合并转发成功: {len(nodes)} 条消息 → 群 {tgt}"))
            self.after(0, lambda: self._on_done("转发成功"))

        except TimeoutError as e:
            self.after(0, lambda: self._log("error", f"操作超时: {e}"))
            self.after(0, lambda: self._on_done("超时"))
        except Exception as e:
            self.after(0, lambda: self._log("error", f"操作失败: {e}"))
            self.after(0, lambda: self._on_done("失败"))
        finally:
            client.close()

    def _on_done(self, status: str) -> None:
        self.status_var.set(status)
        try:
            self.action_btn.configure(state="normal")
        except tk.TclError:
            pass

    def _on_close(self) -> None:
        self._save_config()
        self.destroy()


# ---------- 入口 ----------
def main() -> None:
    setup_logging()
    log.info("QQ群历史消息工具启动")
    HistoryApp().mainloop()
    log.info("QQ群历史消息工具退出")


if __name__ == "__main__":
    main()
