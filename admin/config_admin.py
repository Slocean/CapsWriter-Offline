# coding: utf-8
"""
管理进程配置

默认值面向本仓库部署；可用 admin_settings.json 覆盖（与仓库同目录）。
"""

import json
import os
import sys
from pathlib import Path

REPO_DIR = Path(__file__).parents[1]


class AdminConfig:
    # 监听地址：默认绑定全部接口，供 WSL/网关访问；请用 Windows 防火墙
    # 只放行 WSL/网关来源。仅本机使用时可改为 127.0.0.1。
    host = '0.0.0.0'
    port = 6017

    repo_dir = REPO_DIR
    data_dir = REPO_DIR / 'admin' / 'data'
    static_dir = REPO_DIR / 'admin' / 'static'

    # ASR 识别进程
    # 两种部署形态（admin_settings.json 的 asr_work_dir / asr_launch_cmd 决定）：
    #   源码运行：asr_work_dir=仓库目录，launch_cmd 留空 → [python, start_server.py]
    #   打包版  ：asr_work_dir=实际 ASR 工作目录（如
    #             C:\Users\<user>\Apps\CapsWriter-Offline-v2.6\runtime\CapsWriter-Offline），
    #             asr_launch_cmd=["start_server.exe 的绝对路径"]
    # PID 文件、控制通道令牌、日志、热词、server_settings.json 都指向
    # asr_work_dir，保证状态、配置与日志管理作用于真实 ASR。
    asr_work_dir = REPO_DIR
    asr_launch_cmd: list = []
    asr_entry = REPO_DIR / 'start_server.py'
    asr_host = '127.0.0.1'
    asr_port = 6016
    asr_pid_file = REPO_DIR / 'logs' / 'server.pid'
    asr_control_url = 'ws://127.0.0.1:6019'
    asr_control_token_path = REPO_DIR / 'logs' / 'server_control.token'
    asr_stop_timeout = 30          # 优雅退出最长等待（秒）
    asr_start_timeout = 300        # 启动+模型加载最长等待（秒）

    # 网页可管理的设置文件（config_server.py 启动时读取其覆盖值；位于 ASR 工作目录）
    settings_file = REPO_DIR / 'server_settings.json'

    # 热词与日志（位于 ASR 工作目录）
    hotwords_path = REPO_DIR / 'hot-server.txt'
    log_file = REPO_DIR / 'logs' / 'server_latest.log'
    log_api_max_lines = 200
    log_api_max_bytes = 64 * 1024

    # 管理员认证
    # admin_auth_mode:
    #   'password'  自带管理员密码登录（默认；无需网关即可用）
    #   'gateway'   信任部署面板人用登录网关：管理域名整站受网关保护后启用。
    #               网关须注入可识别的用户请求头（见 gateway_user_headers），
    #               且 Windows 防火墙应只放行网关来源访问本端口，防止伪造头直连。
    admin_auth_mode = 'password'
    gateway_user_headers = ('x-remote-user', 'x-forwarded-user', 'x-auth-request-user', 'remote-user')
    password_file = REPO_DIR / 'admin' / 'data' / 'admin_password.json'
    session_ttl = 12 * 3600
    login_rate_limit = 5           # 每分钟每 IP 失败次数上限
    audit_log = REPO_DIR / 'admin' / 'data' / 'audit.log'

    # 阻塞调用与后台动作线程
    action_session_wait = 120      # 停止/重启前等待活跃会话清空的上限（秒）


def load_overrides(path: Path | str | None = None) -> None:
    """用 JSON 文件覆盖 AdminConfig 类属性（键不认识时忽略）。

    asr_work_dir 设置后会重派生 ASR 相关路径（PID/控制令牌/日志/热词/
    server_settings.json/start_server.py），除非这些键也在覆盖文件里
    显式给出。"""
    p = Path(path) if path else REPO_DIR / 'admin_settings.json'
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return
    if not isinstance(data, dict):
        return
    known = {k for k in dir(AdminConfig) if not k.startswith('_')}
    for key, value in data.items():
        if key.startswith('_') or key not in known:
            continue
        setattr(AdminConfig, key, value)

    work_dir = Path(AdminConfig.asr_work_dir)
    if str(work_dir) != str(REPO_DIR):
        derived = {
            'asr_entry': work_dir / 'start_server.py',
            'asr_pid_file': work_dir / 'logs' / 'server.pid',
            'asr_control_token_path': work_dir / 'logs' / 'server_control.token',
            'hotwords_path': work_dir / 'hot-server.txt',
            'log_file': work_dir / 'logs' / 'server_latest.log',
            'settings_file': work_dir / 'server_settings.json',
        }
        for key, value in derived.items():
            if key not in data:
                setattr(AdminConfig, key, value)
