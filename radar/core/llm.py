"""大模型客户端 —— 让"看懂一个仓库"这件事从机翻升级成"真的读懂了"。

## 它和 translate.py 的分工

    translate.py   把一句英文简介换成中文      —— 逐字层面
    llm.py         读完 README 再写一段人话     —— 理解层面

英文 description 常常只有半句话（"A fast ORM for Python"），机翻出来还是半句话，
读者仍然不知道这玩意解决什么问题、该不该点进去。要让总结真正有用，必须
**给它更多原料**（README 正文、根目录结构、语言构成），再让它写。

## 为什么是 OpenAI 兼容协议

DeepSeek / 月之暗面 / 智谱 / 硅基流动 / 本地 vLLM 都提供
`POST {base_url}/chat/completions` 这一套。只写一种协议，换个 `base_url`
就能换供应商 —— 这也是为什么用户以后自己起 vLLM 也能直接接进来。

## 三条硬约束（和 translate.py 同源，因为同样是无人值守流水线）

1. **永不抛异常**。模型超时、key 失效、余额不足、限流 —— 全部吞掉返回 ""，
   由调用方决定降级成什么。绝不能让"锦上添花"的东西搞崩整轮采集。
2. **必须落盘缓存**。同一批仓库一天跑两次（08:00 / 16:30），语义没变就不该
   重复花钱。缓存 key 里带上模型名，换模型会自然失效。
3. **key 不进仓库**。config.json 是要推到公开 GitHub 的，所以 key 从
   环境变量读（服务器上是 `.env`，systemd 注入）。

## 缓存里存什么

存**接口返回的原始文本**（不是解析后的 dict）：以后改了 JSON 解析逻辑、
改了字段名，老缓存立刻跟着受益，不需要清缓存重花一遍钱。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

# ------------------------------------------------------------------ 供应商表
# 都走 OpenAI 兼容协议，区别只在 base_url 和默认模型。
PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        # ⚠️ `deepseek-flash` / `deepseek-v4-pro` 都是**带思考的模型**：
        #    返回里 `completion_tokens_details.reasoning_tokens` 是「思考」，
        #    它和正文**共用** max_tokens 预算。给少了会出现
        #    「finish_reason=length + 正文为空」——不是报错，是安静地什么都没写出来。
        #    所以：① max_tokens 要留足（见 chat() 里的自动加预算重试）
        #          ② 别把 `deepseek-chat` 当成"另一个模型"，它只是路由过去的别名
        # 实测（同一句提问，JSON 模式）：max_tokens=200 → 正文空、思考 200；
        #    max_tokens=500 → 正常；max_tokens=2000 → 正常。差别只在预算够不够。
        "model": "deepseek-flash",
    },
    "openai": {"base_url": "https://api.openai.com", "model": "gpt-4o-mini"},
    "moonshot": {"base_url": "https://api.moonshot.cn", "model": "moonshot-v1-8k"},
    "zhipu": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    "siliconflow": {"base_url": "https://api.siliconflow.cn", "model": "Qwen/Qwen2.5-7B-Instruct"},
    # 自己在本机/服务器起 vLLM：把 base_url 指向 http://127.0.0.1:8000/v1 即可
    "local": {"base_url": "http://127.0.0.1:8000/v1", "model": "local-model"},
}

# key 默认从这个环境变量读（用户级、服务器级都放这里，不进仓库）
DEFAULT_KEY_ENV = "RADAR_LLM_KEY"

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

# 模型偶尔会把答案再包一层"信封"。实测（deepseek-flash，10 个仓库里出现 1 个）：
#     {"type":"json_object","content":{"tagline":"…","detail":"…","usage":"…"}}
# 原因：`response_format={"type":"json_object"}` 有时被模型**理解成要输出的内容格式**，
# 于是它老老实实回了一个 `type` + `content` 的信封。字段全在，只是层级错了。
# 这**不是协议错误**，重试也可能再套一层 —— 所以必须在这里兼容掉。
_ENVELOPE_KEYS = ("content", "data", "result", "json", "output", "answer", "value")


def _strip_fence(text: str) -> str:
    """模型有时会把 JSON 包在 ```json 里，剥掉再解析。"""
    return _FENCE.sub("", (text or "").strip()).strip()


def _unwrap_envelope(data: dict, required: tuple[str, ...]) -> dict:
    """剥掉模型自己套的信封层（只在顶层缺 required 字段时才动手）。

    判据刻意保守：**必须**是"剥开之后能凑齐 required 字段"才剥。
    本项目要的 JSON 都是扁平结构（tagline/detail/usage），所以不会误伤。
    """
    if not required or all(k in data for k in required):
        return data
    for key in _ENVELOPE_KEYS:
        inner = data.get(key)
        if isinstance(inner, dict) and all(k in inner for k in required):
            return inner
    # 兜底：整层里只有一个 dict 值，且它恰好包含全部 required
    inner_dicts = [v for v in data.values() if isinstance(v, dict)]
    if len(inner_dicts) == 1 and all(k in inner_dicts[0] for k in required):
        return inner_dicts[0]
    return data


class LLM:
    """一个"会失败但不会炸"的大模型调用器。

    对外主要两个入口：

        chat(system, user)             → 字符串（失败返回 ""）
        chat_json(system, user, keys)  → dict（失败返回 None）
    """

    def __init__(self, cfg: dict | None = None, cache_path: Path | None = None,
                 log=None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        name = (cfg.get("provider") or "deepseek").strip().lower()
        preset = PROVIDERS.get(name, {})
        self.provider = name
        self.base_url = (cfg.get("base_url") or preset.get("base_url")
                         or "https://api.deepseek.com").rstrip("/")
        self.model = (cfg.get("model") or preset.get("model") or "deepseek-chat").strip()
        self.timeout = int(cfg.get("timeout", 90))
        self.max_tokens = int(cfg.get("max_tokens", 2000))
        # 思考吃光预算时最多加到多少（防止一路翻倍把额度烧穿）
        self.max_retry_tokens = int(cfg.get("max_retry_tokens", 8000))
        self.temperature = float(cfg.get("temperature", 0.3))

        # key 的取值顺序：环境变量 > 配置里的字面量。
        # 反过来的话，config.json 一旦提交就泄露了 —— 顺序本身就是安全措施。
        self.key_env = (cfg.get("api_key_env") or DEFAULT_KEY_ENV).strip()
        self.api_key = (os.environ.get(self.key_env) or "").strip() \
            or (cfg.get("api_key") or "").strip()

        self.cache_path = Path(cache_path) if cache_path else None
        self._log = log or (lambda *a, **k: None)
        self._cache: dict[str, str] = {}
        self._dirty = False
        self._stats = {"hit": 0, "new": 0, "fail": 0, "tokens": 0,
                       "reasoning": 0, "bumped": 0, "json_bumped": 0}
        self._load_cache()

    # -------------------------------------------------------------- 可用性
    @property
    def available(self) -> bool:
        return bool(self.enabled and self.api_key)

    def why_unavailable(self) -> str:
        if not self.enabled:
            return "未启用（llm.enabled=false）"
        if not self.api_key:
            return f"没读到 key（环境变量 {self.key_env} 为空）"
        return ""

    # ------------------------------------------------------------------ 缓存
    def _load_cache(self) -> None:
        if not self.cache_path or not self.cache_path.exists():
            return
        try:
            self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self._cache = {}

    def save_cache(self) -> None:
        if not self.cache_path or not self._dirty:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=1, sort_keys=True),
                encoding="utf-8")
            self._dirty = False
        except OSError as exc:
            self._log(f"      大模型缓存写入失败（不影响出报告）：{exc}")

    @staticmethod
    def _key(*parts: str) -> str:
        return hashlib.sha1("\x00".join(parts).encode("utf-8")).hexdigest()

    def _ckey(self, system: str, user: str, json_mode: bool) -> str:
        return self._key(self.model, str(json_mode), system, user)

    def log(self, msg: str) -> None:
        """对外暴露日志口（模块层要报"哪个仓库没写出介绍"）。"""
        self._log(msg)

    # ------------------------------------------------------------------ 调用
    def _post(self, payload: dict) -> dict:
        """真发一次 POST。异常一律抛给 chat() 统一吞。"""
        url = f"{self.base_url}/chat/completions"
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def chat(self, system: str, user: str, *, json_mode: bool = False,
             max_tokens: int | None = None, use_cache: bool = True) -> str:
        """发一次对话请求。**任何失败都返回空串**，绝不抛异常。

        两处刻意加的容错，都是实测踩出来的：

        - `json_mode=True` 时若模型/网关不认 `response_format`（400/422），
          自动摘掉该参数再试一次 —— 能跑通比格式严格更重要。
        - **思考吃光预算时自动加预算重试一次**：带思考的模型
          （`deepseek-flash` / `deepseek-v4-pro`）把 reasoning 和正文**共用一个
          max_tokens 预算**，给少了就 `finish_reason=length` + 正文为空。
          这不是报错、更像"安静地什么都没写"，所以必须自己兜住。
        """
        if not self.available:
            return ""

        key = self._ckey(system, user, json_mode)
        if use_cache and key in self._cache:
            self._stats["hit"] += 1
            return self._cache[key]

        budget = max_tokens or self.max_tokens
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": self.temperature,
            "max_tokens": budget,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        data = self._call(payload)
        if data is None:
            return ""
        text, finish, reasoning, total = self._extract(data)
        self._stats["tokens"] += total
        self._stats["reasoning"] += reasoning

        if not text and finish == "length" and reasoning:
            bigger = min(budget * 4, self.max_retry_tokens)
            if bigger > budget:
                self._stats["bumped"] += 1
                self._log(f"      思考吃光了 max_tokens（{budget}，"
                          f"思考 {reasoning}），加到 {bigger} 重试一次…")
                payload["max_tokens"] = bigger
                data = self._call(payload)
                if data is None:
                    return ""
                text, finish, reasoning, total = self._extract(data)
                self._stats["tokens"] += total
                self._stats["reasoning"] += reasoning

        if not text:
            # 记一条日志：否则"一句话都没生成"在整轮日志里完全看不出来
            self._log(f"      模型返回空正文（finish_reason={finish}，"
                      f"思考 {reasoning} tokens）")
            return ""

        self._stats["new"] += 1
        if use_cache:
            self._cache[key] = text
            self._dirty = True
        return text

    def _call(self, payload: dict) -> dict | None:
        """发一次 POST，失败返回 None（并把失败计入统计、打日志）。"""
        body = dict(payload)
        for attempt in (1, 2):
            try:
                return self._post(body)
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:200]
                except Exception:  # noqa: BLE001
                    pass
                # 400/422 且带了 response_format → 大概率是这个参数不被支持，摘掉重试
                if attempt == 1 and "response_format" in body and exc.code in (400, 422):
                    self._log("      模型不支持 JSON 模式，摘掉该参数重试…")
                    body.pop("response_format", None)
                    continue
                self._log(f"      大模型调用失败 HTTP {exc.code}：{detail}")
                self._stats["fail"] += 1
                return None
            except Exception as exc:  # noqa: BLE001 超时/网络/解析全都算失败
                self._log(f"      大模型调用失败 {type(exc).__name__}：{exc}")
                self._stats["fail"] += 1
                return None
        self._stats["fail"] += 1
        return None

    @staticmethod
    def _extract(data: dict) -> tuple[str, str, int, int]:
        """返回 (正文, finish_reason, 思考 tokens, 总 tokens)。

        把"思考 token"单独取出来：它是判断"正文为什么是空的"的唯一线索。
        """
        choices = data.get("choices") or []
        text, finish = "", ""
        if choices and isinstance(choices[0], dict):
            choice = choices[0]
            finish = str(choice.get("finish_reason") or "")
            msg = choice.get("message") or {}
            # reasoner 风格的模型思考在 reasoning_content 里，正文只取 content
            text = (msg.get("content") or "").strip()
        usage = data.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        reasoning = int(details.get("reasoning_tokens") or 0)
        total = int(usage.get("total_tokens") or 0)
        return text, finish, reasoning, total

    def chat_json(self, system: str, user: str, *, max_tokens: int | None = None,
                  required: tuple[str, ...] = ()) -> dict | None:
        """要一段 JSON 回来。解析失败返回 None（调用方走降级）。

        两层容错，都是实测踩出来的：

        - **剥信封**：模型偶尔把字段包进 `{"type":"json_object","content":{…}}`
          （把 `response_format` 理解成了输出格式），交给 `_unwrap_envelope` 兼容。
          这类错误重试可能照样再套一层，所以必须在这里解掉，不能靠重试。
        - **截断后加预算重来一次**：带思考的模型把 reasoning 和正文**共用一个预算**，
          正文写到一半被切断时 `finish_reason=length` 但**正文非空**（只是不是合法 JSON）。
          `chat()` 那道兜底只在「正文为空」时触发，拦不住这种，所以这里必须自己补。
          实测：根目录结构最长（666 字符）的那个仓库就因此整段介绍没生成。
        """
        raw = self.chat(system, user, json_mode=True, max_tokens=max_tokens)
        data = self._parse_json(raw, required)
        if data is not None:
            return data
        # 正文为空 = chat() 那边已经加预算重试过且确实失败了，不再重复花钱
        if not raw:
            return None

        budget = max_tokens or self.max_tokens
        bigger = min(budget * 4, self.max_retry_tokens)
        if bigger <= budget:
            return None
        self._stats["json_bumped"] += 1
        self._log(f"      JSON 不完整（{len(raw)} 字符，预算 {budget}）→ "
                  f"加到 {bigger} 重来一次…")
        raw2 = self.chat(system, user + self._json_hint(required),
                         json_mode=True, max_tokens=bigger)
        data = self._parse_json(raw2, required)
        if data is None:
            return None
        # ⭐ 把成功结果写回「原始提示词」的缓存位：否则每轮都会先失败一次、
        #    再带着重试提示跑第二次 —— 等于每天两轮白多花两次请求。
        self._cache[self._ckey(system, user, True)] = raw2
        self._dirty = True
        return data

    @staticmethod
    def _json_hint(required: tuple[str, ...]) -> str:
        """重试时追加的提示：把两种常见失败模式一次说清。

        实测的两种（都真实踩到过）：
          · 被 length 截断 —— 提醒"精简 + 闭合结尾"
          · 把字段包进 `{"type":"json_object","content":{…}}` —— 提醒"直接输出、别套外层"
        """
        keys = "、".join(required)
        must = f"必须包含这些键：{keys}。" if keys else ""
        return ("\n\n【重试】上一次回复没法解析成想要的 JSON"
                "（可能被截断了，或者把字段包在了 `content` 之类的外层里）。"
                f"请**直接**输出这一个 JSON 对象，{must}"
                "内容精简，务必闭合结尾的 }，不要再套外层。")

    @staticmethod
    def _parse_json(raw: str, required: tuple[str, ...] = ()) -> dict | None:
        """把一段文本解析成 dict；解析失败或缺必需键都返回 None。"""
        if not raw:
            return None
        try:
            data = json.loads(_strip_fence(raw))
        except json.JSONDecodeError:
            # 有时模型会前后加一句话，尝试截取第一个 { 到最后一个 }
            start, end = raw.find("{"), raw.rfind("}")
            if start < 0 or end <= start:
                return None
            try:
                data = json.loads(_strip_fence(raw[start:end + 1]))
            except json.JSONDecodeError:
                return None
        if not isinstance(data, dict):
            return None
        data = _unwrap_envelope(data, required)
        if required and not all(k in data for k in required):
            return None
        return data

    # ------------------------------------------------------------------ 统计
    def summary_line(self) -> str:
        s = self._stats
        line = (f"大模型 {self.provider}/{self.model}：缓存命中 {s['hit']}、"
                f"新请求 {s['new']}、失败 {s['fail']}、共 {s['tokens']} tokens")
        # 思考 token 单独报：它是成本大头，也是"正文为空"的唯一线索
        if s["reasoning"]:
            line += f"（其中思考 {s['reasoning']}）"
        if s["bumped"]:
            line += f"；{s['bumped']} 次因思考吃光预算而自动加预算"
        if s.get("json_bumped"):
            line += f"；{s['json_bumped']} 次因 JSON 截断而加预算重来"
        return line

    def log_summary(self) -> None:
        self._log("      " + self.summary_line())
