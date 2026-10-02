# -*- coding: utf-8 -*-
"""安装后运行时探针：无麦克风、无 UI，验证已安装客户端可导入并真实连网。

模式：
  默认        导入已安装依赖 + 校验交付默认地址 + 真实 WebSocket 握手与 ping
              （需要能连到 --server；用于现场验收）
  --no-network 只做导入与配置校验（GitHub Actions runner 连不到局域网服务器）

用法：
  python scripts/client_runtime_probe.py <installed_dir> <out.json> \
      [--expected-addr 192.168.0.104] [--expected-port 6016] [--no-network]
"""
import asyncio
import hashlib
import json
import os
import pathlib
import sys
import types


def main() -> None:
    sys.dont_write_bytecode = True  # 探针不得向被测安装目录写 __pycache__
    args = sys.argv[1:]
    no_network = "--no-network" in args
    args = [a for a in args if not a.startswith("--")]

    def option(name, default):
        return args[args.index(name) + 1] if name in args else default

    if len(args) < 2:
        raise SystemExit(__doc__)
    installed = pathlib.Path(args[0]).resolve()
    out_path = pathlib.Path(args[1]).resolve()
    expected_addr = option("--expected-addr", "192.168.0.104")
    expected_port = option("--expected-port", "6016")

    internal = installed / "internal"
    if not installed.is_dir() or not internal.is_dir():
        raise SystemExit(f"安装目录不完整: {installed}")
    sys.path[:0] = [str(installed), str(internal), str(internal / "win32"), str(internal / "win32/lib")]
    sys.frozen = True
    sys._MEIPASS = str(internal)
    for dll_dir in {p.parent for p in internal.rglob("*.dll")}:
        os.add_dll_directory(str(dll_dir))

    from config_client import ClientConfig
    from core.client.app import CapsWriterClient
    from core.client.connection import credentials as credentials_module
    from core.client.connection.websocket_manager import WebSocketManager
    from core.client.state import ClientState
    import numpy, pyclip, sounddevice, websockets  # noqa: F401

    modules = [
        sys.modules[n]
        for n in (
            "core.client.app",
            "core.client.connection.websocket_manager",
            "core.client.connection.credentials",
            "sounddevice",
            "numpy",
            "websockets",
            "pyclip",
        )
    ]
    for module in modules:
        path = pathlib.Path(module.__file__).resolve()
        if not path.is_relative_to(installed):
            raise SystemExit(f"{module.__name__} 并非来自安装目录: {path}")

    checks = {
        "installed_dependency_imports": {m.__name__: str(pathlib.Path(m.__file__).relative_to(installed)) for m in modules},
        "default_addr": str(ClientConfig.addr),
        "default_port": str(ClientConfig.port),
        "no_audio_or_ui_started": True,
        "websocket_source_sha256": hashlib.sha256(
            (installed / "core/client/connection/websocket_manager.py").read_bytes()
        ).hexdigest(),
    }
    if ClientConfig.addr != expected_addr or str(ClientConfig.port) != expected_port:
        raise SystemExit(
            f"交付默认地址不符: {ClientConfig.addr}:{ClientConfig.port}（期望 {expected_addr}:{expected_port}）"
        )
    if ClientConfig.server_url != "":
        raise SystemExit("交付默认 server_url 应为空（由用户在界面里开启远程模式）")
    checks["default_delivery_config_verified"] = True

    if not no_network:
        async def probe() -> None:
            state = ClientState()
            manager = WebSocketManager(types.SimpleNamespace(state=state))
            if not await asyncio.wait_for(manager.connect(), 20):
                raise SystemExit("握手失败")
            pong = await state.websocket.ping(b"caps-installer-check")
            await asyncio.wait_for(pong, 10)
            await state.websocket.close()

        asyncio.run(probe())
        checks["actual_installed_client_handshake_and_ping"] = True

    checks["passed"] = True
    out_path.write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in checks.items() if k != "installed_dependency_imports"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
