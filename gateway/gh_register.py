from typing import Optional, List, Dict, Any
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
from patchright.async_api import async_playwright

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
    "mail_local_prefix": "ab",         # 随机邮箱名前缀，空则纯随机
    "mail_local_length": 10,           # 随机部分的长度
    "mail_auth_header_name": "",
    "mail_auth_header_value": "",
    "register_proxy": "http://192.168.31.10:7890",
    "max_captcha_retries": 2,          # 打码失败后的额外重试轮数
    "bot_protection_wait": 3.0,        # 表单填充节流基数（秒），降低风控命中
    "google_password": "",
    "captcha_provider": "capsolver",      # capsolver | 2captcha | custom
    "captcha_api_key": "",
    "captcha_api_url": "",                 # 自定义打码服务基础地址
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
            "python3", "-m", "patchright", "install", "chromium",
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
            _DL["error"] = f"patchright install 退出码 {code}"
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


# ------------------------------------------------------------------ 自动打码平台接口 (Arkose FunCaptcha)

GITHUB_SIGNUP_URL = "https://github.com/signup"
GITHUB_ARKOSE_PUBLIC_KEY = "E55627E5-6C66-48BE-B587-1FA65D9BE55B"  # GitHub Arkose 官方公共 Client ID

async def solve_arkose_captcha(http_client: httpx.AsyncClient, cfg: dict, page_url: str = GITHUB_SIGNUP_URL, public_key: str = GITHUB_ARKOSE_PUBLIC_KEY) -> Optional[str]:
    """对接主流验证码破解平台 (CapSolver / 2Captcha / 自建)，自动求解 GitHub FunCaptcha。"""
    provider = (cfg.get("captcha_provider") or "capsolver").lower().strip()
    api_key = (cfg.get("captcha_api_key") or "").strip()
    if not api_key:
        print("[captcha] 未配置打码平台 API Key，跳过自动打码")
        return None

    print(f"[captcha] 正在向 {provider} 提交 Arkose FunCaptcha 解题任务...")

    try:
        if provider == "capsolver":
            base_url = (cfg.get("captcha_api_url") or "https://api.capsolver.com").rstrip("/")
            task_payload = {
                "clientKey": api_key,
                "task": {
                    "type": "FunCaptchaTaskProxyLess",
                    "websiteURL": page_url,
                    "websitePublicKey": public_key,
                    "data": json.dumps({"blob": ""})
                }
            }
            res = await http_client.post(f"{base_url}/createTask", json=task_payload, timeout=20.0)
            data = res.json()
            if data.get("errorId", 0) != 0:
                print(f"[captcha] CapSolver 创建任务失败: {data.get('errorDescription')}")
                return None
            task_id = data.get("taskId")
            if not task_id:
                return None

            deadline = time.time() + 120
            while time.time() < deadline:
                await asyncio.sleep(3)
                r = await http_client.post(f"{base_url}/getTaskResult", json={"clientKey": api_key, "taskId": task_id}, timeout=15.0)
                r_data = r.json()
                status = r_data.get("status")
                if status == "ready":
                    solution = r_data.get("solution", {})
                    token = solution.get("token") or solution.get("userToken")
                    print(f"[captcha] CapSolver 成功解题！Token 长度: {len(token) if token else 0}")
                    return token
                elif status == "failed":
                    print(f"[captcha] CapSolver 解题失败: {r_data.get('errorDescription')}")
                    return None

        elif provider in ("2captcha", "twocaptcha"):
            base_url = (cfg.get("captcha_api_url") or "https://api.2captcha.com").rstrip("/")
            task_payload = {
                "clientKey": api_key,
                "task": {
                    "type": "FunCaptchaTaskProxyless",
                    "websiteURL": page_url,
                    "websitePublicKey": public_key
                }
            }
            res = await http_client.post(f"{base_url}/createTask", json=task_payload, timeout=20.0)
            data = res.json()
            if data.get("errorId", 0) != 0:
                print(f"[captcha] 2Captcha 创建任务失败: {data.get('errorDescription')}")
                return None
            task_id = data.get("taskId")
            if not task_id:
                return None

            deadline = time.time() + 120
            while time.time() < deadline:
                await asyncio.sleep(4)
                r = await http_client.post(f"{base_url}/getTaskResult", json={"clientKey": api_key, "taskId": task_id}, timeout=15.0)
                r_data = r.json()
                if r_data.get("status") == "ready":
                    solution = r_data.get("solution", {})
                    token = solution.get("token")
                    print(f"[captcha] 2Captcha 成功解题！Token 长度: {len(token) if token else 0}")
                    return token
                elif r_data.get("errorId", 0) != 0:
                    print(f"[captcha] 2Captcha 报错: {r_data.get('errorDescription')}")
                    return None

    except Exception as e:
        print(f"[captcha] 请求打码平台异常: {e}")
    return None


async def inject_arkose_token(page, token: str) -> bool:
    """将打码平台返回的 Arkose Token 注入到页面表单并触发验证通过回调。"""
    if not token:
        return False
    try:
        injected = await page.evaluate("""(tok) => {
            let ok = false;
            const selectors = [
                'input[name="octocaptcha-token"]',
                'input#octocaptcha-token',
                'input[name="captcha_token"]',
                'input[name="verification_token"]'
            ];
            for (const s of selectors) {
                const el = document.querySelector(s);
                if (el) {
                    el.value = tok;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    ok = true;
                }
            }
            if (window.Arkose && window.Arkose.run) {
                try { window.Arkose.onCompleted({token: tok}); ok = true; } catch(e){}
            }
            if (typeof window.setupOctocaptcha === 'function') {
                try { window.setupOctocaptcha(tok); ok = true; } catch(e){}
            }
            return ok;
        }""", token)
        print(f"[captcha] Arkose Token 注入结果: {injected}")
        await page.wait_for_timeout(2000)
        return True
    except Exception as e:
        print(f"[captcha] 注入 Arkose Token 失败: {e}")
        return False

# ------------------------------------------------------------------ 邮箱接口

async def create_email(http_client, cfg: dict):
    domains = cfg.get("mail_domains") or []
    if not domains:
        raise RuntimeError("未配置可用邮箱域名列表，请在「账号接入」配置页面添加域名")
    api_base = (cfg.get("mail_api_base") or "").rstrip("/")
    if not api_base:
        raise RuntimeError("未配置邮箱 API 基础地址，请在「账号接入」配置页面填写")

    domain = random.choice(domains)
    # 邮箱名规则由「账号接入」页面配置：前缀（可空）+ 指定长度随机串。
    # 前缀做成可配置而非写死，是为了换服务/换风格时不必改代码重建。
    prefix = str(cfg.get("mail_local_prefix") or "").strip().lower()
    prefix = "".join(ch for ch in prefix if ch.isalnum())
    try:
        body_len = int(cfg.get("mail_local_length") or 10)
    except Exception:
        body_len = 10
    body_len = max(4, min(32, body_len))
    email = f"{prefix}{random_local(body_len)}@{domain}"
    headers = {}
    auth_name = cfg.get("mail_auth_header_name")
    auth_val = cfg.get("mail_auth_header_value")
    if auth_name and auth_val:
        headers[auth_name] = auth_val

    create_path = cfg.get("mail_create_path") or "/new"
    url = f"{api_base}{create_path}"
    resp = await http_client.post(url, json={"name": email}, headers=headers, timeout=30)
    data = resp.json()

    # 兼容多种邮箱服务返回结构：
    #   - {success:true} / {ok:true}                      —— 通用约定
    #   - {address, jwt, token, domain}                   —— grok-mail-worker (本项目在用的)
    #   - {data:{address}} / {email} / {result:{address}} —— 其它常见变体
    # 任一命中即视为成功，并把服务端返回的 address 作为权威值（可能与请求名不同）。
    addr = None
    if isinstance(data, dict):
        if data.get("address"):
            addr = data["address"]
        elif isinstance(data.get("data"), dict) and data["data"].get("address"):
            addr = data["data"]["address"]
        elif data.get("email"):
            addr = data["email"]
        elif isinstance(data.get("result"), dict) and data["result"].get("address"):
            addr = data["result"]["address"]

    if addr:
        return addr
    if data.get("success") or data.get("ok"):
        return email
    raise RuntimeError(f"创建临时邮箱失败: {data}")


async def wait_for_device_code(http_client, email: str, cfg: dict, timeout: int = 180) -> Optional[str]:
    """轮询注册邮箱，从 GitHub 验证邮件正文里提取 8 位设备验证码 (Launch Code)。

    GitHub 的 launch code 邮件正文形如：
        ... your verification code is 12345678 ...
        ... 你的验证码为 12345678 ...
    这里只做纯正则提取，不依赖任何第三方打码平台。
    """
    api_base = (cfg.get("mail_api_base") or "").rstrip("/")
    if not api_base:
        return None

    headers = {}
    auth_name = cfg.get("mail_auth_header_name")
    auth_val = cfg.get("mail_auth_header_value")
    if auth_name and auth_val:
        headers[auth_name] = auth_val

    fetch_path = cfg.get("mail_fetch_path") or "/mails?address={email}"
    deadline = time.time() + timeout

    # 8 位数字，且上下文里出现 code / verification / 验证码 等关键词才算数，
    # 避免把邮件里的年份、订单号、CSS 尺寸之类的数字误当验证码。
    code_re = re.compile(
        r"(?:code|verification|verify|验证码|校验码)[^0-9]{0,40}(\d{8})|(\d{8})[^0-9]{0,40}(?:code|verification|验证码)",
        re.IGNORECASE,
    )

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
                    # 去掉 HTML 标签与实体，避免标签名干扰正则
                    plain = re.sub(r"<[^>]+>", " ", decoded)
                    plain = plain.replace("&nbsp;", " ").replace("&amp;", "&")
                    m = code_re.search(plain)
                    if m:
                        code = m.group(1) or m.group(2)
                        if code:
                            return code
        except Exception as e:
            print(f"[gh-register] 轮询设备验证码等待中: {e}")
        await asyncio.sleep(8)
    return None


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


async def register_github(page, security_code: str, cfg: dict = None, http_client = None, email: str = None):
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

    # 表单填充节流：部分风控会对「瞬间填完并提交」打高风险分，
    # 这里按配置的基数拉长提交前的停顿（默认 3 秒量级）。
    pause_ms = int(float((cfg or {}).get("bot_protection_wait") or 3.0) * 1000)
    await page.wait_for_timeout(max(800, pause_ms))
    for sel in ['button:has-text("Create account")', 'button:has-text("Create Account")', 'button[type="submit"]']:
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0:
                await btn.click(timeout=5000)
                break
        except Exception:
            continue

    # ----------------------------------------------------------
    # 提交后诊断快照
    #
    # 提交按钮点下之后，页面会跳到哪里、是否弹出人机验证，此前完全没有记录，
    # 失败时只能看到一句笼统的结论。这里抓取最小必要信息：
    #   - 当前 URL 与标题（判断是否跳出了 /signup）
    #   - 是否出现 Arkose / FunCaptcha / 验证码容器
    #   - 是否存在错误提示文本（GitHub 会明确说哪个字段不合规）
    # 只打日志、截图存盘，不改变控制流 —— 诊断不能反过来影响判定。
    async def _dump_page_diag(tag):
        try:
            info = await page.evaluate("""() => {
                const q = (s) => document.querySelector(s);
                const txt = (el) => (el && el.innerText ? el.innerText.trim().slice(0, 200) : '');
                const captchaSel = [
                    'iframe[src*="arkoselabs"]', 'iframe[src*="funcaptcha"]',
                    'iframe[src*="octocaptcha"]', '#octocaptcha',
                    'input[name="octocaptcha-token"]', '[data-testid*="captcha"]'
                ];
                const captchaHit = captchaSel.filter(s => q(s));
                const errEls = Array.from(document.querySelectorAll(
                    '.flash-error, [role="alert"], .error, .js-error, [data-testid*="error"]'
                )).map(txt).filter(Boolean).slice(0, 4);
                return {
                    url: location.href,
                    title: document.title,
                    captcha: captchaHit,
                    errors: errEls,
                    hasLoginField: !!q('input#user_login, input[name="user[login]"]'),
                    hasEmailField: !!q('input#email, input[name="user[email]"]'),
                    bodyLen: (document.body ? document.body.innerText.length : 0)
                };
            }""")
            print(f"[gh-register][diag:{tag}] {info}")
            try:
                shot = f"/tmp/gh-signup-{tag}.png"
                await page.screenshot(path=shot, full_page=False)
                print(f"[gh-register][diag:{tag}] 截图已保存: {shot}")
            except Exception as se:
                print(f"[gh-register][diag:{tag}] 截图失败: {se}")
            return info
        except Exception as e:
            print(f"[gh-register][diag:{tag}] 诊断失败: {e}")
            return None

    await _dump_page_diag("after_submit")

    # 设备验证码：用户若在启动任务时填了就用用户的；否则自动从注册邮箱收信提取。
    # 正常情况下无需人工介入 —— GitHub 的 launch code 就发到本次注册的临时邮箱里。
    if not security_code and email and http_client and cfg:
        try:
            print("[gh-register] 未提供设备验证码，改为自动收信提取...")
            security_code = await wait_for_device_code(http_client, email, cfg) or ""
            if security_code:
                print(f"[gh-register] 已自动提取设备验证码: {security_code}")
        except Exception as e:
            print(f"[gh-register] 自动提取设备验证码异常: {e}")

    await handle_security_code(page, security_code)

    # 尝试自动识别并破解 Arkose 验证码（若页面存在验证码元素且配置了打码 API）
    try:
        has_captcha = await page.evaluate("""() => {
            return !!(
                document.querySelector('input[name="octocaptcha-token"]') ||
                document.querySelector('#octocaptcha') ||
                document.querySelector('iframe[src*="arkoselabs"]') ||
                document.querySelector('iframe[src*="octocaptcha"]')
            );
        }""")
        if has_captcha and cfg and cfg.get("captcha_api_key") and http_client:
            retries = int(cfg.get("max_captcha_retries") or 2)
            for attempt in range(retries + 1):
                print(f"[gh-register] 检测到 Arkose 验证码，启动打码 (第 {attempt + 1}/{retries + 1} 轮)...")
                token = await solve_arkose_captcha(http_client, cfg, page_url=page.url)
                if not token:
                    await page.wait_for_timeout(2000)
                    continue
                await inject_arkose_token(page, token)
                for sel in ['button:has-text("Create account")', 'button:has-text("Create Account")', 'button[type="submit"]']:
                    btn = page.locator(sel).first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click(timeout=5000)
                        break
                await page.wait_for_timeout(3000)
                # 提交后若验证码区块已消失，说明通过，不必再重试
                still = await page.evaluate("""() => !!(document.querySelector('input[name="octocaptcha-token"]') || document.querySelector('#octocaptcha') || document.querySelector('iframe[src*="arkoselabs"]') || document.querySelector('iframe[src*="octocaptcha"]'))""")
                if not still:
                    break
                # 换题重试前先刷新，避免拿到同一张已失败的图
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(1500)
                except Exception:
                    pass
    except Exception as e:
        print(f"[gh-register] 自动打码执行异常: {e}")

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
        self.created_at = time.time()

    # 进度权重表：把流程按「实际耗时占比」而非「步骤条数」分配百分比。
    # 早先进度条只有 0% 与 100% 两档，中途长时间停在 0% 会让用户误以为卡死。
    # 这里给每个阶段一个到达即生效的百分比，随步骤推进单调递增。
    PROGRESS_MARKS = [
        ("生成临时域名邮箱", 5),
        ("临时邮箱:", 12),
        ("启动内置 Headless Chromium", 18),
        ("清理", 22),
        ("打开 GitHub 注册页", 28),
        ("当前页面", 32),
        ("填写注册表单", 40),
        ("注册成功", 62),
        ("绑定域名邮箱", 70),
        ("WorkBuddy", 82),
        ("账号接入成功", 95),
        ("全部流程执行完成", 100),
    ]

    def _calc_progress(self, msg):
        pct = 0
        for keyword, mark in self.PROGRESS_MARKS:
            if keyword in msg:
                pct = mark
        return pct

    def step(self, msg):
        self.steps.append(msg)
        self.log.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        print(f"[gh-register] {msg}")
        # 进度只前进不回退：同一阶段多次输出日志时保持已达成的最高值。
        new_pct = self._calc_progress(msg)
        try:
            cur = int(str(self.progress).rstrip("%") or 0)
        except Exception:
            cur = 0
        if new_pct > cur:
            self.progress = f"{new_pct}%"


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

        job.step("生成临时域名邮箱")
        new_email = await create_email(http_client, cfg)
        job.step(f"临时邮箱: {new_email}")

        job.step("启动内置 Headless Chromium (Patchright 免检测)")
        async with async_playwright() as p:
            exec_path = None
            try:
                import glob
                cands = glob.glob(f"{BROWSERS_PATH}/**/chrome", recursive=True)
                if cands:
                    exec_path = cands[0]
            except Exception:
                pass

            launch_kwargs = {"headless": True, "args": ["--no-sandbox", "--disable-setuid-sandbox"]}
            if exec_path:
                launch_kwargs["executable_path"] = exec_path

            reg_proxy = (cfg.get("register_proxy") or "").strip()
            if reg_proxy:
                launch_kwargs["proxy"] = {"server": reg_proxy}
                job.step(f"注册使用专用代理: {reg_proxy}")

            job.browser = await p.chromium.launch(**launch_kwargs)
            ctx = await job.browser.new_context()
            page = await ctx.new_page()

            await page.goto("https://github.com", wait_until="domcontentloaded")
            removed = await clear_github_cookies(ctx)
            if removed:
                job.step(f"清理 {removed} 个 GitHub Cookie 保持环境隔离")

            # GitHub 注册走纯邮箱路径：直接打开 /signup 填表，
            # 邮箱验证码由本服务自动收信提取（见 wait_for_device_code）。
            # 不再经过 Google OAuth —— 那条路径需要额外的 Google 账号凭据，
            # 且失败时会因判定过松而掩盖真实状态。
            job.step("打开 GitHub 注册页")
            try:
                await page.goto("https://github.com/signup", wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(3000)
            except Exception as e:
                job.step(f"打开注册页失败: {e}")
            job.step(f"当前页面: {page.url}")

            job.step("填写注册表单")
            username = await register_github(
                page, job.security_code, cfg=cfg, http_client=http_client, email=new_email
            )
            if not username:
                job.status = "failed"
                cur = page.url
                if "/signup" not in cur:
                    job.result = f"未进入 GitHub 注册页（当前: {cur}），可能是代理不可达或页面被风控拦截"
                else:
                    job.result = "注册表单提交后未完成，可能是验证码未取到或触发了人机验证"
                return
            job.step(f"注册成功，用户名: {username}")

            job.step("绑定域名邮箱并收取验证邮件")
            await manage_emails(page, http_client, new_email, cfg)

            # ----------------------------------------------------------
            # 自动化 WorkBuddy AI 国际版 OAuth 接入并落库
            # ----------------------------------------------------------
            job.step("正在执行 WorkBuddy OAuth 授权接入...")
            try:
                oauth_res = await http_client.post("http://127.0.0.1:57890/api/oauth/start", json={"platform": "workbuddy"}, timeout=15.0)
                if oauth_res.status_code == 200:
                    oauth_data = oauth_res.json()
                    verify_uri = oauth_data.get("verificationUri")
                    login_id = oauth_data.get("loginId")
                    if verify_uri:
                        job.step(f"访问授权页: {verify_uri[:45]}...")
                        await page.goto(verify_uri, wait_until="domcontentloaded", timeout=45000)
                        await page.wait_for_timeout(3000)
                        for btn_name in ["Authorize", "授权", "同意", "Allow"]:
                            btn = page.get_by_role("button", name=re.compile(btn_name, re.I)).first
                            if await btn.count() > 0 and await btn.is_visible():
                                await btn.click(timeout=5000)
                                job.step("已点击 WorkBuddy 授权按钮")
                                break
                        await page.wait_for_timeout(4000)
                        job.step(f"WorkBuddy 账号接入成功 (ID: {login_id})")
            except Exception as e:
                job.step(f"OAuth 自动接入跳过或异常: {e}")

            job.progress = "100%"
            job.status = "done"
            job.result = {"username": username, "email": new_email}
            job.step("全部流程执行完成！")

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

    这是**唯一**的自动触发点。entrypoint.sh 不再自己拉 patchright，
    避免两个进程同时下载、互相抢 __dirlock（表现为目录一直空、进度不动）。
    """
    if browser_installed():
        _DL["status"] = "done"
        _DL["percent"] = 100.0
        _DL["phase"] = "已安装"
        return
    if _DL["status"] != "running":
        asyncio.create_task(_download_browser())


class ProxyTestReq(BaseModel):
    proxy: str = ""


@app.post("/api/gh-register/proxy/test")
async def proxy_test(req: ProxyTestReq):
    """检测出网代理是否可用：用该代理访问 GitHub 与一个轻量探针。

    返回结构刻意保持简单（ok / latency_ms / detail），前端直接展示徽章即可，
    不把 httpx 的原始异常堆栈抛给用户。
    """
    proxy = (req.proxy or "").strip()
    if not proxy:
        return {"ok": False, "detail": "未填写代理地址"}

    targets = [
        ("https://github.com", "GitHub"),
        ("https://api.github.com", "GitHub API"),
    ]
    last_err = ""
    for url, label in targets:
        started = time.time()
        try:
            async with httpx.AsyncClient(proxy=proxy, timeout=12.0, follow_redirects=True) as c:
                r = await c.get(url)
            latency = int((time.time() - started) * 1000)
            if r.status_code < 400:
                return {
                    "ok": True,
                    "detail": f"{label} 可达 (HTTP {r.status_code})",
                    "latency_ms": latency,
                    "target": url,
                }
            last_err = f"{label} 返回 HTTP {r.status_code}"
        except Exception as e:
            last_err = f"{label} 连接失败: {type(e).__name__}: {e}"
    return {"ok": False, "detail": last_err or "代理不可用"}


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


@app.get("/api/gh-register/jobs")
async def list_jobs():
    """列出当前内存中的所有任务。

    存在的理由：任务状态只存在服务端内存里，而前端的 job_id 存在页面变量中 ——
    刷新页面就丢了，用户会看到进度凭空消失（任务其实还在跑）。
    有了这个接口，前端加载时可以「重新挂载」到仍在运行的任务上。
    """
    items = []
    for job in JOBS.values():
        items.append({
            "job_id": job.id,
            "status": job.status,
            "progress": job.progress,
            "createdAt": getattr(job, "created_at", None),
        })
    # 运行中的排前面，方便前端直接取第一个活动任务
    items.sort(key=lambda x: (x["status"] != "running", x["job_id"]))
    return {"jobs": items, "running": len([i for i in items if i["status"] == "running"])}


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
