"""HTTP 短重试工具测试（#117）"""

import json
import os
import socket
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.net import fetch_bytes, fetch_json


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_bytes_ok():
    assert fetch_bytes("http://x", opener=lambda req, timeout: _FakeResp(b"abc")) == b"abc"


def test_fetch_bytes_retries_then_succeeds():
    """DNS/连接抖动：前两次失败、第三次成功（退避置 0 加速）"""
    calls = {"n": 0}

    def flaky(req, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise socket.gaierror(11001, "getaddrinfo failed")
        return _FakeResp(b"ok")

    assert fetch_bytes("http://x", attempts=3, backoff=0, opener=flaky) == b"ok"
    assert calls["n"] == 3


def test_fetch_bytes_raises_after_all_attempts():
    calls = {"n": 0}

    def always_fail(req, timeout):
        calls["n"] += 1
        raise ConnectionResetError("reset")

    with pytest.raises(OSError):
        fetch_bytes("http://x", attempts=2, backoff=0, opener=always_fail)
    assert calls["n"] == 2


def test_http_error_not_retried():
    """HTTPError 是真实响应，不重试"""
    calls = {"n": 0}

    def http_500(req, timeout):
        calls["n"] += 1
        raise urllib.error.HTTPError("http://x", 500, "boom", {}, None)

    with pytest.raises(urllib.error.HTTPError):
        fetch_bytes("http://x", attempts=3, backoff=0, opener=http_500)
    assert calls["n"] == 1


def test_fetch_json_parses():
    body = json.dumps({"data": {"diff": [1]}}).encode()
    out = fetch_json("http://x", opener=lambda req, timeout: _FakeResp(body))
    assert out["data"]["diff"] == [1]
