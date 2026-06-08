"""OneBot v11 消息事件中提取图片 URL 并下载字节。

OneBot 事件的 message 字段是分段数组：
  [{"type":"image", "data":{"url": "https://...", "file":"abc.jpg", ...}}, ...]

NapCat 通常会把 CDN 直链放在 data.url；data.file 则是文件名/缓存键。
"""

from __future__ import annotations

import logging
from typing import Iterable

import requests

log = logging.getLogger(__name__)


def extract_image_urls(event: dict) -> list[str]:
    """从事件里抽出所有图片直链 URL。无图返回 []。"""
    out: list[str] = []
    segments = event.get("message")
    if not isinstance(segments, list):
        return out
    for seg in segments:
        if not isinstance(seg, dict) or seg.get("type") != "image":
            continue
        data = seg.get("data") or {}
        url = data.get("url") or data.get("file")
        if url and isinstance(url, str) and url.startswith(("http://", "https://")):
            out.append(url)
    return out


def download_image(url: str, *, timeout: int = 15) -> bytes | None:
    """下载图片字节。失败返回 None。"""
    try:
        r = requests.get(url, timeout=timeout, verify=False)
    except requests.RequestException as e:
        log.warning("图片下载失败 %s: %s", url[:80], e)
        return None
    if r.status_code != 200:
        log.warning("图片下载状态码异常 %s: %s", url[:80], r.status_code)
        return None
    return r.content


def first_image_bytes(event: dict) -> bytes | None:
    """事件里第一张图片的字节内容。无图或下载失败返回 None。"""
    urls = extract_image_urls(event)
    for url in urls:
        b = download_image(url)
        if b:
            return b
    return None
