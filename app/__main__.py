"""Entry point: ``python -m app`` — launches uvicorn and opens the browser."""

from __future__ import annotations

import threading
import webbrowser

import uvicorn

from .config import get_settings


def _open_browser(url: str) -> None:
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()


def main() -> None:
    settings = get_settings()
    url = f"http://{settings.host}:{settings.port}"
    print(f"\n  Powens Finance → {url}\n")
    _open_browser(url)
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
