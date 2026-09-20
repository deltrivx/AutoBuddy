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
        "name": "WorkBuddy Switch",
        "url": "https://example.invalid/repo",
        "releases": "https://example.invalid/repo/releases",
        "changelog": "https://example.invalid/repo/blob/main/CHANGELOG.md",
        "issues": "https://example.invalid/repo/issues",
    },
    "dataDir": "/data/.wb-switch",
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
    "accounts": [{"id": "acc-1", "name": "账号甲", "variant": "ai",
                  "models": ["m-a", "m-b"], "disabledModels": ["m-b"],
                  "disabledSources": {"m-b": "manual"}, "disabledTotal": 1,
                  "usable": True, "expired": False}],
    "policy": {"acc-1": ["m-b"]},
    "policySources": {"acc-1": {"m-b": "manual"}},
    "disabledTotal": 1,
    "disabledBySource": {"manual": 1, "auto": 0},
}

ACCOUNT_POOL = {
    "mode": "rotate", "enabledAccountIds": ["acc-1"], "preferredAccountId": None,
    "accounts": [{"id": "acc-1", "name": "账号甲", "inPool": True, "usable": True}],
    "total": 1, "usable": 1,
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
  if (u.indexOf("/api/gateway-info") >= 0) return PAYLOADS.gatewayInfo;
  if (u.indexOf("/api/api-keys") >= 0) return PAYLOADS.keysStatus;
  if (u.indexOf("/api/model-health") >= 0) return PAYLOADS.health;
  if (u.indexOf("/api/account-models") >= 0) return PAYLOADS.accountModels;
  if (u.indexOf("/api/account-pool") >= 0) return PAYLOADS.accountPool;
  if (u.indexOf("/api/token-stats") >= 0) return PAYLOADS.tokenStats;
  return {};
}

function fetch(url) {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve(payloadFor(url)),
    text: () => Promise.resolve(""),
  });
}

const navigator = { userAgent: "node-test", clipboard: undefined };
const localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
const sessionStorage = localStorage;

const WB = __SCRIPT__

let fetchCalls = 0;
const rawFetch = fetch;
globalThis.fetch = (u) => { fetchCalls += 1; return rawFetch(u); };

let runError = null;
try { WB.run(); } catch (e) { runError = "run() 抛异常：" + (e && e.message); }

// 让所有 fetch 的 Promise 链跑完
setTimeout(() => {
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

  const result = {
    errors: errors.concat([runError, secondRunError, thirdRunError].filter(Boolean)),
    fetchCalls,
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

    print("\n[2] 在 DOM 桩里跑一遍所有面板的渲染")
    harness = (HARNESS_JS
               .replace("__PAYLOADS__", json.dumps({
                   "gatewayInfo": GATEWAY_INFO, "keysStatus": KEYS_STATUS,
                   "health": HEALTH, "accountModels": ACCOUNT_MODELS,
                   "accountPool": ACCOUNT_POOL, "tokenStats": {}},
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

    print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
