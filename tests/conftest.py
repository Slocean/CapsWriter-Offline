# coding: utf-8
"""
测试输入隔离防护（fail-closed，2026-10-04 事故回归）

事故：tests/test_app_hotkey_reload.py 的 _ManagerHarness 曾用真实 pynput
控制器向 OS 补发 CapsLock，触发 A:\\CapsWriter 正在运行的客户端开始录音。

本文件在 pytest 收集阶段安装以下拦截，默认无条件启用，无环境变量逃逸：
自动化测试流程不得绕过。需要真实设备/真实输入的集成验证必须由人类
在独立入口显式执行（不经过 pytest 收集本 conftest 的路径），并在
work/ 评审记录中注明原因。

拦截面（对应真实危险源码）：
- pynput keyboard/mouse Controller：实例化即抛错（真实 OS 输入注入；
  ShortcutEmulator 与 ShortcutManager.schedule_restore 的恢复控制器）
- pynput keyboard/mouse Listener：实例化即抛错（真实 OS 钩子注册）
- keyboard（PyPI）模块级 write/press_and_release/press/release/hold：
  调用即抛错（output/result_processor、llm 输出路径的真实键入）
- sounddevice Stream/InputStream/OutputStream/RawStream/RawInputStream/
  RawOutputStream：实例化即抛错（真实麦克风/扬声器设备访问）
- core.client.audio.output_mute.CoreAudioBackend：实例化即抛错
  （真实 COM 音频端点枚举/静音；测试一律用 FakeEndpointBackend）
- socket UDP 发往 127.0.0.1/::1:6018：抛错（用户客户端 UDP 控制端口）
- subprocess/os.startfile 启动 A:\\CapsWriter 安装目录内程序：抛错
  （已审查的 tmp 反射 csc 编译等隔离子进程不受影响）
"""

import os
import subprocess

# 用户客户端控制端口（core/client/udp/udp_control.py 监听端口）
_CLIENT_CONTROL_PORT = 6018
# 用户安装目录（禁止测试启动/触碰）
_INSTALL_ROOT = "a:\\capswriter"


def _blocked(kind):
    def _init(self, *args, **kwargs):
        raise RuntimeError(
            "测试默认禁止真实%s（fail-closed 输入隔离，无环境变量逃逸）。"
            "请注入假实现（FakeListener/_FakeEmulator/_FakeRestoreController/"
            "FakeEndpointBackend）；真实设备集成验证需人类在独立入口执行。"
            % kind
        )
    return _init


def _blocked_call(kind):
    def _fn(*args, **kwargs):
        raise RuntimeError(
            "测试默认禁止真实%s（fail-closed 输入隔离，无环境变量逃逸）。"
            "请在测试内 mock 替换后再调用。" % kind
        )
    return _fn


def install_guards():
    # --- pynput：控制器与监听器 ---
    try:
        from pynput import keyboard as _kb
        from pynput import mouse as _ms
        _kb.Controller.__init__ = _blocked("键盘控制器(pynput.keyboard.Controller)")
        _kb.Listener.__init__ = _blocked("键盘监听器(pynput.keyboard.Listener)")
        _ms.Controller.__init__ = _blocked("鼠标控制器(pynput.mouse.Controller)")
        _ms.Listener.__init__ = _blocked("鼠标监听器(pynput.mouse.Listener)")
    except Exception:
        pass
    # --- keyboard（PyPI）：模块级真实键入 ---
    try:
        import keyboard as _kbd
        for _name in ("write", "press_and_release", "press", "release", "hold"):
            if hasattr(_kbd, _name):
                setattr(_kbd, _name, _blocked_call("键入接口(keyboard.%s)" % _name))
    except Exception:
        pass
    # --- sounddevice：真实音频设备流 ---
    try:
        import sounddevice as _sd
        for _name in ("Stream", "InputStream", "OutputStream", "RawStream",
                      "RawInputStream", "RawOutputStream"):
            _cls = getattr(_sd, _name, None)
            if _cls is not None:
                _cls.__init__ = _blocked("音频设备流(%s)" % _name)
    except Exception:
        pass
    # --- CoreAudioBackend：真实 COM 音频端点 ---
    try:
        from core.client.audio import output_mute as _om
        _om.CoreAudioBackend.__init__ = _blocked(
            "音频后端(CoreAudioBackend，请用 FakeEndpointBackend)")
    except Exception:
        pass
    # --- 用户客户端 UDP 控制端口 ---
    try:
        import socket as _socket
        _real_sendto = _socket.socket.sendto

        def _guarded_sendto(self, data, address):
            try:
                host, port = address
            except Exception:
                return _real_sendto(self, data, address)
            if port == _CLIENT_CONTROL_PORT and (
                    str(host) in ("127.0.0.1", "::1", "localhost")):
                raise RuntimeError(
                    "测试禁止向用户客户端 UDP 控制端口 %d 发送命令（fail-closed）。"
                    % _CLIENT_CONTROL_PORT)
            return _real_sendto(self, data, address)

        _socket.socket.sendto = _guarded_sendto
    except Exception:
        pass
    # --- 启动 A:\CapsWriter 安装目录内程序 ---
    _real_popen_init = subprocess.Popen.__init__

    def _guarded_popen(self, args, *a, **k):
        joined = " ".join(args) if isinstance(args, (list, tuple)) else str(args)
        if _INSTALL_ROOT in joined.lower().replace("/", "\\"):
            raise RuntimeError(
                "测试禁止启动用户安装目录（A:\\CapsWriter）内的程序（fail-closed）。")
        return _real_popen_init(self, args, *a, **k)

    subprocess.Popen.__init__ = _guarded_popen

    def _guarded_startfile(path, *a, **k):
        if _INSTALL_ROOT in str(path).lower().replace("/", "\\"):
            raise RuntimeError("测试禁止触碰用户安装目录（A:\\CapsWriter）。")
        return _real_startfile(path, *a, **k)

    if hasattr(os, "startfile"):
        _real_startfile = os.startfile
        os.startfile = _guarded_startfile


def _redirect_client_logs():
    """测试期间的客户端日志写到本次会话独立的临时目录（Logger.setup 未
    显式给 log_dir 时），不触碰仓库 logs/ 与 A:\\CapsWriter 安装目录日志。
    透明转发全部位置参数（含 level），会话结束关闭句柄并清理。"""
    import tempfile
    import shutil
    import atexit
    import logging
    from core.logger import Logger as _Logger
    log_root = tempfile.mkdtemp(prefix="capswriter-test-logs-")
    orig_setup = _Logger.setup.__func__

    def _setup(cls, name, log_dir=None, *args, **kwargs):
        return orig_setup(cls, name, log_dir if log_dir is not None else log_root,
                          *args, **kwargs)

    _Logger.setup = classmethod(_setup)

    def _cleanup():
        root_norm = log_root.replace("\\", "/")
        for lg in list(logging.Logger.manager.loggerDict.values()):
            for h in list(getattr(lg, "handlers", [])):
                base = getattr(h, "baseFilename", "")
                if base and base.replace("\\", "/").startswith(root_norm):
                    try:
                        h.close()
                    except Exception:
                        pass
                    try:
                        lg.removeHandler(h)
                    except Exception:
                        pass
        shutil.rmtree(log_root, ignore_errors=True)

    atexit.register(_cleanup)


# 顺序：先重定向日志（避免 install_guards 导入 core.client 时首次打开仓库
# 日志），再安装输入/设备防护
_redirect_client_logs()
install_guards()
