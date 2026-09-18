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
  /* 容器内无宿主桌面客户端，「CodeBuddy IDE / CLI 当前账号」状态徽章（绿色对勾）
     永远只能显示「未运行 / 未安装」，属误导。用属性选择器直接命中，无需 JS 介入 */
  [role="status"][aria-label="CodeBuddy IDE 当前账号"],
  [role="status"][aria-label="CodeBuddy CLI 当前账号"] {
    display: none !important;
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
  // 账号卡片上的切换按钮：会启动宿主机 WorkBuddy / CodeBuddy IDE / CodeBuddy CLI，
  // 或把凭证写进宿主机目录，容器内没有对应可执行文件与桌面环境，点击必然失败。
  var WB_UNSUPPORTED_LABELS = [
    "设为 WorkBuddy 当前账号",
    "切换到 CodeBuddy IDE",
    "正在切换 CodeBuddy IDE",
    "设为 CodeBuddy CLI 当前账号",
    "正在切换 CodeBuddy CLI 当前账号"
  ];

  // 右上角/卡片里的「当前账号」状态图标（role=status）。
  // 容器内只有 WorkBuddy 账号是真实可用的；IDE 与 CLI 没有宿主程序，
  // 官方仍会因状态文件存在而误报为已接入，这里只保留真实可用的那个。
  var WB_UNSUPPORTED_STATUS = [
    "CodeBuddy IDE 当前账号",
    "CodeBuddy CLI 当前账号"
  ];

  // 需要按文案匹配的按钮（设置页里的 CLI 接入入口）
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
        return;
      }

      const label = (el.getAttribute("aria-label") || "").trim();

      // 账号卡片上的切换按钮：容器内无法执行，隐藏
      if (label && WB_UNSUPPORTED_LABELS.some(k => label === k || label.indexOf(k) === 0)) {
        el.classList.add("wb-mac-btn-hide");
        return;
      }

      // 右上角「当前账号」状态图标：IDE / CLI 在容器内没有宿主程序，
      // 官方仅凭状态文件误报为已接入，这里只保留真实可用的 WorkBuddy 图标
      if (label && WB_UNSUPPORTED_STATUS.indexOf(label) !== -1) {
        el.classList.add("wb-mac-btn-hide");
      }
    });

    // 右上角那组桌面程序状态图标（WorkBuddy / CodeBuddy IDE / CodeBuddy CLI 是否在运行）：
    // 检测对象是宿主机上的桌面客户端，容器里根本不存在，状态恒为「未运行 / 未安装」，
    // 悬停提示反而误导，整组移除。
    var statusIcons = Array.prototype.slice.call(document.querySelectorAll("span"))
      .filter(function (el) {
        return el.classList.contains("group") && el.classList.contains("relative") &&
          el.classList.contains("inline-flex") && el.classList.contains("cursor-default");
      });
    statusIcons.forEach(function (el) { el.classList.add("wb-mac-btn-hide"); });
    statusIcons.forEach(function (el) {
      var parent = el.parentElement;
      if (!parent || parent.classList.contains("wb-mac-btn-hide")) return;
      var left = Array.prototype.slice.call(parent.children).some(function (child) {
        return !child.classList.contains("wb-mac-btn-hide");
      });
      if (!left) parent.classList.add("wb-mac-btn-hide");
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
            if (stat.requests != null) parts.push("调用 " + stat.requests + " 次");
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

  function run() {
    initCollapse();
    sanitizeMacUI();
    enforceTitle();
    injectAccountModels();
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

GATEWAY_MODELS_URL = os.getenv("WB_GATEWAY_MODELS_URL", "http://127.0.0.1:18091/v1/models")


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

    上游没有「按账号返回可用模型」的接口。可用清单取网关 /v1/models（自动发现，
    不硬编码）；已调用清单由官方 usage 缓存里带 accountId + model 的记录聚合得出。
    没有任何调用记录的新账号同样会拿到完整可用清单，只是 used 为空。
    """
    import json as _json
    data_dir = Path(os.getenv("WB_DATA_DIR", "/data/.wb-switch"))
    cache_file = data_dir / "official_usage_cache.json"

    catalog = await _fetch_gateway_catalog()
    result = {"accounts": {}, "catalog": catalog, "discovered": []}

    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            payload = (_json.load(f) or {}).get("payload") or {}
    except Exception:
        payload = {}

    agg = {}

    def _touch(account_id, account_name):
        entry = agg.get(account_id)
        if entry is None:
            entry = {"name": account_name, "used": set(), "usage": {}}
            agg[account_id] = entry
        elif not entry.get("name") and account_name:
            entry["name"] = account_name
        return entry

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
                agg[aid]["usage"].setdefault(item["model"], {
                    "requests": item.get("requestCount"),
                    "credit": item.get("credit"),
                })

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
        elif "javascript" in media_type:
            content = clean_mac_content(content)
            
        return Response(
            content=content,
            status_code=r.status_code,
            headers=res_headers
        )

if __name__ == "__main__":
    port = int(os.getenv("PORT", 18090))
    uvicorn.run(app, host="0.0.0.0", port=port)
