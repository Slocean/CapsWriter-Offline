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
#   1. 创建/更新流水线 voice-asr-prod   → 网关容器 → Windows ASR :6016
#   2. 创建/更新流水线 voice-admin-prod → 网关容器 → Windows 管理端 :6017
#      （两者用同一份 deploy/voice-gateway.zip，仅构建参数不同）
#   3. 上传并部署两个流水线
#   4. 网关：voice-asr-prod 开机器门（路径 /*）；
#      voice-admin 域名并入登录墙 domains
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
ZIP="${ZIP:-$(cd "$(dirname "$0")" && pwd)/voice-gateway.zip}"

: "${PANEL_KEY:?请以 PANEL_KEY=dpk_xxx bash $0 运行；Key 不会回显或落盘}"
[ -f "$ZIP" ] || { echo "缺少 $ZIP（先打包 deploy/voice-gateway/，见该目录 README）"; exit 1; }

PANEL_IP=$(docker inspect panel --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' | head -1)
[ -n "$PANEL_IP" ] || { echo "找不到 panel 容器"; exit 1; }
API="http://$PANEL_IP:8000"
step() { printf '\n=== %s ===\n' "$*"; }

step "0/5 校验 Key"
if ! curl -sf -H "X-API-Key: $PANEL_KEY" "$API/api/me" >/dev/null; then
  echo "Key 无效或缺 read 作用域"; exit 1
fi
echo "Key 有效（面板 $API）"

step "1/5 创建/更新流水线"
create_pipe() { # slug listen_port upstream name
  local slug="$1" port="$2" upstream="$3" name="$4" pid body
  pid="$slug-prod"
  body=$(UPSTREAM="$upstream" LISTEN="$port" NAME="$name" SLUG="$slug" BASE_DOMAIN="$BASE_DOMAIN" python3 - <<'PY'
import json, os
print(json.dumps({
    "name": os.environ["NAME"],
    "slug": os.environ["SLUG"],
    "env": "prod",
    "type": "api",
    "port": int(os.environ["LISTEN"]),
    "note": "CapsWriter voice gateway",
    "domains": [{"root": os.environ["BASE_DOMAIN"], "sub": os.environ["SLUG"], "bare": False}],
    "build_args": {"UPSTREAM": os.environ["UPSTREAM"], "LISTEN": os.environ["LISTEN"]},
    "container_env": {},
    "api_pipeline": "",
    "auto_db": False,
}, ensure_ascii=False))
PY
)
  if curl -sf -X POST -H "X-API-Key: $PANEL_KEY" -H 'Content-Type: application/json' \
       -d "$body" "$API/api/pipelines" >/dev/null; then
    echo "  $pid OK（上游 $upstream）"
  else
    echo "  $pid 创建失败（检查 Key 是否有 pipeline:write）"; exit 1
  fi
}
create_pipe "voice-asr" "$ASR_LISTEN" "$UPSTREAM_IP:6016" "CapsWriter 语音 ASR 入口"
create_pipe "voice-admin" "$ADMIN_LISTEN" "$UPSTREAM_IP:6017" "CapsWriter 网页管理"

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

step "3/5 部署并等待完成"
deploy_and_wait() { # pid version
  local pid="$1" ver="$2" job resp done_flag text i
  if ! resp=$(curl -sf -X POST -H "X-API-Key: $PANEL_KEY" -H 'Content-Type: application/json' \
        -d "{\"version\": \"$ver\"}" "$API/api/pipelines/$pid/deploy"); then
    echo "  $pid 提交部署失败"; exit 1
  fi
  job=$(printf '%s' "$resp" | python3 -c 'import json, sys; print(json.load(sys.stdin)["job"])')
  for i in $(seq 1 40); do
    sleep 3
    text=$(curl -sf -H "X-API-Key: $PANEL_KEY" "$API/logs/$job" 2>/dev/null || printf '{}')
    done_flag=$(printf '%s' "$text" | python3 -c 'import json, sys
try: print("1" if json.load(sys.stdin).get("done") else "0")
except Exception: print("0")')
    if [ "$done_flag" = "1" ]; then
      echo "  $pid 部署完成（job $job）"
      return 0
    fi
  done
  echo "  $pid 部署超时，请到面板查看部署日志（job $job）"; exit 1
}
deploy_and_wait voice-asr-prod "$V_ASR"
deploy_and_wait voice-admin-prod "$V_ADM"

step "4/5 网关：机器门 + 管理域名过登录墙"
gate_json=$(curl -sf -H "X-API-Key: $PANEL_KEY" "$API/api/gate")
merged=$(GATE_JSON="$gate_json" BASE_DOMAIN="$BASE_DOMAIN" python3 - <<'PY'
import json, os
g = json.loads(os.environ["GATE_JSON"])
gates = g.get("apikey_gates") or {}
gates["voice-asr-prod"] = {"enabled": True, "paths": ["/*"]}
g["apikey_gates"] = gates
dom = "voice-admin." + os.environ["BASE_DOMAIN"]
g["domains"] = list(dict.fromkeys(list(g.get("domains") or []) + [dom]))
print(json.dumps(g, ensure_ascii=False))
PY
)
if curl -sf -X POST -H "X-API-Key: $PANEL_KEY" -H 'Content-Type: application/json' \
     -d "$merged" "$API/api/gate" >/dev/null; then
  echo "  机器门: voice-asr-prod /*；登录墙 domains += voice-admin.$BASE_DOMAIN"
else
  echo "  网关配置失败（检查 Key 是否有 gate:write）"; exit 1
fi

step "5/5 验证（本机 Caddy 回环）"
sleep 2
code_nokey=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: voice.$BASE_DOMAIN" http://127.0.0.1/ || true)
code_key=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: voice.$BASE_DOMAIN" -H "X-API-Key: $PANEL_KEY" http://127.0.0.1/ || true)
echo "  无 Key → HTTP $code_nokey（期望 401）"
echo "  带 Key → HTTP $code_key"
echo "         403 = 还没在面板给这把 Key 勾选 voice-asr-prod 站点（见下）"
echo "         426 = 全链路已通（ASR 的 WS 端口对普通 GET 的正确回应）"
echo
echo "剩余人工步骤："
echo "  1. 面板 UI → API 密钥 → 编辑现有 Key → 勾选站点 voice-asr-prod（密钥不能管理密钥，脚本无权代做）"
echo "  2. 勾选后重跑本脚本（幂等）看第 5 步带 Key 是否变 426"
echo "  3. voice-admin 需先在 Windows 上部署管理进程 :6017，否则该域名 502"
