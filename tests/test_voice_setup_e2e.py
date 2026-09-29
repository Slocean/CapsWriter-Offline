# coding: utf-8
"""
voice-setup.sh 全流程端到端测试：用假 docker / curl / python3 夹具真实执行
Bash 主脚本（复核清单 §3：禁止只跑辅助函数单测）。

夹具机制：
- tests/e2e_bin/docker：按场景输出 panel 网络地址 / 容器状态；
- tests/e2e_bin/curl：拦截脚本的所有 HTTP 调用——/api/* 转发到场景表
  （记录 method/path/body 到调用日志），Host 探测按场景返回状态码；
- tests/e2e_bin/python3：转发到当前解释器（Git Bash 可能没有 python3）；
- 脚本以 POLL_INTERVAL=0.05 运行，全部失败分支非零退出，不触真实面板。
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
BIN_SRC = REPO / 'tests' / 'e2e_bin'
DEPLOY = REPO / 'deploy'

BASE_DOMAIN = 'faiz.dpdns.org'
ASR_DOMAIN = f'voice.{BASE_DOMAIN}'
ADMIN_DOMAIN = f'voice-admin.{BASE_DOMAIN}'
VERSION = '20260929-160000'


def _entry(pid, domain, **extra):
    entry = {
        "id": pid, "name": pid, "slug": pid.rsplit('-', 1)[0], "env": "prod",
        "type": "api", "enabled": True, "note": "CapsWriter voice gateway",
        "domain": domain, "domains": [domain],
        "bindings": [{"root": BASE_DOMAIN, "sub": domain.split('.')[0], "bare": False}],
        "latest": VERSION,
    }
    entry.update(extra)
    return entry


def default_scenario():
    return {
        "panel_ips": ["172.18.0.3", "172.19.0.13"],
        "valid_ip": "172.19.0.13",
        "pipelines": [
            _entry("fundglass-api-prod", "fundglass-api.faiz.dpdns.org"),
            _entry("voice-asr-prod", ASR_DOMAIN),
            _entry("voice-admin-prod", ADMIN_DOMAIN),
        ],
        "gate": {"enabled": True, "domains": ["nav.faiz.dpdns.org"], "exempt": {}},
        "caddy": {"voice_nokey": 401, "voice_withkey_pre": 502, "voice_withkey": 426,
                  "admin_noauth": 302},
        "containers_running": True,
        "deploy_result": "success",   # success | failed | timeout
        "pipeline_warnings": [],
        "gate_warnings": [],
        "me_status": 200,
    }


class VoiceSetupE2E(unittest.TestCase):
    def run_script(self, scenario, panel_key='dpk-test-key-000000', concurrent=False):
        tmp = tempfile.mkdtemp(prefix='vse2e-')
        bin_dir = pathlib.Path(tmp) / 'bin'
        bin_dir.mkdir()
        work = pathlib.Path(tmp) / 'work'
        work.mkdir()
        try:
            # 夹具脚本（强制 LF：bash case/heredoc 对 \r 敏感，git autocrlf 会写成 CRLF）
            for name in ('docker', 'curl', 'python3'):
                src = BIN_SRC / name
                text = src.read_text(encoding='utf-8')
                text = text.replace('@PYTHON@', sys.executable.replace('\\', '/'))
                text = text.replace('\r\n', '\n')
                dst = bin_dir / name
                dst.write_text(text, encoding='utf-8', newline='\n')
                os.chmod(dst, 0o755)

            scen_path = pathlib.Path(tmp) / 'scenario.json'
            scen_path.write_text(json.dumps(scenario, ensure_ascii=False), encoding='utf-8',
                                 newline='\n')

            # 把脚本与其依赖复制到临时工作目录（模拟真实分发：脚本+lib+zip 同目录）
            shutil.copy2(DEPLOY / 'voice-setup.sh', work / 'voice-setup.sh')
            shutil.copy2(DEPLOY / 'voice_setup_lib.py', work / 'voice_setup_lib.py')
            shutil.copy2(DEPLOY / 'voice-gateway.zip', work / 'voice-gateway.zip')
            for f in (work / 'voice-setup.sh', work / 'voice_setup_lib.py'):
                data = f.read_bytes().replace(b'\r\n', b'\n')
                f.write_bytes(data)

            env = dict(os.environ)
            env['PATH'] = f'{bin_dir};{env["PATH"]}' if os.name == 'nt' else f'{bin_dir}:{env["PATH"]}'
            env['VOICE_E2E_SCENARIO'] = str(scen_path)
            env['VOICE_E2E_CALLLOG'] = str(pathlib.Path(tmp) / 'calls.jsonl')
            env['VOICE_E2E_STATE'] = str(pathlib.Path(tmp) / 'state')
            env['POLL_INTERVAL'] = '0.05'
            env['PANEL_KEY'] = panel_key
            env['ZIP'] = str(work / 'voice-gateway.zip')
            env['BASE_DOMAIN'] = BASE_DOMAIN
            # Git Bash 的 MSYS 会把 /api/... 参数改写成 Windows 路径，必须禁用；
            # 服务器上是 WSL 原生 bash，不存在该问题
            env['MSYS_NO_PATHCONV'] = '1'
            env['MSYS2_ARG_CONV_EXCL'] = '*'
            pathlib.Path(env['VOICE_E2E_STATE']).mkdir()

            script = work / 'voice-setup.sh'
            cmd = ['bash', str(script)]
            if concurrent:
                # 并发第二实例：直接预占锁目录（脚本锁定在自己所在目录）
                lock = work / '.voice-setup.lock'
                lock.mkdir()
            proc = subprocess.run(cmd, env=env, cwd=str(work), capture_output=True,
                                  text=True, timeout=180)
            calls = []
            log = pathlib.Path(env['VOICE_E2E_CALLLOG'])
            if log.exists():
                for line in log.read_text(encoding='utf-8').splitlines():
                    if line.strip():
                        calls.append(json.loads(line))
            return {
                'code': proc.returncode,
                'out': proc.stdout,
                'err': proc.stderr,
                'calls': calls,
                'tmp': tmp,
            }
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---------- 断言助手 ----------

    def api_calls(self, result, method=None, path_prefix=None, path_suffix=None):
        out = []
        for c in result['calls']:
            if c['kind'] != 'api':
                continue
            if method and c['method'] != method:
                continue
            if path_prefix and not c['path'].startswith(path_prefix):
                continue
            if path_suffix and not c['path'].endswith(path_suffix):
                continue
            out.append(c)
        return out

    def tearDown(self):
        pass

    # ---------- 场景 ----------

    def test_happy_path_exit0_and_gate_before_deploy(self):
        r = self.run_script(default_scenario())
        self.assertEqual(r['code'], 0, f"stdout={r['out']}\nstderr={r['err']}")
        # 顺序：建站（带机器门）→ 登录墙 → 运行态门验证 → 才允许 deploy
        posts = self.api_calls(r, 'POST')
        order = [c['path'] for c in posts]
        self.assertIn('/api/pipelines', order)
        self.assertIn('/api/gate', order)
        self.assertEqual(order.index('/api/gate') < order.index('/api/pipelines/{pid}/deploy')
                         if '/api/pipelines/{pid}/deploy' in order else True, True)
        deploys = self.api_calls(r, 'POST', path_suffix='/deploy')
        self.assertEqual(len(deploys), 2, '两条流水线各部署一次')
        gate_idx = order.index('/api/gate')
        first_deploy_idx = min(order.index(d['path']) for d in posts if d['path'].endswith('/deploy')) \
            if any(d['path'].endswith('/deploy') for d in posts) else -1
        self.assertLess(gate_idx, first_deploy_idx, '登录墙配置必须先于部署')
        # ASR 创建体自带机器门
        asr_save = [c for c in posts if c['path'] == '/api/pipelines'
                    and 'voice-asr' in json.dumps(c['body'])][0]
        body = asr_save['body'] if isinstance(asr_save['body'], dict)             else json.loads(asr_save['body'])
        self.assertEqual(body['apikey_gate'], {"enabled": True, "paths": ["/*"]})
        self.assertEqual(body['domains'][0]['sub'], 'voice')
        # 登录墙合并：保留现值 + 追加管理域
        gate_save = [c for c in posts if c['path'] == '/api/gate'][0]
        gbody = gate_save['body'] if isinstance(gate_save['body'], dict)             else json.loads(gate_save['body'])
        self.assertEqual(gbody['domains'],
                         ['nav.faiz.dpdns.org', ADMIN_DOMAIN])
        self.assertNotIn('apikey_gates', gbody)
        self.assertIn('exit 0', r['out'])

    def test_rerun_does_not_duplicate_domains(self):
        scen = default_scenario()
        scen['gate']['domains'] = ['nav.faiz.dpdns.org', ADMIN_DOMAIN]  # 已加过
        r = self.run_script(scen)
        self.assertEqual(r['code'], 0, f"stderr={r['err']}")
        gate_save = [c for c in self.api_calls(r, 'POST') if c['path'] == '/api/gate'][0]
        gbody = gate_save['body'] if isinstance(gate_save['body'], dict)             else json.loads(gate_save['body'])
        self.assertEqual(gbody['domains'].count(ADMIN_DOMAIN), 1, '重复执行不得重复追加域名')

    def test_no_key_fails_before_any_write(self):
        r = self.run_script(default_scenario(), panel_key='')
        self.assertNotEqual(r['code'], 0)
        self.assertEqual(self.api_calls(r), [], '无 Key 不得发出任何 API 调用')

    def test_bad_key_no_write(self):
        scen = default_scenario()
        scen['me_status'] = 401
        r = self.run_script(scen)
        self.assertNotEqual(r['code'], 0)
        writes = [c for c in self.api_calls(r) if c['method'] == 'POST']
        self.assertEqual(writes, [], 'Key 无效不得有写操作')

    def test_pipeline_save_warnings_block(self):
        scen = default_scenario()
        scen['pipeline_warnings'] = ['caddy reload failed: exit 1']
        r = self.run_script(scen)
        self.assertEqual(r['code'], 1)
        # 阻断后不得继续网关配置与部署
        self.assertEqual(self.api_calls(r, 'POST', '/api/gate'), [])
        self.assertEqual(self.api_calls(r, 'POST', path_suffix='/deploy'), [])

    def test_gate_save_warnings_block(self):
        scen = default_scenario()
        scen['gate_warnings'] = ['authelia unreachable']
        r = self.run_script(scen)
        self.assertEqual(r['code'], 1)
        self.assertEqual(self.api_calls(r, 'POST', path_suffix='/deploy'), [], '网关应用失败不得部署')

    def test_machine_gate_not_effective_blocks_deploy(self):
        scen = default_scenario()
        scen['caddy']['voice_nokey'] = 200
        r = self.run_script(scen)
        self.assertEqual(r['code'], 4)
        self.assertEqual(self.api_calls(r, 'POST', path_suffix='/deploy'), [],
                         '机器门运行态未生效，不得部署代理')

    def test_login_wall_not_blocking_blocks_deploy(self):
        scen = default_scenario()
        scen['caddy']['admin_noauth'] = 200
        r = self.run_script(scen)
        self.assertEqual(r['code'], 4)
        self.assertEqual(self.api_calls(r, 'POST', path_suffix='/deploy'), [])

    def test_gate_disabled_precondition(self):
        scen = default_scenario()
        scen['gate']['enabled'] = False
        r = self.run_script(scen)
        self.assertEqual(r['code'], 4)
        # 不得部署，也不得改写 gate
        self.assertEqual(self.api_calls(r, 'POST', path_suffix='/deploy'), [])
        self.assertEqual(self.api_calls(r, 'POST', '/api/gate'), [])

    def test_exempt_admin_domain_blocks(self):
        scen = default_scenario()
        scen['gate']['exempt'] = {ADMIN_DOMAIN: ["/api/*"]}
        r = self.run_script(scen)
        self.assertEqual(r['code'], 4)

    def test_deploy_failure_exit5(self):
        scen = default_scenario()
        scen['deploy_result'] = 'failed'
        r = self.run_script(scen)
        self.assertEqual(r['code'], 5)
        self.assertIn('失败', r['out'])

    def test_deploy_timeout_exit5(self):
        scen = default_scenario()
        scen['deploy_result'] = 'timeout'
        r = self.run_script(scen)
        self.assertEqual(r['code'], 5)

    def test_deploy_success_but_container_down_exit5(self):
        scen = default_scenario()
        scen['containers_running'] = False
        r = self.run_script(scen)
        self.assertEqual(r['code'], 5)
        self.assertIn('容器', r['out'] + r['err'])

    def test_site_authorization_pending_exit3(self):
        scen = default_scenario()
        scen['caddy']['voice_withkey'] = 403
        r = self.run_script(scen)
        self.assertEqual(r['code'], 3)
        self.assertIn('voice-asr-prod', r['out'])

    def test_foreign_pipeline_conflict_blocks(self):
        scen = default_scenario()
        for p in scen['pipelines']:
            if p['id'] == 'voice-asr-prod':
                p['note'] = '别人的服务'
        r = self.run_script(scen)
        self.assertEqual(r['code'], 1)
        writes = [c for c in self.api_calls(r) if c['method'] == 'POST']
        self.assertEqual(writes, [], '异签名流水线不得被覆盖')

    def test_wrong_binding_blocks(self):
        scen = default_scenario()
        for p in scen['pipelines']:
            if p['id'] == 'voice-asr-prod':
                p['domain'] = p['domains'][0] = 'voice-asr.faiz.dpdns.org'
        r = self.run_script(scen)
        self.assertEqual(r['code'], 1)
        writes = [c for c in self.api_calls(r) if c['method'] == 'POST']
        self.assertEqual(writes, [], '绑定错误不得继续')

    def test_disabled_pipeline_blocks(self):
        scen = default_scenario()
        for p in scen['pipelines']:
            if p['id'] == 'voice-asr-prod':
                p['enabled'] = False
        r = self.run_script(scen)
        self.assertEqual(r['code'], 1)
        writes = [c for c in self.api_calls(r) if c['method'] == 'POST']
        self.assertEqual(writes, [])

    def test_lock_prevents_concurrent(self):
        r = self.run_script(default_scenario(), concurrent=True)
        self.assertEqual(r['code'], 2)
        self.assertEqual(self.api_calls(r), [])


if __name__ == '__main__':
    unittest.main()
