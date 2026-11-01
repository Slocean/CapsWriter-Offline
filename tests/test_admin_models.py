# coding: utf-8
"""
网页语音模型管理测试（下载/校验/删除/安全边界）

覆盖（对应 speech-model-management-brief.md 验证清单）：
- 下载成功 / 取消 / SHA 错误 / 字节数不符 / 超过 5GB 硬限额（下载与解压两阶段）
- 目录穿越 / 绝对路径 / 符号链接 / 可执行文件 / 布局不匹配 / CRC 损坏拒绝
- 非法模型标识 / 相同任务重复请求 / 下载与删除·ASR 启动互斥
- 运行中模型拒删拒覆盖 / 运行态未知 fail-closed / 删除仅目标目录
- 认证 / CSRF / Origin / 网关模式 / 设置切换完整性门 / 启动清扫

全部使用本地受控下载夹具（内存 zip + 本地 HTTP 服务），不下载 GB 级真实包。
"""

import hashlib
import http.client
import io
import json
import os
import pathlib
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from admin.config_admin import AdminConfig as Cfg  # noqa: E402
from admin.httpd import make_server  # noqa: E402
from admin.model_manager import CAP_BYTES, MODEL_CATALOG, ModelManager, host_allowed  # noqa: E402
from admin.ops_gate import OPS_GATE  # noqa: E402
from admin.webapp import AdminApp  # noqa: E402


# ---------------- 夹具：zip 构造与本地下载服务 ----------------

def build_zip(entries, prefix=None):
    """常规 zip：可选包装目录"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        if prefix:
            zf.writestr(prefix + '/', b'')
        for name, data in entries.items():
            zf.writestr((prefix + '/' + name) if prefix else name, data)
    return buf.getvalue()


def build_raw_zip(infos):
    """带自定义 ZipInfo 的 zip（符号链接/穿越/绝对路径等特殊条目）"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, data, mode in infos:
            zi = zipfile.ZipInfo(name)
            if mode is not None:
                zi.external_attr = mode << 16
            zf.writestr(zi, data)
    return buf.getvalue()


class _FixtureHandler(BaseHTTPRequestHandler):
    """按 self.server.routes 提供 canned 响应；支持慢速分块与 302 重定向"""

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        routes = self.server.routes
        path = self.path.split('?')[0]
        route = routes.get(path)
        if route is None:
            self.send_response(404)
            self.end_headers()
            return
        if route.get('redirect'):
            self.send_response(302)
            self.send_header('Location', route['redirect'])
            self.end_headers()
            return
        body = route['body']
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        chunk = route.get('chunk', 64 * 1024)
        delay = route.get('delay', 0)
        for i in range(0, len(body), chunk):
            self.wfile.write(body[i:i + chunk])
            if delay:
                time.sleep(delay)


class _DownloadFixture:
    """本地 HTTP 下载夹具：注册 path → 响应，提供 base_url"""

    def __init__(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _FixtureHandler)
        self.server.routes = {}
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.02}, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return f'http://127.0.0.1:{self.port}'

    def add(self, path, body, **kw):
        self.server.routes[path] = dict(body=body, **kw)

    def add_redirect(self, path, location):
        self.server.routes[path] = dict(redirect=location)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def sha256(b):
    return hashlib.sha256(b).hexdigest()


# ---------------- 测试用模型目录（小型受控清单） ----------------

MAIN_ZIP = build_zip({'model.onnx': b'M' * 4096, 'tokens.txt': b'tokens\n'},
                     prefix='ModelA-Main')
AUX_ZIP = build_zip({'punct.onnx': b'P' * 2048}, prefix='AuxPunct')
FLAT_ZIP = build_zip({'flat.gguf': b'G' * 8192})


def make_catalog(base):
    return {
        'cap_bytes': CAP_BYTES,
        'policy': {'scheme': 'http', 'host_globs': ['127.0.0.1']},
        'release_page': 'http://127.0.0.1/release',
        'max_files_per_zip': 100,
        'sets': {
            'set_a': {
                'label': '测试模型A', 'model_type': 'sensevoice', 'variant': None,
                'description': '带辅助组件的主模型',
                'components': [
                    {'asset': 'main.zip', 'url': f'{base}/main.zip',
                     'size': len(MAIN_ZIP), 'sha256': sha256(MAIN_ZIP),
                     'installed_size': None,
                     'extract_into': 'ModelA', 'strip_prefix': 'ModelA-Main',
                     'flat_names': None,
                     'required': ('model.onnx', 'tokens.txt'),
                     'purpose': '识别主模型', 'kind': 'main', 'variant_marker': None},
                    {'asset': 'aux.zip', 'url': f'{base}/aux.zip',
                     'size': len(AUX_ZIP), 'sha256': sha256(AUX_ZIP),
                     'installed_size': None,
                     'extract_into': 'Punct', 'strip_prefix': 'AuxPunct',
                     'flat_names': None,
                     'required': ('punct.onnx',),
                     'purpose': '标点辅助模型', 'kind': 'aux', 'variant_marker': None},
                ],
            },
            'set_b': {
                'label': '测试模型B(平铺)', 'model_type': 'paraformer', 'variant': None,
                'description': '平铺布局',
                'components': [
                    {'asset': 'flat.zip', 'url': f'{base}/flat.zip',
                     'size': len(FLAT_ZIP), 'sha256': sha256(FLAT_ZIP),
                     'installed_size': None,
                     'extract_into': 'ModelB/Run', 'strip_prefix': None,
                     'flat_names': ('flat.gguf',),
                     'required': ('flat.gguf',),
                     'purpose': '识别主模型', 'kind': 'main', 'variant_marker': None},
                ],
            },
        },
    }


# ---------------- 假 ASR 与共享基建 ----------------

class _FakeAsr:
    def __init__(self, state='stopped', detail=None):
        self.state = state
        self.detail = detail or {}
        self.submit_calls = []

    def status(self):
        return {'state': self.state, 'busy': False, 'detail': dict(self.detail),
                'control_ok': self.state == 'online', 'controllable': self.state == 'online',
                'can_start': self.state == 'stopped', 'can_stop': self.state == 'online',
                'can_restart': self.state == 'online', 'port_open': self.state == 'online'}

    def current_action(self):
        return None

    def submit(self, kind, force=False):
        self.submit_calls.append(kind)
        return {'ok': False, 'error': 'conflict'}


class _ModelsBase(unittest.TestCase):
    """临时目录 + 线程内 admin 服务器 + 已登录会话"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = pathlib.Path(cls._tmp.name)
        cls.tmp = tmp
        asr_dir = tmp / 'asr'
        asr_dir.mkdir()
        Cfg.data_dir = tmp / 'admin-data'
        Cfg.static_dir = pathlib.Path(__file__).resolve().parents[1] / 'admin' / 'static'
        Cfg.password_file = Cfg.data_dir / 'admin_password.json'
        Cfg.audit_log = Cfg.data_dir / 'audit.log'
        Cfg.settings_file = asr_dir / 'server_settings.json'
        Cfg.hotwords_path = asr_dir / 'hot-server.txt'
        Cfg.log_file = asr_dir / 'server.log'
        Cfg.repo_dir = tmp
        Cfg.asr_work_dir = asr_dir
        Cfg.asr_launch_cmd = []
        Cfg.asr_entry = asr_dir / 'start_server.py'
        Cfg.asr_pid_file = asr_dir / 'logs' / 'server.pid'
        Cfg.asr_control_token_path = asr_dir / 'logs' / 'server_control.token'
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        Cfg.asr_port = sock.getsockname()[1]
        sock.close()

        cls.fixture = _DownloadFixture()
        cls.catalog = make_catalog(cls.fixture.base)
        cls.fixture.add('/main.zip', MAIN_ZIP)
        cls.fixture.add('/aux.zip', AUX_ZIP)
        cls.fixture.add('/flat.zip', FLAT_ZIP)

        Cfg.admin_auth_mode = 'password'
        cls.app = AdminApp()
        cls.app.models = ModelManager(asr=cls.app.asr, catalog=cls.catalog)
        cls.asr = _FakeAsr()
        cls.app.asr = cls.asr
        cls.app.models._asr = cls.asr

        cls.server = make_server('127.0.0.1', 0, cls.app)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={'poll_interval': 0.05}, daemon=True)
        cls.thread.start()

        cls.app.auth.set_password('test-password-1')

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.fixture.close()
        cls._tmp.cleanup()

    def setUp(self):
        # 用例间隔离：等上一用例的下载/删除线程真正退出（闸门释放），
        # 再显式忘记已结束的任务展示——绝不硬清 pending/running 任务
        self._wait_settled()
        self.app.models.forget_finished_task()
        self.app.models._cancel.clear()
        # 临时目录内清空 models 根：上一用例安装/删除残留不影响本用例
        shutil.rmtree(pathlib.Path(Cfg.asr_work_dir) / 'models', ignore_errors=True)
        status, data, resp = self._req('POST', '/api/v1/login', {'password': 'test-password-1'})
        self.assertEqual(status, 200)
        cookies = {}
        for c in resp.msg.get_all('Set-Cookie') or []:
            k, v = c.split(';', 1)[0].split('=', 1)
            cookies[k] = v
        self.cookies = cookies
        self.csrf = data['csrf']
        self.models_root = pathlib.Path(Cfg.asr_work_dir) / 'models'

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

    def _h(self, csrf=True):
        headers = {'Cookie': '; '.join(f'{k}={v}' for k, v in self.cookies.items())}
        if csrf:
            headers['X-CSRF-Token'] = self.csrf
        return headers

    def _models(self):
        status, data, _ = self._req('GET', '/api/v1/models', headers=self._h())
        self.assertEqual(status, 200)
        return data

    def _model(self, key):
        return next(m for m in self._models()['models'] if m['key'] == key)

    def _post(self, path, body=None):
        return self._req('POST', path, body or {}, self._h())

    def _wait_settled(self, timeout=20.0):
        """等待模型下载/删除线程完全退出（共享操作闸门释放）。
        恢复夹具（如改回路由/清单）之前必须先结算，避免线程还在读旧夹具。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            owner = OPS_GATE.owner
            if owner is None or not str(owner).startswith(('model-', 'asr-action')):
                return
            time.sleep(0.05)
        self.fail(f'操作线程未退出，闸门仍被 {OPS_GATE.owner} 占用')

    def _wait_task(self, states, timeout=15.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            task = self._models()['task']
            last = task
            if task and task.get('state') in states:
                return task
            time.sleep(0.1)
        self.fail(f'任务未进入 {states}，最后状态: {last}')

    def _wait_bytes(self, minimum, timeout=10.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            task = self._models()['task']
            last = task
            if task and (task.get('bytes_received') or 0) >= minimum:
                return task
            time.sleep(0.05)
        self.fail(f'下载进度未推进：最后任务状态 {last}')

    def _serve_as_main(self, payload, **kw):
        """把自造 zip 作为 set_a 主资产伺服（同步清单大小/SHA），返回恢复函数"""
        comp = self.catalog['sets']['set_a']['components'][0]
        saved = (comp['size'], comp['sha256'])
        comp['size'] = len(payload)
        comp['sha256'] = sha256(payload)
        self.fixture.add('/main.zip', payload, **kw)

        def restore():
            comp['size'], comp['sha256'] = saved
            self.fixture.add('/main.zip', MAIN_ZIP)
        return restore

    def _serve_as_setb(self, payload, **kw):
        """把自造 zip 作为 set_b 资产伺服（同步清单大小/SHA），返回恢复函数"""
        comp = self.catalog['sets']['set_b']['components'][0]
        saved = (comp['size'], comp['sha256'])
        comp['size'] = len(payload)
        comp['sha256'] = sha256(payload)
        self.fixture.add('/flat.zip', payload, **kw)

        def restore():
            comp['size'], comp['sha256'] = saved
            self.fixture.add('/flat.zip', FLAT_ZIP)
        return restore

    def _slow_download_window(self):
        """
        临时把 set_a 主资产换成 ~200KB 慢速 zip（约 1 秒下载窗口），
        返回恢复函数；用于需要在“任务运行中”窗口内做断言的用例。
        """
        # 用不可压缩的随机字节造包：deflate 后仍 ~400KB（>下载端单次 read 的
        # 256KB），保证 bytes_received 能被轮询到中间值、取消窗口足够
        slow = build_zip({'model.onnx': os.urandom(400_000), 'tokens.txt': b'tokens\n'},
                         prefix='ModelA-Main')
        comp = self.catalog['sets']['set_a']['components'][0]
        saved = (comp['size'], comp['sha256'])
        comp['size'] = len(slow)
        comp['sha256'] = sha256(slow)
        self.fixture.add('/main.zip', slow, chunk=2048, delay=0.01)

        def restore():
            comp['size'], comp['sha256'] = saved
            self.fixture.add('/main.zip', MAIN_ZIP)
        return restore

    def _install_files(self, key, only_main=False):
        """手动放置完整安装文件（模拟已安装状态，不经过下载）"""
        info = make_catalog(self.fixture.base)['sets'][key]
        for c in info['components']:
            if only_main and c['kind'] != 'main':
                continue
            leaf_rel = c['extract_into'] + (f"/{c['strip_prefix']}" if c['strip_prefix'] else '')
            for rel in c['required']:
                p = self.models_root / leaf_rel / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b'installed-by-test')
        return info


# ---------------- 用例 ----------------

class ModelAPITests(_ModelsBase):

    def test_models_requires_auth(self):
        status, _, _ = self._req('GET', '/api/v1/models')
        self.assertEqual(status, 401)

    def test_write_requires_csrf(self):
        status, _, _ = self._req('POST', '/api/v1/models/download', {'model': 'set_a'},
                                 self._h(csrf=False))
        self.assertEqual(status, 403)
        headers = self._h()
        headers['Origin'] = 'https://evil.example.com'
        status, _, _ = self._req('POST', '/api/v1/models/delete', {'model': 'set_a'}, headers)
        self.assertEqual(status, 403)

    def test_list_uninstalled(self):
        data = self._models()
        keys = [m['key'] for m in data['models']]
        self.assertEqual(keys, ['set_a', 'set_b'])
        m = data['models'][0]
        self.assertFalse(m['complete'])
        self.assertFalse(m['main_ready'])
        self.assertEqual(len(m['missing_components']), 2)
        self.assertFalse(m['running'])
        self.assertIsNone(m['download_block'])
        self.assertIsNone(m['delete_block'])
        self.assertEqual(m['cap_bytes'], CAP_BYTES)
        self.assertEqual(data['cap_bytes'], CAP_BYTES)
        self.assertIsNone(data['task'])
        # 资产信息暴露名称/字节/SHA-256
        main = m['components'][0]['asset']
        self.assertEqual(main['size'], len(MAIN_ZIP))
        self.assertEqual(main['sha256'], sha256(MAIN_ZIP))

    def test_download_success(self):
        status, data, _ = self._post('/api/v1/models/download', {'model': 'set_a'})
        self.assertEqual(status, 200)
        self.assertIn(data['task']['state'], ('pending', 'running'))
        task = self._wait_task({'done'})
        self._wait_settled()
        self.assertEqual(task['asset_count'], 2)
        leaf = self.models_root / 'ModelA' / 'ModelA-Main'
        self.assertEqual((leaf / 'model.onnx').read_bytes(), b'M' * 4096)
        self.assertEqual((leaf / 'tokens.txt').read_bytes(), b'tokens\n')
        aux = self.models_root / 'Punct' / 'AuxPunct' / 'punct.onnx'
        self.assertEqual(aux.read_bytes(), b'P' * 2048)
        # 阶段目录清理干净
        self.assertFalse((self.models_root / '.staging').exists())
        # 状态与切换门
        m = self._model('set_a')
        self.assertTrue(m['complete'])
        self.assertTrue(m['switchable'])
        self.assertGreater(m['size_bytes'], 0)
        ok, why = self.app.models.check_switchable('sensevoice')
        self.assertTrue(ok)

    def test_download_flat_layout(self):
        status, _, _ = self._post('/api/v1/models/download', {'model': 'set_b'})
        self.assertEqual(status, 200)
        self._wait_task({'done'})
        self.assertEqual((self.models_root / 'ModelB' / 'Run' / 'flat.gguf').read_bytes(),
                         b'G' * 8192)

    def test_download_complete_refused(self):
        self._install_files('set_a')
        status, data, _ = self._post('/api/v1/models/download', {'model': 'set_a'})
        self.assertEqual(status, 403)
        self.assertIn('已完整安装', data['error'])

    def test_download_unknown_key(self):
        status, data, _ = self._post('/api/v1/models/download', {'model': '../evil'})
        self.assertEqual(status, 422)
        status, data, _ = self._post('/api/v1/models/download', {'model': 'nope'})
        self.assertEqual(status, 422)

    def test_download_wrong_sha(self):
        bad = bytearray(MAIN_ZIP)
        bad[-1] ^= 0xFF   # 内容不同 → SHA 不匹配（大小也可能不同，先拦截大小或 SHA）
        self.fixture.add('/main.zip', bytes(bad))
        try:
            status, _, _ = self._post('/api/v1/models/download', {'model': 'set_a'})
            self.assertEqual(status, 200)
            task = self._wait_task({'failed'})
            self.assertTrue('SHA-256' in task['error'] or '字节' in task['error'],
                            task['error'])
            self.assertFalse((self.models_root / 'ModelA').exists())
            self.assertFalse((self.models_root / '.cw-model-staging').exists())
        finally:
            self._wait_settled()
            self.fixture.add('/main.zip', MAIN_ZIP)

    def test_download_size_mismatch_refused(self):
        self.fixture.add('/main.zip', MAIN_ZIP + b'extra')
        try:
            status, _, _ = self._post('/api/v1/models/download', {'model': 'set_a'})
            self.assertEqual(status, 200)
            task = self._wait_task({'failed'})
            self.assertIn('字节', task['error'])
            self.assertFalse((self.models_root / 'ModelA').exists())
        finally:
            self._wait_settled()
            self.fixture.add('/main.zip', MAIN_ZIP)

    def test_cancel_running_download(self):
        restore = self._slow_download_window()
        try:
            status, _, _ = self._post('/api/v1/models/download', {'model': 'set_a'})
            self.assertEqual(status, 200)
            self._wait_bytes(4096)
            status, data, _ = self._post('/api/v1/models/task/cancel')
            self.assertEqual(status, 200)
            task = self._wait_task({'cancelled'})
            self._wait_settled()
            self.assertEqual(task['state'], 'cancelled')
            self.assertFalse((self.models_root / 'ModelA').exists())
            # 阶段目录被取消任务自己清理（允许留下空暂存根）
            staging = self.models_root / '.cw-model-staging'
            self.assertFalse(staging.exists() and any(staging.iterdir()))
        finally:
            self._wait_settled()
            restore()

    def test_duplicate_download_conflict(self):
        restore = self._slow_download_window()
        try:
            status, _, _ = self._post('/api/v1/models/download', {'model': 'set_a'})
            self.assertEqual(status, 200)
            self._wait_bytes(4096)
            status, data, _ = self._post('/api/v1/models/download', {'model': 'set_b'})
            self.assertEqual(status, 409)
            status, data, _ = self._post('/api/v1/models/delete', {'model': 'set_b', 'confirm': True})
            self.assertEqual(status, 409)
            self._post('/api/v1/models/task/cancel')
            self._wait_task({'cancelled'})
            self._wait_settled()
        finally:
            self._wait_settled()
            restore()

    def test_over_cap_download_refused(self):
        # 下载阶段真实流字节限制用受控小上限验证（不分配 GB 级内存）；
        # 生产常量 5,000,000,000 由 ProductionCatalogTests 单独断言
        payload = build_zip({'flat.gguf': os.urandom(2 * 1024 * 1024)})
        catalog = make_catalog(self.fixture.base)
        catalog['cap_bytes'] = 1024 * 1024
        catalog['sets'] = {
            'set_big': dict(catalog['sets']['set_b'],
                            components=[dict(catalog['sets']['set_b']['components'][0],
                                             url=f'{self.fixture.base}/big.zip',
                                             size=len(payload), sha256=sha256(payload))]),
        }
        self.fixture.add('/big.zip', payload, chunk=256 * 1024)
        self.app.models._catalog = catalog
        try:
            status, _, _ = self._post('/api/v1/models/download', {'model': 'set_big'})
            self.assertEqual(status, 200)
            task = self._wait_task({'failed'})
            self.assertIn('超过', task['error'])
            self.assertFalse((self.models_root / 'ModelB').exists())
        finally:
            self._wait_settled()
            self.app.models._catalog = self.catalog
            del self.fixture.server.routes['/big.zip']

    def test_over_cap_extraction_refused(self):
        # 解压阶段：展开总量预算超限（zip 元数据校验先行拦截）
        entries = {'flat.gguf': b'Q' * (2 * 1024 * 1024)}
        payload = build_zip(entries)
        catalog = make_catalog(self.fixture.base)
        catalog['cap_bytes'] = 1024 * 1024
        catalog['sets']['set_b'] = dict(
            catalog['sets']['set_b'],
            components=[dict(catalog['sets']['set_b']['components'][0],
                             size=len(payload), sha256=sha256(payload))])
        self.app.models._catalog = catalog
        restore = self._serve_as_setb(payload)
        try:
            status, _, _ = self._post('/api/v1/models/download', {'model': 'set_b'})
            self.assertEqual(status, 200)
            task = self._wait_task({'failed'})
            self.assertIn('上限', task['error'])
            self.assertFalse((self.models_root / 'ModelB').exists())
        finally:
            self._wait_settled()
            self.app.models._catalog = self.catalog
            restore()

    def test_zip_traversal_rejected(self):
        for evil in ('../evil.txt', '/abs/evil.txt', 'a\\..\\..\\evil.txt', 'ModelA-Main/../../evil.txt'):
            payload = build_raw_zip([
                ('ModelA-Main/model.onnx', b'M' * 1024, None),
                (evil, b'x' * 32, None),
            ])
            restore = self._serve_as_main(payload)
            try:
                status, _, _ = self._post('/api/v1/models/download', {'model': 'set_a'})
                self.assertEqual(status, 200)
                task = self._wait_task({'failed'})
                self.assertIn('非法路径', task['error'])
                self.assertFalse((self.models_root / 'ModelA').exists())
                self.assertFalse((self.tmp / 'evil.txt').exists())
                self.assertFalse((self.tmp / 'asr' / 'evil.txt').exists())
            finally:
                self._wait_settled()
                restore()

    def test_zip_symlink_rejected(self):
        payload = build_raw_zip([
            ('ModelA-Main/model.onnx', b'M' * 1024, None),
            ('ModelA-Main/link.onnx', b'../other', 0o120777),
        ])
        restore = self._serve_as_main(payload)
        try:
            status, _, _ = self._post('/api/v1/models/download', {'model': 'set_a'})
            task = self._wait_task({'failed'})
            self.assertIn('符号链接', task['error'])
            self.assertFalse((self.models_root / 'ModelA').exists())
        finally:
            self._wait_settled()
            restore()

    def test_zip_executable_rejected(self):
        payload = build_zip({'model.onnx': b'M', 'tool.exe': b'MZ...'}, prefix='ModelA-Main')
        restore = self._serve_as_main(payload)
        try:
            self._post('/api/v1/models/download', {'model': 'set_a'})
            task = self._wait_task({'failed'})
            self.assertIn('可执行文件', task['error'])
        finally:
            self._wait_settled()
            restore()

    def test_zip_layout_mismatch_rejected(self):
        payload = build_zip({'other/file.txt': b'x'}, prefix='NotExpected')
        restore = self._serve_as_main(payload)
        try:
            self._post('/api/v1/models/download', {'model': 'set_a'})
            task = self._wait_task({'failed'})
            self.assertIn('布局', task['error'])
        finally:
            self._wait_settled()
            restore()

    def test_zip_flat_unexpected_file_rejected(self):
        payload = build_zip({'flat.gguf': b'G', 'extra.bin': b'x'})
        restore = self._serve_as_setb(payload)
        try:
            self._post('/api/v1/models/download', {'model': 'set_b'})
            task = self._wait_task({'failed'})
            self.assertIn('固定清单', task['error'])
        finally:
            self._wait_settled()
            restore()

    def test_zip_corrupted_crc_rejected(self):
        payload = bytearray(build_zip({'model.onnx': b'M' * 100000}, prefix='ModelA-Main'))
        # 翻转文件数据区中部一个字节 → CRC 校验失败（或解压异常），不得发布
        payload[len(payload) // 3] ^= 0xFF
        restore = self._serve_as_main(bytes(payload))
        try:
            self._post('/api/v1/models/download', {'model': 'set_a'})
            task = self._wait_task({'failed'})
            self.assertFalse((self.models_root / 'ModelA').exists())
        finally:
            self._wait_settled()
            restore()

    def test_delete_only_target_dirs(self):
        self._install_files('set_a')
        self._install_files('set_b')
        stub = self.models_root / '模型下载链接.txt'
        stub.write_text('stub', encoding='utf-8')
        status, data, _ = self._post('/api/v1/models/delete', {'model': 'set_a', 'confirm': True})
        self.assertEqual(status, 200)
        self.assertEqual(len(data['deleted']), 2)   # 主模型 + 标点辅助
        self.assertTrue((self.models_root / 'ModelB' / 'Run' / 'flat.gguf').exists())
        self.assertTrue(stub.exists())
        self.assertTrue(self.models_root.exists())
        # 空父目录被清理（ModelA 已无内容）
        self.assertFalse((self.models_root / 'ModelA').exists())

    def test_delete_uninstalled_refused(self):
        status, data, _ = self._post('/api/v1/models/delete', {'model': 'set_a', 'confirm': True})
        self.assertEqual(status, 403)
        self.assertIn('尚未安装', data['error'])

    def test_delete_requires_confirm(self):
        status, data, _ = self._post('/api/v1/models/delete', {'model': 'set_a'})
        self.assertEqual(status, 422)

    def test_delete_unknown_key(self):
        status, data, _ = self._post('/api/v1/models/delete', {'model': 'nope', 'confirm': True})
        self.assertEqual(status, 422)

    def test_running_model_delete_refused(self):
        self._install_files('set_a')
        self.asr.state = 'online'
        self.asr.detail = {'model_type': 'sensevoice'}
        try:
            status, data, _ = self._post('/api/v1/models/delete', {'model': 'set_a', 'confirm': True})
            self.assertEqual(status, 403)
            self.assertIn('运行', data['error'])
            m = self._model('set_a')
            self.assertTrue(m['running'])
            self.assertTrue(m['delete_block'])
            # 运行模型的下载也被拒（即使未完整，也不允许覆盖运行目录）
            status, data, _ = self._post('/api/v1/models/download', {'model': 'set_a'})
            self.assertIn(status, (403, 409))
        finally:
            self.asr.state = 'stopped'
            self.asr.detail = {}

    def test_unknown_asr_state_blocks_destructive(self):
        """状态未知（stale/无 asr）时 fail-closed：一切删除都被拒"""
        self._install_files('set_a')
        self.asr.state = 'stale'
        try:
            status, data, _ = self._post('/api/v1/models/delete', {'model': 'set_a', 'confirm': True})
            self.assertEqual(status, 403)
            self.assertIn('无法核实', data['error'])
            m = self._model('set_b')
            self.assertTrue(m['delete_block'])
        finally:
            self.asr.state = 'stopped'

    def test_delete_selected_stopped_model_allowed_with_note(self):
        self._install_files('set_a')
        Cfg.settings_file.write_text(json.dumps({'model_type': 'sensevoice'}), encoding='utf-8')
        try:
            status, data, _ = self._post('/api/v1/models/delete', {'model': 'set_a', 'confirm': True})
            self.assertEqual(status, 200)
            self.assertIn('选中', data['note'])
            m = self._model('set_a')
            self.assertTrue(m['selected'])
            self.assertFalse(m['complete'])
        finally:
            if Cfg.settings_file.exists():
                Cfg.settings_file.unlink()

    def test_start_blocked_while_download_running(self):
        self._install_files('set_a')
        Cfg.settings_file.write_text(json.dumps({'model_type': 'sensevoice'}), encoding='utf-8')
        slow_flat = build_zip({'flat.gguf': os.urandom(400_000)})
        restore_b = self._serve_as_setb(slow_flat, chunk=2048, delay=0.01)
        try:
            status, _, _ = self._post('/api/v1/models/download', {'model': 'set_b'})
            self.assertEqual(status, 200)
            self._wait_bytes(4096)
            status, data, _ = self._post('/api/v1/actions/start')
            self.assertEqual(status, 403)
            self.assertIn('下载任务', data['error'])
            self.asr.submit_calls = []
        finally:
            self._post('/api/v1/models/task/cancel')
            self._wait_task({'cancelled'})
            self._wait_settled()
            restore_b()
            if Cfg.settings_file.exists():
                Cfg.settings_file.unlink()


    def test_start_blocked_when_selected_model_incomplete(self):
        Cfg.settings_file.write_text(json.dumps({'model_type': 'paraformer'}), encoding='utf-8')
        try:
            status, data, _ = self._post('/api/v1/actions/start')
            self.assertEqual(status, 403)
            self.assertIn('未安装或不完整', data['error'])
            self.assertEqual(self.asr.submit_calls, [])
        finally:
            if Cfg.settings_file.exists():
                Cfg.settings_file.unlink()

    def test_settings_switch_gate(self):
        # 未安装 → 拒绝切换
        status, data, _ = self._req('PUT', '/api/v1/settings',
                                    {'model_type': 'sensevoice'}, self._h())
        self.assertEqual(status, 422)
        self.assertIn('未安装或不完整', data['error'])
        # 装好后 → 允许，且标记需重启
        self._install_files('set_a')
        status, data, _ = self._req('PUT', '/api/v1/settings',
                                    {'model_type': 'sensevoice'}, self._h())
        self.assertEqual(status, 200)
        self.assertIn('model_type', data['restart_required'])
        # 非法取值仍被 schema 拒绝
        status, _, _ = self._req('PUT', '/api/v1/settings', {'model_type': 'evil'}, self._h())
        self.assertEqual(status, 422)

    def test_main_ready_aux_missing_can_recover(self):
        """主模型完整但缺辅助组件：可切换、可只补缺失组件，完整组件不覆盖"""
        self._install_files('set_a', only_main=True)
        main_file = self.models_root / 'ModelA' / 'ModelA-Main' / 'model.onnx'
        before = main_file.read_bytes()
        m = self._model('set_a')
        self.assertFalse(m['complete'])            # 整套未完整（缺标点辅助）
        self.assertTrue(m['main_ready'])           # 主模型就绪
        self.assertTrue(m['switchable'])
        self.assertEqual(m['missing_components'], ['aux.zip'])
        self.assertIsNone(m['download_block'])     # 允许补下载
        ok, why = self.app.models.check_switchable('sensevoice')
        self.assertTrue(ok)
        # 只补缺失组件
        status, data, _ = self._post('/api/v1/models/download', {'model': 'set_a'})
        self.assertEqual(status, 200)
        task = self._wait_task({'done'})
        self._wait_settled()
        self.assertEqual(task['asset_count'], 1)   # 只有辅助组件
        # 完整的主组件未被覆盖，缺失的辅助组件已装
        self.assertEqual(main_file.read_bytes(), before)
        self.assertEqual((self.models_root / 'Punct' / 'AuxPunct' / 'punct.onnx').read_bytes(),
                         b'P' * 2048)
        m = self._model('set_a')
        self.assertTrue(m['complete'])
        self.assertTrue(self.app.models.set_fully_complete('sensevoice'))

    def test_cancel_then_resume_installs_only_missing(self):
        """取消后重新下载：只补缺失组件，已完成组件保留"""
        restore = self._slow_download_window()
        try:
            status, _, _ = self._post('/api/v1/models/download', {'model': 'set_a'})
            self.assertEqual(status, 200)
            self._wait_bytes(4096)
            self._post('/api/v1/models/task/cancel')
            self._wait_task({'cancelled'})
            self._wait_settled()
            self.assertFalse((self.models_root / 'ModelA').exists())
        finally:
            self._wait_settled()
            restore()
        # 恢复：重新下载只补缺失（此时两个组件都缺，装完整套）
        status, _, _ = self._post('/api/v1/models/download', {'model': 'set_a'})
        self.assertEqual(status, 200)
        self._wait_task({'done'})
        self._wait_settled()
        m = self._model('set_a')
        self.assertTrue(m['complete'])
        # 取消发生在主组件阶段：主组件此前未发布，这次全部补齐
        self.assertEqual((self.models_root / 'ModelA' / 'ModelA-Main' / 'model.onnx').read_bytes(),
                         b'M' * 4096)

    def test_budget_counts_existing_components(self):
        """5GB 预算包含既有组件字节：既有 600KB + 新组件 600KB 超 1MB 上限被拒"""
        payload = build_zip({'punct.onnx': os.urandom(600 * 1024)}, prefix='AuxPunct')
        catalog = make_catalog(self.fixture.base)
        catalog['cap_bytes'] = 1024 * 1024
        # 主组件已在磁盘上占 600KB
        main_leaf = self.models_root / 'ModelA' / 'ModelA-Main'
        main_leaf.mkdir(parents=True)
        (main_leaf / 'model.onnx').write_bytes(os.urandom(600 * 1024))
        (main_leaf / 'tokens.txt').write_bytes(b't')
        # 辅助组件资产换成 600KB
        comp = catalog['sets']['set_a']['components'][1]
        comp['size'] = len(payload)
        comp['sha256'] = sha256(payload)
        self.fixture.add('/aux.zip', payload)
        saved_catalog = self.app.models._catalog
        self.app.models._catalog = catalog
        try:
            status, _, _ = self._post('/api/v1/models/download', {'model': 'set_a'})
            self.assertEqual(status, 200)
            task = self._wait_task({'failed'})
            self.assertIn('超过', task['error'])
            self.assertFalse((self.models_root / 'Punct').exists())
        finally:
            self._wait_settled()
            self.app.models._catalog = saved_catalog
            self.fixture.add('/aux.zip', AUX_ZIP)
            shutil.rmtree(self.models_root / 'ModelA', ignore_errors=True)
            shutil.rmtree(self.models_root / 'Punct', ignore_errors=True)

    def test_stage_root_junction_rejected_before_install(self):
        """暂存根是 junction：任何写入前拒绝，任务失败且绝不透过它写 models 外"""
        import _winapi
        external = self.tmp / 'outside-models'
        external.mkdir(exist_ok=True)
        (external / 'keep.txt').write_text('owned independent fixture', encoding='utf-8')
        self.models_root.mkdir(parents=True, exist_ok=True)
        link = self.models_root / ModelManager.STAGING_DIRNAME
        _winapi.CreateJunction(str(external), str(link))
        attempted = []

        def observe_install(t, c, stage, idx):
            attempted.append(str(stage))
            stage.mkdir(parents=True)
            (stage / 'owned-write.txt').write_text('fixture', encoding='utf-8')

        manager = self.app.models
        original_install = manager._install_component
        manager._install_component = observe_install
        task = {'id': '0123456789ab', 'model': 'set_a', 'state': 'pending'}
        with manager._lock:
            manager._task = task
        owner = 'test-stage-junction'
        self.assertTrue(OPS_GATE.acquire(owner))
        try:
            manager._run_download(task, self.catalog['sets']['set_a'],
                                  self.catalog['sets']['set_a']['components'], owner)
        finally:
            manager._install_component = original_install   # 恢复，防污染后续用例
        self.assertEqual(attempted, [])                      # 安装从未开始
        self.assertEqual(task['state'], 'failed')
        self.assertTrue((external / 'keep.txt').exists())    # 外部数据原样
        self.assertFalse((external / '0123456789ab').exists())  # 绝不写入外部

    def test_preexisting_task_dir_preserved_on_create_failure(self):
        """任务目录创建失败（已存在且非本任务创建）：任务失败且绝不清理既有内容"""
        staging = self.models_root / ModelManager.STAGING_DIRNAME
        collision = staging / ('c' * 12)
        collision.mkdir(parents=True)
        keep = collision / 'preexisting.txt'
        keep.write_text('not created by this download', encoding='utf-8')
        manager = self.app.models
        task = {'id': collision.name, 'model': 'set_a', 'state': 'pending'}
        with manager._lock:
            manager._task = task
        owner = 'test-stage-collision'
        self.assertTrue(OPS_GATE.acquire(owner))
        manager._run_download(task, self.catalog['sets']['set_a'],
                              self.catalog['sets']['set_a']['components'], owner)
        self.assertTrue(keep.exists())                       # 既有内容保留
        self.assertEqual(task['state'], 'failed')
        self.assertIn('已存在', task['error'])
        # 归属 marker 不应被写入既有目录
        self.assertFalse((collision / ModelManager.STAGE_MARKER_NAME).exists())

    def test_staging_swept_on_restart(self):
        import json as _json
        staging = self.models_root / '.cw-model-staging'
        # 带归属 marker 的本模块任务目录：启动时清理
        orphan = staging / ('a' * 12)
        orphan.mkdir(parents=True)
        (orphan / 'x.tmp').write_bytes(b'x')
        (orphan / ModelManager.STAGE_MARKER_NAME).write_text(_json.dumps(
            {'owner': ModelManager.STAGE_OWNER, 'task_id': 'a' * 12, 'model': 'set_a'}),
            encoding='utf-8')
        # 同名（12位hex）但无 marker：归属不可验证，必须保留
        unowned = staging / ('b' * 12)
        unowned.mkdir(parents=True)
        (unowned / 'keep.txt').write_bytes(b'k')
        # 命名不像本模块任务 ID 的外部数据：必须保留
        other = self.models_root / '.staging' / 'OtherApplication' / 'keep.txt'
        other.parent.mkdir(parents=True)
        other.write_bytes(b'preserve unrelated staging data')
        ModelManager(asr=self.asr, catalog=self.catalog)
        self.assertFalse(orphan.exists())
        self.assertTrue(unowned.exists())
        self.assertTrue(other.exists())

    def test_redirect_to_unknown_host_blocked(self):
        payload = MAIN_ZIP
        self.fixture.add('/main.zip', payload)
        self.fixture.add_redirect('/main.zip', 'http://evil.example/main.zip')
        try:
            self._post('/api/v1/models/download', {'model': 'set_a'})
            task = self._wait_task({'failed'})
            self.assertIn('允许', task['error'])
        finally:
            self._wait_settled()
            self.fixture.add('/main.zip', MAIN_ZIP)

    def test_redirect_within_allowlist_ok(self):
        self.fixture.add_redirect('/main.zip', f'{self.fixture.base}/main-2.zip')
        self.fixture.add('/main-2.zip', MAIN_ZIP)
        try:
            status, _, _ = self._post('/api/v1/models/download', {'model': 'set_a'})
            self.assertEqual(status, 200)
            self._wait_task({'done'})
            self._wait_settled()
        finally:
            self._wait_settled()
            self.fixture.add('/main.zip', MAIN_ZIP)
            del self.fixture.server.routes['/main-2.zip']


class ProductionCatalogTests(unittest.TestCase):
    """生产清单钉死校验：官方资产字节/SHA-256 与 2026-10-03 GitHub API 一致"""

    EXPECTED = {
        'Fun-ASR-Nano-GGUF.zip': (834231292, '26a557923aedc44f1a3033d0a9b9c7b13cbb551f57fb9fd4b15a67bb4b57f998'),
        'Paraformer.zip': (239979687, 'a12a3f9791483329441c94ad759cbcf258d7246784a6d368cd3c591add4d888b'),
        'Punct-CT-Transformer.zip': (271770396, 'de106e6cf13764bd3124f31864bc30158f04961788765b63262bdd5ba21fa421'),
        'Qwen3-ASR-1.7B-q4_k.zip': (1410584449, '9b3d2a66a4a26a0404c32085ec838b7c482495a7827919a5aa674de617c2757b'),
        'Qwen3-ASR-1.7B-q5_k.zip': (1951570656, 'f40040fe62a5ef0c09f8699fdbcb30f18bb8ae2bcd515ed4954e1f62b8b0e88f'),
        'Qwen3-ForcedAligner-0.6B.zip': (626205170, '8fa035db150e1621a01cbb313bf152b593818b0f15dd94180452f22fc77fbf43'),
        'Sensevoice-Small-ONNX.zip': (433798984, '3948b5761f12db1c01d7a7e596294b43b0316aa5c7a8df77981e78573997dcbb'),
    }

    def test_cap_is_decimal_5gb(self):
        self.assertEqual(CAP_BYTES, 5_000_000_000)
        self.assertEqual(MODEL_CATALOG['cap_bytes'], CAP_BYTES)

    def test_assets_frozen(self):
        seen = {}
        for key, spec in MODEL_CATALOG['sets'].items():
            for c in spec['components']:
                seen[c['asset']] = (c['size'], c['sha256'])
                self.assertTrue(c['url'].startswith(
                    'https://github.com/HaujetZhao/CapsWriter-Offline/releases/download/models/'))
        self.assertEqual(seen, self.EXPECTED)

    def test_host_allowlist(self):
        policy = MODEL_CATALOG['policy']
        self.assertTrue(host_allowed('github.com', policy))
        self.assertTrue(host_allowed('objects.githubusercontent.com', policy))
        self.assertTrue(host_allowed('release-assets.githubusercontent.com', policy))
        for bad in ('localhost', '127.0.0.1', '192.168.0.104', '169.254.169.254',
                    'evil.com', 'github.com.evil.com', '', '0.0.0.0'):
            self.assertFalse(host_allowed(bad, policy), bad)

    def test_sets_cover_check_model_semantics(self):
        # 主组件必需文件与 core/server/worker/check_model.py 的清单同源
        def req_files(key):
            spec = MODEL_CATALOG['sets'][key]
            main = next(c for c in spec['components'] if c['kind'] == 'main')
            return sorted(main['required'])
        # required 一律相对叶子目录（与 check_model 的文件名语义一致）
        self.assertEqual(req_files('fun_asr_nano'), sorted([
            'Fun-ASR-Nano-Encoder-Adaptor.fp16.onnx',
            'Fun-ASR-Nano-CTC.fp16.onnx',
            'Fun-ASR-Nano-Decoder.q5_k.gguf',
            'tokens.txt']))
        self.assertEqual(req_files('sensevoice'), sorted([
            'SenseVoice-Encoder.fp16.onnx',
            'SenseVoice-CTC.fp16.onnx',
            'tokenizer.bpe.model']))
        self.assertEqual(req_files('paraformer'), sorted(['model.onnx', 'tokens.txt']))
        # qwen 两变体共享同一主目录（互斥安装），必需文件一致
        q4 = MODEL_CATALOG['sets']['qwen_asr_q4_k']
        q5 = MODEL_CATALOG['sets']['qwen_asr_q5_k']
        self.assertEqual(q4['model_type'], 'qwen_asr')
        self.assertEqual(q5['model_type'], 'qwen_asr')
        leaf4 = q4['components'][0]['extract_into']
        leaf5 = q5['components'][0]['extract_into'] + '/' + q5['components'][0]['strip_prefix']
        self.assertEqual(leaf4, leaf5)
        self.assertEqual(sorted(q4['components'][0]['required']),
                         sorted(q5['components'][0]['required']))

    def test_qwen_variant_marker_sizes(self):
        from admin.model_manager import _VARIANT_MARKER_QWEN
        self.assertEqual(_VARIANT_MARKER_QWEN['sizes'],
                         {'q4_k': 1282434624, 'q5_k': 1471800896})


if __name__ == '__main__':
    unittest.main()
