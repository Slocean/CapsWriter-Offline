import subprocess
import sys
import tempfile
import asyncio
import contextlib
import io
import json
import os
from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import patch
import websockets

from core.server.connection.port_conflict import reserve_listener, listeners, stop_listeners
from core.tools.gateway_endpoint import endpoints, publish
from admin.asr_control import ASRControl
from admin.config_admin import AdminConfig
from config_server import ServerConfig
from core.server.app import CapsWriterServer


class PortConflictTests(unittest.TestCase):
    def test_server_start_conflict_choices(self):
        for mode in ('auto', 'stop'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                child = subprocess.Popen([sys.executable, '-u', '-c',
                    'import socket,time; s=socket.socket(); s.bind(("127.0.0.1",0)); '
                    's.listen(); print(s.getsockname()[1],flush=True); time.sleep(30)'],
                    stdout=subprocess.PIPE, text=True)
                app = None
                try:
                    port = int(child.stdout.readline())
                    owners = listeners(port, '0.0.0.0')
                    expected = {str(pid): list(value) for pid, value in owners.items()}
                    app = CapsWriterServer.__new__(CapsWriterServer)
                    app.is_alive = False
                    app._instance_mutex = None
                    app.listener = None
                    app.base_dir = Path(tmp)
                    app.tray_manager = SimpleNamespace(start=unittest.mock.Mock())
                    app.process_manager = SimpleNamespace(start=unittest.mock.Mock())
                    app.socket_manager = SimpleNamespace(start=unittest.mock.AsyncMock())
                    app.loop = SimpleNamespace(run_until_complete=asyncio.run)
                    app._print_banner = unittest.mock.Mock()
                    mutex = unittest.mock.MagicMock()
                    mutex.acquired = True
                    with patch.object(ServerConfig, 'addr', '0.0.0.0'), \
                         patch.object(ServerConfig, 'port', str(port)), \
                         patch('core.server.app.InstanceMutex', return_value=mutex), \
                         patch('core.server.app.write_pid_file'), \
                         patch('core.server.app.register_signal'), \
                         patch.dict(os.environ, {'CAPSWRITER_PORT_CONFLICT': mode,
                                                 'CAPSWRITER_PORT_OWNERS': json.dumps(expected)}), \
                         contextlib.redirect_stdout(io.StringIO()):
                        app.start()
                        self.assertIsNotNone(app.listener)
                        if mode == 'auto':
                            self.assertNotEqual(app.listener.getsockname()[1], port)
                            self.assertIsNone(child.poll())
                        else:
                            self.assertEqual(app.listener.getsockname()[1], port)
                            child.wait(timeout=10)
                        app.process_manager.start.assert_called_once()
                finally:
                    if app is not None and app.listener is not None:
                        app.listener.close()
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=10)
                    child.stdout.close()

    def test_endpoint_lists_actual_lan_addresses(self):
        records = [(2, 1, 6, '', (ip, 0)) for ip in
                   ('127.0.0.1', '169.254.1.1', '192.168.0.101')]
        with patch('core.tools.gateway_endpoint.socket.getaddrinfo', return_value=records):
            self.assertEqual(endpoints('0.0.0.0', 6123), '192.168.0.101:6123\n')

    def test_publisher_writes_endpoint_file(self):
        async def run(path):
            with patch('core.tools.gateway_endpoint.endpoints', return_value='192.168.0.101:6123\n'):
                task = asyncio.create_task(publish(path, '0.0.0.0', 6123, unittest.mock.Mock()))
                await asyncio.sleep(0.05)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(path.read_text(), '192.168.0.101:6123\n')
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run(Path(tmp) / 'routes.txt'))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows firewall script')
    def test_firewall_block_excludes_only_the_current_addresses(self):
        script = Path(__file__).resolve().parents[1] / 'deploy' / 'sync_gateway_firewall.ps1'
        command = (
            f". '{script}' -AppDir 'C:\\CapsWriterTest' -FunctionsOnly; "
            "$r=@(BlockComplement @('172.21.128.1','192.168.0.101')); "
            "if (($r -join ',') -ne "
            "'0.0.0.0-172.21.128.0,172.21.128.2-192.168.0.100,192.168.0.102-255.255.255.255') { exit 1 }"
        )
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_automatic_port_is_reserved(self):
        with reserve_listener('127.0.0.1', 0) as first:
            port = first.getsockname()[1]
            self.assertGreater(port, 0)
            with self.assertRaises(OSError):
                reserve_listener('127.0.0.1', port)

    def test_wildcard_detects_interface_listener(self):
        with reserve_listener('127.0.0.1', 0) as first:
            with self.assertRaises(OSError) as raised:
                reserve_listener('0.0.0.0', first.getsockname()[1])
            self.assertIn(raised.exception.errno, (98, 10048))

    def test_reserved_socket_serves_websocket(self):
        async def run():
            sock = reserve_listener('127.0.0.1', 0)
            port = sock.getsockname()[1]
            async def handler(ws):
                await ws.send('ok')
            async with websockets.serve(handler, sock=sock):
                async with websockets.connect(f'ws://127.0.0.1:{port}', proxy=None) as ws:
                    self.assertEqual(await ws.recv(), 'ok')
        asyncio.run(run())

    def test_changed_owner_is_not_terminated(self):
        with patch('core.server.connection.port_conflict.listeners', return_value={42: (2, 'new.exe')}), \
             patch('core.server.connection.port_conflict.psutil.Process') as process:
            with self.assertRaisesRegex(RuntimeError, '已变化'):
                stop_listeners(6016, '0.0.0.0', {42: (1, 'old.exe')})
            process.assert_not_called()

    def test_stop_owned_test_listener_and_reuse_port(self):
        child = subprocess.Popen([sys.executable, '-u', '-c',
            'import socket,time; s=socket.socket(); s.bind(("127.0.0.1",0)); '
            's.listen(); print(s.getsockname()[1],flush=True); time.sleep(30)'],
            stdout=subprocess.PIPE, text=True)
        try:
            port = int(child.stdout.readline())
            owners = listeners(port, '127.0.0.1')
            self.assertEqual(set(owners), {child.pid})
            stop_listeners(port, '127.0.0.1', owners)
            child.wait(timeout=10)
            with reserve_listener('127.0.0.1', port):
                pass
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            child.stdout.close()

    def test_admin_rejects_changed_occupant(self):
        child = subprocess.Popen([sys.executable, '-u', '-c',
            'import socket,time; s=socket.socket(); s.bind(("127.0.0.1",0)); '
            's.listen(); print(s.getsockname()[1],flush=True); time.sleep(30)'],
            stdout=subprocess.PIPE, text=True)
        try:
            port = int(child.stdout.readline())
            with tempfile.TemporaryDirectory() as tmp, \
                 patch.object(AdminConfig, 'asr_host', '127.0.0.1'), \
                 patch.object(AdminConfig, 'asr_port', port), \
                 patch.object(AdminConfig, 'asr_pid_file', Path(tmp) / 'missing.pid'), \
                 patch.object(AdminConfig, 'asr_work_dir', Path(tmp)), \
                 patch.object(AdminConfig, 'asr_launch_cmd', ['fake.exe']), \
                 patch('admin.asr_control.winproc.asr_mutex_exists', return_value=False), \
                 patch('admin.asr_control.gpu_snapshot', return_value=None):
                ctl = ASRControl()
                ctl.control.status_sync = lambda *args, **kwargs: None
                status = ctl.status()
                self.assertTrue(status['can_auto_resolve'])
                self.assertIn('自动换端口', status['hint'])
                self.assertIn(str(child.pid), status['port_owners'])
                result = ctl.submit('start', port_conflict='stop', port_owners={'1': [0, 'other']})
                self.assertEqual(result['error'], 'not_allowed')
                self.assertIsNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            child.stdout.close()


if __name__ == '__main__':
    unittest.main()
