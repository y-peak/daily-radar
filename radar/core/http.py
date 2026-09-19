"""统一的 HTTP 客户端。

三个必须自己扛下来的现实（都是实测踩出来的）：

    1. **东财 push2 有反爬** —— 连续请求几次就断连，且长时间不恢复。
       所以要有最小请求间隔 + 指数退避重试 + 主源挂了能切备源。
    2. **新浪 / 腾讯返回 GBK**，不是 UTF-8。按 UTF-8 硬解就是一屏乱码。
    3. **链路会抖** —— 同一个接口这次超时、下次就好，重试能救回来。

只用标准库，不引 requests：少一个依赖，行为也更可控（重试/限速都在自己手里）。
"""

from __future__ import annotations

import gzip
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request


class HttpError(RuntimeError):
    """重试耗尽后仍失败。"""


def sniff_encoding(headers, body: bytes) -> str:
    """先看响应头，再按内容猜。新浪/腾讯都是 GBK 且不声明 charset。"""
    ctype = (headers.get("Content-Type") or "").lower()
    if "charset=" in ctype:
        charset = ctype.split("charset=", 1)[1].split(";")[0].strip()
        if charset:
            return charset
    try:
        body.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "gbk"


class HttpClient:
    """带限速与退避重试的 GET 客户端。"""

    def __init__(self, cfg: dict | None = None, log=None):
        cfg = cfg or {}
        self.ua = cfg.get("user_agent", "Mozilla/5.0")
        self.timeout = cfg.get("timeout", 15)
        self.min_interval = float(cfg.get("min_interval", 0.8))
        self.retries = int(cfg.get("retries", 3))
        self.backoff = float(cfg.get("backoff", 1.6))
        self._last_call = 0.0
        self._log = log or (lambda *a, **k: None)

    def _throttle(self) -> None:
        gap = time.time() - self._last_call
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last_call = time.time()

    def get(self, url: str, headers: dict | None = None, referer: str | None = None,
            retries: int | None = None, encoding: str | None = None) -> str:
        hdr = {
            "User-Agent": self.ua,
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
            "Connection": "close",
        }
        if referer:
            hdr["Referer"] = referer
        hdr.update(headers or {})

        tries = self.retries if retries is None else max(1, retries)
        last: Exception | None = None
        for attempt in range(tries):
            self._throttle()
            try:
                req = urllib.request.Request(url, headers=hdr)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read()
                    if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                        body = gzip.decompress(body)
                    enc = encoding or sniff_encoding(resp.headers, body)
                    return body.decode(enc, "replace")
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < tries - 1:
                    wait = self.backoff ** attempt + random.uniform(0, 0.4)
                    self._log(f"      重试 {attempt + 1}/{tries - 1}"
                              f"（{type(exc).__name__}），{wait:.1f}s 后…")
                    time.sleep(wait)
        raise HttpError(f"GET 失败（试了 {tries} 次）：{url}\n  {type(last).__name__}: {last}")

    def get_json(self, url: str, **kw):
        text = self.get(url, **kw)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise HttpError(f"返回不是合法 JSON：{url}\n  {exc}\n  前 120 字：{text[:120]!r}")

    def get_first(self, sources: list, **kw):
        """多源容错：sources 为 [url] 或 [(url, headers)]，依次尝试，返回第一个成功的。

        用于"主源被限流就切备源"的场景。
        """
        errors: list[str] = []
        for item in sources:
            url, hdr = item if isinstance(item, (tuple, list)) else (item, None)
            try:
                return self.get(url, headers=hdr, **kw)
            except HttpError as exc:
                errors.append(f"{urllib.parse.urlparse(url).netloc}: {exc}")
        raise HttpError("所有候选数据源都失败了：\n  " + "\n  ".join(errors))

    def get_first_json(self, sources: list, **kw):
        text = self.get_first(sources, **kw)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise HttpError(f"返回不是合法 JSON：{exc}\n  前 120 字：{text[:120]!r}")
