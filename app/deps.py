"""FastAPI dependencies: access the shared client, settings and local store."""

from __future__ import annotations

import sqlite3

from fastapi import Request

from pypowens import PowensClient

from .config import Settings


def get_client(request: Request) -> PowensClient:
    return request.app.state.client


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_store(request: Request) -> sqlite3.Connection:
    return request.app.state.store
