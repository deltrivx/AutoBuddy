import db
"""GitHub 自动化注册服务（配置驱动与内置浏览器版）。

运行在 AutoBuddy 容器内部（loopback 18092），对外由 web_proxy 统一转发。
所有私有参数（邮箱 API、认证头、代理节点等）均由用户在前端「账号接入」页面自行配置，
并持久化存储在数据目录（/data/.autobuddy/gh_register_config.json），严禁在代码中
硬编码任何私有域名、密码或内部 IP。

对外路由：
  GET    /api/gh-register/config        获取当前配置
  POST   /api/gh-register/config        保存并持久化配置
  GET    /api/gh-register/browser       获取浏览器状态
  POST   /api/gh-register/browser/install 触发后台下载浏览器
  GET    /api/gh-register/browser/log   查看浏览器下载日志
  POST   /api/gh-register/jobs          启动注册任务
  GET    /api/gh-register/status/{id}   查询任务状态
  DELETE /api/gh-register/abort/{id}    中止注册任务
"""

import asyncio
import json
import os
import random
import re
import string
import time
import quopri
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import httpx
import uvicorn
from playwright.async_api import async_playwright

# ------------------------------------------------------------------ 配置持久化

DATA_DIR = Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy"))
CONFIG_FILE = DATA_DIR / "gh_register_config.json"
BROWSERS_PATH = os.getenv("PLAYWRIGHT_BROWSERS_PATH", str(DATA_DIR / "browsers"))

DEFAULT_CONFIG = {
    "mail_api_base": "",
    "mail_create_path": "/new",
    "mail_fetch_path": "/mails?address={email}",
    "mail_verify_keyword": "github",
    "mail_domains": [],
    "mail_auth_header_name": "",
    "mail_auth_header_value": "",
    "clash_rest_base": "http://127.0.0.1:9090",
    "clash_nodes": [],
    "no_switch_proxy": True,
    "google_password": "",
}


def load_config() -> dict:
    """加载配置：优先从内置 SQLite 数据库读取，其次读 json 文件，再回退环境变量。"""
    cfg = dict(DEFAULT_CONFIG)
    
    # 1. 从 SQLite 数据库读取持久化配置
    try:
        db_cfg = db.get_config("gh_register_config")
        if isinstance(db_cfg, dict):
            cfg.update(db_cfg)
    except Exception as e:
        print(f"[gh-register] 读数据库配置出错: {e}")

    # 2. 兼容旧 json 配置文件（如存在）
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    cfg.update(saved)
        except Exception as e:
            print(f"[gh-register] 读取 json 配置文件出错: {e}")

    # 3. 环境变量作为初始兜底默认值
    if not cfg["mail_api_base"] and os.getenv("CF_MAIL_API_BASE"):
        cfg["mail_api_base"] = os.getenv("CF_MAIL_API_BASE", "")
    if not cfg["mail_domains"] and os.getenv("CF_MAIL_DOMAINS"):
        cfg["mail_domains"] = [d.strip() for d in os.getenv("CF_MAIL_DOMAINS", "").split(",") if d.strip()]
    g_pw = os.environ.get("GH_REGISTER_GOOGLE_PASSWORD", "")
    if not cfg["google_password"] and g_pw:
        cfg["google_password"] = g_pw
    return cfg


def save_config(new_data: dict) -> dict:
    """保存配置并同时持久化到 SQLite 数据库与 json 文件。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    cfg.update(new_data)
    
    # 持久化到 SQLite 数据库
    try:
        db.set_config("gh_register_config", cfg, category="gh_register", description="GitHub 自动化注册与临时邮箱配置")
    except Exception as e:
        print(f"[gh-register] 写数据库配置出错: {e}")

    # 同时写文件兼容备份
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[gh-register] 写 json 文件出错: {e}")
        
    return cfg


# ------------------------------------------------------------------ 浏览器管理

_DL = {
    "status": "idle",        # idle | running | done | failed
    "log": [],
    "error": None,
    "phase": "",            # 当前阶段说明（如「下载内核」「解压安装」）
    "percent": 0.0,         # 0-100，来自 playwright 的进度条
    "received": 0,          # 已下载字节
    "total": 0,             # 总字节
    "startedAt": None,
    "finishedAt": None,
}

# playwright install 的进度行形如：
#   |■■■■■■■■        |  45% of 186.8 MiB
# 我们把它解析成结构化进度，前端就不必自己啃日志尾巴。
_DL_PROGRESS_RE = re.compile(
    r"^\s*\|?[^|]*\|?\s*(?P<pct>\d{1,3})%\s+of\s+(?P<total>[\d.]+)\s*(?P<unit>KiB|MiB|GiB|B)",
    re.IGNORECASE,
)

_UNIT_BYTES = {"B": 1, "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3}


def _parse_download_line(line: str) -> None:
    """从 playwright 输出里提取进度与阶段，写入 _DL。"""
    m = _DL_PROGRESS_RE.match(line)
    if m:
        pct = float(m.group("pct"))
        unit = m.group("unit").upper()
        total = float(m.group("total")) * _UNIT_BYTES.get(unit, 1)
        _DL["percent"] = max(0.0, min(100.0, pct))
        _DL["total"] = int(total)
        _DL["received"] = int(total * pct / 100.0)
        if "download" not in _DL["phase"]:
            _DL["phase"] = "正在下载浏览器内核"
        return

    low = line.lower()
    if "downloading" in low:
        _DL["phase"] = "正在下载浏览器内核"
    elif "extracting" in low or "installing" in low:
        _DL["phase"] = "正在解压安装"
    elif "downloaded to" in low or "install" in low and "done" in low:
        _DL["phase"] = "安装完成"


def browser_installed() -> bool:
    try:
        entries = os.listdir(BROWSERS_PATH)
    except FileNotFoundError:
        return False
    for name in entries:
        if name.startswith("chromium") and os.path.isdir(os.path.join(BROWSERS_PATH, name)):
            return True
    return False


def _browser_dirs() -> list:
    try:
        return sorted(n for n in os.listdir(BROWSERS_PATH) if n.startswith("chromium"))
    except FileNotFoundError:
        return []


async def _download_browser():
    _DL["status"] = "running"
    _DL["log"] = []
    _DL["error"] = None
    _DL["phase"] = "正在准备下载环境"
    _DL["percent"] = 0.0
    _DL["received"] = 0
    _DL["total"] = 0
    _DL["startedAt"] = time.time()
    _DL["finishedAt"] = None
    os.makedirs(BROWSERS_PATH, exist_ok=True)
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=BROWSERS_PATH)
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", "-m", "playwright", "install", "chromium",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                # 进度条行每秒会刷很多条（\r 分隔），只保留非进度行进日志，
                # 结构化进度另存 _DL，避免日志被刷屏、也避免前端读不到重点。
                if not _DL_PROGRESS_RE.match(line):
                    _DL["log"].append(line)
                    if len(_DL["log"]) > 400:
                        _DL["log"] = _DL["log"][-300:]
                _parse_download_line(line)
        code = await proc.wait()
        _DL["finishedAt"] = time.time()
        if code == 0 and browser_installed():
            _DL["status"] = "done"
            _DL["percent"] = 100.0
            _DL["phase"] = "安装完成"
        else:
            _DL["status"] = "failed"
            _DL["error"] = f"playwright install 退出码 {code}"
            _DL["phase"] = "安装失败"
    except Exception as e:
        _DL["status"] = "failed"
        _DL["error"] = f"{type(e).__name__}: {e}"
        _DL["phase"] = "安装失败"
        _DL["finishedAt"] = time.time()


# ------------------------------------------------------------------ 辅助函数

ADJECTIVES = ["happy", "lucky", "brave", "bright", "calm", "clever", "eager",
              "fierce", "gentle", "jolly", "keen", "mighty", "noble", "proud",
              "quick", "sharp", "silent", "steady", "swift", "wild"]
NOUNS = ["cat", "fox", "owl", "wolf", "bear", "hawk", "lion", "tiger",
         "raven", "eagle", "shark", "dragon", "phoenix", "falcon", "panther",
         "nova", "star", "comet", "storm", "river"]


def random_username():
    return random.choice(ADJECTIVES) + random.choice(NOUNS) + str(random.randint(1000, 9999))


def random_local(length=12):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def switch_clash_next_node(cfg: dict):
    """节点切换，用户未配置或禁用时直接跳过。"""
    if cfg.get("no_switch_proxy") or not cfg.get("clash_nodes"):
        return
    base = cfg.get("clash_rest_base")
    if not base:
        return
    try:
        with httpx.Client(timeout=5.0) as c:
            r = c.get(f"{base}/api/config")
            data = r.json().get("data", {})
            proxies = data.get("proxies", {})
            current = proxies.get("now")
            candidates = [n for n, info in proxies.items() if info.get("type") in ("ss", "vmess", "trojan")]
            if not candidates:
                return
            if current in candidates:
                target = candidates[(candidates.index(current) + 1) % len(candidates)]
            else:
                target = cfg["clash_nodes"][0] if cfg["clash_nodes"] else candidates[0]
            c.put(f"{base}/api/proxies/{target}", timeout=5.0)
            print(f"[gh-register] 已切换代理节点: {target}")
    except Exception as e:
        print(f"[gh-register] 节点切换跳过或失败: {e}")


# ------------------------------------------------------------------ 邮箱接口

async def create_email(http_client, cfg: dict):
    domains = cfg.get("mail_domains") or []
    if not domains:
        raise RuntimeError("未配置可用邮箱域名列表，请在「账号接入」配置页面添加域名")
    api_base = (cfg.get("mail_api_base") or "").rstrip("/")
    if not api_base:
        raise RuntimeError("未配置邮箱 API 基础地址，请在「账号接入」配置页面填写")

    domain = random.choice(domains)
    email = f"{random_local(12)}@{domain}"
    headers = {}
    auth_name = cfg.get("mail_auth_header_name")
    auth_val = cfg.get("mail_auth_header_value")
    if auth_name and auth_val:
        headers[auth_name] = auth_val

    create_path = cfg.get("mail_create_path") or "/new"
    url = f"{api_base}{create_path}"
    resp = await http_client.post(url, json={"name": email}, headers=headers, timeout=30)
    data = resp.json()
    if data.get("success") or data.get("ok"):
        return email
    raise RuntimeError(f"创建临时邮箱失败: {data}")


async def wait_for_verify_link(http_client, email: str, cfg: dict):
    api_base = (cfg.get("mail_api_base") or "").rstrip("/")
    if not api_base:
        raise RuntimeError("未配置邮箱 API 基础地址")

    headers = {}
    auth_name = cfg.get("mail_auth_header_name")
    auth_val = cfg.get("mail_auth_header_value")
    if auth_name and auth_val:
        headers[auth_name] = auth_val

    fetch_path = cfg.get("mail_fetch_path") or "/mails?address={email}"
    keyword = (cfg.get("mail_verify_keyword") or "github").lower()
    deadline = time.time() + 180

    while time.time() < deadline:
        try:
            req_path = fetch_path.replace("{email}", email)
            resp = await http_client.get(f"{api_base}{req_path}", headers=headers, timeout=30)
            data = resp.json()
            mails = data.get("results", data.get("mails", data.get("result", [])))
            if isinstance(mails, list):
                for mail in sorted(mails, key=lambda m: m.get("created_at", ""), reverse=True):
                    raw = mail.get("raw", "")
                    if not raw:
                        continue
                    try:
                        decoded = quopri.decodestring(raw).decode("utf-8", errors="replace")
                    except Exception:
                        decoded = raw
                    for url in re.findall(r"https://github\.com/[^\s\"'<>]+", decoded):
                        if keyword in url.lower() or "confirm" in url.lower():
                            return url.replace("&amp;", "&")
        except Exception as e:
            print(f"[gh-register] 轮询收信等待中: {e}")
        await asyncio.sleep(10)
    return None


# ------------------------------------------------------------------ 注册与自动化流程

async def handle_security_code(page, security_code: str):
    if not security_code:
        return False
    try:
        for selector in ['input[name="code"]', 'input[name="verification_code"]',
                         'input[name="otp"]', 'input[name="numeric-code"]',
                         'input[name="security_code"]', 'input[type="tel"]',
                         'input[type="number"]', 'input[autocomplete="one-time-code"]']:
            el = page.locator(selector).first
            if await el.count() > 0 and await el.is_visible():
                await el.fill(security_code)
                print("[gh-register] 已自动填入 GitHub 设备验证码")
                try:
                    await page.get_by_role("button", name=re.compile("Verify|Continue|Submit|确认", re.I)).first.click(timeout=5000)
                except Exception:
                    try:
                        await page.locator("button[type='submit']").first.click(timeout=5000)
                    except Exception:
                        pass
                await page.wait_for_timeout(3000)
                return True
    except Exception:
        pass
    return False


async def get_username(page):
    try:
        cookies = await page.context.cookies()
        for c in cookies:
            if c.get("name") == "dotcom_user" and c.get("value"):
                return c["value"]
    except Exception:
        pass
    try:
        text = await page.inner_text("body")
        m = re.search(r"Signed in as\s*@?([a-zA-Z0-9-]+)", text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


async def clear_github_cookies(ctx):
    all_cookies = await ctx.cookies()
    kept = [c for c in all_cookies if "github" not in c.get("domain", "").lower()]
    await ctx.clear_cookies()
    if kept:
        await ctx.add_cookies(kept)
    return len(all_cookies) - len(kept)


async def google_oauth_login(page, google_password: str):
    await page.goto("https://github.com/login", wait_until="domcontentloaded")
    await page.wait_for_timeout(4000)
    if "login" not in page.url and "accounts.google.com" not in page.url:
        return True

    clicked = False
    try:
        buttons = page.locator("button")
        count = await buttons.count()
        for i in range(min(count, 200)):
            btn = buttons.nth(i)
            text = (await btn.inner_text()).lower()
            if "google" in text:
                await btn.click()
                clicked = True
                break
    except Exception:
        pass
    if not clicked:
        try:
            await page.get_by_text("Continue with Google", exact=False).first.click(timeout=8000)
            clicked = True
        except Exception:
            pass
    if not clicked:
        return False

    account_clicked = False
    for _ in range(150):
        url = page.url
        if url.startswith("https://github.com/") and "accounts.google.com" not in url:
            return True
        if "accounts.google.com" in url:
            if not account_clicked:
                for sel in ['[data-identifier]', '[data-email]', "div[role='link']"]:
                    el = page.locator(sel).first
                    if await el.count() > 0 and await el.is_visible():
                        await el.click(timeout=5000)
                        account_clicked = True
                        break
                if not account_clicked:
                    el = page.locator("div:has-text('@')").first
                    if await el.count() > 0:
                        await el.click(timeout=5000)
                        account_clicked = True
            for btn_text in ["Continue", "继续", "Next", "下一步", "Allow", "同意", "Confirm"]:
                try:
                    btn = page.get_by_role("button", name=re.compile(btn_text, re.I)).first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click(timeout=3000)
                        break
                except Exception:
                    continue
            if google_password:
                try:
                    for sel in ['input[type="password"]', 'input[name="Passwd"]',
                                'input[autocomplete="current-password"]']:
                        pwd = page.locator(sel).first
                        if await pwd.count() > 0 and await pwd.is_visible():
                            await pwd.fill(google_password)
                            for btn_text in ["Next", "下一步", "Sign in", "登录"]:
                                try:
                                    btn = page.get_by_role("button", name=re.compile(btn_text, re.I)).first
                                    if await btn.count() > 0:
                                        await btn.click(timeout=3000)
                                        break
                                except Exception:
                                    continue
                            break
                except Exception:
                    pass
        await page.wait_for_timeout(2000)
    return False


async def register_github(page, security_code: str):
    if "/signup" not in page.url:
        return await get_username(page)

    username = random_username()
    await page.evaluate(f"""(() => {{
        const el = document.querySelector('input[name="user[login]"], input#user_login, input[autocomplete="username"]');
        if (!el) return false;
        const nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        nativeSetter.call(el, '{username}');
        el.dispatchEvent(new Event('input', {{bubbles: true}}));
        el.dispatchEvent(new Event('change', {{bubbles: true}}));
        return true;
    }})()""")

    await page.wait_for_timeout(1000)
    for sel in ['button:has-text("Create account")', 'button:has-text("Create Account")', 'button[type="submit"]']:
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0:
                await btn.click(timeout=5000)
                break
        except Exception:
            continue

    await handle_security_code(page, security_code)
    url = page.url
    if "github.com/" in url and "/signup" not in url:
        return await get_username(page) or username
    if "verified-device" in url or "sessions/verified" in url:
        for _ in range(300):
            await page.wait_for_timeout(1000)
            u = page.url
            if "verified-device" not in u and "sessions/verified" not in u:
                break
        return await get_username(page) or username
    return None


async def goto_github_emails(page):
    for attempt in range(3):
        try:
            await page.goto("https://github.com/settings/emails", wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(3000)
            return
        except Exception:
            if attempt == 2:
                raise
            await page.wait_for_timeout(5000)


async def manage_emails(page, http_client, new_email, cfg: dict):
    await goto_github_emails(page)
    await page.wait_for_timeout(5000)
    try:
        await page.fill('input#email, input[name="email"]', new_email)
        await page.click('button:has-text("Add")')
    except Exception as e:
        print(f"[gh-register] 添加邮箱失败: {e}")
        return False
    await page.wait_for_timeout(3000)

    link = await wait_for_verify_link(http_client, new_email, cfg)
    if link:
        try:
            await page.goto(link, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)
        except Exception as e:
            print(f"[gh-register] 打开验证链接失败: {e}")

    await goto_github_emails(page)
    await page.wait_for_timeout(5000)
    try:
        result = await page.evaluate("""(newEmail) => {
            const sel = document.querySelector('select#primary_email_select, select[name="id"]');
            if (!sel) return {error: 'no select found'};
            const opts = [...sel.options].map(o => ({value: o.value, text: o.textContent.trim()}));
            const target = opts.find(o => o.text.toLowerCase().includes(newEmail.toLowerCase()));
            return {opts, target: target ? target.value : null};
        }""", new_email)
        if result.get("target"):
            await page.select_option('select#primary_email_select, select[name="id"]', result["target"])
            await page.wait_for_timeout(3000)
    except Exception:
        pass

    await goto_github_emails(page)
    await page.wait_for_timeout(5000)
    keep_emails = [new_email.lower()]
    try:
        await page.evaluate("""(keepEmails) => {
            const forms = document.querySelectorAll('form');
            for (const form of forms) {
                const action = form.getAttribute('action') || '';
                const methodInput = form.querySelector('input[name="_method"][value="delete"]');
                if (action.includes('/emails/') && methodInput) {
                    const card = form.closest('li, div, tr');
                    const text = card ? card.textContent : '';
                    const shouldKeep = keepEmails.some(e => text.toLowerCase().includes(e));
                    if (!shouldKeep) { form.submit(); return 'submitted'; }
                }
            }
            return 'no removable old email';
        }""", keep_emails)
    except Exception:
        pass


# ------------------------------------------------------------------ 任务控制

class Job:
    def __init__(self, job_id, security_code, google_password):
        self.id = job_id
        self.security_code = security_code
        self.google_password = google_password
        self.status = "running"
        self.progress = "0%"
        self.steps = []
        self.log = []
        self.result = None
        self.browser = None

    def step(self, msg):
        self.steps.append(msg)
        self.log.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        print(f"[gh-register] {msg}")


JOBS: dict = {}


async def _run_job(job: Job):
    http_client = httpx.AsyncClient()
    cfg = load_config()
    try:
        if not browser_installed():
            job.status = "failed"
            job.result = "浏览器尚未安装：请先在「环境状态」中点击「下载浏览器」"
            job.step("中止：未检测到可用浏览器")
            return

        if not (cfg.get("mail_api_base") and cfg.get("mail_domains")):
            job.status = "failed"
            job.result = "尚未配置临时邮箱 API 或域名列表，请在「参数配置」中填写后保存"
            job.step("中止：邮箱参数未配置")
            return

        job.step("启动内置 Headless Chromium")
        async with async_playwright() as p:
            job.browser = await p.chromium.launch(headless=True)
            ctx = await job.browser.new_context()
            page = await ctx.new_page()

            await page.goto("https://github.com", wait_until="domcontentloaded")
            removed = await clear_github_cookies(ctx)
            if removed:
                job.step(f"清理 {removed} 个 GitHub Cookie 保持环境隔离")

            job.step("执行 Google OAuth 授权")
            pwd = job.google_password or cfg.get("google_password", "")
            logged_in = await google_oauth_login(page, pwd)
            if not logged_in:
                job.status = "failed"
                job.result = "Google OAuth 登录未自动完成，可能需要人工确认"
                return

            job.step("填写注册表单")
            username = await register_github(page, job.security_code)
            if not username:
                job.status = "failed"
                job.result = "GitHub 注册未完成，请检查验证码是否有效"
                return
            job.step(f"注册成功，用户名: {username}")

            job.step("生成临时域名邮箱")
            new_email = await create_email(http_client, cfg)
            job.step(f"临时邮箱: {new_email}")

            job.step("绑定域名邮箱并收取验证邮件")
            await manage_emails(page, http_client, new_email, cfg)

            job.progress = "100%"
            job.status = "done"
            job.result = {"username": username, "email": new_email}
            job.step("全部流程执行完成！")

            # 切换节点（若配置）
            switch_clash_next_node(cfg)
    except Exception as e:
        job.status = "failed"
        job.result = f"{type(e).__name__}: {e}"
        job.step(f"执行异常: {e}")
    finally:
        try:
            await http_client.aclose()
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="gh-register", lifespan=lifespan)


# ------------------------------------------------------------------ 路由定义

@app.get("/api/gh-register/config")
async def get_config_api():
    return load_config()


@app.post("/api/gh-register/config")
async def save_config_api(data: dict):
    saved = save_config(data)
    return {"ok": True, "config": saved}


@app.get("/api/gh-register/browser")
async def browser_status():
    installed = browser_installed()
    elapsed = None
    if _DL["startedAt"]:
        end = _DL["finishedAt"] or time.time()
        elapsed = round(end - _DL["startedAt"], 1)
    return {
        "installed": installed,
        "path": BROWSERS_PATH,
        "dirs": _browser_dirs(),
        "download": {
            "status": _DL["status"],
            "error": _DL["error"],
            "phase": _DL["phase"],
            "percent": round(_DL["percent"], 1),
            "received": _DL["received"],
            "total": _DL["total"],
            "elapsed": elapsed,
        },
    }


@app.on_event("startup")
async def _autostart_download_if_missing():
    """容器部署时自动补内核：未安装且没有下载在跑，就由本服务托管下载。

    这是**唯一**的自动触发点。entrypoint.sh 不再自己拉 playwright，
    避免两个进程同时下载、互相抢 __dirlock（表现为目录一直空、进度不动）。
    """
    if browser_installed():
        _DL["status"] = "done"
        _DL["percent"] = 100.0
        _DL["phase"] = "已安装"
        return
    if _DL["status"] != "running":
        asyncio.create_task(_download_browser())


@app.post("/api/gh-register/browser/install")
async def browser_install():
    if _DL["status"] == "running":
        return {"ok": True, "status": "already_running"}
    if browser_installed():
        return {"ok": True, "status": "already_installed", "dirs": _browser_dirs()}
    asyncio.create_task(_download_browser())
    return {"ok": True, "status": "started"}


@app.get("/api/gh-register/browser/log")
async def browser_log():
    installed = browser_installed()
    return {
        "status": _DL["status"],
        "error": _DL["error"],
        "installed": installed,
        "phase": _DL["phase"],
        "percent": round(_DL["percent"], 1),
        "received": _DL["received"],
        "total": _DL["total"],
        "tail": _DL["log"][-40:],
    }


class StartJob(BaseModel):
    security_code: str = ""
    google_password: str = ""


@app.post("/api/gh-register/jobs")
async def start_job(job: StartJob):
    from uuid import uuid4
    job_id = uuid4().hex
    JOBS[job_id] = Job(job_id, job.security_code, job.google_password)
    asyncio.create_task(_run_job(JOBS[job_id]))
    return {"job_id": job_id, "status": "started"}


@app.get("/api/gh-register/status/{job_id}")
async def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return {
        "job_id": job.id,
        "status": job.status,
        "progress": job.progress,
        "steps": job.steps,
        "log": job.log,
        "result": job.result,
    }


@app.delete("/api/gh-register/abort/{job_id}")
async def abort_job(job_id: str):
    job = JOBS.get(job_id)
    if job and job.browser:
        try:
            await job.browser.close()
        except Exception:
            pass
        job.status = "aborted"
    return {"ok": True}


if __name__ == "__main__":
    port = int(os.getenv("GH_REGISTER_PORT", "18092"))
    uvicorn.run(app, host="127.0.0.1", port=port)
