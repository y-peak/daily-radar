"""GitHub Trending 的数据获取与解析。

四件事，各自独立、互不阻塞：

    fetch_trending   抓榜单页面 —— 必须成功，否则整个模块没意义
    parse_trending   把 HTML 切成结构化条目
    fetch_repo_meta  用 api.github.com 补仓库详情（描述/topics/活跃度）
    fetch_repo_ctx   再补一层"代码长什么样"（README 正文 + 根目录结构）

**为什么要手写 HTTPS 请求**：服务器上 `github.com` 解析到 `20.205.243.x`，
该段被阻断；而老段 `140.82.11x.x` 可用。标准库没有 `curl --resolve` 那种
"指定 IP 但保留 SNI" 的能力，所以手动 socket → wrap_socket(server_hostname)
再手写请求头。TLS 故障是**抖动型**的（同一 IP 时通时不通），所以要多 IP × 多轮。

`api.github.com` 是通的、不用绕 IP，直连即可 —— 但它**有速率限制**：

| 认证方式 | 限额 |
|---|---|
| 不带 token | **60 次/小时** |
| 带 token（`github_token`） | 5000 次/小时 |

所以除了 `fetch_repo_meta`，`fetch_repo_ctx` 也要算额度，见 `fetch_repo_ctx`
里的磁盘缓存 —— 同一份代码只补一次，两次采集（08:00 / 16:30）共用。
"""

from __future__ import annotations

import gzip
import hashlib
import html as _html
import http.client
import json
import re
import socket
import ssl
import time
import urllib.parse
from pathlib import Path

DEFAULT_IPS = [
    "140.82.114.3", "140.82.112.3", "140.82.121.3", "140.82.113.3",
    "140.82.112.4", "140.82.113.4", "140.82.114.4", "140.82.121.4",
]


class SourceError(RuntimeError):
    pass


# ------------------------------------------------------------------ 底层请求
def _request(host: str, path: str, user_agent: str, ip: str | None = None,
             timeout: int = 15, headers: dict | None = None) -> tuple[int, str]:
    """发一个 HTTPS GET。给了 ip 就连 IP 但保留 SNI（绕过被墙的解析结果）。"""
    ctx = ssl.create_default_context()
    target = (ip or host, 443)
    raw = socket.create_connection(target, timeout=timeout)
    try:
        ssock = ctx.wrap_socket(raw, server_hostname=host)
    except Exception:
        raw.close()
        raise

    try:
        ssock.settimeout(timeout)
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}",
            f"User-Agent: {user_agent}",
            "Accept: */*",
            "Accept-Language: en-US,en;q=0.9",
            "Accept-Encoding: gzip, deflate",
            # 必须发 close：否则页面下完连接不关，白等十几秒
            "Connection: close",
        ]
        for key, value in (headers or {}).items():
            lines.append(f"{key}: {value}")
        ssock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))

        resp = http.client.HTTPResponse(ssock, method="GET")
        resp.begin()
        status = resp.status
        body = resp.read()
        if "gzip" in (resp.getheader("Content-Encoding") or "").lower():
            body = gzip.decompress(body)
        return status, body.decode("utf-8", "replace")
    finally:
        try:
            ssock.close()
        except Exception:
            pass


def fetch_trending(fcfg: dict, since: str, log=None) -> dict:
    """多 IP × 多轮 抓 Trending 页面。返回 {html, ip, elapsed, attempt, url}。"""
    host = fcfg.get("host", "github.com")
    base_path = fcfg.get("path", "/trending")
    path = f"{base_path}?since={since}"
    ips = list(fcfg.get("candidate_ips") or DEFAULT_IPS)
    ua = fcfg.get("user_agent", "Mozilla/5.0")
    timeout = int(fcfg.get("timeout", 15))
    attempts = max(1, int(fcfg.get("attempts", 3)))
    deadline = int(fcfg.get("deadline", 120))

    started_all = time.time()
    tried: list[dict] = []

    for attempt in range(attempts):
        # 每轮把起点往后挪一位，避免总从同一个 IP 开始撞墙
        shift = attempt % len(ips)
        for ip in ips[shift:] + ips[:shift]:
            if time.time() - started_all > deadline:
                break
            t0 = time.time()
            try:
                status, page = _request(host, path, ua, ip=ip, timeout=timeout)
                if status != 200:
                    raise SourceError(f"HTTP {status}")
            except Exception as exc:  # noqa: BLE001
                tried.append({"attempt": attempt + 1, "ip": ip, "ok": False,
                              "error": f"{type(exc).__name__}: {exc}"})
                continue
            elapsed = round(time.time() - t0, 2)
            tried.append({"attempt": attempt + 1, "ip": ip, "ok": True, "elapsed": elapsed})
            if log:
                log(f"      Trending 抓取成功：IP {ip} / {elapsed}s（第 {attempt + 1} 轮）")
            return {"html": page, "ip": ip, "elapsed": elapsed,
                    "attempt": attempt + 1, "url": f"https://{host}{path}",
                    "tried": tried}

    detail = "; ".join(f"{t['ip']}:{t.get('error', 'ok')}" for t in tried[-6:])
    raise SourceError(
        f"候选 IP 试了 {attempts} 轮共 {len(tried)} 次全部失败（{int(time.time() - started_all)}s）。"
        f"最近几次：{detail}")


# -------------------------------------------------------------------- 解析
_ROW = re.compile(r'<article class="Box-row".*?</article>', re.S)
_NAME = re.compile(r'<h2[^>]*>.*?href="/([^"/]+)/([^"/]+)"', re.S)
_GROWTH = re.compile(r"([\d,]+)\s*stars?\s*(?:today|this\s+week|this\s+month)")
_TOTAL = re.compile(r'href="/[^"]+/stargazers".*?>\s*([\d,]+)\s*<', re.S)
_LANG = re.compile(r'itemprop="programmingLanguage">\s*([^<]+?)\s*<')
_DESC = re.compile(r'<p class="col-9[^"]*"[^>]*>(.*?)</p>', re.S)


def _strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return _html.unescape(re.sub(r"\s+", " ", text)).strip()


def _to_int(value) -> int | None:
    if not value:
        return None
    try:
        return int(str(value).replace(",", ""))
    except ValueError:
        return None


def parse_trending(page: str) -> list[dict]:
    items: list[dict] = []
    for row in _ROW.findall(page):
        matched = _NAME.search(row)
        if not matched:
            continue
        owner, name = matched.group(1), matched.group(2)
        lang = _LANG.search(row)
        desc = _DESC.search(row)
        growth = _GROWTH.search(row)
        total = _TOTAL.search(row)
        items.append({
            "rank": len(items) + 1,
            "owner": owner,
            "name": name,
            "full_name": f"{owner}/{name}",
            "url": f"https://github.com/{owner}/{name}",
            "language": lang.group(1) if lang else "",
            "stars_growth": _to_int(growth.group(1)) if growth else None,
            "stars_total": _to_int(total.group(1)) if total else None,
            "description": _strip_tags(desc.group(1)) if desc else "",
        })
    return items


# ---------------------------------------------------------------- 详情补全
def _api(path: str, ua: str, token: str = "", timeout: int = 12,
         accept: str = "application/vnd.github+json") -> str:
    """打一次 api.github.com。**失败抛 SourceError**，由调用方决定怎么降级。"""
    headers = {"Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, body = _request("api.github.com", path, ua, timeout=timeout, headers=headers)
    if status == 403 or status == 429:
        # 未认证 60 次/小时用完就是这个。提示里必须带上"怎么修"，否则看到
        # 403 只会以为是仓库不存在。
        raise SourceError(f"HTTP {status}（API 限流：未认证 60 次/小时，"
                          f"配 github_token 可提到 5000）")
    if status != 200:
        raise SourceError(f"HTTP {status}")
    return body


def fetch_repo_meta(full_name: str, ua: str, token: str = "", timeout: int = 12) -> dict:
    """api.github.com 直连（这个域名没被墙）。失败返回空字典，不中断主流程。"""
    try:
        data = json.loads(_api(f"/repos/{full_name}", ua, token, timeout))
    except Exception as exc:  # noqa: BLE001
        return {"_error": f"{type(exc).__name__}: {exc}"}

    return {
        "description": (data.get("description") or "").strip(),
        "topics": data.get("topics") or [],
        "language": data.get("language") or "",
        "stars": data.get("stargazers_count"),
        "forks": data.get("forks_count"),
        "open_issues": data.get("open_issues_count"),
        "license": ((data.get("license") or {}).get("spdx_id") or "").replace("NOASSERTION", ""),
        "homepage": (data.get("homepage") or "").strip(),
        "created_at": data.get("created_at"),
        "pushed_at": data.get("pushed_at"),
        "size_kb": data.get("size"),
        "archived": data.get("archived"),
    }


# ------------------------------------------------------- "代码长什么样"上下文
# 为什么要这层：英文 description 常常只有半句话（"A fast ORM for Python"），
# 机翻出来还是半句话，读者仍然不知道它解决什么问题。给它 README 正文 +
# 根目录结构，才可能写出"这玩意是干嘛的、怎么上手"。
#
# 为什么不是 clone：日榜 10 个仓库平均 95MB，一天下来近 1GB（详见 README 的
# 方案对比）。而 README + 根目录足以支撑一段像样的总结 —— 性价比差两个数量级。

_README_LIMIT = 6000      # README 截断长度（约 2000 汉字，够模型抓重点）
_MAX_ENTRIES = 40         # 根目录最多列几项，避免巨型 monorepo 刷屏


def fetch_readme(full_name: str, ua: str, token: str = "", timeout: int = 12,
                 limit: int = _README_LIMIT) -> str:
    """取 README 正文（原始 Markdown）。没有 README 返回空串。"""
    try:
        text = _api(f"/repos/{full_name}/readme", ua, token, timeout,
                    accept="application/vnd.github.raw")
    except SourceError as exc:
        if "404" in str(exc):
            return ""          # 仓库没有 README，不是错误
        raise
    text = text.strip()
    return text[:limit]


def fetch_tree(full_name: str, ua: str, token: str = "", timeout: int = 12) -> list[dict]:
    """取根目录条目：[{name, type, size}]。目录名后面会加 "/" 便于阅读。"""
    body = _api(f"/repos/{full_name}/contents", ua, token, timeout)
    data = json.loads(body)
    if isinstance(data, dict) and data.get("message"):
        raise SourceError(f"contents 返回错误：{data.get('message')}")
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for ent in data[:_MAX_ENTRIES]:
        if not isinstance(ent, dict) or not ent.get("name"):
            continue
        out.append({"name": ent["name"],
                    "type": "dir" if ent.get("type") == "dir" else "file",
                    "size": ent.get("size") or 0})
    return out


def format_tree(entries: list[dict]) -> str:
    """根目录条目 → 一行人类可读的字符串：`app/ docs/ pyproject.toml`。"""
    bits = []
    for ent in entries or []:
        name = ent.get("name", "")
        if not name:
            continue
        bits.append(name + "/" if ent.get("type") == "dir" else name)
    return " ".join(bits)


class RepoContextCache:
    """README + 根目录的磁盘缓存。key = 仓库名 + 最近提交时间。

    为什么要缓存：这两个接口都吃 api.github.com 的额度（未认证只有 60 次/小时），
    而一天要采两轮（08:00 / 16:30）。带上 `pushed_at` 做 key 之后：

        代码没变（绝大多数情况）→ 直接读缓存，第二次采集零额度消耗
        代码变了                → 自动重新抓，总结跟着更新

    这也是为什么不用"按日期缓存" —— 那样仓库当天更新了总结却不更新。
    """

    def __init__(self, path, log=None, max_entries: int = 300):
        self.path = Path(path)
        self._log = log or (lambda *a, **k: None)
        self.max_entries = max_entries
        self._data: dict[str, dict] = {}
        self._dirty = False
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    @staticmethod
    def _key(full_name: str, pushed_at: str | None) -> str:
        return hashlib.sha1(f"{full_name}@{pushed_at or '?'}".encode("utf-8")).hexdigest()

    def get(self, full_name: str, pushed_at: str | None) -> dict | None:
        hit = self._data.get(self._key(full_name, pushed_at))
        if hit and (hit.get("readme") or hit.get("entries")):
            return hit
        return None

    def put(self, full_name: str, pushed_at: str | None, ctx: dict) -> None:
        self._data[self._key(full_name, pushed_at)] = ctx
        self._dirty = True
        # 只留最近 N 条：这文件是"加速器"不是"档案"，不需要无限增长
        if len(self._data) > self.max_entries:
            for k in list(self._data)[:len(self._data) - self.max_entries]:
                self._data.pop(k, None)

    def save(self) -> None:
        if not self._dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8")
            self._dirty = False
        except OSError as exc:
            self._log(f"      仓库上下文缓存写入失败（不影响出报告）：{exc}")


def fetch_repo_ctx(full_name: str, ua: str, token: str = "",
                   cache: RepoContextCache | None = None,
                   pushed_at: str | None = None) -> dict:
    """README + 根目录，带缓存。**失败不抛异常**，返回已拿到的部分。

    两次请求合并成一个函数，是因为它们总是一起用、也总是一起缓存 ——
    分开写的话每个调用方都要自己记得查两次缓存、存两次。
    """
    if cache:
        hit = cache.get(full_name, pushed_at)
        if hit:
            return {**hit, "_cached": True}

    ctx: dict = {"readme": "", "entries": [], "errors": []}
    for field, fn in (("readme", fetch_readme), ("entries", fetch_tree)):
        try:
            ctx[field] = fn(full_name, ua, token)
        except Exception as exc:  # noqa: BLE001
            ctx["errors"].append(f"{field}: {type(exc).__name__}: {exc}")

    if cache and (ctx["readme"] or ctx["entries"]):
        cache.put(full_name, pushed_at, {"readme": ctx["readme"], "entries": ctx["entries"]})
    return ctx
