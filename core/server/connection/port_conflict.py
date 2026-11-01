"""Reserve a listening port and release a verified conflicting process."""

import os
import socket

import psutil


def reserve_listener(address, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((address, int(port)))
        sock.listen(socket.SOMAXCONN)
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


def listeners(port, address):
    result = {}
    for conn in psutil.net_connections(kind='tcp4'):
        if conn.status != psutil.CONN_LISTEN or not conn.laddr or conn.laddr.port != int(port):
            continue
        if address != '0.0.0.0' and conn.laddr.ip not in ('0.0.0.0', address):
            continue
        if not conn.pid or conn.pid in (os.getpid(), 4):
            raise RuntimeError('无法安全确认端口占用进程')
        proc = psutil.Process(conn.pid)
        result[conn.pid] = (proc.create_time(), proc.exe())
    if not result:
        raise RuntimeError('端口不可用，但无法确认监听进程')
    return result


def stop_listeners(port, address, expected):
    if listeners(port, address) != expected:
        raise RuntimeError('端口占用者已变化，请重新选择')
    for pid, (created, exe) in expected.items():
        proc = psutil.Process(pid)
        if proc.create_time() != created or proc.exe() != exe:
            raise RuntimeError('进程身份已变化，请重新选择')
        proc.terminate()
    for pid, (created, _) in expected.items():
        try:
            proc = psutil.Process(pid)
            if proc.create_time() == created:
                proc.wait(timeout=10)
        except psutil.NoSuchProcess:
            pass
