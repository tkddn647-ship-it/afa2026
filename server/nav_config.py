from __future__ import annotations

import os
from typing import Any


def _public_host() -> str:
    return os.getenv("AFA_PUBLIC_HOST", "127.0.0.1").strip() or "127.0.0.1"


def _service_url(port: int) -> str:
    scheme = os.getenv("AFA_PUBLIC_SCHEME", "http").strip() or "http"
    return f"{scheme}://{_public_host()}:{port}"


LOGGER_URL = os.getenv(
    "LOGGER_URL",
    _service_url(int(os.getenv("LOGGER_PORT", "8000"))),
)
REALTIME_URL = os.getenv(
    "REALTIME_URL",
    _service_url(int(os.getenv("REALTIME_PORT", "8011"))),
)
CAMERA_URL = os.getenv(
    "CAMERA_URL",
    _service_url(int(os.getenv("CAMERA_PORT", "8012"))),
)
ANALYSIS_URL = os.getenv(
    "ANALYSIS_URL",
    _service_url(int(os.getenv("ANALYSIS_PORT", "8011"))),
)


def nav_context(current: str, request: Any = None) -> dict[str, Any]:
    if request is not None:
        host = request.url.hostname or "127.0.0.1"
        scheme = request.url.scheme or "http"
        logger_port = int(os.getenv("LOGGER_PORT", "8000"))
        realtime_port = int(os.getenv("REALTIME_PORT", "8011"))
        camera_port = int(os.getenv("CAMERA_PORT", "8012"))
        analysis_port = int(os.getenv("ANALYSIS_PORT", "8011"))
        nav = {
            "logger": f"{scheme}://{host}:{logger_port}",
            "realtime": f"{scheme}://{host}:{realtime_port}",
            "camera": f"{scheme}://{host}:{camera_port}",
            "camera_origin": f"{scheme}://{host}:{camera_port}",
            "analysis": f"{scheme}://{host}:{analysis_port}",
            "current": current,
        }
        try:
            from afa_auth import auth_template_context, enrich_nav_with_sso

            nav = enrich_nav_with_sso(nav, request, current)
            return {"nav": nav, **auth_template_context(request)}
        except ImportError:
            return {"nav": nav}

    return {
        "nav": {
            "logger": LOGGER_URL,
            "realtime": REALTIME_URL,
            "camera": CAMERA_URL,
            "camera_origin": CAMERA_URL,
            "analysis": ANALYSIS_URL,
            "current": current,
        }
    }
