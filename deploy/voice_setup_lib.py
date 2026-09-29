# coding: utf-8
"""
voice-setup.sh 的 JSON 构造/判定逻辑（独立模块，便于离线单测）

不接触网络、不接触 Key；bash 脚本通过子命令调用：

    python3 voice_setup_lib.py pipeline-body <slug> <sub> <port> <upstream> <name> <base_domain>
    python3 voice_setup_lib.py gate-body <gate_json_file> <asr_pid> <admin_domain> <paths_json>
    python3 voice_setup_lib.py pick-panel-ip <inspect_output>
    python3 voice_setup_lib.py deploy-verdict <log_text_file>

设计要点（对应执行文档 §6.1 复核修法）：
- 机器门请求体是列表 [{"pid","enabled","paths"}]；面板 apply_apikey_gates
  只修改列表中列出的流水线，其余站点不受影响 → 重复运行不会覆盖别的站。
- domains 从 GET /api/gate 读到的现值合并追加，不凭空构造整表。
- 部署结束判定按面板 JobRunner.job_done_marker：出现「成功 =====」才算成功，
  「失败：」即失败；两者都没出现 = 未结束。
"""

from __future__ import annotations

import json
import sys

SUCCESS_MARKER = "成功 ====="
FAILURE_MARKER = "失败："


def pipeline_body(slug: str, sub: str, port: int, upstream: str, name: str, base_domain: str) -> dict:
    """流水线创建/更新请求体：pid 由面板按 slug-env 生成（voice-asr → voice-asr-prod），
    子域独立于 slug（ASR 绑 voice 而不是 voice-asr）。"""
    slug = (slug or "").strip()
    sub = (sub or "").strip()
    if not slug or not sub:
        raise ValueError("slug 与 sub 均不能为空")
    return {
        "name": name or slug,
        "slug": slug,
        "env": "prod",
        "type": "api",
        "port": int(port),
        "note": "CapsWriter voice gateway",
        "domains": [{"root": base_domain, "sub": sub, "bare": False}],
        "build_args": {"UPSTREAM": upstream, "LISTEN": str(port)},
        "container_env": {},
        "api_pipeline": "",
        "auto_db": False,
    }


def gate_body(gate_json: str | dict, asr_pid: str, admin_domain: str, paths: list[str]) -> dict:
    """
    POST /api/gate 请求体（PATCH 语义：未提交字段保留现值）

    - domains：现值 + admin_domain（幂等去重），不覆盖用户已有配置；
    - apikey_gates：列表 [{"pid", "enabled", "paths"}]，只含本次要开的
      机器门，面板只改列出的流水线。
    """
    if isinstance(gate_json, str):
        gate = json.loads(gate_json) if gate_json.strip() else {}
    else:
        gate = gate_json
    if not isinstance(gate, dict):
        raise ValueError("gate_json 必须是 JSON 对象")

    domains = list(dict.fromkeys([str(d) for d in (gate.get("domains") or []) if d] + [admin_domain]))
    return {
        "domains": domains,
        "apikey_gates": [
            {"pid": asr_pid, "enabled": True, "paths": list(paths or ["/*"])},
        ],
    }


def pick_panel_ip(inspect_output: str, probe) -> str | None:
    """
    从 docker inspect 输出（每行一个 IP）中选出可用的面板地址。

    probe(ip) -> bool 由调用方注入（GET /api/me 返回 200 即可用），
    逐个尝试，全部失败返回 None。
    """
    for line in (inspect_output or "").splitlines():
        ip = line.strip()
        if ip and probe(ip):
            return ip
    return None


def deploy_verdict(log_text: str) -> str:
    """
    依据部署日志判定任务状态：

    - 'success'：出现「成功 =====」
    - 'failed'：出现「失败：」
    - 'running'：两者都没有（继续等待）
    """
    text = log_text or ""
    if SUCCESS_MARKER in text:
        return "success"
    if FAILURE_MARKER in text:
        return "failed"
    return "running"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "pipeline-body":
        slug, sub, port, upstream, name, base = argv[2:8]
        print(json.dumps(pipeline_body(slug, sub, int(port), upstream, name, base), ensure_ascii=False))
        return 0
    if cmd == "gate-body":
        gate_json = open(argv[2], encoding="utf-8").read() if argv[2] != "-" else sys.stdin.read()
        asr_pid, admin_domain, paths_json = argv[3], argv[4], argv[5]
        paths = json.loads(paths_json) if paths_json else ["/*"]
        print(json.dumps(gate_body(gate_json, asr_pid, admin_domain, paths), ensure_ascii=False))
        return 0
    if cmd == "pick-panel-ip":
        # probe 由 bash 侧完成；这里只负责把候选 IP 逐行输出（println 已分行）
        for line in (open(argv[2], encoding="utf-8").read() if argv[2] != "-" else sys.stdin.read()).splitlines():
            ip = line.strip()
            if ip:
                print(ip)
        return 0
    if cmd == "deploy-verdict":
        text = open(argv[2], encoding="utf-8").read() if argv[2] != "-" else sys.stdin.read()
        print(deploy_verdict(text))
        return 0
    print(f"未知子命令: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
