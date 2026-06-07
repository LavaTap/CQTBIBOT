"""JWGL 教务系统 HTTP 客户端。

完整流程：
  1) SSO 登录 → auth2Login 返回 ticket (= PORTAL_TICKET)
  2) 门户登录 → 设置 PORTAL_TICKET cookie
  3) JWGL 桥接 → 获取 bzb_jsxsd cookie
  4) 获取课表 → HTML → 解析为 Schedule
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
import urllib3

from sso.sso_common import (
    APPID,
    AUTH_URL_TEMPLATE,
    CAPTCHA_URL_TEMPLATE,
    DEFAULT_REDIRECT,
    LOGIN_URL,
    exchange_code_for_token,
    install_request_logging,
    rsa_encrypt,
)
from schedule.models import ScheduleError
from schedule.parser import ScheduleParser

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("schedule.jwgl")

JWGL_BASE = "https://jwgl.cqtbi.edu.cn"
JWGL_BRIDGE_URL = f"{JWGL_BASE}/Logon.do?method=toCqgszy&PORTAL_TICKET={{}}"
JWGL_SCHEDULE_URL = "https://jwgl.cqtbi.edu.cn:81/jsxsd/xskb/xskb_list.do"
JWGL_GRADE_QUERY_URL = "https://jwgl.cqtbi.edu.cn:81/jsxsd/kscj/cjcx_query"
JWGL_GRADE_LIST_URL = "https://jwgl.cqtbi.edu.cn:81/jsxsd/kscj/cjcx_list"
PORTAL_USER_DATA_URL = "http://szxy.cqtbi.edu.cn/oauth2/v1/getUserDataByTicket"


class JWGLClient:
    """JWGL 教务系统 HTTP 客户端。

    管理 requests.Session，实现 SSO 登录、验证码获取、课表抓取与解析。
    """

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
            ),
        })
        install_request_logging(self.session)
        self.portal_ticket: str | None = None
        self.access_token: str | None = None
        self.acc_key: str | None = None
        self._bridged = False

    # ---- SSO 登录 ----

    def begin_sso(self) -> bytes:
        """开始 SSO 会话：拿 accKey + 下载验证码图片。"""
        log.info("begin_sso: 开始")
        auth_url = AUTH_URL_TEMPLATE.format(appid=APPID, redirect_uri=DEFAULT_REDIRECT)
        r = self.session.get(auth_url, allow_redirects=False, timeout=15)
        loc = r.headers.get("Location") or ""
        qs = parse_qs(urlparse(loc).query)
        acc_key = (qs.get("accKey") or [""])[0]
        if not acc_key:
            raise ScheduleError(f"auth2orize 未返回 accKey: status={r.status_code} loc={loc!r}")
        self.acc_key = acc_key
        log.info("begin_sso: accKey=%s", acc_key)
        return self._fetch_captcha()

    def _fetch_captcha(self) -> bytes:
        if not self.acc_key:
            raise ScheduleError("accKey 未初始化")
        url = CAPTCHA_URL_TEMPLATE.format(checkId=self.acc_key)
        r = self.session.get(url, timeout=15)
        r.raise_for_status()
        return r.content

    def refresh_captcha(self) -> bytes:
        return self._fetch_captcha()

    def sso_login(self, ucode: str, password: str, rcode: str) -> dict:
        """SSO 登录，返回 auth2Login 的原始 JSON（含 ticket + redirect_url）。"""
        if not self.acc_key:
            raise ScheduleError("未初始化 accKey")
        log.info("sso_login: ucode=%s", ucode)
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
            raise ScheduleError(f"登录响应非 JSON: {r.status_code} {r.text[:200]}")
        if not result.get("success"):
            raise ScheduleError(result.get("msg") or f"登录失败: {result}")
        self.portal_ticket = result.get("ticket")
        redirect_url = result.get("redirect_url") or ""
        code = (parse_qs(urlparse(redirect_url).query).get("code") or [""])[0]
        if code:
            token_result = exchange_code_for_token(code)
            self.access_token = token_result.get("access_token")
        log.info("sso_login 成功: ticket=%s… access_token=%s…",
                 (self.portal_ticket or "")[:8], (self.access_token or "")[:8])
        return result

    # ---- 门户登录 ----

    def portal_login(self, ticket: str | None = None) -> str:
        """用 SSO ticket 走门户登录，设置 PORTAL_TICKET cookie。"""
        t = ticket or self.portal_ticket
        if not t:
            raise ScheduleError("没有 ticket，无法登录门户")
        log.info("portal_login: ticket=%s…", t[:8])
        body = {
            "id": "",
            "CreateTime": str(int(time.time() * 1000)),
            "TrackId": "",
            "ticket": t,
            "PORTAL_TICKET": t,
        }
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "http://szxy.cqtbi.edu.cn",
            "Referer": "http://szxy.cqtbi.edu.cn/cqdddt/services.html",
        }
        r = self.session.post(
            "http://szxy.cqtbi.edu.cn/cqdddt/dtLog!log.action",
            data=body, headers=headers, timeout=15,
        )
        log.info("portal_login: status=%s cookies=%s", r.status_code,
                 {k: v[:8] + "…" for k, v in self.session.cookies.get_dict().items()})
        pt = self.session.cookies.get("PORTAL_TICKET")
        if pt:
            self.portal_ticket = pt
            log.info("portal_login: PORTAL_TICKET cookie=%s…", pt[:8])
        else:
            self.portal_ticket = t
            log.info("portal_login: 无 cookie，用 ticket=%s…", t[:8])
        return self.portal_ticket

    # ---- JWGL 桥接 ----

    def bridge(self) -> None:
        """用 PORTAL_TICKET 桥接进 JWGL，获取 bzb_jsxsd cookie。"""
        if not self.portal_ticket:
            raise ScheduleError("没有 PORTAL_TICKET，无法桥接 JWGL")
        log.info("bridge: PORTAL_TICKET=%s…", self.portal_ticket[:8])
        self.session.get(f"{JWGL_BASE}/", timeout=15, verify=False)
        url = JWGL_BRIDGE_URL.format(self.portal_ticket)
        r = self.session.get(url, timeout=15, allow_redirects=True, verify=False)
        bzb_jsxsd = self.session.cookies.get("bzb_jsxsd")
        if not bzb_jsxsd:
            log.warning("bridge: 未拿到 bzb_jsxsd, cookies=%s",
                        {k: v[:8] + "…" for k, v in self.session.cookies.get_dict().items()})
        else:
            log.info("bridge: bzb_jsxsd=%s…", bzb_jsxsd[:8])
        self._bridged = True

    # ---- 课表获取 ----

    def get_schedule_html(self, semester: str = "", week: str = "") -> str:
        """获取课表 HTML。semester 如 '2025-2026-2'，week 如 '5' 或空。"""
        if not self._bridged:
            self.bridge()
        data = {}
        if semester:
            data["xnxq01id"] = semester
        if week:
            data["zc"] = week
        log.info("get_schedule_html: semester=%s week=%s", semester, week)
        r = self.session.post(
            JWGL_SCHEDULE_URL, data=data, timeout=15, verify=False,
        )
        r.raise_for_status()
        return r.text

    def get_schedule(self, semester: str = "", week: str = "",
                     student_id: str = "", student_name: str = "") -> "Schedule":
        """获取并解析课表。"""
        from schedule.models import Schedule
        html = self.get_schedule_html(semester=semester, week=week)
        if not semester:
            semester = self._extract_semester(html)
        return ScheduleParser.parse(html, semester=semester,
                                     student_id=student_id,
                                     student_name=student_name)

    @staticmethod
    def _extract_semester(html: str) -> str:
        """从课表 HTML 提取当前学期。"""
        import re
        m = re.search(r"xnxq01id['\"]?\s*(?:value|:)\s*['\"]?(\d{4}-\d{4}-\d)", html)
        if m:
            return m.group(1)
        m = re.search(r"(\d{4}-\d{4}-\d)", html)
        if m:
            return m.group(1)
        return ""

    # ---- 用户信息 ----

    def get_user_info(self, ticket: str | None = None) -> dict:
        """用 PORTAL_TICKET 获取用户信息。"""
        t = ticket or self.portal_ticket
        if not t:
            raise ScheduleError("没有 ticket")
        body = {"PORTAL_TICKET": t}
        r = self.session.post(PORTAL_USER_DATA_URL, data=body, timeout=15)
        r.raise_for_status()
        return r.json()

    # ---- 成绩查询 ----

    def get_grades_html(self, kksj: str = "", kcxz: str = "", kcsx: str = "",
                        kcmc: str = "") -> str:
        """获取成绩 HTML。先访问查询页获取必要 cookie，再 POST 查询。

        Args:
            kksj: 开课时间（学期筛选），如 '2025-2026-2'，空=全部。
            kcxz: 课程性质筛选，空=全部。
            kcsx: 课程属性筛选，空=全部。
            kcmc: 课程名称模糊搜索，空=全部。
        """
        if not self._bridged:
            self.bridge()
        # 前置：访问查询页获取必要 cookie
        self.session.get(JWGL_GRADE_QUERY_URL, timeout=15, verify=False)
        data = {
            "kksj": kksj,
            "kcxz": kcxz,
            "kcsx": kcsx,
            "kcmc": kcmc,
            "xsfs": "all",
            "sfxsbcxq1": "",
            "mold": "",
        }
        log.info("get_grades_html: kksj=%s kcxz=%s kcsx=%s kcmc=%s",
                 kksj, kcxz, kcsx, kcmc)
        r = self.session.post(
            JWGL_GRADE_LIST_URL, data=data, timeout=15, verify=False,
            headers={
                "Origin": "https://jwgl.cqtbi.edu.cn:81",
                "Referer": "https://jwgl.cqtbi.edu.cn:81/jsxsd/kscj/cjcx_query",
            },
        )
        r.raise_for_status()
        return r.text

    def get_grades(self, kksj: str = "", kcxz: str = "", kcsx: str = "",
                   kcmc: str = "") -> list[dict]:
        """获取并解析成绩列表。

        Returns:
            成绩字典列表，每条含: semester, course_code, course_name,
            course_nature, credit, makeup_semester, total_score, exam_nature, gpa
        """
        html = self.get_grades_html(kksj=kksj, kcxz=kcxz, kcsx=kcsx, kcmc=kcmc)
        return self._parse_grades_html(html)

    @staticmethod
    def _parse_grades_html(html: str) -> list[dict]:
        """从成绩 HTML 表格解析成绩列表。"""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        table = soup.select_one("table.dataList, table#dataList, table")
        if not table:
            return []
        results: list[dict] = []
        for row in table.select("tr")[1:]:  # 跳过表头
            cells = row.select("td")
            if len(cells) < 8:
                continue
            results.append({
                "semester": cells[0].get_text(strip=True),
                "course_code": cells[1].get_text(strip=True),
                "course_name": cells[2].get_text(strip=True),
                "course_nature": cells[3].get_text(strip=True),
                "credit": cells[4].get_text(strip=True),
                "makeup_semester": cells[5].get_text(strip=True) if len(cells) > 5 else "",
                "total_score": cells[6].get_text(strip=True) if len(cells) > 6 else "",
                "exam_nature": cells[7].get_text(strip=True) if len(cells) > 7 else "",
                "gpa": cells[8].get_text(strip=True) if len(cells) > 8 else "",
            })
        return results

    # ---- 检查 JWGL 会话有效性 ----

    def is_jwgl_valid(self) -> bool:
        """快速检查 JWGL session 是否仍有效。"""
        if not self._bridged:
            return False
        try:
            r = self.session.get(
                JWGL_SCHEDULE_URL, timeout=10, verify=False,
                allow_redirects=False,
            )
            if r.status_code in (301, 302, 303, 307):
                loc = r.headers.get("Location", "")
                if "Logon" in loc or "login" in loc:
                    return False
            return r.status_code == 200
        except Exception:
            return False
