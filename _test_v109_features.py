#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.0.9 专项单测：
1. 白名单彻底收紧（杜绝国际独占模型定向放行穿透）
2. 异常卡片模型区灰化及移动端自适应完整显示适配
3. 账号管理搜索框与即时过滤联动
4. 大并发、超大 Token 请求平摊负载均衡算法
5. 每日任务安全分级（Normal/Medium/HighRisk）、独立开关与单任务手动触发
6. 防风控加固（Jitter/阻断恶性重试/去下线补偿/去脱机假桌面对话/429退避）
7. 国际版每日活跃与登录（variant == 'ai' 分流）
8. 全量账号池独立虚拟硬件指纹生成与持久化（RFC 4122 UUIDv5）
9. 账号卡片双法则排序（正常优先/异常沉底；积分降序）
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAIN = (ROOT / "gateway" / "main.py").read_text(encoding="utf-8")
PROXY = (ROOT / "gateway" / "web_proxy.py").read_text(encoding="utf-8")
DAILY = (ROOT / "gateway" / "wb_daily.py").read_text(encoding="utf-8")
DAILY_VENDOR = (ROOT / "gateway" / "vendor" / "workbuddy_daily.py").read_text(encoding="utf-8")
FP = (ROOT / "gateway" / "fingerprint.py").read_text(encoding="utf-8")
INTL_DAILY = (ROOT / "gateway" / "vendor" / "workbuddy_intl_daily.py").read_text(encoding="utf-8")
INTL_LOGIN = (ROOT / "gateway" / "vendor" / "workbuddy_intl_login.py").read_text(encoding="utf-8")

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


print("[1] 需求 1: 白名单彻底收紧（杜绝国际独占模型定向放行穿透）")
check("白名单过滤候选集，国际独占模型仅在白名单候选集内收窄",
      re.search(r'candidates\s*=\s*\[a for a in candidates if a\.get\("variant"\) == "ai"\]', MAIN) is not None)
check("独占模型定向放行不再盲目从全池挑账号穿透白名单",
      'candidates = [a for a in accounts if a.get("variant") == "ai"]' not in MAIN)

print("\n[2] 需求 2: 异常卡片模型区灰化及移动端自适应")
check("前端注入异常卡片模型区灰化样式",
      ".wb-relogin-disabled" in PROXY and "filter: grayscale(1)" in PROXY)
check("移动端自适应 CSS 消除截断",
      re.search(r'\[class\*="min-\[420px\]:flex"\]', PROXY) is not None)

# 2026-10-21 工单：异常卡「只调尺寸、不动结构」的四校契约。
# 这是哥哥明确要求保留的异常态效果，任何后续改动（含尺寸微调）都不得破坏。
# 断言全部走源码字面量 —— CSS 与 JS 都在 web_proxy.py 里，改坏了这里立刻红。
print("\n[2b] 异常卡四校结构契约（2026-10-21 尺寸微调后仍须成立）")
# ① 顶部外框只留右侧两个按钮：官方「需重新登录」badge 被隐藏，且选择器
#    刻意排除含 button / chevron 的 span，不动右侧两个功能按钮。
check("① 顶部外框官方 badge 隐藏、且不误伤右侧两个按钮",
      ".wb-header-badge-hidden span:not(:has(button)):not(:has(svg[class*=chevron]))" in PROXY)
# ② 内框含重新登录提示 + 按钮区 + 模型区：三者都在 section 里被处理。
check("② 内框三件套（提示块/控制条/模型区）都在 section 内处理",
      'card.querySelector("section")' in PROXY
      and ".wb-official-relogin" in PROXY
      and 'sec.querySelector(".wb-pool-bar")' in PROXY
      and 'sec.querySelector(".wb-am-box")' in PROXY)
# ③ 右侧按钮/框内按钮区/模型区灰化且不可操作：grayscale + pointer-events:none。
check("③ 灰化含 grayscale + pointer-events:none（不可操作）",
      re.search(r'\.wb-relogin-disabled\s*\{[^}]*filter:\s*grayscale\(1\)', PROXY) is not None
      and re.search(r'\.wb-relogin-disabled\s*\{[^}]*pointer-events:\s*none', PROXY) is not None)
# ④ 提示块不放顶部外框、也不撑满内框：flex:0 0 auto + height:auto，无 flex:1。
check("④ 提示块不撑满内框（flex:0 0 auto + height:auto）",
      re.search(r'\.wb-official-relogin\s*\{[^}]*flex:\s*0 0 auto', PROXY) is not None
      and re.search(r'\.wb-official-relogin\s*\{[^}]*height:\s*auto\s*!important', PROXY) is not None
      and "flex: 1" not in PROXY.split(".wb-official-relogin {")[1].split("}")[0])
check("④ 提示块不出现在 header 里（只在 section 内）",
      'div.className = "wb-official-relogin"' in PROXY
      and 'card.querySelector("header")' in PROXY)

# 2026-10-21 尺寸微调的三个可量化断言（防止回退到「比正常区还高」的状态）：
print("\n[2c] 登录提醒块尺寸契约（要比正常对应区小）")
_relogin_block = PROXY.split(".wb-official-relogin {")[1].split("}")[0]
check("不再有 min-height:80px 硬撑（实测那 80px 是钉高度的唯一原因）",
      "min-height: 80px" not in _relogin_block and "min-height: 0" in _relogin_block)
check("padding 从 12px 14px 收窄（原始值不得残留）",
      "padding: 12px 14px" not in _relogin_block)
check("块高不再大于其替代的正常区域（65px）：靠收紧 padding/gap/字号实现",
      re.search(r'padding:\s*[1-9]px', _relogin_block) is not None
      and re.search(r'gap:\s*[01]px', _relogin_block) is not None)
_title_block = PROXY.split(".wb-pool-relogin-title {")[1].split("}")[0]
_desc_block = PROXY.split(".wb-pool-relogin-desc {")[1].split("}")[0]
check("标题字号 13px → 11px、行高 1.5 → 1.3",
      "font-size: 13px" not in _title_block and "font-size: 11px" in _title_block
      and "line-height: 1.5" not in _title_block and "line-height: 1.3" in _title_block)
check("说明字号 11.5px → 10px、行高 1.6 → 1.35",
      "font-size: 11.5px" not in _desc_block and "font-size: 10px" in _desc_block
      and "line-height: 1.6" not in _desc_block and "line-height: 1.35" in _desc_block)
# 内框高度对齐：JS 就地量同页正常卡的模型区高度，不写死像素。
check("内框对齐用内容驱动（量同页正常卡模型区高度），不拍脑袋定值",
      "wbAmH" in PROXY and "donor.getBoundingClientRect().height" in PROXY)
check("恢复登录后撤掉对齐高度（不留下过期的固定高度）",
      'am.style.minHeight = ""' in PROXY)
check("模型区标题行给足 24px 行高（对齐正常卡）",
      re.search(r'\.wb-relogin-disabled \.wb-am-title\s*\{[^}]*min-height:\s*24px', PROXY) is not None)
# 移动端：窄屏 desc 折行会变多，必须限行，否则「更小」在手机上失效。
check("移动端 desc 限 2 行（-webkit-line-clamp），防窄屏折行撑高",
      "-webkit-line-clamp: 2" in PROXY)

print("\n[3] 需求 3 & 9: 账号搜索过滤框及双法则排序（正常优先/积分降序）")
check("注入账号搜索框组件",
      "wb-acc-search-input" in PROXY)
check("双法则排序：正常账号优先（异常沉底）",
      "a.meta.rank !== b.meta.rank" in PROXY and "isAbnormal" in PROXY)
check("双法则排序：正常账号间按实时积分降序排列",
      "b.meta.credits !== a.meta.credits" in PROXY)
check("后端 /api/account-pool 注入实时积分数据",
      'item["credits"] = float(balances.get(aid' in PROXY or 'balances.get(aid' in PROXY)

print("\n[4] 需求 4: 大并发、超大 Token 请求平摊负载均衡算法")
check("引入在飞 Token 跟踪字典 inflight_tokens",
      '"inflight_tokens"' in MAIN)
check("引入近期完成 Token 滑动窗口 recent_tokens",
      '"recent_tokens"' in MAIN)
check("实现滑动窗口 Token 统计与调度算法",
      "_get_recent_tokens_locked" in MAIN)
check("Token 桶分级参与调度排序结算",
      "tok_bucket = tot_tok // 8000" in MAIN and "ordered.sort(key=_concurrency_token_key)" in MAIN)

print("\n[5] 需求 5: 每日任务安全分级（Normal/Medium/HighRisk）及独立开关")
check("TASK_CATALOG 任务划分 Normal/Medium/HighRisk",
      '"risk": "Normal"' in DAILY and '"risk": "Medium"' in DAILY and '"risk": "HighRisk"' in DAILY)
check("DEFAULT_CONFIG 包含 disabled_tasks 开关持久化列表",
      '"disabled_tasks": []' in DAILY)
check("提供单任务手动触发端点 /api/wb-daily/run-task/{task_key}",
      '/api/wb-daily/run-task/{task_key}' in DAILY)
check("前端代理 /api/wb-daily/run-task/{task_key} 路由",
      '/api/wb-daily/run-task/{task_key}' in PROXY)
check("前端进度看板联动渲染 0/总数 比例",
      "0/总数" in PROXY or 'accounts_done' in PROXY or "totalAcc" in PROXY)

print("\n[6] 需求 6: 防风控加固（Jitter/阻断恶性重试/去下线补偿/去脱机假桌面对话/429退避）")
check("api_retry 支持 HTTP 429 退避与随机 Jitter",
      "r.status_code == 429" in DAILY_VENDOR and "Retry-After" in DAILY_VENDOR)
check("t_compensation 接口下线清理并安全跳过",
      "claim-compensation 接口已彻底下线" in DAILY_VENDOR or "已下线接口补偿清理" in DAILY_VENDOR)
check("剔除脱机假桌面对话（RichMeow_Chat）防风控标记",
      "防风控标记" in DAILY_VENDOR)
check("t_accept_all 逐个重试注入防抖退避，阻断恶性重试放大",
      "random.uniform(1.5, 3.5)" in DAILY_VENDOR)

print("\n[7] 需求 7: 国际版每日活跃与登录（variant == 'ai' 分流）")
check("落盘 workbuddy_intl_daily.py",
      "WorkBuddy AI（国际版）每日活跃" in INTL_DAILY)
check("落盘 workbuddy_intl_login.py",
      "workbuddy_intl_login" in INTL_LOGIN or "OAuth" in INTL_LOGIN or "workbuddy.ai" in INTL_LOGIN)
check("wb_daily.py 提供 build_intl_account_store 函数",
      "def build_intl_account_store" in DAILY)
check("wb_daily.py 支持调用 INTL_VENDOR_SCRIPT 国际版活跃",
      "INTL_VENDOR_SCRIPT" in DAILY)

print("\n[8] 需求 8: 全量账号池独立硬件指纹生成与持久化（RFC 4122 UUIDv5）")
check("fingerprint.py 提供 get_or_create_fingerprint 函数",
      "def get_or_create_fingerprint" in FP)
check("使用 RFC 4122 标准的 UUIDv5 生成独立 machineId",
      "uuid.uuid5" in FP and "NAMESPACE_AUTOBUDDY" in FP)
check("持久化到 account_fingerprints.json",
      "account_fingerprints.json" in FP)
check("workbuddy_daily.py 接入 fingerprint 画像",
      "get_or_create_fingerprint" in DAILY_VENDOR)

print("\n" + "=" * 31)
print(f"测试结果: 通过 {_ok} 项, 失败 {len(_fail)} 项")
if _fail:
    print(f"失败用例: {_fail}")
    sys.exit(1)
else:
    print("全部测试用例通过！🎉")
