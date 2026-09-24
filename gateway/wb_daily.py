"""WorkBuddy 每日成长任务服务（每日签到自动化）。

运行在 AutoBuddy 容器内部（loopback 18093），对外由 web_proxy 统一转发。
功能来源：内置 vendor 化的上游开源脚本 gateway/vendor/workbuddy_daily.py
（L0NE-6/WorkBuddy-Daily，MIT），由本服务托管执行。

账号来源：AutoBuddy 账号池（/data/.wb-switch/accounts.json）中 variant=cn 的账号，
取其 refresh_token 生成上游脚本需要的 wb_refresh_tokens.json。
兼容性已实测：Keycloak 签发的 RT 可直接走上游插件的刷新接口，且刷新后旧 RT
仍然有效（REUSABLE），因此这里**只读共用**账号池凭据，绝不写回、绝不覆盖
accounts.json，无烧号风险。另支持在前端补充账号池之外的账号（extra_accounts）。

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
ACCOUNTS_JSON = Path("/data/.wb-switch/accounts.json")
VENDOR_SCRIPT = Path(__file__).resolve().parent / "vendor" / "workbuddy_daily.py"

DEFAULT_CONFIG = {
    "enabled": True,          # 定时调度总开关
    "interval_hours": 12,     # 每轮执行间隔（小时）
    "extra_accounts": [],     # 账号池之外补充账号：["手机号:RT", ...]
    "run_mode": "full",       # full=完整任务（签到/玩法/领奖） | query=仅查询
    "last_run_at": None,      # 上次执行时间戳（完成后回写，重启不重跑）
}

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
    """生成上游脚本的 token 池：{user: {"refresh_token": rt, "access_token": at}}。

    只读使用账号池凭据；extra_accounts 追加其后（同名以 extra 为准）。
    """
    cfg = cfg or load_config()
    store: dict = {}
    try:
        arr = json.load(open(ACCOUNTS_JSON, encoding="utf-8"))
    except Exception as e:
        print(f"[wb-daily] 读账号池失败: {e}")
        arr = []
    for a in arr if isinstance(arr, list) else []:
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
        store[user] = {"refresh_token": rt, "access_token": at}
    for item in cfg.get("extra_accounts") or []:
        line = str(item).strip()
        if not line:
            continue
        parts = line.split(":")
        try:
            if parts[0].startswith("eyJ"):
                user, at, rt = "", "", parts[0]
            elif len(parts) >= 3:
                user, at, rt = parts[0].strip(), parts[1].strip(), parts[2].strip()
            elif len(parts) == 2:
                user, at, rt = parts[0].strip(), "", parts[1].strip()
            else:
                user, at, rt = "", "", line
        except Exception:
            continue
        if not rt:
            continue
        if not user:
            user = _jwt_user(at) if at else "extra-%d" % (len(store) + 1)
        store[user] = {"refresh_token": rt, "access_token": at}
    return store


def accounts_preview(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    store = build_account_store(cfg)
    accounts = []
    for user, ent in store.items():
        accounts.append({
            "user": user,
            "has_rt": bool(ent.get("refresh_token")),
            "has_at": bool(ent.get("access_token")),
            "exp": _jwt_exp(ent.get("access_token") or "") or None,
        })
    cn_total = 0
    try:
        arr = json.load(open(ACCOUNTS_JSON, encoding="utf-8"))
        cn_total = sum(1 for a in arr
                       if isinstance(a, dict) and a.get("variant") == "cn")
    except Exception:
        pass
    return {"accounts": accounts, "total": len(accounts), "cn_total": cn_total}


# ------------------------------------------------------------------ 任务执行

class DailyJob:
    """一轮每日任务的执行记录（子进程托管 vendor 脚本）。"""

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
        self._cur_account: dict | None = None    # 当前正在跑的账号

    def log_line(self, text: str):
        if len(self.log) > 800:
            self.log = self.log[-600:]
        self.log.append(f"[{time.strftime('%H:%M:%S')}] {text}")

        # 逐账号阶段跟踪：开始行建卡，🏁 汇总行收尾。
        # 面板要回答「每个账号执行了哪些」，只靠日志反解太脆，这里就地归集。
        if self._ACCOUNT_START in text:
            self.accounts_done += 0
            self._cur_account = {"account": text.split(self._ACCOUNT_START, 1)[-1].strip(),
                                 "done": 0, "total": 0}
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
            self._cur_account = None
        elif self._cur_account is not None:
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


async def _execute(job: DailyJob):
    """生成 token 池并子进程执行 vendor 脚本（默认完整任务模式）。"""
    global _last_run_at
    cfg = load_config()
    store = build_account_store(cfg)
    if not store:
        job.log_line("没有可参与的账号（账号池无 cn 账号，也未配置补充账号）")
        job.finish("failed", "无可用账号")
        return
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
    deadline = time.monotonic() + 1800
    rc = None
    try:
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise TimeoutError()
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=remain)
            except asyncio.TimeoutError:
                raise TimeoutError()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                all_lines.append(text)
                job.log_line(text)
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise TimeoutError()
        rc = await asyncio.wait_for(proc.wait(), timeout=remain)
    except TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        job.log_line("执行超时（30 分钟），已强制终止")
        job.finish("failed", "执行超时")
        return
    except asyncio.CancelledError:
        try:
            proc.kill()
        except Exception:
            pass
        job.log_line("任务已手动中止")
        job.finish("aborted", "手动中止")
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
        job.finish("done", {"rc": 0, "summary": _tail_summary(all_lines)})
    else:
        job.log_line(f"脚本退出码 {rc}，请查看日志定位失败账号")
        job.finish("failed", {"rc": rc, "summary": _tail_summary(all_lines)})
    _persist_run(job, all_lines)


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
        )
    except Exception as e:
        print(f"[wb-daily] 落库执行记录失败: {e}")


async def start_job(mode: str = "manual") -> DailyJob:
    running = [j for j in JOBS.values() if j.status == "running"]
    if running:
        raise HTTPException(status_code=409, detail=f"已有任务在执行（{running[0].id}）")
    job = DailyJob(uuid.uuid4().hex[:12], mode)
    JOBS[job.id] = job
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
        global _last_run_at
        _last_run_at = time.time()
        # 回写持久化：重启后调度以此为基准，不会立即重跑一轮。
        try:
            save_config({"last_run_at": _last_run_at})
        except Exception:
            pass


async def _scheduler_loop():
    """轻量定时调度：每 5 分钟检查一次是否到期（到期 = 上次执行 + 间隔小时）。"""
    while True:
        try:
            cfg = load_config()
            if cfg.get("enabled"):
                interval_h = float(cfg.get("interval_hours") or 12)
                base = _last_run_at if _last_run_at is not None else cfg.get("last_run_at")
                due = base is None or (time.time() - float(base)) >= interval_h * 3600
                if due and not [j for j in JOBS.values() if j.status == "running"]:
                    print("[wb-daily] 到期，自动开始本轮执行")
                    await start_job(mode="scheduled")
        except Exception as e:
            print(f"[wb-daily] 调度检查异常: {e}")
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_scheduler_loop())
    yield
    task.cancel()


app = FastAPI(title="wb-daily", lifespan=lifespan)


@app.get("/api/wb-daily/config")
async def get_config_api():
    return {"config": load_config()}


class ConfigBody(BaseModel):
    enabled: bool | None = None
    interval_hours: float | None = None
    extra_accounts: list[str] | None = None
    run_mode: str | None = None


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
    next_due = None
    if cfg.get("enabled"):
        base = _last_run_at or cfg.get("last_run_at")
        if base:
            next_due = float(base) + float(cfg.get("interval_hours") or 12) * 3600
    return {
        "ok": True,
        "enabled": bool(cfg.get("enabled")),
        "interval_hours": cfg.get("interval_hours"),
        "running": bool(running),
        "running_job_id": running[0].id if running else None,
        "last_run_at": _last_run_at or cfg.get("last_run_at"),
        "next_due_at": next_due,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("WB_DAILY_PORT", "18093")),
                log_level="warning")
