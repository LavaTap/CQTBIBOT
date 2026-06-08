"""二维码解码 + 二课签到 URL 解析。

只暴露两个公开 API：
  - decode_qr_image(bytes) -> str | None  解出二维码内容字符串
  - parse_sign_qr(text) -> dict | None    把 signOnTV.html URL 拆成 (activity_id, channel_id, rand, sign_out)

cv2 是可选依赖。导入时不强求；调用时才报缺包。
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)


def decode_qr_image(image_bytes: bytes) -> str | None:
    """从图片字节解出二维码内容。失败返回 None。"""
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "缺少二维码解码依赖，请在 venv 中 pip install opencv-python-headless numpy"
        ) from e

    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        log.warning("二维码图片解码失败：图片字节无法识别")
        return None

    det = cv2.QRCodeDetector()
    val, points, _ = det.detectAndDecode(img)
    if val:
        return val

    # 兜底：转灰度 + 直方图均衡 再试一次
    try:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        val2, _, _ = det.detectAndDecode(gray)
        if val2:
            return val2
    except Exception as e:
        log.debug("二维码灰度兜底失败: %s", e)
    return None


def parse_sign_qr(text: str) -> dict | None:
    """二维码文本若是 signOnTV.html 链接 → 返回 dict，否则 None。

    返回字段：
        url:         原始 URL
        activity_id: 活动 ID（字符串）
        channel_id:  渠道（int）
        rand:        服务端下发的一次性 token（字符串，照原样回传）
        sign_out:    True=签退，False=签到
    """
    if not text:
        return None
    try:
        parsed = urlparse(text)
    except Exception:
        return None
    if "signOnTV" not in (parsed.path or ""):
        return None

    qs = parse_qs(parsed.query or "")
    aid = (qs.get("activityID") or [""])[0]
    if not aid:
        return None

    ch = (qs.get("channelID") or ["5"])[0]
    rand = (qs.get("rand") or [""])[0]
    is_out = (qs.get("isSignOut") or ["0"])[0]

    try:
        channel_id = int(ch)
    except ValueError:
        channel_id = 5

    return {
        "url": text,
        "activity_id": aid,
        "channel_id": channel_id,
        "rand": rand,
        "sign_out": is_out == "1",
    }
