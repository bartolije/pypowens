"""FastAPI dependencies: access the shared client and settings."""

from __future__ import annotations

from fastapi import Request

from pypowens import PowensClient

from .config import Settings


def get_client(request: Request) -> PowensClient:
    return request.app.state.client


def get_settings(request: Request) -> Settings:
    return request.app.state.settings
