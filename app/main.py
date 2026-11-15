"""Локальный сервер. Запуск: python -m app.main"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.routes import router

UI_DIR = Path(__file__).resolve().parent.parent / "docs"

app = FastAPI(title="Помощник по выбору закупок", version=__version__)
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
