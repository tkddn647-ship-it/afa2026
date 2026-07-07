import os
from pathlib import Path
from typing import Any


def _load_env_file(filename: str = "env.ingest") -> None:
    env_path = Path(__file__).resolve().parent / filename
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file()


def _normalize_base_url(value: str) -> str:
    return value.strip().rstrip("/")


def _resolve_server_url(env_key: str, default_port: int) -> str:
    explicit = _normalize_base_url(os.getenv(env_key, ""))
    if explicit:
        return explicit

    public_host = os.getenv("AFA_PUBLIC_HOST", "").strip()
    if public_host:
        if not public_host.startswith(("http://", "https://")):
            public_host = f"http://{public_host}"
        return f"{_normalize_base_url(public_host)}:{default_port}"

    return f"http://127.0.0.1:{default_port}"


LOGGER_URL = _resolve_server_url("LOGGER_PUBLIC_URL", int(os.getenv("LOGGER_PORT", "8000")))
REALTIME_URL = _resolve_server_url("REALTIME_PUBLIC_URL", int(os.getenv("REALTIME_PORT", "8011")))
CAMERA_URL = _resolve_server_url("CAMERA_PUBLIC_URL", int(os.getenv("CAMERA_PORT", "8012")))


def nav_context(current: str, request: Any = None) -> dict[str, Any]:
    if request is not None:
        host = request.url.hostname or "127.0.0.1"
        scheme = request.url.scheme or "http"
        logger_port = int(os.getenv("LOGGER_PORT", "8000"))
        realtime_port = int(os.getenv("REALTIME_PORT", "8011"))
        camera_port = int(os.getenv("CAMERA_PORT", "8012"))
        nav = {
            "logger": f"{scheme}://{host}:{logger_port}",
            "realtime": f"{scheme}://{host}:{realtime_port}",
            "camera": f"{scheme}://{host}:{camera_port}",
            "camera_origin": f"{scheme}://{host}:{camera_port}",
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
            "current": current,
        }
    }
