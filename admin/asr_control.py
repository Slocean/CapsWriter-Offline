# coding: utf-8
"""
ASR 识别进程生命周期控制（管理进程侧）

- start/stop/restart 作为后台动作执行，带动作 ID、进度与状态，供网页轮询；
- 停止/重启前等待活跃识别会话结束（可配置上限），强制中断需显式 force；
- 进程身份三重核对：互斥锁、PID 文件（含创建时间）、端口探测；
- 单实例保证：启动前检查互斥锁/PID/端口，绝不并发拉起第二个模型实例。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Dict, Optional
from pathlib import Path

from .config_admin import AdminConfig as Cfg
from .control_client import ControlClient
from . import winproc


def _tcp_open(host: str, port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class ASRControl:
    def __init__(self):
        self.control = ControlClient(Cfg.asr_control_url, Cfg.asr_control_token_path)
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None  # 管理进程自己拉起的 ASR
        self._action: Optional[Dict[str, Any]] = None
        self._last_status: Dict[str, Any] = {}

    # ---------- 状态 ----------

    def status(self) -> Dict[str, Any]:
        """聚合 ASR 状态；识别进程崩溃时管理进程本身仍可用"""
        with self._lock:
            action = dict(self._action) if self._action else None
            proc = self._proc

        reply = self.control.status_sync()
        detail = reply.get('status') if reply and reply.get('ok') else None
        online = detail is not None

        pid_entry = None
        try:
            pid_entry = json.loads(Cfg.asr_pid_file.read_text(encoding='utf-8'))
        except Exception:
            pid_entry = None

        verified_pid = None
        if pid_entry and winproc.pid_alive(int(pid_entry.get('pid', 0))):
            if winproc.pid_matches(int(pid_entry['pid']), float(pid_entry.get('created_at', 0))):
                verified_pid = int(pid_entry['pid'])

        if online:
            state = 'online'
        elif verified_pid is not None or (proc is not None and proc.poll() is None):
            state = 'starting' if (action and action.get('type') == 'start' and action.get('state') == 'running') else 'stale'
        elif _tcp_open(Cfg.asr_host, Cfg.asr_port):
            state = 'stale'   # 端口有人占用/残留，但身份与控制通道均无法核对
        else:
            state = 'stopped'

        mem = None
        if verified_pid:
            try:
                mem = winproc.tree_working_set(verified_pid)
            except Exception:
                mem = None

        result = {
            'state': state,                      # online | starting | stale | stopped
            'busy': bool(detail.get('busy')) if online else False,
            'detail': detail,                    # 控制通道提供的信息（在线才有）
            'pid': verified_pid,
            'memory_bytes': mem,
            'gpu': gpu_snapshot(),
            'control_ok': online,
            'controllable': online,              # 旧版 ASR 无控制通道 → 网页不能启动/停止它
            'pid_registered': verified_pid is not None,
            'port_open': _tcp_open(Cfg.asr_host, Cfg.asr_port),
            'launch_cmd': self._launch_command(),
            'action': action,
        }
        if state == 'stale' and not online and verified_pid is None:
            result['hint'] = ('检测到端口有服务但无法核身（无控制通道/PID 登记，可能是旧版 ASR 或其他程序）；'
                              '为避免拉起第二个模型，启动按钮不可用。请升级 ASR 到带控制通道的新版后重试。')
        with self._lock:
            self._last_status = result
        return result

    # ---------- 动作 ----------

    def current_action(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._action) if self._action else None

    def submit(self, kind: str, force: bool = False) -> Dict[str, Any]:
        """
        提交 start/stop/restart 动作；已有动作进行中返回冲突
        """
        assert kind in ('start', 'stop', 'restart')
        with self._lock:
            if self._action and self._action.get('state') in ('pending', 'running'):
                return {'ok': False, 'error': 'conflict', 'action': dict(self._action)}
            action = {
                'id': uuid.uuid4().hex[:8],
                'type': kind,
                'state': 'pending',       # pending → running → done / failed
                'force': bool(force),
                'progress': '',
                'message': '',
                'submitted_at': time.time(),
            }
            self._action = action

        t = threading.Thread(target=self._run_action, args=(action,), daemon=True,
                             name=f'asr-action-{kind}')
        t.start()
        return {'ok': True, 'action': dict(action)}

    def _update(self, action: Dict[str, Any], state: str = None, progress: str = None, message: str = None):
        with self._lock:
            if state is not None:
                action['state'] = state
            if progress is not None:
                action['progress'] = progress
            if message is not None:
                action['message'] = message

    # ---------- 动作实现 ----------

    def _already_running(self) -> bool:
        if winproc.asr_mutex_exists():
            return True
        st = self.status()
        return st['state'] in ('online', 'stale') or st['port_open']

    def _wait_sessions(self, action: Dict[str, Any]) -> None:
        """停止/重启前等待活跃会话清空；force 时跳过等待"""
        if action.get('force'):
            return
        deadline = time.time() + Cfg.action_session_wait
        while time.time() < deadline:
            reply = self.control.status_sync()
            detail = reply.get('status') if reply and reply.get('ok') else None
            if detail is None:
                return  # 控制通道不通，交由后续强制流程
            conns = int(detail.get('connections') or 0)
            if conns == 0:
                return
            self._update(action, progress=f'等待 {conns} 个连接空闲…')
            time.sleep(1.0)

    def _stop_process(self, action: Dict[str, Any]) -> None:
        """停止识别进程：控制通道优雅退出 → 终止核对过的 PID → 强杀进程树"""
        self._wait_sessions(action)
        self._update(action, progress='请求识别进程退出…')

        reply = self.control.request_sync('shutdown', timeout=5.0)

        deadline = time.time() + Cfg.asr_stop_timeout
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                self._proc = None
                return
            st = self.status()
            if st['state'] == 'stopped':
                return
            time.sleep(0.5)

        # 优雅退出失败 → 终止 PID 文件核对过的进程
        pid = self._verified_pid()
        if pid:
            self._update(action, progress=f'终止进程 {pid}…')
            _terminate_tree(pid, force=action.get('force', False))
            deadline = time.time() + 15
            while time.time() < deadline:
                if not _pid_alive(pid):
                    break
                time.sleep(0.3)

        if self._proc is not None:
            try:
                if self._proc.poll() is None:
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
            except Exception:
                pass
            self._proc = None

    def _launch_command(self) -> list[str]:
        """
        ASR 启动命令（P0-07）：优先用配置的 asr_launch_cmd（打包版 EXE），
        否则 [python, start_server.py]（源码运行）。
        """
        if Cfg.asr_launch_cmd:
            cmd = [str(c) for c in Cfg.asr_launch_cmd]
        else:
            cmd = [sys.executable, str(Cfg.asr_entry)]
        return cmd

    def _start_process(self, action: Dict[str, Any]) -> None:
        if self._already_running():
            raise RuntimeError('已有识别服务实例在运行（互斥锁/端口被占用），为避免双实例未启动新进程')
        self._update(action, progress='启动识别进程…')

        work_dir = Path(Cfg.asr_work_dir)
        cmd = self._launch_command()
        if not Cfg.asr_launch_cmd and not Path(Cfg.asr_entry).exists():
            raise RuntimeError(f'找不到启动脚本: {Cfg.asr_entry}（打包版请配置 asr_launch_cmd 指向 EXE）')
        if not work_dir.exists():
            raise RuntimeError(f'ASR 工作目录不存在: {work_dir}（请检查 admin_settings.json 的 asr_work_dir）')

        log_out = open(Cfg.data_dir / 'asr_start.log', 'ab')
        flags = 0
        if sys.platform == 'win32':
            flags = subprocess.CREATE_NO_WINDOW
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(work_dir),
            stdout=log_out,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )

        deadline = time.time() + Cfg.asr_start_timeout
        while time.time() < deadline:
            st = self.status()
            if st['state'] == 'online':
                detail = st.get('detail') or {}
                self._update(action, progress=f'就绪（模型 {detail.get("model_type", "?")}）')
                return
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError(f'识别进程启动后退出（exit={self._proc.poll()}），'
                                   f'详见 admin/data/asr_start.log 与 logs/server_latest.log')
            waited = int(Cfg.asr_start_timeout - (deadline - time.time()))
            self._update(action, progress=f'等待模型加载… {waited}s')
            time.sleep(2.0)
        raise RuntimeError('等待识别服务上线超时；服务可能仍在加载，请刷新状态查看')

    def _verified_pid(self) -> Optional[int]:
        try:
            entry = json.loads(Cfg.asr_pid_file.read_text(encoding='utf-8'))
        except Exception:
            return None
        pid = int(entry.get('pid', 0))
        if pid > 0 and winproc.pid_alive(pid) and winproc.pid_matches(pid, float(entry.get('created_at', 0))):
            return pid
        return None

    def _run_action(self, action: Dict[str, Any]) -> None:
        try:
            self._update(action, state='running')
            kind = action['type']
            if kind == 'start':
                self._start_process(action)
            elif kind == 'stop':
                self._stop_process(action)
            elif kind == 'restart':
                self._stop_process(action)
                time.sleep(1.0)
                self._start_process(action)
            self._update(action, state='done', progress='完成')
        except Exception as e:
            self._update(action, state='failed', message=str(e))
        finally:
            # 保留动作结果供 UI 轮询，60s 后清除
            def _clear():
                time.sleep(60)
                with self._lock:
                    if self._action is action:
                        self._action = None
            threading.Thread(target=_clear, daemon=True).start()


def _pid_alive(pid: int) -> bool:
    try:
        return winproc.pid_alive(pid)
    except Exception:
        return False


def _terminate_tree(pid: int, force: bool) -> None:
    if sys.platform == 'win32':
        # taskkill /T 终止整棵进程树（含模型 worker 子进程）
        flags = '/F' if force else ''
        subprocess.run(['taskkill', '/T', flags, '/PID', str(pid)],
                       capture_output=True, timeout=20)
    else:
        import signal
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)


# ---------- GPU 显存快照 ----------

_gpu_cache: Dict[str, Any] = {'at': 0.0, 'data': None}
_gpu_lock = threading.Lock()


def gpu_snapshot() -> Optional[Dict[str, Any]]:
    """nvidia-smi 显存占用；不可用返回 None（UI 明确显示不可用）"""
    with _gpu_lock:
        if time.time() - _gpu_cache['at'] < 10:
            return _gpu_cache['data']
    data = None
    try:
        out = subprocess.run(
            ['nvidia-smi', '--query-gpu=memory.used,memory.total',
             '--format=csv,noheader,nounits'],
            capture_output=True, timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
        )
        if out.returncode == 0:
            line = out.stdout.decode('utf-8', 'replace').strip().splitlines()[0]
            used, total = [float(x.strip()) for x in line.split(',')]
            data = {'used_mb': used, 'total_mb': total}
    except Exception:
        data = None
    with _gpu_lock:
        _gpu_cache['at'] = time.time()
        _gpu_cache['data'] = data
    return data
