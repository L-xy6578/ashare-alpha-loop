"""
HTTP 请求短重试工具（#117）—— DNS/连接抖动自愈

背景：早盘时段偶发 `getaddrinfo failed` / 连接重置（实测 10-19、10-20、10-22），
守护级重试间隔 10 分钟过粗；在单次请求内做秒级重试，覆盖短时抖动。

约定：
    - 网络类错误（socket.gaierror / ConnectionResetError / TimeoutError，均为 OSError）→ 重试
    - HTTPError（4xx/5xx，已拿到真实响应）→ 不重试，直接抛出
"""

from __future__ import annotations

import json
import time
import urllib.request
from urllib.error import HTTPError

from src.logger import get_logger

log = get_logger(__name__)

DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_bytes(url: str, *, headers: dict | None = None, timeout: int = 12,
                attempts: int = 2, backoff: float = 0.5,
                opener=urllib.request.urlopen) -> bytes:
    """GET 返回原始字节；网络类错误做短重试（退避 backoff, 2·backoff, ...）"""
    last_exc: Exception | None = None
    total = max(1, attempts)
    for i in range(total):
        try:
            req = urllib.request.Request(url, headers=headers or DEFAULT_HEADERS)
            with opener(req, timeout=timeout) as resp:
                return resp.read()
        except HTTPError:
            raise
        except OSError as exc:
            last_exc = exc
            if i + 1 >= total:
                break
            wait = backoff * (i + 1)
            log.warning("请求失败（第%d/%d次）%s: %s；%.1fs 后重试",
                        i + 1, total, url[:90], exc, wait)
            time.sleep(wait)
    raise last_exc if last_exc is not None else RuntimeError("fetch failed")


def fetch_json(url: str, *, headers: dict | None = None, timeout: int = 12,
               attempts: int = 2, backoff: float = 0.5,
               opener=urllib.request.urlopen) -> dict:
    """GET 并解析 JSON（同样带网络短重试）"""
    raw = fetch_bytes(url, headers=headers, timeout=timeout,
                      attempts=attempts, backoff=backoff, opener=opener)
    return json.loads(raw.decode("utf-8", errors="replace"))
