# KEBA Load Manager als Docker-Installation

Das Image enthält die React-Oberfläche, das FastAPI-Gateway, den internen Regler und den Modbus-Client. SQLite liegt in einem Docker-Volume. `cloudflared` läuft als zweiter Container, damit Backend-Updates und Tunnel-Updates unabhängig bleiben.

## Voraussetzungen

- Docker Engine mit Docker Compose v2 auf Linux, macOS oder Windows
- Das Gerät muss die KEBA-Adressen `192.168.1.71` bis `192.168.1.74` über TCP-Port 502 erreichen
- Für Cloudflare Tunnel: ausgehendes TCP/UDP auf Port 7844

Die Stationsdaten stehen in `deploy/config/stations.json`. Das Image läuft als Benutzer `10001`, besitzt keine Linux-Capabilities und schreibt ausschließlich in das Volume `/var/lib/keba` und nach `/tmp`.

Neue Konfigurationen starten mit `MODE=active`. Dabei schreibt der Regler die berechneten Stromlimits per Modbus und verwendet ohne Gebäudenzähler die in der Oberfläche konfigurierte Fallback-Gebäudelast (Standard: 8 kW). Dieser Wert muss mindestens so hoch wie der maximal gleichzeitig zu erwartende Gebäudeverbrauch gewählt werden. `MODE=commissioning` hält Modbus im Nur-Lese-Modus.

Die mitgelieferte Installation ist für 32 A beziehungsweise 22 kW je Ladepunkt konfiguriert. Der tatsächlich freigegebene Strom ist stets das Minimum aus Installationslimit, von der Wallbox gemeldetem Hardwarelimit und dem verfügbaren Standortbudget. Nimmt ein Fahrzeug seine Freigabe stabil nicht vollständig ab, gibt der Regler den ungenutzten Anteil nach einer Anlauf- und Hysteresezeit für andere Fahrzeuge frei.

## Lokal starten

Im Projektverzeichnis:

```bash
python3 deploy/init_config.py
docker compose up -d --build backend
docker compose ps
```

Bei einer bestehenden Installation muss der Modus einmalig in `deploy/backend.env` umgestellt werden:

```text
MODE=active
```

Anschließend `docker compose up -d --build --force-recreate backend` ausführen und den Fallback-Wert in **Einstellungen** prüfen.

Danach sind verfügbar:

- Oberfläche: `http://127.0.0.1:8080`
- geschützte API: `http://127.0.0.1:8000`
- Healthcheck: `http://127.0.0.1:8080/healthz`

Andere Host-Ports können beim Start gesetzt werden:

```bash
KEBA_WEB_PORT=9080 KEBA_BACKEND_PORT=9000 docker compose up -d backend
```

Logs und Aktualisierung:

```bash
docker compose logs -f backend
docker compose build --pull backend
docker compose up -d backend
```

Die Datei `deploy/backend.env` wird mit Dateirechten `0600` erzeugt und enthält die beiden internen Tokens. Sie darf nicht in Git eingecheckt oder weitergegeben werden. Die bestehende lokale `.env` wird übernommen, damit der bereits konfigurierte Cloudflare Worker denselben `API_TOKEN` behält.

## Cloudflare Tunnel einrichten

Die Compose-Datei verwendet das offizielle Image `cloudflare/cloudflared:2026.9.1`. Der Tunnel ist als Profil definiert und startet erst, wenn ein Token vorhanden ist.

1. In Cloudflare Zero Trust unter **Networks → Tunnels** einen remotely-managed Tunnel anlegen.
2. Im Tunnel **Add a replica** öffnen und aus dem angezeigten Befehl nur den langen Tunnel-Token kopieren.
3. Den Token ohne zusätzliche Zeichen in `deploy/secrets/cloudflared-token.txt` speichern und die Datei schützen:

   ```bash
   cp deploy/secrets/cloudflared-token.txt.example deploy/secrets/cloudflared-token.txt
   chmod 600 deploy/secrets/cloudflared-token.txt
   ```

4. Im Tunnel eine veröffentlichte Anwendung anlegen. Es gibt zwei sinnvolle Varianten:

   | Zweck | Öffentlicher Hostname | Service im Tunnel |
   | --- | --- | --- |
   | Bestehender Cloudflare Worker bleibt das Frontend | z. B. `keba-api.example.com` | `http://backend:8000` |
   | Das Frontend aus diesem Container wird direkt veröffentlicht | z. B. `ladepark.example.com` | `http://backend:8080` |

5. Den Tunnel starten:

   ```bash
   docker compose --profile tunnel up -d
   docker compose --profile tunnel ps
   docker compose logs -f cloudflared
   ```

Bei der zweiten Variante muss in `deploy/backend.env` zusätzlich der exakte HTTPS-Ursprung stehen:

```dotenv
WEB_ALLOWED_ORIGINS=https://ladepark.example.com
```

Danach `docker compose up -d --force-recreate backend` ausführen. Der öffentliche Hostname sollte mit einer Cloudflare-Access-Anwendung und einer Benutzerregel geschützt werden.

## Bestehenden Worker mit dem Tunnel verbinden

Für die empfohlene erste Variante:

1. Den API-Hostnamen in Cloudflare Access als **Self-hosted application** schützen.
2. Eine **Service Auth**-Regel für einen eigenen Service Token erstellen.
3. Beim Worker `BACKEND_ORIGIN` auf den HTTPS-API-Hostnamen setzen.
4. `BACKEND_TOKEN` muss dem `API_TOKEN` aus `deploy/backend.env` entsprechen.
5. Service-Token-ID und -Secret als `CF_ACCESS_CLIENT_ID` und `CF_ACCESS_CLIENT_SECRET` beim Worker speichern.

Secrets interaktiv setzen, damit sie nicht im Shell-Verlauf stehen:

```bash
npx wrangler secret put BACKEND_TOKEN
npx wrangler secret put CF_ACCESS_CLIENT_ID
npx wrangler secret put CF_ACCESS_CLIENT_SECRET
```

`BACKEND_ORIGIN` wird in `wrangler.jsonc` oder als Worker-Variable auf `https://keba-api.example.com` gesetzt. Anschließend den Worker neu deployen.

## Daten sichern und auf ein anderes Gerät umziehen

Vor einem konsistenten SQLite-Backup den Backend-Container kurz stoppen:

```bash
docker compose stop backend
docker run --rm -v keba-loadmanager_keba-data:/data -v "$PWD:/backup" alpine \
  tar -czf /backup/keba-data-backup.tar.gz -C /data .
docker compose start backend
```

Für ein anderes Gerät mit Internetzugang genügt es, den Projektordner zu kopieren und dort `python3 deploy/init_config.py` sowie `docker compose up -d --build backend` auszuführen. Docker lädt automatisch die zum Gerät passende `amd64`- oder `arm64`-Basis. Das lokal gebaute Komplett-Image ist `keba-loadmanager:0.2.0`.

Ein Offline-Paket für dieselbe Prozessorarchitektur kann so erzeugt werden:

```bash
docker save -o keba-loadmanager-images.tar \
  keba-loadmanager:0.2.0 cloudflare/cloudflared:2026.9.1
```

Auf dem Zielgerät:

```bash
docker load -i keba-loadmanager-images.tar
docker compose --profile tunnel up -d
```

## Wichtiger Betriebsstand

Der Modus `active` schreibt die berechneten Leistungsfreigaben über Modbus TCP. Ohne Gebäudemessung ist die Einhaltung der realen Anschlussgrenze nur so konservativ wie der eingestellte Fallback-Wert. Der Modus `commissioning` bleibt als vollständig schreibgeschützter Diagnosemodus erhalten. Monta und OCPP bleiben davon unabhängig.
