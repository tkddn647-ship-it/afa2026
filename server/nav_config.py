from __future__ import annotations

import os
from typing import Any


def _public_host() -> str:
    return os.getenv("AFA_PUBLIC_HOST", "your.domain.example").strip() or "your.domain.example"


def _public_scheme() -> str:
    return os.getenv("AFA_PUBLIC_SCHEME", "https").strip() or "https"


def _normalize_public_url(url: str, *, trailing_slash: bool = False) -> str:
    text = (url or "").strip().rstrip("/")
    if trailing_slash:
        return f"{text}/"
    return text


# nginx path proxy 공개 URL (항상 trailing slash — /camera 단독은 로깅으로 떨어질 수 있음)
LOGGER_URL = _normalize_public_url(
    os.getenv("LOGGER_URL", f"{_public_scheme()}://{_public_host()}")
)
REALTIME_URL = _normalize_public_url(
    os.getenv("REALTIME_URL", f"{_public_scheme()}://{_public_host()}/live"),
    trailing_slash=True,
)
CAMERA_URL = _normalize_public_url(
    os.getenv("CAMERA_URL", f"{_public_scheme()}://{_public_host()}/camera"),
    trailing_slash=True,
)
ANALYSIS_URL = _normalize_public_url(
    os.getenv("ANALYSIS_URL", f"{_public_scheme()}://{_public_host()}/analysis"),
    trailing_slash=True,
)


def nav_context(current: str, request: Any = None) -> dict[str, Any]:
    nav = {
        "logger": LOGGER_URL or f"{_public_scheme()}://{_public_host()}",
        "realtime": REALTIME_URL,
        "camera": CAMERA_URL,
        "camera_origin": CAMERA_URL.rstrip("/"),
        "analysis": ANALYSIS_URL,
        "current": current,
    }
    try:
        from afa_auth import auth_template_context, enrich_nav_with_sso

        if request is not None:
            nav = enrich_nav_with_sso(nav, request, current)
            return {"nav": nav, **auth_template_context(request)}
        return {"nav": nav, **auth_template_context(None)}
    except ImportError:
        return {"nav": nav}
