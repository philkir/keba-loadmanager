import { createRemoteJWKSet, jwtVerify } from 'jose';

function json(detail: string, status: number) {
  return Response.json({detail}, {status, headers:{'Cache-Control':'no-store'}});
}

async function limitedBody(request: Request): Promise<ArrayBuffer> {
  const reader=request.body?.getReader();
  if (!reader) return new ArrayBuffer(0);
  const chunks: Uint8Array[]=[];
  let size=0;
  while (true) {
    const {done,value}=await reader.read();
    if (done) break;
    size+=value.byteLength;
    if (size>16384) { await reader.cancel(); throw new Error('body_limit'); }
    chunks.push(value);
  }
  const body=new Uint8Array(size);
  let offset=0;
  for (const chunk of chunks) {body.set(chunk,offset);offset+=chunk.length;}
  return body.buffer;
}

export default {
  async fetch(request, env): Promise<Response> {
    const url=new URL(request.url);
    const local=String(env.LOCAL_DEV)==='true' && ['localhost','127.0.0.1'].includes(url.hostname);
    if (!local && url.pathname.startsWith('/api/')) {
      if (!env.ACCESS_TEAM_DOMAIN || !env.ACCESS_AUD) return json('Cloudflare Access noch nicht konfiguriert.',503);
      const assertion=request.headers.get('Cf-Access-Jwt-Assertion');
      if (!assertion) return json('Anmeldung erforderlich.',401);
      try {
        const issuer=`https://${env.ACCESS_TEAM_DOMAIN}`;
        if (!/^[a-z0-9-]+\.cloudflareaccess\.com$/.test(env.ACCESS_TEAM_DOMAIN)) return json('Access-Konfiguration ungültig.',503);
        await jwtVerify(assertion,createRemoteJWKSet(new URL(`${issuer}/cdn-cgi/access/certs`)), {issuer,audience:env.ACCESS_AUD});
      } catch {return json('Sitzung ungültig oder abgelaufen.',401);}
    }
    if (!url.pathname.startsWith('/api/')) {
      const assets=await env.ASSETS.fetch(request);
      const response=new Response(assets.body,assets);
      response.headers.set('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'");
      response.headers.set('X-Content-Type-Options','nosniff');
      response.headers.set('Referrer-Policy','same-origin');
      // Access-protected HTML must not be served from a shared cache.
      response.headers.set('Cache-Control','private, no-cache');
      return response;
    }
    const path=url.pathname.slice(4);
    if (!(['GET'].includes(request.method) && ['/state','/history','/events'].includes(path)) &&
        !(request.method==='POST' && path==='/commands')) return json('Nicht gefunden.',404);
    if (request.method==='POST') {
      if (request.headers.get('Origin')!==url.origin) return json('Ungültiger Ursprung.',403);
      if (request.headers.get('Content-Type')?.split(';')[0]!=='application/json') return json('JSON erforderlich.',415);
    }
    if (!env.BACKEND_ORIGIN || !env.BACKEND_TOKEN) return json('Lokales Backend noch nicht verbunden.',503);
    let target: URL;
    try {target=new URL(env.BACKEND_ORIGIN);} catch {return json('Backend-Konfiguration ungültig.',503);}
    if (target.username || target.password || (!local && target.protocol!=='https:')) return json('HTTPS-Backend erforderlich.',503);
    target.pathname=`/api${path}`;target.search='';target.hash='';
    const headers=new Headers({'Authorization':`Bearer ${env.BACKEND_TOKEN}`,'Content-Type':'application/json'});
    if (!local) {
      if (!env.CF_ACCESS_CLIENT_ID || !env.CF_ACCESS_CLIENT_SECRET) return json('Tunnel-Zugang noch nicht konfiguriert.',503);
      headers.set('CF-Access-Client-Id',env.CF_ACCESS_CLIENT_ID);
      headers.set('CF-Access-Client-Secret',env.CF_ACCESS_CLIENT_SECRET);
    }
    try {
      const body=request.method==='POST'?await limitedBody(request):undefined;
      const upstream=await fetch(target, {method:request.method,headers,body,redirect:'manual',signal:AbortSignal.timeout(4500)});
      if (upstream.status>=300 && upstream.status<400) return json('Backend-Anmeldung nicht eingerichtet.',502);
      if (!upstream.headers.get('Content-Type')?.includes('application/json')) return json('Unerwartete Backend-Antwort.',502);
      return new Response(upstream.body,{status:upstream.status,headers:{'Content-Type':'application/json','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});
    } catch (error) {
      if (error instanceof Error && error.message==='body_limit') return json('Anfrage zu groß.',413);
      console.error(JSON.stringify({event:'backend_unavailable',path}));
      return json('Lokale Instanz nicht erreichbar. Änderungen wurden nicht bestätigt.',503);
    }
  },
} satisfies ExportedHandler<Env>;
