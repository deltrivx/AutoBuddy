"""WorkBuddy 每日成长任务服务（每日签到自动化）。

运行在 AutoBuddy 容器内部（loopback 18093），对外由 web_proxy 统一转发。
功能来源：内置 vendor 化的上游开源脚本 gateway/vendor/workbuddy_daily.py
（L0NE-6/WorkBuddy-Daily，MIT），由本服务托管执行。

账号来源：**自动关联 AutoBuddy 账号池**——合并读取 /data/.wb-switch/accounts.json
与 $AB_DATA_DIR/accounts.json，取其中 variant=cn 的账号，用其 refresh_token
生成上游脚本需要的 wb_refresh_tokens.json。
不需也不支持手动加入账号：账号池就是唯一权威来源，再开一个手填入口
只会让「哪些账号在跑」出现两个答案。
兼容性已实测：Keycloak 签发的 RT 可直接走上游插件的刷新接口，且刷新后旧 RT
仍然有效（REUSABLE），因此这里**只读共用**账号池凭据，绝不写回、绝不覆盖
accounts.json，无烧号风险。

对外路由：
  GET    /api/wb-daily/config         获取配置
  POST   /api/wb-daily/config         保存配置（合并写入，绝不整字典重写）
  GET    /api/wb-daily/accounts       参与账号预览（含余额外账号）
  POST   /api/wb-daily/run            立即执行一轮
  GET    /api/wb-daily/jobs           任务列表
  GET    /api/wb-daily/status/{id}    任务状态与日志
  DELETE /api/wb-daily/abort/{id}     中止任务
  GET    /api/wb-daily/health         服务健康与调度状态
"""

import asyncio
import threading
import json
import os
import re
import shutil
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import db

DATA_DIR = Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy"))
CONFIG_FILE = DATA_DIR / "wb_daily_config.json"
WORK_DIR = DATA_DIR / "wb-daily"
# 账号池有两份文件，且内容并不一致，**必须合并读**：
#   - /data/.wb-switch/accounts.json —— 官方维护的那份，用户新加入的账号
#     （如「一杯美式」）先出现在这里；
#   - $AB_DATA_DIR/accounts.json —— 容器自己的副本，字段更全。
# 早先只读官方那一份，导致容器副本里独有的账号在每日任务里出现不了。
ACCOUNTS_JSON = Path("/data/.wb-switch/accounts.json")
ACCOUNTS_JSON_LOCAL = Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy")) / "accounts.json"


def _load_pool_accounts() -> list:
    """合并读取两份账号文件（按账号 ID 去重，本地副本覆盖官方）。

    每日任务的参与账号、以及账号数量统计都走这里 —— 账号来源与账号池
    其它功能保持**同一个出处**，避免「账号页看得到、每日任务里没有」。
    """
    merged: dict = {}
    for path in (ACCOUNTS_JSON, ACCOUNTS_JSON_LOCAL):
        try:
            with open(path, encoding="utf-8") as f:
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
            key = str(acc.get("id") or acc.get("uid") or "")
            if not key:
                key = str(acc.get("nickname") or acc.get("email") or "")
            if not key:
                continue
            merged[key] = {**(merged.get(key) or {}), **acc}
    return list(merged.values())
VENDOR_SCRIPT = Path(__file__).resolve().parent / "vendor" / "workbuddy_daily.py"

DEFAULT_CONFIG = {
    "enabled": True,          # 定时调度总开关
    "interval_hours": 12,     # 每轮执行间隔（小时）
    "run_mode": "full",       # full=完整任务（签到/玩法/领奖） | query=仅查询
    "last_run_at": None,      # 上次执行时间戳（完成后回写，重启不重跑）
    # ---- 以下三项原属设置页的「自动签到」区块，已迁到本页统一管理 ----
    "keepalive_days": 30,     # 保活阈值（天）；0 表示每天无条件刷新
    "lazy_refresh_hours": 6,  # 惰性刷新（小时）
    "checkin_enabled": True,  # 启动时核验服务端状态，未签到账号自动补签
    # ---- 任务巡检（用户 2026-10-16 需求）----
    # 背景：任务被中断（容器重启 / 被 kill / 超时）后，原先只能等下一轮调度
    # （默认 12h，用户配到 24h 就更久）才恢复 —— 中断当天的剩余账号要等到
    # 第二天才跑，直接错过当天收益。
    # 巡检是一个**独立于 interval_hours 的更短周期**：到点就检查最近一轮
    # 是否中断，是则立即从剩余账号续跑。
    # ⚠️ 巡检间隔必须小于 interval_hours 才有意义（用户明确要求）——
    #    否则等巡检到点时下一轮调度也到期了，巡检等于没做。
    "patrol_enabled": True,        # 巡检总开关
    "patrol_interval_minutes": 60, # 巡检周期（分钟），默认 1 小时
    # ---- 签到 / 任务调度拆分（用户 2026-10-16 需求）----
    # 「自动签到和任务调度混合，需要你在每日任务界面将其分开，用户可以分别
    #  配置，避免签到和任务相互影响」。
    # 做法：两套独立的 enabled + interval_hours，跑的时候给 vendor 脚本
    # 传不同的任务过滤参数（--tasks / --skip-tasks），互不干扰：
    #   · 签到轮 = 只跑 checkin（每日签到）
    #   · 任务轮 = 跑除 checkin 以外的全部（成长/玩法/开学季/领奖）
    # 任意一边单独停用，不影响另一边；两边都停用则完全不自动跑。
    "checkin_schedule_enabled": True,   # 签到定时开关
    "checkin_interval_hours": 12,       # 签到轮间隔（小时）
    "task_schedule_enabled": True,      # 任务定时开关
    "task_interval_hours": 12,          # 任务轮间隔（小时）
    # 拆分后各自记录上次执行时间（与旧 last_run_at 并存，互不覆盖）
    "checkin_last_run_at": None,
    "task_last_run_at": None,
}

# 巡检周期下限（分钟）：设得比这更短会让循环空转，没有实际意义。
PATROL_MIN_INTERVAL_MINUTES = 5
# 巡检周期上限（分钟）：超过 1 天就失去「快速恢复」的意义。
PATROL_MAX_INTERVAL_MINUTES = 1440

# 签到轮的任务代号（vendor 脚本 want() 的白名单）。
CHECKIN_TASK_CODES = ("checkin",)
# 任务轮要排除的代号 = 签到 —— 用黑名单而不是白名单枚举，
# 因为任务轮要跑的是「除签到以外的全部」，上游新增任务时不用回来改这里。
TASK_ROUND_SKIP_CODES = ("checkin",)

# 冷配置：模块导入时读一次的 env 常量（改了**必须重启容器**才生效）。
# 与「面板配置热生效」区分开 —— 用户改了 env 却期待立即生效，是最常见的误解。
_COLD_ENV_KEYS = (
    "AB_DATA_DIR",     # 数据目录（模块级读一次）
    "WB_DAILY_PORT",   # 本服务端口（__main__ 读）
)
# 参与账号**自动关联账号池**（国内版 cn 账号），不提供手动加入入口。
# 历史上曾有过 extra_accounts（手填「手机号:RT」），已移除：账号池本身就是
# 唯一权威来源，再开一个手填入口只会让「哪些账号在跑」出现两个答案。

# 上游脚本支持的任务清单（用于面板展示「有哪些任务」）。
# 来源：vendor 脚本里的任务常量，随上游版本变动时同步更新。
TASK_CATALOG = [
    {"group": "成长任务", "items": [
        {"key": "first_buddy", "name": "首只 Buddy（领养引导）", "auto": False,
         "note": "需先在桌面端/小程序完成一次领养，后续 17 项任务的前置条件"},
        {"key": "create_canvas", "name": "创建画布", "auto": True},
        {"key": "playbook_prompt", "name": "使用剧本文案", "auto": True},
        {"key": "Library_read", "name": "体验资料库", "auto": True},
        {"key": "chat_5", "name": "和 AI 聊天 5 次", "auto": True},
        {"key": "skill_1", "name": "尝鲜热门技能", "auto": True},
        {"key": "template_5", "name": "使用 5 个模板", "auto": True},
        {"key": "automation_1", "name": "设置自动化任务", "auto": True},
        {"key": "expert_5", "name": "召唤 5 次专家", "auto": True},
        {"key": "Expert_team_use_3", "name": "召唤 3 次专家团", "auto": True},
        {"key": "Expert_lighthouse", "name": "灯塔专家", "auto": True},
        {"key": "Expert_Philanthropy", "name": "公益专家", "auto": True},
        {"key": "Hp_Appearance", "name": "和平精英主题", "auto": True},
        {"key": "Buddy_App", "name": "发现应用", "auto": True},
        {"key": "Buddy_App_QQ", "name": "企鹅教师助手", "auto": True},
        {"key": "Model_chat_GLM5.2", "name": "GLM-5.2 模型对话", "auto": True},
        {"key": "black_cat", "name": "设计/自动化/灵感", "auto": True},
        {"key": "wb_wechat_oa_subscribe_task", "name": "关注官方公众号", "auto": False,
         "note": "需微信扫码关注「腾讯 WorkBuddy」公众号并满 24 小时"},
    ]},
    {"group": "互动玩法", "items": [
        {"key": "checkin", "name": "每日签到", "auto": True},
        {"key": "lottery", "name": "大转盘抽奖", "auto": True},
        {"key": "blindbox", "name": "盲盒开启", "auto": True},
        {"key": "buddy", "name": "Buddy 展示", "auto": True},
        {"key": "travel", "name": "喵喵旅行", "auto": True},
    ]},
    {"group": "开学季活动", "items": [
        {"key": "task_student_verify", "name": "微信学生认证", "auto": False,
         "note": "人工环节，需微信内完成认证"},
        {"key": "expert_use", "name": "专家使用", "auto": True},
        {"key": "share_invite", "name": "分享邀请", "auto": True},
        {"key": "chat_3_times", "name": "对话 3 次", "auto": True},
        {"key": "desktop_chat_1_time", "name": "桌面端对话", "auto": True},
    ]},
    {"group": "夜猫子", "items": [
        {"key": "night_owl", "name": "夜猫子任务（23:00–08:00）", "auto": True,
         "note": "仅在该时段内计入进度，要求真实对话；每天 1 次×累计 3 天"},
    ]},
]


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        db_cfg = db.get_config("wb_daily_config")
        if isinstance(db_cfg, dict):
            cfg.update(db_cfg)
    except Exception as e:
        print(f"[wb-daily] 读数据库配置出错: {e}")
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    cfg.update(saved)
        except Exception as e:
            print(f"[wb-daily] 读 json 配置出错: {e}")
    return cfg


def save_config(new_data: dict) -> dict:
    """合并写入：只覆盖传入的键，绝不清空其它字段（注册配置曾因此丢过数据）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    cfg.update(new_data)
    try:
        db.set_config("wb_daily_config", cfg, category="wb_daily",
                      description="WorkBuddy 每日成长任务配置")
    except Exception as e:
        print(f"[wb-daily] 写数据库配置出错: {e}")
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[wb-daily] 写 json 配置出错: {e}")
    return cfg


def _jwt_user(tok: str) -> str:
    try:
        import base64
        payload = tok.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return str(data.get("preferred_username") or data.get("username") or "")
    except Exception:
        return ""


def _jwt_exp(tok: str):
    try:
        import base64
        payload = tok.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None


def build_account_store(cfg: dict | None = None) -> dict:
    """生成上游脚本的 token 池：{user: {"refresh_token": rt, "access_token": at, "name": nickname}}。

    账号**自动关联账号池**（国内版 cn 账号），只读使用其凭据，不写回。
    """
    cfg = cfg or load_config()
    store: dict = {}
    arr = _load_pool_accounts()
    for a in arr:
        if a.get("variant") != "cn":
            continue
        rt = (a.get("refresh_token") or "").strip()
        if not rt:
            continue
        at = (a.get("access_token") or "").strip()
        user = _jwt_user(at) if at else ""
        if not user:
            user = ((a.get("email") or "").split("@")[0]
                    or (a.get("nickname") or "").strip() or "")
        if not user:
            user = "acct-%d" % (len(store) + 1)
        name = a.get("nickname") or a.get("email") or user
        store[user] = {"refresh_token": rt, "access_token": at, "name": name, "id": a.get("id")}
    return store


def _masked_account_key(label: str) -> str | None:
    """从逐账号明细的账号名里提取「可跨轮次匹配」的键。

    明细里的账号名是上游脚本脱敏后的格式（实测）：
        '1  13*******60'  → 序号 1 + 前 2 位 + 后 2 位
    账号池侧的键是**全号手机号**（如 '13666135560'）。
    两边唯一稳定的公共信息就是「前 2 位 + 后 2 位」：
        - 只用后 2 位不够 —— 22 个账号里 '03' 和 '41' 各出现 2 次（实测）；
        - 加上前 2 位后组合唯一（实测 19/19 全部 OK）。
    匹配不上时返回 None，由调用方按「未跑过」处理（宁可多跑，不能漏跑）。
    """
    m = re.match(r"^\s*\d+\s+(\d{2})\*+(\d{2})\s*$", str(label or ""))
    if not m:
        return None
    return m.group(1) + m.group(2)


def _store_account_key(user: str) -> str | None:
    """账号池侧取同样的「前2 + 后2」键；非手机号形态返回 None。"""
    u = str(user or "").strip()
    if len(u) >= 4 and u[:2].isdigit() and u[-2:].isdigit():
        return u[:2] + u[-2:]
    return None


def _already_done_accounts(job) -> set:
    """上一轮中断前已跑完的账号键集合（断点续跑用）。

    只认**最近一轮**没跑完的记录：
      - 上游脚本是「账号串行」跑的，中断时前面的账号已经真跑完了
        （明细里能查到 done/total、积分、连签等完整数据）；
      - 那些账号当天的任务已经完成，重跑一遍没有收益，只会多打一次接口；
      - 所以下一轮从「剩下的账号」继续，这就是用户要的
        「22个跑了4个能否结算这4个收益，下次自动从剩下的账号继续」。
    收益结算不在这里做 —— 已跑完的 4 个账号的积分/连签/等级
    在中断那一刻就已由 _record_task / accounts_summary 落库，
    面板的积分统计与任务台账读的就是那份数据，天然已结算。
    """
    try:
        rows = db.list_wb_daily_runs(limit=1)
    except Exception:
        return set()
    if not rows:
        return set()
    last = rows[0]
    if str(last.get("status")) == "done":
        return set()          # 上一轮整轮完成，没有「剩下」可言
    jid = last.get("job_id")
    if not jid:
        return set()
    try:
        accs = db.get_wb_daily_accounts(jid)
    except Exception:
        return set()
    done: set = set()
    for a in accs:
        k = _masked_account_key(a.get("account"))
        if k:
            done.add(k)
    if done:
        job.log_line("断点续跑：上一轮 %s 已完成 %d 个账号，本轮从剩余账号继续" % (jid, len(done)))
    return done


def accounts_preview(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    store = build_account_store(cfg)
    accounts = []
    for user, ent in store.items():
        accounts.append({
            "user": user,
            "name": ent.get("name") or user,
            "id": ent.get("id"),
            "has_rt": bool(ent.get("refresh_token")),
            "has_at": bool(ent.get("access_token")),
            "exp": _jwt_exp(ent.get("access_token") or "") or None,
        })
    cn_total = sum(1 for a in _load_pool_accounts()
                   if isinstance(a, dict) and a.get("variant") == "cn")
    return {"accounts": accounts, "total": len(accounts), "cn_total": cn_total}


# ------------------------------------------------------------------ 任务执行

class DailyJob:
    """一轮每日任务的执行记录（子进程托管 vendor 脚本）。"""

    def __init__(self, jid: str, mode: str, kind: str = "task"):
        self.id = jid
        self.mode = mode
        self.kind = kind if kind in ("checkin", "task") else "task"
        self.status = "running"
        self.progress = 0
        self.log: list[str] = []
        self.result = None
        self.created_at = time.time()
        self.finished_at: float | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.accounts_total = 0      # 本轮参与账号数
        self.accounts_done = 0       # 已跑完的账号数
        self.accounts_summary: list[dict] = []   # 逐账号汇总（落库用）
        self.tasks_summary: list[dict] = []      # 任务台账（任务维度，落库用）
        self._cur_account: dict | None = None    # 当前正在跑的账号
        self._checkin_notified = False           # 本轮签到通知是否已发（幂等）
        self._failed_notified = False            # 本轮失败分析通知是否已发（幂等）

    # 从上游脚本输出行推断进度。
    #
    # 关键词一律带 emoji 前缀做**精确子串**匹配，不用宽泛词：
    # 早先用「玩法」「抽奖」这种裸词，结果「执行模式: full（签到/玩法/领奖）」
    # 这句提示行当场把进度顶到末档 —— 明明刚开跑。所有档位子串均取自实测日志原文。
    PROGRESS_MARKS = [
        ("👥 账号数", 6),
        ("💰 积分", 16),
        ("📊 用量", 24),
        ("🌱 成长", 30),
        ("☁️ ── 云端任务", 45),
        ("🏫 ── 开学季", 72),
        ("🏫 lottery", 78),
        ("🎁 ── 领奖", 84),
        ("🏁 ", 92),
        ("签到报告", 96),
    ]

    # 账号起始行，形如 `╭─ 👤 账号1  138...`。
    # 多账号时用它把总进度推到「尚未结束」的合理区间。
    _ACCOUNT_START = "╭─ 👤 账号"

    def __init__(self, job_id: str, mode: str):
        self.id = job_id
        self.mode = mode
        self.status = "running"
        self.progress = 0
        self.log: list[str] = []
        self.result = None
        self.created_at = time.time()
        self.finished_at = None
        self.proc: asyncio.subprocess.Process | None = None
        self.accounts_total = 0      # 本轮参与账号数
        self.accounts_done = 0       # 已跑完的账号数
        self.accounts_summary: list[dict] = []   # 逐账号汇总（落库用）
        self.tasks_summary: list[dict] = []      # 任务台账（任务维度，落库用）
        self._cur_account: dict | None = None    # 当前正在跑的账号
        self._checkin_notified = False           # 本轮签到通知是否已发（幂等）
        self._failed_notified = False            # 本轮失败分析是否已发（幂等）

    def _notify_checkin_once(self) -> None:
        """本轮首个账号完成时推送一次「签到成功」通知（用户 2026-10-13 需求）。

        为何是**同步 + 守护线程**：调用点 `log_line()` 是同步函数（被
        _execute 的读行循环高频调用），不能在这里 await 协程 —— 那样协程
        永远不会执行（还会抛 RuntimeWarning）。通知要发网络请求，也不能
        同步阻塞读行循环，故丢守护线程。

        为何只发一次：一轮有 20+ 账号，逐个推会刷屏；且此刻该账号的签到
        结果已确定、积分汇总已落库，数据是准的。
        """
        if self._checkin_notified:
            return
        self._checkin_notified = True   # 先置位：即使发送失败也不重试（防刷屏）

        def _worker():
            try:
                try:
                    import notify
                except ImportError:
                    from gateway import notify  # type: ignore
                import db as _db
                summ = _db.summarize_wb_daily_credits() or {}
                checkin = float(summ.get("todayCheckinCredits") or 0)
                total = float(summ.get("totalCredits") or 0)
                remaining = 0.0
                try:
                    import credit_floor
                    bals = credit_floor._latest_balances() or {}
                    remaining = sum(v for v in bals.values() if isinstance(v, (int, float)))
                except Exception:
                    pass
                title, lines = notify.build_checkin_done(checkin, remaining, total)
                r = notify.send_scene("checkin_done", title, lines)
                if r.get("ok"):
                    self.log_line("签到通知已发送：%s" % ",".join(r.get("sent_to") or []))
            except Exception as e:
                try:
                    self.log_line(f"签到通知异常（不影响主流程）: {str(e)[:80]}")
                except Exception:
                    pass

        threading.Thread(target=_worker, name="ab-checkin-notice", daemon=True).start()

    def log_line(self, text: str):
        if len(self.log) > 800:
            self.log = self.log[-600:]
        self.log.append(f"[{time.strftime('%H:%M:%S')}] {text}")

        # 逐账号阶段跟踪：开始行建卡，🏁 汇总行收尾。
        # 面板要回答「每个账号执行了哪些」，只靠日志反解太脆，这里就地归集。
        if self._ACCOUNT_START in text:
            self.accounts_done += 0
            self._cur_account = {
                "account": text.split(self._ACCOUNT_START, 1)[-1].strip(),
                "done": 0, "total": 0, "actions": []
            }
            self.accounts_summary.append(self._cur_account)
        elif "🏁 " in text and self._cur_account is not None:
            # `🏁 138xxx: 完成1/19 等级1 剩余: a, b, c`
            m = re.search(r"完成\s*(\d+)\s*/\s*(\d+)", text)
            if m:
                self._cur_account["done"] = int(m.group(1))
                self._cur_account["total"] = int(m.group(2))
            m2 = re.search(r"等级\s*([^\s]+)", text)
            if m2:
                self._cur_account["level"] = m2.group(1)
            m3 = re.search(r"剩余:\s*(.+)$", text)
            if m3:
                self._cur_account["rest"] = [s.strip() for s in m3.group(1).split(",") if s.strip()]
            self.accounts_done += 1
            # 场景通知：签到成功（用户 2026-10-13 需求）。
            # 挂在**账号完成处**而非每个签到动作 —— 一轮有 20+ 账号，逐个推会刷屏；
            # 且此刻该账号的签到结果已确定，积分汇总也已落库，数据是准的。
            self._notify_checkin_once()
            self._cur_account = None
        elif self._cur_account is not None:
            # 记录具体的执行动作（签到、领奖、抽奖、完成等）
            if "actions" not in self._cur_account:
                self._cur_account["actions"] = []
            if any(k in text for k in ("✅签到", "🎁", "兑换", "抽奖", "完成了")):
                clean_act = text.strip()
                if clean_act and clean_act not in self._cur_account["actions"]:
                    self._cur_account["actions"].append(clean_act)

            # 任务维度台账：把这一行归到具体任务项（签到/领奖/抽奖/…）。
            # 面板「任务记录」就是靠它聚合的，不能只存账号级总数。
            self._record_task(text)

            # 账号级关键数值就地留存，供明细展示
            for key, label in (("credits", "💰 积分:"), ("usage", "📊 用量:"),
                               ("streak", "连签"), ("energy", "能量")):
                if key == "streak":
                    m = re.search(r"连签(\d+)天", text)
                    if m:
                        self._cur_account["streak"] = m.group(1)
                elif key == "energy":
                    m = re.search(r"能量(\d+)", text)
                    if m:
                        self._cur_account["energy"] = m.group(1)
                elif label in text:
                    self._cur_account[key] = text.split(label, 1)[-1].strip()

        pct = 0
        for keyword, mark in self.PROGRESS_MARKS:
            if keyword in text:
                pct = mark
        # 账号内部阶段（按已完账号数）+ 账号内小阶段，构成平滑推进：
        # 已完账号占 45%，当前账号的小阶段占剩下的额度。
        if self.accounts_total > 0:
            ratio = min(1.0, self.accounts_done / self.accounts_total)
            pct = max(pct, int(6 + ratio * 86))
        if pct > self.progress:
            self.progress = min(pct, 99)

    # 日志关键词 → 任务项。子串取自 vendor 脚本实测输出原文。
    TASK_SIGNATURES = [
        ("✅签到", "checkin", "每日签到"),
        ("🎁领奖", "claim", "自动领奖"),
        ("🎁 ", "claim", "自动领奖"),
        ("大转盘", "lottery", "大转盘抽奖"),
        ("抽奖", "lottery", "大转盘抽奖"),
        ("盲盒", "blindbox", "盲盒开启"),
        ("喵喵旅行", "travel", "喵喵旅行"),
        ("旅行", "travel", "喵喵旅行"),
        ("📋批量接受", "accept_tasks", "批量接受任务"),
        ("Buddy", "buddy", "Buddy 展示"),
        ("💬", "chat", "对话任务"),
    ]

    def _record_task(self, text: str) -> None:
        """把一行日志归到具体任务项，追加到任务台账。

        判定顺序即优先级：一行同时命中多个关键词时取最具体的那个（签到 > 领奖）。
        """
        task_key = task_name = None
        for kw, key, name in self.TASK_SIGNATURES:
            if kw in text:
                task_key, task_name = key, name
                break
        if not task_key:
            return

        account = (self._cur_account or {}).get("account") or "-"

        # 结果判定（用户 2026-09-27 反馈「除批量接受外完成度全是 0」）。
        #
        # 根因：原判定只认 "✅" / "成功" / "+ "（加号带空格）三个信号，
        # 但上游脚本的真实成功文案几乎都不在里面：
        #   📦盲盒: 暗影喵(SR)              ← 开出稀有猫，无关键词 → 误判 info
        #   🎁领奖[first_buddy]: +300积分    ← 加号后无空格 → 误判 info
        #   Buddy_App_QQ: buddyapp 五连 OK   ← OK 不在名单 → 误判 info
        # 只有「📋批量接受 … → 成功 N」恰好含"成功"二字，所以唯独它有完成度。
        #
        # 修正策略：**先排除再肯定**。
        # 1) 否定词优先 —— "能量不足"、"无次数"、"跳过"、"not completed" 等
        #    即使含 "OK"/"+" 也不能算成功（如 "first_buddy task not completed yet (credit=+0)"）；
        # 2) 再按顺序判 failed → already → success；
        # 3) 成功信号改为**带具体形态**：稀有度 \(R|SR|SSR|UR\)、正的积分增量 \+[1-9]、OK/五连、抽取/获得。
        result = "info"

        # --- 1) 否定信号：本行并没有真正做成 ---
        NEGATIVE = (
            "能量不足", "无次数", "无需抽", "跳过", "余额=0", "未到",
            "✗", "×", "not_accepted", "not completed", "失败", "异常", "错误", "超时",
            "timeout", "error", "无法登记", "仍无 Buddy",
        )
        has_negative = any(k in text for k in NEGATIVE)

        # 注意：「无法登记」算失败（登记这一步确实没做成）；
        # 而 "not completed yet" 只是**待完成**（如首只 Buddy 尚未满足条件），
        # 语义上是 info，不能误算成失败把完成度抹黑。
        if any(k in text for k in (
            "失败", "异常", "错误", "超时", "✗", "error", "timeout", "无法登记",
        )):
            result = "failed"
        elif any(k in text for k in ("已签到", "已领过", "已领", "already", "请明天再来")):
            result = "already"
        elif not has_negative:
            # --- 2) 成功信号 ---
            success = False
            # 抽卡类：开出具体稀有度角色
            if re.search(r"(喵|伙伴|卡)[（(](SSR|SR|UR|R)[）)]", text):
                success = True
            # 奖励类：正的积分/能量增量（\+0 不算，那是未完成的任务说明）
            elif re.search(r"\+[1-9]\d*\s*(积分|能量|credits?|energy)", text, re.I):
                success = True
            # 明确文案
            elif any(k in text for k in ("✅", "成功", "五连 OK", "OK", "获得", "抽中", "已到账", "领取成功")):
                success = True
            # 旅行类：已进入行程（有到达倒计时）
            elif re.search(r"旅行中|约\s*\d+\s*分钟后到达|即将到达", text):
                success = True
            if success:
                result = "success"

        streak = None
        m = re.search(r"连签(\d+)天", text)
        if m:
            streak = int(m.group(1))

        row = {
            "account": account,
            "task_key": task_key,
            "task_name": task_name,
            "group_name": "互动玩法" if task_key in ("checkin", "lottery", "blindbox", "buddy", "travel") else "成长任务",
            "result": result,
            "detail": text.strip()[:300],
            "streak_days": streak,
            "occurred_at": time.time(),
        }
        self.tasks_summary.append(row)

    def finish(self, status: str, result):
        self.status = status
        self.result = result
        self.progress = 100 if status == "done" else self.progress
        self.finished_at = time.time()

    def to_dict(self) -> dict:
        return {
            "job_id": self.id,
            "mode": self.mode,
            "status": self.status,
            "progress": f"{self.progress}%",
            "log": self.log[-120:],
            "result": self.result,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "accounts_total": self.accounts_total,
            "accounts_done": self.accounts_done,
            "steps": [f"👥 参与账号 {self.accounts_total} 个",
                      f"✅ 已完成 {self.accounts_done} / {self.accounts_total}"]
                     + ([f"▶ 当前：{self._cur_account['account']}"] if self._cur_account else []),
        }


JOBS: dict[str, DailyJob] = {}
_last_run_at: float | None = None


def _tail_summary(lines: list[str], n: int = 6) -> list[str]:
    kept = [ln for ln in lines if ln.strip()]
    return kept[-n:]


async def _retry_accept_pending(job: DailyJob, store: dict) -> int:
    """补登记：重试脚本运行期被「前置未满足」拒掉的任务登记。

    实证依据（2026-10-09 job 285268d7eb92，账号 15828020689，完成度仅 1/18）：
    vendor 日志里 16 项报 `prerequisite not met: first_buddy`，脚本据此判定
    「服务端仍无 Buddy 实例」并**永久放弃**；但任务结束后实测：

      - GET /buddy/visible            → has_buddy = **true**（实例其实已存在）
      - first_buddy 任务              → accept_status = claimed，进度 1/1
      - 对那 7 个被拒任务逐个 accept  → **7/7 返回 200 accepted**，回读全部 accepted

    ⇒ 拒绝是**时序问题**（登记那一瞬前置尚未满足），不是任务不可完成。
    但 vendor 一旦放弃就不再重试，这些任务会一直卡在 not_accepted，
    每轮重跑都被同一条理由拒掉 —— 这正是「完成度卡在 1/18」的真因。

    本函数在补领**之前**运行，把所有 not_accepted 的任务重新登记一遍。
    登记成功后任务进入 accepted，下一轮脚本就会对它执行上报动作，进而完成并领奖。

    保守边界（与补领一致）：
    - 只重试 accept_status == not_accepted 的任务；未下发的任务码不管；
    - 逐个 accept + 1s 间隔，不给上游压力；
    - 任何异常都不影响主流程（只记日志）。
    """
    try:
        import httpx
    except Exception as e:
        job.log_line("[re-accept] 缺少 httpx，跳过: %s" % str(e)[:120])
        return 0

    # AT 来源同补领：优先读 vendor 运行期刷新写回的 token
    refreshed = {}
    try:
        tf = WORK_DIR / "wb_refresh_tokens.json"
        if tf.exists():
            refreshed = json.load(open(tf, encoding="utf-8")) or {}
    except Exception:
        refreshed = {}

    ua = "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1"
    base = "https://www.workbuddy.cn"
    accepted = 0
    try:
        async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
            for user, ent in (store or {}).items():
                at = ""
                r = refreshed.get(user)
                if isinstance(r, dict):
                    at = (r.get("access_token") or "").strip()
                if not at:
                    at = (ent.get("access_token") or "").strip()
                if not at:
                    continue
                headers = {
                    "Authorization": "Bearer " + at,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/plain, */*",
                    "Origin": base,
                    "Referer": base + "/profile/growth-center",
                    "User-Agent": ua,
                }
                try:
                    r0 = await client.get(base + "/v2/activity/growth/tasks", headers=headers)
                    t0 = ((r0.json() or {}).get("data") or {}).get("tasks") or []
                except Exception:
                    continue
                codes = [t.get("task_code") for t in t0
                         if isinstance(t, dict)
                         and t.get("accept_status") == "not_accepted"
                         and t.get("task_code")]
                if not codes:
                    continue
                job.log_line("[re-accept] %s: 未登记 %d 项，重试登记" % (user, len(codes)))
                got = 0
                for code in codes:
                    try:
                        ar = await client.post(
                            base + "/v2/activity/growth/tasks/accept",
                            json={"task_codes": [code]}, headers=headers, timeout=15.0)
                        if ar.status_code != 200:
                            continue
                        d = ar.json() or {}
                        results = (d.get("data") or {}).get("results") or []
                        st = (results[0].get("status") or "") if results else ""
                        if st == "accepted":
                            got += 1
                    except Exception:
                        continue
                    await asyncio.sleep(1.0)
                if got:
                    accepted += got
                    job.log_line("[re-accept] %s: 补登记成功 %d 项" % (user, got))
    except Exception as e:
        job.log_line("[re-accept] 整体异常（不影响主流程）: %s" % str(e)[:120])
    return accepted


async def _wait_and_claim_pending(job: DailyJob, store: dict) -> int:
    """等「异步计分落定」后补领奖（本地补丁，不改 vendor 脚本）。

    背景（调研确认）：vendor 脚本在 run_account 末尾一次性领走当时已 completed 的任务，
    但上游进度是**异步累加**的 —— 脚本跑完时仍是 accepted / in_progress 的任务，
    可能几秒后才达标，而脚本已经退出，于是这些任务永远不会被领。

    本函数在 vendor 子进程**返回之后**运行，只补领「脚本结束时还没达标、后来变 completed」的。

    ⚠️ AT 来源必须有优先级（实测踩点）：vendor 运行期会 refresh 出新 AT 并写回
    WORK_DIR/wb_refresh_tokens.json（vendor 343-344 行），而服务层的 store 里仍是
    **脚本启动前的旧 AT** —— 直接用旧 AT 调接口会 401。⇒ 优先读刷新后的 token 文件，
    读不到才回退 store 的旧 AT。

    保守边界：
    - 只处理脚本退出时处于 accepted / in_progress 的任务码（vendor 领过的都已 claimed，不会重复领）；
    - 最多 5 轮 × 2s 轮询，整体约 10 秒，绝不拖长任务；
    - 任何异常都不影响主流程（只记日志）。
    """
    try:
        import httpx
    except Exception as e:
        job.log_line("[wait-claim] 缺少 httpx，跳过: %s" % str(e)[:120])
        return 0

    # 优先读 vendor 运行期刷新后写回的 token（含新 AT）
    refreshed = {}
    try:
        tf = WORK_DIR / "wb_refresh_tokens.json"
        if tf.exists():
            refreshed = json.load(open(tf, encoding="utf-8")) or {}
    except Exception:
        refreshed = {}

    ua = "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1"
    base = "https://www.workbuddy.cn"
    claimed = 0
    try:
        async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
            for user, ent in (store or {}).items():
                # AT 来源：刷新后的优先，旧 AT 兜底
                at = ""
                r = refreshed.get(user)
                if isinstance(r, dict):
                    at = (r.get("access_token") or "").strip()
                if not at:
                    at = (ent.get("access_token") or "").strip()
                if not at:
                    continue
                headers = {
                    "Authorization": "Bearer " + at,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/plain, */*",
                    "Origin": base,
                    "Referer": base + "/profile/growth-center",
                    "User-Agent": ua,
                }
                try:
                    r0 = await client.get(base + "/v2/activity/growth/tasks", headers=headers)
                    t0 = ((r0.json() or {}).get("data") or {}).get("tasks") or []
                except Exception:
                    continue
                pending = [t.get("task_code") for t in t0
                           if isinstance(t, dict)
                           and t.get("accept_status") in ("accepted", "in_progress")
                           and t.get("task_code")]
                if not pending:
                    continue
                job.log_line("[wait-claim] %s: 待落定 %d 项" % (user, len(pending)))
                for _round in range(5):
                    await asyncio.sleep(2)
                    try:
                        r = await client.get(base + "/v2/activity/growth/tasks", headers=headers)
                        tasks = ((r.json() or {}).get("data") or {}).get("tasks") or []
                    except Exception:
                        continue
                    m = {t.get("task_code"): t for t in tasks if isinstance(t, dict)}
                    ready = [c for c in pending
                             if (m.get(c) or {}).get("accept_status") in ("completed", "claimed")]
                    if not ready:
                        continue
                    for code in ready:
                        try:
                            cr = await client.post(
                                base + "/activity/growth/tasks/%s/claim" % code,
                                json={}, headers=headers, timeout=15.0)
                            if cr.status_code == 200:
                                claimed += 1
                                job.log_line("[wait-claim] 补领成功 %s" % code)
                        except Exception as e:
                            job.log_line("[wait-claim] %s 领奖异常: %s" % (code, str(e)[:60]))
                    pending = [c for c in pending if c not in ready]
                    if not pending:
                        break
    except Exception as e:
        job.log_line("[wait-claim] 整体异常（不影响主流程）: %s" % str(e)[:120])
    return claimed


async def _execute(job: DailyJob):
    """生成 token 池并子进程执行 vendor 脚本（默认完整任务模式）。"""
    global _last_run_at
    cfg = load_config()
    store = build_account_store(cfg)
    if not store:
        job.log_line("没有可参与的账号（账号池无 cn 账号，也未配置补充账号）")
        job.finish("failed", "无可用账号")
        return

    # 断点续跑（用户 2026-10-16 需求）：「如果没跑完的比如22个跑了4个能否结算
    # 这4个收益，下次自动从剩下的账号继续」。
    # 上一轮是中断结束（failed / aborted 且非整轮 done）时，把这轮已经跑完的
    # 账号从本轮剔除 —— 上游脚本按账号串行执行，中断点之前的账号当天任务
    # 已经真做完，明细也早已落库（积分/连签/等级/任务台账），
    # 所以「结算这 4 个收益」不需要额外动作，数据已经在库里；
    # 本轮只需要把剩下的 18 个跑完。
    # ⚠️ 只认最近一轮：更早的轮次状态可能与当前账号池不一致，盲目跳过会漏跑。
    resume_skip = _already_done_accounts(job)
    if resume_skip:
        kept = {u: e for u, e in store.items()
                if (_store_account_key(u) or u) not in resume_skip}
        if kept and len(kept) < len(store):
            job.log_line("本轮参与账号 %d → %d（跳过上一轮已完成的 %d 个）"
                         % (len(store), len(kept), len(store) - len(kept)))
            store = kept
        elif not kept:
            job.log_line("上一轮已全部完成，无需续跑")
            job.finish("done", {"detail": "上一轮已全部完成，无需续跑"})
            _persist_run(job, [])
            return
        # 一个都没匹配上时保持全量 —— 宁可重跑，不能漏跑
    if not VENDOR_SCRIPT.exists():
        job.log_line(f"内置脚本缺失: {VENDOR_SCRIPT}")
        job.finish("failed", "vendor 脚本缺失")
        return

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    script_copy = WORK_DIR / "workbuddy_daily.py"
    shutil.copy2(VENDOR_SCRIPT, script_copy)
    token_file = WORK_DIR / "wb_refresh_tokens.json"
    with open(token_file, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=1)
    job.log_line(f"参与账号 {len(store)} 个（账号池 cn + 补充账号）")
    job.accounts_total = len(store)
    run_mode = "query" if str(cfg.get("run_mode") or "full").strip().lower() == "query" else "full"
    script_args = [sys.executable, str(script_copy), "--no-desktop"]
    if run_mode == "query":
        script_args.append("--query")
    job.log_line(f"执行模式: {run_mode}（full=签到/玩法/领奖全量，query=仅查询）")

    # 签到 / 任务拆分（用户 2026-10-16 需求）：按本轮 kind 给 vendor 传任务过滤。
    #   checkin 轮 → --tasks checkin        （只跑每日签到）
    #   task 轮    → --skip-tasks checkin   （跑除签到以外的全部）
    # 手动执行的「立即执行」按钮不带 kind（默认 task），行为与拆分前的完整轮一致。
    # ⚠️ 必须走命令行参数而不是只设 env：env 在容器里可能已被外部注入
    #    （WORKBUDDY_TASKS），命令行参数在 vendor 里是后解析的，优先级更高。
    if job.kind == "checkin":
        script_args += ["--tasks", ",".join(CHECKIN_TASK_CODES)]
        job.log_line("任务范围：仅每日签到（签到/任务拆分，本轮为签到轮）")
    else:
        script_args += ["--skip-tasks", ",".join(TASK_ROUND_SKIP_CODES)]
        job.log_line("任务范围：除每日签到以外的全部任务（签到/任务拆分，本轮为任务轮）")

    env = dict(os.environ)
    env["TZ"] = env.get("TZ", "Asia/Shanghai")
    env["PYTHONIOENCODING"] = "utf-8"
    # 非 TTY 子进程 stdout 默认全缓冲，不关缓冲的话日志会卡到进程退出才一次性吐出。
    env["PYTHONUNBUFFERED"] = "1"
    # 上游脚本按「每行 手机号:AT:RT」解析该变量；池内账号走 wb_refresh_tokens.json，
    # 这里仍同步注入一份，保证脚本自举逻辑两条路都通。
    env["WORKBUDDY_REFRESH_TOKEN"] = "\n".join(
        f"{user}:{ent.get('access_token', '')}:{ent['refresh_token']}"
        if ent.get("access_token") else f"{user}:{ent['refresh_token']}"
        for user, ent in store.items()
    )

    job.log_line("启动内置脚本")
    try:
        proc = await asyncio.create_subprocess_exec(
            *script_args,
            cwd=str(WORK_DIR), env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as e:
        job.log_line(f"启动失败: {e}")
        job.finish("failed", f"{type(e).__name__}: {e}")
        return
    job.proc = proc
    all_lines: list[str] = []
    # ⚠️ 超时判定改为「按账号」而非「总时间」（用户 2026-10-13 指出）：
    # 此前写死 30 分钟总 deadline —— 账号池涨到 22 个后单轮必然超 30 分钟，
    # 于是每轮都被强杀、永远 fail。账号越多炸得越狠，这是设计缺陷。
    #
    # 现在的语义：**单个账号**超过 PER_ACCOUNT_TIMEOUT 没产出新行才算卡住 → 杀。
    # 总轮次时长 = 账号数 × 每账号耗时，随账号数自然伸缩，不再有总上限。
    # 单个账号真的卡死（上游不返回 / 网络黑洞）仍能被切掉，不会拖死整轮。
    per_account_timeout = float(os.getenv("WB_DAILY_ACCOUNT_TIMEOUT", "180") or 180)
    last_activity = time.monotonic()
    rc = None
    try:
        while True:
            idle = time.monotonic() - last_activity
            if idle >= per_account_timeout:
                raise TimeoutError()
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=per_account_timeout - idle)
            except asyncio.TimeoutError:
                raise TimeoutError()
            if not line:
                break
            last_activity = time.monotonic()   # 有产出 = 账号在推进，重置计时
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                all_lines.append(text)
                job.log_line(text)
        rc = await proc.wait()
    except TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        job.log_line("执行超时（单账号 %.0f 秒无产出），已强制终止" % per_account_timeout)
        job.finish("failed", "执行超时")
        # 必须落库：否则被强杀的这轮在面板「执行记录」里完全看不到 ——
        # 用户点了执行、跑了半小时，历史里却一条记录都没有
        # （2026-10-13 实测 job 29302d1e93a8 正是因此 runs/tasks 双 0）。
        _persist_run(job, all_lines)
        return
    except asyncio.CancelledError:
        try:
            proc.kill()
        except Exception:
            pass
        job.log_line("任务已手动中止")
        job.finish("aborted", "手动中止")
        # 同超时分支：中止也要留痕，否则「我中止了但记录里没有」。
        _persist_run(job, all_lines)
        raise
    finally:
        job.proc = None

    if rc is not None and rc < 0:
        job.log_line("任务已被中止")
        job.finish("aborted", "手动中止")
        _persist_run(job, all_lines)
        return
    if rc == 0:
        job.log_line("本轮每日任务执行完成")
        # ① 补登记：脚本运行期被「前置未满足」拒掉的任务，此刻前置往往已满足，重试一次。
        #    实证（2026-10-09）：被拒的 7 项在任务结束后逐个 accept 全部 200/accepted ——
        #    拒绝是时序问题，不是任务不可完成。登记成功后下一轮才会执行上报动作。
        try:
            n_acc = await _retry_accept_pending(job, store)
            if n_acc:
                job.log_line(f"本轮补登记 {n_acc} 项（前置未满足被拒，现已登记）")
        except Exception as e:
            job.log_line(f"补登记异常（不影响主流程）: {str(e)[:80]}")
        # ② 补领「脚本退出时还没达标、几秒后才完成」的任务（vendor 已领过的都已 claimed，不会重复领）
        try:
            extra = await _wait_and_claim_pending(job, store)
            if extra:
                job.log_line(f"本轮补领 {extra} 项（等待异步计分落定后）")
        except Exception as e:
            job.log_line(f"补领异常（不影响主流程）: {str(e)[:80]}")
        job.finish("done", {"rc": 0, "summary": _tail_summary(all_lines)})
        # ③ 场景通知：每日任务完成（用户 2026-10-13 需求）。
        #    失败只记日志，绝不影响任务主流程。
        try:
            await _notify_task_done(job, store)
        except Exception as e:
            job.log_line(f"任务完成通知异常（不影响主流程）: {str(e)[:80]}")
    else:
        job.log_line(f"脚本退出码 {rc}，请查看日志定位失败账号")
        job.finish("failed", {"rc": rc, "summary": _tail_summary(all_lines)})
    _persist_run(job, all_lines)
    # 失败即推分析（成功走 task_done，两者互不重叠）。
    # 放在 _persist_run 之后：确保落库完成再推，面板与推送数据一致。
    if job.status != "done":
        try:
            await _notify_task_failed(job)
        except Exception as e:
            job.log_line(f"失败分析推送异常（不影响主流程）: {str(e)[:80]}")


async def _notify_task_failed(job: DailyJob) -> None:
    """推送「任务失败分析」场景通知（2026-10-14 用户确认「需要」）。

    与 task_done 的分工：task_done 发**事实**（积分/账号数），本场景发**分析**
    （为什么失败 + 怎么办）。两者内容不重叠，不会对同一事件重复推送。

    归因逻辑与助手工具 `diagnose_task_failure` 共用
    `notify._classify_task_failure`，保证「问助手」与「主动推送」口径一致。

    幂等：`_failed_notified` 保证一轮只发一次（失败路径有多处出口）。
    异常一律吞掉 —— 通知是附属功能，绝不能影响任务主流程。
    """
    if job._failed_notified:
        return
    job._failed_notified = True
    try:
        try:
            import notify
        except ImportError:
            try:
                from gateway import notify  # type: ignore
            except ImportError:
                return
        # 失败原因：result 可能是 dict（含 rc/summary）或纯字符串
        detail = ""
        result = job.result
        if isinstance(result, dict):
            detail = str(result.get("detail") or "")
            if not detail and result.get("rc") not in (None, 0):
                detail = "脚本退出码 %s" % result.get("rc")
        else:
            detail = str(result or "")
        duration = 0.0
        if job.finished_at and job.created_at:
            duration = float(job.finished_at) - float(job.created_at)
        title, lines = notify.build_task_failed_analysis(
            detail, accounts_done=job.accounts_done,
            accounts_total=job.accounts_total, duration_sec=duration)
        r = notify.send_scene("task_failed_analysis", title, lines)
        if r.get("ok"):
            job.log_line("任务失败分析已推送：%s" % ",".join(r.get("sent_to") or []))
    except Exception as e:
        try:
            job.log_line(f"失败分析推送异常（不影响主流程）: {str(e)[:80]}")
        except Exception:
            pass


async def _notify_task_done(job: DailyJob, store: dict) -> None:
    """推送「每日任务完成」场景通知：任务积分 + 当前总积分。

    数据来源：wb-daily 自己的 credit 汇总（todayTaskCredits / totalCredits）
    与账号池余额快照（remaining）。通知模块按渠道订阅过滤后发送。
    """
    try:
        import notify
    except ImportError:
        try:
            from gateway import notify  # type: ignore
        except ImportError:
            return
    # 今日任务积分 / 总积分：从 DB 汇总读（与面板 credit-summary 同源）
    task_credits, total_credits = 0.0, 0.0
    try:
        import db as _db
        summ = _db.summarize_wb_daily_credits() or {}
        task_credits = float(summ.get("todayTaskCredits") or 0)
        total_credits = float(summ.get("totalCredits") or 0)
    except Exception:
        pass
    # 账号池剩余：读实时余额快照
    remaining = 0.0
    try:
        import credit_floor
        bals = credit_floor._latest_balances() or {}
        remaining = sum(v for v in bals.values() if isinstance(v, (int, float)))
    except Exception:
        pass
    accounts_total = len(store or {})
    title, lines = notify.build_task_done(
        task_credits, total_credits,
        {"accounts": accounts_total, "done": "%s/%s" % (job.accounts_done, job.accounts_total)})
    r = notify.send_scene("task_done", title, lines)
    if r.get("ok"):
        job.log_line("任务完成通知已发送：%s" % ",".join(r.get("sent_to") or []))


def _persist_run(job: DailyJob, all_lines: list[str]) -> None:
    """把一轮执行落库（汇总 + 逐账号明细），供面板回看历史。

    只保留最新 `db.WB_DAILY_RETAIN` 轮（默认 200），剪枝在 db 层做。
    日志只留尾部：全量日志可能几千行，存着除了撑库没别的用，
    排障真正需要的是最后那几十行结论。
    """
    try:
        db.save_wb_daily_run(
            job_id=job.id,
            mode=job.mode,
            status=job.status,
            started_at=job.created_at,
            finished_at=job.finished_at,
            accounts=job.accounts_total,
            summary=job.result if isinstance(job.result, dict) else {"detail": job.result},
            log=_tail_summary(all_lines, 200),
            account_rows=job.accounts_summary,
            task_rows=job.tasks_summary,
            kind=job.kind,
        )
    except Exception as e:
        print(f"[wb-daily] 落库执行记录失败: {e}")


async def start_job(mode: str = "manual", kind: str = "task") -> DailyJob:
    """启动一轮任务。

    ``kind`` 区分本轮要跑哪一类（用户 2026-10-16 要求签到与任务调度拆分）：
      · ``"checkin"`` —— 只跑每日签到（给 vendor 传 --tasks checkin）
      · ``"task"``     —— 跑除签到以外的全部（给 vendor 传 --skip-tasks checkin）
    巡检触发的续跑沿用上一轮的 kind，保证「签到中断就补签到、任务中断就补任务」。
    """
    running = [j for j in JOBS.values() if j.status == "running"]
    if running:
        raise HTTPException(status_code=409, detail=f"已有任务在执行（{running[0].id}）")
    job = DailyJob(uuid.uuid4().hex[:12], mode, kind=kind)
    JOBS[job.id] = job
    # 先落一条 running：进程若在中途被 SIGKILL / 容器重建，DB 里也留得下一具
    # 「没跑完」的尸体，供启动时的 _reconcile_orphan_runs() 收尾标记。
    _persist_run(job, [])
    if len(JOBS) > 20:
        for old in sorted(JOBS.values(), key=lambda j: j.created_at)[:-20]:
            JOBS.pop(old.id, None)
    asyncio.create_task(_guard(job))
    return job


async def _guard(job: DailyJob):
    try:
        await _execute(job)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        job.log_line(f"执行异常: {e}")
        job.finish("failed", f"{type(e).__name__}: {e}")
    finally:
        if job.status == "running":
            job.finish("failed", "未知退出")
        # _execute 内的失败分支已各自推送；走到这里说明是它没覆盖到的出口
        # （异常/未知退出），补推一次。幂等标记保证不会与前者重复。
        if job.status != "done" and not job._failed_notified:
            try:
                await _notify_task_failed(job)
            except Exception:
                pass
        global _last_run_at
        _last_run_at = time.time()
        # 回写持久化：重启后调度以此为基准，不会立即重跑一轮。
        # 签到/任务拆分后各自记各自的时间戳（用户 2026-10-16 需求）——
        # 若还共用 last_run_at，跑完签到就会把任务轮的下次执行时间也推后 12h，
        # 两边相互影响，正是用户要避免的。
        try:
            if job.kind == "checkin":
                save_config({"last_run_at": _last_run_at,
                             "checkin_last_run_at": _last_run_at})
            else:
                save_config({"last_run_at": _last_run_at,
                             "task_last_run_at": _last_run_at})
        except Exception:
            pass


# ── 任务巡检（用户 2026-10-16 需求）──
# 背景：任务被中断（容器重启 / 被 kill / 超时）后，原先只能等下一轮调度
# （默认 12h）才恢复 —— 中断当天的剩余账号要等到第二天才跑，直接错过当天收益。
# 巡检是一个独立于 interval_hours 的更短周期：到点就检查最近一轮是否中断，
# 是则立即从剩余账号续跑（复用断点续跑逻辑）。
# ⚠️ 巡检间隔必须小于 interval_hours 才有意义（用户明确要求）：
#    否则等巡检到点时下一轮调度也到期了，巡检等于没做。


def _last_interrupted_run() -> dict | None:
    """最近一条「没跑完」的执行记录（failed / aborted 且非整轮 done）。

    与 _already_done_accounts 的判定保持一致：只认最近一轮。
    返回 None 表示上一轮是正常完成的（或没有记录），无需巡检恢复。
    """
    try:
        rows = db.list_wb_daily_runs(limit=1)
    except Exception:
        return None
    if not rows:
        return None
    last = rows[0]
    if str(last.get("status")) == "done":
        return None
    return last


def _interrupted_run_kind(last: dict) -> str:
    """从中断记录推断它属于签到轮还是任务轮。

    优先用 ``kind`` 列（签到/任务拆分时写入，用户 2026-10-16 需求）——
    这是最直接的依据，不依赖任务台账是否已落库。
    列缺失或是拆分上线前的老记录时，退回用任务台账反推：
    明细里有签到任务就按签到轮补，否则按任务轮补
    （任务轮要跑的东西更多，补一轮不会漏签到）。
    """
    k = str(last.get("kind") or "")
    if k in ("checkin", "task"):
        return k
    jid = last.get("job_id")
    if not jid:
        return "task"
    try:
        rows = db.get_wb_daily_tasks(jid)
    except Exception:
        return "task"
    for r in (rows or []):
        if str(r.get("task_key") or "") == "checkin":
            return "checkin"
    return "task"


# 巡检循环上一次的「计划醒来时间」，仅供面板显示「下次巡检」。
# 定义必须在 _patrol_loop / health_api 之前 —— Python 的 global 声明只认
# 模块级已存在的名字，后置定义会让首次赋值直接 NameError。
_PATROL_NEXT_CHECK_AT: float | None = None


def _patrol_next_check_at():
    return _PATROL_NEXT_CHECK_AT


async def _patrol_loop():
    """巡检循环：按 patrol_interval_minutes 检查一次，发现中断就立即续跑。"""
    while True:
        try:
            cfg = load_config()
            # 巡检关着、或总调度关着，都不跑（总开关是最高优先级）
            if not cfg.get("patrol_enabled") or not cfg.get("enabled"):
                await asyncio.sleep(60)
                continue
            # 已有任务在跑就别插手 —— 续跑本身也是一轮完整任务，会互相抢
            if [j for j in JOBS.values() if j.status == "running"]:
                await asyncio.sleep(60)
                continue
            last = _last_interrupted_run()
            if last:
                kind = _interrupted_run_kind(last)
                print(f"[wb-daily] 巡检：发现中断的任务 {last.get('job_id')}"
                      f"（{last.get('status')}），以 {kind} 轮立即恢复")
                try:
                    await start_job(mode="scheduled", kind=kind)
                except HTTPException as e:
                    print(f"[wb-daily] 巡检启动失败: {e.detail}")
                except Exception as e:
                    print(f"[wb-daily] 巡检异常: {e}")
        except Exception as e:
            print(f"[wb-daily] 巡检检查异常: {e}")
        # 睡眠长度按配置走，落在合法区间内
        try:
            cfg = load_config()
            mins = float(cfg.get("patrol_interval_minutes") or 60)
        except Exception:
            mins = 60
        mins = max(PATROL_MIN_INTERVAL_MINUTES, min(PATROL_MAX_INTERVAL_MINUTES, mins))
        global _PATROL_NEXT_CHECK_AT
        _PATROL_NEXT_CHECK_AT = time.time() + mins * 60
        await asyncio.sleep(mins * 60)


async def _scheduler_loop():
    """轻量定时调度：每 5 分钟检查一次是否到期（到期 = 上次执行 + 间隔小时）。

    签到与任务已拆分（用户 2026-10-16 需求）：两套独立的 enabled + interval_hours，
    各自判断各自是否到期，互不干扰。任一边单独停用不影响另一边。
    旧字段 enabled / interval_hours 保留作为「总开关 + 兜底」：
    总开关关掉时两边都不自动跑；开着时以各自的开关为准。
    """
    while True:
        try:
            cfg = load_config()
            now = time.time()
            if cfg.get("enabled") and not [j for j in JOBS.values() if j.status == "running"]:
                # ── 签到轮 ──
                if cfg.get("checkin_schedule_enabled", True):
                    ch_base = (cfg.get("checkin_last_run_at")
                               or cfg.get("last_run_at"))
                    ch_iv = float(cfg.get("checkin_interval_hours") or 12)
                    ch_due = ch_base is None or (now - float(ch_base)) >= ch_iv * 3600
                    if ch_due:
                        print("[wb-daily] 签到轮到期，自动开始执行")
                        await start_job(mode="scheduled", kind="checkin")
                        continue     # 本轮只起一个任务，剩下的下一轮再判
                # ── 任务轮 ──
                if cfg.get("task_schedule_enabled", True):
                    tk_base = (cfg.get("task_last_run_at")
                               or cfg.get("last_run_at"))
                    tk_iv = float(cfg.get("task_interval_hours") or 12)
                    tk_due = tk_base is None or (now - float(tk_base)) >= tk_iv * 3600
                    if tk_due:
                        print("[wb-daily] 任务轮到期，自动开始执行")
                        await start_job(mode="scheduled", kind="task")
                        continue
        except Exception as e:
            print(f"[wb-daily] 调度检查异常: {e}")
        await asyncio.sleep(300)


def _reconcile_orphan_runs() -> None:
    """启动时收尾上一次进程留下的 running 僵尸记录。

    SIGKILL / 容器重建 / OOM 会让 `_guard` 的 finally 都没机会跑，
    DB 里那条 running 行就永远停在「执行中」——面板显示一个永不结束的任务，
    health 的 running 也一直为 True。启动时一次性改判为 failed。
    """
    try:
        n = db.reconcile_orphan_wb_daily_runs()
        if n:
            print(f"[wb-daily] 启动收尾：{n} 条中断的执行记录已标记为 failed")
    except Exception as e:
        print(f"[wb-daily] 启动收尾失败: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    sched = asyncio.create_task(_scheduler_loop())
    # 巡检循环（用户 2026-10-16 需求）：中断任务自动恢复，不等下一轮调度。
    patrol = asyncio.create_task(_patrol_loop())
    _reconcile_orphan_runs()
    yield
    sched.cancel()
    patrol.cancel()


app = FastAPI(title="wb-daily", lifespan=lifespan)


@app.get("/api/wb-daily/config")
async def get_config_api():
    return {"config": load_config()}


class ConfigBody(BaseModel):
    enabled: bool | None = None
    interval_hours: float | None = None
    run_mode: str | None = None
    keepalive_days: int | None = None
    lazy_refresh_hours: int | None = None
    checkin_enabled: bool | None = None
    # ---- 任务巡检（用户 2026-10-16 需求）----
    patrol_enabled: bool | None = None
    patrol_interval_minutes: int | None = None
    # ---- 签到 / 任务调度拆分（用户 2026-10-16 需求）----
    checkin_schedule_enabled: bool | None = None
    checkin_interval_hours: float | None = None
    task_schedule_enabled: bool | None = None
    task_interval_hours: float | None = None


@app.post("/api/wb-daily/config")
async def save_config_api(body: ConfigBody):
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    cfg = save_config(payload)
    return {"config": cfg, "ok": True}


@app.get("/api/wb-daily/accounts")
async def accounts_api():
    return accounts_preview()


@app.get("/api/wb-daily/catalog")
async def catalog_api():
    """返回可执行的任务清单（有哪些任务、哪些需人工）。

    面板上「每日任务提供了哪些任务」就靠它。清单是静态常量，
    放在服务端而不是前端硬编：上游脚本增减任务时只改一处。
    """
    groups = []
    total = 0
    auto = 0
    for group in TASK_CATALOG:
        items = list(group.get("items") or [])
        total += len(items)
        auto += sum(1 for i in items if i.get("auto"))
        groups.append({"group": group.get("group"), "items": items})
    return {"groups": groups, "total": total, "auto": auto, "manual": total - auto}


@app.get("/api/wb-daily/tasks")
async def tasks_api(limit: int = 200, account: str | None = None):
    """任务台账（任务维度）：每个账号的每个任务项每次执行一行。

    面板「任务记录」卡片的数据源。与 /runs 的区别：runs 是一轮一条，
    tasks 是一条任务一条，能直接回答「这个任务做了没、什么时候做的」。
    """
    return {"tasks": db.list_wb_daily_tasks(limit=limit, account=account),
            "retain": db.WB_DAILY_RETAIN}


@app.get("/api/wb-daily/credit-summary")
async def credit_summary_api():
    """每日任务积分聚合：今日 / 签到 / 任务 / 累计。

    面板「每日任务」顶部的四个数字与浮窗的「今日积分」都用它。
    数据在进程内现算（SQLite 本地读，无上游请求），成本极低。
    """
    return db.summarize_wb_daily_credits()


@app.get("/api/wb-daily/task-summary")
async def task_summary_api():
    """任务台账聚合：每个任务项最近一次结果 + 成功次数。"""
    return {"items": db.summarize_wb_daily_tasks()}


@app.get("/api/wb-daily/runs")
async def runs_api(limit: int = 20):
    """历史执行记录（SQLite 持久化，只保留最新 200 轮）。"""
    limit = max(1, min(int(limit or 20), 200))
    runs = db.list_wb_daily_runs(limit=limit)
    running = [j for j in JOBS.values() if j.status == "running"]
    return {"runs": runs, "retain": db.WB_DAILY_RETAIN,
            "running_job_id": running[0].id if running else None}


@app.get("/api/wb-daily/run-accounts/{job_id}")
async def run_accounts_api(job_id: str):
    """某一轮的逐账号明细（每个账号执行了哪些、剩下哪些）。"""
    rows = db.get_wb_daily_accounts(job_id)
    live = JOBS.get(job_id)
    return {"job_id": job_id, "accounts": rows, "count": len(rows),
            "live": live.to_dict() if live else None}


@app.post("/api/wb-daily/run")
async def run_api():
    job = await start_job(mode="manual")
    return {"job_id": job.id, "status": "started"}


@app.get("/api/wb-daily/jobs")
async def jobs_api():
    items = sorted(JOBS.values(), key=lambda j: j.created_at, reverse=True)
    return {"jobs": [j.to_dict() for j in items]}


@app.get("/api/wb-daily/status/{job_id}")
async def status_api(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job.to_dict()


@app.delete("/api/wb-daily/abort/{job_id}")
async def abort_api(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.status != "running":
        return {"ok": True, "detail": "任务已结束"}
    proc = job.proc
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass
    return {"ok": True}


@app.get("/api/wb-daily/health")
async def health_api():
    cfg = load_config()
    running = [j for j in JOBS.values() if j.status == "running"]
    now = time.time()

    def _next_due(base, interval_h):
        if base is None:
            return None
        try:
            return float(base) + float(interval_h or 12) * 3600
        except Exception:
            return None

    # ── 签到轮 / 任务轮各自的下次执行时间（用户 2026-10-16 拆分需求）──
    # 各自的时间戳缺省时退回共用 last_run_at，保证拆分上线后的第一轮
    # 不会因为新字段为空就立刻重跑一轮。
    ch_base = cfg.get("checkin_last_run_at") or cfg.get("last_run_at")
    tk_base = cfg.get("task_last_run_at") or cfg.get("last_run_at")
    ch_due = _next_due(ch_base, cfg.get("checkin_interval_hours"))
    tk_due = _next_due(tk_base, cfg.get("task_interval_hours"))

    # 巡检状态（用户 2026-10-16 需求）
    last = _last_interrupted_run()
    patrol = {
        "enabled": bool(cfg.get("patrol_enabled")),
        "interval_minutes": int(cfg.get("patrol_interval_minutes") or 60),
        "pending_run_id": (last or {}).get("job_id"),
        "pending_kind": _interrupted_run_kind(last) if last else None,
        "pending_status": (last or {}).get("status"),
        "next_check_at": _patrol_next_check_at(),
    }

    return {
        "ok": True,
        "enabled": bool(cfg.get("enabled")),
        "interval_hours": cfg.get("interval_hours"),
        "running": bool(running),
        "running_job_id": running[0].id if running else None,
        "running_kind": running[0].kind if running else None,
        "last_run_at": _last_run_at or cfg.get("last_run_at"),
        "next_due_at": _next_due(_last_run_at or cfg.get("last_run_at"),
                                 cfg.get("interval_hours")),
        # ── 签到 / 任务拆分（用户 2026-10-16 需求）──
        "checkin_schedule": {
            "enabled": bool(cfg.get("checkin_schedule_enabled", True)),
            "interval_hours": cfg.get("checkin_interval_hours"),
            "last_run_at": ch_base,
            "next_due_at": ch_due,
        },
        "task_schedule": {
            "enabled": bool(cfg.get("task_schedule_enabled", True)),
            "interval_hours": cfg.get("task_interval_hours"),
            "last_run_at": tk_base,
            "next_due_at": tk_due,
        },
        "patrol": patrol,
        # ── 热 / 冷口径 ──
        # hot：面板改的这些键**立即生效** —— _scheduler_loop 每 5 分钟重读、_execute 每次 load_config()，
        #      不需要重启（实测：改 enabled / interval_hours 后下一轮调度即按新值走）。
        # cold_env：模块导入时读一次的 env 常量，改了**必须重启容器**。
        # ⚠️ 区别这两者的意义：用户改了 env 却期待立即生效，是「配置没热生效」这类误报最常见的来源 ——
        # 先核对改的是面板项还是 env 常量，别急着改调度逻辑。
        "config_scope": {
            "hot": sorted(DEFAULT_CONFIG.keys()),
            "cold_env": list(_COLD_ENV_KEYS),
        },
    }



if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("WB_DAILY_PORT", "18093")),
                log_level="warning")
