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
import psutil
from typing import Any, Dict, Optional
from pathlib import Path

from .config_admin import AdminConfig as Cfg
from .control_client import ControlClient
from .ops_gate import OPS_GATE
from . import winproc
from core.tools.win_instance import _norm_path  # noqa: F401 复用 pid_matches 的路径归一化语义
from core.server.connection.port_conflict import reserve_listener, listeners
from config_server import ServerConfig as ServerCfg


def _tcp_open(host: str, port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _port_busy(port: int) -> bool:
    try:
        with reserve_listener(ServerCfg.addr, port):
            return False
    except OSError as exc:
        return exc.errno in (98, 10048)


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
            if winproc.pid_matches(int(pid_entry['pid']), float(pid_entry.get('created_at', 0)),
                                   expected_exe=str(pid_entry.get('exe') or '') or None):
                verified_pid = int(pid_entry['pid'])

        port_busy = _tcp_open(Cfg.asr_host, Cfg.asr_port) or _port_busy(Cfg.asr_port)
        if online:
            state = 'online'
        elif verified_pid is not None or (proc is not None and proc.poll() is None):
            state = 'starting' if (action and action.get('type') == 'start' and action.get('state') == 'running') else 'stale'
        elif port_busy:
            state = 'stale'   # 端口有人占用/残留，但身份与控制通道均无法核对
        else:
            state = 'stopped'

        mem = None
        if verified_pid:
            try:
                mem = winproc.tree_working_set(verified_pid)
            except Exception:
                mem = None

        # R03：启动与停止/重启是不同的能力，不能都用 controllable 表达。
        # 启动只要求「确认没有实例 + 启动命令/工作目录有效」；停止/重启需要
        # 控制通道（或本进程自己拉起的子进程）才不会对不可核身的目标误报。
        mutex_held = winproc.asr_mutex_exists()
        launch_cmd = self._launch_command()
        launch_ok = Path(Cfg.asr_work_dir).exists() and (
            bool(Cfg.asr_launch_cmd) or Path(Cfg.asr_entry).exists())
        own_proc_alive = proc is not None and proc.poll() is None
        can_start = (state == 'stopped' and not mutex_held and launch_ok)
        can_stop = online or own_proc_alive
        can_resolve_port = (state == 'stale' and not online and verified_pid is None
                            and not own_proc_alive and not mutex_held and launch_ok
                            and port_busy)
        port_owners = {}
        can_auto_resolve = False
        if can_resolve_port:
            try:
                port_owners = {str(pid): [created, exe] for pid, (created, exe)
                               in listeners(Cfg.asr_port, ServerCfg.addr).items()}
                can_auto_resolve = bool(port_owners)
                for pid, (created, _) in port_owners.items():
                    proc = psutil.Process(int(pid))
                    if proc.create_time() != created or any(
                            'start_server' in part.lower() for part in proc.cmdline()):
                        can_auto_resolve = False
                        break
            except Exception:
                can_auto_resolve = False

        result = {
            'state': state,                      # online | starting | stale | stopped
            'busy': bool(detail.get('busy')) if online else False,
            'detail': detail,                    # 控制通道提供的信息（在线才有）
            'pid': verified_pid,
            'memory_bytes': mem,
            'gpu': gpu_snapshot(),
            'control_ok': online,
            'controllable': online,              # 兼容字段：旧版 ASR 无控制通道
            'can_start': can_start,              # R03：启动按钮用这个
            'can_stop': can_stop,                # R03：停止/重启按钮用这个
            'can_restart': can_stop,
            'can_resolve_port': can_resolve_port,
            'can_auto_resolve': can_auto_resolve,
            'port_owners': port_owners,
            'mutex_held': mutex_held,
            'launch_ok': launch_ok,
            'pid_registered': verified_pid is not None,
            'port_open': port_busy,
            'launch_cmd': launch_cmd,
            'action': action,
        }
        if state == 'stopped' and not can_start:
            if not launch_ok:
                result['hint'] = ('启动命令或工作目录无效（请检查 admin_settings.json 的 '
                                  'asr_work_dir / asr_launch_cmd），启动按钮不可用。')
            elif mutex_held:
                result['hint'] = '检测到识别进程互斥锁仍被占用（可能有实例未完全退出），启动按钮不可用。'
        if state == 'stale' and not online and verified_pid is None:
            if can_resolve_port:
                if not port_owners:
                    result['hint'] = '识别端口已被占用，但无法确认占用进程；请检查权限。'
                elif not can_auto_resolve:
                    result['hint'] = '识别端口已被占用；请核对并关闭列出的占用进程。'
                else:
                    result['hint'] = '识别端口已被占用，请选择自动换端口或关闭列出的占用进程。'
            else:
                result['hint'] = ('检测到端口有服务但无法核身（无控制通道/PID 登记，可能是旧版 ASR 或其他程序）；'
                                  '为避免拉起第二个模型或误停他人进程，启动/停止按钮不可用。'
                                  '请升级 ASR 到带控制通道的新版后重试。')
        with self._lock:
            self._last_status = result
        return result

    # ---------- 动作 ----------

    def current_action(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._action) if self._action else None

    def submit(self, kind: str, force: bool = False, port_conflict: str = '', port_owners=None) -> Dict[str, Any]:
        """
        提交 start/stop/restart 动作；已有动作进行中返回冲突。
        服务端同样校验能力（R03：前端禁用只是第一道门）。
        动作全程持有共享操作闸门，与模型下载/删除互斥。

        闸门释放唯一性：acquire 成功后，本方法内任何提前 return、status()
        异常、线程启动失败都由 finally 释放；只有线程成功启动这一条路会把
        持有权转交给动作线程（其 finally 释放），本方法 finally 不再重复释放。
        """
        assert kind in ('start', 'stop', 'restart')
        owner = f'asr-action-{kind}'
        with self._lock:
            if self._action and self._action.get('state') in ('pending', 'running'):
                return {'ok': False, 'error': 'conflict', 'action': dict(self._action)}
        if not OPS_GATE.acquire(owner):
            return {'ok': False, 'error': 'conflict',
                    'detail': f'其他操作正在进行中（{OPS_GATE.owner}），稍后再试'}
        action = None
        owned_here = True   # 闸门当前由本调用持有；线程启动成功后转交
        try:
            st = self.status()
            if port_conflict not in ('', 'auto', 'stop') or (port_conflict and kind != 'start'):
                return {'ok': False, 'error': 'not_allowed', 'detail': '无效的端口冲突处理方式'}
            if port_conflict == 'stop' and (not st['port_owners'] or port_owners != st['port_owners']):
                return {'ok': False, 'error': 'not_allowed', 'detail': '端口占用者已变化或无法核实，请刷新后重试'}
            if port_conflict == 'auto' and not st['can_auto_resolve']:
                return {'ok': False, 'error': 'not_allowed', 'detail': '无法确认占用端口的进程不是识别服务'}
            if kind == 'start' and not st['can_start'] and not (port_conflict and st['can_resolve_port']):
                reason = st.get('hint') or '识别服务当前状态不允许启动（实例未完全退出或启动配置无效）'
                return {'ok': False, 'error': 'not_allowed', 'detail': reason}
            if kind in ('stop', 'restart') and not st['can_stop']:
                return {'ok': False, 'error': 'not_allowed',
                        'detail': '识别服务不可控（无控制通道且非本进程拉起），拒绝停止/重启'}
            with self._lock:
                if self._action and self._action.get('state') in ('pending', 'running'):
                    return {'ok': False, 'error': 'conflict', 'action': dict(self._action)}
                action = {
                    'id': uuid.uuid4().hex[:8],
                    'type': kind,
                    'state': 'pending',       # pending → running → done / failed
                    'force': bool(force),
                    'port_conflict': port_conflict,
                    'port_owners': st['port_owners'] if port_conflict else None,
                    'progress': '',
                    'message': '',
                    'submitted_at': time.time(),
                }
                self._action = action

            t = threading.Thread(target=self._run_action, args=(action, owner), daemon=True,
                                 name=f'asr-action-{kind}')
            try:
                t.start()
            except Exception:
                with self._lock:
                    if self._action is action:
                        self._action = None
                raise
            owned_here = False   # 持有权已转交动作线程
            return {'ok': True, 'action': dict(action)}
        finally:
            if owned_here:
                OPS_GATE.release(owner)

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

    # ---------- 停止终态核验（A01：唯一出口） ----------

    def _verified_pid_entry(self) -> Optional[Dict[str, Any]]:
        """读取并核身 PID 文件：返回 {pid, created_at, exe} 或 None（A01）"""
        try:
            entry = json.loads(Cfg.asr_pid_file.read_text(encoding='utf-8'))
        except Exception:
            return None
        pid = int(entry.get('pid', 0))
        created = entry.get('created_at')
        exe = str(entry.get('exe') or '') or None
        if (pid > 0 and created is not None
                and winproc.pid_alive(pid)
                and winproc.pid_matches(pid, float(created), expected_exe=exe)):
            return {'pid': pid, 'created_at': float(created), 'exe': exe}
        return None

    def _identity_alive(self, ident: Optional[Dict[str, Any]], tolerance: float = 2.0) -> bool:
        """
        记录身份是否仍存活（A01）：PID 活着，且创建时间（与 exe，若记录）
        和记录值一致。PID 被复用成无关进程时返回 False——调用方绝不据此
        向新身份发终止；创建时间缺失（采集失败）同样按身份不明处理。
        """
        if not ident:
            return False
        pid = ident.get('pid')
        created = ident.get('created_at')
        if not pid or created is None or not _pid_alive(pid):
            return False
        return winproc.pid_matches(int(pid), float(created), tolerance=tolerance,
                                   expected_exe=ident.get('exe'))

    @staticmethod
    def _child_idents(tree: Optional[Dict[str, Any]]) -> list:
        """累计清单中的子进程记录身份 [{pid, created_at}, ...]（A01）"""
        return [{'pid': pid, 'created_at': created}
                for pid, created in (tree or {}).get('children') or []]

    def _control_status_pid(self) -> Optional[int]:
        """经认证控制通道 status 报告的识别服务 PID（A01-control，无则 None）"""
        try:
            reply = self.control.status_sync(timeout=2.0)
        except Exception:
            return None
        detail = reply.get('status') if reply and reply.get('ok') else None
        if not detail:
            return None
        try:
            pid = int(detail.get('pid') or 0)
        except (TypeError, ValueError):
            return None
        return pid if pid > 0 else None

    def _recorded_tree(self) -> Optional[Dict[str, Any]]:
        """
        记录可核身的进程树身份（A01）：

        - 根：PID 文件核身的 {pid, created_at, exe}，或本进程拉起的 _proc
          （同样记录创建时间与镜像路径）——任何终止前都有完整身份可复核，
          父退出后的 PID 复用不会杀到无关进程；
        - 无 PID 文件且非本进程拉起时，经认证控制通道 status.pid 仍可作为
          根身份来源（A01-control）：按 OS 创建时间/exe 核身后记录；读取
          失败记 created_at=None 不确定根（_stop_verified 会阻断，未知树
          绝不当空树）；
        - 后代：快照枚举的 (pid, created_at) 清单；**枚举失败记为未核实，
          绝不等价于空树**（children_enumerated=False）；创建时间读取失败的
          后代记为 (pid, None) 不确定身份——不是已退出，绝不据此终止，
          存活时由 _stop_verified 阻断。
        """
        entry = self._verified_pid_entry()
        own = self._proc is not None and self._proc.poll() is None
        if entry is not None:
            root = dict(entry)
        elif own:
            root = {'pid': self._proc.pid,
                    'created_at': winproc.process_creation_time(self._proc.pid),
                    'exe': winproc.process_image_path(self._proc.pid),
                    'owned': True}
        else:
            ctrl_pid = self._control_status_pid()
            if ctrl_pid is None:
                return None
            created = winproc.process_creation_time(ctrl_pid)
            root = {'pid': ctrl_pid,
                    'created_at': created,
                    'exe': winproc.process_image_path(ctrl_pid),
                    'owned': False, 'via': 'control'}
        children = winproc.child_processes(root['pid'])
        return {'root': root,
                'children': list(children or []),
                'children_enumerated': children is not None}

    @staticmethod
    def _identity_matches_actual(pid: int, actual_ct: float, recorded_ct: float,
                                 expected_exe: Optional[str], tolerance: float = 2.0) -> bool:
        """
        单次读取的实际创建时间与记录比较（A01-read-gap3）。exe 读取失败不
        参与判定（与 pid_matches 语义一致）；差异超容差 = PID 已被复用。
        实际值只读一次、本地比较——不在 pid_matches 内部做第二次 OS 读取，
        避免两次读取中途失败被 bool False 误判为「复用」而放行 done。
        """
        if abs(actual_ct - recorded_ct) > tolerance:
            return False
        if expected_exe:
            image = winproc.process_image_path(pid)
            if image and _norm_path(image) != _norm_path(expected_exe):
                return False
        return True

    def _leftovers(self, tree: Optional[Dict[str, Any]]) -> list:
        """
        返回仍未退出的、身份核对过的进程：[('root'|'child'|…, pid), ...]。

        - 根：三态判定（A01-read）——证实退出不计入；无法核实或不确定根
          （created_at 缺失/读取失败）记 root-unverified 阻断，绝不终止；
          确认存活后单次读取实际创建时间并与记录比较，PID 被复用成无关
          进程时不匹配、不计入、也绝不会误杀；
        - 子：按 PID 归并评估（同一 PID 可能有多条已捕获身份 (pid, ctime)）——
          证实退出不计入；无法核实记 child-unverified 阻断；单次读取实际
          创建时间，与任一已捕获身份匹配才计为可终止遗留；全部不匹配 =
          PID 已被复用（记录身份均已消失），不计入、不阻断；仅有不确定
          记录 (pid, None) 的 PID 由 _stop_verified 单独阻断，绝不终止。
        """
        alive = []
        if not tree:
            return alive
        root = tree.get('root') or {}
        rpid = root.get('pid')
        if rpid:
            state = _pid_state(rpid)
            if state == 'unknown':
                alive.append(('root-unverified', rpid))
            elif state == 'alive':
                created = root.get('created_at')
                actual = winproc.process_creation_time(rpid) if created is not None else None
                if created is None or actual is None:
                    alive.append(('root-unverified', rpid))   # 不确定根/读取失败 → 不可核实
                elif self._identity_matches_actual(rpid, actual, created,
                                                   root.get('exe')):
                    alive.append(('root', rpid))
                # PID 复用 → 记录根身份已消失，不计入、不阻断
        identities: Dict[int, list] = {}
        for child_pid, created in tree.get('children') or []:
            identities.setdefault(child_pid, []).append(created)
        for child_pid, recorded in identities.items():
            state = _pid_state(child_pid)
            if state == 'exited':
                continue   # 证实已退出（残留句柄不算活）：该 PID 全部已捕获身份都已消失
            known = [c for c in recorded if c is not None]
            if not known:
                continue   # 仅有不确定记录：阻断由 _stop_verified 负责，绝不终止未知 PID
            if state == 'unknown':
                alive.append(('child-unverified', child_pid))   # 无法核实：阻断、绝不终止
                continue
            actual = winproc.process_creation_time(child_pid)
            if actual is None:
                alive.append(('child-unverified', child_pid))   # 已知身份子读取失败 ≠ 死亡/复用
                continue
            if any(abs(actual - c) <= 0.5 for c in known):
                alive.append(('child', child_pid))   # 已捕获身份仍存活 → 可终止遗留
            # 全部不匹配 → PID 已被复用，记录身份均已消失 → 不计入、不阻断
        return alive

    def _stop_verified(self, tree: Optional[Dict[str, Any]]) -> tuple:
        """
        终态核验（A01）：未捕获任何身份证据（tree=None）即未知树，不得当
        空树放行；控制服务不再响应、后代清单已核实、仅存不确定记录的后代
        全部证实退出、核身的根进程已退出或身份可核实、ASR 端口已释放、
        命名互斥锁已释放。任一不满足返回 (False, 原因)。
        """
        if tree is None:
            # A01-control：未知树 ≠ 空树——没有身份证据就不能宣布停止完成
            return False, '未捕获可核身的进程树（未知树不得当空树），停止未完成'
        reply = self.control.status_sync(timeout=2.0)
        if reply is not None and reply.get('ok'):
            return False, '控制服务仍在响应'
        if not tree.get('children_enumerated', True):
            return False, '后代进程清单枚举失败，无法核实全部子进程已退出'
        children_pairs = tree.get('children') or []
        known_pids = {p for p, c in children_pairs if c is not None}
        only_unknown = sorted({p for p, c in children_pairs if c is None} - known_pids)
        for child_pid in only_unknown:
            # A01-read：仅有不确定记录 (pid, None) 的后代——仅证实退出
            # （pid_state=exited）才放行；无法核实（权限/读取失败）与确认
            # 存活都必须阻断，绝不把读取失败当成已退出
            if _pid_state(child_pid) != 'exited':
                return False, f'子进程 {child_pid} 身份无法核实（创建时间读取失败），停止未完成'
        root = tree.get('root') or {}
        rpid = root.get('pid')
        if rpid:
            state = _pid_state(rpid)
            if state == 'unknown':
                # A01-read：未知根必须 fail-closed——只有证实退出才放行，
                # 既不终止它，也绝不能宣布停止完成
                return False, f'根进程 {rpid} 状态无法核实（读取失败），停止未完成'
            if state == 'alive':
                created = root.get('created_at')
                actual_ct = winproc.process_creation_time(rpid) if created is not None else None
                if created is None or actual_ct is None:
                    return False, '根进程身份无法核实（创建时间缺失或读取失败），停止未完成'
                if not self._identity_matches_actual(rpid, actual_ct, created,
                                                     root.get('exe')):
                    return False, '根进程身份无法核实（PID 可能被复用），停止未完成'
            # state == 'exited' → 根进程证实已退出，放行
        leftovers = self._leftovers(tree)
        if leftovers:
            return False, '进程仍存活: ' + '、'.join(f'{kind}#{pid}' for kind, pid in leftovers)
        if _tcp_open(Cfg.asr_host, tree.get('port', Cfg.asr_port)):
            return False, 'ASR 端口仍被占用'
        if winproc.asr_mutex_exists():
            return False, '互斥锁仍被占用'
        return True, ''

    @staticmethod
    def _merge_tree(old: Optional[Dict[str, Any]],
                    new: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        刷新树（A01）：根身份取最新枚举；后代清单取稳定并集——父进程运行
        期间生成、之后脱离父进程（或父已退出）的 worker 不得从记录中丢失。

        **保留全部已捕获的不同 (PID, ctime) 身份**（A01-gap4：同 PID 的多个
        记录身份缺一不可——旧记录已退出/复用而新记录仍存活时，只有并集里
        的新身份才能让终态核验看到存活的 worker）；排序按 (PID, ctime 是否
        可读, ctime) 稳定键，绝不对 None/float 直接比较（无 TypeError）。
        任一次枚举失败都会把清单标为未核实。
        """
        if new is None:
            return old
        merged = {tuple(c) for c in (old or {}).get('children') or []} \
            | {tuple(c) for c in new.get('children') or []}
        children = sorted(merged, key=lambda t: (t[0], t[1] is not None,
                                                 t[1] if t[1] is not None else 0.0))
        enumerated = bool((old or {}).get('children_enumerated', True)) \
            and bool(new.get('children_enumerated', True))
        return {'root': new['root'], 'children': children, 'children_enumerated': enumerated,
                'port': (old or {}).get('port', Cfg.asr_port)}

    def _stop_process(self, action: Dict[str, Any]) -> None:
        """
        停止识别进程（A01）：控制通道优雅退出 → 终止核对过的根进程 →
        强杀记录过身份的遗留子进程（模型 worker）。所有出口收敛到
        _stop_verified 终态核验——父进程退出但子进程仍监听、仅互斥锁遗留
        都不得报完成；核验不过必须抛错（写 failed）。

        全过程使用同一份累计身份清单（tree）：任何一次刷新都经 _merge_tree
        合并（绝不替换），新快照的空 children 不得丢掉已记录的 worker；
        每一次终止（优雅、升级强杀、逐个子进程清理）调用前都重新核对记录
        身份，PID 被复用成无关进程时绝不发终止。shutdown 前必须已捕获可
        核身的根/树（tree=None 直接拒绝，未知树不得当空树）。
        """
        self._wait_sessions(action)
        st = self.status()
        if not (st['control_ok'] or st['pid_registered']
                or (self._proc is not None and self._proc.poll() is None)):
            raise RuntimeError('识别服务不可核身（无控制通道、无 PID 登记、非本进程拉起），拒绝执行停止')
        tree = self._recorded_tree()
        if tree is None:
            # A01-control：必须 shutdown 前确认已捕获可核身的根/树——无 PID
            # 文件、非本进程拉起且控制通道未提供可核身 pid 时，拒绝执行停止
            # （未知树不得当空树；不发送退出请求、不进入 restart）
            raise RuntimeError('无法捕获识别服务进程树（无 PID 文件、非本进程拉起、'
                               '控制通道未提供可核身 pid），拒绝执行停止')

        detail = st.get('detail') or {}
        tree['port'] = int(detail.get('port') or Cfg.asr_port)

        self._update(action, progress='请求识别进程退出…')
        reply = self.control.request_sync('shutdown', timeout=5.0)
        if reply is None or not reply.get('ok'):
            self._update(action, progress='控制通道未确认退出请求，改用进程终止…')

        deadline = time.time() + Cfg.asr_stop_timeout
        while time.time() < deadline:
            ok, _ = self._stop_verified(tree)
            if ok:
                self._proc = None
                return
            fresh = self._recorded_tree()
            if fresh is not None:
                # 后代可能在运行期间增加：任何刷新都合并进累计清单（绝不
                # 替换）——已记录的后代（含之后脱离父进程的 worker）不得丢失
                tree = self._merge_tree(tree, fresh)
            time.sleep(0.5)

        # 优雅退出失败 → 只终止记录身份（PID+创建时间/exe）核对过的根进程
        root_ident = (tree or {}).get('root')
        if root_ident and self._identity_alive(root_ident):
            pid = root_ident['pid']
            self._update(action, progress=f'终止进程 {pid}…')
            _terminate_tree(pid, force=False)
            if not self._wait_identity_gone(root_ident, 10):
                # 升级强杀前必须重新核对记录身份：等待期间 PID 可能已被复用
                # 成无关进程（创建时间/exe 不再匹配），绝不能向新身份发强杀
                if self._identity_alive(root_ident):
                    self._update(action, progress=f'优雅终止无效，强制终止 {pid}…')
                    _terminate_tree(pid, force=True)
                    if not self._wait_identity_gone(root_ident, 10):
                        raise RuntimeError(f'进程 {pid} 无法终止（taskkill 失败或权限不足），停止未完成')
                # 记录身份已不匹配（PID 复用）：记录身份已退出，不向新身份发终止
        elif self._proc is None and tree is None:
            raise RuntimeError('优雅退出超时且无可核身的 PID，停止未完成（请人工核对进程）')

        # 清理前最后一次刷新：合并（绝不替换）进累计清单
        fresh = self._recorded_tree()
        if fresh is not None:
            tree = self._merge_tree(tree, fresh)

        # 记录过身份的遗留子进程（父已退出但 worker 仍监听）逐个强制清理；
        # 每次终止前按记录身份（PID+创建时间）重新核对，PID 被复用绝不发强杀
        for ident in self._child_idents(tree):
            if not self._identity_alive(ident, tolerance=0.5):
                continue
            self._update(action, progress=f"清理遗留子进程 {ident['pid']}…")
            _terminate_tree(ident['pid'], force=True)

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

        # 统一终态核验：始终消费同一份累计身份清单，任何遗留都不得报完成
        ok, why = self._stop_verified(tree)
        if not ok:
            raise RuntimeError(f'停止未完成：{why}')

    def _wait_identity_gone(self, ident: Optional[Dict[str, Any]], timeout: float) -> bool:
        """
        等待记录身份退出（A01）：判定对象是记录的（PID+创建时间/exe）身份，
        不是裸 PID——等待期间 PID 被复用成无关进程时视为记录身份已退出，
        绝不据此向新身份发终止。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._identity_alive(ident):
                return True
            time.sleep(0.3)
        return not self._identity_alive(ident)

    def _launch_command(self) -> list[str]:
        """
        ASR 启动命令（P0-07）：优先用配置的 asr_launch_cmd（打包版 EXE），
        否则 [python, start_server.py]（源码运行）。

        Windows 下相对 exe 路径由 CreateProcess 按「调用方进程 cwd」搜索，
        不能假设管理端 cwd=ASR 工作目录（管理端可从任意 cwd 启动）：
        相对路径统一解析到 asr_work_dir/repo 目录下的绝对路径（阶段八便携
        包）；目录带空格时 argv 列表由 subprocess 正确加引号。
        """
        if Cfg.asr_launch_cmd:
            cmd = [str(c) for c in Cfg.asr_launch_cmd]
            cmd[0] = self._resolve_launch_exe(cmd[0])
            return cmd
        return [sys.executable, str(Cfg.asr_entry)]

    @staticmethod
    def _resolve_launch_exe(first: str) -> str:
        """
        相对 exe 命令解析（阶段八）：依次在 asr_work_dir / repo 目录下解析为
        绝对路径；都找不到时保持原值（如 'python' 走 PATH 搜索，启动失败时
        有明确错误提示）。
        """
        try:
            p = Path(first)
        except Exception:
            return first
        if p.is_absolute():
            return first
        for base in (Cfg.asr_work_dir, Cfg.repo_dir):
            candidate = Path(base) / first
            if candidate.exists():
                return str(candidate)
        return first

    def _start_process(self, action: Dict[str, Any]) -> None:
        if self._already_running():
            st = self.status()
            mode = action.get('port_conflict')
            if (not mode or not st.get('can_resolve_port') or
                    action.get('port_owners') != st.get('port_owners') or
                    (mode == 'auto' and not st.get('can_auto_resolve'))):
                raise RuntimeError('识别服务或端口占用者状态已变化，为避免双实例未启动新进程')
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
            env={**os.environ, 'CAPSWRITER_PORT_CONFLICT': action.get('port_conflict', ''),
                 'CAPSWRITER_PORT_OWNERS': json.dumps(action.get('port_owners') or {})},
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
        entry = self._verified_pid_entry()
        return int(entry['pid']) if entry else None

    def apply_log_level(self, level: str) -> tuple:
        """
        R12：把日志级别即时应用到运行中的识别进程（控制通道 set_log_level）。

        返回 (applied, detail)：控制通道不可用或命令失败返回 False——
        调用方据此把 log_level 列入 restart_required，不得谎报已生效。
        """
        reply = self.control.request_sync('set_log_level', level=level, timeout=5.0)
        if reply and reply.get('ok'):
            return True, ''
        return False, '控制通道不可用或命令失败'

    def _run_action(self, action: Dict[str, Any], owner: str = '') -> None:
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
            if owner:
                OPS_GATE.release(owner)
            # 保留动作结果供 UI 轮询，60s 后清除
            def _clear():
                time.sleep(60)
                with self._lock:
                    if self._action is action:
                        self._action = None
            threading.Thread(target=_clear, daemon=True).start()


def _pid_state(pid: int) -> str:
    """三态进程状态（A01-read）：alive/exited/unknown；查询异常按 unknown（fail-closed）"""
    try:
        return winproc.pid_state(pid)
    except Exception:
        return 'unknown'


def _pid_alive(pid: int) -> bool:
    return _pid_state(pid) == 'alive'


def _terminate_tree(pid: int, force: bool) -> bool:
    """
    终止整棵进程树（含模型 worker 子进程）；返回是否成功提交（R07）。

    不传空参数（旧实现构造 ['taskkill','/T','','/PID',pid]，argv 含空串）；
    返回码交给调用方核实，失败会升级或判 failed，不会当作已停止。
    """
    if sys.platform == 'win32':
        argv = ['taskkill', '/PID', str(pid)]
        if force:
            argv.append('/F')
        argv.append('/T')
        try:
            out = subprocess.run(argv, capture_output=True, timeout=20,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            return False
        return out.returncode == 0
    import signal
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        return True
    except OSError:
        return False


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
