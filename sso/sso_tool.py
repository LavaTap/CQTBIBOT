"""重庆工商职业学院 SSO 登录接口抓包工具（v2）。

本工具内嵌 mitmproxy 监听 127.0.0.1:8080，把浏览器流量转发出去同时
丢给我们的 addon。GUI 实时列出请求；带学号/密码字段的请求会被自动
标红并置顶 —— 这就是我们要抓的「登录接口」。

工作流：
1. 启动工具 → 顶栏「启动代理」（默认自动开）
2. 点「设置系统代理」一键写注册表，把 Edge/Chrome 代理指向我们
3. 浏览器登录 SSO，按平时一样输学号密码验证码
4. 列表里被高亮置顶的那条就是真实登录接口；点开看 body 字段
5. 点「保存为登录配置」把接口固化到 accounts.json 备后续脱代理复现
6. 关窗 → 自动恢复系统代理
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import queue
import threading
import time
import tkinter as tk
import winreg
from datetime import datetime
from tkinter import messagebox, scrolledtext, ttk
from urllib.parse import parse_qs, urlparse

from mitmproxy import options
from mitmproxy.tools.dump import DumpMaster

from sso.sso_common import (
    APPID,
    AUTH_URL_TEMPLATE,
    DATA_FILE,
    DEFAULT_REDIRECT,
    exchange_code_for_token,
    load_data,
    redact,
    save_data,
    setup_logging,
)

log = logging.getLogger("sso.tool")


# ---------- sso_tool 专用常量 ----------
TARGET_HOST_KEYWORD = "cqtbi.edu.cn"

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 8080
PROXY_OVERRIDE = "<local>;localhost;127.*;10.*;172.16.*;172.17.*;192.168.*"

INTERNET_SETTINGS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

LOGIN_KEYWORDS_BODY = ("password", "passwd", "pwd", "captcha", "verify", "checkcode")
LOGIN_KEYWORDS_URL = ("login", "signin", "/auth", "logon")


# ---------- Windows 系统代理 ----------
class SystemProxy:
    """读写 HKCU Internet Settings，并在关窗时还原。"""

    def __init__(self) -> None:
        self._saved: dict[str, tuple[int, object]] | None = None

    def _open(self, write: bool = False):
        flag = winreg.KEY_WRITE | winreg.KEY_READ if write else winreg.KEY_READ
        return winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS_KEY, 0, flag)

    def _read_value(self, key, name: str):
        try:
            return winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return None

    def snapshot(self) -> None:
        if self._saved is not None:
            return
        with self._open() as key:
            self._saved = {
                "ProxyEnable": self._read_value(key, "ProxyEnable"),
                "ProxyServer": self._read_value(key, "ProxyServer"),
                "ProxyOverride": self._read_value(key, "ProxyOverride"),
            }

    @staticmethod
    def _notify() -> None:
        """通知 Wininet 设置已变化。"""
        INTERNET_OPTION_SETTINGS_CHANGED = 39
        INTERNET_OPTION_REFRESH = 37
        wininet = ctypes.windll.Wininet
        wininet.InternetSetOptionW(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
        wininet.InternetSetOptionW(0, INTERNET_OPTION_REFRESH, 0, 0)

    def enable(self, server: str, override: str = PROXY_OVERRIDE) -> None:
        self.snapshot()
        with self._open(write=True) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, server)
            winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, override)
        self._notify()
        log.info("系统代理已启用 -> %s", server)

    def restore(self) -> None:
        if self._saved is None:
            return
        with self._open(write=True) as key:
            for name, val in self._saved.items():
                if val is None:
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
                else:
                    data, typ = val
                    winreg.SetValueEx(key, name, 0, typ, data)
        self._notify()
        self._saved = None
        log.info("系统代理已恢复")


# ---------- mitmproxy addon ----------
class CaptureAddon:
    """每个完整 response 进来时把摘要塞进队列给 UI 消费。"""

    def __init__(self, q: queue.Queue) -> None:
        self.q = q

    def response(self, flow):  # mitmproxy hook
        try:
            host = flow.request.host or ""
            if TARGET_HOST_KEYWORD not in host:
                return
            req_body = ""
            try:
                req_body = flow.request.get_text() or ""
            except Exception:  # noqa: BLE001
                req_body = "<binary>"
            resp_body = ""
            try:
                resp_body = flow.response.get_text(strict=False) or ""
            except Exception:  # noqa: BLE001
                resp_body = "<binary>"

            score = _score_login(flow.request.method, flow.request.pretty_url, req_body)
            log.info(
                "捕获 [%s] %s -> %s score=%d",
                flow.request.method,
                flow.request.pretty_url,
                flow.response.status_code,
                score,
            )
            if score > 0:
                log.info("  ↳ 疑似登录请求 body: %s", redact(req_body[:300]))

            self.q.put(
                {
                    "id": flow.id,
                    "ts": time.time(),
                    "method": flow.request.method,
                    "url": flow.request.pretty_url,
                    "host": host,
                    "status": flow.response.status_code,
                    "req_headers": dict(flow.request.headers),
                    "req_body": req_body,
                    "resp_headers": dict(flow.response.headers),
                    "resp_body": resp_body[:50000],  # 截断防 UI 卡死
                    "score": score,
                }
            )
        except Exception as e:  # noqa: BLE001
            log.exception("addon response 异常")
            self.q.put({"id": "err", "error": str(e)})


def _score_login(method: str, url: str, body: str) -> int:
    score = 0
    body_l = body.lower()
    url_l = url.lower()
    if any(k in body_l for k in LOGIN_KEYWORDS_BODY):
        score += 10
    if method.upper() == "POST" and any(k in url_l for k in LOGIN_KEYWORDS_URL):
        score += 5
    if "captcha" in body_l or "checkcode" in body_l or "verify" in body_l:
        score += 3
    return score


# ---------- 代理线程 ----------
class ProxyThread:
    def __init__(self, addon: CaptureAddon, host: str, port: int) -> None:
        self.addon = addon
        self.host = host
        self.port = port
        self.master: DumpMaster | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.ready = threading.Event()
        self.error: str | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.ready.wait(timeout=15)

    def _run(self) -> None:
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._serve())
        except Exception as e:  # noqa: BLE001
            log.exception("代理线程异常退出")
            self.error = str(e)
            self.ready.set()

    async def _serve(self) -> None:
        opts = options.Options(
            listen_host=self.host,
            listen_port=self.port,
        )
        self.master = DumpMaster(
            opts, with_termlog=False, with_dumper=False
        )
        self.master.addons.add(self.addon)
        self.ready.set()
        log.info("代理监听 %s:%s 已就绪", self.host, self.port)
        await self.master.run()
        log.info("代理已停止")

    def stop(self) -> None:
        if self.master and self.loop:
            log.info("发送代理停止信号")
            try:
                self.loop.call_soon_threadsafe(self.master.shutdown)
            except Exception:  # noqa: BLE001
                pass


# ---------- GUI ----------
class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("SSO 抓包工具（mitmproxy）")
        self.geometry("1400x850")

        self.data = load_data()
        self.flow_queue: queue.Queue = queue.Queue()
        self.flows: dict[str, dict] = {}  # id -> flow dict
        self.sysproxy = SystemProxy()
        self.proxy: ProxyThread | None = None

        self._build_ui()
        self._start_proxy()
        self.after(200, self._drain_queue)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----- UI -----
    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=6)
        top.pack(side="top", fill="x")

        self.status_var = tk.StringVar(value="代理未启动")
        ttk.Label(top, textvariable=self.status_var, foreground="#0a0", font=("", 10, "bold")).pack(side="left")

        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(top, text="设置系统代理", command=self._enable_proxy).pack(side="left", padx=2)
        ttk.Button(top, text="取消系统代理", command=self._disable_proxy).pack(side="left", padx=2)
        ttk.Button(top, text="打开证书目录", command=self._open_cert_dir).pack(side="left", padx=2)
        ttk.Button(top, text="打开授权页 (复制 URL)", command=self._copy_auth_url).pack(side="left", padx=2)
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(top, text="清空列表", command=self._clear_list).pack(side="left", padx=2)
        self.only_login_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="只看疑似登录请求", variable=self.only_login_var, command=self._rebuild_list).pack(side="left", padx=4)

        # 主分栏
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)

        # 左：请求列表
        left = ttk.Frame(paned)
        cols = ("score", "method", "status", "host", "path")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", height=30)
        for c, w in zip(cols, (50, 60, 60, 220, 600)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="w")
        self.tree.tag_configure("login", background="#ffe0e0")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_pick)
        paned.add(left, weight=3)

        # 右：详情
        right = ttk.Frame(paned, padding=6)
        paned.add(right, weight=2)
        self.detail_meta = tk.StringVar(value="<选择一条请求>")
        ttk.Label(right, textvariable=self.detail_meta, font=("", 10, "bold"), wraplength=520, justify="left").pack(anchor="w", fill="x")

        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True, pady=4)

        self.req_h_box = self._make_box(nb, "请求 Headers")
        self.req_b_box = self._make_box(nb, "请求 Body")
        self.resp_h_box = self._make_box(nb, "响应 Headers")
        self.resp_b_box = self._make_box(nb, "响应 Body")

        btns = ttk.Frame(right)
        btns.pack(fill="x", pady=4)
        ttk.Button(btns, text="复制为 cURL", command=self._copy_curl).pack(side="left", padx=2)
        ttk.Button(btns, text="复制为 requests 代码", command=self._copy_py).pack(side="left", padx=2)
        ttk.Button(btns, text="保存为登录配置", command=self._save_as_endpoint).pack(side="left", padx=2)

        # 状态栏
        self.bottom_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.bottom_var, anchor="w").pack(side="bottom", fill="x")

    def _make_box(self, nb, title):
        f = ttk.Frame(nb)
        nb.add(f, text=title)
        box = scrolledtext.ScrolledText(f, wrap="word", height=14)
        box.pack(fill="both", expand=True)
        return box

    # ----- 代理控制 -----
    def _start_proxy(self) -> None:
        addon = CaptureAddon(self.flow_queue)
        self.proxy = ProxyThread(addon, PROXY_HOST, PROXY_PORT)
        self.proxy.start()
        if self.proxy.error:
            self.status_var.set(f"代理启动失败: {self.proxy.error}")
            messagebox.showerror("启动失败", self.proxy.error)
            return
        self.status_var.set(f"代理运行中: {PROXY_HOST}:{PROXY_PORT}")
        self.bottom_var.set(
            f"接下来：点「设置系统代理」→ 打开 Edge/Chrome 访问 SSO 登录页 → 登录"
        )

    def _enable_proxy(self) -> None:
        try:
            self.sysproxy.enable(f"{PROXY_HOST}:{PROXY_PORT}")
            messagebox.showinfo("已设置", "系统代理已指向 mitmproxy。\n现在打开浏览器登录即可。")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("失败", str(e))

    def _disable_proxy(self) -> None:
        try:
            self.sysproxy.restore()
            messagebox.showinfo("已恢复", "系统代理设置已还原。")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("失败", str(e))

    def _open_cert_dir(self) -> None:
        cert_dir = Path.home() / ".mitmproxy"
        if not cert_dir.exists():
            messagebox.showinfo(
                "等等",
                "证书还没生成。先让代理跑一会（任意请求经过即可），再来这里。",
            )
            return
        try:
            os.startfile(cert_dir)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("失败", str(e))

    def _copy_auth_url(self) -> None:
        url = AUTH_URL_TEMPLATE.format(appid=APPID, redirect_uri=DEFAULT_REDIRECT)
        self.clipboard_clear()
        self.clipboard_append(url)
        messagebox.showinfo("已复制", f"授权页 URL 已复制：\n{url}\n\n粘贴到浏览器打开。")

    # ----- 列表 -----
    def _drain_queue(self) -> None:
        try:
            while True:
                evt = self.flow_queue.get_nowait()
                if "error" in evt:
                    self.bottom_var.set(f"addon 错误: {evt['error']}")
                    continue
                self.flows[evt["id"]] = evt
                if not self.only_login_var.get() or evt["score"] > 0:
                    self._insert_tree(evt)
        except queue.Empty:
            pass
        self.after(200, self._drain_queue)

    def _insert_tree(self, evt: dict) -> None:
        parsed = urlparse(evt["url"])
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        tags = ("login",) if evt["score"] > 0 else ()
        # 高分置顶
        idx = 0 if evt["score"] > 0 else "end"
        self.tree.insert(
            "",
            idx,
            iid=evt["id"],
            values=(evt["score"], evt["method"], evt["status"], evt["host"], path),
            tags=tags,
        )

    def _rebuild_list(self) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for evt in sorted(self.flows.values(), key=lambda x: (-x["score"], x["ts"])):
            if self.only_login_var.get() and evt["score"] == 0:
                continue
            self._insert_tree(evt)

    def _clear_list(self) -> None:
        self.flows.clear()
        for iid in self.tree.get_children():
            self.tree.delete(iid)

    # ----- 详情 -----
    def _current(self) -> dict | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return self.flows.get(sel[0])

    def _on_pick(self, _e=None) -> None:
        evt = self._current()
        if not evt:
            return
        self.detail_meta.set(
            f"[{evt['method']}] {evt['status']}  {evt['url']}\n"
            f"score={evt['score']}  time={datetime.fromtimestamp(evt['ts']):%H:%M:%S}"
        )
        self._set_box(self.req_h_box, _format_headers(evt["req_headers"]))
        self._set_box(self.req_b_box, _try_pretty(evt["req_body"]))
        self._set_box(self.resp_h_box, _format_headers(evt["resp_headers"]))
        self._set_box(self.resp_b_box, _try_pretty(evt["resp_body"]))

    @staticmethod
    def _set_box(box: scrolledtext.ScrolledText, text: str) -> None:
        box.delete("1.0", "end")
        box.insert("1.0", text)

    # ----- 复制 / 保存 -----
    def _copy_curl(self) -> None:
        evt = self._current()
        if not evt:
            return
        parts = [f'curl -X {evt["method"]} "{evt["url"]}"']
        for k, v in evt["req_headers"].items():
            parts.append(f'  -H "{k}: {v}"')
        if evt["req_body"]:
            body = evt["req_body"].replace('"', '\\"')
            parts.append(f'  --data "{body}"')
        text = " \\\n".join(parts)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.bottom_var.set("cURL 已复制")

    def _copy_py(self) -> None:
        evt = self._current()
        if not evt:
            return
        headers_repr = json.dumps(evt["req_headers"], ensure_ascii=False, indent=4)
        body_repr = repr(evt["req_body"])
        text = (
            "import requests\n\n"
            f"url = {evt['url']!r}\n"
            f"headers = {headers_repr}\n"
            f"data = {body_repr}\n"
            f"r = requests.request({evt['method']!r}, url, headers=headers, data=data)\n"
            "print(r.status_code, r.text)\n"
        )
        self.clipboard_clear()
        self.clipboard_append(text)
        self.bottom_var.set("Python 代码已复制")

    def _save_as_endpoint(self) -> None:
        evt = self._current()
        if not evt:
            return
        fields = _parse_body_fields(
            evt["req_body"],
            evt["req_headers"].get("content-type") or evt["req_headers"].get("Content-Type", ""),
        )
        self.data["login_endpoint"] = {
            "url": evt["url"],
            "method": evt["method"],
            "content_type": evt["req_headers"].get("content-type")
            or evt["req_headers"].get("Content-Type", ""),
            "fields": fields,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "sample_request_headers": evt["req_headers"],
            "sample_response_status": evt["status"],
        }
        save_data(self.data)
        messagebox.showinfo(
            "已保存",
            f"登录接口已写入 {DATA_FILE}\n字段：{list(fields.keys())}",
        )

    # ----- 关窗 -----
    def _on_close(self) -> None:
        try:
            self.sysproxy.restore()
        except Exception:  # noqa: BLE001
            pass
        if self.proxy:
            self.proxy.stop()
        save_data(self.data)
        # 给代理线程一点时间关
        self.after(200, self.destroy)


# ---------- 辅助 ----------
def _format_headers(h: dict) -> str:
    return "\n".join(f"{k}: {v}" for k, v in h.items())


def _try_pretty(text: str) -> str:
    if not text:
        return ""
    s = text.strip()
    if s.startswith(("{", "[")):
        try:
            return json.dumps(json.loads(s), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pass
    return text


def _parse_body_fields(body: str, content_type: str) -> dict:
    if not body:
        return {}
    ct = (content_type or "").lower()
    if "json" in ct:
        try:
            obj = json.loads(body)
            if isinstance(obj, dict):
                return {k: ("<...>" if v else "") for k, v in obj.items()}
        except json.JSONDecodeError:
            pass
    if "form" in ct or "&" in body or "=" in body:
        try:
            parsed = parse_qs(body, keep_blank_values=True)
            return {k: "<...>" for k in parsed.keys()}
        except Exception:  # noqa: BLE001
            pass
    return {"_raw": body[:200]}


def main() -> None:
    setup_logging()
    log.info("sso_tool 启动")
    App().mainloop()
    log.info("sso_tool 退出")


if __name__ == "__main__":
    main()
