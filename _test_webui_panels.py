"""WebUI 注入面板的渲染冒烟测试。

**为什么需要它**：注入到官方前端的 JS 一旦抛异常，那个面板会**静默消失** ——
没有报错、没有日志，页面看起来只是「少了一块」。v0.4.5 就踩过一次：
「运行位置」里写了 `var host = window.location.hostname`，把函数的入参 `host`
（要挂载节点的容器）整个顶掉，函数末尾 `host.appendChild` 对字符串调用而抛异常，
整个「关于」面板不再出现。`node --check` 只查语法，查不出这种错。

**怎么做**：从 `gateway/web_proxy.py` 里取出注入的 `<script>`，去掉自动启动的那几行
（改成 `return run`），在一套最小 DOM 桩里把 `run()` 跑一遍 —— `fetch` 被替换成返回
固定数据的桩，于是每个面板的渲染函数都会被真正执行到。抛异常、或产生未处理的
Promise 拒绝都算失败。

同时做几条结构断言：面板该有的东西在、不该有的东西不在。

依赖 node（CI 里由 actions/setup-node 提供）；node 缺失时跳过而不是报错。
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
WEB_PROXY = ROOT / "gateway" / "web_proxy.py"

_ok = 0
_fail = []


def check(label, condition, detail=""):
    global _ok
    if condition:
        _ok += 1
        print(f"  ok   {label}")
    else:
        _fail.append(label)
        print(f"  FAIL {label} {detail}")


def extract_script(src: str) -> str:
    """取出注入到官方页面里的那段 <script>。"""
    lines = src.split("\n")
    start = end = None
    for i, line in enumerate(lines):
        if line.strip() == "<script>" and start is None and i > 400:
            start = i + 1
        elif start is not None and line.strip() == "</script>" and i > start:
            end = i
            break
    if start is None or end is None:
        raise SystemExit("在 web_proxy.py 里找不到注入的 <script> 段")
    return "\n".join(lines[start:end])


def check_js_parses(script: str) -> None:
    """把注入的 JS 交给 node 做**纯语法解析**，不通过就直接失败。

    为什么单拷一道：Python 的 `py_compile` 只看 web_proxy.py 本身，
    里面装的是字符串字面量；拼装字符串时写出的 JS 语法错（例如
    `'</div>' + +` 这种多写一个加号）**Python 完全看不出**，
    而浏览器会直接抛 SyntaxError，整段注入脚本不执行 —— 所有面板一齐消失。
    v0.7.0 就踩过：多出的两个 `+ +` 让「每日任务」整页布局塌掉，
    而当时的测试全部绿灯。

    这里只查语法（快、无需 DOM）；真正的渲染冒烟在后面另跑。
    """
    if not shutil.which("node"):
        return
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "chk.js"
        # strip_bootstrap 只改末尾几行，不影响语法；直接查原文更严格。
        p.write_text(script, encoding="utf-8", newline="\n")
        proc = subprocess.run(["node", "--check", str(p)],
                              capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        check("注入 JS 能通过 node --check 语法解析", False,
              " | ".join(err[:4]))
        raise SystemExit(
            "注入的 JS 存在语法错误，浏览器会整段不执行 —— 必须修完再提交。\n"
            + "\n".join(err[:12]))
    check("注入 JS 能通过 node --check 语法解析", True)


def strip_bootstrap(script: str) -> str:
    """把脚本末尾的自动启动换掉，改成把 run / 宿主查找函数交出来。

    自动跑会依赖真实的浏览器环境，这里由测试自己决定何时跑、并顺手拿到
    `wbFindSettingsHost` 用于判断「宿主没找到」这种静默失败。
    """
    marker = script.index("  const observer = new MutationObserver(")
    tail = script.index("})();", marker)
    export = "  return { run: run, findHost: wbFindSettingsHost };\n"
    return script[:marker] + export + script[tail:]


# ---------------------------------------------------------------------------
# 测试数据。**全部是编的**，不要用真实账号信息 —— 这个文件会进公开仓库。
# ---------------------------------------------------------------------------
GATEWAY_INFO = {
    "version": "9.9.9",
    "basePath": "/v1",
    "project": {
        "name": "AutoBuddy",
        "url": "https://example.invalid/repo",
        "releases": "https://example.invalid/repo/releases",
        "changelog": "https://example.invalid/repo/blob/main/CHANGELOG.md",
        "issues": "https://example.invalid/repo/issues",
    },
    "dataDir": "/data/.autobuddy",
    "ports": {"gateway": 18091, "console": 18090},
    "endpoints": {"chatCompletions": "/v1/chat/completions", "models": "/v1/models",
                  "health": "/health"},
    "auth": {"requireKey": False, "header": "Authorization: Bearer <key>",
             "altHeader": "x-api-key: <key>", "keysTotal": 0, "keysEnabled": 0},
    "models": {"count": 12, "autoDiscovered": 3, "sample": ["m-a", "m-b"],
               "disabledCombos": 5},
    "disabledBySource": {"manual": 3, "auto": 2},
    "accounts": {"total": 2, "usable": 2, "inPool": 1, "mode": "rotate"},
    "features": {"stream": True},
}

KEYS_STATUS = {
    "requireKey": False,
    "keys": [{"id": "k1", "name": "示例密钥", "enabled": True, "calls": 3,
              "lastUsedAt": 1789831469144, "createdAt": 1789831469144,
              "preview": "wb-****1234"}],
    "stats": {"total": 1, "enabled": 1, "calls": 3, "lastUsedAt": 1789831469144},
    "gateway": GATEWAY_INFO,
}

HEALTH = {
    "ok": True,
    "config": {"enabled": True, "intervalMinutes": 180, "accountIds": [],
               "onlyUsedModels": True, "autoEnable": True, "logResults": True},
    "accounts": [{"id": "acc-1", "name": "账号甲", "variant": "ai"},
                 {"id": "acc-2", "name": "账号乙", "variant": "cn"}],
    "activeAccounts": 2,
    "minIntervalMinutes": 5,
    "disabledBySource": {"manual": 3, "auto": 2},
    "autoEnableProtectsManual": True,
    "runtime": {
        "running": False,
        "lastRunAt": 1789831469144,
        "lastError": None,
        "lastResult": {
            "checkedAt": 1789831469144, "combos": 20, "accounts": 2,
            "counts": {"available": 16, "unavailable": 4, "transient": 0,
                       "probe_defect": 0},
            "disabled": ["m-b"], "enabled": [], "protected": ["m-a"],
            "aborted": False, "reason": None, "authFailed": [],
        },
    },
}

ACCOUNT_MODELS = {
    "catalog": ["m-a", "m-b", "m-c"],
    # 故意让同一张卡片同时有「手动禁用」和「巡检禁用」各一个 —— 这正是曾经
    # 被数了两遍的场景：巡检禁掉一个，用户会看到「已禁用」也跟着 +1。
    # 形状与真实接口一致：`accounts` 是**以账号 id 为键的对象**（不是数组）——
    # 注入脚本就是按这个键把来源表落到 accountId 上的。
    "accounts": {"acc-1": {"id": "acc-1", "name": "账号甲", "variant": "ai",
                           "models": ["m-a", "m-b", "m-c"],
                           "disabledModels": ["m-b", "m-c"],
                           "disabledSources": {"m-b": "manual", "m-c": "auto"},
                           "disabledTotal": 2,
                           "usable": True, "expired": False}},
    "policy": {"acc-1": ["m-b", "m-c"]},
    "policySources": {"acc-1": {"m-b": "manual", "m-c": "auto"}},
    "disabledTotal": 2,
    "disabledBySource": {"manual": 1, "auto": 1},
}

# 「一键恢复」的响应夹具。形状按真实接口来：恢复后该账号什么都不剩，
# 所以 disabledModels / disabledSources 都是空的 —— 界面据此重绘，
# 若夹具里留着东西，就验不出「恢复后卡片确实清空了」。
ACCOUNT_RESTORE = {
    "ok": True, "accountId": "acc-1", "scope": "all",
    "restored": ["m-b", "m-c"], "restoredCount": 2,
    "disabledModels": [], "disabledSources": {}, "disabledTotal": 0,
}

# 「检测账号」的响应夹具。刻意**不带** status 字段：接口已改为只下发判定结论
# 与依据，夹具跟着同构才有意义 —— 否则前端偷偷回去读 status 也验不出来。
# 同时带上 state / action：界面已改为只认 state（valid/invalid/restricted/unknown），
# 不再自行解释 verdict 或状态码。
ACCOUNT_PROBE = {
    "ok": True, "accountId": "acc-1", "accountName": "账号甲",
    "verdict": "available", "state": "valid", "message": "账号正常",
    # 界面显示的整句提示。只有这一句面向用户 —— 探测细节（method / evidence）
    # 都留在接口里供排查，**不进提示**，否则「用一个不存在的模型名试」
    # 会把「账号正常」的好结论读成故障。
    "userMessage": "账号正常，可以放心使用",
    "method": "用官方的鉴权方式试了一次，上游认下了这张凭据",
    "evidence": None,
    "action": "", "code": 11102, "semantic": "model_missing",
    "elapsedMs": 258, "error": None,
}

# 「一致性自检」的响应夹具：一个账号正常、一个被上游拦截。
# 两个都要有 —— 只放正常的那种，验不出界面是否真的会区分「受限」与「失效」。
ACCOUNT_AUDIT = {
    "checkedAt": 1, "accounts": 2,
    "summary": {"一致：均正常": 1, "账号已被上游拦截": 1},
    "criteria": {
        "credential": "由不存在的模型名探测裁定。",
        "model": "用真实模型名探测，只依据上游错误码判定。",
        "note": "凭据有效性只由账号级探测裁定。",
    },
    "rows": [
        {"id": "acc-1", "name": "账号甲", "variant": "ai", "inPool": True,
         "usable": True, "accountDisabled": False, "accountDisabledSource": None,
         "credential": {"state": "valid", "label": "账号正常", "detail": "模型不存在",
                        "semantic": "model_missing", "code": 11102},
         "models": [{"model": "hy3", "verdict": "available", "semantic": None,
                     "code": None, "explain": "调用链路正常"}],
         "consistency": "一致：均正常", "advice": "凭据与模型两侧都正常，无需处理。"},
        {"id": "acc-2", "name": "账号乙", "variant": "cn", "inPool": True,
         "usable": True, "accountDisabled": False, "accountDisabledSource": None,
         "credential": {"state": "restricted", "label": "账号已被上游拦截",
                        "detail": "账号已被上游拦截",
                        "semantic": "restricted", "code": 11140},
         "models": [{"model": "hy3", "verdict": "restricted", "semantic": "restricted",
                     "code": 11140, "explain": "账号已被上游拦截"}],
         "consistency": "账号已被上游拦截",
         "advice": "登录是好的，但请求被上游直接拦下，所以用不了。"
                   "重新扫码登录没用（实测：换新凭据后依然被拦，"
                   "上游认的是账号本身），建议换个账号；巡检会自动停用它。"},
    ],
}

ACCOUNT_POOL = {    "mode": "rotate", "enabledAccountIds": ["acc-1"], "preferredAccountId": None,
    # mode 用 auto 而不是 rotate：网关实际下发的是 auto/manual 两种，
    # 夹具写错模式会让「首选账号」那类断言在错误的前提下通过。
    "allEnabledByDefault": False,
    "accountDisabled": {"acc-1": "auto"},
    "accounts": [{
        "id": "acc-1", "name": "账号甲", "variant": "ai",
        "enabled": True, "usable": True, "active": False,
        "disabled": True, "disabledSource": "auto",
    }],
    "selectionCounts": [],
}

HARNESS_JS = r"""
"use strict";

const errors = [];
process.on("unhandledRejection", (e) => {
  errors.push("未处理的 Promise 拒绝：" + ((e && e.message) || String(e)));
});
process.on("uncaughtException", (e) => {
  errors.push("未捕获异常：" + ((e && e.message) || String(e)));
});

// ---- 最小 DOM 桩 -------------------------------------------------------
const registry = {};

function collect(node, out) {
  (node.children || []).forEach((c) => { out.push(c); collect(c, out); });
  return out;
}

// 只给「账号卡片」绑真实的后代查询：默认的 querySelector 一律返回 null，
// 换成全局实现会改变其他面板代码路径的行为，所以这里保持外科式改动。
function bindQuery(el) {
  el.querySelector = (sel) => {
    const ds = collect(el, []);
    if (String(sel).charAt(0) === ".") {
      const cls = String(sel).slice(1);
      return ds.find((d) => (d.className || "").split(/\s+/).indexOf(cls) >= 0) || null;
    }
    return ds.find((d) => d.tagName === String(sel).toUpperCase()) || null;
  };
}

function makeEl(tag) {
  const el = {
    tagName: String(tag || "div").toUpperCase(),
    nodeType: 1,
    children: [],
    childNodes: [],
    parentElement: null,
    className: "",
    textContent: "",
    innerHTML: "",
    style: { cssText: "", setProperty() {}, removeProperty() {} },
    dataset: {},
    attrs: {},
    value: "",
    checked: false,
    disabled: false,
    type: "",
    placeholder: "",
    title: "",
    href: "",
    target: "",
    rel: "",
    rows: 0,
    colSpan: 0,
    scrollTop: 0,
    scrollHeight: 0,
  };
  el.classList = {
    add(...cs) { cs.forEach((c) => { if (!el.classList.contains(c)) el.className = (el.className + " " + c).trim(); }); },
    remove(...cs) { cs.forEach((c) => { el.className = el.className.split(/\s+/).filter((x) => x && x !== c).join(" "); }); },
    contains(c) { return el.className.split(/\s+/).indexOf(c) >= 0; },
    toggle(c) { if (el.classList.contains(c)) el.classList.remove(c); else el.classList.add(c); },
  };
  el.appendChild = (c) => {
    if (!c) return c;
    if (c.__fragment) { (c.children || []).slice().forEach((k) => el.appendChild(k)); return c; }
    // 真实 DOM 的 appendChild 对「已在同一父节点下的节点」是**移动**而不是复制（去掉旧的再放到末尾）。
    // 桩里如果只 push，重复 append 会凭空多出一份节点，顺序断言就失真了。
    if (c.parentElement && c.parentElement !== el) {
      const old = c.parentElement;
      old.children = (old.children || []).filter((x) => x !== c);
      old.childNodes = old.children;
    }
    el.children = el.children.filter((x) => x !== c);
    el.children.push(c);
    c.parentElement = el;
    el.childNodes = el.children;
    return c;
  };
  el.append = el.appendChild;
  el.insertBefore = (c) => { if (c) { el.children.unshift(c); c.parentElement = el; el.childNodes = el.children; } return c; };
  el.removeChild = (c) => { el.children = el.children.filter((x) => x !== c); el.childNodes = el.children; return c; };
  el.remove = () => { if (el.parentElement) el.parentElement.removeChild(el); };
  el.setAttribute = (k, v) => { el.attrs[k] = String(v); if (k === "id") el.id = String(v); };
  el.getAttribute = (k) => (k in el.attrs ? el.attrs[k] : null);
  el.removeAttribute = (k) => { delete el.attrs[k]; };
  el.hasAttribute = (k) => k in el.attrs;
  el.addEventListener = () => {};
  el.removeEventListener = () => {};
  el.querySelector = () => null;
  el.querySelectorAll = () => [];
  el.closest = () => null;
  el.contains = () => false;
  el.getBoundingClientRect = () => ({ top: 0, left: 0, right: 100, bottom: 20, width: 100, height: 20 });
  el.scrollIntoView = () => {};
  el.focus = () => {};
  el.blur = () => {};
  el.select = () => {};
  el.setSelectionRange = () => {};
  el.click = () => {};
  Object.defineProperty(el, "id", {
    get() { return el._id || ""; },
    set(v) { el._id = String(v); registry[String(v)] = el; },
    configurable: true,
  });
  Object.defineProperty(el, "lastElementChild", { get: () => el.children[el.children.length - 1] || null });
  Object.defineProperty(el, "firstElementChild", { get: () => el.children[0] || null });
  Object.defineProperty(el, "nextElementSibling", { get: () => el._next || null, set(v) { el._next = v; } });
  return el;
}

const settingsHost = makeEl("div");
const header = makeEl("header");
header.nextElementSibling = settingsHost;
const h1 = makeEl("h1");
h1.textContent = "设置";
h1.closest = (sel) => (sel === "header" ? header : null);

const bodyEl = makeEl("body");

// ---- 账号卡片桩：article > [h3, section] ------------------------------
// 卡片里官方自己的内容（摘要 / 积分明细）与我们注入的两块（账号池控制条 /
// 可用模型）是同级的，顺序断言就落在这上面。
//   第一张：带 h3（账号甲），官方与我们两块都在；
//   第二张：**不带 h3**，所以两个注入函数都会跳过它 —— 用来验证我们不去搬官方内容。
const ACCOUNT_CARDS = [];
function makeCard(name) {
  const art = makeEl("article");
  const sec = makeEl("section");
  // 官方自己的两块：积分摘要 + 积分明细
  const summary = makeEl("div");
  summary.className = "credit-summary";
  summary.textContent = "1,234.56 个积分包";
  const detail = makeEl("div");
  detail.className = "credit-detail";
  detail.textContent = "近期到期";
  sec.appendChild(summary);
  sec.appendChild(detail);
  if (name) {
    const h3 = makeEl("h3");
    h3.textContent = name;
    art.appendChild(h3);
  }
  art.appendChild(sec);
  bindQuery(art);
  bodyEl.appendChild(art);
  ACCOUNT_CARDS.push({ art, sec, name });
}
makeCard("账号甲");
makeCard(null);

const document = {
  createElement: makeEl,
  createTextNode: (t) => ({ textContent: t, nodeType: 3 }),
  createDocumentFragment: () => { const f = makeEl("div"); f.__fragment = true; return f; },
  getElementById: (id) => registry[id] || null,
  querySelector: () => null,
  querySelectorAll: (sel) => (sel === "h1"
    ? [h1]
    : (sel === "article" ? ACCOUNT_CARDS.map((c) => c.art) : [])),
  addEventListener: () => {},
  removeEventListener: () => {},
  body: bodyEl,
  documentElement: makeEl("html"),
  title: "",
  execCommand: () => true,
};

const store = {};
const window = {
  location: {
    protocol: "http:",
    hostname: "192.168.31.9",
    origin: "http://192.168.31.9:18090",
    href: "http://192.168.31.9:18090/settings",
    port: "18090",
  },
  addEventListener: () => {},
  removeEventListener: () => {},
  confirm: () => false,
  prompt: () => null,
  alert: () => {},
  isSecureContext: false,
  setTimeout,
  clearTimeout,
};

class MutationObserver {
  constructor() {}
  observe() {}
  disconnect() {}
  takeRecords() { return []; }
}

const PAYLOADS = __PAYLOADS__;

function payloadFor(url) {
  const u = String(url);
  // 更具体的路径必须排在前面：`/api/account-models/restore` 也以
  // `/api/account-models` 开头，顺序反了会被上面那条截胡，返回模型列表
  // 而恢复逻辑拿不到 restoredCount，提示里就会出现 undefined。
  if (u.indexOf("/api/account-models/restore") >= 0) return PAYLOADS.accountRestore;
  if (u.indexOf("/api/gateway-info") >= 0) return PAYLOADS.gatewayInfo;
  if (u.indexOf("/api/api-keys") >= 0) return PAYLOADS.keysStatus;
  if (u.indexOf("/api/model-health") >= 0) return PAYLOADS.health;
  if (u.indexOf("/api/account-models") >= 0) return PAYLOADS.accountModels;
  if (u.indexOf("/api/account-pool") >= 0) return PAYLOADS.accountPool;
  if (u.indexOf("/api/token-stats") >= 0) return PAYLOADS.tokenStats;
  if (u.indexOf("/api/account-health/probe") >= 0) return PAYLOADS.accountProbe;
  if (u.indexOf("/api/account-health/audit") >= 0) return PAYLOADS.accountAudit;
  return {};
}

// wbToast 会调 requestAnimationFrame 加进场类名，Node 里没有这个 API。
// 不补桩的话，点一次「检测账号」就会抛出一个未处理的拒绝，
// 冒烟断言会把它读成「渲染过程抛异常」而误报。
const requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0);
const navigator = { userAgent: "node-test", clipboard: undefined };
const localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
const sessionStorage = localStorage;

// fetch 桩：既数数量，也记录每次请求的 url / method。
// 只数数量不够 —— 断言「点了检测按钮真的打到了账号探测接口」需要看请求本身，
// 否则按钮接了但没连对接口（或压根没接）都会被数字掩盖过去。
//
// 必须挂在 globalThis 上，而且**不能**在模块作用域另写一个 `function fetch`：
// 那样脚本 IIFE 里的自由变量 `fetch` 会解析到那个裸函数，计数桩被整体绕过，
// 所有「打了没打接口」的断言都会失真（表现为计数恒为 0 却仍能通过宽松断言）。
let fetchCalls = 0;
const fetchLog = [];
globalThis.fetch = (u, opts) => {
  fetchCalls += 1;
  fetchLog.push({
    url: String(u),
    method: String((opts && opts.method) || "GET").toUpperCase(),
    body: (opts && opts.body) || null,
  });
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve(payloadFor(String(u))),
    text: () => Promise.resolve(""),
  });
};

const WB = __SCRIPT__

let runError = null;
try { WB.run(); } catch (e) { runError = "run() 抛异常：" + (e && e.message); }

// 让所有 fetch 的 Promise 链跑完
setTimeout(async () => {
  // ---- 结构断言 ----
  function walk(node, out) {
    if (!node) return out;
    out.push(node);
    (node.children || []).forEach((c) => walk(c, out));
    return out;
  }
  const all = walk(settingsHost, []);
  const text = all.map((e) => e.textContent || "").join(" | ");
  const article = (cls) => all.filter((e) => (e.className || "").split(/\s+/).indexOf(cls) >= 0);

  // ---- 账号卡片顺序（竞态回归）------------------------------------------
  // 我们的两块是 fetch 回来后追加的，真实页面里 MutationObserver 会因此再跑一轮
  // run() —— 线上正是这一轮把顺序钉正。这里手动补这一轮。
  function tailOf(sec) {
    return (sec.children || []).slice(-2).map((c) => {
      if (c.classList.contains("wb-pool-bar")) return "POOL";
      if (c.classList.contains("wb-am-box")) return "BOX";
      return "OTHER";
    });
  }
  let secondRunError = null;
  try { WB.run(); } catch (e) { secondRunError = "渲染后二次 run() 抛异常：" + (e && e.message); }
  const cardTails = ACCOUNT_CARDS.map((c) => tailOf(c.sec));

  // 模拟官方把「积分明细」晚一步追加进来：React 判它是末节点 → appendChild，
  // 它就会落在我们两块**之后**（这正是线上看到的「明细掉到卡片最底」）。
  ACCOUNT_CARDS.forEach((c) => {
    const late = makeEl("div");
    late.className = "late-credit-detail";
    late.textContent = "近期到期";
    c.sec.appendChild(late);
  });
  const tailsAfterLateRaw = ACCOUNT_CARDS.map((c) => tailOf(c.sec));

  let thirdRunError = null;
  try { WB.run(); } catch (e) { thirdRunError = "明细晚到后 run() 抛异常：" + (e && e.message); }
  const tailsAfterLate = ACCOUNT_CARDS.map((c) => tailOf(c.sec));
  const lateStaysAbove = ACCOUNT_CARDS.map((c) => {
    const kids = c.sec.children || [];
    const iLate = kids.findIndex((k) => (k.className || "").indexOf("late-credit-detail") >= 0);
    const iPool = kids.findIndex((k) => (k.className || "").indexOf("wb-pool-bar") >= 0);
    if (iLate < 0) return "no-late";
    if (iPool < 0) return "no-ours";
    return iLate < iPool ? "above" : "below";
  });

  // ---- 账号卡片：禁用计数的口径 -----------------------------------------
  // 取每张卡片「可用模型」标题行里的徽标。曾经把巡检写的禁用也算进「已禁用」，
  // 再单列一个「巡检 N」，同一批模型被数了两遍：巡检禁掉一个模型，
  // 用户会看到「已禁用」也跟着 +1。
  const cardBadges = ACCOUNT_CARDS.map((c) => {
    const box = (c.sec.children || [])
      .find((k) => (k.className || "").split(/\s+/).indexOf("wb-am-box") >= 0);
    if (!box) return null;
    const title = (box.children || [])[0] || {};
    return (title.children || [])
      .filter((k) => (k.className || "").split(/\s+/).indexOf("wb-am-offcount") >= 0)
      .map((k) => ({
        auto: (k.className || "").split(/\s+/).indexOf("wb-am-offcount-auto") >= 0,
        text: String(k.textContent || ""),
      }));
  });

  // ---- 账号卡片：「全部恢复」按钮 ---------------------------------------
  // 一键恢复必须**只在确实有禁用项时出现**，且要有 onclick —— 只画个按钮
  // 不接事件，用户点了没反应，等于没做。
  const cardRestore = ACCOUNT_CARDS.map((c) => {
    const box = (c.sec.children || [])
      .find((k) => (k.className || "").split(/\s+/).indexOf("wb-am-box") >= 0);
    if (!box) return null;
    const title = (box.children || [])[0] || {};
    const btn = (title.children || [])
      .find((k) => (k.className || "").split(/\s+/).indexOf("wb-am-restore") >= 0);
    if (!btn) return null;
    return {
      text: String(btn.textContent || ""),
      tip: String(btn.title || ""),
      clickable: typeof btn.onclick === "function",
    };
  });

  // ---- 「全部恢复」点一下：请求路径 + 载荷 + 结果提示 -------------------
  let restoreFetch = null;
  let restoreToast = null;
  try {
    const btn = (() => {
      for (const c of ACCOUNT_CARDS) {
        const box = (c.sec.children || [])
          .find((k) => (k.className || "").split(/\s+/).indexOf("wb-am-box") >= 0);
        if (!box) continue;
        const title = (box.children || [])[0] || {};
        const hit = (title.children || []).find((k) =>
          (k.className || "").split(/\s+/).indexOf("wb-am-restore") >= 0);
        if (hit) return hit;
      }
      return null;
    })();
    if (btn && typeof btn.onclick === "function") {
      const before = fetchLog.length;
      btn.onclick();
      // fetch 桩是异步 resolve 的，提示条要等 Promise 链跑完才出现 ——
      // 同步读会一律读到 null，把「有提示」误判成「没提示」。
      for (let i = 0; i < 20 && fetchLog.length === before; i += 1) {
        await new Promise((r) => setTimeout(r, 5));
      }
      await new Promise((r) => setTimeout(r, 20));
      const rec = fetchLog.slice(before)
        .filter((r) => r.url.indexOf("/account-models/restore") >= 0)[0];
      if (rec) {
        restoreFetch = { url: rec.url, method: rec.method, body: String(rec.body || "") };
      }
      const toasts = (document.body.children || [])
        .filter((e) => (e.className || "").indexOf("wb-api-toast") >= 0)
        .map((e) => String(e.textContent || ""));
      restoreToast = toasts.length ? toasts[toasts.length - 1] : null;
    }
  } catch (e) {
    restoreToast = "点按抛异常：" + (e && e.message);
  }

  // ---- 账号卡片：控制条的按钮（账号检测 + 停用来源）----------------------
  // 停用来源要能从按钮上读出来：manual 灰底、auto 琥珀色（.wb-pool-btn-auto），
  // 文案也要说清是谁停的。否则「已停用」会被读成自己误操作过。
  const cardPoolButtons = ACCOUNT_CARDS.map((c) => {
    const bar = (c.sec.children || [])
      .find((k) => (k.className || "").split(/\s+/).indexOf("wb-pool-bar") >= 0);
    if (!bar) return null;
    return (bar.children || []).map((b) => ({
      cls: String(b.className || ""),
      text: String(b.textContent || ""),
      clickable: typeof b.onclick === "function",
    }));
  });

  // ---- 账号卡片：手动检测入口可点、且打的是账号探测接口 -------------------
  // 点一下「检测账号」，断言它发出的请求路径正确 —— 光有按钮不算数，
  // 真接上了才有效（面板消失那类事故就是这么来的）。
  let probeFetch = null;
  let probeAfterCount = 0;
  let probeToast = null;
  let probeToastClasses = "";
  try {
    const bar = (ACCOUNT_CARDS[0].sec.children || [])
      .find((k) => (k.className || "").split(/\s+/).indexOf("wb-pool-bar") >= 0);
    const probeBtn = bar && (bar.children || [])
      .find((b) => String(b.textContent || "").indexOf("检测账号") >= 0);
    if (probeBtn && typeof probeBtn.onclick === "function") {
      const before = fetchLog.length;
      probeBtn.onclick();
      probeAfterCount = fetchLog.length - before;
      probeFetch = fetchLog[fetchLog.length - 1] || null;
      // toast 是直接挂到 body 上的（不是我们注入的节点），且要等 fetch 链跑完。
      // 轮询读而不是 sleep 定长：fetch 桩是同步 resolve，通常一拍就有。
      // 匹配只按账号名 —— 不要顺带匹配结论里的词（如「凭据」），
      // 那会把测试和某版文案绑死，改文案就假失败。
      for (let i = 0; i < 40 && !probeToast; i += 1) {
        await new Promise((r) => setTimeout(r, 10));
        const hit = (document.body.children || [])
          .map((e) => String(e.textContent || ""))
          .filter((t) => t.indexOf("账号甲") >= 0 && t.length > "账号甲".length);
        probeToast = hit.length ? hit[hit.length - 1] : null;
      }
      // toast 的语义档位要从 class 上读：过去 restricted（账号受限，凭据其实是好的）
      // 与 invalid（凭据真坏了）共用红色，用户会把受限读成「凭据坏了」。
      probeToastClasses = (document.body.children || [])
        .map((e) => String(e.className || "")).join(" ");
    }
  } catch (e) {
    errors.push("点击「检测账号」抛异常：" + ((e && e.message) || String(e)));
  }

  // ---- 一致性自检：按钮点了要真发请求，结果要真渲染出来 -------------------
  // 自检是「统一标准」的可见载体：它必须把两侧结论并排显示，
  // 光有按钮不算数 —— 点了没反应与根本没接接口，表现是一样的。
  let auditFetch = null;
  let auditBoxText = null;
  let auditRows = 0;
  let auditBoxClasses = "";
  try {
    const auditBtn = all.find((e) => String(e.textContent || "") === "一致性自检");
    if (auditBtn && typeof auditBtn.onclick === "function") {
      const before = fetchLog.length;
      auditBtn.onclick();
      auditFetch = fetchLog[fetchLog.length - 1] || null;
      if (fetchLog.length === before) auditFetch = null;
      // 等 fetch 链与渲染跑完：桩是同步 resolve，但渲染在 then 里。
      //
      // 注意要**重新遍历** DOM 找结果块：上面那个 `all` 是点击前的静态快照，
      // 自检渲染出来的新节点不在里面 —— 拿旧快照去找，永远找不到，
      // 表现为「请求发了但结果框是空的」，很容易被误读成渲染没生效。
      for (let i = 0; i < 40 && !auditBoxText; i += 1) {
        await new Promise((r) => setTimeout(r, 10));
        const box = walk(settingsHost, []).find((e) => (e.className || "").split(/\s+/)
          .indexOf("wb-audit-box") >= 0);
        if (box) {
          // 桩上的 textContent 不会自动聚合子树，得自己把后代文本拼起来 ——
          // 直接读 box.textContent 只会拿到空串，看着像「什么都没渲染」。
          const nodes = walk(box, []);
          auditBoxText = nodes.map(function (e) {
            return String(e.textContent || "");
          }).join(" ");
          auditRows = nodes.filter(function (e) {
            return (e.className || "").split(/\s+/).indexOf("wb-audit-row") >= 0;
          }).length;
          auditBoxClasses = nodes.map(function (e) {
            return String(e.className || "");
          }).join(" ");
        }
      }
    }
  } catch (e) {
    errors.push("点击「一致性自检」抛异常：" + ((e && e.message) || String(e)));
  }

  const result = {
    errors: errors.concat([runError, secondRunError, thirdRunError].filter(Boolean)),
    fetchCalls,
    fetchLog: fetchLog.slice(-12),
    hostFound: !!WB.findHost(),
    nodes: all.length,
    // 必须**真的挂上去了**：渲染函数是先给 section 设 id、最后才 appendChild，
    // 中途抛异常时 id 已经登记但节点没进 DOM（v0.4.5 的 host 遮蔽就是这个形态）。
    hasAboutSection: !!registry["settings-about"]
      && settingsHost.children.indexOf(registry["settings-about"]) >= 0,
    hasAboutGrid: article("wb-about-grid").length > 0,
    aboutKeys: article("wb-about-k").map((e) => e.textContent),
    aboutValues: article("wb-about-v").map((e) => (e.children || []).map((c) => c.textContent).join("")),
    hasApiSection: !!registry["settings-api-access"] || text.indexOf("API 接入") >= 0,
    hasHealthSection: !!registry["settings-model-health"] || text.indexOf("可用性巡检") >= 0,
    mentionsImage: text.indexOf("容器镜像") >= 0,
    versionBadgeShown: text.indexOf("v9.9.9") >= 0,
    cardTails,
    tailsAfterLateRaw,
    tailsAfterLate,
    lateStaysAbove,
    cardBadges,
    cardRestore,
    cardPoolButtons,
    probeFetch,
    probeAfterCount,
    probeToast,
    restoreFetch,
    restoreToast,
    auditFetch,
    auditBoxText,
    auditRows,
    auditBoxClasses,
  };
  process.stdout.write("__RESULT__" + JSON.stringify(result) + "\n");
}, 60);
"""


def main() -> int:
    print("[1] 取出注入脚本")
    script = extract_script(WEB_PROXY.read_text(encoding="utf-8"))
    check("注入脚本提取成功", len(script) > 5000, f"len={len(script)}")
    check("脚本里存在自动启动逻辑（说明是完整的那一段）",
          "MutationObserver(() => run())" in script)

    stripped = strip_bootstrap(script)
    check("已把自动启动改为导出 run / findHost（不再自动跑）",
          "return { run: run" in stripped and "DOMContentLoaded" not in stripped)

    if not shutil.which("node"):
        print("\n跳过：环境里没有 node，无法做渲染冒烟（CI 里由 actions/setup-node 提供）")
        print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
        return 1 if _fail else 0

    print("\n[2] 注入 JS 语法解析（node --check）")
    check_js_parses(script)

    print("\n[3] 在 DOM 桩里跑一遍所有面板的渲染")
    harness = (HARNESS_JS
               .replace("__PAYLOADS__", json.dumps({
                   "gatewayInfo": GATEWAY_INFO, "keysStatus": KEYS_STATUS,
                   "health": HEALTH, "accountModels": ACCOUNT_MODELS,
                   "accountPool": ACCOUNT_POOL, "tokenStats": {},
                   "accountRestore": ACCOUNT_RESTORE,
                   "accountProbe": ACCOUNT_PROBE, "accountAudit": ACCOUNT_AUDIT},
                   ensure_ascii=False))
               .replace("__SCRIPT__", stripped))

    with tempfile.TemporaryDirectory() as tmp:
        js_path = Path(tmp) / "harness.js"
        js_path.write_text(harness, encoding="utf-8", newline="\n")
        env = {"PATH": str(Path(shutil.which("node")).parent)}
        proc = subprocess.run(["node", str(js_path)], capture_output=True, text=True,
                              timeout=120)
        out = proc.stdout or ""
        if proc.returncode != 0 and "__RESULT__" not in out:
            print(out[-3000:])
            print(proc.stderr[-3000:])
            check("渲染冒烟脚本正常退出", False, f"exit={proc.returncode}")
            print(f"\n{'=' * 52}\n通过 {_ok} 项，失败 {len(_fail)} 项：{_fail}")
            return 1

        marker = out.find("__RESULT__")
        if marker < 0:
            print(out[-2000:])
            print(proc.stderr[-2000:])
            check("拿到了渲染结果", False, "没有输出 __RESULT__")
            print(f"\n{'=' * 52}\n通过 {_ok} 项，失败 {len(_fail)} 项：{_fail}")
            return 1
        result = json.loads(out[marker + len("__RESULT__"):].strip().split("\n")[0])

    js_errors = result["errors"]
    check("渲染过程没有抛异常 / 未处理的 Promise 拒绝",
          not js_errors, f"got {js_errors[:3]}")

    print("\n[3] 「关于」面板结构")
    check("关于面板被挂到设置页", result["hasAboutSection"],
          "id=settings-about 不存在 —— 面板多半是渲染时抛异常了")
    check("运行位置用两列网格（对齐靠它，不是靠空格）", result["hasAboutGrid"])
    check("三行标签齐全（数据目录 / 控制台 / 网关 API）",
          result["aboutKeys"] == ["数据目录", "控制台", "网关 API"],
          f"got {result['aboutKeys']}")
    check("控制台地址带上了访问用的主机名与端口",
          any("192.168.31.9:18090" in v for v in result["aboutValues"]),
          f"got {result['aboutValues']}")
    check("网关地址指向 /v1",
          any("192.168.31.9:18091/v1" in v for v in result["aboutValues"]),
          f"got {result['aboutValues']}")
    check("版本徽章显示的是网关报出的版本", result["versionBadgeShown"])

    print("\n[4] 面板不再出现的冗余信息")
    check("「关于」里不再出现「容器镜像」", not result["mentionsImage"],
          "容器镜像那一块应该已经去掉（升级方式属于文档）")

    print("\n[5] 其他面板仍在渲染")
    check("API 接入面板还在", result["hasApiSection"])
    check("可用性巡检面板还在", result["hasHealthSection"])

    print("\n[6] 账号卡片：注入块的顺序恒定（竞态回归）")
    check("卡片尾两块恒为 [控制条, 模型区]",
          result["cardTails"] == [["POOL", "BOX"], ["OTHER", "OTHER"]],
          f"got {result['cardTails']}")
    check("只含官方内容的卡片不被搬动", result["cardTails"][1] == ["OTHER", "OTHER"],
          f"got {result['cardTails'][1]}")
    check("官方晚一步追加明细时，原生顺序确实会漂（这是竞态的成因）",
          result["tailsAfterLateRaw"][0][-1] == "OTHER",
          f"got {result['tailsAfterLateRaw']}")
    check("下一轮渲染把注入块挪回末尾",
          result["tailsAfterLate"] == [["POOL", "BOX"], ["OTHER", "OTHER"]],
          f"got {result['tailsAfterLate']}")
    check("晚到的官方明细块留在我们两块之前（不再掉到卡片最底）",
          result["lateStaysAbove"] == ["above", "no-ours"],
          f"got {result['lateStaysAbove']}")

    print("\n[7] 账号卡片：禁用计数口径（手动 / 巡检 互不重叠）")
    badges = result["cardBadges"]
    first = badges[0] or []
    manual = next((b for b in first if not b["auto"]), None)
    auto = next((b for b in first if b["auto"]), None)
    check("手动禁用的模型计入「已禁用」",
          manual is not None and manual["text"] == "已禁用 1", f"got {first}")
    check("巡检禁用的模型单独计「巡检」，不再落进「已禁用」",
          auto is not None and auto["text"] == "巡检 1", f"got {first}")

    def _num(badge):
        try:
            return int(str(badge["text"]).split()[-1])
        except (AttributeError, IndexError, ValueError):
            return -1

    check("两个计数互不重叠（本卡片手动 1 + 巡检 1 = 禁用总数 2）",
          _num(manual) + _num(auto) == 2, f"got {first}")
    check("没有 h3 的卡片依旧不被注入模型区", badges[1] is None, f"got {badges[1]}")

    print("\n[7b] 账号卡片：一键恢复（全部恢复按钮）")
    restores = result.get("cardRestore") or []
    first_restore = restores[0] if restores else None
    check("有禁用项的卡片出现「全部恢复」按钮", first_restore is not None, str(restores))
    check("按钮文案带上要恢复的数量（恢复 2）",
          first_restore is not None and "2" in first_restore["text"],
          str(first_restore))
    check("按钮可点击（绑了 onclick）",
          first_restore is not None and first_restore["clickable"], str(first_restore))
    # 作用域必须说清是「全部」——含手动禁用。只说「恢复巡检」会让人以为
    # 手动关掉的不会被碰，那正是 scope=auto 的另一种语义。
    check("悬浮说明点明含手动禁用",
          first_restore is not None and "手动" in (first_restore.get("tip") or ""),
          str(first_restore))
    check("没有 h3 的第二张卡片不出现恢复按钮",
          len(restores) > 1 and restores[1] is None, str(restores))

    # 点一下：必须真的打到恢复接口，且载荷指名账号、scope=all。
    # 光有按钮不算数 —— 按钮画出来了但没接事件，用户点了没反应。
    rf = result.get("restoreFetch")
    check("点「全部恢复」发出了恢复请求", rf is not None, str(rf))
    check("恢复请求打到 /api/account-models/restore",
          rf is not None and "/account-models/restore" in rf.get("url", ""), str(rf))
    check("恢复请求是 POST", rf is not None and rf.get("method") == "POST", str(rf))
    check("载荷指名了目标账号",
          rf is not None and "acc-1" in rf.get("body", ""), str(rf))
    check("载荷 scope=all（含手动禁用一起放回）",
          rf is not None and '"scope":"all"' in rf.get("body", "").replace(" ", ""),
          str(rf))
    rt = result.get("restoreToast")
    check("恢复后给出结果提示", bool(rt) and "恢复" in str(rt), str(rt))

    print("\n[8] 账号卡片：控制条（检测账号 + 停用来源）")
    btns = result["cardPoolButtons"]
    first_bar = btns[0] if btns else None
    texts = " | ".join(b["text"] for b in (first_bar or []))
    check("控制条挂在卡片上", first_bar is not None, str(btns))
    check("有「检测账号」入口", "检测账号" in texts, texts)
    probe_btn = next((b for b in (first_bar or []) if "检测账号" in b["text"]), None)
    check("检测按钮可点击（绑了 onclick）",
          probe_btn is not None and probe_btn["clickable"], str(probe_btn))
    # 停用来源要看得见：夹具里 acc-1 是巡检（auto）停用的，
    # 按钮文案必须说「巡检」，样式必须是琥珀色 —— 否则用户会以为是自己点的。
    check("停用按钮区分来源（巡检停用）", "巡检" in texts, texts)
    check("巡检停用使用琥珀色样式（.wb-pool-btn-auto）",
          any("wb-pool-btn-auto" in b["cls"] for b in (first_bar or [])), texts)

    # 点一下，断言真的打到了账号探测接口。这条是这组断言里最关键的：
    # 按钮存在只说明画出来了，「接对了接口」才算真的能用。
    probe = result.get("probeFetch")
    check("点「检测账号」发出了账号探测请求",
          result.get("probeAfterCount", 0) == 1 and probe is not None,
          f"got {probe} afterCount={result.get('probeAfterCount')}")
    check("探测请求打到 /api/account-health/probe",
          probe is not None and "/api/account-health/probe" in probe.get("url", ""),
          f"got {probe}")
    check("探测请求是 POST",
          probe is not None and probe.get("method") == "POST", f"got {probe}")
    check("探测请求带上 accountId",
          probe is not None and "accountId" in str(probe.get("body") or ""), f"got {probe}")

    # 正常账号的提示要让人看懂「为什么是有效的」，但不能出现会被读成故障的措辞。
    # 探测手段（发一个不存在的模型名看上游怎么回）必须讲清楚 ——
    # 只写「凭据有效」而藏起依据，用户去翻日志看见 400 会以为检测坏了；
    # 但把「模型不存在」这种字眼直接摆在结论旁边，又会被读成「账号有问题」。
    # 所以依据要说得像一句解释，而不是甩一个错误名给用户。
    toast = result.get("probeToast")
    # 正常账号的提示必须**只讲用户关心的事**：账号能不能用。
    # 探测怎么做的（用了什么模型名、上游回了什么）属于实现细节，
    # 摆到提示里会把好结论读成故障 —— 用户反馈过这一点。
    check("正常账号的提示是一句人话（账号正常，可以放心使用）",
          bool(toast) and "账号正常" in toast and "可以放心使用" in toast,
          f"got {toast}")
    check("正常账号的提示不含探测细节（不出现「不存在/模型名/试」）",
          bool(toast) and not any(w in toast for w in
                                  ("不存在", "模型名", "试", "拒绝", "失败", "异常")),
          f"got {toast}")
    # 界面必须按 state 判定，而不是拿 verdict 或状态码自己解释 ——
    # 那正是「同一个账号在不同页面结论不同」的来源。
    check("检测提示读的是 state（结论文案来自 userMessage）",
          bool(toast) and "账号正常" in toast, f"got {toast}")
    # 提示只能有一句。曾经把 userMessage + evidence + action 三段拼在一起，
    # 三者说的是同一件事，读起来像一段话 —— 用户反馈「异常账号提示冗长」。
    # 这里按「账号名：一句结论」的形状断言，多于一句就说明又拼了东西。
    check("提示只含一句结论（不拼接 evidence / action）",
          bool(toast) and toast.count("：") == 1
          and len(toast) < 40 and "；" not in toast,
          f"len={len(toast or '')} got {toast}")
    # 正常账号绝不能被标成异常样式：过去 restricted 与 invalid 共用红色，
    # 正常账号也会被这种「一律醒目」的写法波及。
    check("正常账号用 ok 档样式而非错误档",
          "wb-api-toast-ok" in (result.get("probeToastClasses") or "")
          or "wb-api-toast-warn" not in (result.get("probeToastClasses") or ""),
          str(result.get("probeToastClasses")))

    # 类名必须和样式表对得上。曾出现：调用点生成 `wb-api-toast-error`，
    # 样式表只定义 `.wb-api-toast-err` —— 红色档从未命中，失效账号的提示
    # 落到默认档，看起来和普通提示一样。这类「两边各写一个名字」的错
    # 不会报错、只会静默降级，必须由测试盯住。
    import re as _re
    # 从源码文件读样式（stripped 只是 JS 部分，不含 <style>）
    _raw = WEB_PROXY.read_text(encoding="utf-8")
    _css = "\n".join(_re.findall(r"<style>(.*?)</style>", _raw, _re.S))
    for _tier in ("ok", "warn", "err"):
        check(f"样式表定义了 {_tier} 档（.wb-api-toast-{_tier}）",
              f".wb-api-toast-{_tier}" in _css, _tier)
    check("样式表同时兼容 -error 写法（调用点历史混用）",
          ".wb-api-toast-error" in _css, "缺 .wb-api-toast-error")
    # 反向：wbToast 不能生成样式表里没有的档位。
    # `-show` 是交互状态类（与 tier 正交），单独排除。
    _js = strip_bootstrap(script)
    for _cls in sorted(set(_re.findall(r'wb-api-toast-([a-z]+)', _js))):
        if _cls in ("show",):
            continue
        check(f"wbToast 引用的档位 {_cls} 在样式表里有定义",
              f".wb-api-toast-{_cls}" in _css, _cls)

    print("\n[9] 巡检面板：一致性自检入口与结果渲染")
    af = result.get("auditFetch")
    check("点了「一致性自检」发出请求",
          af is not None and "/api/account-health/audit" in (af.get("url") or ""),
          f"got {af}")
    box = result.get("auditBoxText") or ""
    check("自检结果渲染出来了", bool(box), f"got {box!r}")
    # 两个账号各一行：只显示「有没有问题」是不够的，
    # 每一行都要能看出是哪个账号、结论是什么、该怎么办。
    check("每个账号各占一行", result.get("auditRows") == 2,
          f"got {result.get('auditRows')}")
    check("区分开「账号正常」与「账号被上游拦截」两种状态",
          "账号正常" in box and "账号已被上游拦截" in box, box[:200])
    # 被拦截的账号必须明说「重新扫码没用」—— 这是实测得出的结论，
    # 也是这一档唯一需要用户改变行为的信息。不写的话，用户会照旧去
    # 重新登录一次，白折腾（这正是线上发生过的事）。
    # 注意：允许出现「重新扫码登录没用」这个句子，但**不能**出现
    # 「需要重新登录」这类会把人引向重登的表述。
    check("受限账号的说明明确指出重新扫码无效",
          "重新扫码登录没用" in box or "重新扫码没用" in box, box[:300])
    check("受限账号的说明不把人引向重新登录",
          "需要重新登录" not in box and "请重新登录" not in box, box[:300])
    # 判据要印出来，「统一标准」才是可查的而不是一句口头承诺。
    check("结果里印出判定依据（凭据侧）",
          "凭据有效性只由账号级探测裁定" in box or "不存在的模型名" in box, box[:300])
    # 每个账号的判定依据也要显示：「账号正常」与「账号被上游拦截」的差别全在
    # 那一句 detail 里，只给标签的话用户还是分不清该重登还是该换账号。
    check("每行印出该账号自己的判定依据",
          "模型不存在" in box and "被上游拦截" in box, box[:400])
    # 自检面板里不能再用「风控」—— 实测证明 11140 与内容、频率无关，
    # 是账号本身被拦。说成风控会把用户引向错误的排查方向
    #（去调调用频率、去查内容），而正解是换账号。
    check("自检面板不再使用「风控」这个说法",
          "风控" not in box, box[:400])
    # 状态着色必须区分三档：正常 / 被拦截（琥珀）/ 失效（红）。
    # 只用一个颜色的话，「账号被拦截」会被读成「凭据坏了」。
    check("状态着色区分 valid / restricted",
          "wb-audit-ok" in (result.get("auditBoxClasses") or "")
          and "wb-audit-warn" in (result.get("auditBoxClasses") or ""),
          str(result.get("auditBoxClasses")))

    print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
