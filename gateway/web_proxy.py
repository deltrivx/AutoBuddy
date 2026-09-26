import db
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, Dict, Any
from fastapi import FastAPI, Request, Response
import httpx
import uvicorn

try:
    from gateway import account_policy
except ImportError:
    import account_policy

try:
    from gateway.token_tracker import get_aggregated_token_stats
except ImportError:
    from token_tracker import get_aggregated_token_stats

app = FastAPI()

# ---------------------------------------------------------------------------
# 容器内的内部调用（WebUI 代理 -> 官方后端 57890 / -> 网关 18091）全部走 loopback。
#
# httpx 默认 trust_env=True，会读取宿主机的 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY。
# NAS 上为了下载或科学上网设了全局代理的环境很常见，一旦被代理接管，内部调用会被
# 转发到代理上并返回 404 —— 表现为「UI 打不开 / 模型清单为空」，但网关本身是好的。
# 内部调用一律 trust_env=False，绕开代理环境变量。
#
# 注意：网关（main.py）访问上游模型服务的客户端**不能**这样改，
# 那里恰恰需要代理环境变量才能出网。
# ---------------------------------------------------------------------------
def _internal_client(timeout: float = 10.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, trust_env=False)



BACKEND_URL = "http://127.0.0.1:57890"
ICON_PATH = Path("/app/icon.png")

COLLAPSE_SCRIPT = r"""
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
  /* 收拢态导航悬停提示：图标模式下用悬浮气泡补偿名称，收拢后零文字残留 */
  aside.wb-collapsed nav a { position: relative; }
  aside.wb-collapsed nav a:hover::after {
    content: attr(data-wb-label);
    position: absolute;
    left: calc(100% + 12px);
    top: 50%;
    transform: translateY(-50%);
    background: var(--card, #ffffff);
    color: var(--foreground, #0f172a);
    border: 1px solid var(--border, rgba(120,120,120,0.25));
    box-shadow: 0 4px 14px rgba(0,0,0,0.14);
    border-radius: 8px;
    padding: 5px 10px;
    font-size: 12px;
    white-space: nowrap;
    z-index: 80;
    pointer-events: none;
  }
  /* 右下角用户球（头像 + 昵称，窄屏只留头像）与弹出层 */
  #wb-user-orb {
    position: fixed;
    right: 18px;
    bottom: 18px;
    z-index: 9800;
    display: flex;
    align-items: center;
    gap: 0;
    padding: 0;
    width: 44px;
    height: 44px;
    justify-content: center;
    border-radius: 50%;
    background: var(--card, #ffffff);
    border: 1px solid var(--border, rgba(120,120,120,0.25));
    box-shadow: 0 6px 18px rgba(0,0,0,0.14);
    cursor: pointer;
    user-select: none;
    -webkit-user-select: none;
    overflow: hidden;
    transition: width 0.18s ease, border-radius 0.18s ease, padding 0.18s ease, gap 0.18s ease;
  }
  /* 展开态：改回胶囊并露出昵称（点击或悬浮均可） */
  #wb-user-orb.wb-orb-open {
    width: auto;
    height: 44px;
    padding: 0 14px 0 5px;
    gap: 8px;
    border-radius: 999px;
  }
  #wb-user-orb img {
    width: 36px;
    height: 36px;
    border-radius: 50%;
    object-fit: cover;
    display: block;
    flex-shrink: 0;
    margin: 0;
  }
  #wb-user-orb .wb-orb-nick {
    display: none;
    max-width: 140px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 12.5px;
    font-weight: 600;
    color: var(--foreground, #0f172a);
  }
  /* 平时只显示头像；点击展开浮窗时才露出昵称 */
  #wb-user-orb.wb-orb-open .wb-orb-nick { display: inline; }
  @media (max-width: 720px) {
    #wb-user-orb { width: 40px; height: 40px; right: 14px; bottom: 14px; }
    #wb-user-orb img { width: 32px; height: 32px; }
  }
  /* 官方图标是方形 App 图标：后端已用圆形 SVG 出口兜底，前端只负责保持正圆 */
  img.wb-avatar-round {
    border-radius: 50% !important;
    object-fit: cover;
    background: var(--muted, rgba(120,120,120,0.12));
    box-sizing: border-box;
  }
  /* 右下角浮窗：有意义的信息行（剩余积分 / 今日消耗 / 账号数量） */
  .wb-pop-stats { margin-top: 10px; display: flex; flex-direction: column; gap: 7px; }
  /* 密码掩码：等宽字体保证 * 等宽，视觉上能反映真实位数 */
  .wb-pwd-mask { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 14px; letter-spacing: 2px; color: var(--muted-foreground, #64748b); }
  .wb-pop-stat { display: flex; align-items: center; justify-content: space-between; gap: 10px; font-size: 12px; }
  .wb-pop-stat-k { color: var(--muted-foreground, #64748b); }
  .wb-pop-stat-v { font-weight: 600; font-variant-numeric: tabular-nums; }
  /* Buddy 旅行三态徽章：同一枚官方飛機图标，仅以「线型 + 颜色」区分状态。
     无 Buddy = 虚线灰 / 未旅行 = 实线中性 / 旅行中 = 实线主题色 */
  .wb-nobuddy-badge {
    display: inline-flex !important;
    align-items: center;
    gap: 4px;
    border: none !important;
    border-radius: 999px;
    padding: 2px 4px;
    vertical-align: middle;
  }
  /* 无 Buddy：虚线描边（未领养，无伙伴）——灰调 */
  .wb-buddy-badge { display: inline-flex !important; align-items: center; gap: 4px; vertical-align: middle; }
  .wb-buddy-badge svg { flex-shrink: 0; }
  .wb-buddy-nobuddy { color: #94a3b8; }
  .wb-buddy-nobuddy svg { stroke-dasharray: 3 2.4; }
  /* 未旅行：实线（已领养，伙伴在家待命） */
  .wb-buddy-idle { color: var(--muted-foreground, #64748b); }
  /* 旅行中：实线 + 主题色（伙伴出门旅行） */
  .wb-buddy-traveling { color: var(--primary, #3b82f6); }
  #wb-user-pop {
    position: fixed;
    right: 18px;
    bottom: 64px;
    z-index: 9801;
    width: 264px;
    background: var(--card, #ffffff);
    color: var(--foreground, #0f172a);
    border: 1px solid var(--border, rgba(120,120,120,0.25));
    border-radius: 14px;
    box-shadow: 0 14px 34px rgba(0,0,0,0.2);
    padding: 14px;
    font-size: 12.5px;
  }
  @media (max-width: 720px) { #wb-user-pop { right: 12px; bottom: 60px; } }
  .wb-pop-head { display: flex; align-items: center; gap: 10px; padding-bottom: 10px; border-bottom: 1px solid var(--border, rgba(120,120,120,0.18)); }
  .wb-pop-head img { width: 40px; height: 40px; border-radius: 50%; object-fit: cover; }
  .wb-pop-menu { display: flex; flex-direction: column; gap: 2px; margin-top: 8px; }
  .wb-pop-item { display: block; width: 100%; text-align: left; background: transparent; border: none; padding: 8px 10px; border-radius: 8px; font-size: 12.5px; color: inherit; cursor: pointer; }
  .wb-pop-item:hover:not(:disabled) { background: var(--muted, rgba(120,120,120,0.12)); }
  .wb-pop-item:disabled { opacity: 0.5; cursor: not-allowed; }
  .wb-pop-item.danger { color: #ef4444; }
  .wb-modal-input { width: 100%; padding: 8px 12px; border-radius: 8px; border: 1px solid var(--border, rgba(120,120,120,0.3)); background: var(--background, #ffffff); color: inherit; font-size: 13px; box-sizing: border-box; }
  .wb-modal-btn { width: 100%; padding: 9px; border-radius: 8px; background: #3b82f6; color: #ffffff; font-size: 13px; font-weight: 600; border: none; cursor: pointer; }
  .wb-modal-btn:disabled { opacity: 0.6; cursor: default; }
  .wb-modal-err { color: #ef4444; font-size: 12px; display: none; }
  .wb-avatar-pick { display: flex; align-items: flex-start; gap: 12px; }
  .wb-avatar-pick img { width: 52px; height: 52px; border-radius: 50%; object-fit: cover; border: 1px solid var(--border, rgba(120,120,120,0.3)); flex-shrink: 0; }
  .wb-avatar-grid { display: flex; flex-wrap: wrap; gap: 8px; }
  /* 头像选择器与「上传自定义头像」共用同一行：
     官方 + 6 个预设 = 7 个 42px 圆钮，加上上传钮刚好排满一行，
     上传钮不再被压到选择器下方单独占一行。
     wrap 保留：窄屏（手机）放不下时自然换行，不会挤出容器。 */
  .wb-avatar-grid-inline { flex: 1; min-width: 0; align-items: center; }
  .wb-avatar-upload {
    width: 42px !important;
    height: 42px !important;
    border-radius: 50% !important;
    padding: 0 !important;
    display: inline-flex !important;
    align-items: center;
    justify-content: center;
    font-size: 20px;
    line-height: 1;
    color: var(--muted-foreground, #64748b);
    border: 2px dashed var(--border, rgba(120,120,120,0.45)) !important;
    background: transparent !important;
    cursor: pointer;
    box-sizing: border-box;
    flex-shrink: 0;
    transition: color .15s, border-color .15s;
  }
  .wb-avatar-upload:hover {
    color: var(--primary, #3b82f6);
    border-color: var(--primary, #3b82f6) !important;
  }
  .wb-avatar-opt { width: 42px; height: 42px; border-radius: 50%; border: 2px solid transparent; padding: 0; background: transparent; cursor: pointer; overflow: hidden; box-sizing: border-box; flex-shrink: 0; }
  .wb-avatar-opt img { width: 100%; height: 100%; border-radius: 50%; object-fit: cover; display: block; }
  .wb-avatar-opt.active { border-color: #3b82f6; }
  .wb-avatar-pick button { line-height: 1; }
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
  .wb-pool-btn-auto {
    border-color: rgba(217, 119, 6, 0.55);
    background: rgba(217, 119, 6, 0.14);
    color: #b45309;
  }
  /* 一致性自检的结果块。每行 = 一个账号的「凭据状态 + 姓名 + 一致性结论 + 建议」。
     窄屏时靠 flex-wrap 自然折行，不写死列宽 —— 写死会让长建议被裁掉。 */
  .wb-audit-box { gap: 8px; }
  .wb-audit-row {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 8px;
    padding: 6px 0;
    border-bottom: 1px solid var(--border, rgba(120, 120, 120, 0.15));
    min-width: 0;
  }
  .wb-audit-row:last-child { border-bottom: 0; }
  .wb-audit-state {
    flex: 0 0 auto;
    font-size: 11px;
    font-weight: 600;
    padding: 1px 7px;
    border-radius: 999px;
    border: 1px solid var(--border, rgba(120, 120, 120, 0.35));
  }
  .wb-audit-ok { color: #047857; border-color: rgba(4, 120, 87, 0.45); background: rgba(4, 120, 87, 0.1); }
  .wb-audit-warn { color: #b45309; border-color: rgba(217, 119, 6, 0.5); background: rgba(217, 119, 6, 0.12); }
  .wb-audit-bad { color: #b91c1c; border-color: rgba(185, 28, 28, 0.5); background: rgba(185, 28, 28, 0.1); }
  .wb-audit-name { flex: 0 0 auto; font-weight: 600; min-width: 0; }
  .wb-audit-consistency { flex: 0 0 auto; font-size: 12px; }
  .wb-audit-detail {
    flex: 1 1 100%;
    min-width: 0;
    font-size: 11px;
    opacity: 0.72;
    line-height: 1.5;
  }
  .wb-audit-advice {
    flex: 1 1 240px;
    min-width: 0;
    font-size: 11px;
    opacity: 0.75;
    line-height: 1.5;
  }
  .wb-audit-criteria { font-size: 11px; opacity: 0.65; }
  .wb-pool-hint {
    font-size: 11px;
    color: var(--muted-foreground, #6b7280);
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
    /* flex + wrap：徽标与「全部恢复」按钮同排，按钮靠 margin-left:auto 右对齐。
       窄屏放不下时整行折行，不会把按钮挤出卡片。 */
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 2px 0;
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
  /* 模型标签可点击：点一下禁用该模型对该账号的调用，再点恢复。
     默认态刻意不加边框/下划线，避免一排标签看起来像按钮墙；
     只有悬停与键盘聚焦时才显形，暗示「这是可以点的」。 */
  .wb-am-tag-click {
    cursor: pointer;
    user-select: none;
    transition: background 0.12s ease, color 0.12s ease, box-shadow 0.12s ease;
  }
  .wb-am-tag-click:hover {
    box-shadow: inset 0 0 0 1px rgba(120, 120, 120, 0.55);
  }
  .wb-am-tag-click:focus-visible {
    outline: 2px solid rgba(59, 130, 246, 0.65);
    outline-offset: 1px;
  }
  /* 禁用态：压暗 + 删除线。用「灰掉」而不是「红掉」——
     红色读起来像出错告警，而这里是用户主动的选择，不是故障。 */
  .wb-am-tag-off {
    background: rgba(120, 120, 120, 0.10);
    color: var(--muted-foreground, #9ca3af);
    text-decoration: line-through;
    font-weight: 400;
  }
  .wb-am-tag-off:hover {
    box-shadow: inset 0 0 0 1px rgba(120, 120, 120, 0.35);
  }
  /* 被**手动**禁用的模型数。只数人点的那些：巡检写的另有 .wb-am-offcount-auto，
     两个数互不重叠，免得同一批模型被数两遍。 */
  .wb-am-offcount {
    font-size: 10px;
    line-height: 1.7;
    padding: 1px 7px;
    border-radius: 999px;
    border: 1px dashed rgba(120, 120, 120, 0.45);
    color: var(--muted-foreground, #6b7280);
    margin-left: 6px;
    font-variant-numeric: tabular-nums;
  }
  /* 巡检自动禁用：与手动禁用一样「不参与调用」，但来源不同 ——
     它由可用性巡检写入、模型恢复后会自行解除，所以用琥珀色虚线区分，
     让用户一眼看出「这不是我关的」。计数上同样与手动禁用分开。 */
  .wb-am-tag-auto {
    background: rgba(245, 158, 11, 0.14);
    color: #b45309;
    text-decoration: line-through;
    font-weight: 400;
    box-shadow: inset 0 0 0 1px rgba(245, 158, 11, 0.32);
  }
  .wb-am-tag-auto:hover {
    box-shadow: inset 0 0 0 1px rgba(245, 158, 11, 0.6);
  }
  .wb-am-offcount-auto {
    border-color: rgba(245, 158, 11, 0.5);
    color: #b45309;
  }
  /* 一键恢复：把该账号被禁用的模型全部放回轮询。
     做成文字按钮而非图标 —— 它改变的是路由行为，值得让人先看清「恢复几个」。
     与左侧两个徽标同一行右对齐，作用域天然读作「这张卡片」。 */
  .wb-am-restore {
    margin-left: auto;
    font: inherit;
    font-size: 11px;
    line-height: 1.6;
    padding: 2px 10px;
    border-radius: 6px;
    cursor: pointer;
    color: var(--primary, #1d4ed8);
    background: transparent;
    border: 1px solid rgba(29, 78, 216, 0.35);
    transition: background .15s ease, border-color .15s ease;
  }
  .wb-am-restore:hover:not(:disabled) {
    background: rgba(29, 78, 216, 0.08);
    border-color: rgba(29, 78, 216, 0.6);
  }
  .wb-am-restore:disabled { opacity: .6; cursor: default; }
  /* ---------------- 设置页：API 接入面板 ---------------- */
  .wb-api-card {
    display: flex;
    flex-direction: column;
    min-width: 0;
    background: var(--card, #ffffff);
    color: var(--card-foreground, #0f172a);
    border: 1px solid var(--border, rgba(120, 120, 120, 0.25));
    border-radius: 12px;
    overflow: hidden;
  }
  .wb-api-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    min-width: 0;
    margin: 0 16px;
    padding: 10px 0;
    border-bottom: 1px solid var(--border, rgba(120, 120, 120, 0.2));
  }
  @media (min-width: 640px) { .wb-api-row { margin: 0 20px; } }
  .wb-api-row:last-child { border-bottom: 0; }
  .wb-api-row-stack { flex-direction: column; align-items: stretch; }
  /* 窄屏下横排会把左边的说明文字压成一条竖线（实测 13px 宽），
     因为右侧的按钮/代码块不肯让位。改成纵向堆叠，各自吃满整行。 */
  @media (max-width: 560px) {
    .wb-api-row {
      flex-direction: column;
      align-items: stretch;
      gap: 8px;
    }
    .wb-api-row > * { max-width: 100%; }
  }
  .wb-api-main { min-width: 0; flex: 1 1 auto; }
  .wb-api-label {
    font-size: 13px;
    line-height: 1.5;
    font-weight: 500;
    color: var(--foreground, #0f172a);
  }
  .wb-api-desc {
    margin-top: 2px;
    font-size: 12px;
    line-height: 1.6;
    color: var(--muted-foreground, #64748b);
  }
  .wb-api-mono {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12px;
  }
  .wb-api-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    max-width: 100%;
    padding: 3px 8px;
    border-radius: 6px;
    background: var(--muted, rgba(120, 120, 120, 0.12));
    color: var(--foreground, #0f172a);
  }
  .wb-api-chip > span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .wb-api-input {
    flex: 1 1 auto;
    min-width: 0;
    padding: 5px 9px;
    border-radius: 6px;
    border: 1px solid var(--border, rgba(120, 120, 120, 0.35));
    background: var(--background, #ffffff);
    color: var(--foreground, #0f172a);
    font-size: 12px;
  }
  .wb-api-input:focus { outline: 2px solid var(--primary, #1d4ed8); outline-offset: 1px; }
  .wb-api-btn {
    flex: 0 0 auto;
    font-size: 11px;
    line-height: 1.9;
    padding: 1px 10px;
    border-radius: 999px;
    border: 1px solid var(--border, rgba(120, 120, 120, 0.35));
    background: transparent;
    color: var(--foreground, #374151);
    cursor: pointer;
    white-space: nowrap;
  }
  .wb-api-btn:hover { background: rgba(120, 120, 120, 0.12); }
  .wb-api-btn-primary {
    border-color: rgba(59, 130, 246, 0.55);
    background: rgba(59, 130, 246, 0.14);
    color: #1d4ed8;
    font-weight: 600;
  }
  .wb-api-btn-danger {
    border-color: rgba(239, 68, 68, 0.5);
    background: rgba(239, 68, 68, 0.1);
    color: #b91c1c;
  }
  .wb-api-btn-on {
    border-color: rgba(34, 197, 94, 0.55);
    background: rgba(34, 197, 94, 0.14);
    color: #15803d;
    font-weight: 600;
  }
  .wb-api-badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    line-height: 1.9;
    padding: 1px 9px;
    border-radius: 999px;
    background: var(--muted, rgba(120, 120, 120, 0.14));
    color: var(--muted-foreground, #64748b);
    white-space: nowrap;
  }
  .wb-api-badge-ok { background: rgba(34, 197, 94, 0.16); color: #15803d; font-weight: 600; }
  .wb-api-badge-warn { background: rgba(245, 158, 11, 0.18); color: #b45309; font-weight: 600; }
  .wb-api-keyrow {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    flex-wrap: wrap;
    padding: 8px 0;
    border-bottom: 1px dashed var(--border, rgba(120, 120, 120, 0.2));
  }
  .wb-api-keyrow:last-child { border-bottom: 0; }
  .wb-api-keyname {
    font-size: 12px;
    font-weight: 500;
    color: var(--foreground, #0f172a);
    max-width: 190px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .wb-api-code {
    margin-top: 6px;
    padding: 10px 12px;
    border-radius: 8px;
    background: var(--muted, rgba(120, 120, 120, 0.12));
    color: var(--foreground, #0f172a);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 11.5px;
    line-height: 1.7;
    white-space: pre;
    overflow-x: auto;
  }
  .wb-api-empty {
    padding: 10px 0;
    font-size: 12px;
    line-height: 1.7;
    color: var(--muted-foreground, #64748b);
  }
  .wb-api-toast {
    position: fixed;
    left: 50%;
    bottom: 28px;
    z-index: 9999;
    transform: translateX(-50%);
    padding: 8px 16px;
    /* 圆角不再用 999px：结论文案是多行的（账号名 + 结论 + 依据 + 建议），
       胶囊形在多行下两端会被拉出怪异的弧形，移动端尤其明显。 */
    border-radius: 10px;
    font-size: 12.5px;
    line-height: 1.6;
    /* 必须限制宽度并允许换行。此前只有 padding 没有上限，
       长文案会把 toast 撑得比视口还宽，两端被裁掉、整块像贴歪的色带。 */
    max-width: min(560px, calc(100vw - 32px));
    box-sizing: border-box;
    white-space: normal;
    overflow-wrap: anywhere;
    text-align: left;
    /* 默认档（纯告知）跟随主题前景/背景，而不是写死深色 ——
       写死深色在浅色主题下就是一块突兀的黑条。 */
    color: var(--background, #ffffff);
    background: color-mix(in srgb, var(--foreground, #0f172a) 92%, transparent);
    box-shadow: 0 8px 24px rgba(15, 23, 42, 0.25);
    pointer-events: none;
    opacity: 0;
    transition: opacity 0.2s ease, transform 0.2s ease;
  }
  .wb-api-toast-show { opacity: 1; transform: translateX(-50%) translateY(-4px); }
  /* 三档语义配色。此前只有「普通 / 红色错误」两档，「账号受限」被并进红色档，
     在语义上错误地暗示了「凭据坏了」—— 而它的凭据其实是好的。
     颜色只用于区分**该不该动手**，不用于表达严重程度：绿=无需处理，
     琥珀=需要关注但重登没用，红=需要你介入（重新登录）。

     选择器同时列 err 与 error 两种写法：调用方历史上混用过
     （`wbToast(msg, "error")` 与 `wb-api-toast-err`），只写一个会让另一个
     静默落到默认档 —— 红色提示看起来像普通提示，正是「背景很奇怪」的来源。 */
  .wb-api-toast-ok {
    color: #ffffff;
    background: rgba(4, 120, 87, 0.95);
  }
  .wb-api-toast-warn {
    color: #ffffff;
    background: rgba(180, 83, 9, 0.95);
  }
  .wb-api-toast-err,
  .wb-api-toast-error { background: rgba(185, 28, 28, 0.95); }
  @media (max-width: 560px) {
    .wb-api-toast {
      bottom: 16px;
      padding: 9px 13px;
      font-size: 12px;
      /* 窄屏再收一点边距，避免贴边；圆角同步收小更好看。 */
      max-width: calc(100vw - 20px);
      border-radius: 9px;
    }
  }
  /* ---------------- 设置页：关于面板 ----------------
     这一块是设置页的最后一段：容器是自建项目，用户需要知道它是什么、
     跑的是哪个镜像、出问题去哪里看，否则只能靠翻文档回忆。 */
  .wb-about-head {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 2px;
  }
  .wb-about-name {
    font-size: 13px;
    font-weight: 600;
    line-height: 1.6;
    color: var(--foreground, #374151);
  }
  .wb-about-links {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 2px;
  }
  .wb-about-link {
    font-size: 11px;
    line-height: 1.7;
    padding: 1px 9px;
    border-radius: 999px;
    border: 1px solid rgba(120, 120, 120, 0.35);
    color: var(--foreground, #374151);
    text-decoration: none;
    white-space: nowrap;
    transition: background 0.12s ease, border-color 0.12s ease;
  }
  .wb-about-link:hover {
    background: rgba(120, 120, 120, 0.12);
    border-color: rgba(120, 120, 120, 0.55);
  }
  .wb-about-note {
    font-size: 10px;
    line-height: 1.8;
    color: var(--muted-foreground, #9ca3af);
  }
  /* 「运行位置」用两列网格而不是一句带分隔符的长文本 ——
     路径很长而端口很短，挤在一行里换行后必然对不齐，读起来像排版坏了。
     网格的标签列取 max-content，值列吃掉剩余宽度，天然左对齐。 */
  .wb-about-grid {
    display: grid;
    grid-template-columns: max-content minmax(0, 1fr);
    align-items: center;
    column-gap: 14px;
    row-gap: 6px;
    margin-top: 4px;
  }
  /* 窄屏时 `max-content` 的键列会把值列挤得放不下（「数据目录」四个字加路径），
     改成键值上下堆叠，键小字灰色、值换行显示。 */
  @media (max-width: 560px) {
    .wb-about-grid {
      grid-template-columns: minmax(0, 1fr);
      row-gap: 2px;
    }
    .wb-about-k { margin-top: 6px; }
  }
  .wb-about-k {
    font-size: 11.5px;
    line-height: 1.6;
    color: var(--muted-foreground, #64748b);
    white-space: nowrap;
  }
  .wb-about-v {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
    font-size: 12px;
    line-height: 1.6;
    color: var(--foreground, #0f172a);
  }
  .wb-about-v > span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
<script>
(function() {

  /* ------------------- 右上角实时进度指示器 -------------------
     参考 OutlookRegister 的做法：把「当前正在跑什么、到哪一步、进度百分之几」
     常驻在右上角，切到别的页面也能看到。

     ⚠️ 曾经反复闪烁的根因与现在约法：
     旧实现每次轮询（2s 一次）都往 document 里 append 一个新的 pill，
     并且每次非运行态都重开一个 6s 淡出定时器 —— 于是看见的就是「不断重新出现 / 消失」。
     现在的规则：
       1) 节点只在首次创建，后续只改文本与颜色（不再重建 DOM）；
       2) 淡出定时器全局唯一，同一轮只计一次；
       3) 内容未发生变化时连样式计算都不做，彻底避免无谓重绘。 */
  var __wbPillState = { key: "", fadeTimer: null };

  function wbEnsureProgressPill() {
    var existing = document.getElementById("wb-progress-pill");
    if (existing) return existing;
    var el = document.createElement("div");
    el.id = "wb-progress-pill";
    el.style.cssText = [
      "position:fixed", "top:14px", "right:16px", "z-index:99998",
      "display:none", "align-items:center", "gap:8px",
      "padding:6px 12px", "border-radius:999px",
      "background:var(--card,#ffffff)", "color:var(--foreground,#0f172a)",
      "border:1px solid var(--border,rgba(120,120,120,0.28))",
      "box-shadow:0 4px 14px rgba(15,23,42,0.14)",
      "font-size:12px", "line-height:1.5", "max-width:min(320px,60vw)",
      "cursor:pointer"
    ].join(";");
    el.innerHTML =
      '<span id="wb-progress-spin" style="width:10px;height:10px;border-radius:50%;flex:0 0 auto;background:#3b82f6;"></span>' +
      '<span id="wb-progress-text" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">准备中…</span>' +
      '<span id="wb-progress-pct" style="font-variant-numeric:tabular-nums;font-weight:600;flex:0 0 auto;">0%</span>';
    document.body.appendChild(el);
    return el;
  }

  /* 渲染进度胶囊。
     status: running / pending / done / failed / aborted / idle(或 none 表示隐藏)
     同一 (status, progress, stepText) 组合只渲染一次，重复轮询不会产生任何 DOM 写入。 */
  function wbRenderProgressPill(status, progress, stepText) {
    var el = wbEnsureProgressPill();
    if (!el) return;
    // idle / none：隐藏并清掉淡出定时器，不重建节点
    if (status === "idle" || status === "none") {
      if (__wbPillState.fadeTimer) { clearTimeout(__wbPillState.fadeTimer); __wbPillState.fadeTimer = null; }
      if (el.style.display !== "none") el.style.display = "none";
      __wbPillState.key = "";
      return;
    }

    var running = (status === "running" || status === "pending");
    var pct = progress || "0%";
    var text = stepText || "注册进行中";
    var key = status + "|" + pct + "|" + text;

    // 内容无变化：不碰 DOM、不重置定时器（这就是闪烁的根治点）
    if (key === __wbPillState.key && el.style.display === "inline-flex") return;
    __wbPillState.key = key;

    if (el.style.display !== "inline-flex") el.style.display = "inline-flex";
    var pctEl = document.getElementById("wb-progress-pct");
    var txtEl = document.getElementById("wb-progress-text");
    var dotEl = document.getElementById("wb-progress-spin");
    if (pctEl && pctEl.textContent !== pct) pctEl.textContent = pct;
    if (txtEl && txtEl.textContent !== text) txtEl.textContent = text;
    if (dotEl) {
      var color = running ? "#3b82f6" : (status === "done" ? "#10b981" : "#f59e0b");
      if (dotEl.style.background !== color) dotEl.style.background = color;
    }

    // 结束后 8 秒自动淡出；定时器全局唯一，重复渲染不会续期
    if (!running) {
      if (__wbPillState.fadeTimer) return;
      __wbPillState.fadeTimer = setTimeout(function () {
        var node = document.getElementById("wb-progress-pill");
        if (node) node.style.display = "none";
        __wbPillState.fadeTimer = null;
        __wbPillState.key = "";
      }, 8000);
    }
  }

  /* 侧边栏左上角品牌文字实时校准。
     官方产物里旧名有「WorkBuddy Switch」与不含 Switch 的裸「WorkBuddy」两种形态，
     后者与业务文案里的 WorkBuddy（账号池指代）同形，所以只处理 aside 内的品牌节点：
     div.truncate 是侧边栏标题容器，业务文案不会出现在这里。 */
  function sanitizeSidebarBrand() {
    var aside = document.querySelector("aside");
    if (!aside) return;
    var truncates = aside.querySelectorAll("div.truncate");
    truncates.forEach(function(el) {
      if (el.textContent && (el.textContent.indexOf("WorkBuddy") !== -1 || el.textContent.indexOf("Switch") !== -1)) {
        el.textContent = "AutoBuddy";
      }
    });
  }

  /* ------------------- WebUI 用户安全认证与登录模态框 ------------------- */
  var __wbAuthChecked = false;
  var __wbAuthChecking = false;
  var __wbCurrentUser = null;

  function wbCheckAuth() {
    if (__wbAuthChecked || __wbAuthChecking) return;
    __wbAuthChecking = true;
    fetch("/api/auth/status")
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (!data.enabled) {
          __wbAuthChecked = true;
          return;
        }
        if (!data.authenticated) {
          wbShowLoginModal();
        } else {
          __wbAuthChecked = true;
          __wbCurrentUser = data.username;
          __wbAuthData = data;
          wbRenderUserOrb(data);
        }
      })
      .catch(function() {
        __wbAuthChecked = true;
      })
      .finally(function() {
        __wbAuthChecking = false;
      });
  }

  function wbShowLoginModal() {
    if (document.getElementById("wb-login-overlay")) return;
    var overlay = document.createElement("div");
    overlay.id = "wb-login-overlay";
    // 遮罩必须真正挡住后面的内容：此前 0.78 透明度 + 6px 模糊，
    // 在高对比度主题下仍能辨认出底层文字与卡片，登录框看起来像「浮在内容上面」。
    // 这里同时提高底色不透明度与模糊半径，并保留 -webkit- 前缀兼容 Safari。
    overlay.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(15,23,42,0.92);backdrop-filter:blur(18px) saturate(120%);-webkit-backdrop-filter:blur(18px) saturate(120%);z-index:99999;display:flex;align-items:center;justify-content:center;padding:16px;";
    
    var card = document.createElement("div");
    card.style.cssText = "background:var(--card,#ffffff);color:var(--foreground,#0f172a);border-radius:16px;box-shadow:0 20px 40px rgba(0,0,0,0.25);width:100%;max-width:380px;padding:28px 24px;border:1px solid var(--border,rgba(120,120,120,0.2));font-family:inherit;";
    
    card.innerHTML = 
      '<div style="text-align:center;margin-bottom:20px;">' +
        '<img src="/icon.png" style="width:56px;height:56px;border-radius:14px;box-shadow:0 4px 12px rgba(0,0,0,0.12);margin:0 auto 12px;display:block;" />' +
        '<div style="font-size:18px;font-weight:700;letter-spacing:-0.02em;">AutoBuddy</div>' +
        '<div style="font-size:12px;color:var(--muted-foreground,#64748b);margin-top:4px;">&nbsp;</div>' +
      '</div>' +
      '<div style="display:flex;flex-direction:column;gap:12px;">' +
        '<div>' +
          '<label style="font-size:12px;font-weight:500;display:block;margin-bottom:4px;">账号</label>' +
          '<input id="wb-login-user" type="text" autocomplete="off" placeholder="" style="width:100%;padding:8px 12px;border-radius:8px;border:1px solid var(--border,rgba(120,120,120,0.3));background:var(--background,#ffffff);color:inherit;font-size:13px;box-sizing:border-box;" />' +
        '</div>' +
        '<div>' +
          '<label style="font-size:12px;font-weight:500;display:block;margin-bottom:4px;">密码</label>' +
          '<input id="wb-login-pass" type="password" autocomplete="new-password" placeholder="" style="width:100%;padding:8px 12px;border-radius:8px;border:1px solid var(--border,rgba(120,120,120,0.3));background:var(--background,#ffffff);color:inherit;font-size:13px;box-sizing:border-box;" />' +
        '</div>' +
        '<div id="wb-login-slider-wrap" style="margin-top:4px;">' +
          '<div id="wb-login-slider" style="position:relative;height:38px;border-radius:8px;background:var(--muted,rgba(120,120,120,0.12));overflow:hidden;user-select:none;-webkit-user-select:none;touch-action:none;">' +
            '<div id="wb-login-slider-fill" style="position:absolute;left:0;top:0;bottom:0;width:0;background:linear-gradient(90deg,rgba(59,130,246,0.18),rgba(59,130,246,0.32));"></div>' +
            '<div id="wb-login-slider-text" style="position:absolute;left:0;right:0;top:0;bottom:0;display:flex;align-items:center;justify-content:center;font-size:12.5px;color:var(--muted-foreground,#64748b);pointer-events:none;letter-spacing:0.02em;">按住滑块，拖动到最右侧</div>' +
            '<div id="wb-login-slider-handle" style="position:absolute;left:2px;top:2px;width:44px;height:34px;border-radius:6px;background:#ffffff;box-shadow:0 1px 4px rgba(0,0,0,0.18);display:flex;align-items:center;justify-content:center;cursor:grab;transition:background 0.15s;">' +
              '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div id="wb-login-err" style="color:#ef4444;font-size:12px;display:none;margin-top:2px;"></div>' +
        '<button id="wb-login-submit" style="margin-top:6px;width:100%;padding:9px;border-radius:8px;background:#3b82f6;color:#ffffff;font-size:13px;font-weight:600;border:none;cursor:pointer;transition:background 0.2s;">登 录</button>' +
      '</div>';
      
    overlay.appendChild(card);
    document.body.appendChild(overlay);

    var subBtn = document.getElementById("wb-login-submit");
    var userIn = document.getElementById("wb-login-user");
    var passIn = document.getElementById("wb-login-pass");
    var errEl = document.getElementById("wb-login-err");

    var sliderOk = false;
    var sliderBox = document.getElementById("wb-login-slider");
    var sliderHandle = document.getElementById("wb-login-slider-handle");
    var sliderFill = document.getElementById("wb-login-slider-fill");
    var sliderText = document.getElementById("wb-login-slider-text");
    var sliderDragging = false;
    var sliderStartX = 0;

    function sliderMax() {
      return Math.max(0, sliderBox.clientWidth - sliderHandle.offsetWidth - 4);
    }

    function sliderMove(clientX) {
      var max = sliderMax();
      var delta = clientX - sliderStartX;
      var left = Math.min(max, Math.max(0, delta + 2));
      sliderHandle.style.left = left + "px";
      sliderFill.style.width = left + "px";
      if (left >= max - 1) {
        sliderOk = true;
        sliderFill.style.background = "linear-gradient(90deg,rgba(16,185,129,0.22),rgba(16,185,129,0.42))";
        sliderHandle.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>';
        sliderText.textContent = "验证通过";
        sliderText.style.color = "#10b981";
      }
    }

    function sliderReset() {
      sliderOk = false;
      sliderDragging = false;
      sliderHandle.style.left = "2px";
      sliderFill.style.width = "0px";
      sliderFill.style.background = "linear-gradient(90deg,rgba(59,130,246,0.18),rgba(59,130,246,0.32))";
      sliderHandle.style.cursor = "grab";
      sliderHandle.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>';
      sliderText.textContent = "按住滑块，拖动到最右侧";
      sliderText.style.color = "var(--muted-foreground,#64748b)";
    }

    sliderHandle.addEventListener("pointerdown", function(e) {
      if (sliderOk) return;
      sliderDragging = true;
      sliderStartX = e.clientX;
      sliderHandle.style.cursor = "grabbing";
      try { sliderHandle.setPointerCapture(e.pointerId); } catch (err) {}
      e.preventDefault();
    });
    sliderHandle.addEventListener("pointermove", function(e) {
      if (!sliderDragging || sliderOk) return;
      sliderMove(e.clientX);
    });
    sliderHandle.addEventListener("pointerup", function(e) {
      if (!sliderDragging) return;
      sliderDragging = false;
      sliderHandle.style.cursor = "grab";
      try { sliderHandle.releasePointerCapture(e.pointerId); } catch (err) {}
      if (!sliderOk) sliderReset();
    });
    sliderHandle.addEventListener("pointercancel", function() {
      if (!sliderOk) sliderReset();
    });
    window.addEventListener("resize", function() {
      if (document.getElementById("wb-login-overlay")) sliderReset();
    });

    function doLogin() {
      var u = (userIn.value || "").trim();
      var p = (passIn.value || "").trim();
      if (!u || !p) {
        errEl.textContent = "请输入账号和密码";
        errEl.style.display = "block";
        return;
      }
      if (!sliderOk) {
        errEl.textContent = "请先完成滑动验证";
        errEl.style.display = "block";
        return;
      }
      subBtn.disabled = true;
      subBtn.textContent = "登录中…";
      errEl.style.display = "none";

      fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: u, password: p })
      })
      .then(function(r) { return r.json(); })
      .then(function(res) {
        subBtn.disabled = false;
        subBtn.textContent = "登 录";
        if (res.ok) {
          overlay.remove();
          __wbAuthChecked = true;
          __wbCurrentUser = res.username;
          wbToast("登录成功，欢迎使用 AutoBuddy！", "ok");
          window.location.reload();
        } else {
          errEl.textContent = res.error || "账号或密码错误";
          errEl.style.display = "block";
          sliderReset();
        }
      })
      .catch(function() {
        subBtn.disabled = false;
        subBtn.textContent = "登 录";
        errEl.textContent = "登录请求失败，请检查网络";
        errEl.style.display = "block";
        sliderReset();
      });
    }

    subBtn.onclick = doLogin;
    passIn.onkeydown = function(e) { if (e.key === "Enter") doLogin(); };
    userIn.onkeydown = function(e) { if (e.key === "Enter") passIn.focus(); };
    setTimeout(function() { passIn.focus(); }, 100);
  }

  var __wbAuthData = null;

  // 内置圆形头像候选：与官方图标同风格（渐变底 + 简约符号），全部为内联 SVG。
  // 选中的候选由后端存为 "preset:<key>"，/api/auth/avatar 按其返回。
  var WB_AVATAR_PRESETS = {
    moon: { name: "月牙", svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#3b82f6"/><stop offset="1" stop-color="#8b5cf6"/></linearGradient></defs><circle cx="32" cy="32" r="32" fill="url(#g)"/><path d="M42 40a14 14 0 0 1-16-21 16 16 0 1 0 22 15 14 14 0 0 1-6 6z" fill="#fff"/></svg>' },
    star: { name: "星辰", svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0ea5e9"/><stop offset="1" stop-color="#6366f1"/></linearGradient></defs><circle cx="32" cy="32" r="32" fill="url(#g)"/><path d="M32 16l4.6 10.2L48 28l-8.4 7.6L41.8 47 32 41.4 22.2 47l2.2-11.4L16 28l11.4-1.8z" fill="#fff"/></svg>' },
    leaf: { name: "新叶", svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#10b981"/><stop offset="1" stop-color="#14b8a6"/></linearGradient></defs><circle cx="32" cy="32" r="32" fill="url(#g)"/><path d="M44 20c0 14-9 22-22 24 2-13 10-22 22-24z" fill="#fff"/><path d="M22 44c4-9 10-15 18-19" stroke="rgba(255,255,255,0.55)" stroke-width="2" fill="none"/></svg>' },
    sun: { name: "暖阳", svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f59e0b"/><stop offset="1" stop-color="#f97316"/></linearGradient></defs><circle cx="32" cy="32" r="32" fill="url(#g)"/><circle cx="32" cy="32" r="10" fill="#fff"/><g stroke="#fff" stroke-width="3" stroke-linecap="round"><path d="M32 12v6"/><path d="M32 46v6"/><path d="M12 32h6"/><path d="M46 32h6"/><path d="M18 18l4.2 4.2"/><path d="M41.8 41.8L46 46"/><path d="M18 46l4.2-4.2"/><path d="M41.8 22.2L46 18"/></g></svg>' },
    heart: { name: "心动", svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f43f5e"/><stop offset="1" stop-color="#ec4899"/></linearGradient></defs><circle cx="32" cy="32" r="32" fill="url(#g)"/><path d="M32 46s-12-7.4-12-16a7 7 0 0 1 12-4.9A7 7 0 0 1 44 30c0 8.6-12 16-12 16z" fill="#fff"/></svg>' },
    gem: { name: "宝石", svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#64748b"/><stop offset="1" stop-color="#334155"/></linearGradient></defs><circle cx="32" cy="32" r="32" fill="url(#g)"/><path d="M22 22h20l6 8-16 14-16-14z" fill="#fff"/></svg>' }
  };

  function wbSvgDataUri(svg) {
    return "data:image/svg+xml;utf8," + encodeURIComponent(svg);
  }

  function wbAvatarSrc(kind, fresh) {
    // 预设图标：直接内联渲染，不额外请求
    if (kind && kind.indexOf("preset:") === 0) {
      var p = WB_AVATAR_PRESETS[kind.slice(7)];
      return p ? wbSvgDataUri(p.svg) : "/icon.png";
    }
    return (kind === "custom" ? "/api/auth/avatar?ts=" + (fresh ? Date.now() : "1") : "/api/auth/avatar?official=1");
  }

  // 官方 App 图标是方形：圆形硬裁会切坏内容，套底衬完整显示；预设/自定义已是圆形构图，照常 cover
  function wbApplyAvatarImg(img, kind) {
    // 所有头像（官方 / 预设 / 自定义）统一圆形。官方图标由后端 SVG 出口
    // 提供「纯圆底衬 + 居中 logo」，因此前端无需再区分 contain / cover。
    img.className = "wb-avatar-round";
  }

  // 头像选择器（官方 + 内置风格预设）：弹窗与设置卡共用
  // extraBtn：可选。传进来就作为网格内的**最后一个圆钮**参与排布
  // （上传自定义头像就挂在这里），而不是另起一行压在下面。
  function wbBuildAvatarPicker(current, onPick, extraBtn) {
    var wrap = document.createElement("div");
    wrap.className = "wb-avatar-grid wb-avatar-grid-inline";
    var picked = current || "official";
    function setActive(kind) {
      wrap.querySelectorAll(".wb-avatar-opt").forEach(function(x) { x.classList.remove("active"); });
      var hit = wrap.querySelectorAll(".wb-avatar-opt[data-av=\"" + kind + "\"]");
      if (hit.length) hit[0].classList.add("active");
    }
    function mkBtn(kind, label, src) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "wb-avatar-opt" + (picked === kind ? " active" : "");
      b.setAttribute("data-av", kind);
      b.title = label;
      var im = document.createElement("img");
      im.src = src;
      im.alt = label;
      im.className = "wb-avatar-round";
      b.appendChild(im);
      b.onclick = function() { picked = kind; setActive(kind); onPick(kind); };
      wrap.appendChild(b);
    }
    mkBtn("official", "官方图标", wbAvatarSrc("official"));
    Object.keys(WB_AVATAR_PRESETS).forEach(function(k) {
      mkBtn("preset:" + k, WB_AVATAR_PRESETS[k].name, wbSvgDataUri(WB_AVATAR_PRESETS[k].svg));
    });
    if (extraBtn) wrap.appendChild(extraBtn);
    return {
      wrap: wrap,
      get: function() { return picked; },
      markNone: function() { setActive(""); }
    };
  }

  function wbOpenModal(title, buildBody) {
    var overlay = document.createElement("div");
    overlay.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(15,23,42,0.6);z-index:9900;display:flex;align-items:center;justify-content:center;padding:16px;";
    var card = document.createElement("div");
    card.style.cssText = "background:var(--card,#ffffff);color:var(--foreground,#0f172a);border-radius:16px;box-shadow:0 20px 40px rgba(0,0,0,0.25);width:100%;max-width:360px;padding:22px 20px;border:1px solid var(--border,rgba(120,120,120,0.2));position:relative;";
    var h = document.createElement("div");
    h.style.cssText = "font-size:15px;font-weight:700;margin-bottom:14px;";
    h.textContent = title;
    card.appendChild(h);
    var closeX = document.createElement("button");
    closeX.textContent = "×";
    closeX.style.cssText = "position:absolute;top:10px;right:12px;background:transparent;border:none;font-size:20px;line-height:1;color:var(--muted-foreground,#64748b);cursor:pointer;padding:2px 6px;";
    closeX.onclick = function() { overlay.remove(); };
    card.appendChild(closeX);
    buildBody(card, function() { overlay.remove(); });
    overlay.appendChild(card);
    document.body.appendChild(overlay);
    overlay.addEventListener("click", function(e) { if (e.target === overlay) overlay.remove(); });
    return overlay;
  }

  function wbRefreshAuthOrb() {
    fetch("/api/auth/status", { cache: "no-store" })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (d && d.authenticated) { __wbAuthData = d; wbRenderUserOrb(d); }
      })
      .catch(function() {});
  }

  function wbSaveProfileFlow(picked, fileData, fileMime, nick, errEl, done) {
    var finish = function() {
      fetch("/api/auth/profile", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nickname: nick, avatar: picked })
      }).then(function(r) { return r.json(); }).then(function(res) {
        if (res.ok) { done(); }
        else { errEl.textContent = res.error || "保存失败"; errEl.style.display = "block"; }
      }).catch(function() { errEl.textContent = "保存请求失败"; errEl.style.display = "block"; });
    };
    if (picked === "custom" && fileData) {
      var b64 = String(fileData).split(",")[1] || "";
      var bin = atob(b64);
      var bytes = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      fetch("/api/auth/avatar-upload", { method: "POST", body: bytes, headers: { "Content-Type": fileMime } })
        .then(function(r) { return r.json(); })
        .then(function(up) {
          if (!up.ok) { errEl.textContent = up.error || "头像上传失败"; errEl.style.display = "block"; return; }
          finish();
        })
        .catch(function() { errEl.textContent = "头像上传失败"; errEl.style.display = "block"; });
    } else {
      finish();
    }
  }

  function wbRenderProfileSection(host, d) {
    if (!host || document.getElementById("settings-profile")) return;
    var section = wbEl("section", "min-w-0 space-y-2.5");
    section.id = "settings-profile";
    var head = wbEl("div", "px-1");
    var h2 = wbEl("h2", "text-[13px] font-medium leading-5", "账号资料");
    h2.id = "settings-profile-title";
    head.appendChild(h2);
    section.appendChild(head);
    section.setAttribute("aria-labelledby", "settings-profile-title");

    var card = wbEl("div", "wb-api-card");
    var picked = d.avatar || "official";
    var fileData = null, fileMime = null;

    // ---- 1. 头像与昵称 ----
    var row1 = wbEl("div", "wb-api-row wb-api-row-stack");
    var pick = wbEl("div", "wb-avatar-pick");
    var img = document.createElement("img");
    function refreshCardImg(kind) {
      img.src = wbAvatarSrc(kind, kind === "custom");
      wbApplyAvatarImg(img, kind);
    }
    refreshCardImg(picked);
    pick.appendChild(img);
    // 上传按钮与头像圆钮排在同一行（作为网格内最后一枚圆钮），
    // 不再被压到选择器下方单独占一行 —— 用户 2026-09-26 反馈。
    // 圆钮化后与官方/预设按钮视觉等高，一行排满 8 枚刚好。
    var uploadBtn = wbEl("button", "wb-avatar-upload", "+");
    uploadBtn.type = "button";
    uploadBtn.title = "上传自定义头像（≤200KB，PNG / JPG / WebP）";
    var fileIn = document.createElement("input");
    fileIn.type = "file";
    fileIn.accept = "image/png,image/jpeg,image/webp";
    fileIn.style.display = "none";
    uploadBtn.onclick = function() { fileIn.click(); };
    var picker = wbBuildAvatarPicker(picked, function(kind) {
      picked = kind;
      fileData = null;
      refreshCardImg(kind);
    }, uploadBtn);
    fileIn.onchange = function() {
      var f = fileIn.files && fileIn.files[0];
      if (!f) return;
      if (f.size > 200 * 1024) { wbToast("图片超过 200KB，请压缩后再试", "err"); return; }
      var rd = new FileReader();
      rd.onload = function() {
        fileData = rd.result;
        fileMime = f.type || "image/png";
        picked = "custom";
        img.src = String(fileData);
        wbApplyAvatarImg(img, "custom");
        picker.markNone();
      };
      rd.readAsDataURL(f);
    };
    pick.appendChild(picker.wrap);
    pick.appendChild(fileIn);
    row1.appendChild(pick);

    var nickLine = wbEl("div", null);
    nickLine.style.cssText = "display:flex;align-items:center;gap:8px;margin-top:10px;";
    nickLine.appendChild(wbEl("span", "wb-api-label", "昵称"));
    var nickIn = document.createElement("input");
    nickIn.className = "wb-api-input";
    nickIn.maxLength = 32;
    nickIn.value = d.nickname || d.username || "";
    nickLine.appendChild(nickIn);
    var saveBtn = wbEl("button", "wb-api-btn wb-api-btn-primary", "保存资料");
    var errEl = wbEl("span", null);
    errEl.style.cssText = "color:#ef4444;font-size:12px;";
    saveBtn.onclick = function() {
      var nick = (nickIn.value || "").trim();
      if (!nick) { errEl.textContent = "昵称不能为空"; return; }
      saveBtn.disabled = true;
      wbSaveProfileFlow(picked, fileData, fileMime, nick, errEl, function() {
        saveBtn.disabled = false;
        errEl.textContent = "";
        wbToast("资料已保存", "ok");
        wbRefreshAuthOrb();
      });
    };
    nickLine.appendChild(saveBtn);
    nickLine.appendChild(errEl);
    row1.appendChild(nickLine);
    var desc1 = wbEl("div", "wb-api-desc", "昵称显示在右下角；头像默认用官方图标，也可上传自定义图片。");
    row1.appendChild(desc1);
    card.appendChild(row1);

    // ---- 2. 用户名 ----
    var row2 = wbEl("div", "wb-api-row");
    var main2 = wbEl("div", "wb-api-main");
    main2.appendChild(wbEl("div", "wb-api-label", "用户名"));
    main2.appendChild(wbEl("div", "wb-api-desc", d.username || ""));
    row2.appendChild(main2);
    if (d.usernameLocked) {
      var lockBadge = wbEl("span", "wb-api-badge", "环境变量已设，不可修改");
      lockBadge.title = "容器环境变量 AUTH_USERNAME 已设置，请修改环境变量后重建容器";
      row2.appendChild(lockBadge);
    } else {
      var userBtn = wbEl("button", "wb-api-btn", "修改");
      userBtn.onclick = function() { wbOpenUsernameModal(); };
      row2.appendChild(userBtn);
    }
    card.appendChild(row2);

    // ---- 3. 密码 ----
    var row3 = wbEl("div", "wb-api-row");
    var main3 = wbEl("div", "wb-api-main");
    main3.appendChild(wbEl("div", "wb-api-label", "登录密码"));
    // 密码掩码：已设定密码时按实际长度展示同位数 *（不泄露真实字符）
    var maskLen = d.passwordLength || (d.passwordLocked ? 8 : 0);
    if (maskLen > 0) {
      var maskEl = wbEl("div", "wb-pwd-mask");
      maskEl.textContent = new Array(Math.min(maskLen, 40) + 1).join("*");
      maskEl.title = "密码已设定（共 " + maskLen + " 位）";
      main3.appendChild(maskEl);
    }
    row3.appendChild(main3);
    if (d.passwordLocked) {
      var lockBadge2 = wbEl("span", "wb-api-badge", "环境变量已设，不可修改");
      lockBadge2.title = "容器环境变量 AUTH_PASSWORD 已设置，请修改环境变量后重建容器";
      row3.appendChild(lockBadge2);
    } else {
      var pwdBtn = wbEl("button", "wb-api-btn", "修改");
      pwdBtn.onclick = function() { wbOpenPasswordModal(); };
      row3.appendChild(pwdBtn);
    }
    card.appendChild(row3);

    section.appendChild(card);
    host.appendChild(section);
  }

  function injectProfile() {
    var host = wbFindSettingsHost();
    if (!host || document.getElementById("settings-profile")) return;
    function render(d) {
      var host2 = wbFindSettingsHost();
      if (!host2 || document.getElementById("settings-profile")) return;
      wbRenderProfileSection(host2, d);
    }
    if (__wbAuthData && __wbAuthData.username) { render(__wbAuthData); return; }
    fetch("/api/auth/profile", { cache: "no-store" })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (d && d.username) { __wbAuthData = d; render(d); }
      })
      .catch(function() {});
  }

  function wbOpenProfileModal() {
    var d = __wbAuthData || {};
    var picked = d.avatar || "official";
    var fileData = null, fileMime = null;
    wbOpenModal("修改资料", function(card, close) {
      var pick = document.createElement("div");
      pick.style.marginBottom = "14px";
      var img = document.createElement("img");
      img.style.cssText = "width:56px;height:56px;border-radius:50%;object-fit:cover;border:1px solid var(--border,rgba(120,120,120,0.3));display:block;margin:0 auto 10px;";
      function refreshModalImg(kind) {
        img.src = wbAvatarSrc(kind, kind === "custom");
        wbApplyAvatarImg(img, kind);
      }
      refreshModalImg(picked);
      pick.appendChild(img);
      // 与设置页保持一致：上传钮作为网格内最后一枚圆钮，同排展示。
      var uploadBtn = document.createElement("button");
      uploadBtn.type = "button";
      uploadBtn.className = "wb-avatar-upload";
      uploadBtn.textContent = "+";
      uploadBtn.title = "上传自定义头像（≤200KB，PNG / JPG / WebP）";
      var fileIn = document.createElement("input");
      fileIn.type = "file";
      fileIn.accept = "image/png,image/jpeg,image/webp";
      fileIn.style.display = "none";
      uploadBtn.onclick = function() { fileIn.click(); };
      var picker = wbBuildAvatarPicker(picked, function(kind) {
        picked = kind;
        fileData = null;
        refreshModalImg(kind);
      }, uploadBtn);
      fileIn.onchange = function() {
        var f = fileIn.files && fileIn.files[0];
        if (!f) return;
        if (f.size > 200 * 1024) { wbToast("图片超过 200KB，请压缩后再试", "err"); return; }
        var rd = new FileReader();
        rd.onload = function() {
          fileData = rd.result;
          fileMime = f.type || "image/png";
          picked = "custom";
          img.src = String(fileData);
          wbApplyAvatarImg(img, "custom");
          picker.markNone();
        };
        rd.readAsDataURL(f);
      };
      pick.appendChild(picker.wrap);
      pick.appendChild(fileIn);
      card.appendChild(pick);

      var lab = document.createElement("label");
      lab.style.cssText = "font-size:12px;font-weight:500;display:block;margin-bottom:4px;";
      lab.textContent = "昵称";
      card.appendChild(lab);
      var nickIn = document.createElement("input");
      nickIn.className = "wb-modal-input";
      nickIn.maxLength = 32;
      nickIn.value = d.nickname || d.username || "";
      card.appendChild(nickIn);

      var errEl = document.createElement("div");
      errEl.className = "wb-modal-err";
      errEl.style.margin = "8px 0 2px";
      card.appendChild(errEl);

      var save = document.createElement("button");
      save.className = "wb-modal-btn";
      save.style.marginTop = "14px";
      save.textContent = "保存";
      save.onclick = function() {
        var nick = (nickIn.value || "").trim();
        if (!nick) { errEl.textContent = "昵称不能为空"; errEl.style.display = "block"; return; }
        save.disabled = true;
        var finish = function() {
          fetch("/api/auth/profile", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ nickname: nick, avatar: picked })
          }).then(function(r) { return r.json(); }).then(function(res) {
            if (res.ok) {
              wbToast("资料已保存", "ok");
              close();
              wbRefreshAuthOrb();
            } else {
              save.disabled = false;
              errEl.textContent = res.error || "保存失败";
              errEl.style.display = "block";
            }
          }).catch(function() {
            save.disabled = false;
            errEl.textContent = "保存请求失败";
            errEl.style.display = "block";
          });
        };
        if (picked === "custom" && fileData) {
          // 先传原始字节（不走 multipart），服务端校验类型与大小
          var b64 = String(fileData).split(",")[1] || "";
          var bin = atob(b64);
          var bytes = new Uint8Array(bin.length);
          for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
          fetch("/api/auth/avatar-upload", { method: "POST", body: bytes, headers: { "Content-Type": fileMime } })
            .then(function(r) { return r.json(); })
            .then(function(up) {
              if (!up.ok) { save.disabled = false; errEl.textContent = up.error || "头像上传失败"; errEl.style.display = "block"; return; }
              finish();
            })
            .catch(function() { save.disabled = false; errEl.textContent = "头像上传失败"; errEl.style.display = "block"; });
        } else {
          finish();
        }
      };
      card.appendChild(save);
    });
  }

  function wbOpenUsernameModal() {
    wbOpenModal("修改用户名", function(card, close) {
      var mk = function(label, type) {
        var l = document.createElement("label");
        l.style.cssText = "font-size:12px;font-weight:500;display:block;margin:10px 0 4px;";
        l.textContent = label;
        card.appendChild(l);
        var inp = document.createElement("input");
        inp.type = type;
        inp.className = "wb-modal-input";
        inp.maxLength = 32;
        card.appendChild(inp);
        return inp;
      };
      var pwdIn = mk("当前密码", "password");
      var nameIn = mk("新用户名", "text");
      var errEl = document.createElement("div");
      errEl.className = "wb-modal-err";
      errEl.style.margin = "8px 0 2px";
      card.appendChild(errEl);
      var save = document.createElement("button");
      save.className = "wb-modal-btn";
      save.style.marginTop = "14px";
      save.textContent = "保存并重新登录";
      save.onclick = function() {
        save.disabled = true;
        fetch("/api/auth/username", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ current_password: pwdIn.value, new_username: (nameIn.value || "").trim() })
        }).then(function(r) { return r.json(); }).then(function(res) {
          if (res.ok) { wbToast("用户名已更新，请重新登录", "ok"); setTimeout(function() { window.location.reload(); }, 800); }
          else { save.disabled = false; errEl.textContent = res.error || "修改失败"; errEl.style.display = "block"; }
        }).catch(function() { save.disabled = false; errEl.textContent = "请求失败"; errEl.style.display = "block"; });
      };
      card.appendChild(save);
    });
  }

  function wbOpenPasswordModal() {
    wbOpenModal("修改密码", function(card, close) {
      var mk = function(label) {
        var l = document.createElement("label");
        l.style.cssText = "font-size:12px;font-weight:500;display:block;margin:10px 0 4px;";
        l.textContent = label;
        card.appendChild(l);
        var inp = document.createElement("input");
        inp.type = "password";
        inp.className = "wb-modal-input";
        card.appendChild(inp);
        return inp;
      };
      var oldIn = mk("当前密码");
      var newIn = mk("新密码（至少 6 位）");
      var newIn2 = mk("确认新密码");
      var errEl = document.createElement("div");
      errEl.className = "wb-modal-err";
      errEl.style.margin = "8px 0 2px";
      card.appendChild(errEl);
      var save = document.createElement("button");
      save.className = "wb-modal-btn";
      save.style.marginTop = "14px";
      save.textContent = "保存并重新登录";
      save.onclick = function() {
        if (newIn.value !== newIn2.value) { errEl.textContent = "两次输入的新密码不一致"; errEl.style.display = "block"; return; }
        save.disabled = true;
        fetch("/api/auth/password", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ old_password: oldIn.value, new_password: newIn.value })
        }).then(function(r) { return r.json(); }).then(function(res) {
          if (res.ok) { wbToast("密码已更新，请重新登录", "ok"); setTimeout(function() { window.location.reload(); }, 800); }
          else { save.disabled = false; errEl.textContent = res.error || "修改失败"; errEl.style.display = "block"; }
        }).catch(function() { save.disabled = false; errEl.textContent = "请求失败"; errEl.style.display = "block"; });
      };
      card.appendChild(save);
    });
  }

  function wbRenderUserOrb(data) {
    if (!data || !data.authenticated) return;
    var nick = data.nickname || data.username || "";
    var orb = document.getElementById("wb-user-orb");
    if (orb) {
      var oimg = orb.querySelector("img");
      var onick = orb.querySelector(".wb-orb-nick");
      if (oimg) {
        oimg.src = wbAvatarSrc(data.avatar, true);
        wbApplyAvatarImg(oimg, data.avatar);
      }
      if (onick) onick.textContent = nick;
      return;
    }
    orb = document.createElement("div");
    orb.id = "wb-user-orb";
    orb.title = "账号：" + (data.username || "") + "（点击查看）";
    var img = document.createElement("img");
    img.src = wbAvatarSrc(data.avatar, data.avatar === "custom");
    img.alt = "用户头像";
    wbApplyAvatarImg(img, data.avatar);
    orb.appendChild(img);
    var nickEl = document.createElement("span");
    nickEl.className = "wb-orb-nick";
    nickEl.textContent = nick;
    orb.appendChild(nickEl);
    document.body.appendChild(orb);

    var pop = null;
    function closePop() { if (pop) { pop.remove(); pop = null; orb.classList.remove("wb-orb-open"); document.removeEventListener("click", onDoc); } }
    function onDoc() { closePop(); }
    function openPop() {
      if (pop) { closePop(); return; }
      pop = document.createElement("div");
      pop.id = "wb-user-pop";
      var head = document.createElement("div");
      head.className = "wb-pop-head";
      var himg = document.createElement("img");
      himg.src = wbAvatarSrc(data.avatar, true);
      himg.alt = "用户头像";
      wbApplyAvatarImg(himg, data.avatar);
      head.appendChild(himg);
      var hmain = document.createElement("div");
      hmain.style.cssText = "min-width:0;";
      var nrow = document.createElement("div");
      nrow.style.cssText = "font-weight:700;font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
      nrow.textContent = nick;
      hmain.appendChild(nrow);
      if (data.username && data.username !== nick) {
        var urow = document.createElement("div");
        urow.style.cssText = "font-size:11px;color:var(--muted-foreground,#64748b);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
        urow.textContent = "@" + data.username;
        hmain.appendChild(urow);
      }
      var srow = document.createElement("div");
      srow.style.cssText = "display:flex;align-items:center;gap:5px;font-size:11px;color:#10b981;margin-top:2px;";
      var dot = document.createElement("span");
      dot.style.cssText = "width:6px;height:6px;border-radius:50%;background:#10b981;display:inline-block;";
      srow.appendChild(dot);
      srow.appendChild(document.createTextNode("在线"));
      hmain.appendChild(srow);
      head.appendChild(hmain);
      pop.appendChild(head);

      // 有意义的信息行：剩余积分 / 今日消耗 / 账号数量。
      // 有效期倒计时已移除——过期了重新登录即可，倒计时只会制造焦虑。
      var stats = document.createElement("div");
      stats.className = "wb-pop-stats";
      function statRow(k, v) {
        var row = document.createElement("div");
        row.className = "wb-pop-stat";
        var kk = document.createElement("span");
        kk.className = "wb-pop-stat-k";
        kk.textContent = k;
        var vv = document.createElement("span");
        vv.className = "wb-pop-stat-v";
        vv.textContent = v;
        row.appendChild(kk);
        row.appendChild(vv);
        return row;
      }
      var creditRow = statRow("剩余积分", "…");
      var todayRow = statRow("今日消耗", "…");
      var acctRow = statRow("账号池", "…");
      stats.appendChild(creditRow);
      stats.appendChild(todayRow);
      stats.appendChild(acctRow);
      pop.appendChild(stats);

      // 积分数据：官方 credits/stats（代理层已过滤僵尸账号并校准汇总）
      fetch("/api/credits/stats", { cache: "no-store" })
        .then(function(r) { return r.ok ? r.json() : {}; })
        .then(function(d) {
          var sum = (d && d.summary) || {};
          creditRow.querySelector(".wb-pop-stat-v").textContent =
            sum.currentRemaining != null ? (Math.round(sum.currentRemaining * 100) / 100 + "") : "—";
          todayRow.querySelector(".wb-pop-stat-v").textContent =
            sum.usageToday != null ? (Math.round(sum.usageToday * 100) / 100 + "") : "—";
        })
        .catch(function() {
          creditRow.querySelector(".wb-pop-stat-v").textContent = "—";
          todayRow.querySelector(".wb-pop-stat-v").textContent = "—";
        });
      // 账号池：网关信息里的账号统计
      fetch("/api/gateway-info", { cache: "no-store" })
        .then(function(r) { return r.ok ? r.json() : {}; })
        .then(function(d) {
          var acc = (d && d.accounts) || {};
          acctRow.querySelector(".wb-pop-stat-v").textContent =
            acc.total != null ? ((acc.usable != null ? acc.usable + "/" : "") + acc.total + " 个") : "—";
        })
        .catch(function() {
          acctRow.querySelector(".wb-pop-stat-v").textContent = "—";
        });

      // 资料与凭据的修改入口统一收在设置页「账号资料」，浮窗只留退出
      var menu = document.createElement("div");
      menu.className = "wb-pop-menu";
      var out = document.createElement("button");
      out.className = "wb-pop-item danger";
      out.textContent = "退出登录";
      out.onclick = function() {
        closePop();
        fetch("/api/auth/logout", { method: "POST" }).then(function() { window.location.reload(); });
      };
      menu.appendChild(out);
      pop.appendChild(menu);
      document.body.appendChild(pop);
      orb.classList.add("wb-orb-open");
      setTimeout(function() { document.addEventListener("click", onDoc); }, 0);
    }
    orb.addEventListener("click", function(e) {
      e.stopPropagation();
      if (pop) { closePop(); } else { openPop(); }
    });
    // 鼠标悬浮即自动展开（含浮窗），移开后由全局点击/悬浮判定收纳
    orb.addEventListener("mouseenter", function() {
      if (!pop) openPop();
    });
    orb.addEventListener("mouseleave", function() {
      // 延迟收纳：给鼠标移向浮窗留出路径，避免中途闪烁
      clearTimeout(orb.__wbLeaveTimer);
      orb.__wbLeaveTimer = setTimeout(function() {
        if (pop && !pop.matches(":hover")) closePop();
      }, 220);
    });
    document.addEventListener("mousemove", function(ev) {
      if (!pop) return;
      var r = orb.getBoundingClientRect(), pr = pop.getBoundingClientRect();
      var inside = (ev.clientX >= r.left && ev.clientX <= r.right && ev.clientY >= r.top && ev.clientY <= r.bottom)
                || (ev.clientX >= pr.left && ev.clientX <= pr.right && ev.clientY >= pr.top && ev.clientY <= pr.bottom);
      if (!inside) closePop();
    });
  }


  // 账号卡片上的 WorkBuddy / CodeBuddy IDE / CodeBuddy CLI 切换按钮与「当前账号」徽章，
  // 以及「导入本机账号」「权限检测」「启动设置」「自动更新」等桌面专有入口，
  // 都已在 patch/patch_binary.py 里从二进制物理移除（它们只会调用宿主桌面程序）。
  // 这里只做兜底：清掉少数没有稳定结构锚点、只能按文案识别的残留入口。
  function sanitizeMacUI() {
    document.querySelectorAll("button, a, [role='status']").forEach(el => {
      const text = (el.innerText || "").trim();
      // 清除无法在容器内执行的动作：Finder、完全磁盘访问
      if (text === "在 Finder 中显示" || text === "在文件管理器中显示" || text === "打开完全磁盘访问" || text === "打开 App 管理") {
        el.classList.add("wb-mac-btn-hide");
        return;
      }
    });

    // 仅精准清理包含「如何授权」或「完全磁盘访问」的引导小卡片，绝不向上寻找普通大容器
    document.querySelectorAll("div.border-l-2, div.rounded-md.border").forEach(box => {
      const text = box.innerText || "";
      if (text.includes("完全磁盘访问") || text.includes("如何授权") || text.includes("workbuddy-switch.app") || text.includes("autobuddy")) {
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
      var label = "";
      Array.from(a.childNodes).forEach(node => {
        if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
          const span = document.createElement("span");
          span.className = "wb-nav-label";
          span.innerText = node.textContent;
          label = node.textContent.trim();
          node.replaceWith(span);
        }
      });
      // 收拢态只留图标：名称挪到 data 属性，悬停时用 CSS 气泡提示
      if (label) a.setAttribute("data-wb-label", label);
    });

    // 窄屏（手机）上侧边栏不再是「可以收起」，而是**必须**收起：
    // 它固定占 220px，在 430px 的屏幕上会吃掉一半，正文被挤成一条竖线。
    // 这里不写进 localStorage —— 那是桌面端的手动偏好，不该被媒体查询改掉。
    const narrowQuery = window.matchMedia("(max-width: 720px)");
    const applyCollapsed = (on) => {
      aside.classList.toggle("wb-collapsed", !!on);
      const svg = btn.querySelector("svg");
      if (svg) {
        svg.innerHTML = on
          ? `<polyline points="9 18 15 12 9 6"></polyline>`
          : `<polyline points="15 18 9 12 15 6"></polyline>`;
      }
    };

    let collapsed = localStorage.getItem("wb_sidebar_collapsed") === "true";
    applyCollapsed(collapsed || narrowQuery.matches);

    narrowQuery.addEventListener("change", (e) => {
      applyCollapsed(e.matches || collapsed);
    });

    btn.onclick = (e) => {
      e.stopPropagation();
      collapsed = !collapsed;
      localStorage.setItem("wb_sidebar_collapsed", collapsed);
      applyCollapsed(collapsed);
    };
    aside.appendChild(btn);
  }

  function enforceTitle() {
    // 浏览器标签标题精简为固定短名，官方标题过长会被标签栏截断
    if (document.title !== "AutoBuddy") {
      document.title = "AutoBuddy";
    }
  }

  var wbModelsCache = null;

  // 本地点按后需要立刻重绘，但重新拉一次 /api/account-models 会有可见延迟。
  // 这里在内存里维护一份「账号 -> {模型: 来源}」，点一下就地改、就地重绘，
  // 网络请求只负责落盘，不参与渲染，点按手感才是即时的。
  // 存来源而不只存布尔值，是为了区分手动禁用（我关的）与巡检禁用（它关的）。
  var wbModelPolicy = {};

  function wbPolicySet(accountId) {
    if (!wbModelPolicy[accountId]) wbModelPolicy[accountId] = {};
    return wbModelPolicy[accountId];
  }

  // "manual" | "auto" | undefined
  function wbModelSource(accountId, model) {
    var set = wbModelPolicy[accountId];
    return set ? set[model] : undefined;
  }

  function wbIsModelDisabled(accountId, model) {
    return !!wbModelSource(accountId, model);
  }

  function wbToggleModel(accountId, model, done) {
    var next = !wbIsModelDisabled(accountId, model);
    var set = wbPolicySet(accountId);
    // 人点出来的禁用一律算 manual —— 这是它后续不被巡检自动放开的依据。
    if (next) { set[model] = "manual"; } else { delete set[model]; }

    fetch("/api/account-models", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accountId: accountId, model: model, disabled: next })
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          return { ok: r.ok, body: body || {} };
        });
      })
      .then(function (res) {
        if (!res.ok || res.body.ok === false) {
          // 落盘失败就把内存里的乐观改动回滚，否则界面会显示一个并未生效的状态。
          if (next) { delete wbPolicySet(accountId)[model]; }
          else { wbPolicySet(accountId)[model] = "manual"; }
          var d = res.body.detail;
          wbToast(typeof d === "string" && d ? d : "操作失败，请重试", true);
          if (done) done(false);
          return;
        }
        wbModelPolicy[accountId] = {};
        wbApplySources(accountId, res.body.disabledSources, res.body.disabledModels);
        wbToast(next ? ("已禁用 " + model + " · 不再对该账号轮询") : ("已恢复 " + model));
        if (done) done(true);
      })
      .catch(function () {
        if (next) { delete wbPolicySet(accountId)[model]; }
        else { wbPolicySet(accountId)[model] = "manual"; }
        wbToast("请求失败，请刷新页面后重试", true);
        if (done) done(false);
      });
  }

  // 渲染「一致性自检」的结果：把每个账号的凭据结论与模型结论并排摆出来。
  //
  // 这块存在的全部理由是**消除口径矛盾**：凭据与模型是两个独立维度，
  // 过去界面上只有一个笼统状态，于是出现过「检测说凭据有效、巡检说凭据失效」
  // 这种让人无从判断的局面。这里把两侧各自的结论与判据一起列出，
  // 让「凭据好但被上游拦截」「某模型无权限」一眼就能分开。
  function wbRenderAuditBox(card, data) {
    var old = card.querySelector(".wb-audit-box");
    if (old && old.parentNode) old.parentNode.removeChild(old);

    var box = wbEl("div", "wb-api-row wb-api-row-stack wb-audit-box");
    var main = wbEl("div", "wb-api-main");
    main.appendChild(wbEl("div", "wb-api-label",
      "一致性自检 · " + (data.accounts || 0) + " 个账号"));

    var rows = data.rows || [];
    var needAttention = rows.filter(function (r) {
      return r.consistency && r.consistency.indexOf("一致：均正常") !== 0;
    });
    main.appendChild(wbEl("div", "wb-api-desc",
      needAttention.length
        ? "需要关注的账号 " + needAttention.length + " 个（下面标红/标黄的行）："
        : "全部账号两侧结论一致，无需处理。"));

    rows.forEach(function (r) {
      var line = wbEl("div", "wb-audit-row");
      // 状态着色：失效=红、受限=琥珀、正常=默认。
      var st = (r.credential || {}).state;
      var cls = "wb-audit-state";
      if (st === "invalid") cls += " wb-audit-bad";
      else if (st === "restricted") cls += " wb-audit-warn";
      else if (st === "valid") cls += " wb-audit-ok";
      line.appendChild(wbEl("span", cls, (r.credential || {}).label || "未检测"));
      line.appendChild(wbEl("span", "wb-audit-name", r.name || r.id));
      line.appendChild(wbEl("span", "wb-audit-consistency", r.consistency || ""));
      // 判定依据必须印出来：「凭据有效」与「账号受限」的差别全在这一句里
      // （前者是上游以模型不存在应答，后者是请求被内容审查拦下）。
      // 只给一个标签，用户还是分不清该重登还是该找上游。
      var detail = (r.credential || {}).detail || "";
      if (detail) {
        line.appendChild(wbEl("span", "wb-audit-detail", detail));
      }
      // 判据与建议：这是这块最该被读到的部分 —— 它解释了「为什么是这个结论」。
      line.appendChild(wbEl("span", "wb-audit-advice", r.advice || ""));
      main.appendChild(line);
    });

    // 把判据本身也印出来，让「统一标准」是可查的而不是口头承诺。
    var crit = data.criteria || {};
    if (crit.credential) {
      main.appendChild(wbEl("div", "wb-api-desc wb-audit-criteria",
        "判据 · 凭据：" + crit.credential));
    }
    if (crit.model) {
      main.appendChild(wbEl("div", "wb-api-desc wb-audit-criteria",
        "判据 · 模型：" + crit.model));
    }
    if (crit.note) {
      main.appendChild(wbEl("div", "wb-api-desc wb-audit-criteria", "判据 · " + crit.note));
    }
    box.appendChild(main);
    card.appendChild(box);
  }

  // 账号级停用（与模型级是两回事：这条管「整个账号参不参与轮询」）。
  // 与 wbToggleModel 同一套写法：先乐观改本地状态让点按即时，再落盘，
  // 失败就把乐观改动回滚 —— 否则界面会显示一个并未生效的状态。
  function wbToggleAccount(accountId, disabled, done) {
    fetch("/api/account-pool/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accountId: accountId, disabled: !!disabled })
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          return { ok: r.ok, body: body || {} };
        });
      })
      .then(function (res) {
        if (!res.ok || res.body.ok === false) {
          var d = res.body.detail;
          wbToast(typeof d === "string" && d ? d : "操作失败，请重试", true);
          if (done) done(false);
          return;
        }
        wbToast(disabled ? "已停用该账号 · 不再参与轮询" : "已恢复该账号参与轮询");
        if (done) done(true);
      })
      .catch(function () {
        wbToast("请求失败，请刷新页面后重试", true);
        if (done) done(false);
      });
  }

  // 后端下发的来源表是权威值，覆盖本地乐观副本。
  // 兼容只有 disabledModels（无来源表）的旧响应：那些一律当手动禁用。
  function wbApplySources(accountId, sources, models) {
    wbModelPolicy[accountId] = {};
    var set = wbModelPolicy[accountId];
    if (sources && typeof sources === "object") {
      Object.keys(sources).forEach(function (m) {
        set[m] = sources[m] === "auto" ? "auto" : "manual";
      });
      return;
    }
    (models || []).forEach(function (m) { set[m] = "manual"; });
  }

  function injectAccountModels() {
    // 在每个账号卡片下方动态插入「已使用模型」标签。
    // 数据来自官方 usage 记录（按 accountId + model 聚合），不硬编码模型清单。
    //
    // 每个标签可点击：点一下把该模型对该账号禁用（不参与轮询），再点恢复。
    // 「可用 / 已用 / 已禁用」三态互斥，已禁用优先于已用 —— 用户主动关闭是更强的意图。
    var cards = Array.prototype.slice.call(document.querySelectorAll("article"));
    if (!cards.length) return;

    var pending = cards.filter(function (c) {
      return !c.querySelector(".wb-am-box") && c.querySelector("h3");
    });
    if (!pending.length) return;

    function render(data) {
      // 建立多重索引：卡片标题是账号昵称，而新账号常常没有昵称
      // （官方流水里 accountName 为空，只能回退成账号 ID）。
      // 只按昵称建索引的话，新账号卡片标题对不上任何键 → 落进 fallback →
      // 丢掉 accountId → 点击禁用 / 全部恢复这些按钮全数不渲染。
      // 所以三个键都注：name、aliases（后端下发）、id。
      var byName = {};
      var accs = (data && data.accounts) || {};
      Object.keys(accs).forEach(function (id) {
        var a = accs[id];
        if (!a) return;
        [a.name, a.id].concat(a.aliases || []).forEach(function (key) {
          if (key) byName[String(key)] = a;
        });
      });
      // 账号没有任何调用记录时（例如刚添加）也要展示网关可路由的完整清单，
      // 并带上 id —— 没有 id 就等于没有交互按钮。
      var fallback = { models: (data && data.catalog) || [], used: [] };
      if (!fallback.models.length) return;

      // 账号池顺序（后端下发），供标题对不上时按位置兜底取 id。
      var pendingIds = Object.keys(accs).map(function (k) { return accs[k] && accs[k].id ? accs[k].id : k; });

      pending.forEach(function (card, cardIndex) {
        if (card.querySelector(".wb-am-box")) return;
        var h3 = card.querySelector("h3");
        if (!h3) return;

        var entry = byName[(h3.textContent || "").trim()] || fallback;
        var models = (entry.models && entry.models.length) ? entry.models : fallback.models;
        var used = {};
        (entry.used || []).forEach(function (m) { used[m] = true; });
        if (!models.length) return;

        // 该卡片的账号 id：取自后端，避免用昵称反查导致的错配。
        // 标题查不到时（新账号/昵称被格式化过）按账号池顺序兜底，
        // 不轻易置 null —— accountId 为空会让整张卡片变成只读展示。
        var accountId = entry.id || null;
        if (!accountId && cardIndex < pendingIds.length) {
          accountId = pendingIds[cardIndex] || null;
        }

        var box = document.createElement("div");
        box.className = "wb-am-box";

        // 标题**不带**任何「已调用 N」计数。
        // 模型维度的「已调用 N」与账号池条的「已调用 N 次」用同一个词、口径却不同
        // （一个是「用过几个模型」，一个是「账号被分到几次请求」），并排出现极易混淆。
        // 模型是否被调用过，由标签高亮（wb-am-tag-used）表达即可 —— 用户 2026-09-19 反馈。
        var title = document.createElement("div");
        title.className = "wb-am-title";

        var titleText = document.createElement("span");
        titleText.textContent = "可用模型 · " + models.length;
        title.appendChild(titleText);

        // 「已禁用」只数**人点的**，「巡检」只数**巡检写的** —— 两个数互不重叠。
        // 曾经写成「已禁用 = 全部禁用（含巡检）」再单列一个「巡检 N」，
        // 同一批模型被数了两遍：巡检禁掉一个模型，用户会看到「已禁用」也跟着 +1，
        // 根本分不清哪个是自己点的、哪个是巡检写的。
        var manualCount = 0;
        var autoCount = 0;
        models.forEach(function (m) {
          if (!accountId) return;
          var src = wbModelSource(accountId, m);
          if (!src) return;
          if (src === "auto") autoCount += 1; else manualCount += 1;
        });
        if (manualCount) {
          var badge = document.createElement("span");
          badge.className = "wb-am-offcount";
          badge.textContent = "已禁用 " + manualCount;
          badge.title = "你手动禁用的模型 " + manualCount
            + " 个 —— 巡检不会自动放开，只有你点它才恢复";
          title.appendChild(badge);
        }
        if (autoCount) {
          // 巡检关的单独标出：它与手动禁用一样「不参与调用」，但会自行解除，
          // 用户需要能分辨，否则会以为是自己误操作。
          var autoBadge = document.createElement("span");
          autoBadge.className = "wb-am-offcount wb-am-offcount-auto";
          autoBadge.textContent = "巡检 " + autoCount;
          autoBadge.title = "可用性巡检自动禁用的模型 " + autoCount
            + " 个（探测到调用不通），模型恢复后会自动放开";
          title.appendChild(autoBadge);
        }

        // 「全部恢复」：一次把这张卡片下所有被禁用的模型放回轮询。
        //
        // 只在**确实有禁用项**时出现，否则按钮点了也不知道在恢复什么。
        // 覆盖手动与巡检两种来源 —— 所以文案不写「恢复巡检禁用」，
        // 那是另一件事（scope=auto 只在需要保留人工决策时用）。
        // 巡检误禁一批、或手动调完想一次性放开时，逐个点标签太碎。
        if (accountId && (manualCount + autoCount) > 0) {
          var restore = document.createElement("button");
          restore.className = "wb-am-restore";
          restore.textContent = "全部恢复 " + (manualCount + autoCount);
          restore.title = "把该账号被禁用的 " + (manualCount + autoCount)
            + " 个模型全部放回轮询（含手动禁用）";
          restore.onclick = function () {
            if (restore.dataset.wbBusy === "1") return;
            restore.dataset.wbBusy = "1";
            restore.disabled = true;
            var before = restore.textContent;
            restore.textContent = "恢复中…";
            fetch("/api/account-models/restore", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ accountId: accountId, scope: "all" })
            })
              .then(function (r) {
                return r.json().catch(function () { return {}; })
                  .then(function (b) { return { ok: r.ok, body: b || {} }; });
              })
              .then(function (res) {
                if (!res.ok || res.body.ok === false) {
                  var d = res.body.detail;
                  wbToast(typeof d === "string" && d ? d : "恢复失败，请重试", "error");
                  restore.textContent = before;
                  restore.disabled = false;
                  delete restore.dataset.wbBusy;
                  return;
                }
                // 策略变了：清掉缓存 + 直接重绘模型区，反馈即时。
                wbModelPolicy[accountId] = {};
                wbModelsCache = null;
                wbToast("已恢复 " + (res.body.restoredCount || 0) + " 个模型");
                box.remove();
                injectAccountModels();
              })
              .catch(function () {
                wbToast("请求失败，请刷新页面后重试", "error");
                restore.textContent = before;
                restore.disabled = false;
                delete restore.dataset.wbBusy;
              });
          };
          title.appendChild(restore);
        }

        var usage = entry.usage || {};
        var list = document.createElement("div");
        list.className = "wb-am-list";
        models.forEach(function (m) {
          var src = accountId ? wbModelSource(accountId, m) : undefined;
          var off = !!src;
          var tag = document.createElement("span");
          // 已禁用优先：禁用态压掉「已用」高亮，否则用户看不到自己刚点掉的那个。
          var stateClass = src === "auto" ? "wb-am-tag-off wb-am-tag-auto"
            : (off ? "wb-am-tag-off" : (used[m] ? "wb-am-tag-used" : ""));
          tag.className = ("wb-am-tag " + stateClass).replace(/\s+$/, "")
            + (accountId ? " wb-am-tag-click" : "");
          tag.textContent = m;
          var stat = usage[m];
          var tip = [];
          if (src === "auto") {
            tip.push("巡检发现调用不通，已自动禁用");
            tip.push("模型恢复后会自动启用");
          } else if (off) {
            tip.push("已手动禁用：该账号不再被分配到 " + m);
            tip.push("巡检不会自动放开手动禁用的模型");
          }
          if (stat) {
            // 只留「用量」类信息（Token / 积分），**不再显示调用次数**。
            // 调用次数是账号维度的指标，只保留在账号池条上的「已调用 N 次」一处。
            if (stat.tokens) tip.push("约 " + stat.tokens + " Token");
            if (stat.credit != null) tip.push("积分 " + Math.round(stat.credit * 100) / 100);
          } else if (used[m] && !off) {
            tip.push("该账号用过此模型");
          }
          if (accountId) {
            tip.push(off ? "点击恢复" : "点击禁用（不再对该账号轮询）");
            tag.onclick = function () {
              if (tag.dataset.wbBusy === "1") return;
              tag.dataset.wbBusy = "1";
              wbToggleModel(accountId, m, function () {
                // 就地重绘：只清掉本卡的模型区，用缓存重画（不再打一次网络请求），
                // 点按反馈才是即时的。缓存里的 updated 状态由 wbToggleModel 维护。
                box.remove();
                injectAccountModels();
              });
            };
            tag.setAttribute("role", "button");
            tag.setAttribute("tabindex", "0");
            tag.onkeydown = function (ev) {
              if (ev.key === "Enter" || ev.key === " ") {
                ev.preventDefault();
                tag.onclick();
              }
            };
          }
          if (tip.length) tag.title = tip.join(" · ");
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
      .then(function (data) {
        wbModelsCache = data;
        // 后端下发的策略是权威值，覆盖本地乐观副本，保证刷新后状态一致。
        var accs = (data && data.accounts) || {};
        Object.keys(accs).forEach(function (id) {
          if (accs[id]) wbApplySources(id, accs[id].disabledSources, accs[id].disabled);
        });
        render(data);
      })
      .catch(function () {});
  }

  var wbPoolCache = null;

  // 巡检改过禁用策略后，卡片上的模型标签需要整体重画。
  // injectAccountModels() 只处理「还没有标签区」的卡片，所以必须先移除旧的，
  // 否则界面会停留在巡检之前的状态。
  function refreshModelTags() {
    Array.prototype.slice.call(document.querySelectorAll(".wb-am-box"))
      .forEach(function (el) { el.remove(); });
    wbModelsCache = null;
    injectAccountModels();
  }

  function effectiveEnabledIds(data) {
    if (data.allEnabledByDefault) {
      return (data.accounts || []).map(function (a) { return a.id; });
    }
    return (data.enabledAccountIds || []).slice();
  }

  function savePool(payload, done) {
    // 这里必须把**失败**也告知用户。此前的写法是 r.json() 后无条件走 done
    // 回调，于是后端 400（例如 enabledAccountIds 里含已删除账号）时：
    //   · 没有 toast —— 用户完全看不到出错；
    //   · done 照常执行 —— refreshPool() 用旧缓存重绘，页面毫无变化。
    // 合起来就是「点了没任何反应」。现在把 HTTP 状态与业务 detail 都转发出去。
    fetch("/api/account-pool", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          return { ok: r.ok, status_code: r.status, body: body || {} };
        });
      })
      .then(function (res) {
        if (!res.ok) {
          var d = res.body.detail;
          var msg;
          if (d && typeof d === "object" && d.unknownAccountIds) {
            // 幽灵账号：白名单里指向了已被删除的账号。后端现已自动剔除，
            // 正常不会再走到这里；保留分支以免旧版本网关下静默失真。
            msg = "设置未生效：启用列表里有 " + d.unknownAccountIds.length + " 个已不存在的账号";
          } else if (typeof d === "string" && d) {
            msg = d;
          } else {
            msg = "设置未生效（HTTP " + res.status_code + "）";
          }
          wbToast(msg, "err");
          if (done) done(null);
          return;
        }
        if (res.body && res.body.status) { wbPoolCache = res.body.status; }
        if (done) done(res.body);
      })
      .catch(function () {
        wbToast("请求失败，请刷新页面后重试", "err");
        if (done) done(null);
      });
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
      // 多重索引：卡片标题是账号昵称，而新账号常常没有昵称，
      // 只能回退成账号 ID（如 a75e21b0-3ec4-...）。
      // 只按昵称建索引的话，这些卡片在账号池控制条上完全空 —— 启用/停用、
      // 设为首选、检测账号、调用次数四个控件都不出现，看上去像「功能缺失」。
      // 所以 name / id / 昵称与邮箱都要注。
      var byName = {};
      data.accounts.forEach(function (a) {
        if (!a) return;
        [a.name, a.id, a.nickname, a.email].forEach(function (key) {
          if (key) byName[String(key)] = a;
        });
      });

      // 各账号被分摊到的请求次数（网关进程启动后累计）。
      var counts = {};
      (data.selectionCounts || []).forEach(function (c) { counts[c.accountId] = c.count; });

      var pendingIds = data.accounts.map(function (a) { return a && a.id; }).filter(Boolean);

      pending.forEach(function (card, cardIndex) {
        if (card.querySelector(".wb-pool-bar")) return;
        var h3 = card.querySelector("h3");
        if (!h3) return;
        var cardTitle = (h3.textContent || "").trim();
        var acc = byName[cardTitle];
        // 标题对不上时（账号无昵称、昵称被格式化过）按位置兜底。
        if (!acc && cardIndex < pendingIds.length) {
          acc = byName[pendingIds[cardIndex]];
        }
        // 后端已保证账号池里每个账号都带完整字段（id/enabled/disabled/…），
        // 这里拿不到就是**数据真的缺了**，不该用占位数据把它遮住。
        if (!acc) {
          console.warn("[AutoBuddy] 账号卡片在后端账号池数据里找不到条目:", cardTitle);
          return;
        }

        var bar = document.createElement("div");
        bar.className = "wb-pool-bar";

        // 两种「不可用」要分开说，否则用户会以为是自己点错了：
        //   - disabled：被账号级停用策略挡住（可能是人点的，也可能是巡检写的）
        //   - 池外：不在 enabledAccountIds 白名单里
        var accDisabled = !!acc.disabled;
        var inPool = !!acc.enabled;

        var toggle = document.createElement("button");
        if (accDisabled) {
          // 来源不同措辞不同。巡检停用用琥珀色（.wb-pool-btn-auto），
          // 与手动停用的灰底区分开，让人一眼看出「这不是我点的」。
          var autoOff = acc.disabledSource === "auto";
          toggle.className = "wb-pool-btn" + (autoOff ? " wb-pool-btn-auto" : "");
          toggle.textContent = autoOff ? "已停用（巡检）· 点击启用" : "已停用 · 点击启用";
          toggle.title = autoOff
            ? "可用性巡检发现这个账号登录已过期或被上游拦截，自动停用了它。\n恢复后巡检会自己放回来，也可以点这里立即启用。"
            : "你手动停用了这个账号。点击恢复参与调用。";
        } else if (!inPool) {
          toggle.className = "wb-pool-btn";
          toggle.textContent = "不在账号池 · 点击启用";
          toggle.title = "这个账号不在账号池的启用列表里，不参与 API 调用。";
        } else {
          toggle.className = "wb-pool-btn wb-pool-btn-on";
          toggle.textContent = "参与调用 · 点击停用";
          toggle.title = "点击后这个账号不再参与自动轮询（显式指定它的请求仍可用）。";
        }
        toggle.onclick = function () {
          // 账号级停用优先：红/灰状态点一下就恢复。
          if (accDisabled) {
            wbToggleAccount(acc.id, false, function () { wbPoolCache = null; refreshPool(); });
            return;
          }
          // 否则走账号池白名单（旧语义，保留不变）。
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
          // 设为首选：必须连 enabledAccountIds 一起提交，且确保目标账号在白名单内。
          // 若为新账号且尚未加入白名单，设为首选时自动将其追加进启用列表，
          // 否则后端安全校验因 manualAccountId not in pool 会退回 auto，导致设置失效。
          var ids = (data.allEnabledByDefault
            ? (data.accounts || []).map(function (a) { return a.id; })
            : (data.enabledAccountIds || [])).slice();
          if (!pinned && acc.id && ids.indexOf(acc.id) === -1) {
            ids.push(acc.id);
          }
          savePool(
            pinned
              ? { mode: "auto", manualAccountId: null, enabledAccountIds: ids }
              : { mode: "manual", manualAccountId: acc.id, enabledAccountIds: ids },
            function () { wbPoolCache = null; refreshPool(); }
          );
        };

        // 手动检测：发一次轻量鉴权请求，就地报告这个账号现在能不能用。
        // 只报告、不改配置 —— 要停用由用户看着结果自己决定（旁边就是停用按钮）。
        var probe = document.createElement("button");
        probe.className = "wb-pool-btn";
        probe.textContent = "检测账号";
        probe.title = "发一次轻量鉴权请求验证凭据是否有效，不消耗额度";
        probe.onclick = function () {
          if (probe.dataset.wbBusy === "1") return;
          probe.dataset.wbBusy = "1";
          probe.disabled = true;
          probe.textContent = "检测中…";
          fetch("/api/account-health/probe", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ accountId: acc.id })
          })
            .then(function (r) { return r.json(); })
            .then(function (res) {
              if (!res || res.error) {
                wbToast("检测失败：" + ((res && res.error) || "无法访问接口"), "error");
                return;
              }
              // 结论一律以 state 为准（valid / invalid / restricted / unknown）。
              // 不再自行解释 verdict 或状态码 —— 那正是过去同一个账号
              // 在这里显示有效、在巡检里显示失效的来源。
              //
              // 配色也要跟着状态走，而不是「非 valid 就一律红」：
              // 受限账号（restricted）的凭据其实是好的，用红色会让用户以为
              // 凭据坏了、跑去重新登录 —— 而重登对这种情况毫无帮助。
              // 三档语义：valid=绿、restricted/unknown=琥珀、invalid=红。
              var state = res.state || "";
              var tone = state === "valid" ? "ok"
                       : (state === "invalid" ? "error" : "warn");
              // 提示只讲一件事：**账号现在能不能用**，外加一句该怎么办。
              //
              // 全文案来自后端下发的 userMessage（一句人话），**不再拼
              // evidence / action**。它们说的是同一件事的另外两个说法：
              //
              //   userMessage  账号被上游拦截，暂时用不了
              //   evidence     账号已被上游拦截              ← 同义重复
              //   action       已并入上面的结论，不再单独拼
              //
              // 三句拼起来是一段要读三遍的话，而用户只需要知道
              //「能不能用、要不要动手」。完整成因仍在接口里，排查时看得到。
              // 「重新扫码也没用」这类需要展开的说明放设置页，不进提示条。
              // 正常档连建议都没有：多一句话反而让人以为还有别的事要处理。
              var name = res.accountName || "账号";
              var msg = name + "：" + (res.userMessage || res.message || res.verdict);
              wbToast(msg, tone);
            })
            .catch(function (e) { wbToast("检测请求失败：" + e, "error"); })
            .then(function () {
              probe.disabled = false;
              probe.textContent = "检测账号";
              probe.dataset.wbBusy = "0";
            });
        };

        var count = document.createElement("span");
        var n = counts[acc.id] || 0;
        count.className = "wb-pool-count" + (n > 0 ? " wb-pool-count-hot" : "");
        // 计数标签**始终渲染**（即使是 0）——「没有调用过」本身就是有效信息，
        // 没这个标签用户无法区分「从没调用」与「这功能不存在」。
        count.textContent = "已调用 " + n + " 次";

        bar.appendChild(toggle);
        bar.appendChild(pin);
        bar.appendChild(probe);
        bar.appendChild(count);

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

  function wbRefreshAccountToken(accountId, done) {
    fetch("/api/refresh-token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accountId: accountId, exportSecrets: false })
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          return { ok: r.ok, body: body || {} };
        });
      })
      .then(function (res) {
        if (!res.ok || res.body.ok === false) {
          var d = res.body.detail || res.body.error;
          wbToast(typeof d === "string" && d ? "刷新失败：" + d : "刷新失败，请重试", "err");
          if (done) done(false);
          return;
        }
        wbToast("Token 已刷新");
        if (done) done(true);
      })
      .catch(function () {
        wbToast("请求失败，请刷新页面后重试", "err");
        if (done) done(false);
      });
  }

  function wbDeleteAccount(accountId, label, done) {
    // 删除是不可逆的，必须先确认；把账号名写进提示里，
    // 避免在长列表里点错行却按习惯直接确认。
    if (!window.confirm("确定删除账号「" + (label || accountId) + "」？\n\n"
        + "删除后该账号的登录凭据会被移除，需重新登录才能恢复；"
        + "它的模型禁用策略与停用记录会一并清理。")) {
      return;
    }
    fetch("/api/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accountId: accountId })
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          return { ok: r.ok, body: body || {} }; 
        });
      })
      .then(function (res) {
        if (!res.ok || res.body.ok === false) {
          var d = res.body.detail || res.body.error;
          wbToast(typeof d === "string" && d ? "删除失败：" + d : "删除失败，请重试", "err");
          if (done) done(false);
          return;
        }
        wbToast("已删除账号");
        if (done) done(true);
      })
      .catch(function () {
        wbToast("请求失败，请刷新页面后重试", "err");
        if (done) done(false);
      });
  }

  function wbManualCheckin(accountId, done) {
    // 手动签到走每日任务服务（它本来就在做签到，不另开一条路径）。
    fetch("/api/wb-daily/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accountId: accountId || undefined })
    })
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (d) {
        if (!d || !d.job_id) {
          wbToast("签到启动失败：" + ((d && (d.detail || d.error)) || "未知原因"), "err");
          if (done) done(false);
          return;
        }
        wbToast("已开始签到，可在「每日任务」页看进度");
        if (done) done(true);
      })
      .catch(function () {
        wbToast("请求失败，请刷新页面后重试", "err");
        if (done) done(false);
      });
  }

  function refreshPool() {
    Array.prototype.slice.call(document.querySelectorAll(".wb-pool-bar")).forEach(function (el) {
      el.remove();
    });
    injectAccountPool();
  }

  function wbPinAccountBlocks() {
    // 账号卡片里我们注入的两块（账号池控制条 / 可用模型）必须**固定在卡片末尾**，
    // 且顺序恒为 [控制条] → [模型区]。有两处竞态会让顺序漂移：
    //   1) 官方的「近期到期 / 积分明细」是数据到齐后才由 React 追加的。React 判定
    //      它是末节点时走的是 appendChild，于是它落到我们两块**之后** —— 同一页里
    //      就出现「积分明细一会儿在上一会儿在下」；
    //   2) 我们自己的两个 fetch（/api/account-pool 与 /api/account-models）谁先回来
    //      谁先 append，控制条与模型区也会互换位置。
    // 每次渲染后按固定顺序把我们两块挪回末尾即可。**已经是目标顺序时不动作**，
    // 否则 appendChild 会触发新的 DOM 变更，被 MutationObserver 接住后无限循环。
    var cards = Array.prototype.slice.call(document.querySelectorAll("article"));
    cards.forEach(function (card) {
      var sec = card.querySelector("section") || card;
      var pool = null;
      var box = null;
      Array.prototype.slice.call(sec.children).forEach(function (child) {
        if (!child.classList) return;
        if (child.classList.contains("wb-pool-bar")) pool = child;
        else if (child.classList.contains("wb-am-box")) box = child;
      });
      var tail = [];
      if (pool) tail.push(pool);
      if (box) tail.push(box);
      if (!tail.length) return;          // 官方自己的卡片，不碰

      var kids = Array.prototype.slice.call(sec.children);
      var n = tail.length;
      var pinned = n <= kids.length && tail.every(function (el, i) {
        return kids[kids.length - n + i] === el;
      });
      if (pinned) return;
      tail.forEach(function (el) { sec.appendChild(el); });
    });
  }

  /* ------------------------------------------------------------------
     设置页：API 接入
     容器只做两件事 —— 账号管理/自动签到，以及对外提供 OpenAI 兼容 API。
     但接入地址与密钥此前在 UI 上完全不可见，这里把「怎么连、用什么密钥连」
     直接落到设置页，用户不用去翻 README。
     ------------------------------------------------------------------ */

  var WB_API_BASE_LS = "wb_api_base_url";

  // tone: "info"（默认，纯告知）| "ok" | "warn" | "error"
  //
  // 仍然接受**布尔**作为第二个参数（true = error），旧调用点不必全改；
  // 但新代码请传字符串档位 —— 二档不够用：「账号受限」既不是成功也不是
  // 需要你重登的错误，用红色会把人引向错误的排查方向。
  function wbToast(message, tone) {
    // tone 兼容三种输入，避免调用点写错就静默变默认档：
    //   字符串 "ok" / "info"  -> 绿（或默认告知档）
    //   字符串 "warn"         -> 琥珀
    //   字符串 "err" / "error" -> 红
    //   true                  -> 红（旧布尔签名）
    //   false / 未传           -> 默认
    var t;
    if (tone === true) t = "err";
    else if (tone === false || tone == null) t = "info";
    else if (tone === "error") t = "err";
    else t = String(tone);
    var cls = "wb-api-toast";
    if (t === "ok" || t === "warn" || t === "err") { cls += " wb-api-toast-" + t; }
    var el = document.createElement("div");
    el.className = cls;
    el.textContent = message;
    document.body.appendChild(el);
    requestAnimationFrame(function () { el.classList.add("wb-api-toast-show"); });
    // 文案越长停留越久：结论 + 依据 + 建议三行挤在一起一闪而过，等于没提示。
    var hold = 1800 + Math.max(0, String(message || "").length - 24) * 45;
    setTimeout(function () {
      el.classList.remove("wb-api-toast-show");
      setTimeout(function () { el.remove(); }, 260);
    }, Math.min(hold, 7000));
  }

  function wbCopy(text, okMessage) {
    // 局域网走的是 http://，不是安全上下文，navigator.clipboard 直接不可用，
    // 所以必须有 execCommand 兜底，否则「复制」按钮点了没反应。
    function fallback() {
      try {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.top = "-1000px";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        ta.setSelectionRange(0, ta.value.length);
        var ok = document.execCommand("copy");
        ta.remove();
        wbToast(ok ? (okMessage || "已复制") : "复制失败，请手动选中复制", !ok);
      } catch (e) {
        wbToast("复制失败，请手动选中复制", true);
      }
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { wbToast(okMessage || "已复制"); }, fallback);
    } else {
      fallback();
    }
  }

  // 所有写操作走这里：统一把网关返回的 detail 弹出来。
  // 后端的「最后一个密钥不能停用」「零密钥不能开启校验」这类防线必须让用户看见，
  // 否则点了按钮没反应，只会以为 UI 坏了。
  function wbApiAction(url, method, payload, okMessage) {
    return fetch(url, {
      method: method,
      headers: { "Content-Type": "application/json" },
      body: payload === undefined ? undefined : JSON.stringify(payload)
    })
      .then(function (r) {
        return r.json()
          .catch(function () { return {}; })
          .then(function (body) { return { ok: r.ok, body: body || {} }; });
      })
      .then(function (res) {
        if (!res.ok || res.body.ok === false) {
          var d = res.body.detail;
          wbToast(typeof d === "string" && d ? d : (res.body.error || "操作失败"), true);
          return null;
        }
        if (okMessage) wbToast(okMessage);
        wbRefreshApi(true);
        return res.body;
      })
      .catch(function () {
        wbToast("请求失败，请刷新页面后重试", true);
        return null;
      });
  }

  function wbApiBase() {    var saved = "";
    try { saved = localStorage.getItem(WB_API_BASE_LS) || ""; } catch (e) { saved = ""; }
    if (saved) return saved;
    var loc = window.location;
    var host = loc.hostname || "127.0.0.1";
    // IPv6 需要方括号
    if (host.indexOf(":") >= 0 && host.charAt(0) !== "[") host = "[" + host + "]";
    var scheme = loc.protocol === "https:" ? "https:" : "http:";
    return scheme + "//" + host + ":18091/v1";
  }

  function wbSetApiBase(value) {
    try {
      if (value) localStorage.setItem(WB_API_BASE_LS, value);
      else localStorage.removeItem(WB_API_BASE_LS);
    } catch (e) {}
  }

  function wbEl(tag, className, text) {
    var el = document.createElement(tag);
    if (className) el.className = className;
    if (text != null) el.textContent = text;
    return el;
  }

  function wbApiRow(label, desc, control) {
    var row = wbEl("div", "wb-api-row");
    var main = wbEl("div", "wb-api-main");
    main.appendChild(wbEl("div", "wb-api-label", label));
    if (desc) main.appendChild(wbEl("div", "wb-api-desc", desc));
    row.appendChild(main);
    if (control) row.appendChild(control);
    return row;
  }

  function wbChip(text) {
    var chip = wbEl("span", "wb-api-chip wb-api-mono");
    chip.appendChild(wbEl("span", null, text));
    return chip;
  }

  function wbTime(ms) {
    if (!ms) return "从未";
    try {
      var d = new Date(ms);
      var p = function (n) { return (n < 10 ? "0" : "") + n; };
      return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
        " " + p(d.getHours()) + ":" + p(d.getMinutes());
    } catch (e) { return "—"; }
  }

  function wbFindSettingsHost() {
    // 设置页标题是唯一的 h1「设置」，用它定位比依赖路由稳定。
    var h1s = document.querySelectorAll("h1");
    for (var i = 0; i < h1s.length; i++) {
      if ((h1s[i].textContent || "").trim() !== "设置") continue;
      var header = h1s[i].closest("header");
      if (!header) continue;
      return header.nextElementSibling || header.parentElement || null;
    }
    return null;
  }

  function wbRenderApiSection(host, data) {
    var gw = data.gateway || {};
    var auth = gw.auth || {};
    var models = gw.models || {};
    var accounts = gw.accounts || {};
    var stats = data.stats || {};
    var keys = data.keys || [];
    var base = wbApiBase();

    var section = wbEl("section", "min-w-0 space-y-2.5");
    section.id = "settings-api-access";
    var head = wbEl("div", "px-1");
    var h2 = wbEl("h2", "text-[13px] font-medium leading-5", "API 接入");
    h2.id = "settings-api-access-title";
    head.appendChild(h2);
    section.appendChild(head);
    section.setAttribute("aria-labelledby", "settings-api-access-title");

    var card = wbEl("div", "wb-api-card");

    // ---- 1. 对外访问地址（可改，覆盖隧道/反代场景）----
    var addrRow = wbEl("div", "wb-api-row wb-api-row-stack");
    var addrHead = wbEl("div", null);
    addrHead.appendChild(wbEl("div", "wb-api-label", "对外访问地址"));
    addrHead.appendChild(wbEl("div", "wb-api-desc",
      "下游客户端（Sub2API、Cherry Studio、各类 Agent）填这个地址。默认按当前访问的主机名自动推导，走域名或隧道时可手动改。"));
    addrRow.appendChild(addrHead);

    var addrLine = wbEl("div", null);
    addrLine.style.cssText = "display:flex;align-items:center;gap:8px;margin-top:8px;";
    var addrInput = document.createElement("input");
    addrInput.className = "wb-api-input wb-api-mono";
    addrInput.value = base;
    addrInput.spellcheck = false;
    var addrCopy = wbEl("button", "wb-api-btn wb-api-btn-primary", "复制");
    addrCopy.onclick = function () { wbCopy(addrInput.value.trim(), "已复制接入地址"); };
    var addrReset = wbEl("button", "wb-api-btn", "恢复默认");
    addrReset.onclick = function () {
      wbSetApiBase("");
      addrInput.value = wbApiBase();
      wbToast("已恢复为自动推导地址");
    };
    addrInput.onchange = function () {
      wbSetApiBase(addrInput.value.trim());
      wbToast("已保存自定义地址");
    };
    addrLine.appendChild(addrInput);
    addrLine.appendChild(addrCopy);
    addrLine.appendChild(addrReset);
    addrRow.appendChild(addrLine);
    card.appendChild(addrRow);

    // ---- 2. 端点 ----
    var epRow = wbEl("div", "wb-api-row");
    var epMain = wbEl("div", "wb-api-main");
    epMain.appendChild(wbEl("div", "wb-api-label", "接口端点"));
    epMain.appendChild(wbEl("div", "wb-api-desc", "标准 OpenAI 协议，改 base_url 即可直接替换官方端点。"));
    epRow.appendChild(epMain);
    var epBtns = wbEl("div", null);
    epBtns.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end;";
    [["POST /v1/chat/completions", "/v1/chat/completions"],
     ["GET /v1/models", "/v1/models"]].forEach(function (item) {
      var b = wbEl("button", "wb-api-btn wb-api-mono", item[0]);
      b.title = "点击复制完整地址";
      b.onclick = function () { wbCopy(addrInput.value.trim().replace(/\/+$/, "") + item[1], "已复制 " + item[1]); };
      epBtns.appendChild(b);
    });
    epRow.appendChild(epBtns);
    card.appendChild(epRow);

    // ---- 3. 运行状态 ----
    var stRow = wbEl("div", "wb-api-row");
    var stMain = wbEl("div", "wb-api-main");
    stMain.appendChild(wbEl("div", "wb-api-label", "网关状态"));
    stMain.appendChild(wbEl("div", "wb-api-desc",
      "版本 " + (gw.version || "—") + " · 账号池模式 " +
      ((accounts.mode === "manual") ? "手动指定" : "自动轮换")));
    stRow.appendChild(stMain);
    var stBadges = wbEl("div", null);
    stBadges.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end;";
    stBadges.appendChild(wbEl("span", "wb-api-badge wb-api-badge-ok",
      "模型 " + (models.count || 0) + " 个"));
    stBadges.appendChild(wbEl("span", "wb-api-badge" + ((accounts.usable || 0) > 0 ? " wb-api-badge-ok" : " wb-api-badge-warn"),
      "可用账号 " + (accounts.usable || 0) + "/" + (accounts.total || 0)));
    stBadges.appendChild(wbEl("span", "wb-api-badge", "参与调用 " + (accounts.inPool || 0) + " 个"));
    stRow.appendChild(stBadges);
    card.appendChild(stRow);

    // ---- 4. 密钥校验开关 ----
    var authRow = wbEl("div", "wb-api-row");
    var authMain = wbEl("div", "wb-api-main");
    authMain.appendChild(wbEl("div", "wb-api-label", "强制 API 密钥校验"));
    authMain.appendChild(wbEl("div", "wb-api-desc", data.requireKey
      ? "已开启：调用 /v1/* 必须携带有效密钥，缺失或错误返回 401。"
      : "未开启：任何能访问该端口的客户端都可直接调用。仅建议在纯内网环境下保持关闭。"));
    authRow.appendChild(authMain);
    var authToggle = wbEl("button",
      "wb-api-btn" + (data.requireKey ? " wb-api-btn-on" : ""),
      data.requireKey ? "已开启 · 点击关闭" : "已关闭 · 点击开启");
    authToggle.onclick = function () {
      authToggle.disabled = true;
      wbApiAction("/api/api-keys/config", "PUT", { requireKey: !data.requireKey },
        !data.requireKey ? "已开启密钥校验" : "已关闭密钥校验")
        .then(function () { authToggle.disabled = false; });
    };
    authRow.appendChild(authToggle);
    card.appendChild(authRow);

    // ---- 4.5 IP 白名单免验证（仅在开启密钥校验时展示）----
    //
    // 为何要隐藏：关掉密钥校验时后端就整体放行了，白名单不产生任何效果。
    // 展示一个“没用”的输入框只会让人误会它在生效。配置本身会保留，
    // 重新打开校验就原样恢复，不会因为隐藏而丢失。
    if (data.requireKey) {
      var wlRow = wbEl("div", "wb-api-row wb-api-row-stack");
      var wlHead = wbEl("div", null);
      wlHead.appendChild(wbEl("div", "wb-api-label", "IP 白名单（免密钥）"));
      wlHead.appendChild(wbEl("div", "wb-api-desc",
        "名单内的来源 IP 调用 /v1/* 无需携密钥。支持精确 IP（192.168.31.5）与 CIDR 网段"
        + "（192.168.31.0/24），一行一个，也可用逗号分隔。留空则不启用白名单。"));
      wlRow.appendChild(wlHead);

      var wlBox = document.createElement("textarea");
      wlBox.className = "wb-api-input wb-api-mono";
      wlBox.rows = 3;
      wlBox.spellcheck = false;
      wlBox.placeholder = "192.168.31.5\n192.168.31.0/24";
      wlBox.style.cssText = "width:100%;margin-top:8px;resize:vertical;line-height:1.5;";
      wlBox.value = (data.whitelist || []).join("\n");
      wlRow.appendChild(wlBox);

      var wlLine = wbEl("div", null);
      wlLine.style.cssText = "display:flex;align-items:center;gap:8px;margin-top:8px;";
      var wlSave = wbEl("button", "wb-api-btn wb-api-btn-primary", "保存白名单");
      wlSave.onclick = function () {
        wlSave.disabled = true;
        wbApiAction("/api/api-keys/whitelist", "PUT", { whitelist: wlBox.value },
          "白名单已保存").then(function () { wlSave.disabled = false; });
      };
      wlLine.appendChild(wlSave);

      var wlSelf = wbEl("button", "wb-api-btn", "添加当前 IP");
      wlSelf.title = "把你现在访问控制台的来源 IP 加进白名单";
      wlSelf.onclick = function () {
        wlSelf.disabled = true;
        fetch("/api/client-ip", { cache: "no-store" })
          .then(function (r) { return r.ok ? r.json() : {}; })
          .then(function (d) {
            var ip = (d && d.ip) || "";
            if (!ip) { wbToast("未能获取当前 IP", "warn"); return; }
            var cur = wlBox.value.trim();
            if (cur.split(/[\n,]/).map(function (s) { return s.trim(); }).indexOf(ip) !== -1) {
              wbToast("当前 IP 已在名单中：" + ip, "ok");
              return;
            }
            wlBox.value = cur ? (cur + "\n" + ip) : ip;
            wbToast("已填入当前 IP：" + ip + "，点「保存白名单」生效", "ok");
          })
          .catch(function () { wbToast("获取当前 IP 失败", "err"); })
          .then(function () { wlSelf.disabled = false; });
      };
      wlLine.appendChild(wlSelf);

      var wlCount = wbEl("span", "wb-api-desc", "共 " + (data.whitelistCount || 0) + " 条");
      wlLine.appendChild(wlCount);
      wlRow.appendChild(wlLine);
      card.appendChild(wlRow);
    }

    // ---- 5. 密钥列表 ----
    var keyHead = wbEl("div", "wb-api-row");
    var keyMain = wbEl("div", "wb-api-main");
    keyMain.appendChild(wbEl("div", "wb-api-label", "API 密钥"));
    keyMain.appendChild(wbEl("div", "wb-api-desc",
      "共 " + (stats.total || 0) + " 个，启用 " + (stats.enabled || 0) + " 个 · 累计调用 " + (stats.calls || 0) + " 次"
      + " · 最近使用 " + wbTime(stats.lastUsedAt)));
    keyHead.appendChild(keyMain);
    var keyActions = wbEl("div", null);
    keyActions.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end;";
    var addKey = wbEl("button", "wb-api-btn wb-api-btn-primary", "新建密钥");
    addKey.onclick = function () {
      var name = window.prompt("给这个密钥起个名字（便于区分调用方，可留空）：", "");
      if (name === null) return;
      wbApiAction("/api/api-keys", "POST", { name: name }).then(function (res) {
        if (res && res.key) wbCopy(res.key.key, "新密钥已生成并复制");
      });
    };
    keyActions.appendChild(addKey);
    if ((stats.total || 0) > 1) {
      var clearKeys = wbEl("button", "wb-api-btn wb-api-btn-danger", "清空全部");
      clearKeys.onclick = function () {
        if (!window.confirm("确认清空全部 " + stats.total + " 个密钥？已在使用这些密钥的客户端会立即失去访问权限。")) return;
        wbApiAction("/api/api-keys/delete-all", "POST", undefined, "已清空全部密钥");
      };
      keyActions.appendChild(clearKeys);
    }
    keyHead.appendChild(keyActions);
    card.appendChild(keyHead);

    var listRow = wbEl("div", "wb-api-row wb-api-row-stack");
    if (!keys.length) {
      listRow.appendChild(wbEl("div", "wb-api-empty",
        "还没有密钥。点「新建密钥」生成一个 —— 密钥格式为 sk-ab-…，明文保存在 /data/.autobuddy/api_keys.json（权限 0600）。"));
    } else {
      keys.forEach(function (k) {
        var line = wbEl("div", "wb-api-keyrow");
        var enabled = wbEl("span", "wb-api-badge" + (k.enabled ? " wb-api-badge-ok" : " wb-api-badge-warn"),
          k.enabled ? "启用" : "已停用");
        line.appendChild(enabled);
        line.appendChild(wbEl("span", "wb-api-keyname", k.name || "未命名"));
        line.appendChild(wbChip(k.maskedKey));

        var meta = wbEl("span", "wb-api-badge", "调用 " + (k.callCount || 0) + " 次");
        meta.title = "创建于 " + wbTime(k.createdAt) + " · 最近使用 " + wbTime(k.lastUsedAt);
        line.appendChild(meta);

        var spacer = wbEl("span", null);
        spacer.style.cssText = "flex:1 1 auto;";
        line.appendChild(spacer);

        var copyBtn = wbEl("button", "wb-api-btn", "复制明文");
        copyBtn.onclick = function () { wbCopy(k.key, "已复制该密钥明文"); };
        line.appendChild(copyBtn);

        var toggleBtn = wbEl("button", "wb-api-btn", k.enabled ? "停用" : "启用");
        toggleBtn.onclick = function () {
          wbApiAction("/api/api-keys/update", "POST", { id: k.id, enabled: !k.enabled },
            k.enabled ? "已停用该密钥" : "已启用该密钥");
        };
        line.appendChild(toggleBtn);

        var delBtn = wbEl("button", "wb-api-btn wb-api-btn-danger", "删除");
        delBtn.onclick = function () {
          if (!window.confirm("确认删除密钥「" + (k.name || "未命名") + "」？使用它的客户端会立即失去访问权限。")) return;
          wbApiAction("/api/api-keys/delete", "POST", { id: k.id }, "已删除");
        };
        line.appendChild(delBtn);

        listRow.appendChild(line);
      });
    }
    card.appendChild(listRow);

    // ---- 6. 自检 ----
    var selfRow = wbEl("div", "wb-api-row");
    var selfMain = wbEl("div", "wb-api-main");
    selfMain.appendChild(wbEl("div", "wb-api-label", "连通性自检"));
    var selfDesc = wbEl("div", "wb-api-desc",
      "在容器内回环实测网关健康、模型清单与密钥配置，用来确认「UI 能打开但 API 调不通」这类问题。");
    selfMain.appendChild(selfDesc);
    var selfResult = wbEl("div", "wb-api-empty");
    selfResult.style.display = "none";
    selfMain.appendChild(selfResult);
    selfRow.appendChild(selfMain);
    var selfBtn = wbEl("button", "wb-api-btn wb-api-btn-primary", "开始自检");
    selfBtn.onclick = function () {
      selfBtn.disabled = true;
      selfBtn.textContent = "检测中…";
      selfResult.style.display = "";
      selfResult.textContent = "正在检测…";
      fetch("/api/gateway-selftest", { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          var lines = (res.steps || []).map(function (s) {
            return (s.ok ? "✓ " : "✗ ") + s.name + " — " + s.detail;
          });
          selfResult.textContent = lines.join("\n");
          selfResult.style.whiteSpace = "pre-wrap";
          selfResult.style.color = res.ok ? "#15803d" : "#b91c1c";
          wbToast(res.ok ? "自检通过" : "自检发现问题", !res.ok);
        })
        .catch(function () {
          selfResult.textContent = "自检失败：无法访问代理接口。";
          selfResult.style.color = "#b91c1c";
          wbToast("自检失败", true);
        })
        .finally(function () {
          selfBtn.disabled = false;
          selfBtn.textContent = "重新自检";
        });
    };
    selfRow.appendChild(selfBtn);
    card.appendChild(selfRow);

    // ---- 7. 调用示例 ----
    var sampleRow = wbEl("div", "wb-api-row wb-api-row-stack");
    var sampleHead = wbEl("div", null);
    sampleHead.appendChild(wbEl("div", "wb-api-label", "调用示例"));
    sampleHead.appendChild(wbEl("div", "wb-api-desc",
      "把 <你的密钥> 换成上面任意一个启用中的密钥即可。"));
    sampleRow.appendChild(sampleHead);

    var sampleKey = (keys.filter(function (k) { return k.enabled; })[0] || {}).key || "<你的密钥>";
    var apiRoot = addrInput.value.trim().replace(/\/+$/, "");
    var sampleText =
      "curl " + apiRoot + "/chat/completions \\\n" +
      "  -H \"Content-Type: application/json\" \\\n" +
      "  -H \"Authorization: Bearer " + sampleKey + "\" \\\n" +
      "  -d '{\n" +
      "    \"model\": \"hy3\",\n" +
      "    \"messages\": [{\"role\": \"user\", \"content\": \"你好\"}]\n" +
      "  }'";
    sampleRow.appendChild(wbEl("div", "wb-api-code", sampleText));
    var sampleBar = wbEl("div", null);
    sampleBar.style.cssText = "display:flex;gap:6px;margin-top:8px;justify-content:flex-end;";
    var copySample = wbEl("button", "wb-api-btn", "复制示例");
    copySample.onclick = function () { wbCopy(sampleText, "已复制调用示例"); };
    sampleBar.appendChild(copySample);
    var copyAll = wbEl("button", "wb-api-btn wb-api-btn-primary", "复制接入信息");
    copyAll.onclick = function () {
      wbCopy([
        "AutoBuddy · OpenAI 兼容 API",
        "Base URL: " + apiRoot,
        "端点: POST /v1/chat/completions, GET /v1/models",
        "鉴权: Authorization: Bearer <key>" + (data.requireKey ? "（当前强制校验）" : "（当前未强制校验）"),
        "密钥: " + (keys.filter(function (k) { return k.enabled; }).map(function (k) { return k.key; }).join(" / ") || "（尚未创建）"),
        "可用模型: " + (models.count || 0) + " 个",
        "账号池: " + (accounts.inPool || 0) + " 个账号参与调用"
      ].join("\n"), "已复制完整接入信息");
    };
    sampleBar.appendChild(copyAll);
    sampleRow.appendChild(sampleBar);
    card.appendChild(sampleRow);

    section.appendChild(card);
    host.appendChild(section);
  }

  var wbApiCache = null;

  function wbRefreshApi(force) {
    if (force) { wbApiCache = null; wbInfoCache = null; }
    var old = document.getElementById("settings-api-access");
    if (old) old.remove();
    var oldHealth = document.getElementById("settings-model-health");
    if (oldHealth) oldHealth.remove();
    var oldAbout = document.getElementById("settings-about");
    if (oldAbout) oldAbout.remove();
    injectApiAccess();
    injectModelHealth();
    // 关于面板里有一行「当前规模」，禁用项增减后要跟着变，所以一并重取。
    injectAbout();
    return wbApiCache;
  }

  // ---------------------------------------------------------------------
  // 设置页：模型可用性巡检
  // 探测会真实向上游发请求，所以默认关闭；开启后按间隔逐个检测「账号 × 模型」，
  // 不可用自动禁用、恢复自动启用 —— 写的都是同一份 model_policy.json。
  // ---------------------------------------------------------------------
  function wbRenderHealthSection(host, data) {
    var cfg = data.config || {};
    var runtime = data.runtime || {};
    var accounts = data.accounts || [];
    var last = runtime.lastResult || null;

    var section = wbEl("section", "min-w-0 space-y-2.5");
    section.id = "settings-model-health";
    var head = wbEl("div", "px-1");
    var h2 = wbEl("h2", "text-[13px] font-medium leading-5", "模型可用性巡检");
    h2.id = "settings-model-health-title";
    head.appendChild(h2);
    section.appendChild(head);
    section.setAttribute("aria-labelledby", "settings-model-health-title");

    var card = wbEl("div", "wb-api-card");

    // ---- 1. 总开关 ----
    var swRow = wbEl("div", "wb-api-row");
    var swMain = wbEl("div", "wb-api-main");
    swMain.appendChild(wbEl("div", "wb-api-label", "自动巡检"));
    swMain.appendChild(wbEl("div", "wb-api-desc",
      "开启后每隔一段时间检测账号的模型可用性 —— 调不通的自动禁用，恢复的自动启用。"
      + "关掉后之前自动写入的禁用项依然生效，仍可在账号卡片上手动禁用 / 启用。"));
    swRow.appendChild(swMain);
    var swBtn = wbEl("button",
      "wb-api-btn" + (cfg.enabled ? " wb-api-btn-on" : ""),
      cfg.enabled ? "已开启 · 点击关闭" : "已关闭 · 点击开启");
    swBtn.onclick = function () {
      swBtn.disabled = true;
      wbApiAction("/api/model-health", "PUT", { enabled: !cfg.enabled },
        !cfg.enabled ? "已开启自动巡检" : "已关闭自动巡检")
        .then(function () { swBtn.disabled = false; });
    };
    swRow.appendChild(swBtn);
    card.appendChild(swRow);

    // ---- 2. 巡检间隔 ----
    var itvRow = wbEl("div", "wb-api-row");
    var itvMain = wbEl("div", "wb-api-main");
    itvMain.appendChild(wbEl("div", "wb-api-label", "巡检间隔"));
    itvMain.appendChild(wbEl("div", "wb-api-desc",
      "两次巡检之间等待多久，最短 " + (data.minIntervalMinutes || 5) + " 分钟。"
      + "每个组合都要发一次真实请求，间隔太短会产生可观的上游调用量。"));
    itvRow.appendChild(itvMain);
    var itvWrap = document.createElement("div");
    itvWrap.style.cssText = "display:flex;align-items:center;gap:6px;flex:0 0 auto;";
    var itvInput = document.createElement("input");
    itvInput.className = "wb-api-input wb-api-mono";
    itvInput.style.cssText = "width:78px;flex:0 0 auto;text-align:center;";
    itvInput.type = "number";
    itvInput.min = String(data.minIntervalMinutes || 5);
    itvInput.value = String(cfg.intervalMinutes || 60);
    var itvSave = wbEl("button", "wb-api-btn", "保存");
    itvSave.onclick = function () {
      var v = parseInt(itvInput.value, 10);
      if (isNaN(v)) { wbToast("请输入分钟数", true); return; }
      itvSave.disabled = true;
      wbApiAction("/api/model-health", "PUT", { intervalMinutes: v }, "已保存巡检间隔")
        .then(function () { itvSave.disabled = false; });
    };
    itvWrap.appendChild(itvInput);
    itvWrap.appendChild(wbEl("span", "wb-api-badge", "分钟"));
    itvWrap.appendChild(itvSave);
    itvRow.appendChild(itvWrap);
    card.appendChild(itvRow);

    // ---- 3. 账号范围 ----
    var scopeRow = wbEl("div", "wb-api-row wb-api-row-stack");
    var scopeMain = wbEl("div", "wb-api-main");
    scopeMain.appendChild(wbEl("div", "wb-api-label", "巡检账号范围"));
    scopeMain.appendChild(wbEl("div", "wb-api-desc",
      "不勾选任何账号 = 全部账号参与巡检。勾选后只有被选中的账号会被探测。"));
    scopeRow.appendChild(scopeMain);

    var picked = {};
    (cfg.accountIds || []).forEach(function (id) { picked[String(id)] = true; });
    var listWrap = wbEl("div", null);
    listWrap.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;";

    accounts.forEach(function (a) {
      var id = String(a.id);
      var on = !(cfg.accountIds || []).length || picked[id];
      var tag = wbEl("button",
        "wb-am-tag wb-am-tag-click" + (on ? " wb-am-tag-used" : ""),
        (a.name || id) + (a.variant === "ai" ? " · 国际版" : " · 国内版"));
      tag.title = on ? "已参与巡检，点击排除" : "未参与巡检，点击加入";
      tag.onclick = function () {
        var all = (cfg.accountIds || []).slice();
        var isAll = all.length === 0;
        var next;
        if (isAll) {
          // 从「全部」切到「显式列表」：先展开成全部账号，再去掉当前这个
          next = accounts.map(function (x) { return String(x.id); })
            .filter(function (x) { return x !== id; });
        } else if (all.indexOf(id) >= 0) {
          next = all.filter(function (x) { return x !== id; });
        } else {
          next = all.concat([id]);
        }
        // 显式选满全部账号时收敛回空数组，语义与「全部」等价
        if (next.length === accounts.length) next = [];
        wbApiAction("/api/model-health", "PUT", { accountIds: next }, "已更新巡检范围");
      };
      listWrap.appendChild(tag);
    });
    if (!accounts.length) {
      listWrap.appendChild(wbEl("div", "wb-api-empty", "还没有账号。"));
    }
    scopeRow.appendChild(listWrap);

    var scopeHint = wbEl("div", "wb-api-desc");
    scopeHint.style.marginTop = "8px";
    scopeHint.textContent = (cfg.accountIds || []).length
      ? "当前：指定 " + cfg.accountIds.length + " 个账号"
      : "当前：全部 " + accounts.length + " 个账号";
    scopeRow.appendChild(scopeHint);
    card.appendChild(scopeRow);

    // ---- 4. 自动启用 ----
    var aeRow = wbEl("div", "wb-api-row");
    var aeMain = wbEl("div", "wb-api-main");
    aeMain.appendChild(wbEl("div", "wb-api-label", "检测到可用时自动启用"));
    aeMain.appendChild(wbEl("div", "wb-api-desc",
      "开启时：巡检自己禁掉的模型恢复可用后会自动放开（自愈）。"
      + "你在账号卡片上手动禁用的模型不受影响 —— 手动禁用代表明确的取舍"
      + "（常见于「能跑但太贵」），不会被自愈抹掉。关闭时只做自动禁用。"));
    aeRow.appendChild(aeMain);
    var aeBtn = wbEl("button",
      "wb-api-btn" + (cfg.autoEnable ? " wb-api-btn-on" : ""),
      cfg.autoEnable ? "已开启 · 点击关闭" : "已关闭 · 点击开启");
    aeBtn.onclick = function () {
      aeBtn.disabled = true;
      wbApiAction("/api/model-health", "PUT", { autoEnable: !cfg.autoEnable },
        !cfg.autoEnable ? "已开启自动启用" : "已关闭自动启用")
        .then(function () { aeBtn.disabled = false; });
    };
    aeRow.appendChild(aeBtn);
    card.appendChild(aeRow);

    // ---- 4b. 当前禁用构成 ----
    // 把「谁关的」摊开给用户看。这两类禁用含义不同、恢复方式也不同，
    // 混在一个「已禁用 N」里会让人无法判断巡检是否已经生效。
    var srcRow = wbEl("div", "wb-api-row");
    var srcMain = wbEl("div", "wb-api-main");
    srcMain.appendChild(wbEl("div", "wb-api-label", "当前禁用构成"));
    var bySource = data.disabledBySource || {};
    var manualN = bySource.manual || 0;
    var autoN = bySource.auto || 0;
    srcMain.appendChild(wbEl("div", "wb-api-desc",
      "手动禁用 " + manualN + " 项（你点的，巡检不会自动放开）"
      + " · 巡检禁用 " + autoN + " 项（探测不通自动写的，恢复后自动放开）"));
    srcRow.appendChild(srcMain);
    card.appendChild(srcRow);

    // ---- 5. 探测范围口径 ----
    var umRow = wbEl("div", "wb-api-row");
    var umMain = wbEl("div", "wb-api-main");
    umMain.appendChild(wbEl("div", "wb-api-label", "只探测用过的模型"));
    umMain.appendChild(wbEl("div", "wb-api-desc",
      "开启时只检测该账号实际调用过的模型（新账号无记录时退回基础清单）；"
      + "关闭时会探测全部已知模型 —— 组合数量会大很多，谨慎使用。"));
    umRow.appendChild(umMain);
    var umBtn = wbEl("button",
      "wb-api-btn" + (cfg.onlyUsedModels ? " wb-api-btn-on" : ""),
      cfg.onlyUsedModels ? "已开启 · 点击关闭" : "已关闭 · 点击开启");
    umBtn.onclick = function () {
      umBtn.disabled = true;
      wbApiAction("/api/model-health", "PUT", { onlyUsedModels: !cfg.onlyUsedModels },
        !cfg.onlyUsedModels ? "已收窄探测范围" : "已放开探测范围")
        .then(function () { umBtn.disabled = false; });
    };
    umRow.appendChild(umBtn);
    card.appendChild(umRow);

    // ---- 6. 立即巡检 + 上次结果 ----
    var runRow = wbEl("div", "wb-api-row");
    var runMain = wbEl("div", "wb-api-main");
    runMain.appendChild(wbEl("div", "wb-api-label", "立即巡检一次"));
    var runDesc = wbEl("div", "wb-api-desc");
    if (runtime.running) {
      runDesc.textContent = "正在巡检中…";
    } else if (last && last.aborted) {
      // 整轮作废：一个可用的都没有，说明探测本身出了问题。
      // 这必须说得比「上次巡检完成」更显眼，否则用户会以为巡检正常但模型全坏了。
      runDesc.textContent = "上次巡检已跳过（未改动任何配置）：" + (last.reason || "探测异常");
      runDesc.style.color = "#b45309";
    } else if (runtime.lastError) {
      // 探测被上游参数校验拒绝、账号凭据失效等情况：本轮结果部分不可信，
      // 已经跳过了那些组合，必须显式说出来，否则看起来就跟「什么都没变」一样。
      runDesc.textContent = "上次巡检有情况被跳过：" + runtime.lastError;
      runDesc.style.color = "#b45309";
    } else if (last) {
      var c = last.counts || {};
      runDesc.textContent = "上次巡检 " + wbTime(last.checkedAt)
        + " · 共 " + (last.combos || 0) + " 个组合（" + (last.accounts || 0) + " 个账号）"
        + " · 可用 " + (c.available || 0)
        + " / 不可用 " + (c.unavailable || 0)
        + " / 跳过 " + (c.transient || 0);
      // 探测被上游参数校验拒绝：只在真的发生过时才显示，避免日常噪音。
      if (c.probe_defect) {
        runDesc.textContent += " / 探测被拒 " + c.probe_defect;
      }
      // 账号被上游拦截：与「不可用」分开显示。它既不是模型坏，也不是凭据失效，
      // 混进「不可用」会让用户点开一堆账号去找原因。
      if (c.restricted) {
        runDesc.textContent += " / 账号被拦截 " + c.restricted;
      }
      // 手动禁用的模型这一轮整个没探测。不说出来的话，用户只会看到组合数
      // 比「账号 × 模型」总数少，却不知道差在哪。
      if (last.skippedManual) {
        runDesc.textContent += " · 手动禁用 " + last.skippedManual + " 项未探测";
      }
      if ((last.disabled || []).length) {
        runDesc.textContent += " · 新禁用 " + last.disabled.length + " 项";
      }
      if ((last.enabled || []).length) {
        runDesc.textContent += " · 新启用 " + last.enabled.length + " 项";
      }
    } else {
      runDesc.textContent = "还没有执行过巡检。点右边按钮可以立刻跑一轮，"
        + "不改变上面的开关状态。";
    }
    runMain.appendChild(runDesc);
    runRow.appendChild(runMain);

    var runBtn = wbEl("button", "wb-api-btn wb-api-btn-primary",
      runtime.running ? "巡检中…" : "立即巡检");
    if (runtime.running) runBtn.disabled = true;
    runBtn.onclick = function () {
      runBtn.disabled = true;
      runBtn.textContent = "巡检中…";
      wbToast("已开始巡检，逐个组合探测中，请稍候…");
      fetch("/api/model-health/run", { method: "POST", body: "{}" })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          var out = res && res.result ? res.result : res;
          if (!out || out.error) {
            wbToast("巡检失败：" + ((out && out.error) || "未知错误"), true);
            return;
          }
          if (out.aborted) {
            wbToast("巡检已跳过（未改动配置）：" + (out.reason || "探测异常"), true);
            return;
          }
          var cc = out.counts || {};
          var msg = "巡检完成 · 可用 " + (cc.available || 0)
            + " / 不可用 " + (cc.unavailable || 0)
            + " / 跳过 " + (cc.transient || 0);
          if (cc.probe_defect) msg += " / 探测被拒 " + cc.probe_defect;
          if (cc.restricted) msg += " / 账号被拦截 " + cc.restricted;
          if ((out.disabled || []).length) msg += " · 新禁用 " + out.disabled.length + " 项";
          if ((out.enabled || []).length) msg += " · 新启用 " + out.enabled.length + " 项";
          if ((out.protected || []).length) msg += " · 手动禁用已跳过 " + out.protected.length + " 项";
          if ((out.authFailed || []).length) {
            msg += " · " + out.authFailed.length + " 个账号凭据失效已跳过";
          }
          if ((out.restricted || []).length) {
            msg += " · " + out.restricted.length + " 个账号被上游拦截";
          }
          wbToast(msg);
        })
        .catch(function (e) { wbToast("巡检请求失败：" + e, true); })
        .then(function () {
          runBtn.disabled = false;
          runBtn.textContent = "立即巡检";
          // 重取配置与上次结果：巡检可能改了禁用策略，账号卡片与巡检面板都要跟着更新。
          wbRefreshApi(true);
          refreshModelTags();
        });
    };
    runRow.appendChild(runBtn);

    // 「一致性自检」：与「立即巡检」分开的两个动作。
    // 巡检会**改配置**（自动禁用/启用），自检只读 —— 排查问题时不该顺带动了配置。
    var auditBtn = wbEl("button", "wb-api-btn", "一致性自检");
    auditBtn.title = "逐个账号对照「凭据」与「模型」两侧的结论，不改动任何配置";
    auditBtn.onclick = function () {
      auditBtn.disabled = true;
      auditBtn.textContent = "自检中…";
      wbToast("正在逐账号对照检测，账号多时需要一会儿…");
      fetch("/api/account-health/audit")
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d || d.error) {
            wbToast("自检失败：" + ((d && d.error) || "无法访问接口"), true);
            return;
          }
          wbRenderAuditBox(card, d);
          wbToast("自检完成：" + Object.keys(d.summary || {}).map(function (k) {
            return k + " " + d.summary[k];
          }).join(" · "));
        })
        .catch(function (e) { wbToast("自检请求失败：" + e, true); })
        .then(function () {
          auditBtn.disabled = false;
          auditBtn.textContent = "一致性自检";
        });
    };
    runRow.appendChild(auditBtn);
    card.appendChild(runRow);

    // ---- 6b. 凭据失效被整账号跳过的名单（有才显示）----
    // 一个账号的 token 过期会让它名下所有模型一起失败，但坏的是凭据不是模型。
    // 巡检会整账号跳过并在这里点名，而不是默默禁用掉整个模型清单。
    if (last && (last.authFailed || []).length) {
      var afRow = wbEl("div", "wb-api-row wb-api-row-stack");
      var afMain = wbEl("div", "wb-api-main");
      afMain.appendChild(wbEl("div", "wb-api-label", "凭据失效、本轮已跳过的账号"));
      afMain.appendChild(wbEl("div", "wb-api-desc",
        (last.authFailed || []).map(function (a) { return a.name || a.id; }).join("、")
        + " —— 这些账号的上游鉴权已失效，坏的是凭据而不是模型，"
        + "本轮没有为它们写入任何禁用项。重新登录后再跑一轮即可。"));
      afRow.appendChild(afMain);
      card.appendChild(afRow);
    }

    // ---- 6c. 被上游拦截的账号名单 + 「重新扫码没用」的说明（有才显示）----
    //
    // 这一块存在的唯一理由是**把「重扫没用」讲清楚**。
    // 提示条只来得及说「账号被上游拦截，暂时用不了」，用户看到 403 的第一反应
    // 通常是「重新登录一下试试」—— 而实测证明那是在白费功夫：
    // 同一个账号换一张全新凭据后仍然被拦（403/11140，响应 0.4s，请求根本没进模型），
    // 说明拦的是账号本身，不是这张凭据、也不是发的内容或调用频率。
    // 这个结论需要完整几句话才讲得清，所以它归设置页，不进提示条。
    if (last && (last.restricted || []).length) {
      var rsRow = wbEl("div", "wb-api-row wb-api-row-stack");
      var rsMain = wbEl("div", "wb-api-main");
      rsMain.appendChild(wbEl("div", "wb-api-label", "被上游拦截的账号"));
      rsMain.appendChild(wbEl("div", "wb-api-desc",
        (last.restricted || []).map(function (a) { return a.name || a.id; }).join("、")
        + " —— 这些账号登录是好的，但发出的请求被上游直接拦下（403），"
        + "所以暂时用不了。"));
      rsMain.appendChild(wbEl("div", "wb-api-desc",
        "重新扫码登录解决不了：同一个账号换上新凭据后依然被拦，"
        + "上游认的是账号本身。建议直接删除这些账号，换新的账号使用。"));
      rsRow.appendChild(rsMain);
      card.appendChild(rsRow);
    }

    // ---- 7. 最近变更明细（有才显示）----
    if (last && ((last.disabled || []).length || (last.enabled || []).length
        || (last.protected || []).length)) {
      var logRow = wbEl("div", "wb-api-row wb-api-row-stack");
      var logMain = wbEl("div", "wb-api-main");
      logMain.appendChild(wbEl("div", "wb-api-label", "上次巡检的变更"));
      logMain.appendChild(wbEl("div", "wb-api-desc",
        "自动禁用：" + ((last.disabled || []).join("、") || "无")
        + "　自动启用：" + ((last.enabled || []).join("、") || "无")));
      if ((last.protected || []).length) {
        // 这一行是「来源标记」机制的可观测证据：这些模型探测得到「可用」，
        // 但因为是你手动禁用的，巡检没有动它们。
        logMain.appendChild(wbEl("div", "wb-api-desc",
          "因手动禁用而被跳过（探测可用但未放开）："
          + (last.protected || []).join("、")));
      }
      logRow.appendChild(logMain);
      card.appendChild(logRow);
    }

    section.appendChild(card);
    host.appendChild(section);
  }

  function injectModelHealth() {
    var host = wbFindSettingsHost();
    if (!host) return;
    if (document.getElementById("settings-model-health")) return;

    function render(data) {
      var host2 = wbFindSettingsHost();
      if (!host2 || document.getElementById("settings-model-health")) return;
      wbRenderHealthSection(host2, data);
    }

    fetch("/api/model-health", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || data.error) return;
        render(data);
      })
      .catch(function () {});
  }

  function injectApiAccess() {
    var host = wbFindSettingsHost();
    if (!host) return;
    if (document.getElementById("settings-api-access")) return;

    function render(data) {
      var host2 = wbFindSettingsHost();
      if (!host2 || document.getElementById("settings-api-access")) return;
      wbRenderApiSection(host2, data);
    }

    if (wbApiCache) { render(wbApiCache); return; }
    fetch("/api/api-keys", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || data.error) return;
        wbApiCache = data;
        render(data);
      })
      .catch(function () {});
  }

  // ---------------------------------------------------------------------
  // 设置页：关于
  // 放在设置页最底部。这是自建容器，用户需要一处能回答「我装的是哪个版本、
  // 账号长什么样、出问题去哪里看」的地方 —— 否则每次都得回去翻 README。
  // 项目地址与项目名由网关下发（可用环境变量覆盖），fork 出去的人不会把使用者
  // 引回上游作者的项目。
  //
  // 版本号来自网关常量，**与 GitHub Release 标签 / 镜像 tag 同号**
  // （曾经是两套号：面板 v1.8.0、发布页 v0.4.4，看的人根本没法判断自己是不是最新）。
  // ---------------------------------------------------------------------
  var wbInfoCache = null;

  function wbRenderAboutSection(host, data) {
    var proj = data.project || {};
    var models = data.models || {};
    var accounts = data.accounts || {};
    var ports = data.ports || {};
    var bySource = data.disabledBySource || {};

    var section = wbEl("section", "min-w-0 space-y-2.5");
    section.id = "settings-about";
    var head = wbEl("div", "px-1");
    var h2 = wbEl("h2", "text-[13px] font-medium leading-5", "关于");
    h2.id = "settings-about-title";
    head.appendChild(h2);
    section.appendChild(head);
    section.setAttribute("aria-labelledby", "settings-about-title");

    var card = wbEl("div", "wb-api-card");

    // ---- 1. 项目名 + 版本 ----
    var nameRow = wbEl("div", "wb-api-row wb-api-row-stack");
    var nameMain = wbEl("div", "wb-api-main");
    var headLine = wbEl("div", "wb-about-head");
    headLine.appendChild(wbEl("span", "wb-about-name", proj.name || "AutoBuddy"));
    var versionBadge = wbEl("span", "wb-api-badge wb-api-badge-ok", "v" + (data.version || "?"));
    versionBadge.title = "与 GitHub 发布标签、镜像 tag 同号";
    headLine.appendChild(versionBadge);
    nameMain.appendChild(headLine);
    nameMain.appendChild(wbEl("div", "wb-api-desc",
      "把 WorkBuddy / CodeBuddy 账号池变成标准 OpenAI 兼容网关：账号自动轮询、"
      + "模型级可用性自愈，容器内自带这个控制台。"));
    var linkRow = wbEl("div", "wb-about-links");
    [
      ["项目主页", proj.url],
      ["更新日志", proj.changelog],
      ["发布版本", proj.releases],
      ["问题反馈", proj.issues],
    ].forEach(function (item) {
      if (!item[1]) return;
      var a = wbEl("a", "wb-about-link", item[0]);
      a.href = item[1];
      a.target = "_blank";
      a.rel = "noreferrer noopener";
      linkRow.appendChild(a);
    });
    nameMain.appendChild(linkRow);
    nameRow.appendChild(nameMain);
    card.appendChild(nameRow);

    // ---- 2. 运行位置 ----
    // 只要「数据目录 + 两个入口地址」。镜像名不在这里重复：升级方式属于文档，
    // 而且版本号已经在上面的徽章里了，再列一次镜像名只会让同一件事出现两遍。
    var runRow = wbEl("div", "wb-api-row wb-api-row-stack");
    var runMain = wbEl("div", "wb-api-main");
    runMain.appendChild(wbEl("div", "wb-api-label", "运行位置"));

    var scheme = window.location.protocol === "https:" ? "https:" : "http:";
    // 注意别用 `host` 这个名字：本函数的参数就叫 host（要往里挂节点的容器），
    // `var` 会把它整个顶掉，函数末尾 host.appendChild 就抛异常、整块面板静默消失。
    var hostName = window.location.hostname || "localhost";
    var consolePort = ports.console || 18090;
    var gatewayPort = ports.gateway || 18091;

    var grid = wbEl("div", "wb-about-grid");
    function addRow(key, value, mono, copyValue) {
      grid.appendChild(wbEl("span", "wb-about-k", key));
      var cell = wbEl("div", "wb-about-v");
      cell.appendChild(wbEl("span", mono ? "wb-api-mono" : null, value));
      if (copyValue) {
        var btn = wbEl("button", "wb-api-btn", "复制");
        btn.onclick = function () { wbCopy(copyValue, "已复制" + key); };
        cell.appendChild(btn);
      }
      grid.appendChild(cell);
    }
    var dir = data.dataDir || "";
    addRow("数据目录", dir || "—", true, dir);
    addRow("控制台", scheme + "//" + hostName + ":" + consolePort, false, null);
    addRow("网关 API", scheme + "//" + hostName + ":" + gatewayPort + "/v1", false, null);
    runMain.appendChild(grid);
    runMain.appendChild(wbEl("div", "wb-api-desc",
      "浏览器打开「控制台」；程序调用填「网关 API」。升级不会动数据目录里的"
      + "账号、密钥与模型策略配置。"));
    runRow.appendChild(runMain);
    card.appendChild(runRow);

    // ---- 3. 当前规模 ----
    var scaleRow = wbEl("div", "wb-api-row");
    var scaleMain = wbEl("div", "wb-api-main");
    scaleMain.appendChild(wbEl("div", "wb-api-label", "当前规模"));
    scaleMain.appendChild(wbEl("div", "wb-api-desc",
      "账号 " + (accounts.total || 0) + " 个（可用 " + (accounts.usable || 0)
      + "，参与调用 " + (accounts.inPool || 0) + "）"
      + " · 模型 " + (models.count || 0) + " 个"
      + " · 禁用组合 " + (models.disabledCombos || 0) + " 项"
      + "（手动 " + (bySource.manual || 0) + " / 巡检 " + (bySource.auto || 0) + "）"));
    scaleRow.appendChild(scaleMain);
    card.appendChild(scaleRow);

    // ---- 4. 免责说明 ----
    var noteRow = wbEl("div", "wb-api-row wb-api-row-stack");
    var noteMain = wbEl("div", "wb-api-main");
    noteMain.appendChild(wbEl("div", "wb-api-label", "说明"));
    var note = wbEl("div", "wb-about-note",
      "非官方项目，与 WorkBuddy / CodeBuddy 官方无任何关联，仅供个人自用；"
      + "使用前请自行确认符合相关服务条款。容器内不含任何官方客户端二进制以外的东西，"
      + "账号凭据始终保存在你自己的数据卷里。");
    noteMain.appendChild(note);
    noteRow.appendChild(noteMain);
    card.appendChild(noteRow);

    section.appendChild(card);
    host.appendChild(section);
  }

  function injectAbout() {
    var host = wbFindSettingsHost();
    if (!host) return;
    if (document.getElementById("settings-about")) return;

    function render(data) {
      var host2 = wbFindSettingsHost();
      if (!host2 || document.getElementById("settings-about")) return;
      wbRenderAboutSection(host2, data);
    }

    if (wbInfoCache) { render(wbInfoCache); return; }
    fetch("/api/gateway-info", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || data.error) return;
        wbInfoCache = data;
        render(data);
      })
      .catch(function () {});
  }

  function wbPinAboutLast() {
    // 「关于」必须是设置页的最后一段。但各面板的插入顺序取决于 fetch 回来的先后，
    // 谁先到谁先 append，光靠在 run() 里排调用顺序保证不了。
    // 因此每次渲染后都把它挪回末尾；已经是最后一个时不做动作，
    // 否则 appendChild 会触发新的 DOM 变更，被 MutationObserver 接住后无限循环。
    var about = document.getElementById("settings-about");
    if (!about) return;
    var parent = about.parentElement;
    if (!parent) return;
    if (parent.lastElementChild !== about) parent.appendChild(about);
  }


  // ---------------- 侧边栏「每日任务」入口与专属视图 ----------------
  function wbFmtTs(ts) {
    if (!ts) return "—";
    try { return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }); } catch (e) { return String(ts); }
  }

  function injectWbDaily() {
    var aside = document.querySelector("aside");
    if (!aside) return;
    var nav = aside.querySelector("nav");
    if (!nav) return;

    var navLink = document.getElementById("wb-nav-wb-daily");
    if (!navLink) {
      navLink = document.createElement("a");
      navLink.id = "wb-nav-wb-daily";
      navLink.href = "#/wb-daily";
      navLink.className = "flex items-center gap-2.5 rounded-lg px-3 py-2.5 text-sm outline-none transition-colors text-muted-foreground hover:bg-foreground/[0.04] hover:text-foreground cursor-pointer";
      navLink.innerHTML = '<svg class="size-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect><line x1="16" y1="2" x2="16" y2="6"></line><line x1="8" y1="2" x2="8" y2="6"></line><line x1="3" y1="10" x2="21" y2="10"></line><path d="M9 16l2 2 4-4"></path></svg><span class="wb-nav-label">每日任务</span>';

      var settingsA = nav.querySelector('a[href="/settings"]') || nav.lastElementChild;
      if (settingsA) {
        nav.insertBefore(navLink, settingsA);
      } else {
        nav.appendChild(navLink);
      }

      navLink.addEventListener("click", function(e) {
        e.preventDefault();
        window.location.hash = "#/wb-daily";
      });

      nav.querySelectorAll("a:not(#wb-nav-wb-daily)").forEach(function(a) {
        a.addEventListener("click", function() {
          if (window.location.hash === "#/wb-daily") {
            window.location.hash = "";
          }
        });
      });
    }
  }

  function wbUpdateWbDailyView() {
    var isDaily = window.location.hash === "#/wb-daily";
    var navLink = document.getElementById("wb-nav-wb-daily");
    var aside = document.querySelector("aside");
    if (!aside) return;

    if (navLink) {
      if (isDaily) {
        navLink.classList.add("bg-foreground/[0.06]", "font-medium", "text-foreground");
        navLink.classList.remove("text-muted-foreground");
        var otherLinks = aside.querySelectorAll("nav a:not(#wb-nav-wb-daily)");
        otherLinks.forEach(function(link) {
          link.classList.remove("bg-foreground/[0.06]", "font-medium", "text-foreground");
          link.classList.add("text-muted-foreground");
        });
      } else {
        navLink.classList.remove("bg-foreground/[0.06]", "font-medium", "text-foreground");
        navLink.classList.add("text-muted-foreground");
      }
    }

    var main = document.querySelector("main");
    if (!main) return;

    var dailyView = document.getElementById("wb-daily-view");
    if (isDaily) {
      if (!dailyView) {
        dailyView = wbCreateWbDailyView();
        main.appendChild(dailyView);
      }
      dailyView.style.display = "block";
      Array.prototype.slice.call(main.children).forEach(function(child) {
        if (child !== dailyView) child.style.display = "none";
      });
      wbLoadWbDailyData();
    } else {
      if (dailyView) dailyView.style.display = "none";
      Array.prototype.slice.call(main.children).forEach(function(child) {
        if (child !== dailyView && child.style.display === "none") {
          child.style.display = "";
        }
      });
    }
  }

  window.addEventListener("hashchange", wbUpdateWbDailyView);

  function wbCreateWbDailyView() {
    var v = document.createElement("div");
    v.id = "wb-daily-view";
    v.style.display = "none";
    v.innerHTML =
      '<div style="max-width:960px;margin:0 auto;padding:24px 20px;display:flex;flex-direction:column;gap:20px">' +
        '<div style="display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border,rgba(120,120,120,.2));padding-bottom:14px">' +
          '<div>' +
            '<h1 style="font-size:20px;font-weight:700;margin:0;color:var(--foreground,#0f172a)">每日任务</h1>' +
            '<p style="font-size:13px;margin:4px 0 0 0;color:var(--muted-foreground,#64748b)">WorkBuddy 成长中心自动化：积分与成长查询、成长任务、互动玩法、开学季活动与自动领奖（内置 WorkBuddy-Daily 脚本，账号池国内版账号只读共用凭据）。</p>' +
          '</div>' +
          '<div id="wb-dl-state" class="wb-api-badge">加载中…</div>' +
        '</div>' +

        '<!-- 执行中：进度条 + 阶段明细（无任务时整块隐藏） -->' +
        '<div id="wb-dl-progress-card" class="wb-api-card" style="display:none">' +
          '<div class="wb-api-row">' +
            '<div class="wb-api-main">' +
              '<div id="wb-dl-progress-label" class="wb-api-label">正在执行</div>' +
              '<div id="wb-dl-progress-step" class="wb-api-desc">准备中…</div>' +
            '</div>' +
            '<div id="wb-dl-progress-pct" class="wb-api-mono" style="font-size:18px;font-weight:600">0%</div>' +
          '</div>' +
          '<div style="width:100%;height:8px;border-radius:4px;background:rgba(120,120,120,.22);overflow:hidden;margin-top:8px">' +
            '<div id="wb-dl-progress-bar" style="width:0%;height:100%;background:#3b82f6;transition:width .4s ease"></div>' +
          '</div>' +
          '<div id="wb-dl-progress-accounts" style="display:flex;flex-wrap:wrap;gap:6px;margin-top:10px"></div>' +
        '</div>' +

        '<div class="wb-api-card">' +
          '<div class="wb-api-row">' +
            '<div class="wb-api-main">' +
              '<div class="wb-api-label">自动签到与调度</div>' +
              '<div class="wb-api-desc">签到的唯一配置入口（原在设置页，已并入本页）。按间隔自动执行一轮；「仅查询」模式只读积分/用量/成长，不产生任何任务状态变更。</div>' +
            '</div>' +
          '</div>' +
          '<div class="wb-api-row wb-api-row-stack">' +
            '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;width:100%">' +
              '<div style="display:flex;align-items:center;gap:8px">' +
                '<input id="wb-dl-enabled" type="checkbox" style="width:16px;height:16px">' +
                '<span class="wb-api-label" style="margin:0">启用定时调度</span>' +
              '</div>' +
              '<div>' +
                '<div class="wb-api-label">执行间隔（小时）</div>' +
                '<input id="wb-dl-interval" type="number" min="1" max="72" class="wb-api-input" style="width:100%;margin-top:4px" value="12">' +
              '</div>' +
              '<div>' +
                '<div class="wb-api-label">执行模式</div>' +
                '<select id="wb-dl-mode" class="wb-api-input" style="width:100%;margin-top:4px">' +
                  '<option value="full">完整任务（签到/玩法/领奖）</option>' +
                  '<option value="query">仅查询（只读）</option>' +
                '</select>' +
              '</div>' +
              '<div style="display:flex;align-items:center;gap:8px">' +
                '<input id="wb-dl-checkin" type="checkbox" style="width:16px;height:16px">' +
                '<span class="wb-api-label" style="margin:0" title="启动时立即核验服务端状态，未签到账号会自动补签">启用自动签到</span>' +
              '</div>' +
              '<div>' +
                '<div class="wb-api-label">保活阈值（天）</div>' +
                '<input id="wb-dl-keep" type="number" min="0" max="90" class="wb-api-input" style="width:100%;margin-top:4px" value="30" title="0 表示每天无条件刷新">' +
              '</div>' +
              '<div>' +
                '<div class="wb-api-label">惰性刷新（小时）</div>' +
                '<input id="wb-dl-lazy" type="number" min="1" max="72" class="wb-api-input" style="width:100%;margin-top:4px" value="6">' +
              '</div>' +
            '</div>' +
          '</div>' +
          '<div class="wb-api-row">' +
            '<div class="wb-api-main">' +
              '<div class="wb-api-desc">上次执行：<span id="wb-dl-lastrun" class="wb-api-mono">—</span>　下次到期：<span id="wb-dl-nextdue" class="wb-api-mono">—</span></div>' +
            '</div>' +
            '<div style="display:flex;gap:8px">' +
              '<button id="wb-dl-abort" class="wb-api-btn wb-api-btn-danger" style="display:none">中止</button>' +
              '<button id="wb-dl-save" class="wb-api-btn">保存配置</button>' +
              '<button id="wb-dl-run" class="wb-api-btn wb-api-btn-primary">立即执行</button>' +
            '</div>' +
          '</div>' +
          '<!-- 账号签到与凭据：一账号一行，同时显示今日签到状态与凭据状态（原「参与账号」卡片已并入此处，不再重复渲染） -->' +
          '<div class="wb-api-row wb-api-row-stack" style="border-bottom:0;padding-top:14px">' +
            '<div style="display:flex;align-items:center;justify-content:space-between;width:100%;margin-bottom:6px">' +
              '<span class="wb-api-label">账号签到与凭据（最近 30 天）</span>' +
              '<button id="wb-dl-checkin-all-btn" class="wb-api-btn" style="font-size:11px">全部立即签到</button>' +
            '</div>' +
            '<div class="wb-api-desc" style="margin-bottom:6px">自动关联账号池里的国内版（cn）账号，新增账号无需在此登记；刷新令牌只读共用（实测续期后旧令牌仍有效，无烧号风险）。</div>' +
            '<div id="wb-dl-checkin-logs-box" style="width:100%;max-height:280px;overflow-y:auto;display:flex;flex-direction:column;gap:6px">' +
              '<div class="wb-api-desc">正在加载账号签到与凭据…</div>' +
            '</div>' +
          '</div>' +
        '</div>' +

        '<!-- 任务清单：每日任务提供了哪些任务 -->' +
        '<div class="wb-api-card">' +
          '<div class="wb-api-row">' +
            '<div class="wb-api-main">' +
              '<div class="wb-api-label">任务清单</div>' +
              '<div class="wb-api-desc" id="wb-dl-catalog-sum">正在加载任务列表…</div>' +
            '</div>' +
          '</div>' +
          '<div id="wb-dl-catalog" class="wb-api-row wb-api-row-stack" style="flex-direction:column"></div>' +
        '</div>' +

        '<!-- 任务记录：任务维度的台账（SQLite 持久化，逐任务项聚合） -->' +
        '<div class="wb-api-card">' +
          '<div class="wb-api-row">' +
            '<div class="wb-api-main">' +
              '<div class="wb-api-label">任务记录</div>' +
              '<div class="wb-api-desc" id="wb-dl-tasks-sum">每个任务项最近一次执行结果与成功次数（内置 SQLite 持久化）</div>' +
            '</div>' +
            '<button id="wb-dl-refresh-tasks" class="wb-api-btn">刷新</button>' +
          '</div>' +
          '<div id="wb-dl-tasks" class="wb-api-row wb-api-row-stack" style="flex-direction:column"></div>' +
        '</div>' +

        '<!-- 执行记录（SQLite 持久化，保留最近 200 轮） -->' +
        '<div class="wb-api-card">' +
          '<div class="wb-api-row">' +
            '<div class="wb-api-main">' +
              '<div class="wb-api-label">执行记录</div>' +
              '<div class="wb-api-desc" id="wb-dl-runs-sum">最近执行轮次（点击任一轮查看逐账号明细）</div>' +
            '</div>' +
            '<button id="wb-dl-refresh-runs" class="wb-api-btn">刷新</button>' +
          '</div>' +
          '<div id="wb-dl-runs" class="wb-api-row wb-api-row-stack" style="flex-direction:column"></div>' +
          '<div id="wb-dl-run-detail" style="display:none"></div>' +
          '<div id="wb-dl-log-wrap" style="display:none">' +
            '<div class="wb-api-label" style="margin-top:10px">实时日志</div>' +
            '<div id="wb-dl-log" class="wb-api-code" style="max-height:220px;overflow-y:auto;white-space:pre-wrap"></div>' +
          '</div>' +
        '</div>' +
      '</div>';

    v.querySelector("#wb-dl-save").addEventListener("click", wbDailySaveConfig);
    v.querySelector("#wb-dl-run").addEventListener("click", wbDailyRunJob);
    v.querySelector("#wb-dl-abort").addEventListener("click", wbDailyAbort);
    v.querySelector("#wb-dl-refresh-runs").addEventListener("click", function() {
      wbLoadWbDailyRuns();
      wbToast("执行记录已刷新", "ok");
    });
    var refreshTasksBtn = v.querySelector("#wb-dl-refresh-tasks");
    if (refreshTasksBtn) {
      refreshTasksBtn.addEventListener("click", function() {
        wbLoadWbDailyTasks();
        wbToast("任务记录已刷新", "ok");
      });
    }
    var checkinAllBtn = v.querySelector("#wb-dl-checkin-all-btn");
    if (checkinAllBtn) {
      checkinAllBtn.addEventListener("click", function() {
        if (checkinAllBtn.disabled) return;
        checkinAllBtn.disabled = true;
        checkinAllBtn.textContent = "签到中…";
        fetch("/api/checkin/all", { method: "POST", headers: { "Content-Type": "application/json" } })
          .then(function(r) { return r.json().catch(function() { return {}; }); })
          .then(function(w) {
            if (w && w.status === "skipped" && w.reason === "already_running") {
              wbToast("签到任务正在进行，请稍后再试", "warn");
              return;
            }
            var accs = (w && w.accounts) || [];
            var s = accs.filter(function(e) { return e.result === "success"; }).length;
            var b = accs.filter(function(e) { return e.result === "already"; }).length;
            var o = accs.filter(function(e) { return e.result === "error"; }).length;
            var tone = o > 0 ? "err" : "ok";
            wbToast("签到完成：成功 " + s + "，已签 " + b + (o > 0 ? "，失败 " + o : ""), tone);
            wbLoadWbDailyCheckinLogs();
          })
          .catch(function(e) { wbToast("签到请求失败: " + e, "err"); })
          .then(function() {
            checkinAllBtn.disabled = false;
            checkinAllBtn.textContent = "全部立即签到";
          });
      });
    }
    return v;
  }

  function wbLoadWbDailyData() {
    fetch("/api/wb-daily/config").then(function(r) { return r.ok ? r.json() : {}; }).then(function(d) {
      var cfg = (d && d.config) || {};
      var el;
      el = document.getElementById("wb-dl-enabled"); if (el) el.checked = cfg.enabled !== false;
      el = document.getElementById("wb-dl-interval"); if (el) el.value = cfg.interval_hours != null ? cfg.interval_hours : 12;
      el = document.getElementById("wb-dl-mode"); if (el) el.value = cfg.run_mode === "query" ? "query" : "full";
      el = document.getElementById("wb-dl-checkin"); if (el) el.checked = cfg.checkin_enabled !== false;
      el = document.getElementById("wb-dl-keep"); if (el) el.value = cfg.keepalive_days != null ? cfg.keepalive_days : 30;
      el = document.getElementById("wb-dl-lazy"); if (el) el.value = cfg.lazy_refresh_hours != null ? cfg.lazy_refresh_hours : 6;
    }).catch(function() {});
    fetch("/api/wb-daily/health").then(function(r) { return r.ok ? r.json() : {}; }).then(function(d) {
      var badge = document.getElementById("wb-dl-state");
      var runBtn = document.getElementById("wb-dl-run");
      var abortBtn = document.getElementById("wb-dl-abort");
      if (badge) {
        if (d && d.running) {
          badge.textContent = "执行中";
          badge.style.background = "rgba(59,130,246,.14)"; badge.style.color = "#1d4ed8";
        } else {
          badge.textContent = d && d.enabled === false ? "调度已停用" : "待机";
          badge.style.background = "rgba(16,185,129,.14)"; badge.style.color = "#047857";
        }
      }
      if (runBtn) { runBtn.disabled = !!(d && d.running); if (!(d && d.running)) runBtn.textContent = "立即执行"; }
      if (abortBtn) abortBtn.style.display = d && d.running ? "" : "none";
      var lr = document.getElementById("wb-dl-lastrun"); if (lr) lr.textContent = wbFmtTs(d && d.last_run_at);
      var nd = document.getElementById("wb-dl-nextdue"); if (nd) nd.textContent = d && d.enabled ? wbFmtTs(d.next_due_at) : "—（调度停用）";
      if (d && d.running && d.running_job_id) wbDailyPoll(d.running_job_id);
      else wbRenderDailyProgress(null);
    }).catch(function() {});
    wbLoadWbDailyCatalog();
    wbLoadWbDailyCheckinLogs();
    wbLoadWbDailyTasks();
    wbLoadWbDailyJobs();
  }

  /* 任务记录：任务维度的台账。数据来自 SQLite（/api/wb-daily/task-summary），
     一任务一行，显示最近结果、最近账号与成功次数。与「执行记录」互补：
     那边是按轮次看，这边是按任务项看。 */
  function wbLoadWbDailyTasks() {
    var box = document.getElementById("wb-dl-tasks");
    if (!box) return;
    fetch("/api/wb-daily/task-summary").then(function(r) { return r.ok ? r.json() : {}; }).then(function(d) {
      var items = (d && d.items) || [];
      var sum = document.getElementById("wb-dl-tasks-sum");
      if (!items.length) {
        if (sum) sum.textContent = "还没有任务记录（执行一轮每日任务后自动累积，内置 SQLite 持久化）";
        box.innerHTML = '<div class="wb-api-desc" style="padding:8px 0;text-align:center">暂无任务记录</div>';
        return;
      }
      if (sum) sum.textContent = "共 " + items.length + " 个任务项有记录（内置 SQLite 持久化，随执行轮次累积）";
      var html = "";
      items.forEach(function(t) {
        var tone = t.last_result === "success" ? "#047857"
                 : t.last_result === "already" ? "#b45309"
                 : t.last_result === "failed" ? "#b91c1c" : "#94a3b8";
        var label = t.last_result === "success" ? "成功"
                  : t.last_result === "already" ? "已做过"
                  : t.last_result === "failed" ? "失败" : "记录";
        var extra = [];
        if (t.streak_days != null) extra.push("连签 " + t.streak_days + " 天");
        html += '<div class="wb-api-row" style="padding:6px 0">' +
          '<div class="wb-api-main" style="min-width:0">' +
            '<span class="wb-api-label" style="margin:0">' + (t.task_name || t.task_key) + '</span>' +
            '<span class="wb-api-desc" style="font-size:11px;margin-left:8px">' +
              (t.last_at ? wbFmtTs(t.last_at) : "—") +
              (t.last_account ? " · " + t.last_account : "") +
              (extra.length ? " · " + extra.join(" · ") : "") +
            '</span>' +
          '</div>' +
          '<div style="display:flex;align-items:center;gap:8px;flex:0 0 auto">' +
            '<span class="wb-api-desc" style="font-size:11px">成功 ' + (t.success || 0) + '/' + (t.total || 0) + '</span>' +
            '<span class="wb-api-badge" style="color:' + tone + ';background:transparent;border:1px solid ' + tone + '33">' + label + '</span>' +
          '</div>' +
        '</div>';
      });
      box.innerHTML = html;
    }).catch(function() {
      box.innerHTML = '<div class="wb-api-desc">任务记录加载失败</div>';
    });
  }

  /* 账号签到与凭据合并展示：一账号一行 ——
     左侧账号名（唯一来源：/api/wb-daily/accounts，与账号池同一出处），
     右侧两个状态：今日签到（/api/checkin/status，实时）+ 凭据（has_rt）。
     原「参与账号」卡片与「签到日志」两张卡重复列同一批账号，此处合并为一张。 */
  function wbLoadWbDailyCheckinLogs() {
    var box = document.getElementById("wb-dl-checkin-logs-box");
    if (!box) return;

    function esc(s) {
      return String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }

    function fmtTime(ts) {
      if (!ts) return "";
      try {
        return new Date(ts).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
      } catch (e) { return String(ts); }
    }

    // 三个接口改成「各自到齐就渲染」：账号列表与签到日志走本地 SQLite（0.03s），
    // 签到状态要打上游（1.5~2.3s）。原先用 Promise.all 必须三个都到才画，
    // 于是飞机旅行图标秒出、签到徽章要等两秒多（用户 2026-09-26 反馈）。
    // 现在先用空签到状态渲染一版（徽章显示「查询中…」），状态到了再局部补写。
    var _acRes = null, _stRes = null, _lgRes = null, _drawn = false;

    function _tryDraw() {
      // 账号列表与签到日志到齐即可先画；签到状态可选，到了再重画补上。
      if (_drawn || !_acRes || !_lgRes) return;
      renderCheckinRows(_acRes, _stRes, _lgRes);
      _drawn = true;
    }

    fetch("/api/wb-daily/accounts").then(function(r) { return r.ok ? r.json() : {}; }).catch(function() { return {}; })
      .then(function(d) { _acRes = d; _tryDraw(); });
    fetch("/api/checkin/logs").then(function(r) { return r.ok ? r.json() : {}; }).catch(function() { return {}; })
      .then(function(d) { _lgRes = d; _tryDraw(); });
    fetch("/api/checkin/status").then(function(r) { return r.ok ? r.json() : {}; }).catch(function() { return {}; })
      .then(function(d) { _stRes = d; _drawn = false; _tryDraw(); });

    function renderCheckinRows(res0, res1, res2) {
      var accounts = (res0 && res0.accounts) || [];
      var statusList = (res1 && res1.accounts) || [];
      var logs = (res2 && res2.logs) || [];

      if (!accounts.length) {
        box.innerHTML = '<div class="wb-api-desc" style="padding:8px 0;text-align:center">暂无参与账号（账号池无国内版账号）</div>';
        return;
      }

      // 账号标识归一：账号池用 user/name，签到接口用 email/accountId。
      // 建一个多键索引，任一边的标识都能命中同一条记录。
      var statusByKey = {};
      statusList.forEach(function(s) {
        if (s.accountId) statusByKey[String(s.accountId)] = s;
        if (s.email) statusByKey[String(s.email)] = s;
      });

      // 每个账号取最近一条签到流水（logs 按时间升序返回）
      var lastLogByKey = {};
      logs.forEach(function(l) {
        if (l.accountId) lastLogByKey[String(l.accountId)] = l;
        if (l.email) lastLogByKey[String(l.email)] = l;
      });

      var html = "";
      accounts.forEach(function(a) {
        var displayName = a.name && a.name !== a.user ? (a.name + " (" + a.user + ")") : (a.user || a.name || "?");
        var st = statusByKey[String(a.id)] || statusByKey[String(a.user)] || statusByKey[String(a.name)] || null;
        var lg = lastLogByKey[String(a.id)] || lastLogByKey[String(a.user)] || lastLogByKey[String(a.name)] || null;

        // 今日签到状态
        var checkinText, checkinTone, checkinTitle = "";
        if (st && st.ok) {
          checkinText = st.todayCheckedIn ? "今日已签到" : "今日未签到";
          checkinTone = st.todayCheckedIn ? "#047857" : "#b45309";
          var exp = {};
          try { exp = (st.raw && st.raw.raw) || st.raw || {}; } catch (e) { exp = {}; }
          if (exp.streak_days != null) checkinTitle = "连签 " + exp.streak_days + " 天";
        } else if (st && st.statusUnsupported) {
          checkinText = "无签到接口";
          checkinTone = "#94a3b8";
          checkinTitle = st.error || "";
        } else if (lg) {
          var lt = lg.result === "success" ? "签到成功" : (lg.result === "already" ? "已签到" : "签到失败");
          checkinText = lt;
          checkinTone = lg.result === "success" ? "#047857" : (lg.result === "already" ? "#b45309" : "#b91c1c");
          if (lg.ts) checkinTitle = fmtTime(lg.ts);
        } else if (!_stRes) {
          // 签到状态还在路上（上游要 1.5~2.3s）：先给出占位，别让徽章看起来像坏了
          checkinText = "查询中…";
          checkinTone = "#94a3b8";
        } else {
          checkinText = "暂无记录";
          checkinTone = "#94a3b8";
        }

        // 凭据状态
        var credText = a.has_rt ? "凭据就绪" : "缺刷新令牌";
        var credTone = a.has_rt ? "#047857" : "#b91c1c";

        var lastTime = lg && lg.ts ? fmtTime(lg.ts) : "";

        html += '<div class="wb-api-row" style="padding:6px 0">' +
          '<div class="wb-api-main" style="min-width:0">' +
            '<span class="wb-api-mono" title="' + esc(displayName) + '">' + esc(displayName) + '</span>' +
            (lastTime ? '<span class="wb-api-desc" style="font-size:11px;margin-left:8px">最近 ' + esc(lastTime) + '</span>' : '') +
          '</div>' +
          '<div style="display:flex;align-items:center;gap:6px;flex:0 0 auto">' +
            '<span class="wb-api-badge" style="color:' + checkinTone + ';background:transparent;border:1px solid ' + checkinTone + '33" title="' + esc(checkinTitle) + '">' + esc(checkinText) + '</span>' +
            '<span class="wb-api-badge" style="color:' + credTone + ';background:transparent;border:1px solid ' + credTone + '33">' + esc(credText) + '</span>' +
          '</div>' +
        '</div>';
      });
      box.innerHTML = html;
    }
  }

  function wbLoadWbDailyJobs() {
    wbLoadWbDailyRuns();
  }

  /* 任务清单：每日任务提供了哪些任务、各自是自动还是需人工 */
  function wbLoadWbDailyCatalog() {
    fetch("/api/wb-daily/catalog").then(function(r) { return r.ok ? r.json() : {}; }).then(function(d) {
      var box = document.getElementById("wb-dl-catalog");
      var sum = document.getElementById("wb-dl-catalog-sum");
      if (!box) return;
      var groups = (d && d.groups) || [];
      if (sum && d && d.total) {
        sum.textContent = "共 " + d.total + " 项任务：" + d.auto + " 项全自动、" + d.manual + " 项需人工（下方标灰）；需人工项不影响其余任务执行。";
      }
      if (!groups.length) { box.innerHTML = '<div class="wb-api-desc">暂无任务清单</div>'; return; }
      var html = "";
      groups.forEach(function(g) {
        var items = g.items || [];
        var autoN = items.filter(function(i) { return i.auto; }).length;
        html += '<div style="width:100%;margin-top:10px">' +
          '<div class="wb-api-label">' + g.group + '（' + autoN + '/' + items.length + ' 自动）</div>' +
          '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:6px;margin-top:6px">';
        items.forEach(function(i) {
          var dim = i.auto ? "" : ";opacity:.62";
          var badge = i.auto
            ? '<span style="color:#047857;font-size:11px">自动</span>'
            : '<span style="color:#b45309;font-size:11px">人工</span>';
          html += '<div style="display:flex;align-items:center;gap:6px;min-width:0' + dim + '" title="' + (i.note || i.name || "") + '">' +
            '<span style="font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + (i.name || i.key) + '</span>' +
            badge + '</div>';
        });
        html += '</div></div>';
      });
      box.innerHTML = html;
    }).catch(function() {});
  }

  /* 执行记录（SQLite 持久化）+ 逐账号明细 */
  function wbLoadWbDailyRuns() {
    fetch("/api/wb-daily/runs").then(function(r) { return r.ok ? r.json() : {}; }).then(function(d) {
      var box = document.getElementById("wb-dl-runs");
      var sum = document.getElementById("wb-dl-runs-sum");
      if (!box) return;
      var runs = (d && d.runs) || [];
      if (sum) sum.textContent = "最近执行轮次（最多保留 " + ((d && d.retain) || 200) + " 轮，点击任意一轮查看逐账号明细）";
      if (!runs.length) { box.innerHTML = '<div class="wb-api-desc">暂无执行记录</div>'; return; }
      var html = "";
      runs.forEach(function(j) {
        var tone = j.status === "done" ? "#047857" : (j.status === "running" ? "#1d4ed8" : "#b45309");
        var label = j.status === "done" ? "✅ 完成" : j.status === "running" ? "⏳ 执行中"
                  : j.status === "aborted" ? "⛔ 已中止" : "❌ 失败";
        html += '<div class="wb-api-row wb-dl-run-row" data-job="' + j.job_id + '" style="padding:7px 0;cursor:pointer">' +
          '<div class="wb-api-main">' +
            '<span class="wb-api-mono">' + String(j.job_id) + '</span>' +
            '<span class="wb-api-desc" style="margin-left:8px">' + wbFmtTs(j.started_at) +
            ' · ' + (j.mode === "scheduled" ? "自动" : "手动") +
            (j.accounts ? " · " + j.accounts + " 个账号" : "") + '</span>' +
          '</div>' +
          '<span style="font-size:12px;font-weight:600;color:' + tone + '">' + label + '</span>' +
        '</div>';
      });
      box.innerHTML = html;
      box.querySelectorAll(".wb-dl-run-row").forEach(function(row) {
        row.addEventListener("click", function() {
          wbLoadWbDailyRunDetail(row.getAttribute("data-job"));
        });
      });
    }).catch(function() {});
  }

  function wbLoadWbDailyRunDetail(jobId) {
    var wrap = document.getElementById("wb-dl-run-detail");
    if (!wrap) return;
    wrap.style.display = "";
    wrap.innerHTML = '<div class="wb-api-desc" style="margin-top:10px">正在加载 ' + jobId + ' 的逐账号明细…</div>';
    fetch("/api/wb-daily/run-accounts/" + jobId)
      .then(function(r) { return r.ok ? r.json() : {}; })
      .then(function(d) {
        var rows = (d && d.accounts) || [];
        var html = '<div class="wb-api-label" style="margin-top:12px">逐账号明细：' + jobId + '</div>';
        if (!rows.length) {
          html += '<div class="wb-api-desc">本轮没有账号级明细（可能是旧记录或任务未跑完）</div>';
          wrap.innerHTML = html;
          return;
        }
        html += '<div style="width:100%;overflow-x:auto;margin-top:6px"><table style="width:100%;font-size:12px;border-collapse:collapse">' +
          '<thead><tr style="text-align:left;color:var(--muted-foreground,#64748b)">' +
          '<th style="padding:4px 6px">账号</th><th style="padding:4px 6px">完成</th>' +
          '<th style="padding:4px 6px">等级</th><th style="padding:4px 6px">连签</th>' +
          '<th style="padding:4px 6px">能量</th><th style="padding:4px 6px">积分</th></tr></thead><tbody>';
        rows.forEach(function(a) {
          html += '<tr style="border-top:1px solid var(--border,rgba(120,120,120,.15))">' +
            '<td style="padding:5px 6px" class="wb-api-mono">' + (a.account || "?") + '</td>' +
            '<td style="padding:5px 6px">' + (a.done || 0) + '/' + (a.total || 0) + '</td>' +
            '<td style="padding:5px 6px">' + (a.level || "—") + '</td>' +
            '<td style="padding:5px 6px">' + (a.streak || "—") + '</td>' +
            '<td style="padding:5px 6px">' + (a.energy || "—") + '</td>' +
            '<td style="padding:5px 6px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + ((a.credits || "").replace(/"/g, "&quot;")) + '">' + (a.credits || "—") + '</td></tr>';
          if (a.actions && a.actions.length) {
            html += '<tr><td colspan="6" style="padding:2px 6px 4px 6px;color:var(--foreground,#0f172a);font-size:11px;background:rgba(59,130,246,0.04)">已执行动作（' + a.actions.length + ' 项）：' + a.actions.join(" · ") + '</td></tr>';
          }
          if (a.rest && a.rest.length) {
            html += '<tr><td colspan="6" style="padding:0 6px 6px 6px;color:var(--muted-foreground,#64748b);font-size:11px">剩余 ' + a.rest.length + ' 项：' + a.rest.join("、") + '</td></tr>';
          }
        });
        html += '</tbody></table></div>';
        wrap.innerHTML = html;
      })
      .catch(function() {
        wrap.innerHTML = '<div class="wb-api-desc" style="margin-top:10px">明细加载失败</div>';
      });
  }

  /* 执行中进度：进度条 + 阶段文字 + 逐账号状态点。
     与右上角胶囊同一套原则：内容无变化不碰 DOM：只有值与上次不同才写。*/
  var __wbDlProgress = { key: "" };

  function wbRenderDailyProgress(d) {
    var card = document.getElementById("wb-dl-progress-card");
    if (!card) return;
    if (!d || (d.status !== "running" && d.status !== "pending")) {
      card.style.display = "none";
      __wbDlProgress.key = "";
      return;
    }
    card.style.display = "";
    var pct = d.progress || "0%";
    var steps = d.steps || [];
    var stepText = steps.length ? steps[steps.length - 1] : "正在执行…";
    var logs = d.log || [];
    var lastLog = logs.length ? logs[logs.length - 1] : "";
    var key = pct + "|" + stepText + "|" + lastLog;
    if (key === __wbDlProgress.key) return;
    __wbDlProgress.key = key;

    var pctEl = document.getElementById("wb-dl-progress-pct");
    var barEl = document.getElementById("wb-dl-progress-bar");
    var stepEl = document.getElementById("wb-dl-progress-step");
    var accEl = document.getElementById("wb-dl-progress-accounts");
    if (pctEl) pctEl.textContent = pct;
    if (barEl) barEl.style.width = pct;
    if (stepEl) stepEl.textContent = lastLog || stepText;
    if (accEl) {
      var total = d.accounts_total || 0, done = d.accounts_done || 0;
      var html = "";
      for (var i = 0; i < total; i++) {
        var color = i < done ? "#10b981" : (i === done ? "#3b82f6" : "rgba(120,120,120,.35)");
        html += '<span title="账号' + (i + 1) + (i < done ? " 已完成" : (i === done ? " 执行中" : " 等待中")) + '" style="width:10px;height:10px;border-radius:50%;background:' + color + '"></span>';
      }
      if (total) html += '<span class="wb-api-desc" style="margin-left:6px">' + done + '/' + total + ' 个账号完成</span>';
      accEl.innerHTML = html;
    }
  }

  function wbDailySaveConfig() {
    // 参与账号自动关联账号池，不再有手填入口。
    var payload = {
      enabled: document.getElementById("wb-dl-enabled").checked,
      interval_hours: parseFloat(document.getElementById("wb-dl-interval").value) || 12,
      run_mode: document.getElementById("wb-dl-mode").value,
      checkin_enabled: document.getElementById("wb-dl-checkin").checked,
      keepalive_days: parseInt(document.getElementById("wb-dl-keep").value, 10) || 0,
      lazy_refresh_hours: parseInt(document.getElementById("wb-dl-lazy").value, 10) || 6
    };
    fetch("/api/wb-daily/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
      .then(function(r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function() { wbToast("配置已保存", "ok"); })
      .catch(function(e) { wbToast("保存失败: " + (e && e.message || e), "err"); });
  }

  function wbDailyRunJob() {
    var btn = document.getElementById("wb-dl-run");
    if (btn) btn.disabled = true;
    fetch("/api/wb-daily/run", { method: "POST" }).then(function(r) { return r.json(); }).then(function(d) {
      if (!d || !d.job_id) throw new Error((d && d.detail) || "启动失败");
      wbToast("每日任务已启动", "ok");
      wbDailyPoll(d.job_id);
    }).catch(function(e) {
      wbToast("启动失败: " + (e && e.message || e), "err");
      if (btn) btn.disabled = false;
    });
  }

  function wbDailyPoll(jobId) {
    if (window.__wbDailyPoll) clearInterval(window.__wbDailyPoll);
    var logBox = document.getElementById("wb-dl-log");
    if (logBox) logBox.style.display = "";
    window.__wbDailyPoll = setInterval(function() {
      fetch("/api/wb-daily/status/" + jobId).then(function(r) { return r.ok ? r.json() : null; }).then(function(d) {
        if (!d) return;
        if (logBox) {
          logBox.textContent = (d.log || []).join("\n");
          logBox.scrollTop = logBox.scrollHeight;
        }
        if (d.status !== "running") {
          clearInterval(window.__wbDailyPoll);
          window.__wbDailyPoll = null;
          wbToast(d.status === "done" ? "每日任务执行完成" : "每日任务结束（" + d.status + "）", d.status === "done" ? "ok" : "warn");
          wbLoadWbDailyData();
        }
      }).catch(function() {});
    }, 2000);
  }

  function wbDailyAbort() {
    fetch("/api/wb-daily/health").then(function(r) { return r.ok ? r.json() : {}; }).then(function(d) {
      var id = d && d.running_job_id;
      if (!id) { wbToast("当前没有执行中的任务", "info"); return; }
      fetch("/api/wb-daily/abort/" + id, { method: "DELETE" }).then(function() {
        wbToast("已发送中止请求", "info");
      }).catch(function() { wbToast("中止请求失败", "err"); });
    }).catch(function() { wbToast("中止失败", "err"); });
  }

  // 「无 Buddy」旅行徽章改造：官方在账号卡片右上角输出的是文字徽章「无 Buddy」，
  // 视觉重、信息重复。这里把它换成虚线描边的小飞机图标，悬浮时给出完整说明；
  // 账号领养 Buddy 后官方文案变成「未旅行 / 旅行中」等，本函数不再命中，
  // 徽章自然恢复官方原样 —— 无需任何持久化。
  // Buddy 旅行三态徽章：统一用同一枚官方「起飞飞机」图标（plane-takeoff），
  // 仅以「线型 + 颜色」区分状态，保证领养前后图标完全一致：
  //   无 Buddy = 虚线灰   未旅行 = 实线中性   旅行中 = 实线主题色
  // 官方文案落在徽章文本里，这里只替换图标与配色，不改动官方状态语义。
  var WB_PLANE_SVG = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="2" y1="22" x2="22" y2="22"></line><path d="M6.36 17.4 4 17l-2-4 1.1-.55a2 2 0 0 1 1.8 0l.17.1a2 2 0 0 0 1.8 0L8 12 5 6l.9-.45a2 2 0 0 1 2.09.2l4.02 3a2 2 0 0 0 2.1.2l4.19-2.06a2.41 2.41 0 0 1 1.73-.17L21 7a1.4 1.4 0 0 1 .87 1.99l-.38.76c-.23.46-.6.84-1.07 1.08L7.58 17.2a2 2 0 0 1-1.22.18Z"></path></svg>';
  function wbBuddyBadgeKind(text) {
    var t = (text || "").trim();
    if (t === "无 Buddy") return "nobuddy";
    if (t === "未旅行") return "idle";
    if (t.indexOf("旅行中") === 0 || t.indexOf("剩余") === 0 || t.indexOf("即将到达") === 0) return "traveling";
    return null;
  }
  function wbTransformNoBuddyBadges() {
    document.querySelectorAll("span, div").forEach(function(el) {
      if (el.classList.contains("wb-buddy-badge") || el.classList.contains("wb-nobuddy-badge")) return;
      if (el.childElementCount !== 0) return;
      var kind = wbBuddyBadgeKind(el.textContent);
      if (!kind) return;
      var cls = kind === "nobuddy" ? "wb-buddy-nobuddy"
              : kind === "traveling" ? "wb-buddy-traveling" : "wb-buddy-idle";
      el.classList.add("wb-buddy-badge", "wb-" + (kind === "nobuddy" ? "nobuddy-badge" : kind === "traveling" ? "traveling-badge" : "idle-badge"));
      if (kind === "nobuddy") el.classList.add("wb-buddy-nobuddy");
      if (kind === "traveling") el.classList.add("wb-buddy-traveling");
      if (kind === "idle") el.classList.add("wb-buddy-idle");
      el.title = kind === "nobuddy"
        ? "无 Buddy：还没有领养宠物伙伴。去官方端完成一次对话并领养 Buddy 后即可自动旅行"
        : (kind === "traveling" ? "Buddy 旅行中：伙伴正在出门旅行" : "未旅行：已领养 Buddy，当前在家待命");
      el.innerHTML = WB_PLANE_SVG;
      el.classList.add(cls);
    });
  }

  function run() {
    sanitizeSidebarBrand();
    wbCheckAuth();
    initCollapse();
    sanitizeMacUI();
    wbTransformNoBuddyBadges();
    enforceTitle();
    injectAccountModels();
    injectAccountPool();
    wbPinAccountBlocks();
    injectApiAccess();
    injectModelHealth();
    injectProfile();
    injectAbout();
    wbPinAboutLast();
    injectWbDaily();
  }

  var __wbRunTimer = null;
  function wbDebouncedRun() {
    if (__wbRunTimer) return;
    __wbRunTimer = requestAnimationFrame(function() {
      __wbRunTimer = null;
      run();
    });
  }

  const observer = new MutationObserver(() => wbDebouncedRun());
  observer.observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("DOMContentLoaded", function() {
    run();
    wbUpdateWbDailyView();
  });
})();
</script>
"""

# 前端 UI 文案的**等长**改写表（只在 JS 资源上应用，见 clean_mac_content 的 is_js）。
# 都是「官方 UI 的措辞与本容器的语义不符」这类适配，不是删功能：
#   按项目 -> 按账号              ：网关没有「项目」概念，只有账号归因
#   消耗最高的会话 -> 消耗最高的调用：每条网关请求就是一次独立调用
#
# 账号页副标题（原「统一管理 WorkBuddy、CodeBuddy IDE 与 CodeBuddy CLI 账号…」）
# **不在这里**。它在 v0.3.14 已改由二进制补丁在源头修掉——留在响应层会是一条永远
# 命不中的死规则。选型原则：源头能改的结构性文案优先放二进制补丁，因为
# `_replace_padded` 要求锚点**恰好命中 1 次**，失配会让 CI 直接失败；而这里的
# `content.replace` 命不中时**静默无操作**（v0.3.10 那类事故的同一形状）。
TEXT_REPLACEMENTS = [
    ("按项目", "按账号"),
    ("消耗最高的会话", "消耗最高的调用"),
    ("按本地聚合 Token 从高到低排列。", "按单次调用 Token 从高到低排列。"),
]

# 已告警过的「未命中」串，避免每次请求都刷屏。
_MISS_WARNED: set = set()


def clean_mac_content(content: bytes, is_js: bool = False) -> bytes:
    # 兜底网：二进制补丁已把 `const X="http://127.0.0.1:57890";` 换成本页 origin，
    # 所以当前上游产物里已**不含**任何 `:57890`（实测 HTML / JS 均 0 处），这 4 条命中数
    # 正常就是 0。保留是因为补丁只认那一个 `const` 形态——上游若在别处新写一个硬编码
    # 后端地址，这里仍能把端口掰到 18090。**命中 0 属预期，不是失效。**
    # 旧品牌名兜底清除。
    #
    # ⚠️ 这里的 `content` 是 HTML/JS 响应体，不是二进制，所以**不要求等长** ——
    # 等长约束只适用于 patch/patch_binary.py 打二进制的情形（详见二进制补丁规程）。
    #
    # 以下四条的定位与边界：
    #   · `autobuddy.app` → `autobuddy`：官方文案里的 macOS 应用名，
    #     容器里没有桌面应用，去掉 .app 后缀纯为了不让用户去找一个不存在的东西。
    #   · 后三条把展示用的品牌名统一成 AutoBuddy，覆盖带引号、尖括号（JSX 文本节点）
    #     与裸串三种形态；顺序必须是「先具体后笼统」，否则后面的规则会先吃掉前面的。
    #
    # ⚠️ **不要**把 `/usr/lib/node_modules/autobuddy` 或 `autobuddy serve`
    # 这类标识符改成 AutoBuddy：那是官方 npm 包名与可执行文件名，改了容器直接起不来。
    # 展示名与标识符是两码事，详见 entrypoint.sh 第 1 步的注释。
    replacements = [
        (b"http://[IP]:57890", b""),
        (b"http://127.0.0.1:57890", b""),
        (b"http://localhost:57890", b""),
        (b":57890", b":18090"),
        (b"/icon-transparent.png", b"/icon.png"),
        (b"\xe5\x9c\xa8 Finder \xe4\xb8\xad\xe6\x98\xbe\xe7\xa4\xba", b"\xe5\x9c\xa8\xe6\x96\x87\xe4\xbb\xb6\xe7\xae\xa1\xe7\x90\x86\xe5\x99\xa8\xe4\xb8\xad\xe6\x98\xbe\xe7\xa4\xba"),
        (b"workbuddy-switch.app", b"autobuddy"),
        # JSX 文本节点：`>WorkBuddy Switch<` 与带引号的字符串字面量 {\"WorkBuddy Switch\"} 都要覆盖，
        # 否则侧边栏标题会留下半截旧名。
        (b'"WorkBuddy Switch"', b'"AutoBuddy"'),
        (b">WorkBuddy Switch<", b">AutoBuddy<"),
        # 笼统兜底放最后：上面两条已处理过的形态，走到这里就只剩裸串了。
        (b"WorkBuddy Switch", b"AutoBuddy"),
    ]
    for old, new in replacements:
        content = content.replace(old, new)

    # 文案适配：官方 UI 是给桌面客户端写的，容器里已无 IDE / CLI 能力。
    # 这里做**等长**字节替换，保证不破坏 JS 资源里的偏移与语法。
    # 这些是**前端 UI 文案**，只存在于 JS 资源里；HTML 外壳（几百字节）里没有它们。
    # 所以只在 is_js 时做，否则每个 HTML 请求都会误报「未命中」（v0.3.15 初版就踩了）。
    if is_js:
        for old, new in TEXT_REPLACEMENTS:
            old_b = old.encode("utf-8")
            new_b = new.encode("utf-8")
            if len(old_b) != len(new_b):
                raise ValueError(f"等长替换被破坏: {old} ({len(old_b)}) != {new} ({len(new_b)})")
            if content.count(old_b) == 0:
                # 上游改了文案就会走到这里。**不要静默通过**（v0.3.10 的教训就是静默失效），
                # 但也别让请求 500——每个串只告警一次，避免刷屏。
                if old not in _MISS_WARNED:
                    _MISS_WARNED.add(old)
                    print(f"[webui] 文案替换未命中（上游文案可能已变，请复核）: {old}")
            content = content.replace(old_b, new_b)

    return content


# ---------------------------------------------------------------------------
# 用户认证与配置持久化 API (SQLite 驱动)
# ---------------------------------------------------------------------------
from fastapi import Cookie, Depends, Response, HTTPException
from pydantic import BaseModel

class LoginReq(BaseModel):
    username: str
    password: str

class PwdChangeReq(BaseModel):
    old_password: str
    new_password: str

class ProfileReq(BaseModel):
    nickname: Optional[str] = None
    avatar: Optional[str] = None  # "official" | "custom"

class UsernameReq(BaseModel):
    current_password: str
    new_username: str


def _env_user_set() -> bool:
    """模板/环境里显式设置过 AUTH_USERNAME（或别名）时，用户名锁定不可改。"""
    return bool((os.getenv("AUTH_USERNAME") or os.getenv("AUTH_USER") or os.getenv("AUTH_DEFAULT_USER") or "").strip())


def _env_pass_set() -> bool:
    """显式设置过 AUTH_PASSWORD（或别名）时，密码锁定不可改（每次启动都会被环境变量覆盖回去）。"""
    return bool((os.getenv("AUTH_PASSWORD") or os.getenv("AUTH_PASS") or os.getenv("AUTH_DEFAULT_PASS") or "").strip())


def _env_pass_len() -> Optional[int]:
    """返回环境变量密码的长度，供前端展示同位数掩码；未设置时返回 None。"""
    pwd = (os.getenv("AUTH_PASSWORD") or os.getenv("AUTH_PASS") or os.getenv("AUTH_DEFAULT_PASS") or "").strip()
    return len(pwd) if pwd else None


try:
    from gateway.official_avatar_svg import official_avatar_svg as _official_avatar_svg
except Exception:
    from official_avatar_svg import official_avatar_svg as _official_avatar_svg


# 启动即把历史超长会话压回当前默认有效期上限：
# 旧版本签发过 7 天票据，收紧到 24h 后必须让它们立即到期，
# 否则「过了有效期必须重新登录」对老会话不生效。
try:
    db.clamp_sessions_to_default()
except Exception as _clamp_err:
    print(f"[auth] 历史会话压回失败（忽略）: {_clamp_err}")

def get_current_user(autobuddy_session: Optional[str] = Cookie(None)) -> Optional[str]:
    if not autobuddy_session:
        return None
    return db.validate_session(autobuddy_session)

@app.get("/api/auth/status")
async def auth_status(
    user: Optional[str] = Depends(get_current_user),
    autobuddy_session: Optional[str] = Cookie(None),
):
    enabled = True
    info = db.get_session_info(autobuddy_session) if user else None
    return {
        "enabled": enabled,
        "authenticated": bool(user),
        "username": user,
        "nickname": db.get_nickname(user) if user else None,
        "avatar": db.get_avatar() if user else "official",
        "usernameLocked": _env_user_set(),
        "passwordLocked": _env_pass_set(),
        "passwordLength": _env_pass_len(),
        "loginAt": int(info["created_at"] * 1000) if info else None,
        "expiresAt": int(info["expires_at"] * 1000) if info else None,
        "sessionHours": db.session_hours(),
    }

@app.post("/api/auth/login")
async def auth_login(req: LoginReq, response: Response):
    if not db.verify_user(req.username, req.password):
        return {"ok": False, "error": "用户名或密码错误"}
    token = db.create_session(req.username)
    hours = db.session_hours()
    response.set_cookie(
        key="autobuddy_session",
        value=token,
        max_age=hours * 3600,
        httponly=True,
        samesite="lax",
        path="/"
    )
    return {
        "ok": True,
        "username": req.username,
        "nickname": db.get_nickname(req.username),
        "avatar": db.get_avatar(),
        "usernameLocked": _env_user_set(),
        "passwordLocked": _env_pass_set(),
        "sessionHours": hours,
    }

@app.post("/api/auth/logout")
async def auth_logout(response: Response, autobuddy_session: Optional[str] = Cookie(None)):
    if autobuddy_session:
        db.destroy_session(autobuddy_session)
    response.delete_cookie("autobuddy_session", path="/")
    return {"ok": True}

@app.post("/api/auth/password")
async def auth_change_pwd(req: PwdChangeReq, response: Response, user: Optional[str] = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已失效")
    if _env_pass_set():
        return {"ok": False, "error": "AUTH_PASSWORD 已通过环境变量设置，此处不可修改（如需变更请修改容器环境变量）"}
    if not db.verify_user(user, req.old_password):
        return {"ok": False, "error": "当前密码不正确"}
    if len(req.new_password) < 6:
        return {"ok": False, "error": "新密码至少需要 6 个字符"}
    db.change_password(user, req.new_password)
    # 改密后作废该用户全部会话并清 Cookie，强制用新密码重新登录
    db.destroy_all_sessions(user)
    response.delete_cookie("autobuddy_session", path="/")
    return {"ok": True, "message": "密码修改成功，请重新登录", "relogin": True}


# ---------------- 用户资料（昵称 / 头像 / 用户名） ----------------

AVATAR_PATH = db.DATA_DIR / "avatars" / "avatar.png"
_AVATAR_MAX_BYTES = 200 * 1024
# 内置圆形头像候选的 key（与前端 WB_AVATAR_PRESETS 保持同步）
AVATAR_PRESET_KEYS = {"moon", "star", "leaf", "sun", "heart", "gem"}


def _valid_avatar_kind(kind: str) -> bool:
    if kind in ("official", "custom"):
        return True
    return isinstance(kind, str) and kind.startswith("preset:") and kind[7:] in AVATAR_PRESET_KEYS


def _avatar_mime(head: bytes) -> Optional[str]:
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:4] == b"RIFF" and len(head) >= 12 and head[8:12] == b"WEBP":
        return "image/webp"
    return None


@app.get("/api/auth/profile")
async def auth_profile_get(user: Optional[str] = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已失效")
    return {
        "username": user,
        "nickname": db.get_nickname(user),
        "avatar": db.get_avatar(),
        "usernameLocked": _env_user_set(),
        "passwordLocked": _env_pass_set(),
        "sessionHours": db.session_hours(),
    }


@app.put("/api/auth/profile")
async def auth_profile_put(req: ProfileReq, user: Optional[str] = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已失效")
    if req.nickname is not None:
        nick = req.nickname.strip()
        if not nick:
            return {"ok": False, "error": "昵称不能为空"}
        if len(nick) > 32:
            return {"ok": False, "error": "昵称最长 32 个字符"}
        db.set_nickname(nick)
    if req.avatar is not None:
        if not _valid_avatar_kind(req.avatar):
            return {"ok": False, "error": "无效的头像类型"}
        if req.avatar == "custom" and not AVATAR_PATH.exists():
            return {"ok": False, "error": "尚未上传自定义头像"}
        db.set_avatar(req.avatar)
    return {"ok": True, "nickname": db.get_nickname(user), "avatar": db.get_avatar()}


@app.post("/api/auth/avatar-upload")
async def auth_avatar_upload(request: Request, user: Optional[str] = Depends(get_current_user)):
    """直接收原始图片字节（不走 multipart，避免额外依赖），限 PNG/JPG/WebP、≤200KB。"""
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已失效")
    data = await request.body()
    if not data:
        return {"ok": False, "error": "未收到图片数据"}
    if len(data) > _AVATAR_MAX_BYTES:
        return {"ok": False, "error": "图片需 ≤ 200KB"}
    mime = _avatar_mime(data[:12])
    if not mime:
        return {"ok": False, "error": "仅支持 PNG / JPG / WebP 图片"}
    try:
        AVATAR_PATH.parent.mkdir(parents=True, exist_ok=True)
        AVATAR_PATH.write_bytes(data)
    except Exception as e:
        return {"ok": False, "error": f"头像保存失败: {e}"}
    db.set_avatar("custom")
    return {"ok": True, "avatar": "custom"}


@app.get("/api/auth/avatar")
async def auth_avatar_get():
    """统一头像出口：自定义回自定义图，否则回官方图标（圆形 SVG 底衬）。"""
    if db.get_avatar() == "custom" and AVATAR_PATH.exists():
        data = AVATAR_PATH.read_bytes()
        mime = _avatar_mime(data[:12]) or "image/png"
        return Response(content=data, media_type=mime, headers={"Cache-Control": "no-cache"})
    return Response(
        content=_official_avatar_svg(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/api/auth/username")
async def auth_change_username(req: UsernameReq, response: Response, user: Optional[str] = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已失效")
    if _env_user_set():
        return {"ok": False, "error": "AUTH_USERNAME 已通过环境变量设置，此处不可修改（如需变更请修改容器环境变量）"}
    if not db.verify_user(user, req.current_password):
        return {"ok": False, "error": "当前密码不正确"}
    new_name = (req.new_username or "").strip()
    if not new_name:
        return {"ok": False, "error": "新用户名不能为空"}
    if " " in new_name:
        return {"ok": False, "error": "用户名不能包含空格"}
    if len(new_name) > 32:
        return {"ok": False, "error": "用户名最长 32 个字符"}
    if not db.change_username(user, new_name):
        return {"ok": False, "error": "新用户名已存在或不可用"}
    db.destroy_all_sessions(new_name)
    response.delete_cookie("autobuddy_session", path="/")
    return {"ok": True, "username": new_name, "relogin": True}

@app.get("/icon.png")
@app.get("/icon-transparent.png")
@app.get("/favicon.ico")
async def get_icon():
    if ICON_PATH.exists():
        with open(ICON_PATH, "rb") as f:
            content = f.read()
        return Response(content=content, media_type="image/png")
    async with _internal_client() as client:
        r = await client.get(f"{BACKEND_URL}/icon.png")
        return Response(content=r.content, media_type="image/png")


def _reconcile_official_usage(data: dict, valid_account_ids) -> None:
    """校准 ``officialUsage`` 里失真的「今日消耗」。

    问题背景（实测）：官方底层的 ``officialUsage.summary.usageToday`` 长期为
    ``0.0``，各账号 ``usageToday`` 也全为 0、``currentRemaining`` 为 ``null``，
    而同一份响应顶层的 ``summary.usageToday`` 是准确的（如 1975.17）。
    官方前端优先采用 ``officialUsage``（``status=complete`` 即视为可用），
    于是把准确的顶层值覆盖成 0 —— 这就是「积分统计页今日消耗一直是 0」的根因。

    这里的做法是**不信任 officialUsage 的今日值**：
    - 顶层 summary.usageToday > 0 时，直接以它为准；
    - 顶层也为 0（或缺失）时，才回退到用 ``daily`` 里**今天**那条数据重算；
    - 各账号同样按其 daily 重算，并用顶层同账号的 currentRemaining 补 null。

    只动「今日」这一个维度：usage7Days / usageThisMonth 官方是有值的，
    动它们反而会引入新的不一致。
    """
    ou = data.get("officialUsage")
    if not isinstance(ou, dict) or not ou:
        return
    ou_summary = ou.get("summary")
    if not isinstance(ou_summary, dict):
        return

    top_summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    top_today = top_summary.get("usageToday")

    def _today_from_daily(daily) -> float:
        if not isinstance(daily, list):
            return 0.0
        today = time.strftime("%Y-%m-%d")
        for row in daily:
            if isinstance(row, dict) and str(row.get("date")) == today:
                try:
                    return float(row.get("usage") or 0.0)
                except Exception:
                    return 0.0
        return 0.0

    # 先按 daily 重算各账号的今日值，并收集可用的兜底合计
    ou_accounts = ou.get("accounts")
    recomputed_total = None
    if isinstance(ou_accounts, list):
        total = 0.0
        any_value = False
        for acc in ou_accounts:
            if not isinstance(acc, dict):
                continue
            value = _today_from_daily(acc.get("daily"))
            if acc.get("usageToday") in (None, 0, 0.0) and value:
                acc["usageToday"] = round(value, 4)
            if acc.get("usageToday"):
                any_value = True
            try:
                total += float(acc.get("usageToday") or 0.0)
            except Exception:
                pass
        if any_value:
            recomputed_total = total

        # currentRemaining 为 null 时用顶层同账号的值补上（官方 officialUsage 不带该字段）
        top_by_id = {}
        for acc in (data.get("accounts") or []):
            if isinstance(acc, dict) and acc.get("accountId"):
                top_by_id[str(acc["accountId"])] = acc
        for acc in ou_accounts:
            if not isinstance(acc, dict):
                continue
            if acc.get("currentRemaining") is None:
                src = top_by_id.get(str(acc.get("accountId")))
                if src and src.get("currentRemaining") is not None:
                    acc["currentRemaining"] = src.get("currentRemaining")

    if isinstance(top_today, (int, float)) and top_today > 0:
        ou_summary["usageToday"] = round(float(top_today), 4)
    elif recomputed_total is not None:
        ou_summary["usageToday"] = round(recomputed_total, 4)


# ---------------------------------------------------------------------------
# 签到状态结果缓存（积分统计页/每日任务页共用的「今日签到」徽章）
#
# 需求来源（用户 2026-09-26）：刷新页面后账号的飞机旅行图标很快显示，
# 但签到图标要很久才出来。
#
# 根因（实测）：/api/checkin/status 上游要 1.46s、经反代 2.34s —— 它得逐个账号
# 去 WorkBuddy 服务端实时查询；而 travel/status、wb-daily/accounts 走本地
# SQLite 只要 0.03s。前端 Promise.all 三个都到齐才渲染，于是签到徽章被拖住。
#
# 修法之一：签到状态本来就是「今天签没签」，分钟级陈旧度完全可接受，
# 缓存 60 秒即可挡掉刷新页面的重复查询，把 2.34s 降到毫秒级。
# ---------------------------------------------------------------------------
_CHECKIN_STATUS_CACHE: Dict[str, Any] = {"at": 0.0, "payload": None}
_CHECKIN_STATUS_TTL_SEC = 60.0


def _checkin_status_cached() -> Optional[Any]:
    """命中未过期缓存则返回，否则 None（让调用方去取真值）。"""
    hit = _CHECKIN_STATUS_CACHE.get("payload")
    if hit is not None and (time.time() - float(_CHECKIN_STATUS_CACHE.get("at") or 0.0)) < _CHECKIN_STATUS_TTL_SEC:
        return hit
    return None


def _checkin_status_store(payload: Any) -> None:
    _CHECKIN_STATUS_CACHE["payload"] = payload
    _CHECKIN_STATUS_CACHE["at"] = time.time()


@app.get("/api/credits/stats")
async def credits_stats_proxy(request: Request):
    """过滤官方 credits/stats 中的已删除僵尸账号，只保留当前账号池真实账号，并校准汇总指标。"""
    try:
        async with _internal_client(timeout=10.0) as client:
            r = await client.get(f"{BACKEND_URL}/api/credits/stats")
            if r.status_code != 200:
                return Response(content=r.content, status_code=r.status_code, media_type="application/json")
            data = r.json()
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502, media_type="application/json")

    # 读取当前有效的真实账号列表
    valid_account_ids = set()
    for acc_file in [Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy")) / "accounts.json", Path("/data/.wb-switch/accounts.json")]:
        if acc_file.exists():
            try:
                with open(acc_file, "r", encoding="utf-8") as f:
                    acc_data = json.load(f)
                if isinstance(acc_data, list):
                    for a in acc_data:
                        if a.get("id"):
                            valid_account_ids.add(str(a["id"]))
                elif isinstance(acc_data, dict) and "accounts" in acc_data:
                    for a in acc_data["accounts"]:
                        if a.get("id"):
                            valid_account_ids.add(str(a["id"]))
                if valid_account_ids:
                    break
            except Exception:
                pass

    if valid_account_ids and "accounts" in data and isinstance(data["accounts"], list):
        # 仅保留真实存在的账号
        filtered_accounts = [a for a in data["accounts"] if str(a.get("accountId")) in valid_account_ids]
        data["accounts"] = filtered_accounts

        # 重新校准 summary
        total_remaining = sum((a.get("currentRemaining") or 0.0) for a in filtered_accounts)
        total_capacity = sum((a.get("totalCapacity") or 0.0) for a in filtered_accounts)
        usage_7d = sum((a.get("usage7Days") or 0.0) for a in filtered_accounts)
        usage_today = sum((a.get("usageToday") or 0.0) for a in filtered_accounts)

        if "summary" in data and isinstance(data["summary"], dict):
            data["summary"]["currentRemaining"] = round(total_remaining, 2)
            data["summary"]["currentCapacity"] = round(total_capacity, 2)
            data["summary"]["usage7Days"] = round(usage_7d, 2)
            data["summary"]["usageToday"] = round(usage_today, 4)

    _reconcile_official_usage(data, valid_account_ids)

    # 口径兜底的产物必须自己校验一遍：反代层发的 Response 不像 FastAPI 路由那样
    # 有框架兜底，一旦这里抛异常就会变成非 JSON 响应，前端只会拿到一句
    # "The string did not match the expected pattern"，看不出真因。
    # 所以出问题就把事故点带回日志，而不是顺着官方响应一起发给前端。
    if os.getenv("AB_DEBUG_RESPONSE", "").strip() in ("1", "true", "yes"):
        try:
            _chk = json.loads(json.dumps(data, ensure_ascii=False))
            _ou = ((_chk.get("officialUsage") or {}).get("summary") or {}).get("usageToday")
            _top = (_chk.get("summary") or {}).get("usageToday")
            logging.info("[credits] 口径对账 顶层=%s officialUsage=%s", _top, _ou)
        except Exception as exc:  # noqa: BLE001 - 自检不得反过来打断主流程
            logging.exception("[credits] 响应自检失败: %s", exc)

    return Response(content=json.dumps(data, ensure_ascii=False), media_type="application/json")


@app.get("/api/token-stats")
async def token_stats_api(request: Request):
    # 容器环境核心增强：接管 /api/token-stats，返回网关实测 Token 统计数据
    stats = get_aggregated_token_stats()
    return stats


GATEWAY_BASE_URL = os.getenv("AB_GATEWAY_BASE_URL", "http://127.0.0.1:18091")
GATEWAY_MODELS_URL = os.getenv("AB_GATEWAY_MODELS_URL", GATEWAY_BASE_URL + "/v1/models")


@app.get("/api/account-pool-official")
async def account_pool_get_official():
    """向官方网关直取的原始账号池状态（保留作为回退与排查对比入口）。

    注意：真正的 `/api/account-pool` 由下方 `account_pool_status_api()` 提供（带字段补充）。
    路由同名会让 FastAPI 只认先注册的那个，所以官方原始转发必须换个路径，
    否则补字段的逻辑永远不会生效。
    """
    try:
        async with _internal_client(timeout=5.0) as client:
            r = await client.get(GATEWAY_BASE_URL + "/account-pool/status")
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.get("/api/account-pool-legacy")
async def account_pool_get_legacy():
    """官方账号池状态的原始转发（保留作为回退入口，供排查对比）。"""
    try:
        async with _internal_client(timeout=5.0) as client:
            r = await client.get(GATEWAY_BASE_URL + "/account-pool/status")
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.get("/api/account-pool")
async def account_pool_get_v2():
    """替官方账号池状态补上“新账号”所需的展示字段（昵称/邮箱与账号级停用状态）。

    背景：官方 `/api/account-pool` 只在 `selectionCounts` 里回已有调用记录的
    账号名，新账号（未调用过）在那里既无昵称也无邮箱；前端只能拿账号 ID 去填
    卡片标题，而卡片标题一旦对不上任何键，启用/停用、设为首选、检测账号、
    调用次数四个控件就整块不渲染 —— 看上去就是「功能缺失」。

    这里只**补充**字段（nickname / email / disabled / disabledSource），
    原样保留官方的 mode / enabledAccountIds / selectionCounts 等，
    不动任何业务语义。同时把未入池的新账号补进 accounts 列表，
    让它们与已入池账号一样能被渲染。
    """
    try:
        async with _internal_client(timeout=8.0) as client:
            r = await client.get(GATEWAY_BASE_URL + "/account-pool/status")
            if r.status_code != 200:
                return Response(content=r.content, status_code=r.status_code,
                                media_type="application/json")
            data = r.json() or {}
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")

    # 账号池白名单（官方的 enabledAccountIds；空数组 + allEnabledByDefault=false
    # 在业务上等于“全部启用”，这里不重新解释，只原样透传）。
    accounts = data.get("accounts")
    if not isinstance(accounts, list):
        accounts = []
    data["accounts"] = accounts

    # 账号池真实条目（昵称/邮箱的唯一来源）
    pool_accounts = {}
    for acc in _load_accounts_for_models():
        aid = str(acc.get("id") or acc.get("uid") or "")
        if aid:
            pool_accounts[aid] = acc

    # 账号级停用策略（手动/巡检），供卡片区分渲染
    disabled_map = {}
    try:
        disabled_map = account_policy.load_policy() or {}
    except Exception as e:
        print(f"[account-pool] 读账号停用策略失败: {e}")

    enabled_ids = set(str(x) for x in (data.get("enabledAccountIds") or []))
    all_default = bool(data.get("allEnabledByDefault"))

    # 已有的账号条目：补 nickname / email / disabled / disabledSource / enabled
    seen = set()
    for item in accounts:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("id") or item.get("accountId") or "")
        if aid:
            seen.add(aid)
        src = pool_accounts.get(aid) or {}
        if not item.get("nickname"):
            item["nickname"] = (src.get("nickname") or src.get("email")
                                or item.get("name") or aid or None)
        if not item.get("email"):
            item["email"] = src.get("email") or None
        if not item.get("id") and aid:
            item["id"] = aid
        # 账号级停用（普通正常账号也有 disabled=false，前端据此渲染启用态）
        item["disabled"] = aid in disabled_map
        item["disabledSource"] = disabled_map.get(aid)
        # 是否入池：给前端一个明确的布尔值，不必自己推白名单语义
        if all_default:
            item["enabled"] = not item["disabled"]
        else:
            item["enabled"] = (aid in enabled_ids) and not item["disabled"]

    # 未出现在账号池列表里的新账号（未入池、未调用过）全部补进去，
    # 否则它们的卡片永远拿不到昵称/邮箱，控制条也永远不出现。
    for aid, src in pool_accounts.items():
        if aid in seen:
            continue
        accounts.append({
            "id": aid,
            "accountId": aid,
            "name": src.get("nickname") or src.get("email") or aid,
            "nickname": src.get("nickname") or src.get("email") or aid,
            "email": src.get("email"),
            "variant": src.get("variant"),
            "enabled": (all_default or aid in enabled_ids) and aid not in disabled_map,
            "disabled": aid in disabled_map,
            "disabledSource": disabled_map.get(aid),
            "count": 0,
        })

    return Response(content=json.dumps(data, ensure_ascii=False),
                    media_type="application/json")


@app.put("/api/account-pool")
async def account_pool_put(request: Request):
    """转发账号池配置更新（模式 / 启用列表 / 首选账号）。"""
    body = await request.body()
    try:
        async with _internal_client(timeout=5.0) as client:
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
        async with _internal_client(timeout=5.0) as client:
            r = await client.get(GATEWAY_BASE_URL + "/account-pool/selections",
                                 params={"limit": limit})
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.put("/api/account-models")
async def account_models_put(request: Request):
    """转发模型级禁用的切换请求（账号 x 模型）。

    前端点一下模型标签就走这里：网关那边只认 ``accountId`` + ``model`` + ``disabled``，
    这里不做任何业务判断，纯粹转发，把错误 detail 原样带回给 UI 弹提示。
    """
    body = await request.body()
    try:
        async with _internal_client(timeout=5.0) as client:
            r = await client.put(GATEWAY_BASE_URL + "/account-models/config",
                                 content=body,
                                 headers={"Content-Type": "application/json"})
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.post("/api/account-models/restore")
async def account_models_restore(request: Request):
    """转发「一键恢复」请求（把某账号被禁用的模型放回）。

    ``scope`` 由前端给：``all`` 全放回、``auto`` 只放回巡检禁的。
    同样纯转发，不做业务判断 —— 判定「哪些该恢复」是网关侧策略模块的职责。
    """
    body = await request.body()
    try:
        async with _internal_client(timeout=5.0) as client:
            r = await client.post(GATEWAY_BASE_URL + "/account-models/restore",
                                  content=body,
                                  headers={"Content-Type": "application/json"})
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.post("/api/account-pool/selections/reset")
async def account_pool_selections_reset():
    """清空选账号流水，便于重新观测并发分摊。"""
    try:
        async with _internal_client(timeout=5.0) as client:
            r = await client.post(GATEWAY_BASE_URL + "/account-pool/selections/reset")
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.post("/api/refresh-token")
async def refresh_token_proxy(request: Request):
    """转发「刷新 Token」（账号卡片菜单）。

    超时给到 40s：要等上游刷新接口回包，默认值太紧会把「上游慢」
    误报成「刷新失败」。
    """
    body = await request.body()
    try:
        async with _internal_client(timeout=40.0) as client:
            r = await client.post(GATEWAY_BASE_URL + "/refresh-token",
                                  content=body,
                                  headers={"Content-Type": "application/json"})
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"ok": False, "error": str(e)}),
                        status_code=502, media_type="application/json")


@app.post("/api/delete")
async def delete_account_proxy(request: Request):
    """转发「删除账号」（账号卡片菜单）。"""
    body = await request.body()
    try:
        async with _internal_client(timeout=20.0) as client:
            r = await client.post(GATEWAY_BASE_URL + "/delete",
                                  content=body,
                                  headers={"Content-Type": "application/json"})
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"ok": False, "error": str(e)}),
                        status_code=502, media_type="application/json")


@app.get("/api/export-accounts")
async def export_accounts_proxy():
    """转发「导出账号备份」。"""
    try:
        async with _internal_client(timeout=20.0) as client:
            r = await client.get(GATEWAY_BASE_URL + "/export-accounts")
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"ok": False, "error": str(e)}),
                        status_code=502, media_type="application/json")


@app.post("/api/import")
async def import_accounts_proxy(request: Request):
    """转发「导入账号备份」（合并式，不覆盖未列出的账号）。"""
    body = await request.body()
    try:
        async with _internal_client(timeout=30.0) as client:
            r = await client.post(GATEWAY_BASE_URL + "/import",
                                  content=body,
                                  headers={"Content-Type": "application/json"})
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"ok": False, "error": str(e)}),
                        status_code=502, media_type="application/json")


@app.post("/api/account-pool/toggle")
async def account_pool_toggle(request: Request):
    """转发账号级停用切换（点击账号卡片上的「停用 / 启用」）。"""
    body = await request.body()
    try:
        async with _internal_client(timeout=5.0) as client:
            r = await client.post(GATEWAY_BASE_URL + "/account-pool/toggle",
                                  content=body,
                                  headers={"Content-Type": "application/json"})
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.post("/api/account-health/probe")
async def account_health_probe(request: Request):
    """转发「检测账号」：发一次轻量鉴权请求，只回报结论、不改配置。

    超时给到 45s —— 探测本身要等上游回第一个字节，5s 的默认值太紧，
    会把「上游慢」误报成「检测失败」。
    """
    body = await request.body()
    try:
        async with _internal_client(timeout=45.0) as client:
            r = await client.post(GATEWAY_BASE_URL + "/account-health/probe",
                                  content=body,
                                  headers={"Content-Type": "application/json"})
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.get("/api/account-health/audit")
async def account_health_audit():
    """转发「一致性自检」：把凭据与模型两侧的结论并排给出。

    超时给到 120s —— 这个接口会对每个账号跑一次凭据探测加数次模型探测，
    账号多时耗时明显长于单账号检测。用 45s 会在账号稍多时稳定超时。
    """
    try:
        async with _internal_client(timeout=120.0) as client:
            r = await client.get(GATEWAY_BASE_URL + "/account-health/audit")
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


# ---------------------------------------------------------------------------
# API 接入信息与访问密钥的转发（WebUI 设置页用）
#
# 这些路由必须注册在文件末尾的 catch-all 之前 —— Starlette 按注册顺序匹配，
# 否则会被 `/{path:path}` 抢走并转发到官方后端 57890（那边没有这些接口）。
# ---------------------------------------------------------------------------

async def _forward_gateway(method: str, path: str, body: Optional[bytes] = None):
    """把请求转发到网关（18091）。失败返回 502 而不是让页面白屏。"""
    try:
        headers = {"Content-Type": "application/json"} if body else None
        async with _internal_client(timeout=10.0) as client:
            r = await client.request(method, GATEWAY_BASE_URL + path,
                                     content=body, headers=headers)
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.get("/api/model-health")
async def model_health_get():
    """巡检配置 + 可选账号 + 上次运行状态（设置页面板数据源）。"""
    return await _forward_gateway("GET", "/model-health/config")


@app.put("/api/model-health")
async def model_health_put(request: Request):
    """更新巡检配置（开关 / 间隔 / 账号范围 / 自动启用）。"""
    return await _forward_gateway("PUT", "/model-health/config", await request.body())


@app.post("/api/model-health/run")
async def model_health_run(request: Request):
    """立即执行一轮巡检。

    探测是逐个账号 × 模型发真实请求，可能耗时较久，因此这里放宽超时
    （默认 5s 的内部客户端不够用）。
    """
    body = await request.body()
    try:
        async with _internal_client(timeout=300.0) as client:
            r = await client.post(GATEWAY_BASE_URL + "/model-health/run",
                                  content=body or b"{}",
                                  headers={"Content-Type": "application/json"})
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=502,
                        media_type="application/json")


@app.get("/api/gateway-info")
async def gateway_info_api():
    """连接信息：对外地址、端点、模型/账号规模、密钥状态。"""
    return await _forward_gateway("GET", "/gateway/info")


@app.get("/api/api-keys")
async def api_keys_get():
    return await _forward_gateway("GET", "/api-keys/status")


@app.post("/api/api-keys")
async def api_keys_create(request: Request):
    return await _forward_gateway("POST", "/api-keys", await request.body())


@app.post("/api/api-keys/update")
async def api_keys_update(request: Request):
    return await _forward_gateway("POST", "/api-keys/update", await request.body())


@app.post("/api/api-keys/delete")
async def api_keys_delete(request: Request):
    return await _forward_gateway("POST", "/api-keys/delete", await request.body())


@app.post("/api/api-keys/delete-all")
async def api_keys_delete_all():
    return await _forward_gateway("POST", "/api-keys/delete-all")


@app.put("/api/api-keys/config")
async def api_keys_config(request: Request):
    return await _forward_gateway("PUT", "/api-keys/config", await request.body())


@app.put("/api/api-keys/whitelist")
async def api_keys_whitelist(request: Request):
    """IP 白名单写入。与 config 同级，语义：仅在开启密钥校验时生效。"""
    return await _forward_gateway("PUT", "/api-keys/whitelist", await request.body())


@app.post("/api/gateway-selftest")
async def gateway_selftest():
    """从容器内实测一次网关连通性。

    浏览器无法直接访问 18091（跨源且网关无 CORS），所以自检必须由代理侧发起：
    loopback 请求免密钥校验，正好验证「网关活着 + 模型清单可用 + 密钥配置自洽」。
    """
    steps = []
    out = {"steps": steps}

    async with _internal_client(timeout=8.0) as client:
        try:
            r = await client.get(GATEWAY_BASE_URL + "/health")
            ok = r.status_code == 200
            detail = f"HTTP {r.status_code}"
            if ok:
                try:
                    h = r.json()
                    out["models"] = h.get("models_count")
                    out["requireKey"] = h.get("require_api_key")
                    detail = f"HTTP 200 · {h.get('models_count')} 个模型 · 密钥校验{'已开启' if h.get('require_api_key') else '未开启'}"
                except Exception:
                    pass
            steps.append({"name": "网关健康检查 /health", "ok": ok, "detail": detail})
        except Exception as e:
            steps.append({"name": "网关健康检查 /health", "ok": False, "detail": str(e)})

        try:
            r = await client.get(GATEWAY_BASE_URL + "/v1/models")
            count = 0
            if r.status_code == 200:
                try:
                    count = len((r.json() or {}).get("data") or [])
                except Exception:
                    count = 0
            steps.append({
                "name": "模型清单 /v1/models",
                "ok": r.status_code == 200,
                "detail": f"HTTP {r.status_code} · {count} 个模型",
            })
        except Exception as e:
            steps.append({"name": "模型清单 /v1/models", "ok": False, "detail": str(e)})

        try:
            r = await client.get(GATEWAY_BASE_URL + "/api-keys/status")
            keys = {}
            if r.status_code == 200:
                try:
                    keys = (r.json() or {})
                except Exception:
                    keys = {}
            require = bool(keys.get("requireKey"))
            enabled = int((keys.get("stats") or {}).get("enabled") or 0)
            ok = not require or enabled > 0
            detail = ("已开启校验，可用密钥 %d 个" % enabled) if require else "未开启校验，接口开放访问"
            steps.append({"name": "密钥配置自洽性", "ok": ok, "detail": detail})
        except Exception as e:
            steps.append({"name": "密钥配置自洽性", "ok": False, "detail": str(e)})

    out["ok"] = all(s["ok"] for s in steps)
    return out


async def _fetch_gateway_catalog() -> list:
    """取网关（18091）可路由的完整模型清单。

    网关的清单 = 内置基础清单 + 从官方 usage 自动发现出来的模型，是「当前能调
    用哪些模型」的唯一权威来源。取不到时降级为空数组，由调用方回退到 usage。
    """
    try:
        async with _internal_client(timeout=5.0) as client:
            r = await client.get(GATEWAY_MODELS_URL)
            if r.status_code != 200:
                return []
            data = r.json() or {}
            return [m.get("id") for m in (data.get("data") or []) if m.get("id")]
    except Exception:
        return []


def _load_accounts_for_models() -> list:
    """读账号池，**合并两份账号文件**（按 ID 去重，字段互补）。

    两个文件各有各的盲区，只取其一必然漏账号：
      - ``/data/.wb-switch/accounts.json``：官方维护的那份，用户新加入的
        账号（如「一杯美式」）先出现在这里；
      - ``$AB_DATA_DIR/accounts.json``：容器自己的副本，字段更全。

    历史 bug：早先写的是「找到第一个存在的文件就 break」，于是只认自己那份，
    官方新加的账号根本进不了 /api/account-models 与 /api/account-pool，
    表现为「新账号的卡片上没有任何控件」（启用/设为首选/检测/调用次数全无）。

    合并原则：先官方后本地覆盖，同 ID 以本地为准；读不到就返回空列表，
    绝不因账号文件异常而让调用方失败。
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for acc_file in [Path("/data/.wb-switch/accounts.json"),
                     Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy")) / "accounts.json"]:
        if not acc_file.exists():
            continue
        try:
            with open(acc_file, "r", encoding="utf-8") as f:
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
                # 没 ID 的条目按昵称/邮箱兜底，总比整条丢掉强。
                key = str(acc.get("nickname") or acc.get("email") or "")
            if not key:
                continue
            # 后遍历的（本地那份）覆盖先遍历的（官方那份）—— 本地字段更全。
            merged[key] = {**(merged.get(key) or {}), **acc}
    return list(merged.values())


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
    data_dir = Path(os.getenv("AB_DATA_DIR", "/data/.autobuddy"))
    cache_file = data_dir / "official_usage_cache.json"
    tracker_file = data_dir / "token_stats_logs.json"

    catalog = await _fetch_gateway_catalog()
    result = {"accounts": {}, "catalog": catalog, "discovered": []}

    # 模型级禁用策略：由网关（18091）持有并落盘，这里读一份用于渲染禁用态。
    # 读不到就当空策略 —— UI 显示「全部可用」，绝不因为策略读取失败而让整卡消失。
    # policySources 让卡片能区分「手动禁用」与「巡检禁用」，两者恢复方式不同。
    disabled_by_account = {}
    disabled_sources_by_account = {}
    try:
        async with _internal_client(timeout=5.0) as client:
            r = await client.get(GATEWAY_BASE_URL + "/account-models/config")
            if r.status_code == 200:
                body = r.json() or {}
                disabled_by_account = body.get("policy") or {}
                disabled_sources_by_account = body.get("policySources") or {}
    except Exception:
        disabled_by_account = {}
        disabled_sources_by_account = {}

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

    # 兜底：某账号可能「一条调用记录都没有，但已被用户禁用过模型」。
    # 不补进 agg 的话，它的禁用状态会静默丢失，用户会以为点了没生效。
    #
    # ‼️ 但必须先确认这个账号**现在还在账号池里**。
    # 这里曾经无约束地把策略文件里的每个账号键都补进来，结果历史残留的
    # 策略（账号已删、键还在）会被当成真账号渲染成一张卡片 ——
    # 卡片标题只能回退成 UUID，与控制条、模型区都对不上，
    # 看上去就像「多出来几个没有名字、没有任何功能的卡片」。
    # 策略文件不会自己清理旧键，所以这层过滤必须放在渲染侧。
    try:
        pool_ids = {
            str(acc.get("id") or acc.get("uid") or "")
            for acc in _load_accounts_for_models()
        }
    except Exception as e:
        print(f"[account-models] 读账号池做策略键过滤失败: {e}")
        pool_ids = set()

    for aid, models in disabled_by_account.items():
        if aid not in agg and models and str(aid) in pool_ids:
            _touch(str(aid), None)

    all_used = set()
    for aid, entry in agg.items():
        used = sorted(entry["used"])
        all_used.update(used)
        # 名称兜底：新账号往往没有昵称，官方流水里 accountName 也是空的。
        # 留空会让前端拿卡片标题去反查时对不上，进而丢掉 accountId ——
        # 而所有需要 accountId 的交互（点击禁用 / 全部恢复）都靠它，
        # 结果就是「新加的账号卡片上没有任何功能性按钮」。
        # 宁可回退成账号 ID（至少唯一、能对上接口），也不留空。
        label = entry.get("name") or str(aid)
        result["accounts"][aid] = {
            "id": aid,
            "name": label,
            # 供前端建立多重索引：昵称可能重名或为空，accountId 永远唯一。
            "aliases": [x for x in (entry.get("name"), str(aid)) if x],
            # 可用清单与网关保持一致，新账号也不会是空的
            "models": catalog or sorted(entry["used"]),
            "used": used,
            "usage": entry.get("usage") or {},
            "gatewayCalls": entry.get("gatewayCalls", 0),
            # 该账号被禁用的模型（模型级禁用），供卡片渲染灰化删除线状态
            "disabled": disabled_by_account.get(str(aid)) or [],
            # 每个禁用项的来源："manual"（人手禁的）/ "auto"（巡检禁的）
            "disabledSources": disabled_sources_by_account.get(str(aid)) or {},
        }
    # 账号池里存在、但两条流水源都没覆盖到的账号（例如刚添加、未曾调用），
    # 同样要出现在结果里 —— 否则它们的卡片拿不到 id，功能按钮全数消失。
    try:
        for acc in _load_accounts_for_models():
            aid = str(acc.get("id") or acc.get("uid") or "")
            if not aid or aid in result["accounts"]:
                continue
            label = acc.get("nickname") or acc.get("email") or aid
            result["accounts"][aid] = {
                "id": aid,
                "name": label,
                "aliases": [x for x in (acc.get("nickname"), acc.get("email"), aid) if x],
                "models": catalog,
                "used": [],
                "usage": {},
                "gatewayCalls": 0,
                "disabled": disabled_by_account.get(aid) or [],
                "disabledSources": disabled_sources_by_account.get(aid) or {},
            }
    except Exception as e:
        print(f"[account-models] 补全账号池条目失败: {e}")
    result["discovered"] = sorted(all_used)
    result["disabledTotal"] = sum(len(v) for v in disabled_by_account.values())
    return result

# ---------------------------------------------------------------------------
# WorkBuddy 每日成长任务服务转发（由容器内网 loopback 18093 提供，不对外暴露端口）。
# 由 wb_daily.py 托管 vendor 版签到脚本；账号池 cn 账号只读共用凭据，无烧号风险。
# ---------------------------------------------------------------------------
WB_DAILY_INTERNAL = os.getenv("WB_DAILY_INTERNAL", "http://127.0.0.1:18093")


def _wb_daily_client(timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, trust_env=False)


@app.get("/api/wb-daily/config")
async def wb_daily_config_get():
    async with _wb_daily_client() as client:
        r = await client.get(f"{WB_DAILY_INTERNAL}/api/wb-daily/config")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/api/wb-daily/config")
async def wb_daily_config_post(request: Request):
    body = await request.body()
    async with _wb_daily_client() as client:
        r = await client.post(f"{WB_DAILY_INTERNAL}/api/wb-daily/config", content=body, headers={"Content-Type": "application/json"})
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/api/wb-daily/accounts")
async def wb_daily_accounts():
    async with _wb_daily_client() as client:
        r = await client.get(f"{WB_DAILY_INTERNAL}/api/wb-daily/accounts")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/api/wb-daily/catalog")
async def wb_daily_catalog():
    async with _wb_daily_client() as client:
        r = await client.get(f"{WB_DAILY_INTERNAL}/api/wb-daily/catalog")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/api/wb-daily/runs")
async def wb_daily_runs():
    async with _wb_daily_client() as client:
        r = await client.get(f"{WB_DAILY_INTERNAL}/api/wb-daily/runs")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/api/wb-daily/run-accounts/{job_id}")
async def wb_daily_run_accounts(job_id: str):
    async with _wb_daily_client() as client:
        r = await client.get(f"{WB_DAILY_INTERNAL}/api/wb-daily/run-accounts/{job_id}")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/api/wb-daily/tasks")
async def wb_daily_tasks(limit: int = 200, account: Optional[str] = None):
    """任务台账：任务维度的记录（面板「任务记录」卡片）。"""
    async with _wb_daily_client() as client:
        params = {"limit": limit}
        if account:
            params["account"] = account
        r = await client.get(f"{WB_DAILY_INTERNAL}/api/wb-daily/tasks", params=params)
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/api/wb-daily/task-summary")
async def wb_daily_task_summary():
    """任务台账聚合：每个任务项最近一次结果 + 成功次数。"""
    async with _wb_daily_client() as client:
        r = await client.get(f"{WB_DAILY_INTERNAL}/api/wb-daily/task-summary")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/api/wb-daily/run")
async def wb_daily_run():
    async with _wb_daily_client() as client:
        r = await client.post(f"{WB_DAILY_INTERNAL}/api/wb-daily/run")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/api/wb-daily/jobs")
async def wb_daily_jobs():
    async with _wb_daily_client() as client:
        r = await client.get(f"{WB_DAILY_INTERNAL}/api/wb-daily/jobs")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/api/wb-daily/status/{job_id}")
async def wb_daily_status(job_id: str):
    async with _wb_daily_client() as client:
        r = await client.get(f"{WB_DAILY_INTERNAL}/api/wb-daily/status/{job_id}")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.delete("/api/wb-daily/abort/{job_id}")
async def wb_daily_abort(job_id: str):
    async with _wb_daily_client() as client:
        r = await client.delete(f"{WB_DAILY_INTERNAL}/api/wb-daily/abort/{job_id}")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/api/wb-daily/health")
async def wb_daily_health():
    async with _wb_daily_client() as client:
        r = await client.get(f"{WB_DAILY_INTERNAL}/api/wb-daily/health")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"])
async def proxy_all(request: Request, path: str):
    # 签到状态缓存：这是全站最慢的只读接口（上游 1.46s / 经反代 2.34s），
    # 而它的语义是「今天签没签」——分钟级陈旧完全可接受。
    # 命中缓存直接返回，刷新页面不再等 2 秒。
    if request.method == "GET" and path == "api/checkin/status":
        cached = _checkin_status_cached()
        if cached is not None:
            return Response(content=json.dumps(cached, ensure_ascii=False),
                            media_type="application/json",
                            headers={"x-autobuddy-cache": "hit"})

    url = f"{BACKEND_URL}/{path}"
    query = str(request.query_params)
    if query:
        url = f"{url}?{query}"
    
    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    
    async with _internal_client(timeout=60.0) as client:
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
            # is_js=True：TEXT_REPLACEMENTS 是前端 UI 文案，只存在于 JS 资源里。
            content = clean_mac_content(content, is_js=True)
            res_headers["cache-control"] = "no-store, must-revalidate"

        # 签到状态取到真值后写入缓存（只缓存成功响应，失败不缓存，
        # 否则一次上游抖动会被缓存放大成整分钟的「查不到签到状态」）。
        if request.method == "GET" and path == "api/checkin/status" and r.status_code == 200:
            try:
                _checkin_status_store(json.loads(content))
            except Exception as e:
                logging.warning("[checkin] 缓存写入失败: %s", e)

        return Response(
            content=content,
            status_code=r.status_code,
            headers=res_headers
        )

class _AccessNoiseFilter(logging.Filter):
    """静音高频轮询与探活的 access log（与 gateway/main.py 中同名实现保持一致）。

    见 main.py 里的详细说明：WebUI 定时轮询与健康探活会在 access log 里刷屏，
    真实请求反而被淹没。这里按「路径 + 2xx」过滤，4xx/5xx 一律保留。
    两个进程各自过滤自己监听端口上的请求：WebUI 面板（本文件，:18090）
    与 API 网关（main.py，:18091）。
    """

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
        if " 2" not in msg or " OK" not in msg:
            return True
        return not any(p in msg for p in self.NOISY_PATHS)


def _install_access_filter() -> None:
    lg = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _AccessNoiseFilter) for f in lg.filters):
        lg.addFilter(_AccessNoiseFilter())


if __name__ == "__main__":
    port = int(os.getenv("PORT", 18090))
    _install_access_filter()
    uvicorn.run(app, host="0.0.0.0", port=port)
