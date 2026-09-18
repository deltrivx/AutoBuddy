import json
import os
from pathlib import Path
from fastapi import FastAPI, Request, Response
import httpx
import uvicorn

try:
    from gateway.token_tracker import get_aggregated_token_stats
except ImportError:
    from token_tracker import get_aggregated_token_stats

app = FastAPI()

BACKEND_URL = "http://127.0.0.1:57890"
ICON_PATH = Path("/app/icon.png")

COLLAPSE_SCRIPT = """
<style>
  aside {
    transition: width 0.25s cubic-bezier(0.4, 0, 0.2, 1), padding 0.25s cubic-bezier(0.4, 0, 0.2, 1) !important;
    position: relative !important;
  }
  #wb-collapse-btn {
    position: absolute;
    right: -12px;
    top: 20px;
    z-index: 50;
    width: 24px;
    height: 24px;
    border-radius: 50%;
    border: 1px solid rgba(120, 120, 120, 0.2);
    background-color: var(--background, #ffffff);
    color: var(--foreground, #374151);
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    box-shadow: 0 1px 4px rgba(0,0,0,0.12);
    transition: all 0.2s ease;
    padding: 0;
  }
  #wb-collapse-btn:hover {
    background-color: rgba(120, 120, 120, 0.1);
    transform: scale(1.08);
  }
  aside.wb-collapsed {
    width: 68px !important;
    padding-left: 10px !important;
    padding-right: 10px !important;
  }
  aside.wb-collapsed div.min-w-0 {
    display: none !important;
  }
  aside.wb-collapsed nav a {
    justify-content: center !important;
    padding-left: 0 !important;
    padding-right: 0 !important;
  }
  aside.wb-collapsed nav a span.wb-nav-label {
    display: none !important;
  }
  /* 隐藏桌面端专有无法在容器执行的操作按钮与完全磁盘访问提示块 */
  .wb-mac-btn-hide,
  .wb-mac-block-hide {
    display: none !important;
  }
  /* 账号池控制条：启用开关 + 设为首选 */
  .wb-pool-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin-top: 10px;
  }
  .wb-pool-btn {
    font-size: 11px;
    line-height: 1.8;
    padding: 1px 10px;
    border-radius: 999px;
    border: 1px solid rgba(120, 120, 120, 0.35);
    background: transparent;
    color: var(--foreground, #374151);
    cursor: pointer;
  }
  .wb-pool-btn:hover { background: rgba(120, 120, 120, 0.12); }
  .wb-pool-btn-on {
    border-color: rgba(34, 197, 94, 0.55);
    background: rgba(34, 197, 94, 0.14);
    color: #15803d;
  }
  .wb-pool-btn-primary {
    border-color: rgba(59, 130, 246, 0.55);
    background: rgba(59, 130, 246, 0.14);
    color: #1d4ed8;
    font-weight: 600;
  }
  .wb-pool-hint {
    font-size: 11px;
    color: var(--muted-foreground, #6b7280);
  }
  /* 官方底层唯一的 CLI 绑定账号标记 */
  .wb-pool-bound {
    font-size: 11px;
    line-height: 1.8;
    padding: 1px 9px;
    border-radius: 999px;
    border: 1px dashed rgba(186, 117, 23, 0.55);
    background: rgba(239, 159, 39, 0.12);
    color: #854f0b;
    cursor: help;
  }
  /* 设置页：区分「官方 CLI 绑定」与「网关账号池」的说明条 */
  .wb-cli-scope-note {
    margin: 0 0 4px;
    padding: 8px 12px;
    border-radius: 8px;
    border: 1px solid rgba(59, 130, 246, 0.28);
    background: rgba(59, 130, 246, 0.08);
    color: var(--foreground, #374151);
    font-size: 12px;
    line-height: 1.6;
  }
  /* 标记最近一次 API 请求实际使用的账号 */
  .wb-pool-last {
    font-size: 11px;
    line-height: 1.8;
    padding: 1px 9px;
    border-radius: 999px;
    border: 1px dashed rgba(120, 120, 120, 0.45);
    color: var(--muted-foreground, #6b7280);
  }
  /* 该账号累计被分摊到的请求次数（进程启动后累计） */
  .wb-pool-count {
    font-size: 11px;
    line-height: 1.8;
    padding: 1px 9px;
    border-radius: 999px;
    background: rgba(120, 120, 120, 0.14);
    color: var(--muted-foreground, #6b7280);
    font-variant-numeric: tabular-nums;
  }
  .wb-pool-count-hot {
    background: rgba(59, 130, 246, 0.16);
    color: #1d4ed8;
    font-weight: 600;
  }
  /* 账号卡片下方动态展示的「可用模型」区域 */
  .wb-am-box {
    margin-top: 12px;
    border-top: 1px dashed rgba(120, 120, 120, 0.25);
    padding-top: 10px;
  }
  .wb-am-title {
    font-size: 11px;
    line-height: 1.6;
    color: var(--muted-foreground, #6b7280);
    margin-bottom: 6px;
  }
  .wb-am-list {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
  .wb-am-tag {
    font-size: 10px;
    line-height: 1.7;
    padding: 1px 7px;
    border-radius: 6px;
    background: rgba(120, 120, 120, 0.12);
    color: var(--foreground, #374151);
    white-space: nowrap;
  }
  /* 该账号已经真实调用过的模型，高亮以区分「可用」与「已用」 */
  .wb-am-tag-used {
    background: rgba(59, 130, 246, 0.16);
    color: var(--primary, #1d4ed8);
    font-weight: 600;
  }
</style>
<script>
(function() {
  // 账号卡片上的 WorkBuddy / CodeBuddy IDE / CodeBuddy CLI 切换按钮与「当前账号」徽章
  // 已在 patch/patch_binary.py 里从二进制物理移除（它们只会调用宿主桌面程序），
  // 这里不再需要任何按 aria-label 遮掩的逻辑。
  // 下面保留的是设置页里少量纯文案入口，二进制里没有稳定的结构锚点，只能按文案隐藏。
  var WB_UNSUPPORTED_TEXTS = [
    "接入 CLI",
    "更新 CLI 认证",
    "升级 CLI helper"
  ];

  function sanitizeMacUI() {
    document.querySelectorAll("button, a, [role='status']").forEach(el => {
      const text = (el.innerText || "").trim();
      // 清除无法在容器内执行的动作：Finder、完全磁盘访问、导入本机账号
      if (text === "在 Finder 中显示" || text === "在文件管理器中显示" || text === "打开完全磁盘访问" || text === "打开 App 管理" || text === "导入本机账号") {
        el.classList.add("wb-mac-btn-hide");
        return;
      }

      if (text && WB_UNSUPPORTED_TEXTS.indexOf(text) !== -1) {
        el.classList.add("wb-mac-btn-hide");
      }
    });

    // 「无 Buddy」表示该账号没有旅行伙伴、无法参与自动旅行，属负面且无参考价值的状态
    document.querySelectorAll("span, div").forEach(el => {
      if (el.children.length === 0 && (el.textContent || "").trim() === "无 Buddy") {
        el.classList.add("wb-mac-btn-hide");
      }
    });

    // 仅精准清理包含「如何授权」或「完全磁盘访问」的引导小卡片，绝不向上寻找普通大容器
    document.querySelectorAll("div.border-l-2, div.rounded-md.border").forEach(box => {
      const text = box.innerText || "";
      if (text.includes("完全磁盘访问") || text.includes("如何授权") || text.includes("workbuddy-switch.app")) {
        box.classList.add("wb-mac-block-hide");
      }
    });
  }

  function initCollapse() {
    const aside = document.querySelector("aside");
    if (!aside || document.getElementById("wb-collapse-btn")) return;
    
    const btn = document.createElement("button");
    btn.id = "wb-collapse-btn";
    btn.setAttribute("title", "收起/展开侧边栏");
    btn.innerHTML = `<svg id="wb-collapse-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"></polyline></svg>`;
    
    aside.querySelectorAll("nav a").forEach(a => {
      Array.from(a.childNodes).forEach(node => {
        if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
          const span = document.createElement("span");
          span.className = "wb-nav-label";
          span.innerText = node.textContent;
          node.replaceWith(span);
        }
      });
    });

    let collapsed = localStorage.getItem("wb_sidebar_collapsed") === "true";
    if (collapsed) {
      aside.classList.add("wb-collapsed");
      btn.querySelector("svg").innerHTML = `<polyline points="9 18 15 12 9 6"></polyline>`;
    }

    btn.onclick = (e) => {
      e.stopPropagation();
      collapsed = !collapsed;
      localStorage.setItem("wb_sidebar_collapsed", collapsed);
      if (collapsed) {
        aside.classList.add("wb-collapsed");
        btn.querySelector("svg").innerHTML = `<polyline points="9 18 15 12 9 6"></polyline>`;
      } else {
        aside.classList.remove("wb-collapsed");
        btn.querySelector("svg").innerHTML = `<polyline points="15 18 9 12 15 6"></polyline>`;
      }
    };
    aside.appendChild(btn);
  }

  function enforceTitle() {
    // 浏览器标签标题精简为固定短名，官方标题过长会被标签栏截断
    if (document.title !== "WorkBuddy Switch") {
      document.title = "WorkBuddy Switch";
    }
  }

  var wbModelsCache = null;

  function injectAccountModels() {
    // 在每个账号卡片下方动态插入「已使用模型」标签。
    // 数据来自官方 usage 记录（按 accountId + model 聚合），不硬编码模型清单。
    var cards = Array.prototype.slice.call(document.querySelectorAll("article"));
    if (!cards.length) return;

    var pending = cards.filter(function (c) {
      return !c.querySelector(".wb-am-box") && c.querySelector("h3");
    });
    if (!pending.length) return;

    function render(data) {
      var byName = {};
      var accs = (data && data.accounts) || {};
      Object.keys(accs).forEach(function (id) {
        var a = accs[id];
        if (a && a.name) byName[a.name] = a;
      });
      // 账号没有任何调用记录时（例如刚添加）也要展示网关可路由的完整清单
      var fallback = { models: (data && data.catalog) || [], used: [] };
      if (!fallback.models.length) return;

      pending.forEach(function (card) {
        if (card.querySelector(".wb-am-box")) return;
        var h3 = card.querySelector("h3");
        if (!h3) return;

        var entry = byName[(h3.textContent || "").trim()] || fallback;
        var models = (entry.models && entry.models.length) ? entry.models : fallback.models;
        var used = {};
        (entry.used || []).forEach(function (m) { used[m] = true; });
        if (!models.length) return;

        var box = document.createElement("div");
        box.className = "wb-am-box";

        var usedCount = Object.keys(used).length;
        var title = document.createElement("div");
        title.className = "wb-am-title";
        title.textContent = usedCount
          ? ("可用模型 · " + models.length + "（已调用 " + usedCount + "）")
          : ("可用模型 · " + models.length + "（暂无调用记录）");

        var usage = entry.usage || {};
        var list = document.createElement("div");
        list.className = "wb-am-list";
        models.forEach(function (m) {
          var tag = document.createElement("span");
          tag.className = used[m] ? "wb-am-tag wb-am-tag-used" : "wb-am-tag";
          tag.textContent = m;
          var stat = usage[m];
          if (stat) {
            var parts = [];
            if (stat.gatewayCalls) parts.push("网关调用 " + stat.gatewayCalls + " 次");
            if (stat.tokens) parts.push("约 " + stat.tokens + " Token");
            if (stat.requests != null) parts.push("官方记录 " + stat.requests + " 次");
            if (stat.credit != null) parts.push("积分 " + Math.round(stat.credit * 100) / 100);
            if (parts.length) tag.title = parts.join(" · ");
          } else if (used[m]) {
            tag.title = "该账号调用过此模型";
          }
          list.appendChild(tag);
        });

        box.appendChild(title);
        box.appendChild(list);
        (card.querySelector("section") || card).appendChild(box);
      });
    }

    if (wbModelsCache) {
      render(wbModelsCache);
      return;
    }
    fetch("/api/account-models", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) { wbModelsCache = data; render(data); })
      .catch(function () {});
  }

  var wbPoolCache = null;

  function effectiveEnabledIds(data) {
    if (data.allEnabledByDefault) {
      return (data.accounts || []).map(function (a) { return a.id; });
    }
    return (data.enabledAccountIds || []).slice();
  }

  function savePool(payload, done) {
    fetch("/api/account-pool", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res && res.status) { wbPoolCache = res.status; }
        if (done) done(res);
      })
      .catch(function () {});
  }

  function injectAccountPool() {
    // 官方那三个「设为当前账号 / 切换到 IDE / CLI」按钮依赖宿主桌面程序，
    // 已在二进制里物理移除。这里注入容器自己的账号池控制：
    //   [启用/停用] 决定该账号是否参与 API 调用；[设为首选] 固定只用这一个账号。
    var cards = Array.prototype.slice.call(document.querySelectorAll("article"));
    if (!cards.length) return;

    var pending = cards.filter(function (c) {
      return !c.querySelector(".wb-pool-bar") && c.querySelector("h3");
    });
    if (!pending.length) return;

    function render(data) {
      if (!data || !data.accounts) return;
      var byName = {};
      data.accounts.forEach(function (a) { if (a.name) byName[a.name] = a; });

      // 各账号被分摊到的请求次数（网关进程启动后累计）。
      var counts = {};
      (data.selectionCounts || []).forEach(function (c) { counts[c.accountId] = c.count; });
      var cliBoundId = data.cliActiveAccountId || null;

      pending.forEach(function (card) {
        if (card.querySelector(".wb-pool-bar")) return;
        var h3 = card.querySelector("h3");
        if (!h3) return;
        var acc = byName[(h3.textContent || "").trim()];
        if (!acc) return;

        var bar = document.createElement("div");
        bar.className = "wb-pool-bar";

        var toggle = document.createElement("button");
        toggle.className = "wb-pool-btn" + (acc.enabled ? " wb-pool-btn-on" : "");
        toggle.textContent = acc.enabled ? "参与调用 · 点击停用" : "已停用 · 点击启用";
        toggle.onclick = function () {
          var ids = effectiveEnabledIds(data);
          var idx = ids.indexOf(acc.id);
          if (idx >= 0) { ids.splice(idx, 1); } else { ids.push(acc.id); }
          var next = { enabledAccountIds: ids };
          if (idx >= 0 && data.mode === "manual" && data.manualAccountId === acc.id) {
            next.mode = "auto";
            next.manualAccountId = null;
          }
          savePool(next, function () { wbPoolCache = null; refreshPool(); });
        };

        var pinned = data.mode === "manual" && data.manualAccountId === acc.id;
        var pin = document.createElement("button");
        pin.className = "wb-pool-btn" + (pinned ? " wb-pool-btn-primary" : "");
        pin.textContent = pinned ? "首选账号 · 点击改回自动分配" : "设为首选";
        pin.onclick = function () {
          savePool(
            pinned
              ? { mode: "auto", manualAccountId: null }
              : { mode: "manual", manualAccountId: acc.id },
            function () { wbPoolCache = null; refreshPool(); }
          );
        };

        var count = document.createElement("span");
        var n = counts[acc.id] || 0;
        count.className = "wb-pool-count" + (n > 0 ? " wb-pool-count-hot" : "");
        count.textContent = "已调用 " + n + " 次";

        bar.appendChild(toggle);
        bar.appendChild(pin);
        bar.appendChild(count);

        // 「当前 CLI 账号」是官方底层的单账号绑定（只维护一个 activeAccountId），
        // 与网关账号池无关。只给被绑定的那张卡打标记，避免看起来「只有它有状态」。
        if (cliBoundId && cliBoundId === acc.id) {
          var bound = document.createElement("span");
          bound.className = "wb-pool-bound";
          bound.textContent = "官方 CLI 绑定";
          bound.title = "官方底层服务只维护一个 CLI 绑定账号，仅影响 CodeBuddy CLI 助手；"
            + "网关 API 调用按账号池在全部启用账号间分摊，与此无关。";
          bar.appendChild(bound);
        }

        if (data.lastSelectedAccountId && data.lastSelectedAccountId === acc.id) {
          var last = document.createElement("span");
          last.className = "wb-pool-last";
          last.textContent = "最近调用";
          bar.appendChild(last);
        }

        (card.querySelector("section") || card).appendChild(bar);
      });
    }

    if (wbPoolCache) { render(wbPoolCache); return; }
    fetch("/api/account-pool", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) { wbPoolCache = data; render(data); })
      .catch(function () {});
  }

  function refreshPool() {
    Array.prototype.slice.call(document.querySelectorAll(".wb-pool-bar")).forEach(function (el) {
      el.remove();
    });
    injectAccountPool();
  }

  function injectCliScopeNote() {
    // 设置页「CodeBuddy CLI 自动轮换」里的「当前 CLI 账号」是官方底层的单账号绑定，
    // 与网关账号池是两套机制。不加说明会让人以为「只有这一个账号在生效」。
    var section = document.querySelector("#settings-auto-rotate");
    if (!section || section.querySelector(".wb-cli-scope-note")) return;
    var note = document.createElement("div");
    note.className = "wb-cli-scope-note";
    note.textContent = "说明：此处「当前 CLI 账号」由官方底层服务（:57890）维护，全局只有一个，"
      + "仅决定 CodeBuddy CLI 助手绑定哪个账号；网关的 API 调用不受它影响，"
      + "会按账号池在全部「参与调用」的账号之间分摊。";
    var body = section.querySelector("div");
    if (body) { section.insertBefore(note, body); } else { section.appendChild(note); }
  }

  function run() {
    initCollapse();
    sanitizeMacUI();
    enforceTitle();
    injectAccountModels();
    injectAccountPool();
    injectCliScopeNote();
  }

  const observer = new MutationObserver(() => run());
  observer.observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("DOMContentLoaded", run);
})();
</script>
"""

def clean_mac_content(content: bytes) -> bytes:
    replacements = [
        (b"http://[IP]:57890", b""),
        (b"http://127.0.0.1:57890", b""),
        (b"http://localhost:57890", b""),
        (b":57890", b":18090"),
        (b"/icon-transparent.png", b"/icon.png"),
        (b"\xe5\x9c\xa8 Finder \xe4\xb8\xad\xe6\x98\xbe\xe7\xa4\xba", b"\xe5\x9c\xa8\xe6\x96\x87\xe4\xbb\xb6\xe7\xae\xa1\xe7\x90\x86\xe5\x99\xa8\xe4\xb8\xad\xe6\x98\xbe\xe7\xa4\xba"),
        (b"workbuddy-switch.app", b"workbuddy-switch"),
    ]
    for old, new in replacements:
        content = content.replace(old, new)

    # 文案适配：官方 UI 是给桌面客户端写的，容器里已无 IDE / CLI 能力。
    # 这里做**等长**字节替换，保证不破坏 JS 资源里的偏移与语法。
    #   按项目 -> 按账号            ：网关没有「项目」概念，只有账号归因
    #   消耗最高的会话 -> 消耗最高的调用：每条网关请求就是一次独立调用
    #   主页副标题                  ：去掉已移除的 IDE / CLI 措辞，改为容器真实能力
    text_replacements = [
        ("按项目", "按账号"),
        ("消耗最高的会话", "消耗最高的调用"),
        ("按本地聚合 Token 从高到低排列。", "按单次调用 Token 从高到低排列。"),
        (
            "统一管理 WorkBuddy、CodeBuddy IDE 与 CodeBuddy CLI 账号、积分和签到状态。",
            "统一管理 WorkBuddy、账号池与 OpenAI 兼容网关服务、积分和签到状态。",
        ),
    ]
    for old, new in text_replacements:
        old_b = old.encode("utf-8")
        new_b = new.encode("utf-8")
        if len(old_b) != len(new_b):
            raise ValueError(f"等长替换被破坏: {old} ({len(old_b)}) != {new} ({len(new_b)})")
        content = content.replace(old_b, new_b)

    return content

@app.get("/icon.png")
@app.get("/icon-transparent.png")
@app.get("/favicon.ico")
async def get_icon():
    if ICON_PATH.exists():
        with open(ICON_PATH, "rb") as f:
            content = f.read()
        return Response(content=content, media_type="image/png")
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BACKEND_URL}/icon.png")
        return Response(content=r.content, media_type="image/png")

@app.get("/api/token-stats")
async def token_stats_api(request: Request):
    # 容器环境核心增强：接管 /api/token-stats，返回网关实测 Token 统计数据
    stats = get_aggregated_token_stats()
    return stats

GATEWAY_BASE_URL = os.getenv("WB_GATEWAY_BASE_URL", "http://127.0.0.1:18091")
GATEWAY_MODELS_URL = os.getenv("WB_GATEWAY_MODELS_URL", GATEWAY_BASE_URL + "/v1/models")


@app.get("/api/account-pool")
async def account_pool_get():
    """转发到网关的账号池状态（WebUI 控制台用）。

    额外并入官方底层的 CodeBuddy CLI 绑定账号：这是**单账号**概念（官方只维护一个
    activeAccountId），与网关的账号池是两套东西，前端需要把两者区分展示。
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(GATEWAY_BASE_URL + "/account-pool/status")
            try:
                payload = r.json() or {}
            except Exception:
                return Response(content=r.content, status_code=r.status_code,
                                media_type="application/json")
            try:
                cli = await client.get(BACKEND_URL + "/api/codebuddy-cli/status")
                cli_data = cli.json() or {}
                payload["cliActiveAccountId"] = cli_data.get("activeAccountId")
                payload["cliActiveAccountName"] = cli_data.get("activeAccountName")
                payload["cliConfigured"] = cli_data.get("configured")
            except Exception:
                payload["cliActiveAccountId"] = None
                payload["cliActiveAccountName"] = None
                payload["cliConfigured"] = None
            return Response(content=json.dumps(payload, ensure_ascii=False),
                            status_code=r.status_code, media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.put("/api/account-pool")
async def account_pool_put(request: Request):
    """转发账号池配置更新（模式 / 启用列表 / 首选账号）。"""
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.put(GATEWAY_BASE_URL + "/account-pool/config",
                                 content=body,
                                 headers={"Content-Type": "application/json"})
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.get("/api/account-pool/selections")
async def account_pool_selections(limit: int = 50):
    """转发选账号分摊流水：各账号被分摊到的请求次数 + 最近明细。"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(GATEWAY_BASE_URL + "/account-pool/selections",
                                 params={"limit": limit})
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.post("/api/account-pool/selections/reset")
async def account_pool_selections_reset():
    """清空选账号流水，便于重新观测并发分摊。"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(GATEWAY_BASE_URL + "/account-pool/selections/reset")
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


async def _fetch_gateway_catalog() -> list:
    """取网关（18091）可路由的完整模型清单。

    网关的清单 = 内置基础清单 + 从官方 usage 自动发现出来的模型，是「当前能调
    用哪些模型」的唯一权威来源。取不到时降级为空数组，由调用方回退到 usage。
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(GATEWAY_MODELS_URL)
            if r.status_code != 200:
                return []
            data = r.json() or {}
            return [m.get("id") for m in (data.get("data") or []) if m.get("id")]
    except Exception:
        return []


@app.get("/api/account-models")
async def account_models_api():
    """按账号返回「可用模型 + 已调用模型」，供账号卡片动态展示。

    两个数据源合并，缺一不可：

    1. **网关自身流水**（`token_stats_logs.json`，带 accountId/accountName）——
       实时、覆盖所有账号，是「这个账号到底调用过哪些模型」的权威来源。
    2. **官方 usage 缓存**（`official_usage_cache.json`）—— 上游只对「当前账号」
       返回模型明细，其余账号的 `models` 恒为空，因此只能作为补充。

    只靠官方缓存会出现「只有一个账号有调用记录」的假象，这正是必须叠加网关归因的原因。
    """
    import json as _json
    data_dir = Path(os.getenv("WB_DATA_DIR", "/data/.wb-switch"))
    cache_file = data_dir / "official_usage_cache.json"
    tracker_file = data_dir / "token_stats_logs.json"

    catalog = await _fetch_gateway_catalog()
    result = {"accounts": {}, "catalog": catalog, "discovered": []}

    agg = {}

    def _touch(account_id, account_name):
        entry = agg.get(account_id)
        if entry is None:
            entry = {"name": account_name, "used": set(), "usage": {}, "gatewayCalls": 0}
            agg[account_id] = entry
        elif not entry.get("name") and account_name:
            entry["name"] = account_name
        return entry

    # ---- 来源 1：网关自身归因（实时、全账号） ----
    try:
        with open(tracker_file, "r", encoding="utf-8") as f:
            tracker_logs = _json.load(f) or []
    except Exception:
        tracker_logs = []

    for rec in tracker_logs:
        if not isinstance(rec, dict):
            continue
        aid = rec.get("accountId")
        model = rec.get("model")
        if not aid or not model:
            continue
        entry = _touch(str(aid), rec.get("accountName"))
        entry["used"].add(str(model))
        entry["gatewayCalls"] += 1
        stat = entry["usage"].setdefault(str(model), {"requests": 0, "credit": None, "gatewayCalls": 0})
        stat["requests"] = (stat.get("requests") or 0) + 1
        stat["gatewayCalls"] = (stat.get("gatewayCalls") or 0) + 1
        stat["tokens"] = (stat.get("tokens") or 0) + int(rec.get("total") or 0)

    # ---- 来源 2：官方 usage 缓存（可能只覆盖当前账号） ----
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            payload = (_json.load(f) or {}).get("payload") or {}
    except Exception:
        payload = {}

    for acc in payload.get("accounts") or []:
        if not isinstance(acc, dict) or not acc.get("accountId"):
            continue
        entry = _touch(acc["accountId"], acc.get("accountName"))
        for day in acc.get("daily") or []:
            for m in (day or {}).get("models") or []:
                if isinstance(m, dict) and m.get("model"):
                    entry["used"].add(m["model"])

    # payload.models / requests 补充调用次数与积分，并兜底补齐 model
    for item in payload.get("models") or []:
        if isinstance(item, dict) and item.get("model"):
            for aid in list(agg.keys()):
                stat = agg[aid]["usage"].setdefault(item["model"], {"requests": None, "credit": None})
                if stat.get("credit") is None:
                    stat["credit"] = item.get("credit")

    for req in payload.get("requests") or []:
        if not isinstance(req, dict) or not req.get("accountId") or not req.get("model"):
            continue
        entry = _touch(req["accountId"], req.get("accountName"))
        entry["used"].add(req["model"])

    all_used = set()
    for aid, entry in agg.items():
        used = sorted(entry["used"])
        all_used.update(used)
        result["accounts"][aid] = {
            "name": entry.get("name"),
            # 可用清单与网关保持一致，新账号也不会是空的
            "models": catalog or sorted(entry["used"]),
            "used": used,
            "usage": entry.get("usage") or {},
            "gatewayCalls": entry.get("gatewayCalls", 0),
        }
    result["discovered"] = sorted(all_used)
    return result

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"])
async def proxy_all(request: Request, path: str):
    url = f"{BACKEND_URL}/{path}"
    query = str(request.query_params)
    if query:
        url = f"{url}?{query}"
    
    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.request(
            method=request.method,
            url=url,
            headers=headers,
            content=body
        )
        content = r.content
        media_type = r.headers.get("content-type", "")
        
        is_html = "text/html" in media_type or content.lstrip().startswith(b"<!doctype html") or content.lstrip().startswith(b"<html")
        
        res_headers = {k: v for k, v in r.headers.items() if k.lower() not in ["content-encoding", "content-length", "transfer-encoding"]}
        
        if is_html:
            res_headers["content-type"] = "text/html; charset=utf-8"
            content = clean_mac_content(content)
            if b"</body>" in content:
                content = content.replace(b"</body>", f"{COLLAPSE_SCRIPT}</body>".encode("utf-8"))
            # 注入脚本与文案替换都发生在响应阶段，而 assets 文件名带 hash 不会变。
            # 不禁止缓存的话，浏览器会一直用旧副本，表现为「改了但没生效」。
            res_headers["cache-control"] = "no-store, must-revalidate"
        elif "javascript" in media_type:
            content = clean_mac_content(content)
            res_headers["cache-control"] = "no-store, must-revalidate"
            
        return Response(
            content=content,
            status_code=r.status_code,
            headers=res_headers
        )

if __name__ == "__main__":
    port = int(os.getenv("PORT", 18090))
    uvicorn.run(app, host="0.0.0.0", port=port)
