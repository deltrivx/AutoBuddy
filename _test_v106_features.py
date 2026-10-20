#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.0.6 专项单测：调用分流三档开关 + API 密钥编辑/重置/高危确认。

全部走源码正则断言（与 CI 的 _test_*.py 同一套路，不依赖 fastapi）。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAIN = (ROOT / "gateway" / "main.py").read_text(encoding="utf-8")
PROXY = (ROOT / "gateway" / "web_proxy.py").read_text(encoding="utf-8")
APIKEYS = (ROOT / "gateway" / "api_keys.py").read_text(encoding="utf-8")

_ok = 0
_fail = []


def check(name, cond, detail=""):
    global _ok
    if cond:
        _ok += 1
        print(f"  ok   {name}")
    else:
        _fail.append(name)
        print(f"  FAIL {name} {detail}")


print("[1] 调用分流三档开关：后端")

check("pool config 默认含 callRouting=all",
      re.search(r'"callRouting": "all"', MAIN) is not None,
      "默认档位必须是 all（保持现有行为）")
check("_enabled_accounts 按 variant 过滤 cn/ai",
      re.search(r'routing == "cn".*?\n.*?variant.*?!= "ai"', MAIN, re.S) is not None
      and re.search(r'routing == "ai".*?\n.*?variant.*?== "ai"', MAIN, re.S) is not None)
check("分流只在自动轮询路径生效（显式指定 include_disabled=True 不过滤 variant）",
      re.search(r'if not include_disabled:\n        routing', MAIN) is not None)
check("PUT /account-pool/config 校验 callRouting 档位",
      'callRouting must be all, cn or ai' in MAIN)
check("未提交 callRouting 沿用原值（不被重置成默认）",
      re.search(r'callRouting.*current\.get\("callRouting"\)', MAIN, re.S) is not None)
check("_gateway_info 回传 callRouting 供前端显示",
      '"callRouting": config.get("callRouting") or "all"' in MAIN)
check("AI 独占模型的定向放行仍在分流过滤之前（不被分流截胡）",
      MAIN.index('_is_ai_exclusive_model') < MAIN.index('routing == "cn"'))

print("\n[2] 调用分流三档开关：前端")
check("设置页有三档按钮（全部/国内/国外）",
      '"全部账号"' in PROXY and '"国内账号"' in PROXY and '"国外账号"' in PROXY)
check("三档按钮走 PUT /api/account-pool 转发",
      'wbApiAction("/api/account-pool", "PUT", { callRouting' in PROXY)
check("当前档位高亮（wb-api-btn-on）",
      re.search(r'currentRouting.*?wb-api-btn-on', PROXY, re.S) is not None)

print("\n[3] API 密钥：编辑 + 重置 + 高危弹窗")
check("编辑按钮存在（改名 prompt）",
      'window.prompt("修改密钥名称' in PROXY)
check("重置按钮存在", '"重置统计"' in PROXY)
check("重置走 reset-stats 端点",
      '"/api/api-keys/reset-stats"' in PROXY)
check("重置前弹窗二次确认（含不可恢复提示）",
      re.search(r'重置密钥「.*?无法恢复', PROXY, re.S) is not None)
check("停用前也弹窗确认（原为直接停用）",
      re.search(r'停用密钥「.*?失去访问权限', PROXY, re.S) is not None)
check("删除前弹窗确认（不可恢复）",
      re.search(r'删除密钥「.*?不可恢复', PROXY, re.S) is not None)
check("web_proxy 转发 reset-stats",
      '@app.post("/api/api-keys/reset-stats")' in PROXY)
check("网关 reset-stats 端点存在",
      '@app.post("/api-keys/reset-stats")' in MAIN)
check("api_keys.reset_key_stats 只清计数不动密钥",
      'def reset_key_stats' in APIKEYS
      and re.search(r'def reset_key_stats.*?callCount.*?=.*?0.*?lastUsedAt.*?=.*?None.*?return _public',
                    APIKEYS, re.S) is not None
      and 'secrets.token_hex' not in APIKEYS.split('def reset_key_stats')[1].split('def ')[0])
check("密钥显示仍走 mask_key 脱敏（maskedKey）",
      'maskedKey' in APIKEYS and 'mask_key' in APIKEYS)

print("\n[4] 既有行为回归护栏")
check("默认 callRouting 缺失时按 all 处理（老配置文件无此字段）",
      'config.get("callRouting") or "all"' in MAIN)
check("编辑密钥不改 enabled（update_key 仍只认 name/enabled）",
      'for k in ("name", "enabled") if k in payload' in MAIN)
check("最后一个密钥防线仍在",
      '_guard_last_enabled_key' in MAIN)

print("\n" + "=" * 50)
print(f"通过 {_ok} 项，失败 {len(_fail)} 项" + ("：" + str(_fail) if _fail else " ✅"))
if _fail:
    raise SystemExit(1)

# ---- v1.0.6 追加：401 需重新登录（2026-10-20 工单，校准后最终版）----
print("\n[5] 401 需重新登录：后端判定与下发")
check("后端把 accounts.json 的 needs_relogin 并入判定（Mr.H 场景）",
      'src_acc.get("needs_relogin")' in PROXY)
check("后端下发 reloginReason",
      'item["reloginReason"]' in PROXY)
check("新补录账号也带 reloginReason",
      re.search(r'"needsRelogin": aid in needs_relogin,\s*\n\s*"reloginReason"', PROXY) is not None)
check("控制条保持原样（无整根替换横幅分支）",
      "wb-pool-relogin-btn" not in PROXY
      and 'bar.className = "wb-pool-relogin"' not in PROXY
      and ".wb-pool-relogin {" not in PROXY,
      "控制条应还原：不再有横幅分支/CSS/按钮")
check("无角落角标实现",
      "wb-pool-relogin-corner" not in PROXY)

print("\n" + "=" * 50)

# ---- v1.0.6 追加 3：官方积分区 401 原始错误文本替换（最终定义版）----
print("\n[7] 操作按钮上方的官方 401 代码块替换（哥哥三次澄清的最终定义）")
check("wbReplaceReloginErrorBlocks 已定义",
      "function wbReplaceReloginErrorBlocks()" in PROXY)
check("按 401 错误页特征识别（<html>+401 Authorization）",
      "401\\s*(Authorization\\s*Required)?" in PROXY.replace("\\\\s", "\\s") or
      re.search(r'401\\s\*\(Authorization\\s\*Required\)\?', PROXY) is not None or
      "Authorization Required" in PROXY)
check("整块替换（textContent 清空 + 重写）",
      re.search(r'wbReplaceReloginErrorBlocks[\s\S]{0,2400}div\.textContent = ""', PROXY) is not None)
check("错误原文从 title 属性一并移除",
      'div.removeAttribute("title")' in PROXY)
check("只处理需重登卡片（正常账号不动）",
      re.search(r'var relogin = wbCardNeedsRelogin\(card\);[\s\S]{0,1600}if \(!relogin\) return;', PROXY) is not None)
check("替换文案是「需要重新登录」",
      re.search(r'wbReplaceReloginErrorBlocks[\s\S]{0,2000}需要重新登录', PROXY) is not None)
check("挂在 run() 注入链上（injectAccountPool 之后）",
      re.search(r'injectAccountPool\(\);\s*\n\s*wbReplaceReloginErrorBlocks\(\);', PROXY) is not None)
check("替换块样式 wb-official-relogin（轻量不喧宾夺主）",
      ".wb-official-relogin {" in PROXY)
# ---- v1.0.6 四校（哥哥完整结构对比）：不霸屏 + badge 隐藏 + 灰化 ----
check("替换块对齐正常卡官方区高度（min-height:112px，实测 30+17+65）",
      re.search(r'\.wb-official-relogin \{[\s\S]{0,450}min-height: 112px', PROXY) is not None
      and re.search(r'\.wb-official-relogin \{[\s\S]{0,400}flex: 0 0 auto;[\s\S]{0,80}height: auto;', PROXY) is not None)
check("desc 不再带横幅的 flex-grow（霸屏根因）",
      "flex: 1 1 240px" not in re.search(r'\.wb-pool-relogin-desc \{[\s\S]{0,250}?\}', PROXY).group(0))
check("顶部外框官方 badge 隐藏（提示只在内框）",
      "wb-header-badge-hidden" in PROXY)
check("按钮区+模型区灰化禁用（wb-relogin-disabled）",
      ".wb-relogin-disabled" in PROXY
      and "wbApplyReloginDisabled" in PROXY and "wbApplyReloginEnabled" in PROXY)
check("灰化含 pointer-events:none（不可操作）",
      re.search(r'\.wb-relogin-disabled \{[\s\S]{0,200}pointer-events: none', PROXY) is not None)
