"""把英文简介翻成中文。

## 为什么需要它

GitHub 上的 description 全是英文，每天看十条英文句子很累。这个模块把它翻成中文。

## 设计约束（决定了为什么是现在这个样子）

流水线是**无人值守**跑的（systemd timer，每天 08:00 / 16:30），所以：

1. **不能用需要人工介入的东西**（不能弹窗、不能要求登录）
2. **不能因为翻译失败就让整轮采集崩掉** —— 翻不出来就退回英文，报告照出
3. **不能用需要付费 key 的服务**（默认配置下开箱即用；配了 key 就自动升级）

于是走"多 provider 降级 + 本地磁盘缓存 + 失败退回原文"这条路。

## 两级质量，自动选择

    配了大模型 key  → 走 llm（见 core/llm.py），译文更顺、专有名词不会被直译
    没配 / 调用失败 → 退回有道、MyMemory 这些免 key 接口

判断在运行时做（`_build_providers`），所以**给不给 key 是同一个代码路径**，
运维上不需要改任何别的地方 —— 把 `RADAR_LLM_KEY` 塞进 `.env` 就升级了。

## 缓存为什么必须有

免 key 接口都有额度限制（MyMemory 匿名每天几千字），大模型则是按 token 计费；
而网页上每 60 秒可能就有一次重建。同一句英文翻一万次纯属浪费，也可能被限流。
所以按内容落盘缓存，翻过就不再翻：

    data/.translate_cache.json

⚠️ 缓存 key 里**带上 provider 签名**（`_sig()`）：早期用有道翻的结果和现在用大模型
翻的结果质量差很多，如果不区分，换了渠道还会一直吃老缓存，升级等于没升。
代价是换渠道后要重翻一遍，值得。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
from pathlib import Path

# ---------------------------------------------------------------- 术语大小写修正
# 机翻会把专有名词和缩写搞成小写（llm / gpu / api），读起来很别扭。
# 翻译完成后统一纠回来。只放"一定要大写"的，避免误伤普通词。
ACRONYMS = {
    "llm": "LLM", "llms": "LLMs", "gpu": "GPU", "gpus": "GPUs", "cpu": "CPU",
    "api": "API", "apis": "APIs", "cli": "CLI", "sdk": "SDK", "ide": "IDE",
    "sql": "SQL", "nosql": "NoSQL", "http": "HTTP", "https": "HTTPS",
    "json": "JSON", "yaml": "YAML", "toml": "TOML", "xml": "XML",
    "html": "HTML", "css": "CSS", "js": "JS", "ts": "TS", "ui": "UI", "ux": "UX",
    "ai": "AI", "ml": "ML", "nlp": "NLP", "rag": "RAG", "mcp": "MCP",
    "orm": "ORM", "jwt": "JWT", "oauth": "OAuth", "rest": "REST", "grpc": "gRPC",
    "graphql": "GraphQL", "os": "OS", "ram": "RAM", "ssd": "SSD", "ssh": "SSH",
    "tls": "TLS", "ssl": "SSL", "dns": "DNS", "tcp": "TCP", "udp": "UDP",
    "ci": "CI", "cd": "CD", "vm": "VM", "k8s": "K8s", "wasm": "WASM",
    "openai": "OpenAI", "github": "GitHub", "gitlab": "GitLab",
    "javascript": "JavaScript", "typescript": "TypeScript", "python": "Python",
    "node.js": "Node.js", "nodejs": "Node.js", "react": "React", "vue": "Vue",
    "docker": "Docker", "kubernetes": "Kubernetes", "redis": "Redis",
    "postgresql": "PostgreSQL", "postgres": "PostgreSQL", "mysql": "MySQL",
    "mongodb": "MongoDB", "sqlite": "SQLite", "nginx": "Nginx",
    "tensorflow": "TensorFlow", "pytorch": "PyTorch", "linux": "Linux",
    "macos": "macOS", "windows": "Windows", "android": "Android",
    "ios": "iOS", "openclaw": "OpenClaw",
    # 榜单上高频出现的专有名词（机翻常把它们搞成小写）
    "whatsapp": "WhatsApp", "bittorrent": "BitTorrent", "telegram": "Telegram",
    "discord": "Discord", "youtube": "YouTube", "chatgpt": "ChatGPT",
    "claude": "Claude", "grok": "Grok", "polymarket": "Polymarket",
    "binance": "Binance", "solana": "Solana", "ethereum": "Ethereum",
    "pvr": "PVR", "tv": "TV", "p2p": "P2P", "vpn": "VPN", "url": "URL",
    "etl": "ETL", "kpi": "KPI", "evm": "EVM", "lgpd": "LGPD", "wa": "WA",
}
# 多词专有名词：单靠逐词大写修不了（"android tv" → "Android TV"）
_PHRASES = {
    r"\bandroid\s+tv\b": "Android TV",
    r"\bnode\.?js\b": "Node.js",
    r"\bnext\.?js\b": "Next.js",
    r"\bvue\.?js\b": "Vue.js",
}
_PHRASE_RES = [(re.compile(p, re.IGNORECASE), r) for p, r in _PHRASES.items()]

# ------------------------------------------------------- 品牌名误译的定向修复
# 机翻把"本身就是普通英文词"的产品名按字面意思翻了，例如：
#     Intercom → 对讲机     Notion → 概念     Gemini → 双子座     Flash → 闪光
# 实测过 <b> 标记、方括号、大写等所有"保护"写法：**只要该词本身是个普通英文词，
# 有道照样翻**（Intercom 在任何编码下都变成"对讲机"）。所以只能事后修。
#
# 安全性靠**英文原文里确实出现过这个词**来保证 —— 只有原文含 `Intercom` 且
# 译文出现"对讲机"时才替换。否则"对讲机"这种正常词会被误伤。
BRAND_REPAIR: dict[str, tuple[str, ...]] = {
    "intercom": ("对讲机",),
    "notion": ("概念",),
    "gemini": ("双子座",),
    "flash": ("闪光",),
    "antigravity": ("反重力",),
    "airtable": ("空军基地表",),
    "linear": ("线性的",),
    "vercel": ("维尔塞尔",),
    "stripe": ("条纹",),
    "slack": ("松弛",),
    "rust": ("铁锈",),
    "swift": ("斯威夫特",),
    "go": ("去",),          # 只在原文含 Go 且译文出现独立"去"时才有意义，见下方限制
}
# "go" 这类太容易误伤的，不参与自动替换
_BRAND_SAFE = {k: v for k, v in BRAND_REPAIR.items() if k != "go"}
# ⚠️ 这里**不能**用 \b 做边界。Python 的 \b 是按 \w 判定的，而中文也算 \w，
#    于是 `面向llm的` 里 `向|l` 之间没有边界 → `\bllm\b` 匹配不上，
#    缩写就大写不了。改成"前后不能是 ASCII 字母"的环视：
#    既不误伤 Airtable(含 ai)、clips(含 cli) 这类词，又能正确处理中文紧邻的情况。
_ACRO_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(re.escape(k) for k in ACRONYMS) + r")(?![A-Za-z])",
    re.IGNORECASE)

# 中文字符占比超过这个值就认为"本来就是中文"，不再翻译
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    return len(_CJK_RE.findall(text)) / max(1, len(text))


def _fix_mixed(text: str) -> str:
    """中文排版清理：机翻常见的空格问题。

    ⚠️ 全部用**环视（lookaround）**而不是捕获组。用 `(汉字)\\s+(汉字)` 这种写法
    会踩 re.sub 不重叠扫描的坑：替换掉前一个汉字后，指针已经越过它，
    紧跟的空格就再也匹配不上，于是"免费的 工具"这种残留空格清不掉。
    环视不消费字符，就不会有这个问题。
    """
    if not text:
        return ""
    out = text.strip()
    out = re.sub(r"\s+", " ", out)                       # 换行/多空格压成一个
    out = re.sub(r"\s+(?=[，。、；：？！）】」』%])", "", out)   # 标点/百分号前不留空格
    out = re.sub(r"(?<=[，。、；：？！（【「『])\s+", "", out)   # 标点/开括号后不留空格
    out = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[（【「『])", "", out)  # 汉字后的开括号紧贴
    out = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", out)  # 汉字之间的空格
    return out.strip()


def _fix_acronyms(text: str) -> str:
    out = _ACRO_RE.sub(lambda m: ACRONYMS[m.group(0).lower()], text)
    for rx, repl in _PHRASE_RES:
        out = rx.sub(repl, out)
    return out


def _repair_brands(zh: str, source_en: str) -> str:
    """把被机翻当普通词翻掉的产品名改回来。

    只在**英文原文确实含该词**时才动手 —— 这是唯一的防误伤手段。
    例如原文有 `Intercom`、译文出现"对讲机"，才把它换回 Intercom；
    原文没有 Intercom 时，"对讲机"照旧不动（它可能真是对讲机）。
    """
    if not zh or not source_en:
        return zh
    src = source_en.lower()
    out = zh
    for brand, wrongs in _BRAND_SAFE.items():
        if brand not in src:
            continue
        # 原文里已经有该品牌名，说明译文可能没丢；丢了才修
        if brand in out.lower():
            continue
        for wrong in wrongs:
            if wrong in out:
                out = out.replace(wrong, brand.capitalize()
                                  if brand.islower() else brand)
    return out


class _Provider:
    """一个翻译后端。返回 None 表示这个后端没翻出来（交给下一个）。"""

    name = "?"

    def translate(self, http, text: str) -> str | None:  # pragma: no cover
        raise NotImplementedError

    def translate_many(self, texts: list[str]) -> list[str | None] | None:
        """批量翻译。返回 None 表示"我不支持批量"，交给逐条逻辑。

        大模型支持批量（十句话一次请求，比十次请求省 90% 往返和开销），
        免 key 的接口不支持。
        """
        return None


class _Youdao(_Provider):
    """有道翻译的公开 demo 接口。质量比 MyMemory 好（inference 会译成"推理"）。"""

    name = "youdao"
    URL = "https://aidemo.youdao.com/trans"

    def translate(self, http, text: str) -> str | None:
        url = self.URL + "?" + urllib.parse.urlencode(
            {"q": text, "from": "en", "to": "zh-CHS"})
        data = http.get_json(url, retries=2)
        parts = data.get("translation")
        if isinstance(parts, list) and parts:
            return "".join(str(p) for p in parts)
        return None


class _MyMemory(_Provider):
    """MyMemory：完全免费、免 key，作为兜底。匿名有每日额度。"""

    name = "mymemory"
    URL = "https://api.mymemory.translated.net/get"

    def translate(self, http, text: str) -> str | None:
        url = self.URL + "?" + urllib.parse.urlencode(
            {"q": text, "langpair": "en|zh-CN"})
        data = http.get_json(url, retries=2)
        rd = data.get("responseData") or {}
        out = rd.get("translatedText")
        # 被限流时它会回一个说明性英文句子，不是真翻译，丢掉
        if not out or "MYMEMORY WARNING" in out.upper() or "QUERY LENGTH LIMIT" in out.upper():
            return None
        return str(out)


_LLM_SYSTEM = (
    "你是技术编辑，负责把软件项目的英文简介翻成简体中文，给中文开发者看。"
    "只输出 JSON，不要任何解释。"
)

_LLM_USER = """把下面 JSON 里 t 数组的每一条英文简介翻成简体中文。

要求：
- 保留产品名 / 公司名 / 技术缩写 / 框架名的原样（例：Intercom、Notion、Gemini、
  LLM、GPU、API、Kubernetes 不要意译）
- 简洁，保持原来一句话的长度，不要补充原文没有的信息
- 不要加书名号、不要加"这是一个"之类的起手式

只输出 JSON 对象，形如 {{"t": ["译文1", "译文2"]}}，t 的长度必须与输入一致。

输入：{payload}"""


class _LLM(_Provider):
    """走大模型翻译。质量最好，且天然不会把产品名直译。"""

    def __init__(self, llm, chunk: int = 12):
        self.llm = llm
        self.chunk = max(1, chunk)

    @property
    def name(self) -> str:
        """签名里带上**具体模型名**，不是笼统的 "llm"。

        否则换模型（deepseek-flash → deepseek-v4-pro）时翻译缓存照样命中，
        等于"换了模型但译文还是旧模型翻的" —— 和换渠道不失效是同一类 bug。
        """
        return f"llm:{self.llm.model}"

    def translate(self, http, text: str) -> str | None:
        got = self.translate_many([text])
        return got[0] if got else None

    def translate_many(self, texts: list[str]) -> list[str | None] | None:
        out: list[str | None] = []
        for i in range(0, len(texts), self.chunk):
            part = texts[i:i + self.chunk]
            payload = json.dumps({"t": part}, ensure_ascii=False)
            # 预算里要给**思考**留位置：带思考的模型（deepseek-flash / v4-pro）
            # reasoning 与正文共用 max_tokens，给少了会正文全空。
            data = self.llm.chat_json(
                _LLM_SYSTEM, _LLM_USER.format(payload=payload),
                max_tokens=min(8000, 200 * len(part) + 800),
                required=("t",))
            got = (data or {}).get("t")
            # 长度对不上说明模型漏译/串行，整块放弃（宁可退回免 key 接口，
            # 也不能把错位的译文安到别的仓库头上）
            if not isinstance(got, list) or len(got) != len(part):
                out.extend([None] * len(part))
            else:
                out.extend([str(x).strip() or None for x in got])
        return out


class Translator:
    """带缓存和降级的翻译器。**对外只有 to_zh / translate_batch，且永不抛异常。**

    传了可用的大模型（`llm=`）就优先走它；否则走免 key 接口。这两条路径
    是同一套缓存和降级逻辑，调用方不用关心走了哪条。
    """

    def __init__(self, cfg: dict | None = None, http=None, cache_path: Path | None = None,
                 log=None, llm=None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        order = cfg.get("providers") or ["youdao", "mymemory"]
        table = {"youdao": _Youdao, "mymemory": _MyMemory}
        self.llm_provider = (_LLM(llm, chunk=int(cfg.get("llm_chunk", 12)))
                             if getattr(llm, "available", False) else None)
        self.providers = [table[n]() for n in order if n in table]
        # 渠道签名进缓存 key：换了渠道（免 key → 大模型）就该重新翻一遍，
        # 否则升级了却还在吃老渠道的低质量缓存，等于没升级。
        chain = ([self.llm_provider.name] if self.llm_provider else []) \
            + [p.name for p in self.providers]
        self.sig = "+".join(chain) or "none"
        self.http = http
        self.cache_path = Path(cache_path) if cache_path else None
        self._log = log or (lambda *a, **k: None)
        self._cache: dict[str, str] = {}
        self._dirty = False
        self._stats = {"hit": 0, "new": 0, "fail": 0, "skip": 0}
        self._load_cache()

    @property
    def backend(self) -> str:
        """当前用的渠道，给日志和自检看。"""
        return "大模型" if self.llm_provider else "免 key 接口"

    # ------------------------------------------------------------------ 缓存
    def _load_cache(self) -> None:
        if not self.cache_path or not self.cache_path.exists():
            return
        try:
            self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self._cache = {}

    def save_cache(self) -> None:
        """显式落盘。不放在 to_zh 里逐条写 —— 那样每次翻译都要写一遍文件。"""
        if not self.cache_path or not self._dirty:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=1, sort_keys=True),
                encoding="utf-8")
            self._dirty = False
        except OSError as exc:
            self._log(f"      翻译缓存写入失败（不影响出报告）：{exc}")

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    def _key(self, text: str) -> str:
        return self._hash(f"{self.sig}\x00{text}")

    # ---------------------------------------------------------------- 后处理
    @staticmethod
    def _finalize(raw: str, source_en: str) -> str:
        """机翻后处理链。大模型翻的结果过一遍也无害（多数规则空转）。"""
        return _repair_brands(_fix_mixed(_fix_acronyms(raw)), source_en)

    # ------------------------------------------------------------ 单条（兜底路径）
    def _fetch_one(self, text: str) -> str | None:
        """没走批量时的一条。先大模型，再免 key 接口。"""
        if self.llm_provider:
            got = self.llm_provider.translate_many([text])
            if got and got[0]:
                return got[0]
        for prov in self.providers:
            try:
                got = prov.translate(self.http, text)
            except Exception:  # noqa: BLE001  —— provider 挂了就换下一个
                continue
            if got:
                return got
        return None

    def to_zh(self, text: str) -> str:
        """翻成中文。任何异常都吞掉并返回原文 —— 这是刻意的：

        翻译只是"锦上添花"，不该有能力让整轮采集失败。
        """
        return self.translate_batch([text], quiet=True)[0]

    # ------------------------------------------------------------------ 批量
    def translate_batch(self, texts: list[str], quiet: bool = False) -> list[str]:
        """批量翻译。返回的列表与输入等长。

        顺序上做了三件事，目的是"尽量少发请求"：

            1. 先查缓存，命中直接出结果
            2. 没命中的攒起来，交给大模型**一次请求翻一批**（十句话一个往返）
            3. 大模型没翻出来的（没配 key / 调用失败 / 长度对不上），
               再逐条走免 key 接口兜底
        """
        src = [(t or "").strip() for t in texts]
        out: list[str | None] = [None] * len(src)

        # ---- 1. 预处理 + 查缓存
        pending: list[tuple[int, str, str]] = []      # (下标, 原文, 缓存key)
        for i, text in enumerate(src):
            if not text or not self.enabled:
                out[i] = ""
                continue
            # 本来就是中文（或中英混排为主）就不用翻了
            if _cjk_ratio(text) > 0.3:
                self._stats["skip"] += 1
                out[i] = text
                continue
            key = self._key(text)
            raw = self._cache.get(key)
            if raw:
                self._stats["hit"] += 1
                out[i] = self._finalize(raw, text)
            else:
                pending.append((i, text, key))

        # ---- 2. 大模型批量
        if pending and self.llm_provider:
            batch = [p[1] for p in pending]
            got = self.llm_provider.translate_many(batch) or []
            left: list[tuple[int, str, str]] = []
            for item, zh in zip(pending, list(got) + [None] * len(pending)):
                if zh:
                    # ⚠️ 缓存里存的是**接口原始输出**，不是后处理成品：
                    #    以后改进缩写/排版/品牌名规则时，已有缓存立刻跟着受益，
                    #    不需要清缓存重翻（省额度，也避免新旧数据风格不一致）。
                    self._cache[item[2]] = zh
                    self._dirty = True
                    self._stats["new"] += 1
                    out[item[0]] = self._finalize(zh, item[1])
                else:
                    left.append(item)
            pending = left

        # ---- 3. 免 key 接口逐条兜底
        for pos, (i, text, key) in enumerate(pending):
            raw = None
            for prov in self.providers:
                try:
                    raw = prov.translate(self.http, text)
                except Exception:  # noqa: BLE001
                    continue
                if raw:
                    break
            if raw:
                self._cache[key] = raw
                self._dirty = True
                self._stats["new"] += 1
                out[i] = self._finalize(raw, text)
            else:
                self._stats["fail"] += 1
                out[i] = ""      # 调用方决定退化成什么
            # 只有真的发了请求才慢一点，别把免费接口打挂
            if pos < len(pending) - 1:
                time.sleep(0.4)

        self.save_cache()
        s = self._stats
        if not quiet:
            self._log(f"      翻译 {len(src)} 条（{self.backend}）：缓存命中 {s['hit']}、"
                      f"新翻 {s['new']}、失败 {s['fail']}、跳过(已是中文) {s['skip']}")
        return [x if x is not None else "" for x in out]
