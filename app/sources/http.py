"""Исходящие запросы к API: без перенаправлений и с потолком размера ответа.

Перенаправления запрещены, потому что вместе с запросом уходит секрет: ключ DaMIA стоит в адресе,
ключ модели в заголовке Authorization. При перенаправлении стандартная библиотека повторила бы
запрос по новому адресу, возможно на другой сервер или по http, и секрет ушёл бы туда.
"""
import urllib.error
import urllib.request


class TooLarge(RuntimeError):
    """Ответ больше потолка. Текст исключения показывается пользователю."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # ответ 3xx вернётся как ошибка HTTPError с этим кодом


_opener = urllib.request.build_opener(_NoRedirect)


def open_no_redirect(request: urllib.request.Request, timeout: float):
    """Как urlopen, но ответ с кодом 3xx не исполняется, а приходит как HTTPError."""
    return _opener.open(request, timeout=timeout)


def read_capped(response, limit: int, what: str) -> bytes:
    """Читает ответ целиком, но не больше limit байт: иначе TooLarge."""
    data = response.read(limit + 1)
    if len(data) > limit:
        raise TooLarge(f"{what}: ответ больше {limit // 2**20} МБ, чтение остановлено.")
    return data
