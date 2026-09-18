#!/usr/bin/env python3
"""容器启动时初始化 CodeBuddy CLI 的绑定账号。

背景
----
wb-switch 为 CodeBuddy CLI 生成的认证链路是：

    ~/.codebuddy/settings.json  ->  apiKeyHelper = ~/.codebuddy-rotate/helper.cjs
    helper.cjs 读取 ~/.codebuddy-rotate/state.json 的 activeAccountId，
    输出 "Bearer <该账号的 access_token>"，CLI 将其作为 X-Api-Key /
    Authorization 头使用（apiKeyHelper 是 CodeBuddy CLI 的官方配置项）。

问题
----
若 state.json 不存在，helper.cjs 会 fallback 到 accounts[0]，即"永远用第一个
账号"，行为不确定，且与 WebUI 上显示的"当前 CLI 账号"不一致。

本脚本
------
在容器启动时，若 state.json 缺失，则调用 wb-switch 官方的
POST /api/codebuddy-cli/switch 绑定一个账号，让 wb-switch 自己写出格式正确的
state.json。仅在缺失时执行，因此不会覆盖用户手动选择或后续的自动轮换结果。

CLI 默认走国际版端点（settings.json 里 CODEBUDDY_BASE_URL=https://www.codebuddy.ai/v2），
因此优先选择 variant == "ai" 的账号，避免区域不匹配。
"""

import json
import os
import sys
import urllib.request

BACKEND = "http://127.0.0.1:57890"
STATE_FILE = "/data/.codebuddy-rotate/state.json"
POOL_CONFIG = "/data/.wb-switch/account_pool_config.json"
TAG = "[CodeBuddy-CLI]"


def _get_json(url, timeout=8):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def pick_account_id():
    """按账号池启用列表挑一个账号，优先国际版（variant=ai）。"""
    data = _get_json(f"{BACKEND}/api/accounts")
    items = data if isinstance(data, list) else data.get("accounts", [])

    try:
        with open(POOL_CONFIG, encoding="utf-8") as fh:
            enabled = json.load(fh).get("enabledAccountIds") or []
    except Exception:
        enabled = []

    pool = [a for a in items if not enabled or a.get("id") in enabled] or items
    pool.sort(key=lambda a: 0 if a.get("variant") == "ai" else 1)
    return pool[0].get("id", "") if pool else ""


def main():
    if os.path.exists(STATE_FILE):
        print(f"{TAG} state.json 已存在，保留现有绑定")
        return 0

    try:
        account_id = pick_account_id()
    except Exception as exc:
        print(f"{TAG} 读取账号失败，跳过初始化: {exc}")
        return 0

    if not account_id:
        print(f"{TAG} 无可用账号，跳过初始化")
        return 0

    payload = json.dumps({"accountId": account_id}).encode()
    req = urllib.request.Request(
        f"{BACKEND}/api/codebuddy-cli/switch",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        print(f"{TAG} 已绑定 CLI 账号: {result.get('activeAccountId')}")
    except Exception as exc:
        print(f"{TAG} 绑定失败（不影响启动）: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
