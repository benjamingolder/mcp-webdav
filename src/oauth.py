"""
Minimaler OAuth 2.0 Authorization Code + PKCE Server für Claude.ai MCP Integration.
Authentifizierung via einfaches Passwort (MCP_AUTH_PASSWORD).
"""
import hashlib
import base64
import os
import secrets

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.middleware.base import BaseHTTPMiddleware

AUTH_PASSWORD = os.environ.get("MCP_AUTH_PASSWORD", "")

# In-Memory Stores (reichen für Single-User-Betrieb)
_auth_codes: dict[str, dict] = {}   # code -> {redirect_uri, code_challenge}
_valid_tokens: set[str] = set()


def _base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host  = request.headers.get("x-forwarded-host", request.url.netloc)
    return f"{proto}://{host}"


def _login_page(error: bool = False) -> str:
    err = '<p style="color:#c0392b;margin:0 0 1rem">Falsches Passwort – bitte erneut versuchen.</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>JuventusSchulen MCP – Anmeldung</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: system-ui, sans-serif; background: #f0f2f5;
           display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
    .card {{ background: #fff; border-radius: 12px; box-shadow: 0 4px 24px rgba(0,0,0,.08);
             padding: 2.5rem 2rem; width: 100%; max-width: 360px; }}
    h1 {{ margin: 0 0 .25rem; font-size: 1.3rem; color: #1a1a2e; }}
    p.sub {{ margin: 0 0 1.5rem; color: #666; font-size: .9rem; }}
    label {{ display: block; font-size: .875rem; font-weight: 500; color: #333; margin-bottom: .25rem; }}
    input[type=password] {{ width: 100%; padding: .6rem .75rem; border: 1px solid #d1d5db;
                            border-radius: 6px; font-size: 1rem; outline: none; }}
    input[type=password]:focus {{ border-color: #2563eb; box-shadow: 0 0 0 3px rgba(37,99,235,.15); }}
    button {{ margin-top: 1.25rem; width: 100%; padding: .75rem; background: #2563eb; color: #fff;
              border: none; border-radius: 6px; font-size: 1rem; cursor: pointer; font-weight: 500; }}
    button:hover {{ background: #1d4ed8; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>JuventusSchulen MCP</h1>
    <p class="sub">Bitte melde dich an um Claude Zugriff zu geben.</p>
    {err}
    <form method="post">
      <label for="pw">Passwort</label>
      <input id="pw" type="password" name="password" autofocus autocomplete="current-password">
      <button type="submit">Anmelden &amp; Zugriff erlauben</button>
    </form>
  </div>
</body>
</html>"""


# ── OAuth Endpoints ────────────────────────────────────────────────────────────

async def oauth_protected_resource(request: Request) -> JSONResponse:
    base = _base_url(request)
    return JSONResponse({
        "resource": base,
        "authorization_servers": [base],
    })


async def oauth_metadata(request: Request) -> JSONResponse:
    base = _base_url(request)
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    })


async def authorize(request: Request) -> Response:
    params       = dict(request.query_params)
    redirect_uri = params.get("redirect_uri", "")
    state        = params.get("state", "")
    code_challenge = params.get("code_challenge", "")

    if request.method == "POST":
        form     = await request.form()
        password = str(form.get("password", ""))

        if AUTH_PASSWORD and password == AUTH_PASSWORD:
            code = secrets.token_urlsafe(32)
            _auth_codes[code] = {
                "redirect_uri":   redirect_uri,
                "code_challenge": code_challenge,
            }
            sep = "&" if "?" in redirect_uri else "?"
            return RedirectResponse(
                f"{redirect_uri}{sep}code={code}&state={state}",
                status_code=302,
            )
        return HTMLResponse(_login_page(error=True), status_code=401)

    return HTMLResponse(_login_page())


async def token(request: Request) -> JSONResponse:
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)

    code          = str(body.get("code", ""))
    code_verifier = str(body.get("code_verifier", ""))

    if code not in _auth_codes:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    stored = _auth_codes.pop(code)

    # PKCE Validierung
    if stored.get("code_challenge") and code_verifier:
        digest    = hashlib.sha256(code_verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if challenge != stored["code_challenge"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

    access_token = secrets.token_urlsafe(32)
    _valid_tokens.add(access_token)

    return JSONResponse({
        "access_token": access_token,
        "token_type":   "bearer",
        "expires_in":   86400,
    })


# ── Bearer Token Middleware ────────────────────────────────────────────────────

_OPEN_PATHS = {
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server",
    "/authorize",
    "/token",
}


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in _OPEN_PATHS:
            return await call_next(request)

        # Kein Passwort gesetzt → keine Auth nötig
        if not AUTH_PASSWORD:
            return await call_next(request)

        auth  = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip()

        if token not in _valid_tokens:
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="JuventusSchulen MCP"'},
            )

        return await call_next(request)


OAUTH_ROUTES = [
    Route("/.well-known/oauth-protected-resource", oauth_protected_resource, methods=["GET"]),
    Route("/.well-known/oauth-authorization-server", oauth_metadata, methods=["GET"]),
    Route("/authorize", authorize, methods=["GET", "POST"]),
    Route("/token",     token,     methods=["POST"]),
]
