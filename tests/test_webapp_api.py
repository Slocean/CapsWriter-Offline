# coding: utf-8
"""
管理端 HTTP API 全流程测试：登录、CSRF、设置、热词、令牌、日志、审计
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


class WebappAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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
        Cfg.repo_dir = tmp  # 令牌存储放在临时目录

        app = AdminApp()
        app.auth.set_password('test-password-1')
        cls.app = app
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

    def setUp(self):
        # 每个用例独立登录
        status, data, resp = self._req('POST', '/api/v1/login', {'password': 'test-password-1'})
        self.assertEqual(status, 200)
        cookies = {}
        for c in resp.msg.get_all('Set-Cookie'):
            k, v = c.split(';', 1)[0].split('=', 1)
            cookies[k] = v
        self.cookies = cookies
        self.csrf = data['csrf']
        self.auth = {'Cookie': f"{self.cookies['cw_admin_session']}"}

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
        self.assertIn('restart', data['schema']['model_type'])

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

    # ---------- 令牌 ----------

    def test_token_lifecycle(self):
        status, data, _ = self._req('GET', '/api/v1/client-tokens', headers=self._h())
        self.assertEqual(status, 200)

        status, data, _ = self._req('POST', '/api/v1/client-tokens', {'name': '测试机'}, self._h())
        self.assertEqual(status, 200)
        token = data['token']
        tid = data['id']
        self.assertTrue(token.startswith('cw.'))
        self.assertNotIn(tid, '')  # 占位

        status, data, _ = self._req('GET', '/api/v1/client-tokens', headers=self._h())
        names = json.dumps(data)
        self.assertNotIn(token, names)  # 列表绝不回显明文
        self.assertNotIn('hash', names)
        entry = next(t for t in data['tokens'] if t['id'] == tid)
        self.assertEqual(entry['name'], '测试机')
        self.assertFalse(entry['revoked'])

        status, data, _ = self._req('POST', f'/api/v1/client-tokens/{tid}/rotate', {}, self._h())
        self.assertEqual(status, 200)
        new_token = data['token']
        self.assertNotEqual(token, new_token)

        status, data, _ = self._req('DELETE', f'/api/v1/client-tokens/{tid}', None, self._h())
        self.assertEqual(status, 200)

        status, data, _ = self._req('POST', f'/api/v1/client-tokens/{tid}/rotate', {}, self._h())
        self.assertEqual(status, 404)

    # ---------- 日志与审计 ----------

    def test_logs_endpoint(self):
        raw = '2026-09-29 INFO hello\n2026-09-29 INFO token cw.AbCdEfGh12345678 seen\n'
        Cfg.log_file.write_text(raw, encoding='utf-8', newline='')
        status, data, _ = self._req('GET', '/api/v1/logs?cursor=0', headers=self._h())
        self.assertEqual(status, 200)
        self.assertEqual(len(data['lines']), 2)
        self.assertIn('<token>', '\n'.join(data['lines']))
        self.assertNotIn('cw.AbCd', '\n'.join(data['lines']))
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


if __name__ == '__main__':
    unittest.main()
