# coding: utf-8
"""
管理进程 HTTP 服务（标准库实现，零新增依赖）

- ThreadingHTTPServer + 简单路由表（method + 正则 + 鉴权/CSRF 标记）；
- 请求封装与 JSON/静态文件响应助手；
- 所有 Cookie 属性、Origin 校验等安全策略集中在 webapp 的中间层处理。
"""

from __future__ import annotations

import json
import re
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

__all__ = ['Request', 'Response', 'Router', 'HTTPError', 'make_server']


class HTTPError(Exception):
    def __init__(self, status: int, message: str, request_id: str = ''):
        super().__init__(message)
        self.status = status
        self.message = message
        self.request_id = request_id


class Request:
    def __init__(self, handler: BaseHTTPRequestHandler, body_bytes: bytes):
        split = urlsplit(handler.path)
        self.method = handler.command
        self.path = unquote(split.path)
        self.query: Dict[str, str] = {k: v[0] for k, v in parse_qs(split.query).items()}
        self.headers = handler.headers
        self.body_bytes = body_bytes
        self.remote_ip = handler.client_address[0] if handler.client_address else ''
        self.params: Dict[str, str] = {}

    @property
    def secure(self) -> bool:
        return self.headers.get('x-forwarded-proto', '').lower() == 'https'

    @property
    def origin(self) -> str:
        return self.headers.get('origin', '') or self.headers.get('referer', '')

    @property
    def host(self) -> str:
        return self.headers.get('host', '')

    def cookies(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        raw = self.headers.get('cookie', '')
        for part in raw.split(';'):
            if '=' in part:
                k, v = part.split('=', 1)
                out[k.strip()] = v.strip()
        return out

    def json(self) -> Any:
        if not self.body_bytes:
            return {}
        try:
            return json.loads(self.body_bytes.decode('utf-8'))
        except Exception:
            raise HTTPError(400, '请求体不是合法 JSON')


class Response:
    def __init__(self, status: int = 200, body: bytes = b'', content_type: str = 'application/json; charset=utf-8',
                 headers: Optional[List[Tuple[str, str]]] = None):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers or []

    @classmethod
    def json(cls, data: Any, status: int = 200, headers: Optional[List[Tuple[str, str]]] = None) -> 'Response':
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        return cls(status, body, headers=headers)

    @classmethod
    def text(cls, data: str, status: int = 200, headers: Optional[List[Tuple[str, str]]] = None) -> 'Response':
        return cls(status, data.encode('utf-8'), 'text/plain; charset=utf-8', headers=headers)


RouteHandler = Callable[[Request], Response]


class Router:
    def __init__(self):
        # (method, compiled_regex, handler, needs_auth, needs_csrf)
        self.routes: List[Tuple[str, re.Pattern, RouteHandler, bool, bool]] = []

    def add(self, method: str, pattern: str, handler: RouteHandler, auth: bool = False, csrf: bool = False) -> None:
        compiled = re.compile('^' + pattern + '$')
        self.routes.append((method.upper(), compiled, handler, auth, csrf))

    def resolve(self, method: str, path: str) -> Tuple[RouteHandler, Dict[str, str], bool, bool]:
        allowed_methods = set()
        for m, compiled, handler, auth, csrf in self.routes:
            match = compiled.match(path)
            if not match:
                continue
            if m != method:
                allowed_methods.add(m)
                continue
            return handler, {k: v for k, v in match.groupdict().items()}, auth, csrf
        if allowed_methods:
            raise HTTPError(405, 'method not allowed')
        raise HTTPError(404, 'not found')


class _Handler(BaseHTTPRequestHandler):
    server_version = 'CapsWriterAdmin/1.0'
    protocol_version = 'HTTP/1.1'
    app = None  # type: Any

    def do_GET(self):    self._dispatch()  # noqa: E701
    def do_POST(self):   self._dispatch()  # noqa: E701
    def do_PUT(self):    self._dispatch()  # noqa: E701
    def do_DELETE(self): self._dispatch()  # noqa: E701

    def _read_body(self) -> bytes:
        length = int(self.headers.get('content-length') or 0)
        if length > 4 * 1024 * 1024:
            raise HTTPError(413, 'request body too large')
        return self.rfile.read(length) if length else b''

    def _dispatch(self) -> None:
        request_id = ''
        try:
            body = self._read_body()
            request = Request(self, body)
            handler, params, needs_auth, needs_csrf = self.app.router.resolve(request.method, request.path)
            request.params = params
            response = self.app.handle(request, handler, needs_auth, needs_csrf)
        except HTTPError as e:
            response = Response.json({'ok': False, 'error': e.message, 'request_id': e.request_id}, e.status)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            response = Response.json({'ok': False, 'error': 'internal error', 'request_id': request_id}, 500)
        try:
            self.send_response(response.status)
            self.send_header('Content-Type', response.content_type)
            self.send_header('Content-Length', str(len(response.body)))
            for key, value in response.headers:
                self.send_header(key, value)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(response.body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):  # 安静模式：访问日志交给审计
        pass


def make_server(host: str, port: int, app) -> ThreadingHTTPServer:
    handler = type('BoundHandler', (_Handler,), {'app': app})

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    return Server((host, port), handler)
