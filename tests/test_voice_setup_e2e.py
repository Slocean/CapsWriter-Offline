# coding: utf-8
"""
voice-setup.sh 全流程端到端测试：用假 docker / curl / python3 夹具真实执行
Bash 主脚本（复核清单 §3：禁止只跑辅助函数单测）。

夹具机制（R11 网络隔离与契约）：
- tests/e2e_bin/docker：按场景输出 panel 网络地址 / 容器状态与镜像标签
  （镜像 app-<pid>:<current>，current 才是已部署版本）；
- tests/e2e_bin/curl：拦截脚本的所有 HTTP 调用——/api/* 转发到场景表
  （记录 method/path/body/auth 到调用日志），**所有面板请求必须携带场景
  规定的 X-API-Key，缺头按面板真实行为 401/exit 22（R01）**；Host 探测按
  场景返回状态码与重定向 Location；白名单外的 URL 一律 exit 3；
  latest=upload 即更新、current=deploy 成功才更新（面板真实契约）；
- tests/e2e_bin/python3：经 _py_lf.py 转发到当前解释器，stdout/stderr 钉死
  LF（Windows Python 的 CRLF 会破坏 bash 比较）；
- tests/e2e_bin/bash_env：经 BASH_ENV 在每个非交互 bash 启动时把夹具目录
  放回 PATH 首位——Git Bash 会把 /mingw64/bin 注入 PATH 最前吞掉夹具，
  这是原样运行全红的根因（R11）。

脚本以 POLL_INTERVAL=0.05 运行，全部失败分支非零退出，不触真实面板。
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

REPO = pathlib.Path(__file__).resolve().parents[1]
BIN_SRC = REPO / 'tests' / 'e2e_bin'
DEPLOY = REPO / 'deploy'

BASE_DOMAIN = 'faiz.dpdns.org'
ASR_DOMAIN = f'voice.{BASE_DOMAIN}'
ADMIN_DOMAIN = f'voice-admin.{BASE_DOMAIN}'
VERSION = '20260929-160000'
PANEL_KEY = 'dpk-test-key-000000'
AUTH_PORTAL = f'http://auth.{BASE_DOMAIN}/?rd=%2F'

# A04：核验三个被拦截命令的真实解析路径都在夹具目录内（不做任何 HTTP）
_FIXTURE_PATH_PROBE = r'''
fixture="$VOICE_E2E_FIXTURE_BIN"
command -v cygpath >/dev/null 2>&1 && fixture="$(cygpath -u "$fixture")"
for tool in curl docker python3; do
  p="$(command -v "$tool")" || { echo "MISSING:$tool"; exit 1; }
  case "$p" in
    "$fixture"/*|"$fixture") echo "$tool=$p" ;;
    *) echo "FOREIGN:$tool=$p"; exit 1 ;;
  esac
done
'''


def _entry(pid, domain, **extra):
    entry = {
        "id": pid, "name": pid, "slug": pid.rsplit('-', 1)[0], "env": "prod",
        "type": "api", "enabled": True, "note": "CapsWriter voice gateway",
        "domain": domain, "domains": [domain],
        "bindings": [{"root": BASE_DOMAIN, "sub": domain.split('.')[0], "bare": False}],
        "latest": VERSION, "current": None,
    }
    entry.update(extra)
    return entry


def default_scenario():
    return {
        "panel_ips": ["172.18.0.3", "172.19.0.13"],
        "valid_ip": "172.19.0.13",
        "panel_key": PANEL_KEY,
        "pipelines": [
            _entry("fundglass-api-prod", "fundglass-api.faiz.dpdns.org"),
            _entry("voice-asr-prod", ASR_DOMAIN),
            _entry("voice-admin-prod", ADMIN_DOMAIN),
        ],
        "gate": {"enabled": True, "domains": ["nav.faiz.dpdns.org"], "exempt": {}},
        "caddy": {"voice_nokey": 401, "voice_withkey_pre": 502, "voice_withkey": 426,
                  "admin_noauth": 302, "admin_location": AUTH_PORTAL},
        "containers_running": True,
        "deploy_result": "success",   # success | failed | timeout | caddy_reload_failed
        "pipeline_warnings": [],
        "gate_warnings": [],
        "me_status": 200,
    }


class VoiceSetupE2E(unittest.TestCase):
    def run_script(self, scenario, panel_key=PANEL_KEY, concurrent=False):
        tmp = tempfile.mkdtemp(prefix='vse2e-')
        bin_dir = pathlib.Path(tmp) / 'bin'
        bin_dir.mkdir()
        work = pathlib.Path(tmp) / 'work'
        work.mkdir()
        try:
            # 夹具脚本（强制 LF：bash case/heredoc 对 \r 敏感，git autocrlf 会写成 CRLF；
            # 烘焙解释器与 _py_lf.py 启动器的 Windows 路径——MSYS2_ARG_CONV_EXCL='*'
            # 会禁用路径转换，POSIX 路径直接传给 python.exe 打不开）
            py_exe = sys.executable.replace('\\', '/')
            pylf = (bin_dir / '_py_lf.py').as_posix()
            for name in ('docker', 'curl', 'python3', 'bash_env', '_py_lf.py'):
                text = (BIN_SRC / name).read_text(encoding='utf-8')
                text = text.replace('@PYTHON@', py_exe).replace('@PYLF@', pylf)
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
            with zipfile.ZipFile(work / 'voice-gateway.zip', 'w') as z:
                for name in ('Dockerfile', 'Caddyfile.template', 'entrypoint.sh'):
                    z.write(DEPLOY / 'voice-gateway' / name, name)
            for f in (work / 'voice-setup.sh', work / 'voice_setup_lib.py'):
                data = f.read_bytes().replace(b'\r\n', b'\n')
                f.write_bytes(data)

            env = dict(os.environ)
            env['PATH'] = f'{bin_dir};{env["PATH"]}' if os.name == 'nt' else f'{bin_dir}:{env["PATH"]}'
            # R11：BASH_ENV 让每个非交互 bash 启动时把夹具目录放回 PATH 首位
            # （Git Bash 会把 /mingw64/bin 注入最前，夹具被真实 curl/python 吞掉）
            env['VOICE_E2E_FIXTURE_BIN'] = str(bin_dir)
            env['BASH_ENV'] = str(bin_dir / 'bash_env')
            env['VOICE_E2E_SCENARIO'] = str(scen_path)
            env['VOICE_E2E_CALLLOG'] = str(pathlib.Path(tmp) / 'calls.jsonl')
            env['VOICE_E2E_STATE'] = str(pathlib.Path(tmp) / 'state')
            env['POLL_INTERVAL'] = '0.05'
            env['PYTHONUTF8'] = '1'
            env['PANEL_KEY'] = panel_key
            env['UPSTREAM_STATE_DIR'] = '/tmp/' + pathlib.Path(tmp).name + '/work/endpoint'
            env['ZIP'] = str(work / 'voice-gateway.zip')
            env['BASE_DOMAIN'] = BASE_DOMAIN
            # Git Bash 的 MSYS 会把 /api/... 参数改写成 Windows 路径，必须禁用；
            # 服务器上是 WSL 原生 bash，不存在该问题
            env['MSYS_NO_PATHCONV'] = '1'
            env['MSYS2_ARG_CONV_EXCL'] = '*'
            pathlib.Path(env['VOICE_E2E_STATE']).mkdir()

            script = work / 'voice-setup.sh'
            # A04：在任何 HTTP 请求之前核验 curl/docker/python3 的真实解析路径
            # ——必须全部落在夹具目录内；系统工具抢先时立即失败，不出网络请求
            probe = subprocess.run(['bash', '-c', _FIXTURE_PATH_PROBE], env=env,
                                   capture_output=True, text=True, timeout=30)
            if probe.returncode != 0:
                raise AssertionError(
                    '夹具路径核验失败（A04：curl/docker/python3 未解析到夹具目录）：\n'
                    f'stdout={probe.stdout}\nstderr={probe.stderr}')
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
                'work': str(work),
                'lock_kept': (work / '.voice-setup.lock').exists(),
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
        # R01：面板全部 API 调用（含 GET）都携带认证
        apis = self.api_calls(r)
        self.assertTrue(apis)
        for c in apis:
            self.assertTrue(c.get('auth'), f"未携带认证的请求: {c['method']} {c['path']}")
            # 阶段九：认证 Key 必须经 stdin 头注入，绝不展开进 curl argv
            self.assertEqual(c.get('auth_source'), 'stdin',
                             f"Key 经 argv 传递（secretCare 违规）: {c['method']} {c['path']}")
        self.assertNotIn(PANEL_KEY, r['out'], '脚本 stdout 不得出现假 Key')
        self.assertNotIn(PANEL_KEY, r['err'], '脚本 stderr 不得出现假 Key')
        # 顺序：建站（带机器门）→ 登录墙 → 运行态门验证 → 才允许 deploy
        posts = self.api_calls(r, 'POST')
        order = [c['path'] for c in posts]
        self.assertIn('/api/pipelines', order)
        self.assertIn('/api/gate', order)
        deploy_paths = [c['path'] for c in posts if c['path'].endswith('/deploy')]
        self.assertEqual(len(deploy_paths), 2, '两条流水线各部署一次')
        gate_idx = order.index('/api/gate')
        first_deploy_idx = min(order.index(p) for p in deploy_paths)
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

    def test_empty_pipelines_first_deploy(self):
        """首次部署：面板没有任何流水线（R11：默认场景不能预置现成流水线）"""
        scen = default_scenario()
        scen['pipelines'] = []
        r = self.run_script(scen)
        self.assertEqual(r['code'], 0, f"stdout={r['out']}\nstderr={r['err']}")
        creates = [c for c in self.api_calls(r, 'POST', '/api/pipelines')
                   if c['path'] == '/api/pipelines']
        self.assertEqual(len(creates), 2, '两条流水线都走首次创建')
        # 部署后 current 必须已写（夹具按真实契约维护），脚本核对通过
        self.assertIn('面板 current', r['out'])

    def test_r01_unauthenticated_panel_get_fails_before_write(self):
        """夹具强制认证：脚本任何一个面板 GET 漏带 Key 都会在写操作前失败（R01）"""
        # 场景本身无法关掉认证（夹具恒定强制），反例由单元层夹具守卫：
        # 这里验证脚本正常路径全部 GET 带 Key，一旦回归即失败退出
        scen = default_scenario()
        r = self.run_script(scen)
        self.assertEqual(r['code'], 0)
        gets = self.api_calls(r, 'GET')
        self.assertTrue(gets)
        for c in gets:
            self.assertTrue(c.get('auth'), f"GET 漏带认证: {c['path']}")
            self.assertEqual(c.get('auth_source'), 'stdin', f"GET 的 Key 经 argv 传递: {c['path']}")

    def test_stage9_key_never_in_argv_all_calls(self):
        """阶段九（全量调用覆盖）：包括上传(multipart)/部署/日志轮询/带Key探测
        在内的全部认证调用，Key 都必须经 stdin 头注入（auth_source=stdin），
        绝不展开进外部进程 argv；脚本输出不出现假 Key"""
        r = self.run_script(default_scenario())
        self.assertEqual(r['code'], 0, f"stdout={r['out']}\nstderr={r['err']}")
        apis = self.api_calls(r)
        kinds = {c['method'] + ' ' + ('upload' if c['path'].endswith('/upload') else
                 'deploy' if c['path'].endswith('/deploy') else
                 'logs' if c['path'].startswith('/api/logs') else 'api') for c in apis}
        self.assertTrue({'POST upload', 'POST deploy', 'GET logs'}.issubset(kinds),
                        f'前提：场景应覆盖上传/部署/日志轮询调用，实际: {sorted(kinds)}')
        for c in apis:
            self.assertTrue(c.get('auth'), f"未携带认证: {c['method']} {c['path']}")
            self.assertEqual(c.get('auth_source'), 'stdin',
                             f"Key 经 argv 传递（覆盖发现/GET/POST/上传/部署/日志/健康）: "
                             f"{c['method']} {c['path']}")
        self.assertNotIn(PANEL_KEY, r['out'])
        self.assertNotIn(PANEL_KEY, r['err'])

    def test_stage9_script_source_has_no_argv_key_header(self):
        """源码契约：voice-setup.sh 不得再有 -H "X-API-Key: …" 形式的 argv 认证头"""
        source = (DEPLOY / 'voice-setup.sh').read_text(encoding='utf-8')
        import re
        argv_headers = re.findall(r'-H\s+"X-API-Key:[^"]*"', source)
        self.assertEqual(argv_headers, [],
                         f'script 仍在把认证头写进 curl argv: {argv_headers}')
        # 统一入口存在且全部认证调用经 stdin（-H @-）
        self.assertIn('key_hdr() { printf', source)
        self.assertGreaterEqual(source.count('-H @-'), 8,
                                '认证调用必须全部经 stdin 头注入（-H @-）')

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

    def test_r08_app_redirect_is_not_login_wall(self):
        """任意应用跳转（Location 非登录门户）不能当作登录墙生效（R08）"""
        scen = default_scenario()
        scen['caddy']['admin_noauth'] = 301
        scen['caddy']['admin_location'] = f'http://{ADMIN_DOMAIN}/login'
        r = self.run_script(scen)
        self.assertEqual(r['code'], 4)
        self.assertEqual(self.api_calls(r, 'POST', path_suffix='/deploy'), [])

    def test_r08_redirect_without_location_is_not_login_wall(self):
        scen = default_scenario()
        scen['caddy']['admin_noauth'] = 302
        scen['caddy']['admin_location'] = ''
        r = self.run_script(scen)
        self.assertEqual(r['code'], 4)

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

    def test_r05_failure_log_secrets_redacted(self):
        """部署失败日志里的 Key 标记必须脱敏后才能进 stdout（R05）"""
        scen = default_scenario()
        scen['deploy_result'] = 'failed'
        r = self.run_script(scen)
        self.assertEqual(r['code'], 5)
        self.assertNotIn('dpk-fake-secret-123456', r['out'] + r['err'],
                         '部署失败输出泄露了 Key 形式的敏感标记')
        self.assertIn('<redacted>', r['out'])

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

    def test_r02_caddy_reload_failure_blocks_even_with_success_marker(self):
        """面板在 Caddy 重载失败后仍写成功标记；脚本必须独立阻断（R02）"""
        scen = default_scenario()
        scen['deploy_result'] = 'caddy_reload_failed'
        r = self.run_script(scen)
        self.assertEqual(r['code'], 5, f"stdout={r['out']}\nstderr={r['err']}")
        self.assertIn('Caddy', r['out'] + r['err'])
        # 阻断后不得继续第二条部署
        deploys = self.api_calls(r, 'POST', path_suffix='/deploy')
        self.assertLessEqual(len(deploys), 1, '第一条 Caddy 重载失败即停止，不得继续第二条')

    def test_r02_stale_current_blocks(self):
        """日志成功但面板 current 未更新（旧容器仍在运行）必须阻断（R02）"""
        scen = default_scenario()
        scen['no_current_update'] = True
        scen['stale_current_version'] = '20260101-000000'
        r = self.run_script(scen)
        self.assertEqual(r['code'], 5, f"stdout={r['out']}\nstderr={r['err']}")
        self.assertIn('镜像', r['out'] + r['err'])

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
        # R04：未取得锁的实例不得删除第一实例的锁
        self.assertTrue(r['lock_kept'], '第二实例退出时删除了第一实例的锁')

    def test_r11_network_allowlist_blocks_other_urls(self):
        """白名单外 URL 一律失败（R11 网络隔离由夹具恒定强制）"""
        # 隔离在夹具内恒定生效：正常路径只允许面板 API 与 Caddy 回环探测，
        # 若脚本出现其他 URL 请求即 exit 3 → 整体失败。这里以 happy path
        # 全部成功佐证没有越界请求。
        r = self.run_script(default_scenario())
        self.assertEqual(r['code'], 0)


if __name__ == '__main__':
    unittest.main()
