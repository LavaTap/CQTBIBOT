"""账号存储工具 —— 读写 accounts.json，支持按 QQ 号关联查询。

扩展 accounts.json 的 accounts 数组，每个账号增加 qq 字段：
  {
    "qq": 3602653998,
    "student_id": "2403740",
    "password": "...",
    "access_token": "...",
    "expires_at": ...,
    "portal_ticket": "...",
    "last_login": "..."
  }
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sso.sso_common import DATA_FILE, load_data, save_data

log = logging.getLogger("account_store")


def find_account_by_qq(qq: int) -> dict | None:
    """按 QQ 号查找账号。

    Args:
        qq: QQ 号。

    Returns:
        账号字典（包含 student_id, password, access_token 等字段），
        未找到时返回 None。
    """
    data = load_data()
    for acc in data.get("accounts", []):
        if acc.get("qq") == qq:
            return acc
    return None


def find_account_by_student_id(sid: str) -> dict | None:
    """按学号查找账号。

    Args:
        sid: 学号。

    Returns:
        账号字典，未找到时返回 None。
    """
    data = load_data()
    for acc in data.get("accounts", []):
        if acc.get("student_id") == sid:
            return acc
    return None


def save_account(qq: int, account_data: dict) -> None:
    """保存或更新账号（按 QQ 匹配）。

    若 accounts 中已存在相同 QQ 或学号的记录则更新，否则新增。

    Args:
        qq: QQ 号。
        account_data: 账号数据字典（student_id, password, access_token 等）。
    """
    data = load_data()
    accounts: list[dict] = data.setdefault("accounts", [])
    account_data["qq"] = qq
    for i, acc in enumerate(accounts):
        if acc.get("qq") == qq or acc.get("student_id") == account_data.get("student_id"):
            accounts[i] = account_data
            break
    else:
        accounts.append(account_data)
    save_data(data)
    log.info("账号已保存: qq=%s student_id=%s", qq, account_data.get("student_id", ""))


def update_account_field(qq: int, key: str, value: Any) -> None:
    """更新指定 QQ 账号的单个字段。

    Args:
        qq: QQ 号。
        key: 字段名（如 "access_token", "portal_ticket"）。
        value: 字段值。
    """
    data = load_data()
    for acc in data.get("accounts", []):
        if acc.get("qq") == qq:
            acc[key] = value
            save_data(data)
            log.info("账号字段已更新: qq=%s %s=%s", qq, key, value)
            return
    log.warning("未找到 qq=%s 的账号，无法更新字段 %s", qq, key)


def ensure_account(
    qq: int,
    student_id: str,
    password: str,
    access_token: str = "",
    portal_ticket: str = "",
    expires_at: int = 0,
    ssid: str = "",
) -> None:
    """确保账号存在，不存在则创建，存在则更新关键字段。

    按 QQ 或学号匹配，已存在的字段不覆盖（除非提供新值）。

    Args:
        qq: QQ 号。
        student_id: 学号。
        password: 密码。
        access_token: OAuth2 令牌。
        portal_ticket: 门户票据。
        expires_at: token 过期时间戳。
        ssid: 二课会话 ID。
    """
    data = load_data()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    entry = {
        "qq": qq,
        "student_id": student_id,
        "password": password,
        "access_token": access_token,
        "expires_at": expires_at,
        "portal_ticket": portal_ticket,
        "ssid": ssid,
        "last_login": now_iso,
    }
    accounts = data.setdefault("accounts", [])
    for i, acc in enumerate(accounts):
        if acc.get("qq") == qq or acc.get("student_id") == student_id:
            entry["access_token"] = access_token or acc.get("access_token", "")
            entry["portal_ticket"] = portal_ticket or acc.get("portal_ticket", "")
            entry["ssid"] = ssid or acc.get("ssid", "")
            if not password:
                entry["password"] = acc.get("password", "")
            accounts[i] = entry
            break
    else:
        accounts.append(entry)
    save_data(data)
    log.info("账号已确保更新: qq=%s student_id=%s", qq, student_id)
