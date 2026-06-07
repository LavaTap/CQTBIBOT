"""重庆工商职业学院 SSO 自动登录工具（v3）。

脱离代理，直接复现学校 Login.js 的登录流程：
  1) GET auth2orize → 服务器 302 → 拿 accKey
  2) GET createVertifyCode?checkId=<accKey> → 验证码图片
  3) JS RSA(PKCS#1 v1.5) 加密密码
  4) POST auth2Login → 拿到 redirect_url 里的 code
  5) GET access_token → 拿 token

每个 accKey 一次性，登录失败会自动 begin() 重拉。
"""

from __future__ import annotations

import io
import logging
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from urllib.parse import parse_qs, urlparse

import requests
from PIL import Image, ImageTk

from sso.sso_common import (
    APPID,
    AUTH_URL_TEMPLATE,
    CAPTCHA_URL_TEMPLATE,
    DATA_FILE,
    DEFAULT_REDIRECT,
    LOGIN_URL,
    exchange_code_for_token,
    install_request_logging,
    load_data,
    rsa_encrypt,
    save_data,
    setup_logging,
)

from core.account_store import ensure_account

log = logging.getLogger("sso.login")


# ---------- SSO 客户端 ----------
class LoginError(Exception):
    """SSO 登录过程中的异常基类。"""
    pass


class SSOClient:
    """SSO OAuth2 登录客户端。

    封装了完整的 SSO 登录流程：
    1. begin() — 获取 accKey + 验证码图片
    2. fetch_captcha() — 刷新验证码
    3. login() — RSA 加密密码，提交登录，换 token
    """

    def __init__(self) -> None:
        """初始化 SSO 客户端，设置 User-Agent 和请求日志钩子。"""
        self.session = requests.Session()
        # 走真实 UA，避免被风控
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
                )
            }
        )
        # 永不走系统代理（避免 v2 还开着代理时被自己代理转发）
        self.session.trust_env = False
        install_request_logging(self.session)
        self.acc_key: str | None = None
        self.portal_ticket: str | None = None

    def begin(self) -> bytes:
        """开始一次新会话：拿新 accKey，下载验证码图，返回 PNG 字节。"""
        log.info("begin() 开始")
        t0 = time.time()
        auth_url = AUTH_URL_TEMPLATE.format(
            appid=APPID, redirect_uri=DEFAULT_REDIRECT
        )
        r = self.session.get(auth_url, allow_redirects=False, timeout=15)
        loc = r.headers.get("Location") or ""
        qs = parse_qs(urlparse(loc).query)
        acc_key = (qs.get("accKey") or [""])[0]
        if not acc_key:
            log.error("begin: auth2orize 未给 accKey, status=%s loc=%r", r.status_code, loc)
            raise LoginError(
                f"auth2orize 未返回 accKey；status={r.status_code} loc={loc!r}"
            )
        self.acc_key = acc_key
        log.info("begin: accKey=%s (%dms)", acc_key, int((time.time() - t0) * 1000))
        img = self.fetch_captcha()
        return img

    def fetch_captcha(self) -> bytes:
        """刷新验证码图片（基于已存在的 accKey）。

        Returns:
            验证码图片的 PNG 字节数据。

        Raises:
            LoginError: accKey 未初始化时抛出。
        """
        if not self.acc_key:
            raise LoginError("accKey 未初始化，请先调用 begin()")
        url = CAPTCHA_URL_TEMPLATE.format(checkId=self.acc_key)
        r = self.session.get(url, timeout=15)
        r.raise_for_status()
        log.info("fetch_captcha: %dB jpeg (accKey=%s)", len(r.content), self.acc_key)
        return r.content

    def login(self, ucode: str, password: str, rcode: str) -> dict:
        """执行登录并换出 token + 提取 portal_ticket。

        返回 dict，包含 access_token 接口的原始 JSON 再加上 portal_ticket 字段。
        """
        if not self.acc_key:
            raise LoginError("未初始化 accKey")
        log.info(
            "login: ucode=%s accKey=%s rcode=%s pwd_len=%d",
            ucode, self.acc_key, rcode, len(password),
        )
        t0 = time.time()
        body = {
            "ucode": ucode,
            "upwd": rsa_encrypt(password),
            "accKey": self.acc_key,
            "checkId": self.acc_key,
            "rcode": rcode,
        }
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "http://szxy.cqtbi.edu.cn",
            "Referer": f"http://szxy.cqtbi.edu.cn/oauth2/Login.html?accKey={self.acc_key}",
        }
        r = self.session.post(LOGIN_URL, data=body, headers=headers, timeout=15)
        try:
            result = r.json()
        except ValueError:
            log.error("login: 响应非 JSON status=%s body=%r", r.status_code, r.text[:200])
            raise LoginError(f"登录响应不是 JSON: {r.status_code} {r.text[:200]}")
        if not result.get("success"):
            log.warning(
                "login 失败 (%dms): msg=%r raw=%s",
                int((time.time() - t0) * 1000),
                result.get("msg"),
                {k: v for k, v in result.items() if k != "ticket"},
            )
            raise LoginError(result.get("msg") or f"登录失败: {result}")
        # 提取 portal_ticket
        self.portal_ticket = result.get("ticket") or ""
        redirect_url = result.get("redirect_url") or ""
        code = (parse_qs(urlparse(redirect_url).query).get("code") or [""])[0]
        if not code:
            log.error("login 成功但没 code: redirect_url=%r", redirect_url)
            raise LoginError(f"登录响应里没有 code，redirect_url={redirect_url!r}")
        log.info(
            "login 成功 (%dms)：拿到 code + ticket=%s…，准备换 token",
            int((time.time() - t0) * 1000),
            self.portal_ticket[:8] if self.portal_ticket else "无",
        )
        token_result = exchange_code_for_token(code)
        token_result["portal_ticket"] = self.portal_ticket
        return token_result


# ---------- GUI ----------
class App(tk.Tk):
    """SSO 自动登录 GUI 应用。"""

    def __init__(self) -> None:
        super().__init__()
        self.title("SSO 自动登录 (v3)")
        self.geometry("520x680")
        self.resizable(False, False)

        self.data = load_data()
        self.client = SSOClient()
        self.captcha_image: ImageTk.PhotoImage | None = None
        self._busy = False

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # 启动时自动 begin() 拉首张验证码
        self.after(100, self._renew_session)

    def _build_ui(self) -> None:
        """构建图形界面。"""
        pad = {"padx": 8, "pady": 4}

        acc_frame = ttk.LabelFrame(self, text="账号")
        acc_frame.pack(fill="x", **pad)

        row1 = ttk.Frame(acc_frame)
        row1.pack(fill="x", padx=6, pady=4)
        ttk.Label(row1, text="已存账号:").pack(side="left")
        self.account_var = tk.StringVar()
        self.account_combo = ttk.Combobox(
            row1, textvariable=self.account_var, state="readonly", width=18
        )
        self.account_combo.pack(side="left", padx=4)
        self.account_combo.bind("<<ComboboxSelected>>", self._on_pick_account)
        ttk.Button(row1, text="删除", command=self._delete_account).pack(side="left", padx=2)

        row2 = ttk.Frame(acc_frame)
        row2.pack(fill="x", padx=6, pady=4)
        ttk.Label(row2, text="学号:").pack(side="left")
        self.ucode_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.ucode_var, width=14).pack(side="left", padx=4)
        ttk.Label(row2, text="密码:").pack(side="left")
        self.pwd_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.pwd_var, width=18, show="*").pack(side="left", padx=4)

        self.remember_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(acc_frame, text="记住此账号", variable=self.remember_var).pack(anchor="w", padx=6, pady=2)

        cap_frame = ttk.LabelFrame(self, text="验证码")
        cap_frame.pack(fill="x", **pad)
        self.captcha_label = ttk.Label(cap_frame, text="(加载中…)", anchor="center")
        self.captcha_label.pack(pady=6)
        self.captcha_label.bind("<Button-1>", lambda e: self._refresh_captcha())

        crow = ttk.Frame(cap_frame)
        crow.pack(pady=2)
        ttk.Label(crow, text="填入验证码:").pack(side="left")
        self.rcode_var = tk.StringVar()
        ttk.Entry(crow, textvariable=self.rcode_var, width=8).pack(side="left", padx=4)
        ttk.Button(crow, text="刷新", command=self._refresh_captcha).pack(side="left", padx=2)
        ttk.Label(cap_frame, text="（点击图片也可刷新）", foreground="#888").pack()

        self.go_btn = ttk.Button(self, text="获取 Access Token", command=self._do_login)
        self.go_btn.pack(fill="x", **pad)

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self, textvariable=self.status_var, foreground="#0a0").pack(anchor="w", padx=10)

        tok_frame = ttk.LabelFrame(self, text="登录结果")
        tok_frame.pack(fill="both", expand=True, **pad)
        self.token_text = tk.Text(tok_frame, height=10, wrap="word")
        self.token_text.pack(fill="both", expand=True, padx=6, pady=6)

        btns = ttk.Frame(self)
        btns.pack(fill="x", **pad)
        ttk.Button(btns, text="复制 access_token", command=self._copy_token).pack(side="left", padx=2)
        ttk.Button(btns, text="复制 portal_ticket", command=self._copy_ticket).pack(side="left", padx=2)
        ttk.Button(btns, text="保存到 JSON", command=self._save_now).pack(side="left", padx=2)

        self._refresh_account_combo()

    # ----- 账号列表 -----
    def _refresh_account_combo(self) -> None:
        """刷新账号下拉列表。"""
        ids = [a["student_id"] for a in self.data["accounts"]]
        self.account_combo["values"] = ids

    def _on_pick_account(self, _e=None) -> None:
        """选中账号时自动填充学号和密码。"""
        sid = self.account_var.get()
        for a in self.data["accounts"]:
            if a["student_id"] == sid:
                self.ucode_var.set(sid)
                self.pwd_var.set(a.get("password", ""))
                tok = a.get("access_token")
                if tok:
                    self._show_token(
                        {
                            "access_token": tok,
                            "portal_ticket": a.get("portal_ticket", ""),
                            "expires_at": a.get("expires_at"),
                            "last_login": a.get("last_login"),
                            "_cached": True,
                        }
                    )
                return

    def _delete_account(self) -> None:
        """删除当前选中的账号。"""
        sid = self.account_var.get()
        if not sid:
            return
        self.data["accounts"] = [a for a in self.data["accounts"] if a["student_id"] != sid]
        save_data(self.data)
        self.account_var.set("")
        self._refresh_account_combo()

    # ----- 验证码 -----
    def _renew_session(self) -> None:
        """后台 begin()：拿新 accKey + 新验证码图。"""
        self._set_busy(True, "拉取验证码…")
        threading.Thread(target=self._renew_session_bg, daemon=True).start()

    def _renew_session_bg(self) -> None:
        """后台线程：获取新 accKey + 验证码。"""
        try:
            img_bytes = self.client.begin()
        except Exception as e:  # noqa: BLE001
            log.exception("renew_session 失败")
            self.after(0, lambda: self._on_captcha_error(str(e)))
            return
        self.after(0, lambda: self._render_captcha(img_bytes))

    def _refresh_captcha(self) -> None:
        if not self.client.acc_key:
            self._renew_session()
            return
        self._set_busy(True, "刷新验证码…")
        threading.Thread(target=self._refresh_captcha_bg, daemon=True).start()

    def _refresh_captcha_bg(self) -> None:
        try:
            img_bytes = self.client.fetch_captcha()
        except Exception as e:  # noqa: BLE001
            log.exception("refresh_captcha 失败")
            self.after(0, lambda: self._on_captcha_error(str(e)))
            return
        self.after(0, lambda: self._render_captcha(img_bytes))

    def _render_captcha(self, img_bytes: bytes) -> None:
        try:
            img = Image.open(io.BytesIO(img_bytes))
            # 放大 1.5x 看得清
            w, h = img.size
            img = img.resize((int(w * 1.5), int(h * 1.5)), Image.LANCZOS)
            self.captcha_image = ImageTk.PhotoImage(img)
            self.captcha_label.configure(image=self.captcha_image, text="")
        except Exception as e:  # noqa: BLE001
            self.captcha_label.configure(image="", text=f"图片解析失败: {e}")
        self.rcode_var.set("")
        self._set_busy(False, f"验证码就绪 (accKey={self.client.acc_key})")

    def _on_captcha_error(self, msg: str) -> None:
        self.captcha_label.configure(image="", text=f"加载失败: {msg}")
        self._set_busy(False, f"验证码失败: {msg}")

    # ----- 登录 -----
    def _do_login(self) -> None:
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
        self._set_busy(True, "登录中…")
        threading.Thread(
            target=self._do_login_bg, args=(ucode, pwd, rcode), daemon=True
        ).start()

    def _do_login_bg(self, ucode: str, pwd: str, rcode: str) -> None:
        try:
            result = self.client.login(ucode, pwd, rcode)
        except LoginError as e:
            self.after(0, lambda: self._on_login_failed(str(e)))
            return
        except Exception as e:  # noqa: BLE001
            log.exception("登录过程异常")
            self.after(0, lambda: self._on_login_failed(f"异常: {e}"))
            return
        self.after(0, lambda: self._on_login_ok(ucode, pwd, result))

    def _on_login_failed(self, msg: str) -> None:
        messagebox.showerror("登录失败", msg)
        self._set_busy(False, f"失败: {msg}")
        # 失败后 accKey 大概率已废，自动拉新
        self._renew_session()

    def _on_login_ok(self, ucode: str, pwd: str, result: dict) -> None:
        token = result.get("access_token", "")
        portal_ticket = result.get("portal_ticket", "")
        expires_in = int(result.get("expires_in") or 0)
        meta = {
            "access_token": token,
            "portal_ticket": portal_ticket,
            "expires_in": expires_in,
            "expires_at": int(datetime.now().timestamp()) + expires_in if expires_in else 0,
            "last_login": datetime.now().isoformat(timespec="seconds"),
            "raw": result,
        }
        self._show_token(meta)
        if self.remember_var.get():
            ensure_account(
                qq=0,
                student_id=ucode,
                password=pwd,
                access_token=meta["access_token"],
                portal_ticket=meta.get("portal_ticket", ""),
                expires_at=meta["expires_at"],
            )
        self._set_busy(False, "已拿到 token ✓" + (f" (ticket={portal_ticket[:8]}…)" if portal_ticket else ""))
        # 用过一次的 accKey 不能复用，预备下次
        self._renew_session()

    # ----- token 面板 -----
    def _show_token(self, meta: dict) -> None:
        import json
        self.token_text.delete("1.0", "end")
        self.token_text.insert(
            "1.0", json.dumps(meta, ensure_ascii=False, indent=2)
        )

    def _copy_token(self) -> None:
        text = self.token_text.get("1.0", "end").strip()
        import json
        try:
            obj = json.loads(text)
            token = obj.get("access_token", "")
        except json.JSONDecodeError:
            token = text
        if not token:
            messagebox.showwarning("无 token", "还没拿到 access_token")
            return
        self.clipboard_clear()
        self.clipboard_append(token)
        self._set_busy(False, "access_token 已复制")

    def _copy_ticket(self) -> None:
        text = self.token_text.get("1.0", "end").strip()
        import json
        try:
            obj = json.loads(text)
            ticket = obj.get("portal_ticket", "")
        except json.JSONDecodeError:
            ticket = ""
        if not ticket:
            messagebox.showwarning("无 ticket", "还没拿到 portal_ticket")
            return
        self.clipboard_clear()
        self.clipboard_append(ticket)
        self._set_busy(False, "portal_ticket 已复制")

    def _save_now(self) -> None:
        save_data(self.data)
        messagebox.showinfo("已保存", f"已写入 {DATA_FILE}")

    # ----- 工具 -----
    def _set_busy(self, busy: bool, status: str = "") -> None:
        self._busy = busy
        if status:
            self.status_var.set(status)
        try:
            self.go_btn.configure(state="disabled" if busy else "normal")
        except tk.TclError:
            pass

    def _on_close(self) -> None:
        save_data(self.data)
        self.destroy()


def main() -> None:
    setup_logging()
    log.info("sso_login 启动")
    App().mainloop()
    log.info("sso_login 退出")


if __name__ == "__main__":
    main()
