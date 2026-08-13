"""Entry point: ``python -m app`` — launches uvicorn and opens the browser."""

from __future__ import annotations

import logging
import threading
import webbrowser

import uvicorn

from .config import _LOOPBACK_HOSTS, get_settings


def _open_browser(url: str) -> None:
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s"
    )
    settings = get_settings()
    url = f"http://{settings.host}:{settings.port}"
    print(f"\n  Powens Finance → {url}\n")
    # Écouter au-delà de la loopback, c'est tourner sur un serveur : personne
    # n'est devant l'écran, et un conteneur n'a de toute façon pas de navigateur.
    if settings.host in _LOOPBACK_HOSTS:
        _open_browser(url)
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
