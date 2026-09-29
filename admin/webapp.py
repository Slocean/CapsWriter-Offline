# coding: utf-8
"""
管理端 API 与页面装配

鉴权边界（admin_auth_mode，见 config_admin.AdminConfig）：
- 'password'（默认）：自带管理员密码登录；无需网关即可使用。
- 'gateway'：信任部署面板人用登录网关。管理域名整站受网关保护后启用；
  每个请求须携带网关注入的用户请求头（gateway_user_headers），且应通过
  Windows 防火墙把本端口来源限制为网关，防止伪造头直连。

两种模式下：
- /health 与 /api/v1/bootstrap 不要求登录（bootstrap 只回报模式，
  不泄露数据）；
- 其余 /api/* 要求认证；写操作另要求 CSRF 头与 Origin 同源；
- 客户端语音接入的 API Key 校验在部署面板网关完成，与本进程无关。
"""

from __future__ import annotations

import hmac
import mimetypes
import secrets
import time
import uuid
from pathlib import Path
from typing import Optional

from config_server import ServerConfig as ServerConfigClass

from . import audit, logsource
from .asr_control import ASRControl
from .config_admin import AdminConfig as Cfg
from .hotwords_store import read_hotwords, write_hotwords
from .httpd import HTTPError, Request, Response, Router
from .sessions import AdminAuth
from .settings_store import SETTINGS_SCHEMA, load_settings, save_settings

SESSION_COOKIE = 'cw_admin_session'
CSRF_COOKIE = 'cw_admin_csrf'


class AdminApp:
    def __init__(self):
        self.router = Router()
        self.mode = Cfg.admin_auth_mode if Cfg.admin_auth_mode in ('password', 'gateway') else 'password'
        self.auth = AdminAuth() if self.mode == 'password' else None
        self.asr = ASRControl()
        self.started_at = time.time()
        self._register_routes()

    # ---------------- 中间层 ----------------

    def handle(self, request: Request, handler, needs_auth: bool, needs_csrf: bool) -> Response:
        request.session = None
        request.gateway_user = ''
        if needs_auth:
            if self.mode == 'gateway':
                self._require_gateway(request)
            else:
                request.session = self._require_session(request)
            if needs_csrf:
                self._require_csrf(request, request.session)
        try:
            return handler(request)
        except HTTPError:
            raise
        except Exception:
            raise HTTPError(500, 'internal error', request_id=uuid.uuid4().hex[:8])

    def _require_session(self, request: Request) -> dict:
        token = request.cookies().get(SESSION_COOKIE, '')
        session = self.auth.session(token)
        if not session:
            raise HTTPError(401, '未登录或会话已过期')
        return session

    def _require_gateway(self, request: Request) -> None:
        """网关模式：必须携带部署面板登录网关注入的用户请求头"""
        for header in Cfg.gateway_user_headers:
            value = (request.headers.get(header) or '').strip()
            if value:
                request.gateway_user = value[:128]
                return
        raise HTTPError(401, '请先通过部署面板登录网关访问管理页')

    def _require_csrf(self, request: Request, session: Optional[dict]) -> None:
        # 写操作：CSRF double-submit + Origin 同源校验
        origin = request.origin
        if origin:
            from urllib.parse import urlsplit
            origin_host = urlsplit(origin).hostname or ''
            request_host = urlsplit(f'//{request.host}').hostname if request.host else ''
            if request_host and origin_host and not hmac.compare_digest(origin_host.lower(), request_host.lower()):
                raise HTTPError(403, 'Origin 校验失败')
        header = request.headers.get('x-csrf-token', '')
        cookie = request.cookies().get(CSRF_COOKIE, '')
        if not header or not cookie or not hmac.compare_digest(header, cookie):
            raise HTTPError(403, 'CSRF 校验失败')
        if session is not None and not hmac.compare_digest(header, session.get('csrf', '')):
            raise HTTPError(403, 'CSRF 校验失败')

    def _set_auth_cookies(self, session_token: str, csrf: str, secure: bool):
        flags = 'Path=/; HttpOnly; SameSite=Strict'
        if secure:
            flags += '; Secure'
        csrf_flags = 'Path=/; SameSite=Strict'
        if secure:
            csrf_flags += '; Secure'
        return [
            ('Set-Cookie', f'{SESSION_COOKIE}={session_token}; {flags}'),
            ('Set-Cookie', f'{CSRF_COOKIE}={csrf}; {csrf_flags}'),
        ]

    def _set_csrf_cookie(self, csrf: str, secure: bool):
        flags = 'Path=/; SameSite=Strict'
        if secure:
            flags += '; Secure'
        return [('Set-Cookie', f'{CSRF_COOKIE}={csrf}; {flags}')]

    def _clear_auth_cookies(self):
        return [
            ('Set-Cookie', f'{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'),
            ('Set-Cookie', f'{CSRF_COOKIE}=; Path=/; SameSite=Strict; Max-Age=0'),
        ]

    # ---------------- 路由 ----------------

    def _register_routes(self):
        r = self.router
        r.add('GET', r'/health', self.health)
        r.add('GET', r'/api/v1/bootstrap', self.bootstrap)

        if self.mode == 'password':
            r.add('POST', r'/api/v1/login', self.login)
            r.add('POST', r'/api/v1/logout', self.logout, auth=True)

        r.add('GET', r'/api/v1/status', self.status, auth=True)
        r.add('GET', r'/api/v1/logs', self.logs, auth=True)
        r.add('GET', r'/api/v1/audit', self.audit_tail, auth=True)

        r.add('GET', r'/api/v1/settings', self.get_settings, auth=True)
        r.add('PUT', r'/api/v1/settings', self.put_settings, auth=True, csrf=True)

        r.add('GET', r'/api/v1/hotwords', self.get_hotwords, auth=True)
        r.add('PUT', r'/api/v1/hotwords', self.put_hotwords, auth=True, csrf=True)

        r.add('POST', r'/api/v1/actions/(?P<kind>start|stop|restart)', self.post_action, auth=True, csrf=True)
        r.add('GET', r'/api/v1/actions/current', self.current_action, auth=True)

        r.add('GET', r'/', self.index)
        r.add('GET', r'/static/(?P<name>[A-Za-z0-9._\-]+)', self.static_file)

    # ---------------- 基础 ----------------

    def health(self, request: Request) -> Response:
        # 网关/存活探针：不要求 ASR 正常
        return Response.json({'ok': True, 'service': 'capswriter-admin', 'uptime_s': round(time.time() - self.started_at, 1)})

    def bootstrap(self, request: Request) -> Response:
        """前端启动探测：回报认证模式；网关模式且已过网关时签发 CSRF cookie"""
        if self.mode == 'gateway':
            try:
                self._require_gateway(request)
            except HTTPError:
                return Response.json({'ok': True, 'mode': 'gateway', 'authenticated': False})
            csrf = request.cookies().get(CSRF_COOKIE, '')
            headers = []
            if not csrf:
                csrf = secrets.token_urlsafe(24)
                headers = self._set_csrf_cookie(csrf, request.secure)
            audit.record('gateway.access', request.remote_ip, detail=f'user={request.gateway_user}')
            return Response.json({'ok': True, 'mode': 'gateway', 'authenticated': True, 'csrf': csrf},
                                 headers=headers)
        token = request.cookies().get(SESSION_COOKIE, '')
        authenticated = bool(self.auth and self.auth.session(token))
        return Response.json({'ok': True, 'mode': 'password', 'authenticated': authenticated})

    def login(self, request: Request) -> Response:
        ip = request.remote_ip
        if not self.auth.login_allowed(ip):
            audit.record('login.rate_limited', ip, ok=False)
            return Response.json({'ok': False, 'error': '尝试过于频繁，请稍后再试'}, 429)
        data = request.json()
        password = str(data.get('password', ''))
        if not password or not self.auth.verify_password(password):
            self.auth.record_login_failure(ip)
            audit.record('login.failed', ip, ok=False)
            return Response.json({'ok': False, 'error': '密码错误'}, 401)
        token = self.auth.login()
        session = self.auth.session(token) or {}
        audit.record('login.ok', ip)
        return Response.json({'ok': True, 'csrf': session.get('csrf')},
                             headers=self._set_auth_cookies(token, session.get('csrf', ''), request.secure))

    def logout(self, request: Request) -> Response:
        self.auth.logout(request.cookies().get(SESSION_COOKIE, ''))
        return Response.json({'ok': True}, headers=self._clear_auth_cookies())

    # ---------------- 状态与日志 ----------------

    def status(self, request: Request) -> Response:
        return Response.json({'ok': True, 'asr': self.asr.status(), 'admin': {'version': 1, 'uptime_s': round(time.time() - self.started_at, 1)}})

    def logs(self, request: Request) -> Response:
        try:
            cursor = int(request.query.get('cursor', '0'))
        except ValueError:
            raise HTTPError(422, 'cursor 必须是整数')
        result = logsource.tail(cursor=cursor)
        result['ok'] = True
        return Response.json(result)

    def audit_tail(self, request: Request) -> Response:
        return Response.json({'ok': True, 'entries': audit.tail(50)})

    # ---------------- 设置 ----------------

    def get_settings(self, request: Request) -> Response:
        values = load_settings()
        # 输出补齐 schema 中每个字段的当前生效值（含默认值）
        current = {k: values.get(k, getattr(ServerConfigClass, k, None)) for k in SETTINGS_SCHEMA}
        return Response.json({
            'ok': True,
            'values': current,
            'schema': {
                k: {'label': s['label'], 'type': s['type'], 'choices': s.get('choices'),
                    'min': s.get('min'), 'max': s.get('max'), 'restart': s.get('restart', False),
                    'help': s.get('help', '')}
                for k, s in SETTINGS_SCHEMA.items()
            },
        })

    def put_settings(self, request: Request) -> Response:
        data = request.json()
        ok, err = save_settings(data)
        if not ok:
            audit.record('settings.save', request.remote_ip, ok=False, detail=err)
            raise HTTPError(422, err)
        audit.record('settings.save', request.remote_ip, detail=','.join(sorted(data.keys())))
        restart_needed = [k for k in data.keys() if SETTINGS_SCHEMA.get(k, {}).get('restart')]
        return Response.json({'ok': True, 'restart_required': restart_needed})

    # ---------------- 热词 ----------------

    def get_hotwords(self, request: Request) -> Response:
        try:
            info = read_hotwords()
        except ValueError as e:
            raise HTTPError(422, str(e))
        info['ok'] = True
        return Response.json(info)

    def put_hotwords(self, request: Request) -> Response:
        data = request.json()
        text = data.get('text')
        base_mtime = data.get('base_mtime')
        try:
            current = read_hotwords()
        except ValueError as e:
            raise HTTPError(422, str(e))
        # 冲突检测：页面打开期间文件被其他进程改过
        if base_mtime is not None and current.get('exists') and current.get('mtime') != base_mtime:
            raise HTTPError(409, '热词文件已被其他进程修改，请刷新后再保存')
        try:
            write_hotwords(text)
        except ValueError as e:
            raise HTTPError(422, str(e))
        audit.record('hotwords.save', request.remote_ip, detail=f'{len(text)} chars')
        return Response.json({'ok': True, 'restart_required': True})

    # ---------------- 动作 ----------------

    def post_action(self, request: Request) -> Response:
        kind = request.params['kind']
        data = request.json()
        result = self.asr.submit(kind, force=bool(data.get('force')))
        if not result.get('ok'):
            audit.record(f'action.{kind}', request.remote_ip, ok=False, detail='conflict')
            raise HTTPError(409, '已有操作正在进行中')
        audit.record(f'action.{kind}', request.remote_ip, detail=f"action={result['action']['id']} force={bool(data.get('force'))}")
        return Response.json({'ok': True, 'action': result['action']})

    def current_action(self, request: Request) -> Response:
        return Response.json({'ok': True, 'action': self.asr.current_action()})

    # ---------------- 静态页面 ----------------

    def index(self, request: Request) -> Response:
        return self._static('index.html')

    def static_file(self, request: Request) -> Response:
        return self._static(request.params['name'])

    def _static(self, name: str) -> Response:
        path = (Cfg.static_dir / name).resolve()
        try:
            path.relative_to(Cfg.static_dir.resolve())
        except ValueError:
            raise HTTPError(404, 'not found')
        if not path.exists():
            raise HTTPError(404, 'not found')
        ctype = mimetypes.guess_type(str(path))[0] or 'application/octet-stream'
        body = path.read_bytes()
        headers = [('Cache-Control', 'no-store')]
        return Response(200, body, f'{ctype}; charset=utf-8' if ctype.startswith('text/') else ctype, headers)
