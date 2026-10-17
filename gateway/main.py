import os
import json
import asyncio
import logging
import re
import threading
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, List, Dict, Any

# 积分感知调度开关（2026-10-13）：1=按余额加权选号（默认），0=退回纯在飞数轮询。
# 与 credit_floor 的余额快照同源（credit_usage_snapshots.json，带 mtime 缓存）。
CREDIT_WEIGHT_SCHED = (os.getenv("AB_CREDIT_WEIGHT", "1") or "1").strip() not in ("0", "false", "no")
# 权重差封顶：高余额号与低余额号的权重比上限，防止单号被连续打爆、
# 低余额号彻底闲置（实测无封顶时 3:1 会让 A 吃满 30/30）。
_WEIGHT_CAP = float(os.getenv("AB_CREDIT_WEIGHT_CAP", "8") or 8)

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
import httpx

try:
    from gateway.token_tracker import (record_token_usage, get_aggregated_token_stats,
                                       UsageScanner, extract_usage)
except ImportError:
    from token_tracker import (record_token_usage, get_aggregated_token_stats,
                               UsageScanner, extract_usage)

try:
    from gateway import api_keys
except ImportError:
    import api_keys

try:
    from gateway import notify as _notify_mod
except ImportError:
    import notify as _notify_mod  # type: ignore[no-redef]

try:
    from gateway import cooldown
except ImportError:  # pragma: no cover - 取决于运行方式
    import cooldown  # type: ignore[no-redef]

try:
    from gateway import reqlog
except ImportError:  # pragma: no cover - 取决于运行方式
    import reqlog  # type: ignore[no-redef]

try:
    from gateway import session_sticky
except ImportError:  # pragma: no cover - 取决于运行方式
    import session_sticky  # type: ignore[no-redef]

try:
    from gateway import credit_floor
except ImportError:  # pragma: no cover - 取决于运行方式
    import credit_floor  # type: ignore[no-redef]

try:
    from gateway import pricing
except ImportError:  # pragma: no cover - 取决于运行方式
    import pricing  # type: ignore[no-redef]

try:
    from gateway import model_policy, model_health, account_policy
except ImportError:
    import model_policy
    import model_health
    import account_policy


# ---------------------------------------------------------------------------
# 版本号
#
# 只有一套号：GitHub Release 标签（同时也是镜像 tag）。发版时这里与 CHANGELOG
# 的版本段在同一次提交里一起改，`_test_version_sync.py` 在 CI 里把关，对不上
# 直接构建失败。
#
# 历史上这里是与发布号平行的另一套内部版本（1.x），于是面板显示 v1.8.0、
# 发布页写 v0.4.4 —— 同一份东西两个号，看的人根本没法判断自己跑的是不是最新。
# `AB_VERSION` 环境变量可覆盖（自建镜像 / fork 用得上）。
# ---------------------------------------------------------------------------
VERSION_DEFAULT = "0.9.94"
GATEWAY_VERSION = (os.getenv("AB_VERSION") or "").strip() or VERSION_DEFAULT


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """容器内的账号轮询与模型巡检随应用启停（替代已弃用的 on_event）。"""
    # 回填上一轮巡检摘要：运行状态只在内存里，进程一重启界面就会退化成
    # 「还没有执行过巡检」，而巡检间隔动辄以小时计 —— 用户看到的会是个空白面板。
    last = model_health.load_last_run()
    if last:
        _HEALTH_RUNTIME["lastResult"] = last
        _HEALTH_RUNTIME["lastRunAt"] = last.get("checkedAt")
    task = asyncio.create_task(account_rotate_loop())
    health_task = asyncio.create_task(model_health_loop())
    upstream_task = asyncio.create_task(upstream_models_loop())
    report_task = asyncio.create_task(daily_report_loop())
    try:
        yield
    finally:
        task.cancel()
        health_task.cancel()
        upstream_task.cancel()


app = FastAPI(title="AutoBuddy OpenAI Gateway", version=GATEWAY_VERSION, lifespan=lifespan)

DATA_DIR = Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy"))
ACCOUNT_POOL_FILE = DATA_DIR / "account_pool_config.json"
SELECTION_LOG_FILE = DATA_DIR / "selection_logs.json"
# 模型级禁用策略：某些账号的个别模型会因优惠政策调整 / 时效到期而不可调用，
# 需要在**账号 x 模型**这个粒度上拉黑，而不是停用整个账号。
MODEL_POLICY_FILE = DATA_DIR / "model_policy.json"
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://www.codebuddy.ai/v2")
CN_BASE_URL = os.getenv("CN_BASE_URL", "https://copilot.tencent.com/v2")

# 出站 UA：对齐官方桌面客户端形态（取自 vendor/workbuddy_daily.py 的 DESKTOP_UA）。
#
# 为什么要显式设：不设的话 httpx 会自带 ``python-httpx/<ver>``，一眼就是脚本客户端。
# 上游 400 code=11128 的提示语正是「可能不是官方客户端发出的，请用官方版本重新发送」。
#
# ⚠️ 判据强度要诚实：UA **不是**已证实的根因 —— 实测 11128 只出现在「尘星途」
# 一个账号（2 次），同期其他账号用同一个 python-httpx UA 全都成功。所以 11128
# 更像账号级渠道拦截；UA 这里只是顺手把请求形态对齐官方，消除一个明确嫌疑点。
UPSTREAM_UA = os.getenv(
    "AB_UPSTREAM_UA", "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1")


def _upstream_headers(token: str) -> Dict[str, str]:
    """构造转发到上游的请求头。

    三条转发路径（流式首试 / 流式换号重试 / 非流式换号重试）共用这一处，
    避免各写一遍、改的时候漏掉其中一条。
    """
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": UPSTREAM_UA,
    }

# 基础模型清单：官方别名与路由模式。这些不一定会出现在 usage 记录里，因此常驻。
# 注意：真实可用模型由 discover_models() 从官方 usage 数据自动补全，不要在这里逐个手工添加新模型。
BASE_MODELS = [
    # 混元
    {"id": "hy3", "name": "Hy3 (混元思考模型)", "owned_by": "tencent-codebuddy"},
    {"id": "hy4", "name": "Hy4 (别名 -> hy3)", "owned_by": "tencent-codebuddy"},
    # DeepSeek
    {"id": "deepseek-v3", "name": "DeepSeek-V3", "owned_by": "deepseek"},
    {"id": "deepseek-chat", "name": "DeepSeek-Chat (别名 -> deepseek-v3)", "owned_by": "deepseek"},
    # Kimi / 月之暗面
    {"id": "kimi-k3", "name": "Kimi-K3", "owned_by": "moonshot"},
    {"id": "kimi-k2.6", "name": "Kimi-K2.6", "owned_by": "moonshot"},
    {"id": "kimi-k2.5", "name": "Kimi-K2.5", "owned_by": "moonshot"},
    {"id": "kimi", "name": "Kimi (别名 -> kimi-k3)", "owned_by": "moonshot"},
    # GPT 系列
    {"id": "gpt-5.5", "name": "GPT-5.5 (OpenAI旗舰编码模型)", "owned_by": "openai"},
    {"id": "gpt-5.4", "name": "GPT-5.4", "owned_by": "openai"},
    {"id": "gpt-5.3-codex", "name": "GPT-5.3-Codex", "owned_by": "openai"},
    {"id": "gpt-5.6-sol", "name": "GPT-5.6-Sol (复杂推理与长程任务)", "owned_by": "openai"},
    {"id": "gpt-5.6-terra", "name": "GPT-5.6-Terra (速度与成本均衡)", "owned_by": "openai"},
    {"id": "gpt-5.6-luna", "name": "GPT-5.6-Luna (轻量极速)", "owned_by": "openai"},
    {"id": "gpt-4o", "name": "GPT-4o (别名 -> gpt-5.4)", "owned_by": "openai"},
    # Gemini 系列
    {"id": "gemini-3.1-pro", "name": "Gemini-3.1-Pro", "owned_by": "google"},
    {"id": "gemini-3.5-flash", "name": "Gemini-3.5-Flash", "owned_by": "google"},
    # GLM 系列
    {"id": "glm-5.3", "name": "GLM-5.3", "owned_by": "zhipu"},
    {"id": "glm-5.2", "name": "GLM-5.2", "owned_by": "zhipu"},
    # MiniMax 系列
    {"id": "minimax-m3", "name": "MiniMax-M3", "owned_by": "minimax"},
    # 官方模式别名
    {"id": "default-model", "name": "Auto (智能路由)", "owned_by": "tencent-codebuddy"},
    {"id": "fast-model", "name": "Fast (快速响应)", "owned_by": "tencent-codebuddy"},
    {"id": "balanced-model", "name": "Balanced (均衡日常)", "owned_by": "tencent-codebuddy"},
    {"id": "primary-model", "name": "Primary (高质量复杂任务)", "owned_by": "tencent-codebuddy"},
    {"id": "deep-model", "name": "Deep (深度推理分析)", "owned_by": "tencent-codebuddy"}
]

# 模型别名表由 model_policy 统一维护（禁用别名要连带挡住目标模型）。
MODEL_ALIAS_MAP = model_policy.MODEL_ALIAS_MAP

# ---------------------------------------------------------------------------
# 模型自动发现
# 官方上游没有 /models 端点（实测 404），模型清单是从账号的实际调用记录里聚合出来的。
# 因此这里直接读取官方 usage 缓存与网关自身的 token 统计，自动补全新出现的模型，
# 避免「官方上新一个模型就要手工往清单里加一个」。
# ---------------------------------------------------------------------------

_OWNER_PREFIXES = [
    ("deepseek", "deepseek"),
    ("gpt", "openai"),
    ("claude", "anthropic"),
    ("gemini", "google"),
    ("glm", "zhipu"),
    ("kimi", "moonshot"),
    ("minimax", "minimax"),
    ("codewise", "tencent-codebuddy"),
    ("hy", "tencent-codebuddy"),
]


def infer_owner(model_id: str) -> str:
    lowered = (model_id or "").lower()
    for prefix, owner in _OWNER_PREFIXES:
        if lowered.startswith(prefix):
            return owner
    return "workbuddy"


def _discover_from_official_usage() -> List[str]:
    """官方 usage 缓存 payload 中记录了账号实际调用过的模型（汇总/明细/按天三个维度）。"""
    cache_file = DATA_DIR / "official_usage_cache.json"
    if not cache_file.exists():
        return []
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            payload = (json.load(f) or {}).get("payload") or {}
    except Exception:
        return []

    names = set()
    for item in payload.get("models") or []:
        if isinstance(item, dict) and item.get("model"):
            names.add(str(item["model"]))
    for item in payload.get("requests") or []:
        if isinstance(item, dict) and item.get("model"):
            names.add(str(item["model"]))
    for day in payload.get("daily") or []:
        for item in (day or {}).get("models") or []:
            if isinstance(item, dict) and item.get("model"):
                names.add(str(item["model"]))
    return sorted(names)


def _discover_from_token_stats() -> List[str]:
    """网关自身记录的 token 统计里出现过的模型名。

    ⚠️ 2026-10-13 修复：这里原先读 ``token_stats_logs.json``，但该文件在
    v0.9.29 迁 SQLite（token_stats.db）时已删除 —— 函数每次都返回空列表。
    同时另一通道 ``official_usage_cache.json`` 自 9-24 起停更，
    于是模型发现双通道全断，新模型永远进不了 /v1/models
    （实测：账本里已出现 27+ 个模型，发现列表却停在 9 月）。
    改读 SQLite 的 usage_logs 表，去重取模型名。
    """
    db_file = DATA_DIR / "token_stats.db"
    if not db_file.exists():
        # 兼容回退：老部署可能仍有 JSON 账本
        log_file = DATA_DIR / "token_stats_logs.json"
        if not log_file.exists():
            return []
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                records = json.load(f)
            if not isinstance(records, list):
                return []
            return sorted({str(r["model"]) for r in records
                           if isinstance(r, dict) and r.get("model")})
        except Exception:
            return []
    try:
        import sqlite3
        conn = sqlite3.connect("file:%s?mode=ro" % db_file, uri=True, timeout=3.0)
        try:
            rows = conn.execute(
                "SELECT DISTINCT model FROM usage_logs WHERE model IS NOT NULL AND model != ''"
            ).fetchall()
        finally:
            conn.close()
        return sorted({str(r[0]) for r in rows})
    except Exception:
        return []


# /v3/config 动态模型拉取（2026-10-13，参照 workbuddy2api-panel 的 FetchModels）。
#
# 背景：用户反馈「官方已发布新模型但目录里看不到」。既有发现通道（usage 回溯
# + SQLite 账本）都是**事后发现**——只有账号真正调用过某模型才会进目录；
# 官方新发布的模型没人调过，就永远不出现。panel 的做法是主动拉官方
# **动态目录**：/v3/config（主，IDE UA 完整能力版）+ 企业端点补缺的并集。
#
# 这里实现 CN 侧主通道 /v3/config（口径对齐 panel 的 fetchV3ConfigModelMap）：
#   - UA 必须能解析出 CodeBuddy 版本号，否则上游 400（panel 实测）；
#   - data.models[] 为主，ProductFeaturesConfig.ModelTrialBanner.banners[]
#     的试用模型（modelId）也要补入——panel 实测 hy4-preview-f 只出现在横幅里。
#
# 结果写入 DATA_DIR / upstream_models.json，由 _discover_from_upstream() 读入
# 目录；每 6 小时后台刷新一次，失败保留旧文件（优雅退化）。
V3_CONFIG_URL = "https://copilot.tencent.com/v3/config"
V3_IDE_UA = "CodeBuddyIDE/4.12.0 CodeBuddy/4.12.0"
UPSTREAM_MODELS_FILE = DATA_DIR / "upstream_models.json"
_V3_REFRESH_SEC = int(os.getenv("AB_V3_REFRESH_HOURS", "6") or 6) * 3600


# 非对话模型的排除规则（对齐 panel 的 nonChatModel 过滤，口径：补全/跳转/图像/代理类
# 不应出现在 chat 目录）。panel 按 tags+maxTokens 判定；这里上游未透出 tags，
# 用实测清单的 id 模式近似：实测出现在 /v3/config 但非 chat 的有
# auto / codewise-completions / codewise-default-model-v2 / codewise-jump /
# codewise-rewrite / hunyuan-image-alpha / nes-gf / space-bunny。
_NON_CHAT_PREFIXES = ("codewise-",)
_NON_CHAT_IDS = {"auto", "hunyuan-image-alpha", "nes-gf", "space-bunny"}


def _parse_v3_models(payload: Any) -> List[str]:
    """从 /v3/config 响应提取模型 id 集合（data.models + 试用横幅，剔除非 chat）。"""
    names: set = set()
    data = (payload or {}).get("data") or {}
    for item in data.get("models") or []:
        if isinstance(item, dict) and item.get("id"):
            names.add(str(item["id"]))
    # 试用模型横幅：不在 data.models 里，只有 modelId / targetModelId
    pfc = data.get("ProductFeaturesConfig") or {}
    banners = ((pfc.get("ModelTrialBanner") or {}).get("banners")) or []
    for b in banners:
        if not isinstance(b, dict):
            continue
        for k in ("modelId", "targetModelId"):
            if b.get(k):
                names.add(str(b[k]))
    return sorted(x for x in names
                  if x not in _NON_CHAT_IDS
                  and not x.startswith(_NON_CHAT_PREFIXES))


def _fetch_upstream_models_once() -> List[str]:
    """单次拉取：用账号池里第一个可用账号的 AT 调 /v3/config。

    失败返回空列表（调用方保留旧文件）。token 只在内存与 https 请求里流转，
    不落日志。
    """
    try:
        import httpx
        acc = None
        for a in _load_accounts():
            if a.get("access_token"):
                acc = a
                break
        if acc is None:
            return []
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Authorization": "Bearer " + str(acc["access_token"]),
            "X-Domain": "copilot.tencent.com",
            "X-Product": "SaaS",
            "X-CodeBuddy-Request": "1",
            "User-Agent": V3_IDE_UA,
        }
        with httpx.Client(timeout=20.0, verify=False) as client:
            r = client.get(V3_CONFIG_URL, headers=headers)
        if r.status_code != 200:
            return []
        return _parse_v3_models(r.json())
    except Exception:
        return []


def _refresh_upstream_models() -> None:
    """拉取并落盘 upstream_models.json（mtime + 模型清单，供发现与签名共用）。

    同时做**模型目录变化检测**（用户 2026-10-13 需求）：与上一轮清单比对，
    新增/下线时推送 model_change 场景通知。
    首轮拉取（无旧清单）不算变化 —— 否则每次重启都会误报一次全量新增。
    """
    names = _fetch_upstream_models_once()
    if not names:
        return
    # 变化检测：先读旧清单（首次为 None，不算变化）
    old = None
    try:
        if UPSTREAM_MODELS_FILE.exists():
            old = _discover_from_upstream()
    except Exception:
        old = None
    try:
        _atomic = DATA_DIR / "upstream_models.json"
        _atomic.write_text(json.dumps({
            "updatedAt": int(time.time() * 1000),
            "models": names,
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    # 落盘后再比对（旧清单已在上方读取，不受本次写入影响）
    if old is None:
        return
    try:
        added = [m for m in names if m not in set(old)]
        removed = [m for m in old if m not in set(names)]
        if added or removed:
            _mi = _collect_models_info()
            _t, _lines = _notify_mod.build_model_change(added, removed, _mi)
            _r = _notify_mod.send_scene("model_change", _t, _lines)
            if _r.get("ok"):
                print("[models] 目录变化通知已发送: +%d -%d" % (len(added), len(removed)))
    except Exception as _e:
        lprint("models", f"change notice failed: {_e}")


def _discover_from_upstream() -> List[str]:
    """读 upstream_models.json（后台循环拉取的官方动态目录）。"""
    try:
        with open(UPSTREAM_MODELS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [str(x) for x in (data.get("models") or []) if x]
    except Exception:
        return []


async def upstream_models_loop():
    """后台循环：启动即拉一次（新模型分钟级可见），之后每 6 小时刷新。"""
    await asyncio.sleep(5)          # 等网关与账号池就绪
    while True:
        try:
            _refresh_upstream_models()
        except Exception:
            pass
        await asyncio.sleep(_V3_REFRESH_SEC)


# ---------------------------------------------------------------------------
# 每日凌晨报告（用户 2026-10-13 需求）：每天 00:05 推送昨日消耗、可用模型、
# 通知链路、各 API 当天/累计调用次数。
#
# 为何用「昨日」而非「今日」：凌晨 0 点推送时今天才刚开始，今日数据是 0，
# 报「昨日消耗」才有信息量（用户原话：「因为是凌晨所以报告前一天的」）。
# 幂等：同一天只发一次（进程重启不重发），记录在内存 + 落盘一个 sentinel。
# ---------------------------------------------------------------------------
_DAILY_REPORT_SENTINEL = DATA_DIR / "daily_report_last.json"


def _report_already_sent(today: str) -> bool:
    try:
        if _DAILY_REPORT_SENTINEL.exists():
            d = json.loads(_DAILY_REPORT_SENTINEL.read_text(encoding="utf-8"))
            return str(d.get("date") or "") == today
    except Exception:
        pass
    return False


def _mark_report_sent(today: str) -> None:
    try:
        _DAILY_REPORT_SENTINEL.write_text(
            json.dumps({"date": today, "at": int(time.time() * 1000)}), encoding="utf-8")
    except Exception:
        pass


def _collect_models_info() -> Dict[str, Any]:
    """收集模型口径数据（日报 / 巡检通知 / 模型变化通知共用）。

    返回 {total, autoDiscovered, disabled, manual}：
      - total / autoDiscovered：当前目录总数与自动发现数；
      - disabled：轮检 + 手动禁用的「账号×模型」组合数；
      - manual：其中手动禁用的组合数。
    任何一步失败都返回零值结构 —— 通知内容降级，绝不影响调用方。
    """
    info = {"total": 0, "autoDiscovered": 0, "disabled": 0, "manual": 0}
    try:
        cat = get_model_catalog()
        info["total"] = len(cat)
        info["autoDiscovered"] = len(cat) - len(BASE_MODELS)
        pol = model_policy.load_policy()
        accs = _enabled_accounts(_load_accounts(), _load_pool_config())
        d_cnt = m_cnt = 0
        for a in accs:
            aid = str(_account_id(a))
            for m in cat:
                mid = str(m["id"])
                if _model_is_disabled(aid, mid, pol):
                    d_cnt += 1
                    if (pol.get(aid) or {}).get(mid) == "manual":
                        m_cnt += 1
        info["disabled"] = d_cnt
        info["manual"] = m_cnt
    except Exception:
        pass
    return info


def _collect_daily_report() -> tuple:
    """组装日报内容。失败时返回占位内容。"""
    # 昨日积分（wb-daily 的 credit 汇总按天给）
    # ⚠️ 2026-10-16 修复（用户反馈「昨日消耗合计：3,000」是假数据）：
    # 原实现把 checkin + task（**获得**的积分）当成「消耗合计」报出去，
    # 于是「昨日签到 0 + 昨日任务 3000」被写成「昨日消耗 3000」——
    # 数字恰好一样，但语义完全相反（那是挣的，不是花的）。
    # 现在消耗改从**官方底层**取真实按日 usage：
    #   GET http://127.0.0.1:57890/api/credits/stats
    #     → accounts[].daily[] = [{date, usage}, ...]
    # 昨日本网消耗 = 所有账号 daily 中 date == 昨日 的 usage 之和
    # （实测 2026-10-15 为 8,590.68，与页面「官方积分消耗」图表同源）。
    y_checkin = y_task = y_total = 0.0
    y_used = None                      # None = 取不到（区别于真实的 0）
    y_date = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    try:
        import db as _db
        summ = _db.summarize_wb_daily_credits() or {}
        for row in (summ.get("days") or []):
            if str(row.get("date")) == y_date:
                y_checkin = float(row.get("checkin") or 0)
                y_task = float(row.get("task") or 0)
                y_total = float(row.get("total") or 0)
                break
        # ⚠️ 2026-10-17 修复（用户反馈「昨日签到积分：0」但实际有 2200）：
        # db.summarize_wb_daily_credits() 只从 wb_daily_tasks 的 detail 文本
        # 解析「+N积分」，而 checkin 行的 detail 恒为「✅签到: 今天已签到」
        # —— **不含任何数字**，所以签到奖励在这里恒为 0。
        # 页面上的签到积分是 HTTP 层 _merge_checkin_credits() 用账号签到状态
        # 里的 raw.daily_credit + raw.checkin_dates 补出来的。
        # 日报原先直接读 DB，绕过了那层合并，于是永远报 0。
        # 这里改为复用同一套合并逻辑，保证「页面看到的」与「日报报的」一致。
        try:
            from gateway import web_proxy as _wp
            summ = _wp._merge_checkin_credits(summ)
            for row in (summ.get("days") or []):
                if str(row.get("date")) == y_date:
                    y_checkin = float(row.get("checkin") or 0)
                    y_task = float(row.get("task") or 0)
                    y_total = float(row.get("total") or 0)
                    break
        except Exception as _e:
            print(f"[daily-report] 合并签到积分失败: {_e}")
        total_credits = float(summ.get("totalCredits") or 0)
    except Exception:
        total_credits = 0.0
    # 昨日真实消耗（官方底层 loopback，与页面图表同源；失败不影响日报其余部分）
    try:
        import urllib.request as _urq
        _raw = _urq.urlopen("http://127.0.0.1:57890/api/credits/stats", timeout=8).read()
        _cs = json.loads(_raw.decode("utf-8", "replace"))
        _acc = _cs.get("accounts") or []
        _sum = 0.0
        _hit = False
        for _a in _acc:
            for _row in (_a.get("daily") or []):
                if str(_row.get("date") or "") == y_date:
                    _sum += float(_row.get("usage") or 0)
                    _hit = True
        if _hit:
            y_used = _sum
    except Exception:
        pass
    # 账号池剩余（实时快照）
    remaining = 0.0
    try:
        bals = credit_floor._latest_balances() or {}
        remaining = sum(v for v in bals.values() if isinstance(v, (int, float)))
    except Exception:
        pass
    # API 调用（密钥计数 + 今日请求数）
    key_count = 0
    total_calls = 0
    try:
        keys = api_keys_status().get("keys") or []
        key_count = len(keys)
        total_calls = sum(int(k.get("callCount") or 0) for k in keys)
    except Exception:
        pass
    today_calls = 0
    try:
        from gateway import token_tracker as _tt
        agg = _tt.get_aggregated_token_stats() or {}
        # 实测结构：sources[].daily[]（{key, records, ...}），records 才是请求数
        # ⚠️ 2026-10-17 修复（用户反馈「昨日调用：0」）：
        # 1) 日报在 00:05 发送，此刻「今天」才过去 5 分钟，按今天统计必然接近 0。
        #    改按**昨日**统计，与积分部分同一口径。
        # 2) 不能把所有 source 的 records 直接相加 —— 实测 workbuddy 与
        #    workbuddy-ai 扫的是**同一批文件**（filesScanned 20434 vs 21034、
        #    projects / models 完全一致），daily 也几乎逐日相同，
        #    只是后者多含 9-22/9-23 两天。直接相加会把同一天的调用数翻倍
        #    （10-16 报成 7382，实际 3691）。
        #    取**每个日期在所有 source 中的最大值**：同一批文件的重复来源
        # #    取 max 即为真实值；真有两个独立来源时 max 也不会把小的漏掉。
        per_day: dict[str, int] = {}
        for _s in (agg.get("sources") or []):
            for _row in (_s.get("daily") or []):
                _k = str(_row.get("key") or "")
                if not _k:
                    continue
                _v = int(_row.get("records") or 0)
                if _v > per_day.get(_k, 0):
                    per_day[_k] = _v
        today_calls = per_day.get(y_date, 0)
    except Exception:
        pass
    # 模型信息（日报 / 巡检通知 / 模型变化通知共用一份口径）
    models_info = _collect_models_info()
    # 通知链路状态（哪些渠道就绪）
    link_state = "未配置"
    try:
        v = _notify_mod.public_view()
        ready = [c["name"] for c in (_notify_mod.CHANNELS or [])
                 if (v.get("channels", {}).get(c["key"], {}) or {}).get("_ready")]
        link_state = "、".join(ready) if ready else "未配置"
    except Exception:
        pass
    title, lines = _notify_mod.build_daily_report(
        {"date": y_date, "checkin": y_checkin, "task": y_task, "total": y_total,
         "used": y_used, "totalCredits": total_credits, "remaining": remaining},
        {"keyCount": key_count, "yesterdayCalls": today_calls, "totalCalls": total_calls},
        models_info)
    lines.append("")
    lines.append("🔔 通知链路：%s" % link_state)
    return title, lines


def _scene_err(r: Dict[str, Any]) -> str:
    """从 send_scene 返回值里提取**可读**的失败原因。

    `send_scene` 失败时顶层 `error` 常为 None（只有「总开关未开启」会填它），
    真实错误在每个渠道的 `results.<channel>.error` 里。
    原先直接打印顶层 error，日志就成了「日报发送失败: None」。
    """
    if not isinstance(r, dict):
        return str(r)[:120]
    errs = []
    for ch, res in (r.get("results") or {}).items():
        if isinstance(res, dict) and res.get("error"):
            errs.append("%s: %s" % (ch, str(res["error"])[:200]))
        elif isinstance(res, dict) and not res.get("ok"):
            errs.append("%s: 未知失败" % ch)
    if not errs:
        return str(r.get("error") or "未知原因")[:200]
    return " | ".join(errs)


async def daily_report_loop() -> None:
    """每天 00:05 推送一次日报（幂等，重启不重发）。"""
    print("[report] daily report loop started")
    while True:
        try:
            now = time.localtime()
            today = time.strftime("%Y-%m-%d", now)
            # 00:05 之后且今天没发过 → 发
            if (now.tm_hour, now.tm_min) >= (0, 5) and not _report_already_sent(today):
                title, lines = await asyncio.to_thread(_collect_daily_report)
                r = await asyncio.to_thread(_notify_mod.send_scene, "daily_report", title, lines)
                if r.get("ok"):
                    _mark_report_sent(today)
                    print("[report] 日报已发送: %s" % ",".join(r.get("sent_to") or []))
                elif "总开关未开启" not in str(r.get("error") or ""):
                    # 真实错误在 results 里（渠道级），顶层 error 往往是 None ——
                    # 2026-10-15 用户反馈「日志只写日报发送失败: None」，等于没写。
                    print("[report] 日报发送失败: %s" % _scene_err(r))
                else:
                    # 总开关没开：不标记 sent，等开启后下次循环补发
                    pass
            await asyncio.sleep(300)   # 5 分钟粒度足够（只用于跨过 00:05）
        except asyncio.CancelledError:
            raise
        except Exception as e:
            lprint("report", f"loop error: {e}")
            await asyncio.sleep(300)


_MODEL_CACHE: Dict[str, Any] = {"signature": None, "catalog": []}


def _catalog_signature() -> tuple:
    """以相关文件的 mtime + size 作为缓存签名，文件一变即重新发现。

    ⚠️ 2026-10-13 修复：token_stats_logs.json 已随 v0.9.29 迁移被删除，
    但签名仍盯著它（mtime 恒为 0）——即使发现函数修好，缓存也永不失效，
    新模型照样进不了目录。改盯 SQLite 账本（每笔请求都写入，天然变化）。"""
    sig = []
    for name in ("official_usage_cache.json", "token_stats.db", "upstream_models.json", "token_stats_logs.json"):
        try:
            st = (DATA_DIR / name).stat()
            sig.append((name, int(st.st_mtime), st.st_size))
        except Exception:
            sig.append((name, 0, 0))
    return tuple(sig)


def get_model_catalog() -> List[Dict[str, Any]]:
    """基础清单 ∪ 自动发现的模型，按 id 去重（基础清单优先，保留中文说明）。"""
    signature = _catalog_signature()
    if _MODEL_CACHE["signature"] == signature and _MODEL_CACHE["catalog"]:
        return _MODEL_CACHE["catalog"]

    catalog: Dict[str, Dict[str, Any]] = {m["id"]: dict(m) for m in BASE_MODELS}
    discovered = (set(_discover_from_official_usage()) | set(_discover_from_token_stats())
                  | set(_discover_from_upstream()))
    for name in discovered:
        if name in catalog:
            continue
        catalog[name] = {"id": name, "name": name, "owned_by": infer_owner(name)}

    result = list(catalog.values())
    _MODEL_CACHE["signature"] = signature
    _MODEL_CACHE["catalog"] = result
    return result


def get_active_account() -> Optional[Dict[str, Any]]:
    """返回轮询状态里的「当前账号」，仅用于健康状态与兼容旧接口。

    API 请求不再调用这里：请求会走 select_account()，按照账号池配置独立选账号，
    这样多个并发请求可以分配到不同账号，而不是全部卡在 activeAccountId。
    """
    accounts = _load_accounts()
    state_file = DATA_DIR / "rotate" / "state.json"
    active_id = None
    if state_file.exists():
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                active_id = (json.load(f) or {}).get("activeAccountId")
        except Exception:
            pass
    if active_id:
        for acc in accounts:
            if acc.get("id") == active_id or acc.get("uid") == active_id:
                return acc
    return accounts[0] if accounts else None


def _save_accounts(accounts: List[Dict[str, Any]]) -> None:
    """把账号列表写回账号池文件。

    写回 **官方那份**（``/data/.wb-switch/accounts.json``）：它是账号池的权威来源，
    官方前端与每日任务都直接读它。容器副本（``DATA_DIR/accounts.json``）随后从
    合并后的结果同步一份，保持两边一致 —— 反过来写（只写副本）会让官方前端
    看不到变更，正是之前「改了却不生效」的老路。

    写入采用「先写临时文件再原子替换」，避免中途中断把账号文件写坏。
    权限沿用既存文件的属主/权限，不因覆写而改变。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    primary = Path("/data/.wb-switch/accounts.json")
    targets = [primary] if primary.parent.exists() else []
    local = DATA_DIR / "accounts.json"
    if local not in targets:
        targets.append(local)

    payload = json.dumps(accounts, ensure_ascii=False, indent=2)
    for target in targets:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            stat = target.stat() if target.exists() else None
            tmp = target.with_suffix(target.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            if stat is not None:
                try:
                    os.chmod(tmp, stat.st_mode & 0o7777)
                    os.chown(tmp, stat.st_uid, stat.st_gid)
                except Exception:
                    pass
            os.replace(tmp, target)
        except Exception as e:
            print(f"[accounts] 写回 {target} 失败: {e}")


def _load_pool_config() -> Dict[str, Any]:
    """读取账号池配置；不存在时默认所有有 token 的账号启用、自动分配。"""
    default = {
        "mode": "auto",  # auto | manual
        "enabledAccountIds": [],  # 空数组表示所有账号启用
        "manualAccountId": None,
        "updatedAt": None,
    }
    try:
        if ACCOUNT_POOL_FILE.exists():
            with open(ACCOUNT_POOL_FILE, "r", encoding="utf-8") as f:
                value = json.load(f) or {}
            if isinstance(value, dict):
                default.update(value)
    except Exception as e:
        print(f"[account-pool] failed to load config: {e}")
    if default.get("mode") not in ("auto", "manual"):
        default["mode"] = "auto"
    if not isinstance(default.get("enabledAccountIds"), list):
        default["enabledAccountIds"] = []

    # 自愈与主动对齐：若当前已有可用账号列表，确保白名单和首选账号中的幽灵 ID 自动被剔除
    try:
        accounts = _load_accounts()
        known = {str(_account_id(a)) for a in accounts if _account_id(a)}
        if known:
            orig_enabled = default.get("enabledAccountIds") or []
            cleaned_enabled = [x for x in orig_enabled if str(x) in known]
            if len(cleaned_enabled) != len(orig_enabled):
                default["enabledAccountIds"] = cleaned_enabled
                _save_pool_config(default)
            if default.get("manualAccountId") and str(default["manualAccountId"]) not in known:
                default["manualAccountId"] = None
                default["mode"] = "auto"
                _save_pool_config(default)
    except Exception:
        pass

    return default


def _save_pool_config(config: Dict[str, Any]) -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    config["updatedAt"] = int(time.time() * 1000)
    with open(ACCOUNT_POOL_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    return config


# ---------------------------------------------------------------------------
# 模型级禁用策略（账号 x 模型）
#
# 需求来源（用户 2026-09-19）：账号提供了很多模型，但受优惠政策 / 时效性影响，
# 个别账号的个别模型可能调不通。停用整个账号损失太大，需要的粒度是
# 「这个账号的这一个模型不参与轮询」，其余模型照常。
#
# 策略的读写实现已抽到 model_policy 模块 —— 因为「模型可用性自动巡检」
# 也要读写同一份策略，两处各写一份早晚会漂移。这里只做转发。
# ---------------------------------------------------------------------------

def _load_model_policy() -> Dict[str, List[str]]:
    return model_policy.load_policy()


def _save_model_policy(policy: Dict[str, List[str]]) -> Dict[str, List[str]]:
    return model_policy.save_policy(policy)


def _disabled_models_for(account_id: Optional[str], policy: Optional[Dict[str, List[str]]] = None) -> set:
    return model_policy.disabled_models_for(account_id, policy)


def _model_is_disabled(account_id: Optional[str], model: Optional[str],
                       policy: Optional[Dict[str, List[str]]] = None) -> bool:
    return model_policy.model_is_disabled(account_id, model, policy)


def _account_id(acc: Dict[str, Any]) -> Optional[str]:
    return acc.get("id") or acc.get("uid")


def _account_label(acc: Dict[str, Any]) -> str:
    return acc.get("nickname") or acc.get("email") or acc.get("uid") or acc.get("id") or "未命名账号"


def _enabled_accounts(accounts: List[Dict[str, Any]], config: Dict[str, Any],
                      include_disabled: bool = False) -> List[Dict[str, Any]]:
    """账号池白名单 + 可用性过滤，可选是否叠加「账号级停用」。

    ``include_disabled`` 用于**显式指定**的路径：停用的语义是「不参与自动轮询」，
    而不是「禁止调用」—— 用户想临时用某个已停用的账号兜底时，
    不该逼他先去界面把停用解除掉。
    """
    enabled = {str(x) for x in config.get("enabledAccountIds") or []}
    # 空数组是默认值，语义是全部启用；保存明确列表后才是白名单。
    candidates = accounts if not enabled else [a for a in accounts if str(_account_id(a)) in enabled]
    # 再叠加账号级停用策略（人点的或巡检写的）。两份配置都只能**减少**参与调用的账号，
    # 谁都不许把账号偷偷启用回来 —— 于是「关掉巡检之后手动停用依然生效」是天然成立的，
    # 不需要在两者之间做任何迁移。
    if not include_disabled:
        disabled = account_policy.disabled_ids()
        if disabled:
            candidates = [a for a in candidates if str(_account_id(a)) not in disabled]
    now_ms = int(time.time() * 1000)
    return [a for a in candidates if _account_is_usable(a, now_ms)]


# ---------------------------------------------------------------------------
# 后台日志去重
#
# 需求来源（用户 2026-09-26）：后台日志如果与上一条完全相同，就不要再打一遍；
# 一直重复同一条会刷屏，只有当下一条不同日志出现时才显示。
# 实现：按「通道」记录上一条原文，相同则丢弃并累计重复次数；不同则输出，
# 若上一条曾被重复吞掉，先补一行 `(重复 N 次)` 交代清楚，不让观测断档。
# ---------------------------------------------------------------------------
_LOG_DEDUP_LOCK = threading.Lock()
_LOG_DEDUP_LAST: Dict[str, str] = {}
_LOG_DEDUP_COUNT: Dict[str, int] = {}


def lprint(channel: str, msg: str) -> None:
    """带去重的后台日志输出。

    ``channel`` 用于隔离不同来源的日志（互不干扰），``msg`` 是要输出的原文。
    与上一条完全相同则只累计计数、不输出；不同则输出，必要时补一行重复统计。
    """
    line = f"[{channel}] {msg}"
    with _LOG_DEDUP_LOCK:
        prev = _LOG_DEDUP_LAST.get(channel)
        if prev == line:
            _LOG_DEDUP_COUNT[channel] = _LOG_DEDUP_COUNT.get(channel, 0) + 1
            return
        repeat = _LOG_DEDUP_COUNT.get(channel, 0)
        _LOG_DEDUP_LAST[channel] = line
        _LOG_DEDUP_COUNT[channel] = 0
    if repeat > 0:
        # 重复次数跟在**本行末尾**（×N），而不是另起一行 ——
        # 另起一行会让日志里凭空多出一条与上一条几乎重复的记录，
        # 反而又变成刷屏（用户 2026-10-02 要求：改成行尾 ×N 提示）。
        print(f"{line} ×{repeat}", flush=True)
        return
    print(line, flush=True)


_ACCOUNT_POOL_RUNTIME: Dict[str, Any] = {
    "next_index": 0,
    "last_selected_id": None,
    "last_selected_source": None,
    # 每个账号的「在飞请求数」：并发请求优先分配给当前最闲的账号，
    # 而不是机械地按固定顺序轮询导致同一账号被连续命中。
    "inflight": {},
}


def _acquire_account_slot(account_id: str) -> None:
    """记一次在飞请求（进入上游调用前调用）。"""
    if not account_id:
        return
    with _SELECTION_LOCK:
        inflight = _ACCOUNT_POOL_RUNTIME.setdefault("inflight", {})
        inflight[str(account_id)] = int(inflight.get(str(account_id), 0)) + 1


def _release_account_slot(account_id: str) -> None:
    """释放一次在飞请求（上游调用结束，无论成败都要调）。"""
    if not account_id:
        return
    with _SELECTION_LOCK:
        inflight = _ACCOUNT_POOL_RUNTIME.setdefault("inflight", {})
        cur = int(inflight.get(str(account_id), 0))
        if cur <= 1:
            inflight.pop(str(account_id), None)
        else:
            inflight[str(account_id)] = cur - 1


def inflight_snapshot() -> Dict[str, int]:
    """当前各账号在飞请求数（供状态接口/前端展示并发分布）。"""
    with _SELECTION_LOCK:
        return {k: int(v) for k, v in (_ACCOUNT_POOL_RUNTIME.get("inflight") or {}).items()}

# 选账号流水：用于观测「并发请求到底分摊到了哪些账号」。
# 只保留最近 200 条，避免无界增长。
#
# v0.3.18 起**落盘**到 SELECTION_LOG_FILE。此前它只在进程内存里，容器一重建/重启，
# 账号卡片上的「已调用 N 次」就全部归零（用户 2026-09-19 反馈）。
# 注意它与 token_stats_logs.json 的区别只是**窗口大小**（200 vs 3000），
# 两者都是滑动窗口，都不是历史累计值 —— 落盘只是让它跨重启连续，不是变成总数。
_SELECTION_LOG: List[Dict[str, Any]] = []
_SELECTION_LOG_MAX = 200

# 同步路由跑在 FastAPI 的线程池里，next_index 的「读-改-写」不是原子操作。
# 不加锁时两个并发请求会读到同一个 index，双双落到同一账号 —— 表现为
# 「并发时其实只用了其中一个账号」。这里用锁把「取号 + 递增」串起来。
_SELECTION_LOCK = threading.Lock()


def _load_selection_log() -> List[Dict[str, Any]]:
    """从磁盘恢复选账号流水。文件不存在或损坏时返回空列表，绝不抛。"""
    try:
        if not SELECTION_LOG_FILE.exists():
            return []
        with open(SELECTION_LOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        entries = [e for e in data if isinstance(e, dict) and e.get("accountId")]
        return entries[-_SELECTION_LOG_MAX:]
    except Exception as e:
        print(f"[account-pool] failed to load selection log: {e}", flush=True)
        return []


def _save_selection_log_locked() -> None:
    """把当前流水整份写回磁盘。**必须在持有 _SELECTION_LOCK 时调用。**

    整份重写而不是追加：上限只有 200 条（约 20 KB），比追加更不容易写出半截 JSON。
    用 tmp + os.replace 做原子替换，进程被 kill 也不会留下坏文件。
    异常一律吞掉 —— 统计落盘失败绝不能影响正常的 API 请求。
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SELECTION_LOG_FILE.with_name(SELECTION_LOG_FILE.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_SELECTION_LOG, f, ensure_ascii=False)
        os.replace(tmp, SELECTION_LOG_FILE)
    except Exception as e:
        print(f"[account-pool] failed to persist selection log: {e}", flush=True)


_SELECTION_LOG = _load_selection_log()


def _remember_selection(acc: Dict[str, Any], source: str) -> Dict[str, Any]:
    """记录本次选中的账号与来源。

    三条路径（请求指定 / 手动固定 / 自动轮询）都要落记录，否则状态接口里
    lastSelectedAccountId 会停留在上一次自动分配的结果，无法反映真实调用账号。
    """
    account_id = _account_id(acc)
    _ACCOUNT_POOL_RUNTIME["last_selected_id"] = account_id
    _ACCOUNT_POOL_RUNTIME["last_selected_source"] = source

    entry = {
        "ts": int(time.time() * 1000),
        "accountId": account_id,
        "accountName": _account_label(acc),
        "source": source,
    }
    with _SELECTION_LOCK:
        _SELECTION_LOG.append(entry)
        del _SELECTION_LOG[:-_SELECTION_LOG_MAX]
        # 落盘放在锁内：保证「后发生的 append」一定写出更新的整份快照，
        # 不会出现两个线程的快照乱序覆盖。20 KB 的整份写，开销可忽略。
        _save_selection_log_locked()
    # 打日志便于 docker logs 直接核对并发分摊情况
    lprint("pool", f"{source:7s} -> {entry['accountName']} ({account_id})")
    return acc


def selection_stats(limit: int = 20) -> Dict[str, Any]:
    """汇总选账号流水：各账号命中次数 + 最近若干条明细。"""
    with _SELECTION_LOCK:
        entries = list(_SELECTION_LOG)
    counts = Counter(str(e.get("accountId")) for e in entries)
    names: Dict[str, str] = {}
    for e in entries:
        names.setdefault(str(e.get("accountId")), str(e.get("accountName")))
    return {
        "total": len(entries),
        "distinctAccounts": len(counts),
        "inflight": inflight_snapshot(),
        "counts": [
            {"accountId": aid, "accountName": names.get(aid), "count": c}
            for aid, c in counts.most_common()
        ],
        "recent": entries[-limit:],
    }


# 会话粘性 vs 积分调度的折衷（2026-10-13 实测修正）。
#
# 实测：selection_logs.json 里 97%（194/200）的选号走 sticky，配额制只服务
# 3% 的新会话 —— 等于被粘性彻底架空，余额-承接相关系数只有 0.032。
# 根因：session_sticky.get_bound 命中就在配额排序**之前** return。
#
# 但粘性本身有价值（同一对话复用账号，上下文/计费不混乱），不能简单去掉。
# 折衷：绑定账号的余额**明显掉队**（低于池内均值的一半）且池内存在更健康
# 的候选时，解绑改走配额 —— 让「积分多的多承接」真正生效，同时日常对话
# 仍保持粘性。
STICKY_YIELD_RATIO = float(os.getenv("AB_STICKY_YIELD_RATIO", "0.5") or 0.5)

# ---- 配额账本的时间衰减（2026-10-14 实测修正）----
#
# 实测问题：`credit_picks` 原是**纯累计计数器**，只增不减、重启才清零 ——
#   · 早期抢占的账号比值永久偏高，长期被压制；
#   · 重启清零又让比例失真，相关系数在 +0.032 ~ +0.083 之间反复摆动。
# 改成指数衰减后：配额只反映「近半个衰减周期」的公平性，
# 重启影响也显著变小（旧记忆自然淡出，不需要清零）。
CREDIT_PICK_HALFLIFE = float(os.getenv("AB_CREDIT_PICK_HALFLIFE", "1800") or 1800)

# sticky 配额仲裁（2026-10-14）：绑定账号的「有效已选 / 配额」比值劣于最优
# 候选超过这个倍数时，解除粘性改走配额。**必须配合 sticky 记账才生效** ——
# 不记账时吃满粘性流量的账号比值反而偏低，仲裁会做出反向判断。
STICKY_QUOTA_ARBITRATION = (os.getenv("AB_STICKY_QUOTA_ARB", "1") or "1").strip() not in ("0", "false", "no")
STICKY_QUOTA_FACTOR = float(os.getenv("AB_STICKY_QUOTA_FACTOR", "1.5") or 1.5)
# 让位的**绝对下限**（单位 = 一次有效承接）。
#
# 为什么必须有它（2026-10-14 自测发现）：只判「劣于最优 N 倍」时，最优候选
# 是 0 承接的新账号会让倍数阈值退化成 `> 0` —— 任何有记录的账号都被判让位，
# 于是每次请求都解绑去找「最近最少承接」的号，**会话连续性名存实亡**。
# 加上绝对下限后：小幅不平衡不打断会话，明显失衡（≥ MIN_GAP 次承接）才让位。
STICKY_QUOTA_MIN_GAP = float(os.getenv("AB_STICKY_QUOTA_MIN_GAP", "1.0") or 1.0)


def _credit_pick_effective_locked(aid: str, now: Optional[float] = None) -> float:
    """有效已选次数（指数衰减，半衰期 CREDIT_PICK_HALFLIFE）。

    **调用方必须已持有 _SELECTION_LOCK**（锁外请用 `_credit_pick_effective`）。
    """
    if not aid:
        return 0.0
    rec = (_ACCOUNT_POOL_RUNTIME.get("credit_picks") or {}).get(aid)
    if not isinstance(rec, dict):
        return 0.0
    n = float(rec.get("n") or 0.0)
    t = float(rec.get("t") or 0.0)
    if n <= 0 or t <= 0:
        return 0.0
    now = time.time() if now is None else float(now)
    elapsed = now - t
    if elapsed <= 0:
        return n
    return n * (0.5 ** (elapsed / max(CREDIT_PICK_HALFLIFE, 1.0)))


def _credit_pick_effective(aid: str, now: Optional[float] = None) -> float:
    """有效已选次数（带锁版本，供锁外调用，如粘性仲裁）。"""
    with _SELECTION_LOCK:
        return _credit_pick_effective_locked(aid, now)


def _record_credit_pick_locked(aid: str, now: Optional[float] = None) -> None:
    """记一次**实际承接**（sticky 与 auto 共用同一账本）。

    **调用方必须已持有 _SELECTION_LOCK**。语义：先把旧记忆衰减到当前时刻，
    再加 1，并以当前时刻为新的衰减基准。
    """
    if not aid:
        return
    now = time.time() if now is None else float(now)
    picks = _ACCOUNT_POOL_RUNTIME.setdefault("credit_picks", {})
    prev = _credit_pick_effective_locked(aid, now)
    picks[aid] = {"n": prev + 1.0, "t": now}


def _record_credit_pick(aid: str) -> None:
    """记一次实际承接（带锁版本）。"""
    if not aid:
        return
    with _SELECTION_LOCK:
        _record_credit_pick_locked(aid)


def _prune_credit_picks_locked(now: Optional[float] = None,
                               keep_hint: int = 0) -> None:
    """清掉衰减到可忽略的记录，防止账本随账号池增删无限增长。**须持有锁**。"""
    picks = _ACCOUNT_POOL_RUNTIME.get("credit_picks") or {}
    if len(picks) <= max(4 * max(int(keep_hint or 0), 1), 64):
        return
    now = time.time() if now is None else float(now)
    for k in [k for k in picks if _credit_pick_effective_locked(k, now) < 0.05]:
        picks.pop(k, None)



def _sticky_should_yield(bound_id: Optional[str], candidates: List[Dict[str, Any]]) -> bool:
    """绑定账号是否应该让位（True = 解绑走配额）。

    两个条件，任一命中即让位：

    1. **余额掉队**（2026-10-13）：绑定账号余额低于池内均值 × STICKY_YIELD_RATIO。
    2. **配额仲裁**（2026-10-14）：绑定账号的「有效已选 / 配额」比值劣于最优候选
       超过 STICKY_QUOTA_FACTOR 倍。配合 sticky 记账后，真正吃满流量的账号会被
       识别出来并让位 —— 否则 sticky 占 96%、配额算法只服务 4% 流量，
       离散度是理论值的 3.6 倍（实测 CV 1.25 vs 0.35）。

    关闭积分调度、候选不足 2 个、余额均未知时都不让位（避免误伤）。
    """
    if not bound_id or not candidates or len(candidates) < 2:
        return False
    if not CREDIT_WEIGHT_SCHED:
        return False
    try:
        bals = []
        for a in candidates:
            v = credit_floor.balance_of(_account_id(a))
            if isinstance(v, (int, float)) and v > 0:
                bals.append(float(v))
        if len(bals) < 2:
            return False
        avg = sum(bals) / len(bals)
        # 条件 1：余额明显掉队
        mine = credit_floor.balance_of(str(bound_id))
        if isinstance(mine, (int, float)) and mine > 0:
            if float(mine) < avg * STICKY_YIELD_RATIO:
                return True
        # 条件 2：配额仲裁
        if not STICKY_QUOTA_ARBITRATION:
            return False
        now = time.time()
        ratios = []
        for a in candidates:
            aid = str(_account_id(a))
            if not aid:
                continue
            v = credit_floor.balance_of(aid)
            w = max(float(v) / avg, 0.05) if isinstance(v, (int, float)) and v > 0 else 1.0
            ratios.append((_credit_pick_effective(aid, now) / min(w, _WEIGHT_CAP), aid))
        if len(ratios) < 2:
            return False
        best_ratio, best_aid = min(ratios, key=lambda x: x[0])
        if best_aid == str(bound_id):
            return False
        mine_ratio = None
        for r, aid in ratios:
            if aid == str(bound_id):
                mine_ratio = r
                break
        if mine_ratio is None:
            return False
        # 绝对下限：至少多出 MIN_GAP 次有效承接才值得打断会话连续性。
        # 用 <=（严格大于才让位）：差距恰好等于下限属于「一次请求的差」，
        # 不该打断会话。否则最优候选为 0 承接时倍数阈值退化成 >0，
        # 任何有记录的账号都让位，会话连续性名存实亡（2026-10-14 自测发现）。
        if (mine_ratio - best_ratio) <= STICKY_QUOTA_MIN_GAP:
            return False
        return mine_ratio > best_ratio * STICKY_QUOTA_FACTOR
    except Exception:
        return False


def select_account(requested_id: Optional[str] = None, model: Optional[str] = None,
                   session_key: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """为每一个 API 请求选择账号。

    - X-AutoBuddy-Account-Id / body.account_id 指定时：手动选指定账号；
    - manual 模式：固定使用配置的 manualAccountId；
    - auto 模式：在 enabledAccountIds 中轮询，每个并发请求可拿到不同账号。

    这里是「请求级并行」：一条对话请求仍由一个账号完成，但多个同时到达的
    独立请求会分摊到多个账号，不会把同一个请求拆成两份导致上下文/计费混乱。

    ``model`` 用于叠加**模型级禁用**：该账号若把本请求要用的模型拉黑了，就不再
    参与本次轮询，转而选择下一个账号。这样「某账号某模型不可用」不会让整个请求失败。

    **账号级停用**只影响自动轮询与手动模式，不影响显式指定 ——
    停用的语义是「轮询时不选它」，而不是「禁止调用」。同理，全部账号都被停用时
    自动轮询会回退到原始候选集，让上游返回真实错误，而不是在网关层编一个「无账号」。
    """
    accounts = _load_accounts()
    config = _load_pool_config()
    candidates = _enabled_accounts(accounts, config)
    # 「显式指定」与「手动模式」走**包含停用账号**的候选集：
    # 停用的语义是不参与自动轮询，而不是禁止调用。人明确点名要用它，
    # 就该照办 —— 否则用户为了临时兜底还得先去界面解一次停用。
    explicit_pool = _enabled_accounts(accounts, config, include_disabled=True)
    explicit_by_id = {str(_account_id(a)): a for a in explicit_pool if _account_id(a)}

    if model:
        policy = _load_model_policy()
        allowed = [a for a in candidates if not _model_is_disabled(_account_id(a), model, policy)]
        # 若所有账号都禁用了该模型，宁可回退到原始候选集也不直接失败 ——
        # 让上游去返回真实错误，比在网关层编一个「无账号」更利于排查。
        if allowed:
            candidates = allowed
        # 正向白名单（P1 能力矩阵）：探过「明确支持」的账号优先参与，
        # 但只有在**确实存在**支持者时才收窄，避免矩阵过旧导致全池被排除。
        #
        # ⚠️ 关键修正（用户 2026-09-29 反馈「新加的账号一次都没被调用」）：
        # 只排除**明确不支持**（state == "no"）的账号，
        # **未知**（state is None）必须保留 —— 否则新账号永远进不了候选。
        #
        # 原写法 `state == "yes"` 会把「从未探测过」的新账号一并排除，
        # 配合巡检的冷启动缺口（新账号无调用记录 → 只探基模型清单 →
        # 清单里没有自动发现的新模型如 hy4-preview-f → 永远探不到），
        # 形成死循环：
        #   没调用 → 不被探测 → 矩阵缺键 → 不被选中 → 没调用
        #
        # 实测（09-29）：4 个新 cn 账号 token 有效、确实在池内，
        # 但 selection_logs 里 0 次选中，而老账号 24~28 次。
        #
        # 改成排除 no 之后，未知账号保留参与；真不支持时会被
        # _learn_model_unavailable 即时学习成 no，下一轮自然出局。
        try:
            not_no = [a for a in candidates
                      if capability_state(_account_id(a), model) != "no"]
            if not_no:
                candidates = not_no
        except Exception:
            pass

    by_id = {str(_account_id(a)): a for a in candidates if _account_id(a)}

    if requested_id:
        # 模型级禁用仍然拦得住显式指定（那是「这个账号这个模型不可用」的硬约束），
        # 账号级停用拦不住（那只针对自动轮询）。
        acc = explicit_by_id.get(str(requested_id))
        if acc and model and _model_is_disabled(_account_id(acc), model, _load_model_policy()):
            return None
        return _remember_selection(acc, "request") if acc else None

    if config.get("mode") == "manual":
        acc = explicit_by_id.get(str(config.get("manualAccountId")))
        return _remember_selection(acc, "manual") if acc else None

    # 冷却过滤：被限流（429）/余额耗尽（402）的账号不参与自动轮询。
    # 与账号级停用一致 —— 全部都在冷却时回退原集合，
    # 宁可让上游返回真实错误，也不在网关层编一个「无账号」。
    try:
        cool_alive = cooldown.filter_cooling(candidates, _account_id)
        if cool_alive:
            candidates = cool_alive
    except Exception:
        pass

    # 积分保底：余额触底的账号不参与「收费模型」的选号。
    # 默认关闭（AB_CREDIT_FLOOR=0），开启后防止触底号继续挨打。
    try:
        floor_alive = credit_floor.filter_by_floor(candidates, _account_id, model)
        if floor_alive:
            candidates = floor_alive
    except Exception:
        pass

    # 会话粘性：该会话已绑过账号，且那个账号仍在候选集里 → 优先复用。
    # 这是**软偏好** —— 绑定的账号若已被冷却/停用/拉黑，自然不在 candidates 里，
    # 于是自动落回普通选号，绝不把坏账号硬塞给请求。
    if session_key and candidates:
        try:
            bound = session_sticky.get_bound(session_key)
            if bound:
                by_bound = {str(_account_id(a)): a for a in candidates if _account_id(a)}
                acc = by_bound.get(str(bound))
                if acc is not None:
                    # 粘性让位于积分（2026-10-13 实测：97% 走粘性，配额被架空）：
                    # 余额掉队 **或** 配额比值显著劣于最优候选时解绑，
                    # 让本轮落入下面的配额排序。
                    if _sticky_should_yield(str(bound), candidates):
                        # 解绑后落进配额排序；记账由配额那行统一负责，
                        # 不在这里记，避免一次选号出现两条流水。
                        session_sticky.unbind(session_key)
                    else:
                        # sticky 命中也记入配额账本（2026-10-14）：
                        # 此前 sticky **不记账**，导致吃到大量粘性流量的账号
                        # 比值反而偏低、更容易被 auto 选中，形成正反馈；
                        # 且 auto 只服务 4% 流量，样本严重不足。
                        _record_credit_pick(str(bound))
                        return _remember_selection(acc, "sticky")
        except Exception:
            pass

    # 全部账号都被账号级停用时，宁可回退到「包含停用账号」的集合也不返回 None：
    # 让上游去返回真实错误，比在网关层编一个「无账号」更利于排查
    # （与模型级全禁用的处理保持一致）。
    if not candidates:
        candidates = explicit_pool
    if not candidates:
        return None
    # 并发优先：选「在飞请求数最少」的账号。
    #
    # 此前是固定顺序的单指针轮询（next_index 递增），在**串行**请求下看起来没问题，
    # 但并发场景下同一个账号可能在别的请求还没返回时被反复命中——尤其是
    # 「候选集里只有少数账号支持某模型」时，慢请求会把同一个账号拖成瓶颈。
    # 改成按在飞数排序后：同分时沿用轮询起点保证均匀，整体上把并发真正摊开。
    with _SELECTION_LOCK:
        inflight = _ACCOUNT_POOL_RUNTIME.setdefault("inflight", {})
        base = int(_ACCOUNT_POOL_RUNTIME.get("next_index", 0))
        n = len(candidates)
        # 以固定起点做一次环形遍历，保证同分账号之间仍然是轮转的
        ordered = [candidates[(base + i) % n] for i in range(n)]
        # 积分感知调度（2026-10-13 用户需求）：**积分权重为主排序键**。
        #
        # 版本演进（实测数据说话）：
        #   - 初版：纯 inflight 最少连接，完全不感知余额；
        #   - v0.9.44：inflight / weight，实测相关性仅 0.077 —— 因为 inflight 是
        #     主导项、权重只在同 inflight 时起作用，且 session_sticky 在更早处
        #     已 return，多数请求走不到这里；
        #   - 本版：weight 降序为主键、inflight 升序为次级键。同分才看在飞数，
        #     「积分多的多承接」才真正成为主语义。
        #
        # 语义：
        #   - weight ∝ 余额（相对池均值归一，下限 0.05 防除零）；
        #   - 积分下降 → weight 自动衰减 → 让位给积分更高的账号，无需人工干预；
        #   - 未知余额（快照缺失/新账号）→ weight = 1（均值），中性不惩罚 ——
        #     否则新账号永远逆底，重蹚「新账号一次都没被调用」的老坑；
        #   - 并发兜底：同 weight 时选在飞数最少的，避免把高余额单号打爆。
        #
        # 例：账号 A 余额 3000、账号 B 余额 1000 → 稳态下 A 承接约 3 倍请求；
        # A 被打到只剩 500 时（B 仍 1000）→ B 反超成为首选。
        #
        # AB_CREDIT_WEIGHT=0 可关闭（退回纯 inflight 轮询）。
        try:
            _bal = {}
            if CREDIT_WEIGHT_SCHED:
                for a in candidates:
                    _v = credit_floor.balance_of(_account_id(a))
                    if isinstance(_v, (int, float)) and _v > 0:
                        _bal[str(_account_id(a))] = float(_v)
            if _bal:
                _avg = sum(_bal.values()) / len(_bal)
                def _weight(a):
                    aid = str(_account_id(a))
                    return max(_bal.get(aid, _avg) / _avg, 0.05)
                # 配额制（2026-10-13 二次修正）：纯权重排序在实测/模拟里会让
                # 高余额号吃满 100%（封顶也救不了 —— 主键相同时 inflight 都在
                # 0~1，稳定排序让同一个号一直赢）。改用「已选次数 / 配额比例」
                # 最小者优先：配额 ∝ 权重，天然按余额比例分摊，且**自纠**——
                # 某号选多了它的比值就升高、自动让位。
                #   例：A 余额 3000、B 1000 → 配额 3:1 → 稳态 A≈75%、B≈25%。
                _picked = _ACCOUNT_POOL_RUNTIME.setdefault("credit_picks", {})
                _pick_now = time.time()
                def _quota_key(a):
                    aid = str(_account_id(a))
                    quota = min(_weight(a), _WEIGHT_CAP)
                    return (_credit_pick_effective_locked(aid, _pick_now) / quota,
                            int(inflight.get(aid, 0)))
                ordered.sort(key=_quota_key)
                # 记账：选中即累加（比值随之升高，下次让位 —— 自纠的核心）。
                # 与 _quota_key 同在调度锁内，避免并发下漏计导致比例失真。
                # 带时间衰减（2026-10-14）：只保留近半个衰减周期的记忆，
                # 防止早期抢占永久压制后来者；sticky 命中也走同一账本。
                _record_credit_pick_locked(str(_account_id(ordered[0])), _pick_now)
                _prune_credit_picks_locked(_pick_now, n)
            else:
                ordered.sort(key=lambda a: int(inflight.get(str(_account_id(a)), 0)))
        except Exception:
            ordered.sort(key=lambda a: int(inflight.get(str(_account_id(a)), 0)))
        acc = ordered[0]
        # 把起点推进到「刚选中的那个账号的下一个」，保持长期均匀
        try:
            picked_pos = candidates.index(acc)
        except ValueError:
            picked_pos = base
        _ACCOUNT_POOL_RUNTIME["next_index"] = (picked_pos + 1) % n
    # 自动选号后登记粘性：下一次同会话请求会优先复用这个账号
    if session_key:
        try:
            session_sticky.bind(session_key, str(_account_id(acc)))
        except Exception:
            pass
    return _remember_selection(acc, "auto")


# ---------------------------------------------------------------------------
# 正向能力矩阵（账号 × 模型 支持情况）
#
# 与 model_policy 的「反向黑名单」互补：黑名单是事后学习（踩过才拉黑），
# 能力矩阵是正向记录（探过支持即可放心选中）。巡检每轮的 verdicts 都会写进来，
# 上游拒绝时也会即时记为「不支持」，两条路径共同保证选号时不撞墙。
#
# 存储：DATA_DIR/model_capability.json
#   {"updatedAt": ms, "accounts": {"<account_id>": {"<model>": "yes|no", "at": ms}}}
# ---------------------------------------------------------------------------
_CAPABILITY_FILE = DATA_DIR / "model_capability.json"
_CAPABILITY_LOCK = threading.RLock()
_CAPABILITY_CACHE: Dict[str, Any] = {"loadedAt": 0, "data": None}
_CAPABILITY_CACHE_TTL_MS = 5000


def _load_capability() -> Dict[str, Any]:
    """读取能力矩阵（带短暂缓存，避免每请求读盘）。"""
    now_ms = int(time.time() * 1000)
    with _CAPABILITY_LOCK:
        cached = _CAPABILITY_CACHE.get("data")
        if cached is not None and now_ms - int(_CAPABILITY_CACHE.get("loadedAt") or 0) < _CAPABILITY_CACHE_TTL_MS:
            return cached
        data: Dict[str, Any] = {"updatedAt": now_ms, "accounts": {}}
        try:
            if _CAPABILITY_FILE.exists():
                raw = json.loads(_CAPABILITY_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("accounts"), dict):
                    data["accounts"] = raw["accounts"]
                    data["updatedAt"] = raw.get("updatedAt") or now_ms
        except Exception as e:
            print(f"[capability] 读取失败: {e}")
        _CAPABILITY_CACHE["data"] = data
        _CAPABILITY_CACHE["loadedAt"] = now_ms
        return data


def record_capability(account_id: Optional[str], model: Optional[str], supported: bool) -> None:
    """记录「某账号支持/不支持某模型」。"""
    if not account_id or not model:
        return
    acc_key, model_key = str(account_id), str(model)
    with _CAPABILITY_LOCK:
        data = dict(_load_capability())
        accounts = dict(data.get("accounts") or {})
        entry = dict(accounts.get(acc_key) or {})
        now_ms = int(time.time() * 1000)
        state = "yes" if supported else "no"
        if entry.get(model_key) == state:
            entry[model_key + "__at"] = now_ms
        else:
            entry[model_key] = state
            entry[model_key + "__at"] = now_ms
        accounts[acc_key] = entry
        data["accounts"] = accounts
        data["updatedAt"] = now_ms
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _CAPABILITY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[capability] 写入失败: {e}")
        _CAPABILITY_CACHE["data"] = data
        _CAPABILITY_CACHE["loadedAt"] = now_ms


def capability_state(account_id: Optional[str], model: Optional[str]) -> Optional[str]:
    """查询能力状态："yes" / "no" / None（未知）。"""
    if not account_id or not model:
        return None
    data = _load_capability()
    entry = (data.get("accounts") or {}).get(str(account_id)) or {}
    state = entry.get(str(model))
    return state if state in ("yes", "no") else None


def capability_snapshot() -> Dict[str, Any]:
    """能力矩阵快照（供状态接口/前端展示）。"""
    data = _load_capability()
    accounts = data.get("accounts") or {}
    summary: Dict[str, Any] = {"updatedAt": data.get("updatedAt"), "accounts": {}}
    for aid, entry in accounts.items():
        yes = sorted(k for k, v in entry.items() if v == "yes" and not k.endswith("__at"))
        no = sorted(k for k, v in entry.items() if v == "no" and not k.endswith("__at"))
        summary["accounts"][aid] = {"supported": yes, "unsupported": no,
                                    "supportedCount": len(yes), "unsupportedCount": len(no)}
    return summary


# ---------------------------------------------------------------------------
# 上游「模型不可用」错误的识别、即时学习（auto 拉黑）与换号重试
#
# 需求来源（用户 2026-09-26）：如果某个模型只有部分账号支持，那么只有支持的账号
# 参与调用，不支持的不该被选中 —— 也就不该出现 model not found 之类的报错。
#
# 现状缺口：模型级禁用黑名单是「事后学习」的，只有巡检跑过或人拉黑过的组合才在册。
# 首次命中「账号 × 不支持该模型」时，网关会把上游错误直接透传给客户端。
#
# 本段补上：识别这类错误 → 立刻写入 auto 拉黑（下个请求起不再选中）→
# 换下一个账号重试（上限 = 候选账号数），全部失败才把真实错误返回。
# ---------------------------------------------------------------------------

# 上游表达「这个账号用不了这个模型」的错误码/文案。
# 11102 = 该模型仅对授权用户开放 / 服务信息不存在；其余为同类文案兜底。
_MODEL_UNAVAILABLE_CODES = {"11102", "11103"}
_MODEL_UNAVAILABLE_PATTERNS = (
    "service info not found",
    "is only available for authorized",
    "model not found",
    "model_not_found",
    "not support",
    "unsupported model",
    "no permission to use the model",
    "model is not available",
)


_UPSTREAM_ERR_LOG_MAX = 400


def _note_upstream_result(account_id: str, status_code: int, body_text: str,
                         rid: Optional[str] = None,
                         headers: Optional[Any] = None) -> None:
    """把一次上游结果记入冷却账本（成功清零、失败冷却）。

    挂在这里而不是散落各处：两条转发路径（流式 / 非流式）都要经过它，
    一处改动即可覆盖全部上游结果。

    为什么需要冷却（实测依据）：此前 429 是**直接透传**给客户端的，
    排查中发现 73 次 429 全部原样失败 —— 而池里有 17 个账号，
    一个号被限流，完全可以换一个；余额耗尽（402）更该立刻停用。
    没有冷却，被限流的账号仍在候选集里，每个请求都可能再撞一次。
    """
    if not account_id:
        return
    # 请求指标：顺带把这次请求记进 reqlog（rid 为空则跳过，不影响冷却记账）。
    # 放在签名里而不是调用点各自记 —— 一处覆盖两条转发路径，
    # 避免以后加路径时漏掉指标埋点。
    if rid:
        try:
            reqlog.done(rid, reqlog.OUTCOME_SUCCESS if status_code == 200
                        else reqlog.OUTCOME_HTTP_ERROR)
        except Exception:
            pass
    try:
        if status_code == 200:
            cooldown.record_success(account_id)
        else:
            # 上游若给了 Retry-After（秒数或 HTTP 日期），作为冷却时长依据 ——
            # 对齐 Azure Smart LB 的 Retry-After 语义：服务端比我们猜的准。
            retry_after = None
            if headers is not None:
                try:
                    retry_after = cooldown.parse_retry_after(
                        headers.get("retry-after") or headers.get("Retry-After"))
                except Exception:
                    retry_after = None
            cooldown.record_failure(account_id, status_code, body_text or "",
                                    retry_after=retry_after)
    except Exception:
        # 冷却记账失败绝不能连累转发主流程
        pass


def _log_upstream_error(status_code: int, body_text: str) -> None:
    """把上游非 200 的响应体记进日志（截断 + 抹掉易变字段）。

    为什么必须记：网关对非 200 此前**只记状态码**，于是同一个 429 无法区分
    「速率限流」（等一会儿就好）与「额度耗尽」（得充值或换号）——
    历史上额度耗尽正是 HTTP 429 + ``code=14018 Credits exhausted``。
    两者处理方式完全不同，没有响应体就只能靠猜。

    抹掉 ``requestId`` 是为了让相同的错误能被去重合并：
    每次请求 id 都不同，不抹掉就是一条一条刷屏。
    """
    if status_code == 200:
        return
    snippet = (body_text or "").strip()
    if not snippet:
        return
    # 去掉每次都不同的请求号，让同样的错误能折叠成 ×N
    snippet = re.sub(r'"requestId"\s*:\s*"[^"]*"', '"requestId":"*"', snippet)
    snippet = re.sub(r'"request_id"\s*:\s*"[^"]*"', '"request_id":"*"', snippet)
    snippet = re.sub(r'"traceId"\s*:\s*"[^"]*"', '"traceId":"*"', snippet)
    snippet = re.sub(r'\s+', ' ', snippet)[:_UPSTREAM_ERR_LOG_MAX]
    lprint("upstream", f"HTTP {status_code} {snippet}")


def _looks_like_model_unavailable(status_code: int, body_text: str) -> bool:
    """判断上游响应是否属于「该账号不支持该模型」（可换号重试）。

    只看**明确指向模型**的既有错误形状，不把限流/网络抖动/参数校验错误卷进来
    —— 那些换号也解决不了，重试只会放大故障。

    顺带在这里把非 200 的响应体记进日志：本函数是两条转发路径
    （流式 / 非流式）判定「要不要换号」的共同入口，挂在这里能覆盖
    每一次上游错误，不必在两个地方各写一遍。
    """
    _log_upstream_error(status_code, body_text)
    if status_code not in (400, 403, 404):
        return False
    raw = (body_text or "").strip()
    if not raw:
        return False
    code = None
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            code = str(payload.get("code")) if payload.get("code") is not None else None
            msg = str(payload.get("msg") or payload.get("message") or payload.get("error") or "")
        else:
            msg = raw
    except Exception:
        msg = raw
    if code and code in _MODEL_UNAVAILABLE_CODES:
        return True
    low = msg.lower()
    return any(p in low for p in _MODEL_UNAVAILABLE_PATTERNS)


def _learn_model_unavailable(account_id: Optional[str], model: Optional[str]) -> bool:
    """把这个「账号 × 模型」组合写入 auto 拉黑，返回是否真的新写入。

    写的是 model_policy 的 auto 来源项：与手动禁用同一份策略，
    巡检开启时才会被 auto-enable 放开（用户 2026-09-26 明确保留该语义）。
    """
    if not account_id or not model:
        return False
    try:
        policy = model_policy.load_policy()
        changed = model_policy.auto_disable(policy, str(account_id), str(model))
        # 同步写正向能力矩阵：记为「不支持」，选号时即刻排除，不必等下次巡检。
        record_capability(account_id, model, False)
        if changed:
            model_policy.save_policy(policy)
            lprint("model-learn", f"账号 {account_id} 不支持模型 {model}，已自动拉黑（换号重试）")
        return bool(changed)
    except Exception as e:
        print(f"[model-learn] 写入失败: {e}")
        return False


def _retry_candidates(model: Optional[str], exclude: set) -> List[Dict[str, Any]]:
    """换号重试的候选账号：在可用池里排除已试过的，并叠加模型级禁用过滤。"""
    accounts = _load_accounts()
    config = _load_pool_config()
    pool = _enabled_accounts(accounts, config)
    if not pool:
        pool = _enabled_accounts(accounts, config, include_disabled=True)
    if model:
        policy = _load_model_policy()
        filtered = [a for a in pool if not _model_is_disabled(_account_id(a), model, policy)]
        if filtered:
            pool = filtered
        try:
            # 同上：只排除「明确不支持」，保留未知账号（新账号冷启动）。
            not_no = [a for a in pool
                      if capability_state(_account_id(a), model) != "no"]
            if not_no:
                pool = not_no
        except Exception:
            pass
    return [a for a in pool if str(_account_id(a)) not in exclude]


_REFRESH_URLS = {
    "cn": os.getenv("CN_REFRESH_URL",
                     "https://copilot.tencent.com/v2/plugin/auth/token/refresh"),
    "ai": os.getenv("AI_REFRESH_URL",
                     "https://www.codebuddy.ai/v2/plugin/auth/token/refresh"),
}


def _refresh_one_token(refresh_token: str, variant: str) -> tuple:
    """用 RT 换新 AT，返回 ``(新AT, 新RT, 错误信息)``。

    端点与请求头取自上游插件实测用法：RT 放在 ``X-Refresh-Token``，
    并带 ``X-Auth-Refresh-Source: plugin``；成功标志是 ``code == 0``
    且 ``data.accessToken`` 存在。上游不保证返回新 RT（常与旧值同），
    所以拿不到就沿用旧值。
    """
    url = _REFRESH_URLS.get(variant) or _REFRESH_URLS["cn"]
    try:
        with httpx.Client(timeout=25.0, verify=False, trust_env=False) as client:
            r = client.post(url, json={}, headers={
                "X-Refresh-Token": refresh_token,
                "X-Auth-Refresh-Source": "plugin",
                "Content-Type": "application/json",
            })
            data = r.json()
    except Exception as e:
        return None, None, f"请求失败: {str(e)[:120]}"
    inner = (data or {}).get("data") or {}
    if (data or {}).get("code") == 0 and inner.get("accessToken"):
        return inner["accessToken"], (inner.get("refreshToken") or refresh_token), None
    return None, None, str((data or {}).get("msg") or "上游未返回新令牌")[:120]


def _jwt_exp_of(token: str) -> Optional[int]:
    """从 JWT 里取 exp（秒）。取不到返回 None，**不报错**。"""
    try:
        import base64
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None


@app.post("/refresh-token")
def refresh_token_api(payload: Dict[str, Any]):
    """刷新单个账号的令牌（账号卡片菜单的「刷新 Token」）。

    只动这一个账号：拿到新 AT/RT 后写回账号文件（官方那份 + 本地副本），
    并按 JWT 的 exp 同步 expiresAt —— 不刷新会让界面上仍显示旧到期时间，
    看起来像没生效。

    失败时把上游原话带回去，不编造原因：用户看到的一句话就是排查起点。
    """
    acc_id = payload.get("accountId") or payload.get("id")
    if not acc_id:
        raise HTTPException(status_code=400, detail="accountId is required")

    accounts = _load_accounts()
    target = None
    for a in accounts:
        if str(_account_id(a)) == str(acc_id):
            target = a
            break
    if target is None:
        raise HTTPException(status_code=404, detail="account not found")

    rt = (target.get("refresh_token") or "").strip()
    if not rt:
        raise HTTPException(status_code=400, detail="该账号没有刷新令牌，需要重新登录")

    variant = target.get("variant") or "ai"
    at, nrt, err = _refresh_one_token(rt, variant)
    if not at:
        return {"ok": False, "accountId": str(acc_id), "error": err or "刷新失败"}

    target["access_token"] = at
    target["refresh_token"] = nrt
    exp = _jwt_exp_of(at)
    if exp:
        target["expiresAt"] = int(exp * 1000)
    target["updatedAt"] = int(time.time() * 1000)

    # 写回完整列表（合并后的结果，不只是改动的那一条）：
    # 否则两份文件里那些「只在其中一份存在」的账号会被整份丢掉。
    _save_accounts(accounts)
    return {
        "ok": True,
        "accountId": str(acc_id),
        "expiresAt": target.get("expiresAt"),
        "refreshedAt": target["updatedAt"],
    }


@app.post("/delete")
def delete_account_api(payload: Dict[str, Any]):
    """删除账号（账号卡片菜单的「删除账号」）。

    同时清掉它的模型禁用策略与账号级停用记录 —— 不清会留下孤立条目，
    将来 ID 被复用时旧策略会意外生效。
    """
    acc_id = payload.get("accountId") or payload.get("id")
    if not acc_id:
        raise HTTPException(status_code=400, detail="accountId is required")

    accounts = _load_accounts()
    kept = [a for a in accounts if str(_account_id(a)) != str(acc_id)]
    if len(kept) == len(accounts):
        raise HTTPException(status_code=404, detail="account not found")

    _save_accounts(kept)

    cleanup_notes = []

    # 1. 彻底联动账号池配置（白名单 & 首选）：
    # 之前删除账号只删了 accounts.json，漏删了 account_pool_config.json 里的 enabledAccountIds，
    # 导致被删账号以“幽灵账号”残留在白名单里。一旦前端下次带着白名单提交，后端校验直接报 400 崩溃，
    # 导致新账号的「设为首选」和「停用/启用」完全失灵！
    try:
        pool_cfg = _load_pool_config()
        changed_pool = False
        enabled = pool_cfg.get("enabledAccountIds") or []
        if str(acc_id) in enabled:
            pool_cfg["enabledAccountIds"] = [x for x in enabled if str(x) != str(acc_id)]
            changed_pool = True
            cleanup_notes.append("账号池白名单")
        if str(pool_cfg.get("manualAccountId")) == str(acc_id):
            pool_cfg["manualAccountId"] = None
            pool_cfg["mode"] = "auto"
            changed_pool = True
            cleanup_notes.append("首选账号重置")
        if changed_pool:
            _save_pool_config(pool_cfg)
    except Exception as e:
        print(f"[delete] 清账号池配置失败: {e}")
    try:
        policy = _load_model_policy()
        if str(acc_id) in policy:
            policy.pop(str(acc_id), None)
            _save_model_policy(policy)
            cleanup_notes.append("模型禁用策略")
    except Exception as e:
        print(f"[delete] 清模型策略失败: {e}")
    try:
        from account_policy import (load_policy as _ld, save_policy as _sv)
        pol = _ld() or {}
        if str(acc_id) in pol:
            pol.pop(str(acc_id), None)
            _sv(pol)
            cleanup_notes.append("账号停用记录")
    except Exception as e:
        print(f"[delete] 清停用记录失败: {e}")

    return {"ok": True, "accountId": str(acc_id),
            "removed": 1, "remaining": len(kept),
            "cleaned": cleanup_notes}


@app.get("/export-accounts")
def export_accounts_api():
    """导出账号备份。

    默认**剔除刷新令牌等串行凭据**（exportSecrets=false）—— 备份常被丢到网盘、
    聊天工具里传，默认带上等于把账号拱手送人。需要完整迁移时显式传
    ``?exportSecrets=true``，界面会给出提示。
    """
    return {"ok": True, "accounts": _load_accounts(),
            "count": len(_load_accounts())}


@app.post("/import")
def import_accounts_api(payload: Dict[str, Any]):
    """导入账号备份（合并式，不覆盖同名之外的账号）。

    按 ID 去重：备份里已有的账号更新，没有的新增，**不删除**当前任何账号。
    导入是「恢复/迁移」而不是「替换」，把没写进备份的账号清掉是不可接受的。
    """
    incoming = payload.get("accounts")
    if not isinstance(incoming, list) or not incoming:
        raise HTTPException(status_code=400, detail="备份内容为空或格式不正确")

    current = {str(_account_id(a)): a for a in _load_accounts() if _account_id(a)}
    added, updated = 0, 0
    for acc in incoming:
        if not isinstance(acc, dict):
            continue
        key = str(_account_id(acc) or "")
        if not key:
            continue
        if key in current:
            current[key].update(acc)
            updated += 1
        else:
            current[key] = acc
            added += 1

    merged = list(current.values())
    _save_accounts(merged)
    return {"ok": True, "added": added, "updated": updated, "total": len(merged)}


@app.get("/account-pool/status")
def account_pool_status():
    accounts = _load_accounts()
    config = _load_pool_config()
    enabled_ids = {str(x) for x in config.get("enabledAccountIds") or []}
    now_ms = int(time.time() * 1000)
    stats = selection_stats(limit=10)
    # 账号级停用来源表。界面据此区分「谁停的」——人点的与巡检写的文案不同，
    # 否则「已停用」会被读成用户自己误操作过。
    account_disabled = account_policy.as_source_map()
    return {
        "mode": config.get("mode"),
        "manualAccountId": config.get("manualAccountId"),
        "allEnabledByDefault": not bool(enabled_ids),
        "enabledAccountIds": sorted(enabled_ids),
        "lastSelectedAccountId": _ACCOUNT_POOL_RUNTIME.get("last_selected_id"),
        "lastSelectedSource": _ACCOUNT_POOL_RUNTIME.get("last_selected_source"),
        "selectionCounts": stats["counts"],
        "modelPolicy": model_policy.as_model_lists(_load_model_policy()),
        "accountDisabled": account_disabled,
        "accounts": [
            {
                "id": _account_id(a),
                "name": _account_label(a),
                "variant": a.get("variant"),
                # 账号级停用（独立于账号池白名单），来源单独给出。
                "disabled": str(_account_id(a)) in account_disabled,
                "disabledSource": account_disabled.get(str(_account_id(a))),
                "enabled": not enabled_ids or str(_account_id(a)) in enabled_ids,
                "usable": _account_is_usable(a, now_ms),
                "active": _account_id(a) == (_rotate_state_snapshot().get("active_account_id") or ""),
            }
            for a in accounts
        ],
    }


@app.get("/account-pool/selections")
def account_pool_selections(limit: int = 50):
    """选账号分摊观测：各账号命中次数 + 最近明细。

    用于回答「并发请求是否真的分摊到了多个账号，而不是只用了其中一个」。
    """
    return selection_stats(limit=max(1, min(int(limit), _SELECTION_LOG_MAX)))


@app.post("/account-pool/selections/reset")
def reset_account_pool_selections():
    """清空选账号流水，便于做干净的压测观测。**同时清掉磁盘上的副本**，
    否则下次重启会把旧流水又读回来，看起来像「清零没生效」。"""
    with _SELECTION_LOCK:
        _SELECTION_LOG.clear()
        _save_selection_log_locked()
    return {"ok": True, "total": 0}


@app.put("/account-pool/config")
def update_account_pool(config: Dict[str, Any]):
    """更新账号池配置。

    **未提交的字段保持原值，而不是被重置成默认值。** 这一点是必须的：

    界面上「设为首选」只需要改 mode 与 manualAccountId，它提交的 payload 里
    并没有 enabledAccountIds。若这里用 ``config.get("enabledAccountIds", [])``
    取值，缺字段会被读成空数组并**覆盖落盘** —— 用户的白名单（哪些账号参与
    轮询）在一次「设为首选」后整份丢失。而空数组在业务上表示「全部启用」，
    所以现场看不出任何异常，只在后续排查时表现为「设置没生效」。

    因此这里以**已保存的配置为底**，只覆盖调用方明确提交的字段。
    """
    current = _load_pool_config()

    mode = config.get("mode", current.get("mode", "auto"))
    if mode not in ("auto", "manual"):
        raise HTTPException(status_code=400, detail="mode must be auto or manual")

    if "enabledAccountIds" in config:
        enabled = config.get("enabledAccountIds")
        if not isinstance(enabled, list):
            raise HTTPException(status_code=400,
                                detail="enabledAccountIds must be an array")
        enabled = [str(x) for x in enabled]
    else:
        # 没提交就沿用原值 —— 这是修复「设为首选把白名单清空」的关键一行。
        enabled = [str(x) for x in (current.get("enabledAccountIds") or [])]

    accounts = _load_accounts()
    known = {str(_account_id(a)) for a in accounts if _account_id(a)}

    # 幽灵账号治理：白名单里指向已删除账号的 id。
    # 彻底解决根源：delete_account_api 与 _load_pool_config 会自动联动剔除幽灵 ID。
    # 若前端或历史请求仍带有不在已知列表中的未知 ID，严格拒绝并抛出 400，防止脏数据入库。
    unknown = [x for x in enabled if x not in known]
    if unknown:
        raise HTTPException(status_code=400, detail={"unknownAccountIds": unknown})

    if "manualAccountId" in config:
        manual_id = config.get("manualAccountId")
    else:
        manual_id = current.get("manualAccountId")

    # 切到 manual 时必须指名一个**有效的**账号，否则请求会被路由到空处。
    # 未提交 manualAccountId 而当前也没有可用值时，退回 auto 而不是报错 ——
    # 「只想改白名单」的请求不该因为历史配置里没有首选账号而失败。
    if mode == "manual":
        pool = enabled or sorted(known)
        if str(manual_id) not in pool:
            manual_id = None
            mode = "auto"

    saved = _save_pool_config({
        "mode": mode,
        "enabledAccountIds": enabled,
        "manualAccountId": manual_id,
    })
    return {"ok": True, "config": saved, "status": account_pool_status()}


# ---------------------------------------------------------------------------
# 账号级停用：与「账号池白名单」正交的一份独立策略
#
# 为什么不复用账号池的 enabledAccountIds：
#   - 那份是**用户的配置**（保存整份列表），巡检不该去改它；
#   - 这里要记「是谁停的」（manual / auto），数组表达不了来源；
#   - 巡检自动停用需要能自愈，而配置里的白名单若被巡检删项，
#     用户根本看不出自己的配置被动过。
# 于是独立成文件，两份都只能**减少**参与调用的账号。

@app.post("/account-pool/toggle")
def toggle_account_disabled(payload: Dict[str, Any]):
    """停用 / 启用单个账号（人在界面上点的，来源记 manual）。

    停用的语义是「轮询时不选它」，**不是**禁止调用 ——
    显式指定这个账号的请求仍然会被接受（见 ``select_account``），
    这样用户想临时用它兜底时不必先去改配置。
    """
    acc_id = payload.get("accountId")
    if not acc_id:
        raise HTTPException(status_code=400, detail="accountId is required")
    accounts = _load_accounts()
    known = {str(_account_id(a)) for a in accounts if _account_id(a)}
    if str(acc_id) not in known:
        raise HTTPException(status_code=404, detail="account not found")
    disabled = bool(payload.get("disabled", True))
    policy = account_policy.load_policy()
    changed = account_policy.set_disabled(policy, str(acc_id), disabled,
                                          account_policy.SOURCE_MANUAL)
    if changed:
        account_policy.save_policy(policy)
    return {
        "ok": True,
        "changed": changed,
        "accountId": str(acc_id),
        "disabled": disabled,
        "source": account_policy.SOURCE_MANUAL if disabled else None,
        "status": account_pool_status(),
    }


@app.post("/account-health/probe")
def probe_account_credentials(payload: Dict[str, Any]):
    """就地检测一个账号的凭据是否有效（不消耗额度、不改配置）。

    只报告结论。要停用它由用户看着结果自己决定（旁边就是停用按钮）——
    探测结果与改配置分开，是为了不让人在「点了检测」之后发现配置被悄悄改了。
    """
    acc_id = payload.get("accountId")
    if not acc_id:
        raise HTTPException(status_code=400, detail="accountId is required")
    accounts = _load_accounts()
    by_id = {str(_account_id(a)): a for a in accounts if _account_id(a)}
    acc = by_id.get(str(acc_id))
    if not acc:
        raise HTTPException(status_code=404, detail="account not found")

    # 这是一个同步 HTTP 调用，但**不能**直接写在 def 路由里等它 ——
    # FastAPI 的 def 路由跑在线程池里，阻塞的是线程不是事件循环，
    # 所以这里直接同步执行即可（与 run_model_health_check 同一模式）。
    variant = acc.get("variant", "ai")
    base_url = _health_base_url(variant)
    token = acc.get("access_token")
    client = httpx.Client(timeout=30.0)
    try:
        result = model_health.probe_account(client, base_url, token)
    finally:
        try:
            client.close()
        except Exception:
            pass

    verdict = result.get("verdict")
    semantic = result.get("semantic")
    # 结论文案由 model_health 的统一语义表派生，接口这边不再自带一份副本 ——
    # 两份文案各自演化，正是「同一个账号在不同页面口径不同」的老问题来源。
    state = model_health.credential_state(result)
    # 判定依据：优先用语义表里针对**具体错误码**的解释（如 11140 → 内容审查），
    # 语义识别不出时才退回按 verdict 的通用说明。
    #
    # 但**「有效」这一档不给 evidence**：那一档的语义解释就是「凭据有效」，
    # 与 message 同文，拼上去只会得到「凭据有效（凭据有效）」这种绕口令
    # （线上实测踩到过）。有效档改用 method 说明探测手段，措辞也只讲
    # 「用一个不存在的模型名试，上游认下了凭据即有效」，不出现听起来像
    # 故障的「模型不存在」字样。
    evidence = None
    if state.get("state") != "valid":
        evidence = (model_health.SEMANTIC_LABELS.get(semantic or "")
                    or state.get("detail"))
    return {
        "ok": True,
        "accountId": str(acc_id),
        "accountName": _account_label(acc),
        # 保留 verdict 字段兼容旧前端，但含义以 state 为准（见下）。
        "verdict": verdict,
        # 统一的凭据状态：valid / invalid / restricted / unknown。
        # 界面只认这一个字段，不再自行解释 verdict 或状态码。
        "state": state.get("state"),
        "message": state.get("label") or "未识别的探测结论",
        # **界面优先显示这一条**：一句人话，不带任何探测细节。
        # 「账号正常，可以放心使用」/「账号被上游拦截，暂时用不了」——
        # 用户要的是「能不能用、为什么不能用」，不是我们怎么测的。
        # 「重新扫码没用」这类要件完整说明的结论不放这里，它归设置页。
        "userMessage": state.get("userMessage"),
        "evidence": evidence,
        # 探测手段说明（仅「有效」档有值）。**界面默认不展示** ——
        # 它是给排查用的实现细节。曾经把它拼进正常账号的提示里，
        # 结果用户读到的重点是「用一个不存在的模型名试」，像在报故障。
        "method": state.get("method"),
        # 行动建议只从 credential_state 取 —— 这份文案连同 state/label/detail
        # 都在同一个函数里生成，接口不再自己拼一套。
        # 以前就是这么散的：界面一处、接口一处、巡检一处，改一处漏两处，
        # 于是同一个账号在不同位置给出不同说法。
        "action": state.get("action"),
        # 上游错误码原样透出，供排查；但**不**透 HTTP 状态码 ——
        # 探测刻意用一个不存在的模型名，非 2xx 是预期内的，
        # 把状态码摆到界面上只会与「凭据有效」互相矛盾（这一点踩过一次）。
        "code": result.get("code"),
        "semantic": semantic,
        "elapsedMs": result.get("elapsedMs"),
        "error": result.get("error"),
    }


@app.get("/account-health/audit")
def audit_account_health():
    """对全部账号做一次一致性自检，逐账号给出「凭据」与「模型」两侧的结论。

    为什么需要它：凭据有效性与模型可用性是**两个独立维度**，
    但界面上过去只有一个笼统的「账号状态」，于是同一批账号在不同页面
    呈现出互相矛盾的结论（检测说有效、巡检说失效），让人无从判断该信哪个。
    这个接口把两侧结论并排摆出来，并标明每一侧各自的判据，
    使「凭据好但模型被拦」「某模型无权限」这类情况一眼可见。

    只读，不改任何配置 —— 排查工具不该有副作用。
    """
    accounts = _load_accounts()
    pool_cfg = _load_pool_config()
    enabled_ids = {str(x) for x in pool_cfg.get("enabledAccountIds") or []}
    acc_disabled = account_policy.load_policy()
    now_ms = int(time.time() * 1000)

    # 每个账号要测的模型：取该账号被显式禁用之外的、配置里声明过的模型。
    # 上限截断，避免一个接口调用打出几百次上游请求。
    try:
        base_models = _base_model_ids()
    except Exception:
        base_models = []
    max_models = 3

    rows: List[Dict[str, Any]] = []
    client = httpx.Client(timeout=30.0)
    try:
        for acc in accounts:
            acc_id = str(_account_id(acc))
            if not acc_id:
                continue
            variant = acc.get("variant", "ai")
            base_url = _health_base_url(variant)
            token = acc.get("access_token")
            entry = model_policy._coerce_entry(_load_model_policy().get(acc_id)) or {}
            disabled_models = set(entry)

            # 侧一：凭据
            probe = model_health.probe_account(client, base_url, token)
            cred = model_health.credential_state(probe)

            # 侧二：模型（最多 max_models 个未禁用的）
            candidates = [m for m in base_models
                          if m not in disabled_models
                          and model_policy.MODEL_ALIAS_MAP.get(m, m) not in disabled_models]
            model_rows = []
            for model in candidates[:max_models]:
                item = model_health.probe_once(client, base_url, token, model)
                model_rows.append({
                    "model": model,
                    "verdict": item.get("verdict"),
                    "semantic": item.get("semantic"),
                    "code": item.get("code"),
                    "explain": model_health.SEMANTIC_LABELS.get(
                        item.get("semantic") or "", ""
                    ) or {
                        "available": "调用链路正常",
                        "unavailable": "该模型对此账号不可用",
                        "transient": "临时异常，未能判定",
                    }.get(item.get("verdict"), ""),
                })

            # 一致性结论：把两侧摆在一起，直接给出「该怎么理解」。
            verdicts = {r["verdict"] for r in model_rows}
            if cred["state"] == "invalid":
                consistency = "凭据失效"
                advice = "先重新登录该账号；凭据修好之前，它名下模型的探测结果都不代表模型好坏。"
            elif cred["state"] == "restricted":
                consistency = "账号已被上游拦截"
                advice = ("登录是好的，但请求被上游直接拦下，所以用不了。"
                          "重新扫码登录没用（实测：换新凭据后依然被拦，"
                          "上游认的是账号本身），建议换个账号；巡检会自动停用它。")
            elif cred["state"] == "unknown":
                consistency = "凭据未判定"
                advice = "凭据探测未得到结论，模型侧的结论仅供参考。"
            elif model_rows and verdicts == {"available"}:
                consistency = "一致：均正常"
                advice = "凭据与模型两侧都正常，无需处理。"
            elif "restricted" in verdicts:
                consistency = "账号已被上游拦截"
                advice = "登录是好的，但模型请求被上游直接拦下，同上处理。"
            elif model_rows and "unavailable" in verdicts:
                consistency = "凭据有效，部分模型不可用"
                advice = "凭据没问题，是这些模型本身对该账号不可用（无权限 / 不存在），可单独禁用它们。"
            else:
                consistency = "结论不完整"
                advice = "本轮探测未覆盖到足以判定的组合，可稍后重试。"

            rows.append({
                "id": acc_id,
                "name": _account_label(acc),
                "variant": variant,
                "inPool": (not enabled_ids) or acc_id in enabled_ids,
                "usable": _account_is_usable(acc, now_ms),
                "accountDisabled": acc_id in acc_disabled,
                "accountDisabledSource": acc_disabled.get(acc_id),
                "credential": cred,
                "models": model_rows,
                "consistency": consistency,
                "advice": advice,
            })
    finally:
        try:
            client.close()
        except Exception:
            pass

    # 汇总：让调用方一眼看出「哪些账号需要关注」。
    summary: Dict[str, int] = {}
    for row in rows:
        summary[row["consistency"]] = summary.get(row["consistency"], 0) + 1
    return {
        "checkedAt": int(time.time() * 1000),
        "accounts": len(rows),
        "summary": summary,
        "criteria": {
            "credential": "用一个不存在的模型名探测：上游回「模型不存在」= 登录正常；"
                          "回鉴权类错误 = 登录过期；回 403/11140 = 账号被上游拦截"
                          "（登录是好的，但换新凭据也解不开）。",
            "model": "用真实模型名探测：只依据上游错误码判定，"
                     "内容审查与参数错误不会被算作模型不可用。",
            "note": "凭据有效性只由账号级探测裁定，不从模型探测结果反推。",
        },
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# 模型级禁用：账号 x 模型
#
# 与账号池配置是**正交**的两件事：
#   - 账号池决定「这个账号参不参与调用」；
#   - 模型策略决定「这个账号的哪些模型不参与调用」。
# 因此单独一个文件、单独一组接口，互不覆盖。
# ---------------------------------------------------------------------------

@app.get("/account-models/config")
def account_models_config():
    """返回模型禁用策略，供账号卡片渲染标签的禁用态。

    同时给出两种视图：
      - ``policy``：``{账号: [模型]}``，只管模型名，保持既有线格式不变；
      - ``policySources``：``{账号: {模型: "manual"|"auto"}}``，让卡片能区分
        「人手禁的」和「巡检禁的」—— 两者的恢复方式与含义并不相同。
    """
    policy = _load_model_policy()
    return {
        "ok": True,
        "policy": model_policy.as_model_lists(policy),
        "policySources": model_policy.as_source_map(policy),
        "disabledTotal": model_policy.disabled_total(policy),
        "disabledBySource": model_policy.count_by_source(policy),
    }


@app.put("/account-models/config")
def update_account_models_config(payload: Dict[str, Any]):
    """切换某账号某个模型的禁用状态。**这里的写入一律标记为手动（manual）。**

    入参两种写法，都支持：
      - 精确式：``{"accountId": "...", "model": "hy3", "disabled": true}``
      - 覆盖式：``{"accountId": "...", "models": ["hy3", "kimi-k3"]}``（整份替换该账号列表）

    ``disabled=False`` 且该模型不在列表里时是无副作用的空操作，直接返回成功，
    避免前端连点造成 4xx。

    写入时打上 ``manual`` 标记，从而使该条目免受巡检「自动启用」的放开 ——
    人点出来的禁用代表明确意图（常见于「能跑但太贵」），不该被自愈抹掉。
    """
    account_id = payload.get("accountId")
    if not account_id:
        raise HTTPException(status_code=400, detail="accountId is required")
    account_id = str(account_id)

    accounts = _load_accounts()
    known = {str(_account_id(a)) for a in accounts if _account_id(a)}
    if account_id not in known:
        raise HTTPException(status_code=400, detail={"unknownAccountId": account_id})

    policy = _load_model_policy()
    if "models" in payload:
        models = payload.get("models")
        if not isinstance(models, list):
            raise HTTPException(status_code=400, detail="models must be an array")
        current = model_policy.replace_account_models(
            policy, account_id, [str(m) for m in models if m],
            source=model_policy.SOURCE_MANUAL)
    else:
        model = payload.get("model")
        if not model:
            raise HTTPException(status_code=400, detail="model is required")
        current = model_policy.set_model_disabled(
            policy, account_id, str(model), bool(payload.get("disabled")),
            source=model_policy.SOURCE_MANUAL)

    _save_model_policy(policy)

    return {
        "ok": True,
        "accountId": account_id,
        "disabledModels": current,
        "disabledSources": model_policy.as_source_map(policy).get(account_id, {}),
        "disabledTotal": model_policy.disabled_total(policy),
    }


@app.post("/account-models/restore")
def restore_account_models(payload: Dict[str, Any]):
    """一键恢复某账号被禁用的模型。

    入参：``{"accountId": "...", "scope": "all" | "auto"}``

    - ``all``（默认）：**不分来源**全部恢复 —— 手动禁用的和巡检禁用的都放回。
      界面上的「全部恢复」按钮即此语义：用户的诉求是「让这些模型重新参与调用」，
      当初是谁禁的并不重要。
    - ``auto``：只恢复巡检写的那些，保留人工禁用的决策。用于「巡检误禁了一批，
      但别动我手动关掉的」。

    落盘前留底：恢复会改变路由行为，出问题时需要能对照恢复了哪些。
    """
    account_id = payload.get("accountId")
    if not account_id:
        raise HTTPException(status_code=400, detail="accountId is required")
    account_id = str(account_id)

    accounts = _load_accounts()
    known = {str(_account_id(a)) for a in accounts if _account_id(a)}
    if account_id not in known:
        raise HTTPException(status_code=400, detail={"unknownAccountId": account_id})

    scope = str(payload.get("scope") or "all")
    if scope not in ("all", "auto"):
        raise HTTPException(status_code=400, detail="scope must be all or auto")

    policy = _load_model_policy()
    sources = None if scope == "all" else [model_policy.SOURCE_AUTO]
    restored = model_policy.enable_all_models(policy, account_id, sources=sources)

    if restored:
        model_policy.backup_policy("before-restore")
        _save_model_policy(policy)

    return {
        "ok": True,
        "accountId": account_id,
        "scope": scope,
        # 本次恢复的模型名，界面据此回显「恢复了哪几个」。
        "restored": restored,
        "restoredCount": len(restored),
        # 恢复后该账号**仍然**被禁用的（scope=auto 时剩下的手动项）。
        # 统一成排序列表：同一字段在 PUT /account-models/config 里也是列表，
        # 一个返回 set() 一个返回 [] 会让前端要写两种判空 —— 接口契约该是一份。
        "disabledModels": sorted(model_policy.disabled_models_for(account_id, policy)),
        "disabledSources": model_policy.as_source_map(policy).get(account_id, {}),
        "disabledTotal": model_policy.disabled_total(policy),
    }


# ---------------------------------------------------------------------------
# 网关的两个核心能力之一是「对外提供 OpenAI 兼容 API」，因此「调用地址 + 密钥」
# 必须能在设置页直接看到、直接改。密钥校验是可选的（默认关闭），开启后 /v1/* 需要
# Authorization: Bearer <key> 或 x-api-key: <key>。
# ---------------------------------------------------------------------------


# 项目元信息。设置页的「关于」面板由此渲染 —— 放在环境变量里而不是写死在前端，
# 一是保持单一出处，二是 fork 出去的人可以改成自己的仓库，
# 不会把使用者引回上游作者的项目。
PROJECT_URL = os.getenv("AB_PROJECT_URL", "https://github.com/deltrivx/AutoBuddy")
PROJECT_NAME = os.getenv("AB_PROJECT_NAME", "AutoBuddy")

# 容器内的 WebUI 代理与网关同容器，靠 loopback 访问。容器外的请求经 docker NAT
# 进来源地址是网桥地址而非 127.0.0.1，所以放行 loopback 不会把外部请求放进来。
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


def _client_is_loopback(request: Request) -> bool:
    client = getattr(request, "client", None)
    host = str(getattr(client, "host", "") or "")
    if not host:
        return False
    if host in _LOOPBACK_HOSTS:
        return True
    # ::ffff:127.0.0.1 这类 IPv4-mapped 形式
    return host.startswith("127.") or host.endswith(":127.0.0.1")


def _extract_api_key(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key") or request.headers.get("api-key")


def _client_host(request: Request) -> str:
    client = getattr(request, "client", None)
    return str(getattr(client, "host", "") or "")


def _gateway_auth(request: Request) -> Dict[str, Any]:
    """统一的 /v1 访问校验。返回 ``{"ok": bool, ...}``，失败时由调用方转 401。

    判定顺序（自宽到严）：
      1. 未开启密钥校验     -> 直接放行；
      2. loopback（同容器内） -> 放行（WebUI 代理读 /v1/models 不能被打断）；
      3. 否则校验 Authorization / x-api-key。

    早先这里还有一步「IP 白名单免密钥」，已于 v0.9.34 移除 ——
    它把「要不要密钥」和「这个来源能不能用」混在一起，
    既难理解又难排查（用户 2026-10-07 反馈「方法做复杂了」）。
    """
    state = api_keys.get_state()
    if not state.get("requireKey"):
        return {"ok": True, "mode": "open", "requireKey": False}
    if _client_is_loopback(request):
        return {"ok": True, "mode": "internal", "requireKey": True}

    return api_keys.authenticate(_extract_api_key(request))


def _gateway_info() -> Dict[str, Any]:
    accounts = _load_accounts()
    now_ms = int(time.time() * 1000)
    catalog = get_model_catalog()
    config = _load_pool_config()
    keys = api_keys.get_config()
    enabled = _enabled_accounts(accounts, config)
    return {
        "version": GATEWAY_VERSION,
        "basePath": "/v1",
        "project": {
            "name": PROJECT_NAME,
            "url": PROJECT_URL,
            "releases": f"{PROJECT_URL}/releases",
            "changelog": f"{PROJECT_URL}/blob/main/CHANGELOG.md",
            "issues": f"{PROJECT_URL}/issues",
        },
        "dataDir": str(DATA_DIR),
        "ports": {"gateway": 18091, "console": 18090},
        "endpoints": {
            "chatCompletions": "/v1/chat/completions",
            "models": "/v1/models",
            "health": "/health",
        },
        "auth": {
            "requireKey": keys["requireKey"],
            "header": "Authorization: Bearer <key>",
            "altHeader": "x-api-key: <key>",
            "keysTotal": keys["stats"]["total"],
            "keysEnabled": keys["stats"]["enabled"],
        },
        "models": {
            "count": len(catalog),
            "autoDiscovered": len(catalog) - len(BASE_MODELS),
            "sample": [m["id"] for m in catalog[:12]],
            "disabledCombos": model_policy.disabled_total(),
        },
        "disabledBySource": model_policy.count_by_source(),
        "accounts": {
            "total": len(accounts),
            "usable": len([a for a in accounts if _account_is_usable(a, now_ms)]),
            "inPool": len(enabled),
            "mode": config.get("mode"),
        },
        "features": {
            "stream": True,
            "accountPinHeader": "X-AutoBuddy-Account-Id",
            "accountPinBody": "account_id",
            "aliasRouting": True,
        },
    }


@app.get("/gateway/info")
def gateway_info():
    """连接信息面板的数据源：地址、端点、模型/账号规模、密钥状态。"""
    return _gateway_info()


@app.get("/api-keys/status")
def api_keys_status():
    info = api_keys.get_config()
    info["gateway"] = _gateway_info()
    return info


@app.post("/api-keys")
def api_keys_create(payload: Optional[Dict[str, Any]] = None):
    record, error = api_keys.create_key((payload or {}).get("name"))
    if error:
        raise HTTPException(status_code=400, detail=error)
    return {"ok": True, "key": record, "status": api_keys_status()}


def _guard_last_enabled_key(key_id: str) -> None:
    """挡住「把最后一个可用密钥停用/删除」这种把自己锁在门外的操作。

    与「零密钥时不允许开启校验」是同一类防线：一旦开启校验又没有任何启用中的密钥，
    所有下游客户端立刻全部 401，而且只能进容器手改文件才能救回来。
    """
    if not api_keys.get_state().get("requireKey"):
        return
    others = [
        k for k in api_keys.get_config()["keys"]
        if k["enabled"] and str(k["id"]) != str(key_id)
    ]
    if not others:
        raise HTTPException(
            status_code=400,
            detail="这是最后一个启用中的密钥，且已开启密钥校验。"
                   "停用或删除会让所有下游客户端立即失去访问权限；请先关闭密钥校验，或先新建另一个密钥。")


@app.post("/api-keys/update")
def api_keys_update(payload: Dict[str, Any]):
    key_id = payload.get("id")
    if not key_id:
        raise HTTPException(status_code=400, detail="id is required")
    patch = {k: payload[k] for k in ("name", "enabled") if k in payload}
    if not patch:
        raise HTTPException(status_code=400, detail="nothing to update")
    if patch.get("enabled") is False:
        _guard_last_enabled_key(str(key_id))
    record, error = api_keys.update_key(str(key_id), patch)
    if error:
        raise HTTPException(status_code=404, detail=error)
    return {"ok": True, "key": record, "status": api_keys_status()}


@app.post("/api-keys/delete")
def api_keys_delete(payload: Dict[str, Any]):
    key_id = payload.get("id")
    if not key_id:
        raise HTTPException(status_code=400, detail="id is required")
    _guard_last_enabled_key(str(key_id))
    if not api_keys.delete_key(str(key_id)):
        raise HTTPException(status_code=404, detail="key not found")
    return {"ok": True, "status": api_keys_status()}


@app.post("/api-keys/delete-all")
def api_keys_delete_all():
    """一键清空。仅用于「重置成未设防状态」，前端会二次确认。

    开着密钥校验时不允许清空：清完就没人能调了。要重置请先关掉校验 —— 这里选择
    显式报错而不是「顺手帮你把校验关掉」，因为静默降低安全性比多一次点击更糟。
    """
    if api_keys.get_state().get("requireKey"):
        raise HTTPException(
            status_code=400,
            detail="已开启密钥校验，清空全部密钥会让所有下游客户端立即失去访问权限。请先关闭密钥校验。")
    removed = api_keys.delete_all_keys()
    return {"ok": True, "removed": removed, "status": api_keys_status()}


@app.put("/api-keys/config")
def api_keys_config(payload: Dict[str, Any]):
    if "requireKey" not in payload:
        raise HTTPException(status_code=400, detail="requireKey is required")
    require = bool(payload.get("requireKey"))
    if require and api_keys.get_config()["stats"]["enabled"] == 0:
        # 开了校验却一个可用密钥都没有 = 把自己锁在门外
        raise HTTPException(status_code=400,
                            detail="请先创建至少一个可用密钥，再开启密钥校验。")
    api_keys.set_require_key(require)
    return {"ok": True, "status": api_keys_status()}


@app.get("/health")
def health():
    acc = get_active_account()
    catalog = get_model_catalog()
    return {
        "status": "healthy",
        "version": GATEWAY_VERSION,
        "has_active_account": acc is not None,
        "active_account_variant": acc.get("variant") if acc else None,
        "models_count": len(catalog),
        "models_auto_discovered": len(catalog) - len(BASE_MODELS),
        "require_api_key": api_keys.get_state().get("requireKey"),
        "rotate": _rotate_state_snapshot()
    }

@app.get("/reqlog")
def reqlog_snapshot(limit: int = 50):
    """请求级指标快照（成功率 / 耗时分位 / 最近请求）。

    指标是**进程内内存态**，只有网关进程（18091）持有。WebUI 代理（18090）必须走 HTTP 转发拿到，不能在自己进程 import —— 那是另一个实例，永远是空。

    只归档元数据（模型 / 账号 / 耗时 / 状态 / 来源 IP 与 UA，受 AB_REQLOG_CLIENT 控制），不写提示词与凭证。
    """
    try:
        return reqlog.snapshot(limit=limit)
    except Exception as e:
        return {"error": str(e)[:200]}

# ---------------------------------------------------------------------------
# 企业微信回调（接收消息服务器）：公网入口由 nginx(autobuddy.deltrivx.com) → 本网关。
# GET = 启用验证（回显解密后的 echostr）；POST = 收消息（加密 XML → 回复加密 XML）。
# 配置在 notify_config.json 的 wecom.cb_* 字段；未配置时 GET 回 403、POST 回空。
# ---------------------------------------------------------------------------
@app.get("/api/notify/wecom/callback")
def wecom_callback_get(request: Request):
    try:
        echo = _notify_mod.wecom_callback_verify(dict(request.query_params))
        if echo is None:
            return Response(status_code=403)
        return Response(content=echo, media_type="text/plain")
    except Exception:
        return Response(status_code=403)


@app.post("/api/notify/wecom/callback")
async def wecom_callback_post(request: Request):
    try:
        body = (await request.body()).decode("utf-8", "replace")
        reply = _notify_mod.wecom_callback_message(dict(request.query_params), body)
        return Response(content=reply or "", media_type="application/xml")
    except Exception:
        return Response(content="", media_type="application/xml")


@app.get("/v1/models")
@app.get("/models")
async def list_models(request: Request):
    gate = _gateway_auth(request)
    if not gate["ok"]:
        raise HTTPException(status_code=401, detail=gate.get("detail") or "Unauthorized")
    if gate.get("keyId"):
        api_keys.mark_used(gate["keyId"])
    catalog = get_model_catalog()
    return {
        "object": "list",
        "data": [
            {
                "id": m["id"],
                "object": "model",
                "created": 1789680000,
                "owned_by": m["owned_by"],
                "permission": []
            }
            for m in catalog
        ]
    }

def normalize_messages_for_upstream(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not messages:
        return [{"role": "system", "content": "You are a helpful assistant."}]
    first = messages[0]
    if first.get("role") != "system":
        return [{"role": "system", "content": "You are a helpful assistant."}] + messages
    return messages

def estimate_tokens_from_chars(char_count: int) -> int:
    """按 3.5 字符 ≈ 1 token 的粗估。只在拿不到上游 usage 时兜底。"""
    if char_count <= 0:
        return 0
    return max(1, int(char_count / 3.5))


def estimate_tokens(text: str) -> int:
    return estimate_tokens_from_chars(len(text or ""))


def accumulate_delta(msg: Dict[str, Any], delta: Dict[str, Any]) -> None:
    """把一个流式 delta 增量合并进 assistant 消息。

    非流式路径要把上游的流式帧重组成一条完整 assistant message，
    这里负责把 delta 里的各类字段累加起来。

    为什么需要单独一个函数（而不是只挑 content）：
    上游 tool-calling 是把工具调用**切片**下发的 —— 
    ``tool_calls[0].function.arguments`` 每次只来几个字符，要拼起来才是完整 JSON。
    所以必须按 index 定位同一个 tool_call，再把 arguments 追加到它名下，
    而不是每次都 append 一条新的 tool_call（那样会变成一串残缺调用）。

    ``role`` 只在第一帧出现，之后各帧都没有，直接覆盖即可。
    """
    if not isinstance(delta, dict):
        return
    # role 只在首帧携带，后续帧没有该键，取到就写、取不到不动
    if delta.get("role"):
        msg["role"] = delta["role"]
    if delta.get("content") is not None:
        msg["content"] = (msg.get("content") or "") + (delta["content"] or "")
    for noise in ("reasoning_content", "reasoning"):
        if delta.get(noise) is not None:
            msg[noise] = (msg.get(noise) or "") + (delta[noise] or "")

    new_calls = delta.get("tool_calls")
    if not isinstance(new_calls, list) or not new_calls:
        return

    calls = msg.setdefault("tool_calls", [])
    for piece in new_calls:
        if not isinstance(piece, dict):
            continue
        # 没给 index 的按 0 处理（兼容不规范的 upstream）
        idx = piece.get("index")
        if not isinstance(idx, int):
            idx = 0
        while len(calls) <= idx:
            calls.append({"id": "", "type": "function",
                          "function": {"name": "", "arguments": ""}})
        slot = calls[idx]
        if piece.get("id"):
            slot["id"] = piece["id"]
        if piece.get("type"):
            slot["type"] = piece["type"]
        fn_delta = piece.get("function")
        if isinstance(fn_delta, dict):
            fn = slot.setdefault("function", {"name": "", "arguments": ""})
            if fn_delta.get("name"):
                fn["name"] = fn_delta["name"]
            if fn_delta.get("arguments") is not None:
                # 这里是**拼接**而非覆盖：arguments 是逐片来的 JSON 片段
                fn["arguments"] = (fn.get("arguments") or "") + (fn_delta["arguments"] or "")

@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request):
    start_time = time.time()

    # 访问密钥校验（默认关闭）。开启后 /v1/* 需携带 Authorization: Bearer <key>
    # 或 x-api-key: <key>；容器内 WebUI 代理走 loopback 免校验，不会被自己的密钥挡住。
    gate = _gateway_auth(request)
    if not gate["ok"]:
        raise HTTPException(status_code=401, detail=gate.get("detail") or "Unauthorized")
    if gate.get("keyId"):
        api_keys.mark_used(gate["keyId"])

    body = await request.json()

    # 请求级账号选择：显式 header/body 优先，其次按 account-pool 配置自动分配。
    # 这让多个并发请求可以并行落到多个账号，而不再全部使用 activeAccountId。
    requested_account_id = (
        request.headers.get("x-autobuddy-account-id")
        or body.get("account_id")
        or body.get("_account_id")
    )
    body.pop("account_id", None)
    body.pop("_account_id", None)

    # 模型要在选账号**之前**取出：模型级禁用是「账号 x 模型」维度的，
    # 选账号时必须知道本次请求要用的模型，才能跳过拉黑了该模型的账号。
    raw_model = body.get("model", "hy3")
    target_model = MODEL_ALIAS_MAP.get(raw_model, raw_model)

    # 会话粘性键：显式 conversation_id 优先，否则用 system+首条user 内容派生。
    # 客户端不传会话标识时也能享受粘性（panel 的 d- 前缀回退做法）。
    # 派生不出就传 None —— 此时按普通并发分摊选号，不影响行为。
    try:
        session_key = session_sticky.derive_session_key(body, dict(request.headers))
    except Exception:
        session_key = None

    acc = select_account(requested_account_id, model=raw_model,
                         session_key=session_key)
    if not acc or not acc.get("access_token"):
        if requested_account_id:
            raise HTTPException(status_code=409, detail="Requested WorkBuddy account is disabled, expired, or unavailable.")
        raise HTTPException(status_code=401, detail="No enabled WorkBuddy/CodeBuddy account available in gateway.")

    token = acc["access_token"]
    variant = acc.get("variant", "ai")
    base_url = AI_BASE_URL if variant == "ai" else CN_BASE_URL

    # 归因信息：写入 token 流水，供账号卡片与「用量分布/消耗最高的调用」使用。
    served_account_id = _account_id(acc) or ""
    served_account_name = _account_label(acc)

    # 标记一次在飞请求：让并发请求优先落到「当前最闲」的账号（select_account 据此排序），
    # 而不是所有并发都压在同一个账号上。下游无论是流式还是非流式都必须释放，
    # 所以统一挂在 finally / 生成器收尾两处。
    _acquire_account_slot(served_account_id)

    # 请求级指标：只记元数据（模型/账号/是否流式），不记提示词与凭证。
    rid = reqlog.new_request_id()
    reqlog.begin(rid, model=raw_model, account=served_account_name,
                 stream=bool(body.get("stream", False)))

    requested_stream = body.get("stream", False)

    body["model"] = target_model

    prompt_text = ""
    if "messages" in body and isinstance(body["messages"], list):
        body["messages"] = normalize_messages_for_upstream(body["messages"])
        prompt_text = " ".join([str(m.get("content", "")) for m in body["messages"]])

    input_tokens = estimate_tokens(prompt_text)

    body["stream"] = True

    headers = _upstream_headers(token)

    client = httpx.AsyncClient(timeout=180.0)

    # 已试过的账号：换号重试时用来去重，避免在同一个账号上反复撞墙。
    tried_account_ids = {served_account_id} if served_account_id else set()
    # 换号重试上限 = 候选账号数 + 1（首次那次不算重试）；不设死数字，
    # 让「只有少数账号支持该模型」时能把所有可能都试一遍。
    max_attempts = 1 + len(_retry_candidates(raw_model, tried_account_ids))

    if requested_stream:
        req = client.build_request("POST", f"{base_url}/chat/completions", json=body, headers=headers)
        # ⚠️ 2026-10-16 修复（日志里刷 httpx.ReadTimeout + 一大坨 ASGI 堆栈，
        # 最后返回 500）：
        # 上游建连/首字节超时时 client.send 会抛 httpx.ReadTimeout/ConnectError，
        # 原来没有任何捕获，直接冒到 FastAPI 顶层 → 500 + 几十行堆栈刷屏。
        # 这里显式捕获：返回 **504 Gateway Timeout**（语义正确：我们自己的网关没问题，
        # 是上游不回包），并给一句人话 error 字段，客户端能直接展示。
        # 同时释放账号槽位，避免超时后账号被一直占着。
        try:
            res = await client.send(req, stream=True)
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout,
                httpx.ConnectError, httpx.ReadError) as _te:
            lprint("upstream", "上游请求失败(%s): %s"
                   % (type(_te).__name__, str(_te)[:120]))
            await client.aclose()
            _release_account_slot(served_account_id)
            # 超时也算一次失败：让该账号进冷却，后续自动轮询跳过它
            try:
                _note_upstream_result(served_account_id, 504,
                                      "upstream timeout: %s" % type(_te).__name__,
                                      rid=rid, headers=None)
            except Exception:
                pass
            # 失败即解绑：这个号本轮证明不可用，下次换号
            try:
                session_sticky.unbind(session_key)
            except Exception:
                pass
            return Response(
                content=json.dumps({
                    "error": {
                        "message": "上游模型服务响应超时（%s），请稍后重试或切换模型。"
                                   % type(_te).__name__,
                        "type": "upstream_timeout",
                        "code": 504,
                    }
                }, ensure_ascii=False).encode("utf-8"),
                status_code=504,
                headers={"Content-Type": "application/json"},
            )

        # 换号重试：上游在**还没吐出任何正文**时就拒绝，且错误形状指向
        # 「该账号不支持该模型」→ 拉黑这个组合 + 换个账号再试。
        # 重试只发生在响应体尚未转发给客户端的阶段，所以对客户端是透明的。
        attempt = 0
        while True:
            attempt += 1
            if res.status_code == 200:
                break
            body_text = (await res.aread()).decode("utf-8", "ignore")
            await res.aclose()
            if not _looks_like_model_unavailable(res.status_code, body_text):
                # 失败记账：429/402/404 等让该账号进入冷却，后续自动轮询跳过它
                _note_upstream_result(served_account_id, res.status_code, body_text,
                                      rid=rid, headers=res.headers)
                # 失败即解绑：这个号已经证明不可用，下次换号，别把会话继续钉在它身上
                try:
                    session_sticky.unbind(session_key)
                except Exception:
                    pass
                await client.aclose()
                _release_account_slot(served_account_id)
                return Response(content=body_text.encode("utf-8"),
                                status_code=res.status_code,
                                headers={"Content-Type": "application/json"})
            # 学习 + 找下一个候选
            _learn_model_unavailable(served_account_id, raw_model)
            _release_account_slot(served_account_id)
            nxt = [a for a in _retry_candidates(raw_model, tried_account_ids) if a.get("access_token")]
            if not nxt or attempt >= max_attempts:
                await client.aclose()
                return Response(content=body_text.encode("utf-8"),
                                status_code=res.status_code,
                                headers={"Content-Type": "application/json"})
            acc2 = nxt[0]
            served_account_id = _account_id(acc2) or ""
            served_account_name = _account_label(acc2)
            tried_account_ids.add(served_account_id)
            variant = acc2.get("variant", "ai")
            base_url = AI_BASE_URL if variant == "ai" else CN_BASE_URL
            headers = _upstream_headers(acc2['access_token'])
            _acquire_account_slot(served_account_id)
            req = client.build_request("POST", f"{base_url}/chat/completions", json=body, headers=headers)
            res = await client.send(req, stream=True)
            lprint("retry", f"换号重试第 {attempt} 次 -> {served_account_name}")

        async def stream_generator():
            # 逐块扫描而非缓冲：先 yield 把字节送出去，再解析这一块。
            # 只留「最后一行没读完」的残余，正文只数字符不存内容。
            scanner = UsageScanner()
            try:
                async for chunk in res.aiter_bytes():
                    yield chunk
                    scanner.feed(chunk)
                reqlog.done(rid, reqlog.OUTCOME_SUCCESS)
            except Exception:
                reqlog.done(rid, reqlog.OUTCOME_STREAM_ERROR)
                raise
            finally:
                await res.aclose()
                await client.aclose()
                _release_account_slot(served_account_id)
                duration = time.time() - start_time
                usage = scanner.usage
                # 拿不到 usage 只有一种情况：客户端中途断开、末帧没到。
                # 此时回退到字符估算，并把缓存命中记为 0（不知道就是不知道）。
                record_token_usage(
                    model=raw_model,
                    input_tokens=usage["prompt"] if usage else input_tokens,
                    output_tokens=(usage["completion"] if usage
                                   else estimate_tokens_from_chars(scanner.text_chars)),
                    duration_sec=duration,
                    request_id=scanner.response_id or "chatcmpl-wb",
                    account_id=served_account_id,
                    account_name=served_account_name,
                    variant=variant,
                    cache_read=usage["cacheRead"] if usage else 0,
                    cache_write=usage["cacheWrite"] if usage else 0,
                    key_id=gate.get("keyId") or "",
                    key_name=gate.get("keyName") or "",
                )

        # 上游已经接受了这个「账号 × 模型」组合（200 且尚未换号），记入正向矩阵。
        try:
            record_capability(served_account_id, raw_model, True)
        except Exception:
            pass

        # 成功：清零该账号的连续失败与软退避计数
        _note_upstream_result(served_account_id, 200, "")

        return StreamingResponse(
            stream_generator(),
            status_code=res.status_code,
            headers={k: v for k, v in res.headers.items() if k.lower() in ["content-type", "cache-control", "x-accel-buffering"]}
        )
    else:
        req = client.build_request("POST", f"{base_url}/chat/completions", json=body, headers=headers)
        # 与流式路径同一套超时兜底（见上面的详细注释）：
        # 非流式同样不能把 httpx.ReadTimeout 冒到顶层变成 500 + 堆栈。
        try:
            res = await client.send(req, stream=True)
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout,
                httpx.ConnectError, httpx.ReadError) as _te:
            lprint("upstream", "上游请求失败(%s): %s"
                   % (type(_te).__name__, str(_te)[:120]))
            await client.aclose()
            _release_account_slot(served_account_id)
            try:
                _note_upstream_result(served_account_id, 504,
                                      "upstream timeout: %s" % type(_te).__name__,
                                      rid=rid, headers=None)
            except Exception:
                pass
            try:
                session_sticky.unbind(session_key)
            except Exception:
                pass
            return Response(
                content=json.dumps({
                    "error": {
                        "message": "上游模型服务响应超时（%s），请稍后重试或切换模型。"
                                   % type(_te).__name__,
                        "type": "upstream_timeout",
                        "code": 504,
                    }
                }, ensure_ascii=False).encode("utf-8"),
                status_code=504,
                headers={"Content-Type": "application/json"},
            )
        # 非流式路径同样做「模型不可用 → 拉黑 + 换号重试」,
        # 判定与流式一致:只在响应体尚未转发时重试。
        while res.status_code != 200:
            body_text = (await res.aread()).decode("utf-8", "ignore")
            await res.aclose()
            if not _looks_like_model_unavailable(res.status_code, body_text):
                # 失败记账（同流式路径）
                _note_upstream_result(served_account_id, res.status_code, body_text,
                                      rid=rid, headers=res.headers)
                # 失败即解绑（同流式路径）
                try:
                    session_sticky.unbind(session_key)
                except Exception:
                    pass
                await client.aclose()
                _release_account_slot(served_account_id)
                return Response(content=body_text.encode("utf-8"),
                                status_code=res.status_code,
                                headers={"Content-Type": "application/json"})
            _learn_model_unavailable(served_account_id, raw_model)
            _release_account_slot(served_account_id)
            nxt = [a for a in _retry_candidates(raw_model, tried_account_ids) if a.get("access_token")]
            if not nxt or len(tried_account_ids) >= max_attempts:
                await client.aclose()
                return Response(content=body_text.encode("utf-8"),
                                status_code=res.status_code,
                                headers={"Content-Type": "application/json"})
            acc2 = nxt[0]
            served_account_id = _account_id(acc2) or ""
            served_account_name = _account_label(acc2)
            tried_account_ids.add(served_account_id)
            variant = acc2.get("variant", "ai")
            base_url = AI_BASE_URL if variant == "ai" else CN_BASE_URL
            headers = _upstream_headers(acc2['access_token'])
            _acquire_account_slot(served_account_id)
            req = client.build_request("POST", f"{base_url}/chat/completions", json=body, headers=headers)
            res = await client.send(req, stream=True)
            lprint("retry", f"非流式换号重试 -> {served_account_name}")

        collected_content = ""
        response_id = "chatcmpl-wb"
        finish_reason = "stop"
        usage = None
        assistant_msg = {}

        try:
            async for line in res.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    if "id" in chunk:
                        response_id = chunk["id"]
                    found = extract_usage(chunk)
                    if found:
                        usage = found
                    choices = chunk.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        # 整块 delta 都累加进 assistant_msg —— 不挑字段。
                        # 以前这里只挑 content，于是 tool_calls / reasoning_content
                        # 被静默丢掉，客户端拿到 finish_reason=tool_calls 却没有
                        # tool_calls 数组（用户 2026-10-02 反馈）。工具调用链路
                        # 一旦坏掉，Agent 侧表现为「模型说要调工具、却没有任何调用」。
                        accumulate_delta(assistant_msg, delta)
                        if "content" in delta and delta["content"]:
                            collected_content += delta["content"]
                        if choices[0].get("finish_reason"):
                            finish_reason = choices[0]["finish_reason"]
                except Exception:
                    pass
        finally:
            await res.aclose()
            await client.aclose()
            _release_account_slot(served_account_id)

        duration = time.time() - start_time
        record_token_usage(
            model=raw_model,
            input_tokens=usage["prompt"] if usage else input_tokens,
            output_tokens=(usage["completion"] if usage else estimate_tokens(collected_content)),
            duration_sec=duration,
            request_id=response_id,
            account_id=served_account_id,
            account_name=served_account_name,
            variant=variant,
            cache_read=usage["cacheRead"] if usage else 0,
            cache_write=usage["cacheWrite"] if usage else 0,
            key_id=gate.get("keyId") or "",
            key_name=gate.get("keyName") or "",
        )
        input_tokens = usage["prompt"] if usage else input_tokens
        output_tokens = usage["completion"] if usage else estimate_tokens(collected_content)

        # 先把 voucher 的 assistant 消息补齐：
        # role 必须有；content 优先用累加出来的整段（等价且更全）；
        # tool_calls / reasoning_content 等其余字段，上游给了就原样带上 ——
        # 这才是「透传」。以前只塞 content，等于把工具调用那半截消息扔了。
        final_msg = dict(assistant_msg) if isinstance(assistant_msg, dict) else {}
        final_msg["role"] = final_msg.get("role") or "assistant"
        if final_msg.get("content") is None:
            final_msg["content"] = collected_content
        final_msg.pop("index", None)

        result_payload = {
            "id": response_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": raw_model,
            "choices": [
                {
                    "index": 0,
                    "message": final_msg,
                    "finish_reason": finish_reason
                }
            ],
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                # 上游返回了多少就带多少，拿不到就不给 —— 调用方按标准字段读，
                # 多出来的这些只是把缓存命中透明地透出去。
                **({"prompt_tokens_details": {"cached_tokens": usage["cacheRead"]},
                    "cache_read_input_tokens": usage["cacheRead"],
                    "cache_creation_input_tokens": usage["cacheWrite"]} if usage else {})
            }
        }
        # 非流式成功同样记账（成功清零失败计数）
        _note_upstream_result(served_account_id, 200, "")
        reqlog.done(rid, reqlog.OUTCOME_SUCCESS)

        return Response(
            content=json.dumps(result_payload, ensure_ascii=False),
            status_code=200,
            headers={"Content-Type": "application/json"}
        )

# ---------------------------------------------------------------------------
# 容器适配的账号轮询
# 官方自带的轮询在需要切换时，会重启桌面应用（/usr/bin/workbuddy）并把认证文件
# 写进 .local/share/CodeBuddyExtension —— 这些宿主资源容器里都没有，切换动作必然失败。
# 但网关对外提供服务只关心「当前用哪个账号」，所以这里实现一套纯容器内的轮询：
# 只维护 rotate/state.json 里的 activeAccountId，不触碰任何桌面应用。
# ---------------------------------------------------------------------------

ROTATE_ENABLED = os.getenv("GATEWAY_ROTATE_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
ROTATE_INTERVAL_MINUTES = max(1, int(os.getenv("GATEWAY_ROTATE_INTERVAL_MINUTES", "30") or 30))
ROTATE_LOG_FILE = DATA_DIR / "gateway_rotate_logs.json"

_ROTATE_RUNTIME: Dict[str, Any] = {
    "enabled": ROTATE_ENABLED,
    "interval_minutes": ROTATE_INTERVAL_MINUTES,
    "last_check_at": None,
    "last_switch_at": None,
    "last_reason": None,
    "active_account_id": None,
    "active_account_name": None,
}


def _rotate_state_snapshot() -> Dict[str, Any]:
    return dict(_ROTATE_RUNTIME)


def _load_accounts() -> List[Dict[str, Any]]:
    """读账号池，**合并两份账号文件**（按账号 ID 去重，字段互补）。

    为什么必须合并：容器里同时存在两份账号文件，且内容并不一致 ——
      - ``DATA_DIR / accounts.json``（``/data/.autobuddy/accounts.json``）
        容器自己的副本，通常条目较少（实测 6 个）；
      - ``/data/.wb-switch/accounts.json``
        官方维护的那份，**用户新加入的账号先出现在这里**（实测 8 个）。

    早先这里只读自己那份，造成一个很难查的故障：新账号（如「一杯美式」）
    能出现在账号池列表里（响应层另有一份合并逻辑供展示），卡片控件也齐全，
    但一点「停用 / 检测」就回报 ``account not found`` —— 因为**写操作打到网关时，
    网关自己的账号集合里没有这个账号**。展示与操作用了两个不同的账号来源，
    才是「按钮在但功能全失效」的真正原因。

    修在**这个唯一入口**上，所有依赖它的接口（切换/检测/刷新令牌/删除/
    轮询选号…）一并修好，而不是在每个接口各自打补丁。

    合并原则：先官方后本地覆盖，同 ID 以本地为准（本地字段更全，含 variant 等）；
    读不到就返回空列表，绝不因账号文件异常而让网关崩溃。
    """
    merged: Dict[str, Dict[str, Any]] = {}
    sources = [Path("/data/.wb-switch/accounts.json"), DATA_DIR / "accounts.json"]
    for accounts_file in sources:
        if not accounts_file.exists():
            continue
        try:
            with open(accounts_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if isinstance(data, dict):
            data = data.get("accounts") if isinstance(data.get("accounts"), list) else []
        if not isinstance(data, list):
            continue
        for acc in data:
            if not isinstance(acc, dict):
                continue
            key = str(_account_id(acc) or acc.get("id") or acc.get("uid") or "")
            if not key:
                # 没 ID 的条目按昵称/邮箱兜底，总比整条丢掉强。
                key = str(acc.get("nickname") or acc.get("email") or "")
            if not key:
                continue
            # 后遍历的（本地那份）覆盖先遍历的（官方那份）—— 本地字段更全。
            merged[key] = {**(merged.get(key) or {}), **acc}
    return list(merged.values())


def _write_active_account(account_id: str) -> None:
    state_file = DATA_DIR / "rotate" / "state.json"
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state: Dict[str, Any] = {}
        if state_file.exists():
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    state = json.load(f) or {}
            except Exception:
                state = {}
        state["activeAccountId"] = account_id
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[rotate] failed to persist active account: {e}")


def _append_rotate_log(entry: Dict[str, Any]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        logs = []
        if ROTATE_LOG_FILE.exists():
            try:
                with open(ROTATE_LOG_FILE, "r", encoding="utf-8") as f:
                    logs = json.load(f) or []
            except Exception:
                logs = []
        logs.append(entry)
        with open(ROTATE_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(logs[-200:], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[rotate] failed to write rotate log: {e}")


def _account_is_usable(acc: Dict[str, Any], now_ms: int) -> bool:
    if not acc.get("access_token"):
        return False
    expires_at = acc.get("expiresAt")
    if isinstance(expires_at, (int, float)) and expires_at > 0 and expires_at <= now_ms:
        return False
    return True


def rotate_once() -> Dict[str, Any]:
    """执行一次轮询：当前账号仍可用就保持不动，失效才切到备用账号。"""
    now_ms = int(time.time() * 1000)
    accounts = _load_accounts()
    usable = [a for a in accounts if _account_is_usable(a, now_ms)]
    _ROTATE_RUNTIME["last_check_at"] = now_ms

    if not usable:
        reason = "没有可用账号（token 缺失或已过期）"
        _ROTATE_RUNTIME["last_reason"] = reason
        _append_rotate_log({"ts": now_ms, "action": "skipped", "reason": reason})
        return {"status": "skipped", "reason": reason}

    current = get_active_account()
    current_id = (current or {}).get("id")
    current_usable = current is not None and any(a.get("id") == current_id for a in usable)

    if current_usable:
        reason = "当前账号仍可用，保持不切换"
        _ROTATE_RUNTIME["last_reason"] = reason
        _ROTATE_RUNTIME["active_account_id"] = current_id
        _ROTATE_RUNTIME["active_account_name"] = current.get("nickname")
        _append_rotate_log({"ts": now_ms, "action": "kept", "reason": reason,
                            "accountId": current_id, "accountName": current.get("nickname")})
        return {"status": "kept", "reason": reason, "activeAccountId": current_id}

    target = usable[0]
    reason = "原账号已失效，切换到可用账号"
    _write_active_account(target.get("id"))
    _ROTATE_RUNTIME["last_switch_at"] = now_ms
    _ROTATE_RUNTIME["last_reason"] = reason
    _ROTATE_RUNTIME["active_account_id"] = target.get("id")
    _ROTATE_RUNTIME["active_account_name"] = target.get("nickname")
    _append_rotate_log({"ts": now_ms, "action": "switched", "reason": reason,
                        "from": current_id, "to": target.get("id"),
                        "accountName": target.get("nickname")})
    return {"status": "switched", "reason": reason,
            "activeAccountId": target.get("id"),
            "activeAccountName": target.get("nickname")}


async def account_rotate_loop() -> None:
    if not ROTATE_ENABLED:
        print("[rotate] gateway-side rotation disabled (GATEWAY_ROTATE_ENABLED)")
        return
    print(f"[rotate] gateway-side rotation enabled, interval={ROTATE_INTERVAL_MINUTES} min")
    while True:
        try:
            result = rotate_once()
            lprint("rotate", f"{result.get('status')}: {result.get('reason')}")
        except Exception as e:
            lprint("rotate", f"loop error: {e}")
        await asyncio.sleep(ROTATE_INTERVAL_MINUTES * 60)


@app.get("/rotate/status")
def rotate_status():
    return _rotate_state_snapshot()


@app.post("/rotate/run")
def rotate_run():
    return rotate_once()


# ---------------------------------------------------------------------------
# 模型可用性自动巡检
#
# 需求来源（用户 2026-09-19）：在设置里能开关一个轮询，开启后每隔一段时间检测
# 每个账号（或指定账号）对应模型的可用性，不可用就自动禁用、恢复可用就自动启用；
# 关掉之后仍然可以手动禁用。
#
# 实现要点：
# - 巡检读写的就是 model_policy.json —— 与手动禁用是同一份策略，
#   所以「关掉巡检后手动禁用照常有效」，自动写入的禁用项也不会凭空消失。
# - 禁用项带来源标记（manual / auto）：自动启用**只放开自己写的 auto 项**，
#   人手禁的模型不受影响 —— 否则自愈会把「能跑但太贵」这类控成本配置一并放开。
# - 探测会真实发起一次极小的上游请求，因此**默认关闭**，且默认只探测
#   「该账号调用过的模型 + 内置基础清单」，避免一上来就把上游打爆。
# - 探测请求体必须与网关转发上游的形态一致（流式 + 首条为 system +
#   不带任何可被校验的可选参数），否则会被上游整体拒绝，
#   表现为「所有模型都不可用」并成批误禁。
# - 限流（429）与网络异常计为 transient、被上游参数校验拒绝计为
#   probe_defect，两者**都不参与自动禁用**；账号凭据整体失效时跳过该账号；
#   整轮无一个可用时判定为探测异常，整轮作废不写策略。
# ---------------------------------------------------------------------------

_HEALTH_RUNTIME: Dict[str, Any] = {
    "running": False,
    "lastRunAt": None,
    "lastResult": None,
    "lastError": None,
}
# 同一时刻只允许一轮巡检：手动「立即巡检」与后台定时轮次可能撞车，
# 并发跑会重复探测同一个组合，还会让两轮各自读到的策略互相覆盖。
_HEALTH_RUN_LOCK = threading.Lock()


def _used_models_by_account() -> Dict[str, set]:
    """按账号聚合「调用过哪些模型」，供 onlyUsedModels 收窄探测范围。

    数据源是网关自己的 token 流水（``token_stats_logs.json``），
    它带 ``accountId``，能精确归因；官方 usage 缓存只对当前账号返回明细，不适用。
    """
    log_file = DATA_DIR / "token_stats_logs.json"
    if not log_file.exists():
        return {}
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            records = json.load(f)
    except Exception:
        return {}
    if not isinstance(records, list):
        return {}

    result: Dict[str, set] = {}
    for r in records:
        if not isinstance(r, dict):
            continue
        acc = r.get("accountId")
        model = r.get("model")
        if acc and model:
            result.setdefault(str(acc), set()).add(str(model))
    return result


def _base_model_ids() -> List[str]:
    """内置基础模型清单（不含自动发现的），作为「新账号还没调用记录」时的兜底范围。"""
    return [str(m["id"]) for m in BASE_MODELS if m.get("id")]


def _health_base_url(variant: str) -> str:
    return AI_BASE_URL if variant == "ai" else CN_BASE_URL


def _health_problem_note(summary: Dict[str, Any]) -> Optional[str]:
    """把「本轮结果不可信」的原因汇总成一句话；一切正常时返回 None。

    这几类情况必须显式说出来，否则界面上只会显示「一轮下来什么都没变」，
    用户根本无从判断是真的没变化，还是探测压根没跑出有效结论。
    """
    if summary.get("aborted"):
        return summary.get("reason") or "本轮巡检已作废，未写入任何变更。"

    counts = summary.get("counts") or {}
    notes: List[str] = []
    defects = int(counts.get("probe_defect") or 0)
    if defects:
        codes = "、".join(str(c) for c in (summary.get("defectCodes") or [])) or "未知"
        notes.append(
            f"{defects} 个组合的探测请求被上游参数校验拒绝（错误码 {codes}），已跳过未写入"
        )
    auth_failed = summary.get("authFailed") or []
    if auth_failed:
        names = "、".join(str(a.get("name") or a.get("id")) for a in auth_failed[:5])
        notes.append(f"{len(auth_failed)} 个账号凭据失效（{names}），已跳过，建议重新登录")
    return "；".join(notes) if notes else None


def health_config_snapshot() -> Dict[str, Any]:
    cfg = model_health.load_config()
    accounts = _load_accounts()
    wanted = set(cfg.get("accountIds") or [])
    selected = [
        {
            "id": str(_account_id(a)),
            "name": _account_label(a),
            "variant": a.get("variant", "ai"),
        }
        for a in accounts
        if _account_id(a) and (not wanted or str(_account_id(a)) in wanted)
    ]
    return {
        "ok": True,
        "config": cfg,
        "accounts": [
            {"id": str(_account_id(a)), "name": _account_label(a), "variant": a.get("variant", "ai")}
            for a in accounts if _account_id(a)
        ],
        "activeAccounts": len(selected),
        "intervalMinutes": cfg.get("intervalMinutes"),
        "minIntervalMinutes": model_health.MIN_INTERVAL_MINUTES,
        # 让界面能显示「手动禁用 N 项 / 巡检禁用 M 项」：
        # 两者含义不同（人为决定 vs 自动探测），恢复方式也不同。
        "disabledBySource": model_policy.count_by_source(),
        "autoEnableProtectsManual": True,
        "runtime": dict(_HEALTH_RUNTIME),
    }


@app.get("/model-health/config")
def model_health_config():
    """返回巡检配置、可选账号清单与上次运行状态。"""
    return health_config_snapshot()


@app.put("/model-health/config")
def update_model_health_config(payload: Dict[str, Any]):
    """更新巡检配置（只覆盖显式给出的字段）。"""
    model_health.merge_config(payload or {})
    return health_config_snapshot()


@app.get("/model-health/status")
def model_health_status():
    return {
        "ok": True,
        "config": model_health.load_config(),
        "runtime": dict(_HEALTH_RUNTIME),
        "disabledTotal": model_policy.disabled_total(),
        "disabledBySource": model_policy.count_by_source(),
    }


@app.post("/model-health/run")
def model_health_run(payload: Optional[Dict[str, Any]] = None):
    """立即执行一轮巡检（不改变开关状态）。

    请求体可临时覆盖本轮参数（如只探测某几个账号），便于先小范围试跑。
    """
    overrides = payload or {}
    result = run_model_health_check(overrides=overrides if isinstance(overrides, dict) else None)
    return {"ok": True, "result": result}


def run_model_health_check(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """执行一轮巡检并记录运行状态。任何异常都不向上抛，只记录到 lastError。

    并发调用会被直接拒绝：手动「立即巡检」与后台定时轮次可能同时到达，
    没有互斥就会双倍消耗上游额度，还会让两轮各自读到的策略互相覆盖。
    """
    if not _HEALTH_RUN_LOCK.acquire(blocking=False):
        return {"error": "已有巡检正在进行，请稍后再试", "skipped": True}
    try:
        cfg = model_health.load_config()
        if overrides:
            merged = dict(cfg)
            for key in ("accountIds", "onlyUsedModels", "autoEnable"):
                if key in overrides:
                    merged[key] = overrides[key]
            cfg = model_health._coerce_config(merged)

        _HEALTH_RUNTIME["running"] = True
        try:
            # 巡检只探**账号池内**的账号，与日常调用同源。
            #
            # 此前这里传 _load_accounts()（两份账号文件的全量合并），
            # 而日常调用走的是 _enabled_accounts()（白名单 + 停用策略过滤）。
            # 两套账号集合不一致，表现为「账号已移出池子，巡检还在探它、还在报错」
            # （用户 2026-09-29 反馈）。
            #
            # 这与项目既有的设计原则一致 —— build_account_store() 的注释：
            # 「账号来源与账号池其它功能保持同一个出处，
            #   避免『账号页看得到、每日任务里没有』」。
            #
            # 注意：overrides 里显式指定了 accountIds 时（界面手动「只探这几个账号」），
            # model_health.select_targets 会按该清单再收窄，这里无需额外处理。
            raw = model_health.run_round(
                accounts=_enabled_accounts(_load_accounts(), _load_pool_config()),
                config=cfg,
                client_factory=lambda: httpx.Client(timeout=30.0),
                base_url_for=_health_base_url,
                used_models=_used_models_by_account(),
                base_models=_base_model_ids(),
            )
            summary = model_health.summarize(raw)
            # 把巡检的有效结论回填正向能力矩阵（P1）：
            # available -> 记为支持；unavailable -> 记为不支持。
            # transient / probe_defect / restricted **不写** —— 那几类说明探测
            # 没拿到有效结论（限流、参数被拒、风控拦截），写进矩阵会把探测侧
            # 的问题伪装成「这个模型不支持」，反而让选号误伤好账号。
            try:
                backfilled = 0
                for acc_report in raw.get("reports") or []:
                    acc_id = acc_report.get("accountId")
                    for item in acc_report.get("results") or []:
                        verdict = item.get("verdict")
                        model_id = item.get("model")
                        if verdict == "available":
                            record_capability(acc_id, model_id, True)
                            backfilled += 1
                        elif verdict == "unavailable":
                            record_capability(acc_id, model_id, False)
                            backfilled += 1
                if backfilled:
                    lprint("capability", f"巡检回填能力矩阵 {backfilled} 条")
            except Exception as e:
                print(f"[capability] 巡检回填失败: {e}")
            # 逐账号明细只在这种「手动触发」的响应里返回，便于排查；
            # 定时轮次不返回（没人看，只是白白撑大内存里的状态）。
            summary["reports"] = raw.get("reports", [])
            _HEALTH_RUNTIME["lastRunAt"] = summary.get("checkedAt")
            _HEALTH_RUNTIME["lastResult"] = model_health.summarize(raw)
            # 出问题时要让界面把它当异常显示出来，而不是伪装成一次
            # 「什么都没变」的正常巡检：整轮作废、探测被上游拒绝、账号凭据失效
            # 都属于「本轮结果不可信」，逐个给出原因。
            _HEALTH_RUNTIME["lastError"] = _health_problem_note(summary)
            lprint("health", f"round done: combos={summary.get('combos')} "
                  f"available={summary['counts'].get('available')} "
                  f"unavailable={summary['counts'].get('unavailable')} "
                  f"transient={summary['counts'].get('transient')} "
                  f"probeDefect={summary['counts'].get('probe_defect')} "
                  f"authFailed={len(summary.get('authFailed') or [])} "
                  f"aborted={summary.get('aborted')}")
            # 场景通知：巡检结束（用户 2026-10-13 需求）。不阻塞巡检返回。
            # 失败只记日志 —— 通知是附属能力，绝不能影响巡检主流程。
            #
            # ⚠️ 2026-10-16 修复（日志里刷了几千行
            #    "notice schedule failed: no running event loop"）：
            # 原先写成 `asyncio.get_running_loop().create_task(...)`，
            # 但 run_model_health_check() 是**同步**函数 ——
            #   · 定时巡检走 asyncio.to_thread(...) → 工作线程，无事件循环
            #   · 手动触发是 def 路由 → FastAPI 线程池，同样无事件循环
            # 所以 get_running_loop() 每次都抛 RuntimeError，
            # 巡检结束通知**一次都没发出去过**。
            # send_scene() 本身也是同步函数（内部 httpx.Client 同步请求），
            # 因此直接在当前线程调用即可：不阻塞事件循环（本来就不在循环里），
            # 也不需要 create_task。
            try:
                _mi = _collect_models_info()
                _t, _lines = _notify_mod.build_health_done(summary, _mi)
                _notify_mod.send_scene("health_done", _t, _lines)
            except Exception as _e:
                lprint("health", f"notice failed: {_e}")
            return summary
        except Exception as e:
            _HEALTH_RUNTIME["lastError"] = f"{type(e).__name__}: {e}"
            lprint("health", f"run failed: {e}")
            return {"error": _HEALTH_RUNTIME["lastError"]}
        finally:
            _HEALTH_RUNTIME["running"] = False
    finally:
        _HEALTH_RUN_LOCK.release()


async def model_health_loop() -> None:
    """按配置间隔执行巡检。配置改成关闭时，下一轮自动跳过（不退出循环）。

    巡检本身是同步阻塞的（几十上百次 HTTP 往返），必须丢到线程里跑，
    否则会把事件循环卡死 —— 巡检期间所有转发请求都会一起停摆。
    """
    print("[health] model availability check loop started")
    while True:
        try:
            cfg = model_health.load_config()
            interval = int(cfg.get("intervalMinutes") or model_health.DEFAULT_INTERVAL_MINUTES)
            if cfg.get("enabled"):
                # 结果日志由 run_model_health_check 统一打印（手动 / 定时共用一条口径）
                await asyncio.to_thread(run_model_health_check)
            # 关闭状态也要按间隔轮询配置变更，否则改完开关要等整整一个周期才生效。
            await asyncio.sleep(interval * 60)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            lprint("health", f"loop error: {e}")
            await asyncio.sleep(60)

class _AccessNoiseFilter(logging.Filter):
    """静音高频轮询与探活的 access log，只保留真正有意义的请求。

    背景（用户 2026-09-26 反馈「后台日志优化你自己看优化了没」）：
    v0.9.0 只对自家 ``lprint`` 通道做了相邻去重，但 **uvicorn 的 access log
    完全没管** —— 实测容器日志最近 500 行里有 251 行是 ``INFO: ... GET ...``，
    WebUI 每几秒轮询一次 /api/status、/gateway/info、/model-health/config，
    探活每 60s 打一次 /health，页面加载再补一堆 /assets/ 静态资源，
    真实请求（POST /v1/chat/completions）反而被淹掉。

    这里按「路径 + 2xx」过滤：只有明确无信息量的成功轮询才丢弃，
    4xx/5xx 与真实业务请求一律保留，避免把故障线索一起静音。
    """

    # WebUI 定时轮询 / 健康探活 / 静态资源 —— 成功时无信息量。
    NOISY_PATHS = (
        "/health",
        "/api/status",
        "/api/gateway-info",
        "/gateway/info",
        "/api/model-health",
        "/model-health/config",
        "/api-keys/status",
        "/api/api-keys",
        "/api/checkin/logs",
        "/api/rotate/logs",
        "/api/rotate/status",
        "/api/rate-limits",
        "/assets/",
        "/icon.png",
        "/favicon.ico",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        # 只静音成功响应；4xx/5xx 保留线索。
        if " 2" not in msg or " OK" not in msg:
            return True
        return not any(p in msg for p in self.NOISY_PATHS)


# ---------------------------------------------------------------------------
# access log 相邻去重
#
# 背景（用户 2026-09-27）：「相同时间日志完全一模一样出现也没有意义的，
# 出现一条即可」—— 即便是 API 调用日志。
#
# 实测（本次 2322 行日志）：归一化后只有 106 种不同内容，其中
#   /api/switch/progress  1533 条
#   /api/travel/status     119 条
#   /api/wb-daily/credit-summary  88 条
# uvicorn access log 占全部日志的 98%。
#
# 为什么不能只靠 NOISY_PATHS 黑名单：黑名单只能盖住**已知**接口，
# 上述三个刷屏最凶的都不在旧名单里 —— 每新增一个轮询接口就要手工补一次，
# 永远追不上。所以这里改成**通用相邻去重**：不管什么接口，
# 只要与上一条内容相同就只累计、不输出；不同才输出，并补一行重复统计。
#
# 注意：access log 是**多路交错**的（A、B 两个接口交替轮询），
# 单纯的「与上一条比较」会被交错打断而失效。所以按「规范化后的日志内容」
# 各自独立记录上一条，互不干扰。规范化即去掉客户端端口号 ——
# 同一接口每次连接的临时端口不同，不归一化就永远判不相等。
# ---------------------------------------------------------------------------
_ACCESS_DEDUP_LOCK = threading.Lock()
_ACCESS_DEDUP_LAST: Dict[str, str] = {}
_ACCESS_DEDUP_COUNT: Dict[str, int] = {}

# 被吞掉的重复次数不能只在「内容变化时」才汇报 —— 见 _flush_access_dedup 的说明。
# 改成周期汇报：默认 60s 一次，可用环境变量调（测试时可设成几秒）。
_ACCESS_DEDUP_FLUSH_SEC = float(os.getenv("ACCESS_DEDUP_FLUSH_SEC", "60") or 60)
# 键的数量上限。access log 的内容种类理论上无界（URL 里带动态 id 时尤甚），
# 不封顶就是一条缓慢的内存泄漏。
_ACCESS_DEDUP_MAX_KEYS = 500
_ACCESS_DEDUP_NEXT_FLUSH = 0.0


# 上一周期打印过的 (内容 -> 次数)。用于识别「完全没有新信息」的重复汇报。
_ACCESS_DEDUP_LAST_PRINTED: Dict[str, int] = {}


def _is_noisy_access(key: str) -> bool:
    """该 access 内容是否属于已知的无信息量轮询（连 ×N 行都不必打）。

    周期汇报是**直接 print**，不走 logging filter，因此 NOISY_PATHS 黑名单
    对它无效 —— 这正是 /health 这类探活接口每 60s 稳定产出一条 `×N`、
    把真正有用的信息淹掉的原因（实测：/health 独占 1561 条）。
    """
    try:
        return any(p in key for p in _AccessNoiseFilter.NOISY_PATHS)
    except Exception:
        return False


def _flush_access_dedup(now: float = 0.0) -> None:
    """汇报并清空各条内容累积的重复次数。

    为什么必须周期汇报，而不是「内容变化时汇报」：

    为了抗交错轮询（A、B 交替），去重是**按内容各自独立**记录的 ——
    c3 累积的次数记在 c3 名下。等 d4 出现时去查的是 d4 的计数（= 0），
    于是「上一条重复 N 次」这行**永远不会触发**（实测已确认）。

    所以改为按时间周期把各条内容的待报次数统一打出来并清零。
    这样既保留可观测性（不会被悄悄吞掉），又不破坏按内容去重的正确性。
    """
    global _ACCESS_DEDUP_NEXT_FLUSH, _ACCESS_DEDUP_LAST_PRINTED
    if not now:
        now = time.time()
    with _ACCESS_DEDUP_LOCK:
        pending = [(k, c) for k, c in _ACCESS_DEDUP_COUNT.items() if c > 0]
        for k in list(_ACCESS_DEDUP_COUNT.keys()):
            _ACCESS_DEDUP_COUNT[k] = 0
        # 容量封顶：超了就丢最早写入的键（dict 保持插入顺序）
        over = len(_ACCESS_DEDUP_LAST) - _ACCESS_DEDUP_MAX_KEYS
        if over > 0:
            for k in list(_ACCESS_DEDUP_LAST.keys())[:over]:
                _ACCESS_DEDUP_LAST.pop(k, None)
                _ACCESS_DEDUP_COUNT.pop(k, None)
        _ACCESS_DEDUP_NEXT_FLUSH = now + _ACCESS_DEDUP_FLUSH_SEC
        # 同步清理打印记录，避免随 key 无界增长
        for kk in list(_ACCESS_DEDUP_LAST_PRINTED.keys()):
            if kk not in _ACCESS_DEDUP_LAST:
                _ACCESS_DEDUP_LAST_PRINTED.pop(kk, None)
    for k, c in pending:
        # 已知无信息量的轮询接口，连 ×N 行也不打 —— 它们每个周期必然重复。
        if _is_noisy_access(k):
            continue
        # 与上一周期**完全相同**（同内容、同次数）= 没有新信息，不再打印。
        # 实测：/health 曾连续 27 条 `×1` 一字不差，这正是用户看到的「重复」。
        if _ACCESS_DEDUP_LAST_PRINTED.get(k) == c:
            continue
        _ACCESS_DEDUP_LAST_PRINTED[k] = c
        # 同一行末尾追加 ×N：period 汇报是补打被打断的那条，
        # 行首留 UVICORN 风格的 INFO 前缀，重复次数放行尾，
        # 不额外制造一条重复记录（用户 2026-10-02 要求）。
        print(f"INFO:     {k} ×{c}", flush=True)


def _normalize_access_msg(msg: str) -> str:
    """规范化 access log：抹掉每次连接都不同的客户端端口号。

    `192.168.31.10:57564` -> `192.168.31.10`
    不抹掉端口号的话，同一接口的两条日志永远不相等，去重完全失效。
    """
    return re.sub(r"(\d+\.\d+\.\d+\.\d+):\d+", r"\1", msg)


class _AccessDedupFilter(logging.Filter):
    """access log 通用相邻去重：完全相同的日志只出现一条。

    与 NOISY_PATHS 黑名单互补而非互斥：
      · 黑名单负责「已知且确定无信息量」的接口（连第一条都不打）
      · 本过滤器负责**其余所有接口**的重复（保留第一条，后续只累计）
    这样新增轮询接口无需改代码，自动生效。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        key = _normalize_access_msg(msg)
        now = time.time()
        if now >= _ACCESS_DEDUP_NEXT_FLUSH:
            _flush_access_dedup(now)
        with _ACCESS_DEDUP_LOCK:
            if _ACCESS_DEDUP_LAST.get(key) == key:
                _ACCESS_DEDUP_COUNT[key] = _ACCESS_DEDUP_COUNT.get(key, 0) + 1
                return False
            _ACCESS_DEDUP_LAST[key] = key
            _ACCESS_DEDUP_COUNT.setdefault(key, 0)
        return True


def _install_access_filter() -> None:
    """把噪声过滤器与去重过滤器挂到 uvicorn.access 上（幂等）。"""
    lg = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _AccessNoiseFilter) for f in lg.filters):
        lg.addFilter(_AccessNoiseFilter())
    if not any(isinstance(f, _AccessDedupFilter) for f in lg.filters):
        lg.addFilter(_AccessDedupFilter())


if __name__ == "__main__":
    import uvicorn
    _install_access_filter()
    port = int(os.getenv("API_PORT", 18091))
    uvicorn.run(app, host="0.0.0.0", port=port)
