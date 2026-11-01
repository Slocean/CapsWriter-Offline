# coding: utf-8
"""
管理端 HTTP API 全流程测试：登录、CSRF、设置、热词、日志、审计、网关认证模式
（单密钥改造后不再有 CapsWriter 自建客户端令牌接口）
"""

import http.client
import json
import pathlib
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from admin.config_admin import AdminConfig as Cfg  # noqa: E402
from admin.httpd import make_server  # noqa: E402
from admin.webapp import AdminApp  # noqa: E402


class _AdminServerBase(unittest.TestCase):
    """共享：临时目录配置 + 线程内 HTTP 服务器 + 请求助手"""

    @classmethod
    def _configure(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = pathlib.Path(cls._tmp.name)
        Cfg.data_dir = tmp / 'admin-data'
        Cfg.static_dir = pathlib.Path(__file__).resolve().parents[1] / 'admin' / 'static'
        Cfg.password_file = Cfg.data_dir / 'admin_password.json'
        Cfg.audit_log = Cfg.data_dir / 'audit.log'
        Cfg.settings_file = tmp / 'server_settings.json'
        Cfg.hotwords_path = tmp / 'hot-server.txt'
        Cfg.log_file = tmp / 'server.log'
        Cfg.asr_pid_file = tmp / 'server.pid'
        Cfg.asr_control_token_path = tmp / 'control.token'
        Cfg.repo_dir = tmp

    @classmethod
    def _start(cls, app):
        cls.server = make_server('127.0.0.1', 0, app)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls._tmp.cleanup()

    def _req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        h = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            h.setdefault('Content-Type', 'application/json')
        conn.request(method, path, payload, h)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {'_raw': raw[:200].decode('utf-8', 'replace')}
        return resp.status, data, resp

    def _cookie_dict(self, resp):
        cookies = {}
        for c in resp.msg.get_all('Set-Cookie') or []:
            k, v = c.split(';', 1)[0].split('=', 1)
            cookies[k] = v
        return cookies


class WebappAPITests(_AdminServerBase):
    """password 模式（默认）"""

    @classmethod
    def setUpClass(cls):
        cls._configure()
        Cfg.admin_auth_mode = 'password'
        app = AdminApp()
        app.auth.set_password('test-password-1')
        cls.app = app
        cls._start(app)

    def setUp(self):
        # 每个用例独立登录
        status, data, resp = self._req('POST', '/api/v1/login', {'password': 'test-password-1'})
        self.assertEqual(status, 200)
        self.cookies = self._cookie_dict(resp)
        self.csrf = data['csrf']

    def _h(self, csrf=True):
        headers = {'Cookie': '; '.join(f'{k}={v}' for k, v in self.cookies.items())}
        if csrf:
            headers['X-CSRF-Token'] = self.csrf
        return headers

    # ---------- 基础与鉴权 ----------

    def test_health_no_auth(self):
        status, data, _ = self._req('GET', '/health')
        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])

    def test_api_requires_login(self):
        status, _, _ = self._req('GET', '/api/v1/status')
        self.assertEqual(status, 401)

    def test_static_pages_served(self):
        status, _, resp = self._req('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn('text/html', resp.msg.get('Content-Type', ''))
        status, _, resp = self._req('GET', '/static/app.css')
        self.assertEqual(status, 200)

    def test_path_traversal_blocked(self):
        status, _, _ = self._req('GET', '/static/..%2F..%2Fconfig_server.py')
        self.assertIn(status, (404, 400))

    def test_wrong_password_rejected(self):
        status, _, _ = self._req('POST', '/api/v1/login', {'password': 'nope'})
        self.assertEqual(status, 401)

    def test_bootstrap_reports_password_mode(self):
        status, data, _ = self._req('GET', '/api/v1/bootstrap')
        self.assertEqual(status, 200)
        self.assertEqual(data['mode'], 'password')
        self.assertFalse(data['authenticated'])

    def test_no_client_token_endpoints(self):
        """单密钥改造后：CapsWriter 自建令牌接口必须消失"""
        headers = self._h()
        status, _, _ = self._req('GET', '/api/v1/client-tokens', headers=headers)
        self.assertEqual(status, 404)
        status, _, _ = self._req('POST', '/api/v1/client-tokens', {'name': 'x'}, headers)
        self.assertEqual(status, 404)

    def test_write_requires_csrf(self):
        status, data, _ = self._req('PUT', '/api/v1/settings',
                                    {'log_level': 'INFO'}, self._h(csrf=False))
        self.assertEqual(status, 403)

    def test_write_with_bad_csrf_rejected(self):
        status, data, _ = self._req('PUT', '/api/v1/settings',
                                    {'log_level': 'INFO'}, self._h(csrf=False) | {'X-CSRF-Token': 'forged'})
        self.assertEqual(status, 403)

    def test_cross_origin_write_rejected(self):
        headers = self._h()
        headers['Origin'] = 'https://evil.example.com'
        status, data, _ = self._req('PUT', '/api/v1/settings', {'log_level': 'INFO'}, headers)
        self.assertEqual(status, 403)

    # ---------- 设置 ----------

    def test_settings_roundtrip(self):
        status, data, _ = self._req('GET', '/api/v1/settings', headers=self._h())
        self.assertEqual(status, 200)
        self.assertIn('model_type', data['values'])
        self.assertNotIn('auth_mode', data['schema'])

        status, data, _ = self._req('PUT', '/api/v1/settings',
                                    {'log_level': 'WARNING'}, self._h())
        self.assertEqual(status, 200)

        status, data, _ = self._req('PUT', '/api/v1/settings',
                                    {'log_level': 'EVIL'}, self._h())
        self.assertEqual(status, 422)

        status, data, _ = self._req('PUT', '/api/v1/settings',
                                    {'not_allowed': 1}, self._h())
        self.assertEqual(status, 422)

    # ---------- 热词 ----------

    def test_hotwords_roundtrip_and_conflict(self):
        status, data, _ = self._req('PUT', '/api/v1/hotwords',
                                    {'text': '热词A\n热词B\n'}, self._h())
        self.assertEqual(status, 200)
        self.assertTrue(data.get('restart_required'))

        status, data, _ = self._req('GET', '/api/v1/hotwords', headers=self._h())
        self.assertEqual(status, 200)
        self.assertTrue(data['text'].startswith('热词A'))
        mtime = data['mtime']

        # 冲突：过期 mtime → 409
        status, data, _ = self._req('PUT', '/api/v1/hotwords',
                                    {'text': 'x', 'base_mtime': mtime - 999}, self._h())
        self.assertEqual(status, 409)

        # 匹配 mtime → 成功
        status, data, _ = self._req('PUT', '/api/v1/hotwords',
                                    {'text': '热词C\n', 'base_mtime': mtime}, self._h())
        self.assertEqual(status, 200)

    # ---------- 日志与审计 ----------

    def test_logs_endpoint(self):
        raw = '2026-09-29 INFO hello\n2026-09-29 INFO X-API-Key: sk-real-key-123 seen\n'
        Cfg.log_file.write_text(raw, encoding='utf-8', newline='')
        status, data, _ = self._req('GET', '/api/v1/logs?cursor=0', headers=self._h())
        self.assertEqual(status, 200)
        self.assertEqual(len(data['lines']), 2)
        joined = '\n'.join(data['lines'])
        self.assertIn('<redacted>', joined)
        self.assertNotIn('sk-real-key-123', joined)
        self.assertEqual(data['next_cursor'], len(raw.encode('utf-8')))

        # 游标增量：已读尽则无新行
        status, data2, _ = self._req('GET', f"/api/v1/logs?cursor={data['next_cursor']}", headers=self._h())
        self.assertEqual(data2['lines'], [])

    def test_audit_tail(self):
        status, data, _ = self._req('GET', '/api/v1/audit', headers=self._h())
        self.assertEqual(status, 200)
        self.assertIsInstance(data['entries'], list)

    # ---------- 动作 ----------

    def test_action_unknown_method(self):
        status, _, _ = self._req('POST', '/api/v1/actions/reboot', {}, self._h())
        self.assertEqual(status, 404)

    def test_logout_clears_session(self):
        status, _, _ = self._req('POST', '/api/v1/logout', {}, self._h())
        self.assertEqual(status, 200)
        status, _, _ = self._req('GET', '/api/v1/status', headers=self._h())
        self.assertEqual(status, 401)

    # ---------- R12/R13/R03：独立验收反例 ----------

    def test_r13_bootstrap_returns_csrf_for_existing_session(self):
        """R13：password 模式刷新页面（bootstrap）必须带回会话 CSRF"""
        status, data, _ = self._req('GET', '/api/v1/bootstrap',
                                    headers={'Cookie': self._h()['Cookie']})
        self.assertEqual(status, 200)
        self.assertTrue(data['authenticated'])
        self.assertEqual(data['csrf'], self.csrf)
        # 用 bootstrap 拿到的 CSRF 发写请求（刷新后场景）必须成功
        status, _, _ = self._req('PUT', '/api/v1/settings', {'log_level': 'WARNING'},
                                 {'Cookie': self._h()['Cookie'], 'X-CSRF-Token': data['csrf']})
        self.assertEqual(status, 200)

    def test_r12_restart_flags_are_honest(self):
        """R12：无法运行态应用的设置必须如实列入 restart_required"""
        status, data, _ = self._req('PUT', '/api/v1/settings',
                                    {'gpu_boost_enabled': True, 'aligner_idle_timeout': 30},
                                    self._h())
        self.assertEqual(status, 200)
        self.assertEqual(sorted(data['restart_required']),
                         ['aligner_idle_timeout', 'gpu_boost_enabled'])

    def test_r12_log_level_not_applied_marks_restart_required(self):
        """R12：控制通道不可用（ASR 离线）时 log_level 不得谎报已生效"""
        status, data, _ = self._req('PUT', '/api/v1/settings', {'log_level': 'WARNING'},
                                    self._h())
        self.assertEqual(status, 200)
        self.assertIn('log_level', data['restart_required'])

    def test_r12_log_level_applied_at_runtime(self):
        """R12/A05：ASR 在线且控制通道确认时即时应用，并如实说明生效范围"""
        from unittest import mock
        with mock.patch.object(self.app.asr, 'apply_log_level', return_value=(True, '')):
            status, data, _ = self._req('PUT', '/api/v1/settings', {'log_level': 'ERROR'},
                                        self._h())
        self.assertEqual(status, 200)
        self.assertEqual(data['restart_required'], [])
        self.assertIn('主进程', data.get('notice', ''))
        self.assertIn('worker', data.get('notice', ''))

    def test_r03_start_rejected_when_launch_invalid(self):
        """R03：启动配置无效时服务端拒绝 start（403），不是前端-only 防护"""
        from unittest import mock
        from admin.config_admin import AdminConfig as _C
        saved = (_C.asr_work_dir, _C.asr_launch_cmd, _C.asr_port,
                 _C.asr_pid_file, _C.asr_control_token_path)
        import socket as _s
        sock = _s.socket(); sock.bind(('127.0.0.1', 0)); free = sock.getsockname()[1]; sock.close()
        try:
            _C.asr_work_dir = pathlib.Path('Z:/nonexistent-asr-dir')
            _C.asr_launch_cmd = ['Z:/nonexistent/start_server.exe']
            _C.asr_port = free
            _C.asr_pid_file = pathlib.Path('Z:/nonexistent/server.pid')
            _C.asr_control_token_path = pathlib.Path('Z:/nonexistent/t.token')
            with mock.patch.object(self.app.asr.control, 'status_sync', return_value=None):
                status, data, _ = self._req('POST', '/api/v1/actions/start', {}, self._h())
            self.assertEqual(status, 403)
        finally:
            (_C.asr_work_dir, _C.asr_launch_cmd, _C.asr_port,
             _C.asr_pid_file, _C.asr_control_token_path) = saved


class GatewayModeTests(_AdminServerBase):
    """gateway 认证模式：信任部署面板登录网关注入的用户请求头"""

    @classmethod
    def setUpClass(cls):
        cls._configure()
        Cfg.admin_auth_mode = 'gateway'
        Cfg.gateway_user_headers = ('x-remote-user',)
        app = AdminApp()
        cls.app = app
        cls._start(app)

    def test_bootstrap_unauthenticated_without_gateway_header(self):
        status, data, _ = self._req('GET', '/api/v1/bootstrap')
        self.assertEqual(status, 200)
        self.assertEqual(data['mode'], 'gateway')
        self.assertFalse(data['authenticated'])

    def test_api_rejected_without_gateway_header(self):
        status, _, _ = self._req('GET', '/api/v1/status')
        self.assertEqual(status, 401)

    def test_login_disabled_in_gateway_mode(self):
        status, _, _ = self._req('POST', '/api/v1/login', {'password': 'x'})
        self.assertEqual(status, 404)

    def test_gateway_header_grants_access_and_csrf(self):
        status, data, resp = self._req('GET', '/api/v1/bootstrap',
                                       headers={'X-Remote-User': 'admin-user'})
        self.assertEqual(status, 200)
        self.assertTrue(data['authenticated'])
        csrf = data['csrf']
        cookies = self._cookie_dict(resp)
        self.assertIn('cw_admin_csrf', cookies)

        headers = {'Cookie': f"cw_admin_csrf={cookies['cw_admin_csrf']}",
                   'X-Remote-User': 'admin-user',
                   'X-CSRF-Token': csrf}
        status, data, _ = self._req('GET', '/api/v1/status', headers=headers)
        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])

    def test_write_requires_csrf_even_via_gateway(self):
        headers = {'X-Remote-User': 'admin-user'}
        status, _, _ = self._req('PUT', '/api/v1/settings', {'log_level': 'INFO'}, headers)
        self.assertEqual(status, 403)

    def test_logs_require_gateway_header(self):
        # 缺用户头的直连请求一律 401；防伪造直连由防火墙部署保证（见配置注释）
        status, _, _ = self._req('GET', '/api/v1/logs', headers={})
        self.assertEqual(status, 401)


if __name__ == '__main__':
    unittest.main()
