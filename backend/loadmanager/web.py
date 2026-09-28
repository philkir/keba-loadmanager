"""Local web UI and same-container API gateway."""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .auth import AuthInputError, AuthStore, PasswordHashError, SESSION_SECONDS, SetupAlreadyCompleted


STATIC_DIR = Path(os.getenv('STATIC_DIR', '/app/static'))
ALLOWED = {('GET', 'state'), ('GET', 'history'), ('GET', 'events'), ('POST', 'commands')}


def allowed_origins(request: Request):
    configured = {value.strip().rstrip('/') for value in os.getenv('WEB_ALLOWED_ORIGINS', '').split(',') if value.strip()}
    return configured | {str(request.base_url).rstrip('/')}


def request_user(request: Request):
    return request.app.state.auth.session_user(request.cookies.get('keba_session'))


def origin_ok(request: Request):
    return request.headers.get('origin') in ({None} | allowed_origins(request))


def client_key(request: Request):
    return request.headers.get('cf-connecting-ip') or (request.client.host if request.client else 'unknown')


def with_session(response, token):
    response.set_cookie('keba_session', token, max_age=SESSION_SECONDS, httponly=True,
                        secure=True, samesite='strict', path='/')
    return response


@asynccontextmanager
async def lifespan(app):
    token = os.getenv('API_TOKEN', '')
    if len(token) < 24:
        raise RuntimeError('API_TOKEN mit mindestens 24 Zeichen erforderlich.')
    async with httpx.AsyncClient(
        base_url=os.getenv('LOCAL_API_URL', 'http://127.0.0.1:8000'),
        headers={'Authorization': 'Bearer ' + token},
        timeout=5,
        trust_env=False,
    ) as client:
        app.state.client = client
        app.state.auth = AuthStore(os.getenv('DB_PATH', '/var/lib/keba/loadmanager.sqlite'))
        yield


app = FastAPI(title='KEBA Load Manager · Web', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(PasswordHashError)
async def password_hash_failed(request: Request, exc: PasswordHashError):
    logging.getLogger(__name__).error('Password hashing failed on the server.', exc_info=exc)
    return JSONResponse({'detail': 'Passwortverarbeitung auf dem Server fehlgeschlagen. Bitte später erneut versuchen.'}, status_code=503)


async def auth_body(request: Request):
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(422, 'Ungültige JSON-Anfrage.') from exc
    if not isinstance(body, dict):
        raise HTTPException(422, 'JSON-Objekt erforderlich.')
    return body


@app.middleware('http')
async def security_headers(request: Request, call_next):
    if request.url.path.startswith('/api/'):
        user = request_user(request)
        if not user:
            return JSONResponse({'detail': 'Anmeldung erforderlich.'}, status_code=401)
        if request.method == 'POST' and user['role'] == 'viewer':
            return JSONResponse({'detail': 'Diese Rolle darf keine Änderungen vornehmen.'}, status_code=403)
        request.state.user = user
    response = await call_next(request)
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Cache-Control'] = 'no-store' if request.url.path.startswith(('/api/', '/auth/')) else 'private, no-cache'
    return response


@app.get('/healthz')
async def healthz():
    try:
        response = await app.state.client.get('/healthz')
        if response.status_code == 200:
            return {'status': 'ok'}
    except httpx.RequestError:
        pass
    return JSONResponse({'status': 'unavailable'}, status_code=503)


@app.get('/auth/status')
async def auth_status(request: Request):
    return {'setup_required': request.app.state.auth.setup_required(), 'user': request_user(request)}


@app.post('/auth/setup')
async def auth_setup(request: Request):
    if not origin_ok(request):
        return JSONResponse({'detail': 'Ungültiger Ursprung.'}, status_code=403)
    try:
        body = await auth_body(request)
        request.app.state.auth.create_user(body.get('username'), body.get('password'), initial=True)
        user, token = request.app.state.auth.login(body.get('username'), body.get('password'))
        return with_session(JSONResponse({'user': user}, status_code=201), token)
    except SetupAlreadyCompleted as exc:
        return JSONResponse({'detail': str(exc)}, status_code=409)
    except AuthInputError as exc:
        return JSONResponse({'detail': str(exc)}, status_code=422)


@app.post('/auth/login')
async def auth_login(request: Request):
    if not origin_ok(request):
        return JSONResponse({'detail': 'Ungültiger Ursprung.'}, status_code=403)
    key = client_key(request)
    if not request.app.state.auth.login_allowed(key):
        return JSONResponse({'detail': 'Zu viele Anmeldeversuche. Bitte in 15 Minuten erneut versuchen.'}, status_code=429)
    body = await auth_body(request)
    user, token = request.app.state.auth.login(body.get('username', ''), body.get('password', ''))
    if not user:
        request.app.state.auth.failed_login(key)
        return JSONResponse({'detail': 'Benutzername oder Passwort ist ungültig.'}, status_code=401)
    return with_session(JSONResponse({'user': user}), token)


@app.post('/auth/logout')
async def auth_logout(request: Request):
    if not origin_ok(request):
        return JSONResponse({'detail': 'Ungültiger Ursprung.'}, status_code=403)
    request.app.state.auth.logout(request.cookies.get('keba_session'))
    response = JSONResponse({'ok': True})
    response.delete_cookie('keba_session', path='/')
    return response


def is_admin(request: Request):
    user = request_user(request)
    return user if user and user['role'] == 'admin' else None


@app.get('/auth/users')
async def auth_users(request: Request):
    if not is_admin(request):
        return JSONResponse({'detail': 'Administratorrechte erforderlich.'}, status_code=403)
    return {'users': request.app.state.auth.users()}


@app.post('/auth/users')
async def auth_create_user(request: Request):
    if not origin_ok(request):
        return JSONResponse({'detail': 'Ungültiger Ursprung.'}, status_code=403)
    if not is_admin(request):
        return JSONResponse({'detail': 'Administratorrechte erforderlich.'}, status_code=403)
    try:
        body = await auth_body(request)
        user = request.app.state.auth.create_user(body.get('username'), body.get('password'), body.get('role', 'operator'))
        return JSONResponse({'user': user}, status_code=201)
    except AuthInputError as exc:
        return JSONResponse({'detail': str(exc)}, status_code=422)


@app.api_route('/api/{path:path}', methods=['GET', 'POST'])
async def proxy(path: str, request: Request):
    if (request.method, path) not in ALLOWED:
        return JSONResponse({'detail': 'Nicht gefunden.'}, status_code=404)
    if request.method == 'POST':
        if request.headers.get('content-type', '').split(';')[0] != 'application/json':
            return JSONResponse({'detail': 'JSON erforderlich.'}, status_code=415)
        if request.headers.get('origin') not in ({None} | allowed_origins(request)):
            return JSONResponse({'detail': 'Ungültiger Ursprung.'}, status_code=403)
        body = await request.body()
        if len(body) > 16384:
            return JSONResponse({'detail': 'Anfrage zu groß.'}, status_code=413)
        if path == 'commands':
            payload = await auth_body(request)
            if payload.get('kind') == 'local_access' and request.state.user['role'] != 'admin':
                return JSONResponse({'detail': 'KEBA-Zugänge dürfen nur Administratoren hinterlegen.'}, status_code=403)
    else:
        body = b''
    try:
        upstream = await app.state.client.request(
            request.method,
            '/api/' + path,
            content=body,
            headers={'content-type': 'application/json'},
        )
        return Response(upstream.content, status_code=upstream.status_code, media_type='application/json')
    except httpx.RequestError:
        return JSONResponse({'detail': 'Lokale Instanz nicht erreichbar.'}, status_code=503)


if not STATIC_DIR.is_dir():
    raise RuntimeError(f'Frontend-Verzeichnis fehlt: {STATIC_DIR}')
app.mount('/', StaticFiles(directory=STATIC_DIR, html=True), name='frontend')
