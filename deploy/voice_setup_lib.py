# coding: utf-8
"""
voice-setup.sh 的 JSON 构造/解析/判定逻辑（独立模块，便于离线单测）

不接触网络、不接触 Key；bash 脚本通过子命令调用（见 main）。

对应《单密钥接入全面复核与修复清单-2026-09-29》与《单密钥接入修复独立验收-2026-09-30》的修法：
- P0-01  GET /api/pipelines 是数组，按 id 精确匹配；核对绑定域名/启用状态；
- P0-03  ASR 流水线创建体自带 apikey_gate，首次路由生成即带机器门；
- P0-04  POST /api/pipelines、/api/gate 的 200 响应必须解析 ok/id/warnings，
         warnings 视为阻断；部署后核对面板当前版本；
- P0-05  人用网关前置条件：gate.enabled 为真、管理域名不在 exempt 内；
- P1-11  deploy_verdict 以「末次终态」为准，成功标记后出现的失败不会被压过；
- R01    面板 GET 读取（/api/pipelines、/api/gate）同样要求认证——认证头的
         携带由 bash 侧统一封装，这里负责响应形状校验；
- R02    部署后版本核对只认 current（latest 只是最新上传目录，与已部署版本
         无关）；Caddy 重载失败是独立阻断信号——面板在重载失败后仍会写
         「===== deploy 成功 =====」，末次终态判定会被它骗过；
- R05    所有会到达 stdout/stderr 的文本（warnings、错误摘要、截断响应）
         先过 redact_secrets，密钥样式值不落输出；
- R08    登录墙判定不看裸状态码：301/302 必须携带指向登录门户
         （auth.<根域名>）的 Location 才算被墙拦截，任意应用跳转不算。
"""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.parse import urlsplit

SUCCESS_MARKER = "成功 ====="
FAILURE_MARKER = "失败："

# R02：面板 deploy_service 在 Caddy 重载失败后仍写成功标记（实测契约：
# 「⚠️  部署本身完成了，但 Caddy 重载失败：{err}」+「===== deploy 成功 =====」）
CADDY_FAILURE_MARKERS = ("caddy 重载失败", "caddy reload failed")

# 我方流水线的身份签名：同 ID 已存在但不是本方案的配置时，拒绝覆盖
PIPELINE_NOTE = "CapsWriter voice gateway"


class SetupError(Exception):
    """阻断性错误：bash 侧捕获后打印并以非零码退出"""


# ---------- 敏感值脱敏（R05/A03） ----------

# 文本形式：请求头/查询参数/Bearer
_TEXT_REDACTIONS = [
    (re.compile(r'(?i)(x-api-key\s*[:=]\s*)[A-Za-z0-9._\-]{8,}'), r'\1<redacted>'),
    (re.compile(r'(?i)(api_key=)[A-Za-z0-9._\-]{8,}'), r'\1<redacted>'),
    (re.compile(r'(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}'), r'\1<redacted>'),
    (re.compile(r'(?i)(token=)[A-Za-z0-9._\-]{8,}'), r'\1<redacted>'),
]

# 结构化 JSON / Python 字面量形式（A03 反例：{"api_key": "..."} 原样漏出）
_JSON_REDACTIONS = [
    (re.compile(r'(?i)("(?:x-api-key|api-key|api_key|apikey|server_token|token|authorization|password|secret)"\s*:\s*)"[^"]*"'),
     r'\1"<redacted>"'),
    (re.compile(r"(?i)('(?:x-api-key|api-key|api_key|apikey|server_token|token|authorization|password|secret)'\s*:\s*)'[^']*'"),
     r"\1'<redacted>'"),
    (re.compile(r'(?i)\b(api_key|server_token)\s*=\s*[\'"][^\'"]*[\'"]'),
     r"\1='<redacted>'"),
]

REDACTED_PLACEHOLDER = '<redacted>'


def known_secrets() -> list:
    """
    本次运行已知的秘密值（A03）：当前只取 PANEL_KEY 环境变量。
    精确替换必须先于任何正则/截断，且绝不经过 argv/日志传递 Key。
    """
    out = []
    for name in ('PANEL_KEY',):
        value = (os.environ.get(name) or '').strip()
        if value:
            out.append(value)
    return out


def redact_secrets(text: str, secrets: list | None = None) -> str:
    """
    输出前统一脱敏：已知 Key 精确替换（最先做）→ JSON/字面量敏感字段 →
    文本形式。清洗必须发生在任何截断之前。
    """
    out = str(text or "")
    for secret in (secrets if secrets is not None else known_secrets()):
        if secret:
            out = out.replace(secret, REDACTED_PLACEHOLDER)
    for pattern, repl in _JSON_REDACTIONS:
        out = pattern.sub(repl, out)
    for pattern, repl in _TEXT_REDACTIONS:
        out = pattern.sub(repl, out)
    return out


def safe_summary(text: str, limit: int = 200) -> str:
    """脱敏后再截断；若已知 Key 仍残留在结果中（清洗失败），输出固定摘要"""
    redacted = redact_secrets(text)
    for secret in known_secrets():
        if secret and secret in redacted:
            return '（内容含未脱敏的敏感值，详情已省略；请到面板核对任务日志）'
    return redacted[:limit]


# ---------- 流水线 ----------

def pipeline_body(slug: str, sub: str, port: int, endpoint_dir: str, name: str,
                  base_domain: str, machine_gate_paths: list[str] | None = None) -> dict:
    """
    流水线创建/更新请求体。

    pid 由面板按 slug-env 生成（voice-asr → voice-asr-prod）；子域独立于 slug
    （ASR 绑 voice 而不是 voice-asr）。

    machine_gate_paths 非空时，创建体自带 apikey_gate —— 面板保存后重新生成
    Caddyfile 时该站点即刻带机器门，不存在「先可服务、后补门」的间隙（P0-03）。
    """
    slug = (slug or "").strip()
    sub = (sub or "").strip()
    if not slug or not sub:
        raise SetupError("slug 与 sub 均不能为空")
    if not endpoint_dir.startswith('/') or any(c in endpoint_dir for c in ' \t\r\n:'):
        raise SetupError("网关状态目录必须是无空格的 WSL 绝对路径")
    body = {
        "name": name or slug,
        "slug": slug,
        "env": "prod",
        "type": "api",
        "port": int(port),
        "note": PIPELINE_NOTE,
        "domains": [{"root": base_domain, "sub": sub, "bare": False}],
        "build_args": {"ENDPOINT_ROLE": "admin" if slug == "voice-admin" else "asr", "LISTEN": str(port)},
        "container_volumes": [f"{endpoint_dir}:/capswriter-endpoint:ro"],
        "container_env": {},
        "api_pipeline": "",
        "auto_db": False,
    }
    if machine_gate_paths:
        body["apikey_gate"] = {"enabled": True, "paths": list(machine_gate_paths)}
    return body


def parse_pipeline_list(text: str) -> dict[str, dict]:
    """GET /api/pipelines（数组）→ {id: entry}；形状不对即抛错（P0-01）"""
    try:
        data = json.loads(text) if (text or "").strip() else None
    except json.JSONDecodeError as e:
        raise SetupError(f"流水线列表不是合法 JSON: {e}") from e
    if not isinstance(data, list):
        raise SetupError("GET /api/pipelines 应返回数组（现网 list_pipelines 形状）")
    out: dict[str, dict] = {}
    for entry in data:
        if not isinstance(entry, dict) or not entry.get("id"):
            raise SetupError("流水线列表项缺少 id")
        pid = entry["id"]
        if pid in out:
            raise SetupError(f"流水线列表出现重复 id: {pid}")
        out[pid] = entry
    return out


def check_pipeline_entry(entry: dict | None, pid: str, expected_domain: str) -> str:
    """
    核对一条流水线（P0-01/P0-04）：

    - 不存在 → 新建（返回 ''）；
    - 存在但不是本方案的签名（note/type 不符）→ 拒绝覆盖；
    - 存在且停用 → 拒绝（启用属人工决定）；
    - 绑定域名与预期不符 → 拒绝；
    - 全部相符 → 返回实际绑定域名。
    """
    if entry is None:
        return ""
    note = str(entry.get("note") or "")
    if entry.get("type") != "api" or note != PIPELINE_NOTE:
        raise SetupError(
            f"流水线 {pid} 已存在且不是本方案创建的（note={note!r}, type={entry.get('type')!r}）；"
            f"为避免覆盖他人配置，请先在面板人工处理该流水线")
    if entry.get("enabled") is False:
        raise SetupError(f"流水线 {pid} 处于停用状态；请先在面板启用，或人工处理后再运行")
    domains = [str(d) for d in (entry.get("domains") or [])]
    if expected_domain not in domains:
        raise SetupError(
            f"流水线 {pid} 的绑定域名 {domains or '<空>'} 不含预期的 {expected_domain}；"
            f"拒绝在错误绑定上继续")
    return domains[0] if domains else expected_domain


# ---------- 网关配置 ----------

def gate_preconditions(gate_json: str | dict, admin_domain: str) -> dict:
    """
    人用网关前置条件检查（P0-05）。返回 {'enabled': bool, 'exempt': {...}, ...}。

    - gate.enabled 必须为真，否则管理域名加进 domains 也不会过门；
    - admin_domain 出现在 exempt（免过门）清单里 → 加入也直通。
    两者任一不满足由调用方停止并报告，不擅自改写用户其他配置。
    """
    if isinstance(gate_json, str):
        gate = json.loads(gate_json) if (gate_json or "").strip() else {}
    else:
        gate = gate_json
    if not isinstance(gate, dict):
        raise SetupError("GET /api/gate 应返回 JSON 对象")
    exempt = gate.get("exempt") or {}
    if not isinstance(exempt, dict):
        exempt = {}
    return {
        "enabled": bool(gate.get("enabled", False)),
        "exempt_domains": sorted(str(k) for k in exempt.keys()),
        "admin_domain_exempt": admin_domain in exempt,
    }


def gate_body(gate_json: str | dict, admin_domain: str) -> dict:
    """
    POST /api/gate 请求体（PATCH 语义：未提交字段保留现值）。

    - domains：现值 + admin_domain（幂等去重），不覆盖用户已有配置；
    - 机器门已改为随流水线创建体提交（P0-03），这里不再携带 apikey_gates，
      避免与流水线保存双写。
    """
    if isinstance(gate_json, str):
        gate = json.loads(gate_json) if (gate_json or "").strip() else {}
    else:
        gate = gate_json
    if not isinstance(gate, dict):
        raise SetupError("GET /api/gate 应返回 JSON 对象")
    domains = [str(d) for d in (gate.get("domains") or []) if d]
    if admin_domain not in domains:
        domains.append(admin_domain)
    return {"domains": domains}


def gate_verify(gate_json: str | dict, admin_domain: str) -> dict:
    """
    部署后网关配置复核（R02/R08）：enabled 仍为真、管理域名仍在 domains、
    且不在 exempt。前置条件字段缺失/形状不对同样阻断。
    """
    result = gate_preconditions(gate_json, admin_domain)
    if isinstance(gate_json, str):
        gate = json.loads(gate_json) if (gate_json or "").strip() else {}
    else:
        gate = gate_json
    if not isinstance(gate, dict):
        raise SetupError("GET /api/gate 应返回 JSON 对象")
    domains = [str(d) for d in (gate.get("domains") or [])]
    result["admin_domain_present"] = admin_domain in domains
    return result


# ---------- 登录门户校验（R08） ----------

def check_auth_portal(code: str | int, location: str, admin_domain: str,
                      auth_sub: str = "auth") -> str:
    """
    判定「管理域名未登录被登录墙拦截」：

    - 状态码必须是 301/302；
    - Location 必须指向同根域名的登录门户 auth.<根域名>（面板固定契约：
      render.py 对所有根域名生成 auth.<root>，Authelia forward_auth 跳转
      落在该门户上）。任意应用跳转/规范化跳转不能算被墙拦截。
    通过返回门户 host，否则抛 SetupError。
    """
    code = str(code or "").strip()
    if code not in ("301", "302"):
        raise SetupError(f"管理域名未登录应被重定向到登录门户，实际 HTTP {code or '<空>'}")
    loc = str(location or "").strip()
    if not loc:
        raise SetupError("管理域名返回 301/302 但缺少 Location，不能证明是登录墙拦截")
    parsed = urlsplit(loc if "://" in loc else f"http://{loc}")
    host = (parsed.hostname or "").lower()
    labels = admin_domain.split(".")
    root = ".".join(labels[1:]) if len(labels) >= 3 else admin_domain
    expected = f"{auth_sub}.{root}".lower()
    if host != expected:
        raise SetupError(
            f"重定向目标 {host or '<空>'} 不是登录门户 {expected}；"
            f"应用自身的跳转不能当作登录墙已生效")
    return expected


# ---------- 响应与部署判定 ----------

def check_write_response(payload: str | dict, what: str) -> dict:
    """
    解析面板写操作响应（P0-04）：HTTP 200 不代表生效。

    - 非 JSON / ok 非 true → 阻断；
    - warnings 非空（Caddy/Authelia 应用失败等）→ 阻断并展示（脱敏后）；
    - 通过 → 返回解析后的对象。
    """
    if isinstance(payload, str):
        try:
            data = json.loads(payload) if payload.strip() else None
        except json.JSONDecodeError as e:
            raise SetupError(f"{what}: 响应不是合法 JSON: {e}") from e
    else:
        data = payload
    if not isinstance(data, dict) or data.get("ok") is not True:
        # A03：先脱敏后截断；已知 Key 残留时输出固定摘要
        raise SetupError(f"{what}: 响应缺少 ok:true（{safe_summary(json.dumps(data, ensure_ascii=False))}）")
    warnings = data.get("warnings") or []
    if warnings:
        raise SetupError(f"{what}: 面板返回警告（配置已保存但应用失败），不得继续："
                         + redact_secrets("；".join(str(w) for w in warnings)))
    return data


def pipeline_current_version(entry: dict | None, pid: str) -> str:
    """
    部署后核对：面板列表项的 current 字段（R02）。

    current 才是「已部署并写入记录」的版本；latest 只是 releases 目录里
    最新上传的目录名——上传之后、部署成功之前，latest 就已等于新版本。
    只认 latest 会把「上传了但没部署成功」判定为部署完成。
    """
    if not entry:
        raise SetupError(f"部署后核对失败：流水线 {pid} 不在面板列表中")
    version = entry.get("current")
    if not version:
        raise SetupError(
            f"部署后核对失败：流水线 {pid} 没有已部署版本（current 缺失；"
            f"latest 只是最新上传目录，不代表已部署）")
    return str(version)


def deploy_verdict(log_text: str) -> str:
    """
    依据部署日志判定任务状态（P1-11：末次终态优先）。

    - 'success'：最后一次终态标记是「成功 =====」
    - 'failed'：最后一次终态标记是「失败：」
    - 'running'：没有任何终态标记（继续等待）
    """
    text = log_text or ""
    i_s = text.rfind(SUCCESS_MARKER)
    i_f = text.rfind(FAILURE_MARKER)
    if i_s == -1 and i_f == -1:
        return "running"
    return "success" if i_s >= i_f else "failed"


def caddy_reload_failed(log_text: str) -> bool:
    """面板日志里的 Caddy 重载失败信号（R02，独立于终态标记）"""
    text = (log_text or "").lower()
    return any(marker in text for marker in CADDY_FAILURE_MARKERS)


def deploy_health(log_text: str) -> str:
    """
    综合部署健康判定（R02）：

    - 'caddy_reload_failed'：日志含 Caddy 重载失败信号——即使末态是成功
      标记也必须阻断（面板在重载失败后仍写「===== deploy 成功 =====」）；
    - 'failed' / 'success' / 'running'：同 deploy_verdict。
    """
    text = log_text or ""
    if caddy_reload_failed(text):
        return "caddy_reload_failed"
    return deploy_verdict(text)


# ---------- CLI ----------

def main(argv: list[str]) -> int:
    # stdin/stdout 统一 UTF-8：Windows Python 默认跟随本地代码页（GBK），
    # 处理含中文的部署日志时会出现乱码或 UnicodeDecodeError
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, OSError):
            pass
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    cmd = argv[1]

    def _read(arg: str) -> str:
        return open(arg, encoding="utf-8").read() if arg != "-" else sys.stdin.read()

    try:
        if cmd == "pipeline-body":
            slug, sub, port, upstream, name, base, paths_json = argv[2:9]
            paths = json.loads(paths_json) if paths_json and paths_json != "-" else None
            print(json.dumps(pipeline_body(slug, sub, int(port), upstream, name, base, paths),
                             ensure_ascii=False))
        elif cmd == "gate-preconditions":
            result = gate_preconditions(_read(argv[2]), argv[3])
            print(json.dumps(result, ensure_ascii=False))
            if not result["enabled"]:
                print("人用网关总开关未开启，无法把管理域名并入登录墙", file=sys.stderr)
                return 5
            if result["admin_domain_exempt"]:
                print("管理域名在免过门（exempt）清单中，加入登录墙不会生效", file=sys.stderr)
                return 5
        elif cmd == "gate-body":
            print(json.dumps(gate_body(_read(argv[2]), argv[3]), ensure_ascii=False))
        elif cmd == "gate-verify":
            result = gate_verify(_read(argv[2]), argv[3])
            print(json.dumps(result, ensure_ascii=False))
            if not result["enabled"]:
                print("部署后人用网关总开关不再开启（配置被并发改动？）", file=sys.stderr)
                return 1
            if result["admin_domain_exempt"]:
                print("部署后管理域名出现在免过门清单中", file=sys.stderr)
                return 1
            if not result["admin_domain_present"]:
                print("部署后登录墙 domains 不再包含管理域名", file=sys.stderr)
                return 1
        elif cmd == "check-write":
            check_write_response(_read(argv[2]), argv[3])
            print("ok")
        elif cmd == "check-portal":
            check_auth_portal(argv[2], argv[3], argv[4])
            print("ok")
        elif cmd == "redact":
            # A03：已知 Key 经环境变量传入（绝不走 argv）；清洗失败（异常/
            # 残留）时输出固定摘要，绝不回传原文——没有 `|| cat` 降级
            text = _read(argv[2])
            sys.stdout.write(safe_summary(text, limit=10 ** 9) + ('\n' if text.endswith('\n') else ''))
        elif cmd == "parse-pipelines":
            entries = parse_pipeline_list(_read(argv[2]))
            pid, expected = argv[3], argv[4]
            domain = check_pipeline_entry(entries.get(pid), pid, expected)
            print(json.dumps({"domain": domain,
                              "latest": (entries.get(pid) or {}).get("latest"),
                              "current": (entries.get(pid) or {}).get("current"),
                              "exists": pid in entries}, ensure_ascii=False))
        elif cmd == "check-current-version":
            entries = parse_pipeline_list(_read(argv[2]))
            pid, expected_version = argv[3], argv[4]
            actual = pipeline_current_version(entries.get(pid), pid)
            if actual != expected_version:
                raise SetupError(f"部署后核对失败：{pid} 面板当前版本 {actual} ≠ 部署版本 {expected_version}")
            print(actual)
        elif cmd == "deploy-verdict":
            print(deploy_verdict(_read(argv[2])))
        elif cmd == "deploy-health":
            print(deploy_health(_read(argv[2])))
        else:
            print(f"未知子命令: {cmd}", file=sys.stderr)
            return 2
    except SetupError as e:
        print(f"BLOCKED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
