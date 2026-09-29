# coding: utf-8
"""
voice-setup.sh 的 JSON 构造/解析/判定逻辑（独立模块，便于离线单测）

不接触网络、不接触 Key；bash 脚本通过子命令调用（见 main）。

对应《单密钥接入全面复核与修复清单-2026-09-29》的修法：
- P0-01  GET /api/pipelines 是数组，按 id 精确匹配；核对绑定域名/启用状态；
- P0-03  ASR 流水线创建体自带 apikey_gate，首次路由生成即带机器门；
- P0-04  POST /api/pipelines、/api/gate 的 200 响应必须解析 ok/id/warnings，
         warnings 视为阻断；部署后核对面板当前版本；
- P0-05  人用网关前置条件：gate.enabled 为真、管理域名不在 exempt 内；
- P1-11  deploy_verdict 以「末次终态」为准，成功标记后出现的失败不会被压过。
"""

from __future__ import annotations

import json
import sys

SUCCESS_MARKER = "成功 ====="
FAILURE_MARKER = "失败："

# 我方流水线的身份签名：同 ID 已存在但不是本方案的配置时，拒绝覆盖
PIPELINE_NOTE = "CapsWriter voice gateway"


class SetupError(Exception):
    """阻断性错误：bash 侧捕获后打印并以非零码退出"""


# ---------- 流水线 ----------

def pipeline_body(slug: str, sub: str, port: int, upstream: str, name: str,
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
    body = {
        "name": name or slug,
        "slug": slug,
        "env": "prod",
        "type": "api",
        "port": int(port),
        "note": PIPELINE_NOTE,
        "domains": [{"root": base_domain, "sub": sub, "bare": False}],
        "build_args": {"UPSTREAM": upstream, "LISTEN": str(port)},
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


# ---------- 响应与部署判定 ----------

def check_write_response(payload: str | dict, what: str) -> dict:
    """
    解析面板写操作响应（P0-04）：HTTP 200 不代表生效。

    - 非 JSON / ok 非 true → 阻断；
    - warnings 非空（Caddy/Authelia 应用失败等）→ 阻断并原样展示（不含密钥）；
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
        raise SetupError(f"{what}: 响应缺少 ok:true（{json.dumps(data, ensure_ascii=False)[:200]}）")
    warnings = data.get("warnings") or []
    if warnings:
        raise SetupError(f"{what}: 面板返回警告（配置已保存但应用失败），不得继续："
                         + "；".join(str(w) for w in warnings))
    return data


def pipeline_current_version(entry: dict | None, pid: str) -> str | None:
    """部署后核对：面板列表项的 latest/current 字段（P0-04）"""
    if not entry:
        raise SetupError(f"部署后核对失败：流水线 {pid} 不在面板列表中")
    version = entry.get("latest") or entry.get("current")
    if not version:
        raise SetupError(f"部署后核对失败：流水线 {pid} 没有已部署版本")
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


# ---------- CLI ----------

def main(argv: list[str]) -> int:
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
        elif cmd == "check-write":
            check_write_response(_read(argv[2]), argv[3])
            print("ok")
        elif cmd == "parse-pipelines":
            entries = parse_pipeline_list(_read(argv[2]))
            pid, expected = argv[3], argv[4]
            domain = check_pipeline_entry(entries.get(pid), pid, expected)
            print(json.dumps({"domain": domain,
                              "latest": (entries.get(pid) or {}).get("latest"),
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
        else:
            print(f"未知子命令: {cmd}", file=sys.stderr)
            return 2
    except SetupError as e:
        print(f"BLOCKED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
