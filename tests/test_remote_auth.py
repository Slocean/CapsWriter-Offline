# coding: utf-8
"""
令牌存储、握手决策、日志脱敏、设置校验的单元测试（P1/P2 安全核心）
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.server.connection.auth import TokenStore  # noqa: E402
from core.server.connection import handshake  # noqa: E402
from admin.logsource import sanitize  # noqa: E402
from admin import settings_store  # noqa: E402


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._td.name, 'tokens.json')

    def tearDown(self):
        self._td.cleanup()

    def test_create_verify(self):
        store = TokenStore(self.path)
        token, entry = store.create('笔记本')
        self.assertTrue(token.startswith('cw.'))
        self.assertGreaterEqual(len(token), 32)
        hit = store.verify(token)
        self.assertEqual(hit['id'], entry['id'])
        self.assertEqual(hit['name'], '笔记本')
        self.assertIsNone(store.verify('cw.notatoken'))
        self.assertIsNone(store.verify(''))
        self.assertIsNone(store.verify(None))

    def test_plaintext_never_stored(self):
        store = TokenStore(self.path)
        token, _ = store.create('a')
        token2, _ = store.create('b')
        content = Path(self.path).read_text(encoding='utf-8')
        self.assertNotIn(token, content)
        self.assertNotIn(token2, content)
        self.assertIn('hash', content)

    def test_backup_created(self):
        store = TokenStore(self.path)
        store.create('a')
        store.create('b')  # 第二次落盘时保留上一版为 .bak
        self.assertTrue(os.path.exists(self.path + '.bak'))

    def test_revoke(self):
        store = TokenStore(self.path)
        token, entry = store.create('a')
        self.assertTrue(store.revoke(entry['id']))
        self.assertIsNone(store.verify(token))
        self.assertTrue(store.revoke(entry['id']))   # 幂等：重复吊销仍返回 True
        self.assertFalse(store.revoke('nonexistent'))  # 不存在的条目返回 False

    def test_rotate_invalidates_old(self):
        store = TokenStore(self.path)
        old, entry = store.create('a')
        result = store.rotate(entry['id'])
        self.assertIsNotNone(result)
        new, new_entry = result
        self.assertNotEqual(old, new)
        self.assertIsNone(store.verify(old))
        self.assertIsNotNone(store.verify(new))
        self.assertEqual(new_entry['name'], 'a')

    def test_summary_is_sanitized(self):
        store = TokenStore(self.path)
        store.create('secret-name')
        summary = json.dumps(store.summary())
        self.assertNotIn('hash', summary)
        self.assertNotIn('cw.', summary)

    def test_survives_reload(self):
        store = TokenStore(self.path)
        token, _ = store.create('a')
        store2 = TokenStore(self.path)
        self.assertIsNotNone(store2.verify(token))

    def test_corrupt_file_rejects_all(self):
        Path(self.path).write_text('{corrupt!!', encoding='utf-8')
        store = TokenStore(self.path)
        self.assertEqual(store.summary(), [])
        # 不会覆盖损坏文件
        self.assertIn('corrupt', Path(self.path).read_text(encoding='utf-8'))


class HandshakeDecisionTests(unittest.TestCase):
    def _call(self, ip, headers, mode, conns=0, verify=None):
        return handshake.decide(ip, headers, mode, conns, verify or (lambda t: {'id': 'ok'}))

    def test_valid_token_always_allowed(self):
        ok, status, _ = self._call('1.2.3.4', {'authorization': 'Bearer cw.x'}, 'required')
        self.assertTrue(ok)

    def test_invalid_token_rejected_even_on_lan(self):
        ok, status, _ = self._call('192.168.0.5', {'authorization': 'Bearer cw.bad'}, 'lan_legacy',
                                   verify=lambda t: None)
        self.assertFalse(ok)
        self.assertEqual(status, 401)

    def test_lan_legacy_allows_trusted_lan(self):
        ok, _, _ = self._call('192.168.0.66', {}, 'lan_legacy')
        self.assertTrue(ok)
        ok, _, _ = self._call('127.0.0.1', {}, 'lan_legacy')
        self.assertTrue(ok)

    def test_lan_legacy_rejects_unknown_source(self):
        ok, status, _ = self._call('8.8.8.8', {}, 'lan_legacy')
        self.assertFalse(ok)
        self.assertEqual(status, 401)

    def test_gateway_forwarded_requires_token(self):
        ok, status, _ = self._call('172.17.0.5', {'x-forwarded-for': '203.0.113.9'}, 'lan_legacy')
        self.assertFalse(ok)
        self.assertEqual(status, 401)

    def test_required_mode_rejects_lan_without_token(self):
        ok, status, _ = self._call('192.168.0.66', {}, 'required')
        self.assertFalse(ok)
        self.assertEqual(status, 401)

    def test_connection_limit(self):
        ok, status, _ = self._call('127.0.0.1', {'authorization': 'Bearer cw.x'}, 'required', conns=999)
        self.assertFalse(ok)
        self.assertEqual(status, 503)

    def test_token_via_custom_header(self):
        ok, _, _ = self._call('1.2.3.4', {'x-capswriter-token': 'cw.abc'}, 'required')
        self.assertTrue(ok)


class SanitizeTests(unittest.TestCase):
    def test_redacts_client_token(self):
        self.assertEqual(sanitize('使用令牌 cw.AbCdEfGh12345678 失败'), '使用令牌 <token> 失败')

    def test_redacts_bearer(self):
        self.assertEqual(sanitize('Authorization: Bearer cw.AbCdEfGh12345678'), 'Authorization: Bearer <redacted>')

    def test_redacts_control_token(self):
        self.assertEqual(sanitize('ctrl.AbCdEfGh123456789'), '<control-token>')

    def test_keeps_normal_lines(self):
        self.assertEqual(sanitize('普通日志，正常'), '普通日志，正常')


class SettingsValidationTests(unittest.TestCase):
    def test_rejects_unknown_key(self):
        _, err = settings_store.validate_settings({'root_password': 'x'})
        self.assertTrue(err)

    def test_rejects_bad_enum(self):
        _, err = settings_store.validate_settings({'model_type': 'gpt-5'})
        self.assertTrue(err)

    def test_rejects_out_of_range(self):
        _, err = settings_store.validate_settings({'aligner_idle_timeout': 99999})
        self.assertTrue(err)
        _, err = settings_store.validate_settings({'aligner_idle_timeout': -1})
        self.assertTrue(err)

    def test_rejects_non_bool(self):
        _, err = settings_store.validate_settings({'gpu_boost_enabled': 'yes'})
        self.assertTrue(err)

    def test_accepts_valid(self):
        cleaned, err = settings_store.validate_settings({
            'model_type': 'sensevoice',
            'log_level': 'INFO',
            'aligner_idle_timeout': 0,
            'gpu_boost_enabled': False,
            'auth_mode': 'required',
        })
        self.assertEqual(err, '')
        self.assertEqual(cleaned['model_type'], 'sensevoice')

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            settings_store.Cfg.settings_file = Path(td) / 'server_settings.json'
            ok, err = settings_store.save_settings({'log_level': 'WARNING'})
            self.assertEqual(err, '')
            ok, err = settings_store.save_settings({'auth_mode': 'required'})
            self.assertEqual(err, '')
            values = settings_store.load_settings()
            self.assertEqual(values['log_level'], 'WARNING')
            self.assertEqual(values['auth_mode'], 'required')
            # 备份存在
            self.assertTrue((Path(td) / 'server_settings.json.bak').exists())


if __name__ == '__main__':
    unittest.main()
