# coding: utf-8
"""
管理进程入口

    python -m admin                 启动网页管理进程（默认 0.0.0.0:6017）
    python -m admin set-password    修改管理员密码（交互输入）
    python -m admin --host 127.0.0.1 --port 6017   指定监听地址

管理进程不加载识别模型，识别进程崩溃时本进程仍然存活并可在网页上恢复。
"""

from __future__ import annotations

import argparse
import getpass
import sys

from .config_admin import AdminConfig as Cfg, load_overrides


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='admin', description='CapsWriter 网页管理进程')
    parser.add_argument('--host', default=None, help='监听地址（默认见 config_admin）')
    parser.add_argument('--port', type=int, default=None, help='监听端口')
    parser.add_argument('--auth-mode', choices=['password', 'gateway'], default=None,
                        help="password=自带密码登录（默认）；gateway=信任部署面板登录网关"
                             "（须确认管理域名整站受网关保护，且防火墙只放行网关来源）")
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('set-password', help='修改管理员密码')
    args = parser.parse_args(argv)

    load_overrides()
    if args.host:
        Cfg.host = args.host
    if args.port:
        Cfg.port = args.port
    if args.auth_mode:
        Cfg.admin_auth_mode = args.auth_mode

    from .sessions import AdminAuth

    if args.command == 'set-password':
        auth = AdminAuth()  # 首次运行会生成并打印初始密码
        password = getpass.getpass('新管理员密码（至少 8 位）: ')
        confirm = getpass.getpass('再次输入: ')
        if password != confirm:
            print('两次输入不一致')
            return 1
        try:
            auth.set_password(password)
        except ValueError as e:
            print(f'失败: {e}')
            return 1
        print('密码已更新，所有会话已失效')
        return 0

    Cfg.data_dir.mkdir(parents=True, exist_ok=True)

    from .httpd import make_server
    from .webapp import AdminApp

    app = AdminApp()
    server = make_server(Cfg.host, Cfg.port, app)
    launch = app.asr._launch_command()
    print(f'CapsWriter 管理进程已启动: http://{Cfg.host}:{Cfg.port}/')
    print(f'  数据目录: {Cfg.data_dir}')
    print(f'  ASR 工作目录: {Cfg.asr_work_dir}')
    print(f'  ASR 启动命令: {" ".join(launch)}')
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print('\n管理进程退出')
    return 0


if __name__ == '__main__':
    sys.exit(main())
