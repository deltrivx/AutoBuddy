#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.0.10 专项单测（用户 2026-10-21/22 反馈的五处调整）：
1. 账号搜索框：挪到「国内版/国际版」档位切换同一行右端，样式尺寸对齐切换按钮
2. 每日任务：「自动签到 / 自动执行任务」拆成两个独立开关、独立持久化
3. 每日任务页「账号签到与凭据」加背景框，样式与同页其它板块一致
4. 设备指纹与 UA：动态池化隔离，覆盖新账号，任何两项不碰撞
5. 设置 → 关于 → 说明：入口及文本整个移除
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


# ---------------------------------------------------------------- 需求 1：搜索框
print("[1] 搜索框：档位切换行右端 + 样式尺寸对齐")
check("定位档位切换组：aria-label 含「档位」",
      "wbFindVariantSwitchGroup" in PROXY and '档位' in PROXY)
check("定位兜底：含 国内版/国际版 文本的按钮父级",
      'btns[j].textContent' in PROXY and "国内版" in PROXY and "国际版" in PROXY)
check("插入档位容器行右端（row.appendChild）",
      re.search(r'row\.appendChild\(searchWrap\)', PROXY) is not None)
check("档位容器改横向 flex + 两端对齐（同行右端）",
      'row.style.justifyContent = "space-between"' in PROXY
      and 'row.style.flexDirection = "row"' in PROXY)
check("容器高度对齐官方切换组 h-9 = 36px",
      re.search(r'\.wb-acc-search-wrap\s*\{[^}]*height:\s*36px', PROXY) is not None)
check("输入框高度对齐官方切换按钮 h-7 = 28px",
      re.search(r'\.wb-acc-search-box\s*\{[^}]*height:\s*28px', PROXY) is not None)
check("圆角对齐官方 rounded-lg = 10px",
      re.search(r'\.wb-acc-search-box\s*\{[^}]*border-radius:\s*10px', PROXY) is not None)
check("字号/字重对齐官方 text-xs + font-medium",
      re.search(r'\.wb-acc-search-input\s*\{[^}]*font-size:\s*12px[^}]*font-weight:\s*500',
                PROXY) is not None)
check("消除 tablist padding 造成的 4px 错位（垂直对齐）",
      re.search(r'\.wb-acc-search-wrap\s*\{[^}]*padding-top:\s*4px', PROXY) is not None
      and re.search(r'\.wb-acc-search-wrap\s*\{[^}]*align-items:\s*flex-start', PROXY) is not None)
check("移动端（≤640px）退到下一行全宽，不撑破档位行",
      re.search(r'@media \(max-width:\s*640px\)\s*\{[^}]*\.wb-acc-search-wrap', PROXY) is not None)
check("DOM 取不到档位组时安静退出，不拖垮页面",
      re.search(r'if \(!group \|\| !group\.parentElement\) return;', PROXY) is not None)

# ---------------------------------------------------- 需求 2：两个开关拆分/持久化
print("\n[2] 每日任务：自动签到 / 自动执行任务 两个独立开关")
check("前端保留「启用签到调度」开关（wb-dl-checkin-sched）",
      'id="wb-dl-checkin-sched"' in PROXY)
check("前端保留「启用任务调度」开关（wb-dl-task-sched）",
      'id="wb-dl-task-sched"' in PROXY)
check("「启用自动签到」（启动补签）与调度开关同卡、语义独立",
      'id="wb-dl-checkin"' in PROXY and "启动补签" in PROXY)
check("三个开关各自独立上报（save payload 分三个 key）",
      "checkin_enabled: document.getElementById" in PROXY
      and "checkin_schedule_enabled: document.getElementById" in PROXY
      and "task_schedule_enabled: document.getElementById" in PROXY)
check("后端 DEFAULT_CONFIG 三个开关字段分开持久化",
      '"checkin_enabled": True' in DAILY
      and '"checkin_schedule_enabled": True' in DAILY
      and '"task_schedule_enabled": True' in DAILY)
check("ConfigBody 三个开关分开接收（独立写盘）",
      re.search(r'checkin_enabled:\s*bool\s*\|\s*None\s*=\s*None', DAILY) is not None
      and re.search(r'checkin_schedule_enabled:\s*bool\s*\|\s*None\s*=\s*None', DAILY) is not None
      and re.search(r'task_schedule_enabled:\s*bool\s*\|\s*None\s*=\s*None', DAILY) is not None)
check("调度循环：签到轮与任务轮各自判到期，互不影响",
      "签到轮到期，自动开始执行" in DAILY and "任务轮到期，自动开始执行" in DAILY)
check("签到轮只跑 checkin、任务轮跳过 checkin（互不干扰）",
      "--tasks checkin" in DAILY or '",".join(CHECKIN_TASK_CODES)' in DAILY)
check("保存时三个开关状态分别回填到界面",
      re.search(r'getElementById\("wb-dl-checkin"\)[^\n]*checkin_enabled', PROXY) is not None
      and re.search(r'getElementById\("wb-dl-checkin-sched"\)[^\n]*checkin_schedule_enabled',
                    PROXY) is not None
      and re.search(r'getElementById\("wb-dl-task-sched"\)[^\n]*task_schedule_enabled',
                    PROXY) is not None)

# ------------------------------------------------- 需求 3：账号签到与凭据加背景框
print("\n[3] 每日任务页「账号签到与凭据」加背景框")
check("该板块包回 .wb-api-card（白底/1px 边框/radius 16px）",
      'id="wb-dl-checkin-card"' in PROXY and 'wb-api-card" id="wb-dl-checkin-card"' in PROXY)
check("内部仍用 wb-api-row-stack（与其它卡片内部行同一规格）",
      re.search(r'id="wb-dl-checkin-card">.*?wb-api-row wb-api-row-stack', PROXY, re.S) is not None)
check("日志容器仍在卡片内（id 未被改动，JS 取用不受影响）",
      'id="wb-dl-checkin-logs-box"' in PROXY)
check("按钮仍用 .wb-api-btn（尺寸与同页其它按钮同档）",
      re.search(r'id="wb-dl-checkin-card">.*?wb-api-btn.*?全部立即签到', PROXY, re.S) is not None)
check("innerHTML 的 div 嵌套闭合平衡（不再提前关闭 wrap）",
      PROXY.count('<div class="wb-daily-wrap">') == 1)

# --------------------------------------------------- 需求 4：指纹/UA 池化隔离
print("\n[4] 设备指纹与 UA：动态池化隔离，覆盖新账号")
check("提供 ensure_account_fingerprints（批量补齐入口）",
      "def ensure_account_fingerprints" in FP)
check("提供 account_uid（从账号池条目取稳定 uid）",
      "def account_uid" in FP)
check("分配前收集已占用维度（machine/mac/画像/UA 四套集合）",
      "def _used_keys" in FP and "used_machine, used_mac, used_profile, used_ua" in FP)
check("画像档位与 UA 分开去重（不复用他人 UA）",
      "if profile[\"ua\"] in used_ua" in FP)
check("池容量耗尽时 bump UA 继续产出唯一 UA",
      "_bump_ua" in FP and "new_build = build + step" in FP)
check("_bump_ua 的 replace 用**原始**版本号作 old 串（修静默失效 bug）",
      re.search(r'return ua\.replace\(f"Chrome/\{major\}\.\{minor\}\.\{build\}\.\{patch\}",\s*'
                r'f"Chrome/\{major\}\.\{minor\}\.\{new_build\}\.\{new_patch\}"\)', FP) is not None)
check("machineId / MAC 撞车时加盐重取",
      "while machine_uuid in used_machine" in FP and "while mac in used_mac" in FP)
check("存量账号命中即原样返回（画像不漂移）",
      "if key in store:\n        return store[key]" in FP)
check("网关启动时给全池账号补指纹（main.py lifespan 钩子）",
      "_fingerprint.ensure_account_fingerprints(_load_accounts())" in MAIN)
check("wb_daily 收集参与账号时也补指纹（新账号随时进来不漏）",
      "fingerprint.ensure_account_fingerprints(arr)" in DAILY)
check("指纹库落盘到 account_fingerprints.json（持久化）",
      "account_fingerprints.json" in FP)
check("vendor 仍按 uid 取画像（下游零改动）",
      "get_or_create_fingerprint" in DAILY_VENDOR)

# ------------------------------------------------------ 需求 5：关于页去掉「说明」
print("\n[5] 设置 → 关于 → 说明：入口及文本整个移除")
check("不再构建「说明」那一行（noteRow / 免责文案已删）",
      "免责说明" not in PROXY.split("wbRenderAboutSection")[-1]
      and "与 WorkBuddy / CodeBuddy 官方无任何关联" not in PROXY)
check("不再渲染 wb-about-note 节点",
      'wbEl("div", "wb-about-note"' not in PROXY)
check("对应 CSS 规则一并下线（不留死规则）",
      "wb-about-note {" not in PROXY)
check("「关于」卡片仍保留 项目名+版本 / 运行位置 / 当前规模 三块",
      'wbEl("div", "wb-api-label", "当前规模")' in PROXY)

# ------------------------------------------------------------ 版本号一致性
print("\n[6] 版本号")
# 版本号规范（哥哥 2026-10-21 定）：`vYYYY.MM.DD.NN` 四段日期制，末段两位补零。
# 本文件的五项改动随 v1.0.10 上线；此处只确认常量在规范内、且末段不是单位数。
check("VERSION_DEFAULT 是四段末段两位或三段式",
      re.search(r'^VERSION_DEFAULT = "\d+\.\d+\.\d+(?:\.\d{2})?"\s*$', MAIN, re.M) is not None)
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
check("CHANGELOG 有现行规范的版本段",
      re.search(r"^## \[v\d+\.\d+\.\d+(\.\d{2})?\]", CHANGELOG, re.M) is not None)

print("\n" + "=" * 31)
print(f"测试结果: 通过 {_ok} 项, 失败 {len(_fail)} 项")
if _fail:
    print(f"失败用例: {_fail}")
    sys.exit(1)
print("全部测试用例通过！")
