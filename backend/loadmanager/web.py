"""Local web UI and same-container API gateway."""
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles


STATIC_DIR = Path(os.getenv('STATIC_DIR', '/app/static'))
ALLOWED = {('GET', 'state'), ('GET', 'history'), ('GET', 'events'), ('POST', 'commands')}


def allowed_origins(request: Request):
    configured = {value.strip().rstrip('/') for value in os.getenv('WEB_ALLOWED_ORIGINS', '').split(',') if value.strip()}
    return configured | {str(request.base_url).rstrip('/')}


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
        yield


app = FastAPI(title='KEBA Load Manager · Web', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware('http')
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Cache-Control'] = 'no-store' if request.url.path.startswith('/api/') else 'private, no-cache'
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
