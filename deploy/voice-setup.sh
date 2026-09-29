#!/usr/bin/env bash
# CapsWriter 语音入口一键配置（部署面板 API）
#
# 在「服务器 WSL」里运行（面板容器只在 WSL Docker 网络内可达，绕过人用登录门；
# 认证完全靠 X-API-Key 的作用域检查，所有写操作都会记入面板审计）：
#
#   PANEL_KEY=dpk_xxx bash voice-setup.sh
#
# 需要 Key 具备作用域：read + pipeline:write + deploy + gate:write。
# 本脚本不回显、不落盘 Key；用完可用 `unset PANEL_KEY` 清除。
#
# 做什么：
#   1. 创建/更新流水线（ASR 流水线 id=voice-asr-prod 绑子域 voice；
#      管理流水线 id=voice-admin-prod 绑子域 voice-admin），实际绑定域名
#      以面板返回为准核对，再进入后续步骤
#   2. 上传并部署两个网关容器（同一份 deploy/voice-gateway.zip，构建参数不同）
#   3. 部署结束按面板成功/失败收尾标记判定；失败即非零退出并展示错误摘要，
#      不会继续配置机器门
#   4. 网关：voice-asr-prod 开机器门（路径 /*，请求体为列表，只改该流水线，
#      不覆盖其他站点）；voice-admin 域名追加进登录墙 domains（幂等）
#   5. 验证：无 Key 401 / 带 Key 426（ASR 的 WS 端口对普通 GET 的正确回应）
#
# 不做什么（需人工）：
#   - 给「现有的那把」Key 勾选站点授权（面板 UI → API 密钥 → 编辑 → 勾 voice-asr-prod）。
#     密钥不能管理密钥，脚本无能为力；不勾则带 Key 也会 403。
#   - Windows 管理端 :6017 的部署与防火墙（当前未运行，voice-admin 会 502，属预期）。

set -euo pipefail

BASE_DOMAIN="${BASE_DOMAIN:-faiz.dpdns.org}"
UPSTREAM_IP="${UPSTREAM_IP:-192.168.0.104}"
ASR_LISTEN="${ASR_LISTEN:-8111}"
ADMIN_LISTEN="${ADMIN_LISTEN:-8112}"
GATE_PATHS="${GATE_PATHS:-[\"/*\"]}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ZIP="${ZIP:-$HERE/voice-gateway.zip}"
LIB="$HERE/voice_setup_lib.py"

: "${PANEL_KEY:?请以 PANEL_KEY=dpk_xxx bash $0 运行；Key 不会回显或落盘}"
[ -f "$ZIP" ] || { echo "缺少 $ZIP（先打包 deploy/voice-gateway/，见该目录 README）"; exit 1; }
[ -f "$LIB" ] || { echo "缺少 $LIB（与脚本同目录分发）"; exit 1; }

step() { printf '\n=== %s ===\n' "$*"; }

# ---- 面板地址：多网络逐个探测，用 GET /api/me 确认可达后再做任何写操作 ----
step "0/5 选择面板地址并校验 Key"
IPS=$(docker inspect panel --format '{{range .NetworkSettings.Networks}}{{println .IPAddress}}{{end}}')
[ -n "$IPS" ] || { echo "找不到 panel 容器或其网络地址"; exit 1; }
PANEL_IP=""
for ip in $IPS; do
  if curl -sf -m 3 -H "X-API-Key: $PANEL_KEY" "http://$ip:8000/api/me" >/dev/null 2>&1; then
    PANEL_IP="$ip"
    break
  fi
done
[ -n "$PANEL_IP" ] || { echo "Key 对面板 /api/me 不可用（检查 Key 有效性或网络）；候选地址：$IPS"; exit 1; }
API="http://$PANEL_IP:8000"
echo "面板: $API（容器直连，经 GET /api/me 确认）"

# ---- 流水线：ASR 绑子域 voice（流水线 id 仍是 voice-asr-prod），管理绑 voice-admin ----
step "1/5 创建/更新流水线"
create_pipe() { # slug sub listen_port upstream name
  local slug="$1" sub="$2" port="$3" upstream="$4" name="$5" pid body
  pid="$slug-prod"
  body=$(python3 "$LIB" pipeline-body "$slug" "$sub" "$port" "$upstream" "$name" "$BASE_DOMAIN")
  if ! curl -sf -X POST -H "X-API-Key: $PANEL_KEY" -H 'Content-Type: application/json' \
       -d "$body" "$API/api/pipelines" >/dev/null; then
    echo "  $pid 创建失败（检查 Key 是否有 pipeline:write）"; exit 1
  fi
  # 以面板返回的绑定域名为准核对（不靠本地拼字符串）
  BOUND_DOMAIN=$(curl -sf -H "X-API-Key: $PANEL_KEY" "$API/api/pipelines" \
    | BASE_PID="$pid" python3 -c '
import json, os, sys
pipes = json.load(sys.stdin)
if isinstance(pipes, dict): pipes = pipes.get("pipelines", pipes)
p = pipes.get(os.environ["BASE_PID"]) or {}
d = p.get("domain") or ", ".join(x.get("sub", "") + "." + x.get("root", "") for x in p.get("domains") or [])
print(d)' 2>/dev/null || true)
  echo "  $pid → 面板绑定域名: ${BOUND_DOMAIN:-<未取到，请人工核对>}"
  echo "$pid $BOUND_DOMAIN" >> "$HERE/.voice-setup-domains.tmp"
}

rm -f "$HERE/.voice-setup-domains.tmp"
create_pipe "voice-asr" "voice" "$ASR_LISTEN" "$UPSTREAM_IP:6016" "CapsWriter 语音 ASR 入口"
create_pipe "voice-admin" "voice-admin" "$ADMIN_LISTEN" "$UPSTREAM_IP:6017" "CapsWriter 网页管理"

ASR_DOMAIN=$(grep '^voice-asr-prod ' "$HERE/.voice-setup-domains.tmp" | head -1 | cut -d' ' -f2)
ADMIN_DOMAIN=$(grep '^voice-admin-prod ' "$HERE/.voice-setup-domains.tmp" | head -1 | cut -d' ' -f2)
rm -f "$HERE/.voice-setup-domains.tmp"
case "$ASR_DOMAIN" in voice."$BASE_DOMAIN") ;; *) echo "  ⚠️ ASR 绑定域名不是 voice.$BASE_DOMAIN（实际: $ASR_DOMAIN），请核对后再继续"; ;; esac
case "$ADMIN_DOMAIN" in voice-admin."$BASE_DOMAIN") ;; *) echo "  ⚠️ 管理绑定域名不是 voice-admin.$BASE_DOMAIN（实际: $ADMIN_DOMAIN），请核对后再继续"; ;; esac

# ---- 上传与部署 ----
step "2/5 上传网关容器源码（同一份 zip）"
upload() { # pid → version
  local pid="$1" resp
  if ! resp=$(curl -sf -X POST -H "X-API-Key: $PANEL_KEY" -F "file=@$ZIP" "$API/api/pipelines/$pid/upload"); then
    echo "  $pid 上传失败（检查 Key 是否有 deploy 作用域）"; exit 1
  fi
  echo "$resp" | python3 -c 'import json, sys; print(json.load(sys.stdin)["version"])'
}
V_ASR=$(upload voice-asr-prod);   echo "  voice-asr-prod → 版本 $V_ASR"
V_ADM=$(upload voice-admin-prod); echo "  voice-admin-prod → 版本 $V_ADM"

step "3/5 部署并按成败标记判定"
deploy_and_wait() { # pid version
  local pid="$1" ver="$2" job resp verdict logtext
  if ! resp=$(curl -sf -X POST -H "X-API-Key: $PANEL_KEY" -H 'Content-Type: application/json' \
        -d "{\"version\": \"$ver\"}" "$API/api/pipelines/$pid/deploy"); then
    echo "  $pid 提交部署失败"; exit 1
  fi
  job=$(printf '%s' "$resp" | python3 -c 'import json, sys; print(json.load(sys.stdin)["job"])')
  verdict="running"
  for _ in $(seq 1 60); do
    sleep 3
    logtext=$(curl -sf -H "X-API-Key: $PANEL_KEY" "$API/logs/$job" 2>/dev/null | \
      python3 -c 'import json, sys
try: print(json.load(sys.stdin).get("text") or "")
except Exception: print("")' 2>/dev/null || printf '')
    verdict=$(printf '%s' "$logtext" | python3 "$LIB" deploy-verdict -)
    if [ "$verdict" != "running" ]; then
      break
    fi
  done
  if [ "$verdict" != "success" ]; then
    echo "  $pid 部署未成功（判定: $verdict，job $job）；错误摘要（不含密钥）："
    printf '%s\n' "$logtext" | grep -a "失败" | tail -5
    exit 1
  fi
  echo "  $pid 部署成功（job $job）"
}
deploy_and_wait voice-asr-prod "$V_ASR"
deploy_and_wait voice-admin-prod "$V_ADM"

# ---- 网关：机器门（列表格式，只改本流水线）+ 管理域名并入登录墙（从现值合并） ----
step "4/5 网关：机器门 + 管理域名过登录墙"
curl -sf -H "X-API-Key: $PANEL_KEY" "$API/api/gate" > "$HERE/.voice-setup-gate.json"
GATE_BODY=$(ASR_PID="voice-asr-prod" ADMIN_DOMAIN="$ADMIN_DOMAIN" PATHS="$GATE_PATHS" \
  python3 "$LIB" gate-body "$HERE/.voice-setup-gate.json" "$ASR_PID" "$ADMIN_DOMAIN" "$PATHS")
rm -f "$HERE/.voice-setup-gate.json"
if curl -sf -X POST -H "X-API-Key: $PANEL_KEY" -H 'Content-Type: application/json' \
     -d "$GATE_BODY" "$API/api/gate" >/dev/null; then
  echo "  机器门: voice-asr-prod paths=$GATE_PATHS（列表格式，不覆盖其他站点）"
  echo "  登录墙 domains += $ADMIN_DOMAIN（从现值合并）"
else
  echo "  网关配置失败（检查 Key 是否有 gate:write）"; exit 1
fi

# ---- 验证：本机 Caddy 回环；426 只证明上游是 WS 服务，真实识别需真机验收 ----
step "5/5 验证（本机 Caddy 回环，域名取自面板返回）"
sleep 2
code_nokey=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $ASR_DOMAIN" http://127.0.0.1/ || true)
code_key=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $ASR_DOMAIN" -H "X-API-Key: $PANEL_KEY" http://127.0.0.1/ || true)
echo "  无 Key → HTTP $code_nokey（期望 401）"
echo "  带 Key → HTTP $code_key"
echo "         403 = 还没在面板给这把 Key 勾选 voice-asr-prod 站点（见下）"
echo "         426 = 网关与上游链路已通；这不等于识别可用，仍需真机录音验收"
echo
echo "剩余人工步骤："
echo "  1. 面板 UI → API 密钥 → 编辑现有 Key → 勾选站点 voice-asr-prod（密钥不能管理密钥，脚本无权代做）"
echo "  2. 勾选后重跑本脚本（幂等）看第 5 步带 Key 是否变 426"
echo "  3. voice-admin 需先在 Windows 上部署管理进程 :6017，否则该域名 502"
echo "  4. 远程客户端把服务器地址填成 wss://$ASR_DOMAIN 并录入面板 Key，做真实录音验收"
