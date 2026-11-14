"""Локальный сервер. Запуск: python -m app.main"""
from fastapi import FastAPI

from app import __version__
from app.api.routes import router

app = FastAPI(title="Помощник по выбору закупок", version=__version__)
app.include_router(router)


if __name__ == "__main__":
    import os

    import uvicorn

    # Сервер слушает только этот компьютер: снаружи он недоступен.
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8765")))
