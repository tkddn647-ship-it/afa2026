import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware


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

SESSION_COOKIE = "afa_session"
PUBLIC_GET_PATHS = {
    "/login",
    "/auth/sso",
    "/api/auth/status",
    "/api/auth/login",
    "/api/stats",
    "/api/offset/status",
    "/api/telemetry/snapshot",
    "/api/recording/status",
    "/favicon.ico",
}
PUBLIC_POST_PATHS = {
    "/ingest",
    "/api/live/ingest",
    "/api/camera/frame",
    "/api/auth/login",
    "/api/auth/logout",
}
PUBLIC_GET_PREFIXES = (
    "/api/live/snapshot",
    "/api/camera/latest",
    "/api/camera/stream",
    "/api/camera/preview",
)


def auth_enabled() -> bool:
    return os.getenv("AFA_AUTH_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def _auth_username() -> str:
    return os.getenv("AFA_AUTH_USERNAME", "afa").strip() or "afa"


def _auth_password() -> str:
    return os.getenv("AFA_AUTH_PASSWORD", "")


def _auth_secret() -> str:
    secret = os.getenv("AFA_AUTH_SECRET", "").strip()
    if secret:
        return secret
    return hashlib.sha256(f"{_auth_username()}:{_auth_password()}".encode()).hexdigest()


def _session_seconds() -> int:
    hours = float(os.getenv("AFA_AUTH_SESSION_HOURS", "24"))
    return max(300, int(hours * 3600))


def _sso_seconds() -> int:
    return max(15, int(os.getenv("AFA_AUTH_SSO_SECONDS", "60")))


def _sign_payload(payload: str) -> str:
    return hmac.new(_auth_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()


def _encode_token(kind: str, username: str, expires_at: int) -> str:
    payload = f"{kind}:{username}:{expires_at}"
    token = f"{payload}:{_sign_payload(payload)}"
    return base64.urlsafe_b64encode(token.encode()).decode()


def _decode_token(token: str, *, kind: str) -> str | None:
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        signed_payload, signature = raw.rsplit(":", 1)
        if not signed_payload.startswith(f"{kind}:"):
            return None
        if not hmac.compare_digest(_sign_payload(signed_payload), signature):
            return None
        _, username, expires_raw = signed_payload.split(":", 2)
        if int(expires_raw) < int(time.time()):
            return None
        return username
    except Exception:
        return None


def create_session_token(username: str) -> str:
    return _encode_token("session", username, int(time.time()) + _session_seconds())


def create_sso_token(username: str) -> str:
    return _encode_token("sso", username, int(time.time()) + _sso_seconds())


def verify_session_token(token: str) -> str | None:
    return _decode_token(token, kind="session")


def verify_sso_token(token: str) -> str | None:
    return _decode_token(token, kind="sso")


def get_request_user(request: Request) -> str | None:
    if not auth_enabled():
        return None
    cached = getattr(request.state, "auth_user", None)
    if isinstance(cached, str) and cached:
        return cached
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    user = verify_session_token(token)
    if user:
        request.state.auth_user = user
    return user


def credentials_valid(username: str, password: str) -> bool:
    expected_user = _auth_username()
    expected_pass = _auth_password()
    if not expected_pass:
        return False
    return secrets.compare_digest(username.strip(), expected_user) and secrets.compare_digest(password, expected_pass)


def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept or request.url.path == "/"


def _is_public_request(request: Request) -> bool:
    path = request.url.path
    if request.method == "GET" and path in PUBLIC_GET_PATHS:
        return True
    if request.method == "POST" and path in PUBLIC_POST_PATHS:
        return True
    if request.method == "GET" and any(
        path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + ".")
        for prefix in PUBLIC_GET_PREFIXES
    ):
        return True
    return False


def build_sso_login_url(base_url: str, username: str, next_path: str = "/") -> str:
    token = create_sso_token(username)
    return f"{base_url.rstrip('/')}/auth/sso?token={quote(token)}&next={quote(next_path)}"


def enrich_nav_with_sso(nav: dict[str, Any], request: Request, current: str) -> dict[str, Any]:
    if not auth_enabled():
        return nav
    user = get_request_user(request)
    if not user:
        return nav
    enriched = dict(nav)
    for key in ("logger", "realtime", "camera"):
        if key == current:
            continue
        base_url = str(enriched.get(key, "")).strip()
        if base_url:
            enriched[key] = build_sso_login_url(base_url, user)
    return enriched


def auth_template_context(request: Request | None) -> dict[str, Any]:
    user = get_request_user(request) if request is not None else None
    return {
        "auth_enabled": auth_enabled(),
        "auth_user": user or "",
    }


def _login_html(next_path: str, error: str = "") -> str:
    error_block = f'<div class="error">{error}</div>' if error else ""
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AFA Login</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #070b12;
      --panel: #101827;
      --line: #22324a;
      --text: #f6f8fc;
      --muted: #8fb2df;
      --accent: #5cc8ff;
      --bad: #ff8f8f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: radial-gradient(circle at top, #122038 0%, var(--bg) 55%);
      color: var(--text);
      font-family: Arial, sans-serif;
    }}
    .card {{
      width: min(420px, 92vw);
      padding: 28px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: linear-gradient(180deg, var(--panel), #0b111c);
      box-shadow: 0 18px 50px rgba(0,0,0,0.35);
    }}
    h1 {{ margin: 0 0 8px; font-size: 30px; }}
    p {{ margin: 0 0 22px; color: var(--muted); }}
    label {{ display: block; margin-bottom: 8px; color: var(--muted); font-size: 13px; }}
    input {{
      width: 100%;
      margin-bottom: 14px;
      padding: 12px 14px;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: #090f18;
      color: var(--text);
      font-size: 15px;
    }}
    button {{
      width: 100%;
      min-height: 44px;
      border: 1px solid #2f6c9b;
      border-radius: 8px;
      background: rgba(18, 58, 92, 0.95);
      color: #fff;
      font-size: 15px;
      font-weight: 700;
      cursor: pointer;
    }}
    .error {{
      margin-bottom: 14px;
      padding: 10px 12px;
      border-radius: 8px;
      background: rgba(127, 29, 29, 0.35);
      border: 1px solid rgba(248, 113, 113, 0.35);
      color: var(--bad);
      font-size: 14px;
    }}
  </style>
</head>
<body>
  <form class="card" id="loginForm">
    <h1>로그인</h1>
    <p>AFA 대시보드 접속</p>
    <div id="loginError" class="error" style="display:none;"></div>
    {error_block}
    <label for="username">아이디</label>
    <input id="username" name="username" autocomplete="username" required>
    <label for="password">비밀번호</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <input type="hidden" id="next" name="next" value="{quote(next_path, safe='/')}">
    <button type="submit">로그인</button>
  </form>
  <script>
    const form = document.getElementById('loginForm');
    const errorBox = document.getElementById('loginError');
    form.addEventListener('submit', async (event) => {{
      event.preventDefault();
      errorBox.style.display = 'none';
      const username = document.getElementById('username').value.trim();
      const password = document.getElementById('password').value;
      const next = document.getElementById('next').value || '/';
      try {{
        const res = await fetch('/api/auth/login', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          credentials: 'same-origin',
          cache: 'no-store',
          body: JSON.stringify({{ username, password, next }}),
        }});
        const data = await res.json().catch(() => ({{}}));
        if (!res.ok || !data.ok) {{
          errorBox.textContent = data.message || '로그인에 실패했습니다.';
          errorBox.style.display = 'block';
          return;
        }}
        window.location.replace(data.next || '/');
      }} catch (err) {{
        errorBox.textContent = '서버 연결에 실패했습니다.';
        errorBox.style.display = 'block';
      }}
    }});
  </script>
</body>
</html>"""


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not auth_enabled() or _is_public_request(request):
            return await call_next(request)

        user = get_request_user(request)
        if user:
            return await call_next(request)

        if _wants_html(request):
            next_path = request.url.path
            if request.url.query:
                next_path = f"{next_path}?{request.url.query}"
            safe_next = _safe_next_path(next_path)
            login_url = "/login" if safe_next == "/" else f"/login?next={quote(safe_next)}"
            return RedirectResponse(url=login_url, status_code=303)

        return JSONResponse(
            status_code=401,
            content={"ok": False, "message": "login required"},
        )


def _safe_next_path(value: str) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    path_only = value.split("?", 1)[0]
    blocked_prefixes = ("/api/", "/auth/", "/login")
    if path_only in {"/api/auth/login", "/api/auth/logout"}:
        return "/"
    if any(path_only.startswith(prefix) for prefix in blocked_prefixes):
        return "/"
    return value


def _set_session_cookie(response: RedirectResponse | JSONResponse, username: str) -> None:
    _clear_session_cookie(response)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=create_session_token(username),
        httponly=True,
        samesite="lax",
        max_age=_session_seconds(),
        path="/",
    )


def _clear_session_cookie(response: RedirectResponse | JSONResponse) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="lax",
    )


def install_auth(app: FastAPI) -> None:
    if not auth_enabled():
        return

    app.add_middleware(AuthMiddleware)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if get_request_user(request):
            return RedirectResponse(url=_safe_next_path(request.query_params.get("next", "/")), status_code=303)
        return HTMLResponse(_login_html(_safe_next_path(request.query_params.get("next", "/"))))

    @app.get("/api/auth/login")
    async def login_get_redirect():
        return RedirectResponse(url="/login", status_code=303)

    @app.post("/api/auth/login")
    async def login_submit(request: Request):
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                body = await request.json()
            except Exception:
                body = {}
            username = str(body.get("username", "")).strip()
            password = str(body.get("password", ""))
            next_path = _safe_next_path(str(body.get("next", "/")))
        else:
            form = await request.form()
            username = str(form.get("username", "")).strip()
            password = str(form.get("password", ""))
            next_path = _safe_next_path(str(form.get("next", "/")))

        if not credentials_valid(username, password):
            if "application/json" in content_type:
                return JSONResponse(status_code=401, content={"ok": False, "message": "아이디 또는 비밀번호가 올바르지 않습니다."})
            return HTMLResponse(_login_html(next_path, "아이디 또는 비밀번호가 올바르지 않습니다."), status_code=401)

        if "application/json" in content_type:
            response: RedirectResponse | JSONResponse = JSONResponse({"ok": True, "next": next_path})
        else:
            response = RedirectResponse(url=next_path, status_code=303)
        _set_session_cookie(response, username)
        return response

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        wants_json = "application/json" in request.headers.get("accept", "")
        if wants_json:
            response: RedirectResponse | JSONResponse = JSONResponse({"ok": True})
        else:
            response = RedirectResponse(url="/login", status_code=303)
        _clear_session_cookie(response)
        return response

    @app.get("/api/auth/status")
    async def auth_status(request: Request) -> dict[str, Any]:
        user = get_request_user(request)
        return {"ok": True, "enabled": True, "authenticated": bool(user), "user": user or ""}

    @app.get("/auth/sso")
    async def auth_sso(request: Request):
        token = request.query_params.get("token", "")
        next_path = _safe_next_path(request.query_params.get("next", "/"))
        user = verify_sso_token(token)
        if not user:
            return RedirectResponse(url=f"/login?next={quote(next_path)}", status_code=303)
        response = RedirectResponse(url=next_path, status_code=303)
        _set_session_cookie(response, user)
        return response
