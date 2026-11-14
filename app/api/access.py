"""Доступ к изменяющим запросам только с этого компьютера.

Изменяющий запрос тратит квоту ключа или переписывает результаты. Он принимается, только если:
соединение пришло с этого компьютера, заголовок Host называет этот компьютер (защита от подмены
адреса через DNS) и страница-источник запроса (Origin), если она указана, открыта с него же
(защита от запроса с чужого сайта, открытого в браузере владельца).
"""
import ipaddress
from typing import Optional
from urllib.parse import urlsplit

_LOCAL_NAMES = {"localhost"}


def _is_local_host(host: str) -> bool:
    host = host.strip().lower().strip("[]")
    if host in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_of(header: str) -> str:
    """Имя из заголовка Host: "localhost:8000", "[::1]:8000"."""
    header = header.strip()
    if header.startswith("["):
        return header[1:].split("]", 1)[0]
    return header.rsplit(":", 1)[0] if ":" in header else header


def is_local_request(client_host: Optional[str], host_header: Optional[str],
                     origin: Optional[str]) -> bool:
    if not client_host or not _is_local_host(client_host):
        return False
    if not host_header or not _is_local_host(_host_of(host_header)):
        return False
    if origin is not None:
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return False
        if not _is_local_host(parts.hostname):
            return False
    return True
