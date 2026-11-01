#!/usr/bin/env bash
# CapsWriter 语音入口一键配置（部署面板 API）
#
# 在「服务器 WSL」里运行：
#
#   PANEL_KEY=dpk_xxx bash voice-setup.sh
#
# 需要 Key 具备作用域：read + pipeline:write + deploy + gate:write。
# 本脚本不回显、不落盘 Key；临时文件用 mktemp 目录并在退出时清理；
# mkdir 互斥锁防止并发重复执行（只有取得锁的实例才能删锁，R04）。
# 所有输出（警告摘要、错误信息）先脱敏再落 stdout/stderr（R05）。
#
# 面板 API 一律携带 X-API-Key 认证头（R01）：面板全部 /api/* 路由都要求
# 已验证的 Key principal 或会话，读取请求（GET /api/pipelines、GET /api/gate）
# 不带 Key 必然 401。唯一不带 Key 的是对 Caddy 的无 Key 探测。
# 阶段九：认证头经 curl stdin（-H @-）注入，Key 不进外部进程 argv。
#
# 执行顺序（安全门先于任何可服务的代理，见复核清单 P0-03）：
#   1. 创建/更新流水线：ASR 流水线（id=voice-asr-prod，子域 voice）创建体
#      自带 apikey_gate /*，首次路由生成即带机器门；管理流水线
#      （id=voice-admin-prod，子域 voice-admin）不带
#   2. 人用网关前置条件检查（总开关开启、管理域名不在免过门清单），
#      然后把管理域名并入登录墙 domains（从现值合并，不覆盖其他配置）
#   3. 运行态验证两个门（部署代理之前）：语音域名无 Key 必须 401；
#      管理域名未登录必须 301/302 且 Location 指向登录门户 auth.<根域名>，
#      任意应用跳转不算（R08）。任一不满足即停止，不部署
#   4. 上传并部署两个网关容器；部署结束按「Caddy 重载失败独立阻断 +
#      末次终态」判定成败（R02），成功后核对容器运行状态、容器镜像
#      app-<pid>:<version> 与面板 current 版本（current 才是已部署版本，
#      latest 只是最新上传目录）
#   5. 终验：语音域名无 Key 401 / 带 Key 426（或 403=待面板授权站点）；
#      管理域名未登录 301/302 且指向登录门户；部署后网关配置读回复核
#
# 退出码：
#   0 = 全部自动可验项目达标（真实录音验收仍属现场步骤）
#   1 = 配置/核对被阻断（lib 判定：绑定错误、warnings、读回不一致等）
#   2 = 环境错误（缺文件、面板不可达、Key 无效、已有实例在运行）
#   3 = 部署完成但站点授权待办（需在面板 UI 给同一把现有 Key 勾选 voice-asr-prod）
#   4 = 安全门未生效（机器门未拦 / 登录墙未挡 / 网关前置条件不满足）
#   5 = 部署失败/超时或部署后核对失败
#
# 不做什么（需人工，脚本无权代做）：
#   - 给「现有的那把」Key 勾选站点授权（面板 UI → API 密钥 → 编辑 → 勾 voice-asr-prod；
#     密钥不能管理密钥，不得新建第二把 CapsWriter Key）
#   - Windows 管理端 :6017 的部署、防火墙与管理进程切 gateway 模式
#     （只能在第 3/5 步的管理域登录墙验证通过后进行）
#   - 普通 GET 的 401/426 只是辅助信号，真实 WS 握手与录音属现场验收

set -euo pipefail

BASE_DOMAIN="${BASE_DOMAIN:-faiz.dpdns.org}"
UPSTREAM_STATE_DIR="${UPSTREAM_STATE_DIR:-}"
ASR_LISTEN="${ASR_LISTEN:-8111}"
ADMIN_LISTEN="${ADMIN_LISTEN:-8112}"
GATE_PATHS='["/*"]'
ASR_PID="voice-asr-prod"
ADMIN_PID="voice-admin-prod"
ASR_DOMAIN="voice.$BASE_DOMAIN"
ADMIN_DOMAIN="voice-admin.$BASE_DOMAIN"
POLL_INTERVAL="${POLL_INTERVAL:-3}"   # 部署日志轮询间隔；测试可调小
CURL="curl --connect-timeout 2 -m 8 -sf"
HERE="$(cd "$(dirname "$0")" && pwd)"
# Git Bash 下 pwd 是 POSIX 形式（/e/...），python.exe 打不开；转成混合形式
# （E:/...，bash 与 Windows python 都能读）。WSL 无 cygpath，保持原样即可。
command -v cygpath >/dev/null 2>&1 && HERE="$(cygpath -m "$HERE")"
ZIP="${ZIP:-$HERE/voice-gateway.zip}"
LIB="$HERE/voice_setup_lib.py"
LOCK_DIR="$HERE/.voice-setup.lock"
OWNED_LOCK=""
TMP_DIR=""

: "${PANEL_KEY:?请以 PANEL_KEY=dpk_xxx bash $0 运行；Key 不会回显或落盘}"
if [ -z "$UPSTREAM_STATE_DIR" ]; then
  WIN_POWERSHELL=/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe
  [ -x "$WIN_POWERSHELL" ] || { echo '无法定位 Windows 服务目录，请通过 UPSTREAM_STATE_DIR 指定'; exit 2; }
  WIN_SERVER_DIR=$("$WIN_POWERSHELL" -NoProfile -Command '$p = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq "start_server.exe" -and $_.ExecutablePath } | Select-Object -ExpandProperty ExecutablePath -Unique); if ($p.Count -eq 1) { Split-Path -Parent $p[0] }' | tr -d '\r')
  [ -n "$WIN_SERVER_DIR" ] || { echo '无法唯一定位识别服务目录，请通过 UPSTREAM_STATE_DIR 指定'; exit 2; }
  UPSTREAM_STATE_DIR="$(wslpath -u "$WIN_SERVER_DIR")/gateway-endpoint"
fi
mkdir -p "$UPSTREAM_STATE_DIR"
[ -f "$ZIP" ] || { echo "缺少 $ZIP（先打包 deploy/voice-gateway/，见该目录 README）"; exit 2; }
[ -f "$LIB" ] || { echo "缺少 $LIB（与脚本同目录分发）"; exit 2; }

# R04：只有成功取得锁的实例才清理锁目录；未取得锁的实例绝不触碰它。
cleanup() {
  [ -n "$TMP_DIR" ] && rm -rf "$TMP_DIR"
  if [ -n "$OWNED_LOCK" ] && [ "$OWNED_LOCK" = "1" ]; then
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 2' INT TERM
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "已有 voice-setup 实例在运行（锁：$LOCK_DIR）；若确认无并发请删除该目录"; exit 2
fi
OWNED_LOCK=1
TMP_DIR=$(mktemp -d)
command -v cygpath >/dev/null 2>&1 && TMP_DIR="$(cygpath -m "$TMP_DIR")"

# A04：任何网络请求之前核验依赖工具存在且可解析——避免 PATH 指向非预期
# 的 curl/docker 时带着错误工具继续跑
for _tool in curl docker python3; do
  command -v "$_tool" >/dev/null 2>&1 || die 2 "缺少依赖命令：$_tool（检查 PATH 后重试）"
done
echo "  依赖检查：$(command -v curl)；$(command -v docker)；$(command -v python3)"

step() { printf '\n=== %s ===\n' "$*"; }
die() { local code="$1"; shift; echo "错误：$*" >&2; exit "$code"; }

# lib 子命令封装：非零即整体退出（lib 已打印 BLOCKED 原因）
libx() { python3 "$LIB" "$@" || die 1 "配置核对被阻断（见上方 BLOCKED 原因）"; }
# R05/A03：lib 输出（含异常文本）一律过脱敏再落控制台；已知 Key 经环境
# 变量传给 lib（不进 argv）；清洗失败输出固定摘要，绝不回传原文（无降级）
redact() { python3 "$LIB" redact -; }

# 阶段九：所有认证请求统一经 stdin 头注入（curl -H @-，curl ≥7.55）——
# Key 绝不展开进外部进程 argv（secretCare：Key 不放 argv/日志/聊天）。
# stdin 只承载头行：-d "$body"（argv 体）与 -F "file=@…"（文件体）不抢
# stdin，两者可并存；不使用 set -x，不回显值。
key_hdr() { printf 'X-API-Key: %s\n' "$PANEL_KEY"; }

# ---- 0. 面板地址：多网络逐个探测，GET /api/me 确认可达后再做任何写操作 ----
step "0/5 选择面板地址并校验 Key"
IPS=$(docker inspect panel --format '{{range .NetworkSettings.Networks}}{{println .IPAddress}}{{end}}')
[ -n "$IPS" ] || die 2 "找不到 panel 容器或其网络地址"
PANEL_IP=""
for ip in $IPS; do
  if { key_hdr | $CURL -m 3 -H @- "http://$ip:8000/api/me"; } >/dev/null 2>&1; then
    PANEL_IP="$ip"; break
  fi
done
[ -n "$PANEL_IP" ] || die 2 "Key 对面板 /api/me 不可用（检查 Key 有效性或网络）；候选地址：$(echo "$IPS" | tr '\n' ' ')"
API="http://$PANEL_IP:8000"
echo "面板: $API（容器直连，经 GET /api/me 确认）"

# R01：面板全部 /api/* 请求（含 GET）统一带 Key（阶段九：Key 经 stdin 头
# 注入，不进 argv）。面板路由全部调用 require_user，无凭据的读取请求必然
# 401——不能用裸 curl 读面板 API。
api_get() { # path → 认证 GET；失败即阻断（面板读取失败无从核对）
  local path="$1"
  { key_hdr | $CURL -H @- "$API$path"; } \
    || die 1 "面板 GET $path 失败（HTTP 错误或网络不可达；检查 Key 的 read 作用域）"
}

# 写操作封装：POST 并立即解析 ok/warnings（HTTP 200 不代表生效，P0-04）
post_json() { # path body what
  local path="$1" body="$2" what="$3" resp
  resp=$({ key_hdr | $CURL -X POST -H @- -H 'Content-Type: application/json' \
    -d "$body" "$API$path"; }) || die 1 "$what：请求失败（网络或 HTTP 错误）"
  printf '%s' "$resp" | python3 "$LIB" check-write - "$what" >/dev/null \
    || die 1 "$what：响应未通过 ok/warnings 核对（见上方 BLOCKED 原因）"
}

# ---- 1. 流水线（ASR 创建体自带机器门） ----
step "1/5 创建/更新流水线（ASR 自带机器门）"
get_pipelines() { api_get /api/pipelines; }

asr_body=$(python3 "$LIB" pipeline-body voice-asr voice "$ASR_LISTEN" "$UPSTREAM_STATE_DIR" \
  "CapsWriter 语音 ASR 入口" "$BASE_DOMAIN" "$GATE_PATHS")
admin_body=$(python3 "$LIB" pipeline-body voice-admin voice-admin "$ADMIN_LISTEN" "$UPSTREAM_STATE_DIR" \
  "CapsWriter 网页管理" "$BASE_DOMAIN" "")

# 预检：同 ID 已存在但不是本方案签名/绑定不符/停用 → lib 在此阻断，不会覆盖
get_pipelines > "$TMP_DIR/pipes-before.json"
libx parse-pipelines "$TMP_DIR/pipes-before.json" "$ASR_PID" "$ASR_DOMAIN" >/dev/null
libx parse-pipelines "$TMP_DIR/pipes-before.json" "$ADMIN_PID" "$ADMIN_DOMAIN" >/dev/null
echo "  预检通过（无冲突流水线）"

post_json /api/pipelines "$asr_body" "创建/更新 $ASR_PID"
post_json /api/pipelines "$admin_body" "创建/更新 $ADMIN_PID"

# 保存后立即读回核对（含启用状态与绑定域名；读回不一致即停止）
get_pipelines > "$TMP_DIR/pipes-after.json"
libx parse-pipelines "$TMP_DIR/pipes-after.json" "$ASR_PID" "$ASR_DOMAIN" > "$TMP_DIR/asr-check.json"
libx parse-pipelines "$TMP_DIR/pipes-after.json" "$ADMIN_PID" "$ADMIN_DOMAIN" > "$TMP_DIR/admin-check.json"
python3 - <<PY || die 1 "流水线读回核对未通过"
import json, sys
asr = json.load(open(r"$TMP_DIR/asr-check.json"))
adm = json.load(open(r"$TMP_DIR/admin-check.json"))
problems = []
if not asr["exists"]:
    problems.append("$ASR_PID 保存后未出现在面板列表")
elif asr["domain"] != "$ASR_DOMAIN":
    problems.append(f"$ASR_PID 绑定 {asr['domain']} ≠ 预期 $ASR_DOMAIN")
if not adm["exists"]:
    problems.append("$ADMIN_PID 保存后未出现在面板列表")
elif adm["domain"] != "$ADMIN_DOMAIN":
    problems.append(f"$ADMIN_PID 绑定 {adm['domain']} ≠ 预期 $ADMIN_DOMAIN")
if problems:
    print("; ".join(problems), file=sys.stderr); sys.exit(1)
print("  ASR: $ASR_PID → $ASR_DOMAIN；管理: $ADMIN_PID → $ADMIN_DOMAIN（读回一致）")
PY

# ---- 2. 人用网关：前置条件 + 并入登录墙 + 读回 ----
step "2/5 管理域名并入登录墙（先查前置条件）"
api_get /api/gate > "$TMP_DIR/gate-before.json"
# 前置条件失败 → lib 退出 5 → 直接映射为退出码 4（不走 libx，避免被吞成 1）
if ! python3 "$LIB" gate-preconditions "$TMP_DIR/gate-before.json" "$ADMIN_DOMAIN" >/dev/null; then
  die 4 "人用网关前置条件不满足（总开关关闭或管理域名在免过门清单）——已停止，不擅自改写其他配置"
fi
gate_body=$(python3 "$LIB" gate-body "$TMP_DIR/gate-before.json" "$ADMIN_DOMAIN")
post_json /api/gate "$gate_body" "更新登录墙 domains"
api_get /api/gate > "$TMP_DIR/gate-after.json"
python3 - <<PY || die 1 "登录墙读回核对未通过"
import json, sys
g = json.load(open(r"$TMP_DIR/gate-after.json"))
if "$ADMIN_DOMAIN" not in (g.get("domains") or []):
    print("读回的 domains 不含 $ADMIN_DOMAIN", file=sys.stderr); sys.exit(1)
print("  登录墙 domains 已含 $ADMIN_DOMAIN（读回一致）")
PY

# ---- 3. 运行态验证安全门（部署代理之前；本机 Caddy 回环） ----
step "3/5 运行态验证安全门（部署代理之前）"
# probe <host> [with-key] → stdout 两行：状态码、Location（无则空）；人读信息走 stderr
probe() {
  local host="$1" key="${2:-}" out
  if [ "$key" = "with-key" ]; then
    # 阶段九：Key 经 stdin 头注入，不进 argv
    out=$(key_hdr | curl --connect-timeout 2 -m 5 -s -o /dev/null \
      -w '%{http_code}\n%{redirect_url}' -H "Host: $host" -H @- http://127.0.0.1/ || true)
  else
    out=$(curl --connect-timeout 2 -m 5 -s -o /dev/null \
      -w '%{http_code}\n%{redirect_url}' -H "Host: $host" http://127.0.0.1/ || true)
  fi
  printf '%s\n' "$out"
}
code=$(probe "$ASR_DOMAIN" | head -1)
echo "  语音域名无 Key → HTTP $code（期望 401）" >&2
[ "$code" = "401" ] || die 4 "语音域名无 Key 未被机器门拦截（HTTP $code）——安全门未生效，不部署代理"
admin_probe=$(probe "$ADMIN_DOMAIN")
code=$(printf '%s\n' "$admin_probe" | head -1)
loc=$(printf '%s\n' "$admin_probe" | tail -n +2)
echo "  管理域名未登录 → HTTP $code → ${loc:-<无 Location>}" >&2
# R08：只认 301/302 + Location 指向登录门户 auth.<根域名>；任意跳转不算
if ! python3 "$LIB" check-portal "$code" "$loc" "$ADMIN_DOMAIN" >/dev/null 2>&1; then
  die 4 "管理域名未登录未被登录门户拦截（HTTP $code → ${loc:-无}）——人用网关未生效，不部署代理"
fi
echo "  两个安全门运行态生效，开始部署代理"

# ---- 4. 上传与部署（Caddy 重载失败独立阻断；成功后核对容器/镜像/current） ----
step "4/5 上传并部署网关容器"
upload() { # pid → version
  local pid="$1" resp
  # 阶段九：Key 经 stdin 头注入（-F 文件体不抢 stdin），不进 argv
  if ! resp=$(key_hdr | curl --connect-timeout 2 -m 60 -sf -X POST -H @- \
    -F "file=@$ZIP" "$API/api/pipelines/$pid/upload"); then
    die 5 "$pid 上传失败（检查 Key 是否有 deploy 作用域）"
  fi
  printf '%s' "$resp" | python3 -c 'import json, sys
d = json.load(sys.stdin)
print(d.get("version") or "")' | grep -q . || die 5 "$pid 上传响应缺少 version"
  printf '%s' "$resp" | python3 -c 'import json, sys; print(json.load(sys.stdin)["version"])'
}
deploy_and_wait() { # pid version
  local pid="$1" ver="$2" job resp verdict logtext fails=0 i
  # 阶段九：Key 经 stdin 头注入，不进 argv
  if ! resp=$(key_hdr | curl --connect-timeout 2 -m 8 -sf -X POST -H @- \
        -H 'Content-Type: application/json' -d "{\"version\": \"$ver\"}" "$API/api/pipelines/$pid/deploy"); then
    die 5 "$pid 提交部署失败"
  fi
  job=$(printf '%s' "$resp" | python3 -c 'import json, sys
d = json.load(sys.stdin)
print(d.get("job") or "")' )
  [ -n "$job" ] || die 5 "$pid 部署响应缺少任务 id"
  verdict="running"
  for i in $(seq 1 60); do
    sleep "$POLL_INTERVAL"
    # 阶段九：响应是 JSON：必须解析出 text 字段（原始 JSON 里中文是 \uXXXX 转义，
    # 直接拿原文匹配「成功 =====」永远匹配不上）；Key 经 stdin 头注入
    if rawlog=$(key_hdr | curl --connect-timeout 2 -m 8 -sf -H @- "$API/api/logs/$job" 2>/dev/null); then
      fails=0
      logtext=$(printf '%s' "$rawlog" | python3 -c 'import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get("text") or "")
except Exception:
    print("")')
    else
      fails=$((fails + 1))
      if [ "$fails" -ge 5 ]; then
        # 日志接口连续不可读：查容器最终状态后报告，不无限等待
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^app-$pid\$"; then
          die 5 "$pid 部署日志不可读，但容器在运行；请到面板核对任务 $job 与容器状态"
        fi
        die 5 "$pid 部署日志不可读且容器未运行（任务 $job）"
      fi
      continue
    fi
    verdict=$(printf '%s' "$logtext" | python3 "$LIB" deploy-health -)
    if [ "$verdict" != "running" ]; then break; fi
  done
  if [ "$verdict" != "success" ]; then
    echo "  $pid 部署未成功（判定: $verdict，任务 $job）；错误摘要："
    # R05：摘要先脱敏再输出
    printf '%s\n' "$logtext" | grep -a "失败\|错误" | tail -5 | redact || true
    exit 5
  fi
  # 部署成功标记 ≠ 真的成功（R02）：核对容器运行 + 镜像标签 + 面板 current
  if ! docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null | grep "^app-$pid " | grep -q "Up"; then
    die 5 "$pid 日志报成功但容器未运行（可能 Caddy 重载失败等）；请到面板核对"
  fi
  if ! docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null | grep -q "^app-$pid app-$pid:$ver\$"; then
    die 5 "$pid 容器在运行但镜像不是 app-$pid:$ver（旧容器未替换）"
  fi
  get_pipelines > "$TMP_DIR/pipes-deployed.json"
  printf '%s' "$(cat "$TMP_DIR/pipes-deployed.json")" | libx check-current-version - "$pid" "$ver" >/dev/null \
    || die 1 "$pid 面板当前版本与部署版本不一致（见上方 BLOCKED 原因）"
  echo "  $pid 部署成功：容器运行、镜像 app-$pid:$ver、面板 current = $ver"
}
V_ASR=$(upload "$ASR_PID");   echo "  $ASR_PID → 版本 $V_ASR"
V_ADM=$(upload "$ADMIN_PID"); echo "  $ADMIN_PID → 版本 $V_ADM"
deploy_and_wait "$ASR_PID" "$V_ASR"
deploy_and_wait "$ADMIN_PID" "$V_ADM"

# ---- 5. 终验（决定退出码） ----
step "5/5 终验"
sleep 2
code_nokey=$(curl --connect-timeout 2 -m 5 -s -o /dev/null -w '%{http_code}' \
  -H "Host: $ASR_DOMAIN" http://127.0.0.1/ || true)
# 阶段九：Key 经 stdin 头注入，不进 argv
code_key=$(key_hdr | curl --connect-timeout 2 -m 5 -s -o /dev/null -w '%{http_code}' \
  -H "Host: $ASR_DOMAIN" -H @- http://127.0.0.1/ || true)
admin_probe=$(probe "$ADMIN_DOMAIN")
code_admin=$(printf '%s\n' "$admin_probe" | head -1)
loc_admin=$(printf '%s\n' "$admin_probe" | tail -n +2)
echo "  语音域名无 Key → HTTP $code_nokey（期望 401）"
echo "  语音域名带 Key → HTTP $code_key（426=链路通；403=待站点授权）"
echo "  管理域名未登录 → HTTP $code_admin → ${loc_admin:-<无 Location>}（期望 301/302 → 登录门户）"

[ "$code_nokey" = "401" ] || die 4 "语音域名无 Key 未被拦截（HTTP $code_nokey）"
# R08：终验同样要求 Location 指向登录门户
if ! python3 "$LIB" check-portal "$code_admin" "$loc_admin" "$ADMIN_DOMAIN" >/dev/null 2>&1; then
  die 4 "管理域名未登录未被登录门户拦截（HTTP $code_admin → ${loc_admin:-无}）"
fi
# R02：部署后网关配置读回复核（enabled/domains/exempt 不因部署被改掉）
api_get /api/gate > "$TMP_DIR/gate-final.json"
libx gate-verify "$TMP_DIR/gate-final.json" "$ADMIN_DOMAIN" >/dev/null \
  || die 1 "部署后人用网关配置复核未通过（见上方 BLOCKED 原因）"

case "$code_key" in
  426)
    echo
    echo "自动可验项目全部达标（exit 0）。注意：426 只证明网关与上游链路通，"
    echo "真实 WS 握手与录音、管理页已登录操作属现场验收；便携包仍待重打。"
    exit 0
    ;;
  403)
    echo
    echo "部署完成，但站点授权待办（exit 3）：请在面板 UI → API 密钥 → 编辑"
    echo "现有 Key → 勾选站点 voice-asr-prod，然后重跑本脚本。"
    echo "不得新建第二把 CapsWriter Key。"
    exit 3
    ;;
  *)
    die 1 "带 Key 访问得到意外状态 HTTP $code_key（401=Key 无效；502/000=链路断；200=异常放行）"
    ;;
esac
