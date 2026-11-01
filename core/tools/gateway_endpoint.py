"""Publish the current listening endpoints for the local gateway."""

import asyncio
import ipaddress
import os
import socket
from pathlib import Path


def endpoints(address, port):
    if address != '0.0.0.0':
        addresses = {address}
    else:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM)}
    return ''.join(f'{ip}:{int(port)}\n' for ip in sorted(addresses)
                   if ipaddress.ip_address(ip).version == 4
                   and not ipaddress.ip_address(ip).is_loopback
                   and not ipaddress.ip_address(ip).is_link_local)


async def publish(path, address, port, logger):
    path = Path(path)
    while True:
        try:
            value = endpoints(address, port)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.tmp')
            tmp.write_text(value, encoding='ascii')
            os.replace(tmp, path)
        except (OSError, ValueError) as exc:
            logger.warning(f'无法更新网关地址: {exc}')
        await asyncio.sleep(3)
