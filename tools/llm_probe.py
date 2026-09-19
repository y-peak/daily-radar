"""探针：确认大模型 API 能用、延迟多少、返回什么格式。

    python3 tools/llm_probe.py                      # 从环境变量取 key
    python3 tools/llm_probe.py --model deepseek-flash --max-tokens 2000

不打日志、不写文件，就是打一次真实的对话请求，把原始返回打出来。
用于"配好了 key 但不确定能不能用"的时候快速定位。

⚠️ 特别关注两件事，都是实测踩过的：

  1. **正文是空的 + finish_reason=length** → 模型的「思考」把 max_tokens 吃光了。
     `deepseek-flash` / `deepseek-v4-pro` 都是**带思考的模型**，2000 以下很容易
     全是 reasoning tokens、正文一个字都没有。见下面的「思考 token」一行。
  2. **「真实模型」与请求的模型名不一致** → 说明这个客户端名是个别名，
     被服务端路由到了别的后端（`deepseek-reasoner` 就路由到 `deepseek-flash`）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "https://api.deepseek.com"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("RADAR_LLM_BASE", DEFAULT_BASE))
    ap.add_argument("--model", default=os.environ.get("RADAR_LLM_MODEL", "deepseek-flash"))
    ap.add_argument("--key-env", default="RADAR_LLM_KEY")
    ap.add_argument("--max-tokens", type=int, default=800,
                    help="带思考的模型要留足空间，太小会全是 reasoning、正文为空")
    ap.add_argument("--json-mode", action="store_true",
                    help="要求返回 JSON（测模型是否支持 response_format）")
    args = ap.parse_args()

    key = (os.environ.get(args.key_env) or os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not key:
        print(f"✗ 环境变量 {args.key_env} 为空")
        return 2
    print(f"· key {key[:7]}…{key[-4:]}（长度 {len(key)}）")
    print(f"· 端点 {args.base}/chat/completions")
    print(f"· 模型 {args.model}")

    payload: dict = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": "你是技术编辑，用简体中文回答。"},
            {"role": "user", "content": "用一句话说明 vLLM 的 PagedAttention 解决了什么问题。"},
        ],
        "max_tokens": args.max_tokens,
        "temperature": 0.3,
    }
    if args.json_mode:
        payload["response_format"] = {"type": "json_object"}
        payload["messages"][1]["content"] = (
            '只输出 JSON，形如 {"a": "一句话"}，说明 vLLM 是什么。')

    req = urllib.request.Request(
        args.base.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        print(f"✗ HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:500]}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"✗ {type(exc).__name__}: {exc}")
        return 1

    elapsed = time.time() - t0
    print(f"✓ HTTP 200，{elapsed:.2f}s")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        print("返回不是 JSON：", body[:500])
        return 1

    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    finish = choice.get("finish_reason")
    usage = data.get("usage") or {}
    reasoning = ((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0

    print("· 内容：", content or "（空）")
    print(f"· 结束原因：{finish}   思考 token：{reasoning}")
    print("· 用量：", usage)
    print("· 真实模型：", data.get("model"))

    if not content and reasoning:
        print(f"✗ 正文为空，但烧了 {reasoning} 个思考 token —— "
              f"max_tokens={args.max_tokens} 太小，全被「思考」吃光了。"
              f" 调大 --max-tokens（试试 2000）再测。")
        return 1
    if not content:
        print("✗ 正文为空，且不是思考吃掉的 → 看上面的 finish_reason 和用量。")
        return 1
    if reasoning:
        print(f"ℹ️ 这是带思考的模型：本次思考 {reasoning} / 正文 "
              f"{usage.get('completion_tokens', 0) - reasoning} tokens。"
              f" 生产代码里 max_tokens 必须留出思考空间。")
    if data.get("model") and data["model"] != args.model:
        print(f"ℹ️ 请求 {args.model} 被路由到 {data['model']}（别名）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
