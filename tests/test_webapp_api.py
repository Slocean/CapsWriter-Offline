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
