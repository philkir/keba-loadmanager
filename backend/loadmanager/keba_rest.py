"""Local session authorization, separate from the Modbus current regulator.

API paths are advertised by the installed firmware's /openapi.json. No OCPP,
RFID, load-management or authorization configuration is changed here.
"""
from urllib.parse import quote

import httpx


class LocalStartError(Exception):
    pass


class LocalStartUncertain(LocalStartError):
    pass


class KebaRest:
    def __init__(self, station, credentials):
        self.station = station
        self.credentials = credentials

    async def start(self, serial):
        # KEBA ships a self-signed local HTTPS certificate. Destinations come
        # exclusively from the validated private station IP, never request URLs.
        async with httpx.AsyncClient(base_url=f"https://{self.station['host']}:8443",
                                     verify=False, trust_env=False, timeout=5,
                                     follow_redirects=False) as client:
            try:
                login = await client.post('/v2/jwt/login', json={
                    key: self.credentials[key] for key in ('username', 'password')})
                if login.status_code in (401, 403):
                    raise LocalStartError('KEBA-Zugang abgelehnt. Zugangsdaten am Ladepunkt prüfen.')
                login.raise_for_status()
                token = login.json().get('accessToken')
                if not isinstance(token, str) or not token:
                    raise LocalStartError('KEBA-Anmeldung wurde nicht bestätigt.')
                client.headers['Authorization'] = 'Bearer ' + token
                specification = await client.get('/openapi.json')
                specification.raise_for_status()
                paths = specification.json().get('paths', {})
                candidates = [f'/v2/wallboxes/{{serialNumber}}/{suffix}'
                              for suffix in ('start-charging', 'startCharging')]
                path = next((p for p in candidates if 'post' in paths.get(p, {})), None)
                if not path:
                    raise LocalStartError('Diese KEBA-Firmware bietet keinen unterstützten lokalen Ladestart an.')
                wallboxes = await client.get('/v2/wallboxes')
                wallboxes.raise_for_status()
                entries = wallboxes.json().get('wallboxes', [])
                match = [entry for entry in entries if str(entry.get('serialNumber')) == str(serial)]
                if len(match) != 1:
                    raise LocalStartError('Seriennummer der lokalen KEBA-API stimmt nicht mit dem Ladepunkt überein.')
            except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
                raise LocalStartError('Lokale KEBA-API nicht erreichbar oder Antwort ungültig.') from exc

            # Never retry this mutation: a lost response can still mean success.
            try:
                response = await client.post(path.replace('{serialNumber}', quote(str(serial), safe='')))
                if response.status_code in (401, 403):
                    raise LocalStartError('KEBA erlaubt mit diesem Zugang keinen lokalen Ladestart.')
                if 400 <= response.status_code < 500:
                    raise LocalStartError(f'KEBA hat den lokalen Ladestart abgelehnt (HTTP {response.status_code}).')
                response.raise_for_status()
                body = response.json()
                if body.get('acceptStatus') != 'ACCEPTED':
                    raise LocalStartError('KEBA hat den lokalen Ladestart nicht akzeptiert. Autorisierung an der Wallbox prüfen.')
            except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
                raise LocalStartUncertain('Keine eindeutige KEBA-Startbestätigung. Ladezustand vor einem erneuten Versuch prüfen.') from exc
