#!/usr/bin/env bash
# AutoBuddy 部署脚本：先 abort 正在跑的任务，再重启容器。
#
# 为什么需要这一步（2026-10-17 用户要求）：
#   直接 `docker restart` 会把正在执行的每日任务**硬杀**，
#   那一轮的逐账号明细停在最后几次增量落库上（v0.9.90 起每 20 秒一次），
#   看起来像「任务莫名失败」。先 abort 让它走正常收尾路径
#   （落库 + 标记 aborted），巡检稍后会自动从剩余账号续跑，
#   比硬杀干净得多。
#
# 用法：
#   ./scripts/deploy.sh              # 只 abort + 重启（代码已 scp 过）
#   ./scripts/deploy.sh --sync       # 额外把本地 gateway/*.py 同步进容器
#
# 幂等：没有任务在跑时 abort 步骤直接跳过，不影响重启。

set -euo pipefail

CONTAINER="${AB_CONTAINER:-AutoBuddy}"
BASE="${AB_BASE:-http://192.168.31.2:18090}"
SYNC=0
[ "${1:-}" = "--sync" ] && SYNC=1

say() { printf '  %s\n' "$*"; }

# ---------------------------------------------------------------- 1. abort
say "检查是否有正在执行的每日任务…"
HEALTH="$(curl -s -m 15 "$BASE/api/wb-daily/health" || true)"
RUNNING="$(printf '%s' "$HEALTH" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print("1" if d.get("running") else "0")
' 2>/dev/null || echo 0)"
JOB="$(printf '%s' "$HEALTH" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get("running_job_id") or "")
' 2>/dev/null || true)"

if [ "$RUNNING" = "1" ] && [ -n "$JOB" ]; then
  say "发现执行中的任务 $JOB，先发送中止请求…"
  # abort 是软中止：_execute 的 CancelledError 分支会落库并标记 aborted
  curl -s -m 20 -X DELETE "$BASE/api/wb-daily/abort/$JOB" >/dev/null || true
  # 等它真正结束（最多 30 秒；上游脚本每账号约 10 秒，给足余量）
  for _ in $(seq 1 30); do
    sleep 1
    ST="$(curl -s -m 10 "$BASE/api/wb-daily/health" || true)"
    ST="$(printf '%s' "$ST" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print("1" if d.get("running") else "0")
' 2>/dev/null || echo 0)"
    [ "$ST" = "0" ] && break
  done
  if [ "$ST" = "0" ]; then
    say "任务已正常中止并落库（巡检稍后会自动从剩余账号续跑）"
  else
    say "⚠️ 任务 30 秒内未结束，仍将继续重启（会被硬杀）"
  fi
else
  say "当前没有执行中的任务，跳过中止步骤"
fi

# ---------------------------------------------------------------- 2. sync
if [ "$SYNC" = "1" ]; then
  say "同步 gateway/*.py 到容器…"
  REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
  for f in "$REPO_ROOT"/gateway/*.py; do
    [ -f "$f" ] || continue
    b="$(basename "$f")"
    scp -q "$f" "unraid-root:/tmp/$b"
    ssh -T unraid-root "docker cp /tmp/$b $CONTAINER:/app/gateway/$b" >/dev/null
    say "  已同步 $b"
  done
  # 陈旧字节码会让新代码不生效（v0.9.89 踩过：DailyJob.__init__ 被旧 pyc 覆盖）
  ssh -T unraid-root "docker exec $CONTAINER sh -c 'find /app -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null'" >/dev/null
  say "  已清理 __pycache__"
fi

# ---------------------------------------------------------------- 3. restart
say "重启容器 $CONTAINER …"
ssh -T unraid-root "docker restart $CONTAINER" >/dev/null
say "等待服务就绪…"
for _ in $(seq 1 40); do
  sleep 1
  H="$(ssh -T unraid-root "docker ps --filter name=$CONTAINER --format '{{.Status}}'" 2>/dev/null | tr -d '\r' || true)"
  case "$H" in
    *healthy*) break ;;
  esac
done
say "容器状态：$H"
say "完成。"
