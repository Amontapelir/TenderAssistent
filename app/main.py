"""Локальный сервер. Запуск: python -m app.main"""
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.access import is_local_host_header
from app.api.routes import router

UI_DIR = Path(__file__).resolve().parent.parent / "docs"

# Ответы сервера нельзя встраивать в чужие страницы (иначе чужой сайт мог бы подложить кнопку
# «Подтвердить» под свой рисунок), а браузер не должен угадывать тип содержимого.
SECURITY_HEADERS = [
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-resource-policy", b"same-origin"),
]


class LocalGuard:
    """Отвечает только на запросы, адресованные этому компьютеру, и добавляет защитные заголовки."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"host"), "")
        if not is_local_host_header(host):
            body = json.dumps({"detail": "Сервер отвечает только на запросы к этому компьютеру "
                                         "(localhost или 127.0.0.1)."}, ensure_ascii=False).encode("utf-8")
            await send({"type": "http.response.start", "status": 400,
                        "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode())] + SECURITY_HEADERS})
            await send({"type": "http.response.body", "body": body})
            return

        async def guarded_send(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": list(message.get("headers", [])) + SECURITY_HEADERS}
            await send(message)

        await self.app(scope, receive, guarded_send)


app = FastAPI(title="Помощник по выбору закупок", version=__version__)
app.add_middleware(LocalGuard)
app.include_router(router)
# Страница та же, что на GitHub Pages. Подключается последней: запросы /api и страница
# документации /docs совпадают раньше, а всё остальное отдаётся из папки docs.
if (UI_DIR / "index.html").is_file():
    app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")


if __name__ == "__main__":
    import os

    import uvicorn

    # Сервер слушает только этот компьютер: снаружи он недоступен.
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8765")))
