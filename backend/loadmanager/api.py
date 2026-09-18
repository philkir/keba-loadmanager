"""Public API process; never communicates with Modbus devices."""
import hmac
import os
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response


@asynccontextmanager
async def lifespan(app):
    if min(len(os.getenv('API_TOKEN','')), len(os.getenv('CONTROLLER_TOKEN',''))) < 24:
        raise RuntimeError('API_TOKEN und CONTROLLER_TOKEN erforderlich; scripts/dev.py erzeugt lokale Secrets.')
    async with httpx.AsyncClient(base_url=os.getenv('CONTROLLER_URL','http://127.0.0.1:8091'),
        headers={'Authorization':'Bearer '+os.environ['CONTROLLER_TOKEN']}, timeout=3, trust_env=False) as client:
        app.state.client = client
        yield


app = FastAPI(title='KEBA Load Manager',version='0.2.0',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)


@app.middleware('http')
async def authenticate(request: Request, call_next):
    if request.method == 'GET' and request.url.path == '/healthz':
        return await call_next(request)
    token = os.getenv('API_TOKEN','')
    if len(token)<24 or not hmac.compare_digest(request.headers.get('authorization',''), 'Bearer '+token):
        return JSONResponse({'detail':'Nicht autorisiert.'},status_code=401)
    if request.method not in ['GET','POST']:
        return JSONResponse({'detail':'Methode nicht erlaubt.'}, status_code=405)
    if request.method == 'POST':
        if request.headers.get('content-type','').split(';')[0] != 'application/json':
            return JSONResponse({'detail':'JSON erforderlich.'},status_code=415)
        size=0
        chunks=[]
        async for chunk in request.stream():
            size += len(chunk)
            if size>16384: return JSONResponse({'detail':'Anfrage zu groß.'},status_code=413)
            chunks.append(chunk)
        request._body=b''.join(chunks)
    response=await call_next(request)
    response.headers['Cache-Control']='no-store'
    return response


@app.get('/healthz')
async def healthz():
    try:
        response = await app.state.client.get('/healthz')
        if response.status_code == 200:
            return {'status':'ok'}
    except httpx.RequestError:
        pass
    return JSONResponse({'status':'unavailable'}, status_code=503)


@app.api_route('/api/{path:path}',methods=['GET','POST'])
async def proxy(path: str, request: Request):
    if (request.method,path) not in [('GET','state'),('GET','history'),('GET','events'),('POST','commands')]:
        return JSONResponse({'detail':'Nicht gefunden.'},status_code=404)
    try:
        response=await app.state.client.request(request.method,'/'+path,content=await request.body(),headers={'content-type':'application/json'})
        return Response(response.content,status_code=response.status_code,media_type='application/json')
    except httpx.RequestError:
        return JSONResponse({'detail':'Lokaler Regler nicht erreichbar. Befehl wurde nicht bestätigt.'},status_code=503)
