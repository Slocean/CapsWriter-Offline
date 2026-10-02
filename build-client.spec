# -*- mode: python ; coding: utf-8 -*-
"""
客户端专用 PyInstaller 规格（GitHub Actions 与本地共用）。

只分析 start_client.py 入口；core/config_client/LLM 等私有模块一律以
源码形式由 installer/stage_payload.py 从仓库复制，本文件不做任何文件
复制、不建 junction、不引用 dist/build 里的已有产物——保证 Actions
干净环境可复现。
"""

from PyInstaller.utils.hooks import collect_all
import os

binaries = []
hiddenimports = []
datas = []

# sounddevice 的 PortAudio 二进制在 _sounddevice_data（含
# portaudio-binaries/README.md——旧版全局排除反例），必须整目录随包分发。
# 目录与 sounddevice 模块同层（0.5.x 为 site-packages 下的单模块发行）；
# 缺失直接失败，保证安装器/便携版永远不丢这个目录。
import sounddevice as _sounddevice  # noqa: F401
_probe = os.path.dirname(os.path.abspath(_sounddevice.__file__))
_sd_data = None
for _ in range(3):
    _candidate = os.path.join(_probe, '_sounddevice_data')
    if os.path.isdir(_candidate):
        _sd_data = _candidate
        break
    _probe = os.path.dirname(_probe)
if not _sd_data:
    raise SystemExit('未找到 _sounddevice_data（sounddevice 安装不完整）')
datas.append((_sd_data, '_sounddevice_data'))

# 收集 Pillow 相关文件（托盘图标）
try:
    pillow = collect_all('PIL')
    datas += pillow[0]
    binaries += pillow[1]
    hiddenimports += pillow[2]
except Exception:
    pass

hiddenimports += [
    'websockets',
    'websockets.client',
    'websockets.server',
    'rich',
    'rich.console',
    'rich.markdown',
    'rich._unicode_data.unicode17-0-0',
    'keyboard',
    'pyclip',
    'numpy',
    'sounddevice',
    'pypinyin',
    'watchdog',
    'typer',
    'srt',
    'PIL.Image',
    'pystray',
    'tkhtmlview',
    # LLM 润色链（core.client.app 顶层 import，缺一启动即崩）
    'openai',
    'ollama',
    'httpx',
    'pydantic',
    # 客户端源码直接 import 的其余第三方（旧 payload 均含，缺则运行时崩）
    'pynput',
    'rapidfuzz',
    'markdown',
    'requests',
    'win32gui',
    'win32process',
    'win32api',
    'win32con',
]

a_2 = Analysis(
    ['start_client.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['build_hook.py'],
    excludes=['IPython',
              'PySide6', 'PySide2', 'PyQt5',
              'matplotlib', 'wx',
              'funasr', 'torch',
              'sherpa_onnx',
              ],
    noarchive=True,
)

# 客户端过滤从系统 CUDA 目录收集的 DLL
filtered_binaries = []
for name, src, type in a_2.binaries:
    src_lower = src.lower() if isinstance(src, str) else ''
    is_system_cuda_dll = (
        '\\nvidia gpu computing toolkit\\cuda\\' in src_lower or
        '\\nvidia\\cudnn\\' in src_lower or
        ('\\cuda\\v' in src_lower and '\\bin\\' in src_lower)
    )
    is_unwanted_onnx_dll = (
        'onnxruntime_providers_cuda.dll' in name.lower() or
        'directml.dll' in name.lower()
    )
    if not is_system_cuda_dll and not is_unwanted_onnx_dll:
        filtered_binaries.append((name, src, type))
    else:
        reason = "环境 CUDA DLL" if is_system_cuda_dll else "冗余 ONNX DLL"
        print(f"[INFO] 排除 {reason}: {name} (从 {src} 收集)")
a_2.binaries = filtered_binaries

# 私有模块不进 PYZ（作为源码随包分发）
private_module = ['core', 'config_client', 'config_server', 'LLM', 'admin']

filtered = []
for name, src, type in a_2.pure:
    if not any(name == m or name.startswith(m + '.') for m in private_module):
        filtered.append((name, src, type))
a_2.pure = filtered

# noarchive 会把私有模块编译成 .pyc 放进 datas，同样排除
filtered = []
for name, src, type in a_2.datas:
    is_private = any(
        name.startswith(m + '/') or name.startswith(m + '\\') or name in (m + '.py', m + '.pyc')
        for m in private_module
    )
    if not is_private:
        filtered.append((name, src, type))
a_2.datas = filtered

pyz_2 = PYZ(a_2.pure)

exe_2 = EXE(
    pyz_2,
    a_2.scripts,
    [],
    exclude_binaries=True,
    name='start_client',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\icon.ico'],
    contents_directory='internal',
)

coll = COLLECT(
    exe_2,
    a_2.binaries,
    a_2.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='CapsWriter-Client-Raw',
)
