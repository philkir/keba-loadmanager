"""Single-owner simulation controller; runs separately from the public API."""
import asyncio
import hmac
import math
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import ValidationError

from .allocator import allocate
from .modbus import KebaModbus
from .models import Settings, Simulation, StationPatch, StationSetting, station_defaults_from_file
from .storage import Store


class Controller:
    def __init__(self, store):
        self.store = store
        self.settings = Settings.model_validate(store.get('settings', {}))
        configured = station_defaults_from_file(os.getenv('STATIONS_CONFIG_PATH'))
        defaults = {s['id']: s for s in configured}
        stored = store.get('stations', configured)
        self.station_config = [StationSetting.model_validate({**defaults.get(s.get('id'), {}), **s}).model_dump() for s in stored]
        for station in self.station_config:
            station['max_current_a'] = defaults.get(station['id'], station)['max_current_a']
        self.sim = Simulation()  # Fault injection is deliberately reset after a restart.
        self.state = None
        self.last_sample = 0
        self.last_meter = time.time()
        self.session_kwh = {s['id']: 0.0 for s in self.station_config}
        self.last_tick = time.monotonic()
        self.last_condition = None
        self.revision = int(store.get('revision', 0))
        self.lock = asyncio.Lock()

    async def tick(self):
        now, mono = time.time(), time.monotonic()
        elapsed = min(3, max(0, mono-self.last_tick))
        self.last_tick = mono
        fractions = [1/3]*3 if self.sim.profile == 'balanced' else [0.6, 0.25, 0.15]
        building_a = [self.sim.building_kw * 1000*f/230 for f in fractions]
        stations = [{**s, 'connected': s['id'] not in self.sim.disconnected,
                     'online': s['id'] not in self.sim.offline, 'phases': [0,1,2]} for s in self.station_config]
        fault = (not self.sim.meter_online) or any(not s['online'] for s in stations)
        if self.sim.meter_online:
            self.last_meter = now
        grants = allocate(stations, building_a, self.sim.building_kw, self.settings, mono)
        if fault:
            grants = dict.fromkeys(grants, 0)
        for s in stations:
            amps = grants[s['id']]
            s['current_a'] = amps
            s['power_kw'] = round(amps*690/1000, 3)
            self.session_kwh[s['id']] += s['power_kw']*elapsed/3600
            s['session_kwh'] = round(self.session_kwh[s['id']], 3)
            s['status'] = ('offline' if not s['online'] else 'available' if not s['connected'] else
                           'paused' if s['paused'] or self.settings.paused else 'safe' if fault else
                           'charging' if amps else 'waiting')
        charging = sum(s['power_kw'] for s in stations)
        total = self.sim.building_kw+charging
        phases = [round(b+sum(grants[s['id']] for s in stations if p in s['phases']), 2) for p,b in enumerate(building_a)]
        condition = 'safe' if fault else 'paused' if self.settings.paused else 'active'
        if condition != self.last_condition:
            message = {'safe':'Sicherer Halt: Messung oder Ladepunkt nicht erreichbar.', 'paused':'Alle Ladepunkte wurden pausiert.', 'active':'Lokale Regelung aktiv.'}[condition]
            await asyncio.to_thread(self.store.event, message, 'warning' if fault else 'info')
            self.last_condition = condition
        self.state = {
            'mode':'simulation', 'status':condition, 'timestamp':now, 'meter_timestamp':self.last_meter,
            'site_name': self.settings.site_name, 'settings':self.settings.model_dump(), 'stations':stations,
            'simulation':self.sim.model_dump(), 'building_kw':round(self.sim.building_kw,3),
            'charging_kw':round(charging,3), 'total_kw':round(total,3), 'phase_currents_a':phases,
            'headroom_kw':round(max(0,self.settings.power_limit_kw-self.settings.reserve_kw-total),3),
            'meter_online':self.sim.meter_online, 'revision':self.revision,
            'overload': total > self.settings.power_limit_kw or any(x > self.settings.phase_limit_a for x in phases),
            'monta_status':'external_unverified', 'commissioned':False,
        }
        if now-self.last_sample >= 5:
            # Meter outages are gaps, never fabricated historical measurements.
            if self.sim.meter_online:
                await asyncio.to_thread(self.store.sample, now, self.sim.building_kw, charging)
            self.last_sample = now

    async def run(self):
        while True:
            async with self.lock:
                await self.tick()
            await asyncio.sleep(1)

    async def command(self, body):
        try:
            command_id = str(uuid.UUID(body['id']))
            issued = float(body['issued_at'])
            if not time.time()-15 <= issued <= time.time()+3:
                raise HTTPException(409, 'Befehl abgelaufen. Bitte erneut ausführen.')
        except (KeyError, TypeError, ValueError):
            raise HTTPException(422, 'Ungültiger Befehl.')
        async with self.lock:
            previous = await asyncio.to_thread(self.store.previous, command_id)
            if previous:
                return previous
            kind, value = body.get('kind'), body.get('value')
            try:
                if kind == 'settings':
                    parsed = Settings.model_validate(value)
                    key, data = 'settings', parsed.model_dump()
                    message = 'Standorteinstellungen aktualisiert.'
                elif kind == 'station':
                    station_id = body.get('station_id')
                    if station_id not in [s['id'] for s in self.station_config]:
                        raise HTTPException(404, 'Ladepunkt nicht gefunden.')
                    patch = StationPatch.model_validate(value).model_dump(exclude_none=True)
                    key, data = 'stations', [{**s, **patch} if s['id'] == station_id else s.copy() for s in self.station_config]
                    message = f'Einstellungen für {station_id} aktualisiert.'
                elif kind == 'simulation':
                    parsed = Simulation.model_validate(value)
                    valid_ids = {s['id'] for s in self.station_config}
                    if not set(parsed.offline+parsed.disconnected) <= valid_ids:
                        raise HTTPException(422, 'Unbekannter Ladepunkt.')
                    key, data = 'last_simulation', parsed.model_dump()
                    message = 'Simulationsszenario geändert.'
                else:
                    raise HTTPException(422, 'Unbekannter Befehl.')
            except ValidationError as exc:
                raise HTTPException(422, 'Eingaben außerhalb der zulässigen Grenzen.') from exc
            result = {'ok':True, 'id':command_id, 'applied_at':time.time()}
            await asyncio.to_thread(self.store.commit_command, key, data, command_id, result, message)
            if kind == 'settings': self.settings = Settings.model_validate(data)
            elif kind == 'station': self.station_config = data
            else: self.sim = Simulation.model_validate(data)
            self.revision += 1
            await self.tick()
            return result


async def port_open(host, port):
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=0.8)
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


class CommissioningController:
    """Read-only live commissioning. It never writes a Modbus register."""
    def __init__(self, store):
        self.store = store
        self.settings = Settings.model_validate(store.get('settings', {}))
        configured = station_defaults_from_file(os.getenv('STATIONS_CONFIG_PATH'))
        defaults = {s['id']: s for s in configured}
        stored = store.get('stations', configured)
        self.station_config = [StationSetting.model_validate({**defaults.get(s.get('id'), {}), **s}).model_dump() for s in stored]
        for station in self.station_config:
            station['max_current_a'] = defaults.get(station['id'], station)['max_current_a']
        self.sim = Simulation(building_kw=0, meter_online=False, disconnected=[], offline=[])
        self.state = None
        self.revision = int(store.get('revision', 0))
        self.lock = asyncio.Lock()
        self.last_snapshot = None
        self.telemetry_cache = {}
        self.last_full_read = {}

    async def probe(self, setting):
        station = {**setting, 'connected':False, 'online':False, 'network_online':False,
                   'phases':[0,1,2], 'current_a':0, 'currents_a':[0,0,0], 'power_kw':0,
                   'session_kwh':0, 'energy_kwh':0, 'serial':None, 'firmware_raw':None,
                   'error_code':None, 'status':'offline', 'connection_detail':'IP-Adresse fehlt'}
        if not setting['host']:
            return station
        station['web_url'] = f"https://{setting['host']}:8443" if setting['model'] == 'P30 x' else f"https://{setting['host']}"
        station['network_online'] = await port_open(setting['host'], 443) or await port_open(setting['host'], 80)
        modbus_online = await port_open(setting['host'], setting['port'])
        station['network_online'] = station['network_online'] or modbus_online
        if not modbus_online:
            station['status'] = 'setup' if station['network_online'] else 'offline'
            station['connection_detail'] = ('Gerät erreichbar, Modbus TCP ist nicht aktiv'
                                            if station['network_online'] else 'Gerät im Netz nicht erreichbar')
            return station
        wallbox = KebaModbus(setting['host'], setting['model'], port=setting['port'],
                            device_id=setting['device_id'], writes_enabled=False,
                            max_current_a=setting['max_current_a'])
        try:
            await wallbox.connect()
            full = time.monotonic()-self.last_full_read.get(setting['id'], 0) >= 60
            data = await wallbox.telemetry(full=full)
            if full:
                self.telemetry_cache[setting['id']] = data.copy()
                self.last_full_read[setting['id']] = time.monotonic()
            else:
                data = {**self.telemetry_cache.get(setting['id'], {}), **data}
            station.update(data)
            station['installation_limit_a'] = setting['max_current_a']
            reported_limits = [value for value in (data.get('device_limit_a'), data.get('hardware_limit_a'))
                               if value is not None and value >= 6]
            station['max_current_a'] = min([setting['max_current_a'], *reported_limits])
            station['online'] = True
            station['connected'] = data['cable_state'] in (5, 7)
            station['current_a'] = round(max(data['currents_a']), 3)
            station['status'] = ('charging' if data['state'] == 3 else 'safe' if data['state'] == 4
                                 else 'paused' if data['state'] == 5 else 'waiting' if data['state'] == 2
                                 else 'available')
            station['connection_detail'] = 'Modbus TCP verbunden (nur Lesen)'
        except Exception:
            station['status'] = 'setup'
            station['connection_detail'] = 'Modbus TCP antwortet, Register konnten nicht gelesen werden'
        finally:
            wallbox.close()
        return station

    async def tick(self):
        now = time.time()
        stations = await asyncio.gather(*(self.probe(s) for s in self.station_config))
        charging = round(sum(s['power_kw'] for s in stations), 3)
        phases = [round(sum(s['currents_a'][phase] for s in stations), 3) for phase in range(3)]
        snapshot = tuple((s['id'], s['status'], s.get('serial')) for s in stations)
        if snapshot != self.last_snapshot:
            online = sum(s['online'] for s in stations)
            web = sum(s['network_online'] for s in stations)
            await asyncio.to_thread(self.store.event,
                f'Inbetriebnahme: {web} Geräte im Netz, {online} über Modbus lesbar.',
                'info' if online == len(stations) else 'warning')
            self.last_snapshot = snapshot
        self.state = {
            'mode':'commissioning', 'status':'commissioning', 'timestamp':now, 'meter_timestamp':0,
            'site_name':self.settings.site_name, 'settings':self.settings.model_dump(), 'stations':stations,
            'simulation':self.sim.model_dump(), 'building_kw':0, 'charging_kw':charging,
            'total_kw':charging, 'phase_currents_a':phases, 'headroom_kw':0, 'meter_online':False,
            'revision':self.revision, 'overload':False, 'monta_status':'external_unverified',
            'commissioned':False,
        }

    async def run(self):
        while True:
            async with self.lock:
                await self.tick()
            await asyncio.sleep(2)

    async def command(self, body):
        try:
            command_id = str(uuid.UUID(body['id']))
            issued = float(body['issued_at'])
            if not time.time()-15 <= issued <= time.time()+3:
                raise HTTPException(409, 'Befehl abgelaufen. Bitte erneut ausführen.')
        except (KeyError, TypeError, ValueError):
            raise HTTPException(422, 'Ungültiger Befehl.')
        async with self.lock:
            previous = await asyncio.to_thread(self.store.previous, command_id)
            if previous:
                return previous
            kind, value = body.get('kind'), body.get('value')
            try:
                if kind == 'settings':
                    parsed = Settings.model_validate(value)
                    key, data = 'settings', parsed.model_dump()
                    message = 'Standorteinstellungen aktualisiert.'
                elif kind == 'station':
                    station_id = body.get('station_id')
                    if station_id not in [s['id'] for s in self.station_config]:
                        raise HTTPException(404, 'Ladepunkt nicht gefunden.')
                    patch = StationPatch.model_validate(value).model_dump(exclude_none=True)
                    key, data = 'stations', [{**s, **patch} if s['id'] == station_id else s.copy() for s in self.station_config]
                    data = [StationSetting.model_validate(s).model_dump() for s in data]
                    message = f'Verbindungsdaten für {station_id} aktualisiert.'
                elif kind == 'start' and hasattr(self, 'manual_start_until'):
                    station_id = body.get('station_id')
                    if station_id not in [s['id'] for s in self.station_config]:
                        raise HTTPException(404, 'Ladepunkt nicht gefunden.')
                    key = 'stations'
                    data = [{**s, 'paused':False} if s['id'] == station_id else s.copy() for s in self.station_config]
                    message = f'Manuelle Ladeanforderung für {station_id} aktiviert.'
                else:
                    raise HTTPException(422, 'Im Inbetriebnahmemodus sind nur lokale Einstellungen erlaubt.')
            except ValidationError as exc:
                raise HTTPException(422, 'Eingaben außerhalb der zulässigen Grenzen.') from exc
            result = {'ok':True, 'id':command_id, 'applied_at':time.time()}
            await asyncio.to_thread(self.store.commit_command, key, data, command_id, result, message)
            if kind == 'settings': self.settings = Settings.model_validate(data)
            else:
                self.station_config = data
                if kind == 'start':
                    self.manual_start_until[station_id] = time.monotonic()+max(300, self.settings.rotation_seconds)
            self.revision += 1
            await self.tick()
            return result


class ActiveController(CommissioningController):
    """Live controller using a conservative configured load when no site meter exists."""
    def __init__(self, store):
        super().__init__(store)
        self.failsafe_ready = set()
        self.applied_limits = {}
        self.manual_start_until = {}
        self.limit_changed_at = {}
        self.underuse_since = {}
        self.last_condition = None
        self.last_sample = 0

    async def apply_limit(self, setting, amps):
        if setting['id'] in self.failsafe_ready and self.applied_limits.get(setting['id']) == amps:
            return None
        wallbox = KebaModbus(setting['host'], setting['model'], port=setting['port'],
                            device_id=setting['device_id'], writes_enabled=True,
                            max_current_a=setting['max_current_a'])
        try:
            await wallbox.connect()
            if setting['id'] not in self.failsafe_ready:
                await wallbox.configure_failsafe(timeout=10)
                self.failsafe_ready.add(setting['id'])
            await wallbox.set_limit(amps)
            self.applied_limits[setting['id']] = amps
            self.limit_changed_at[setting['id']] = time.monotonic()
            return None
        except Exception as exc:
            self.failsafe_ready.discard(setting['id'])
            self.applied_limits.pop(setting['id'], None)
            return str(exc)
        finally:
            wallbox.close()

    def adaptive_cap(self, station, mono):
        """Reclaim stable unused current without reacting to normal vehicle ramp-up."""
        station_id = station['id']
        maximum = station['max_current_a']
        commanded = self.applied_limits.get(station_id)
        measured = max(station.get('currents_a') or [0])
        station['unused_grant_a'] = round(max(0, (commanded or 0)-measured), 3)
        if (commanded is None or commanded < 6 or station.get('state') != 3
                or mono-self.limit_changed_at.get(station_id, mono) < 12):
            self.underuse_since.pop(station_id, None)
            station['adaptive_limit_a'] = maximum
            return maximum
        if measured >= commanded-0.5:
            self.underuse_since.pop(station_id, None)
            station['adaptive_limit_a'] = maximum
            return maximum
        if measured < commanded-0.9:
            since = self.underuse_since.setdefault(station_id, mono)
            if mono-since >= 10:
                cap = min(maximum, max(6, math.ceil(measured+0.5)))
                station['adaptive_limit_a'] = cap
                return cap
        else:
            self.underuse_since.pop(station_id, None)
        station['adaptive_limit_a'] = maximum
        return maximum

    async def tick(self):
        now, mono = time.time(), time.monotonic()
        stations = await asyncio.gather(*(self.probe(s) for s in self.station_config))
        building_kw = self.settings.fallback_building_kw
        building_a = [building_kw * 1000 / 690] * 3
        self.manual_start_until = {station_id:until for station_id,until in self.manual_start_until.items() if until > mono}
        allocation_stations = []
        for station in stations:
            cap = self.adaptive_cap(station, mono)
            allocation_stations.append({**station, 'max_current_a':cap,
                                        'priority':'high' if station['id'] in self.manual_start_until else station['priority']})
        allocation_stations.sort(key=lambda station:self.manual_start_until.get(station['id'], 0), reverse=True)
        grants = allocate(allocation_stations, building_a, building_kw, self.settings,
                          0 if self.manual_start_until else mono)
        limits = [0 if self.settings.paused or s['paused'] or not s['connected'] else grants[s['id']]
                  for s in stations]
        results = await asyncio.gather(*(self.apply_limit(s, limit) if s['online'] else asyncio.sleep(0, result='nicht erreichbar')
                                         for s, limit in zip(stations, limits)))
        for station, limit, error in zip(stations, limits, results):
            station['commanded_current_a'] = limit
            station['control_error'] = error
            if error:
                station['status'] = 'offline' if not station['online'] else 'safe'
                station['connection_detail'] = ('Keine Regelverbindung: ' + error)[:180]
            elif station['connected']:
                station['status'] = ('paused' if self.settings.paused or station['paused'] else
                                     'charging' if station['state'] == 3 and limit else 'waiting')
                adaptive = station.get('adaptive_limit_a', station['max_current_a'])
                detail = f'Aktive Freigabe: {limit} A · Fallback-Gebäudelast'
                if adaptive < station['max_current_a']:
                    detail += f' · Fahrzeugbedarf auf {adaptive} A erkannt'
                station['connection_detail'] = detail
                station['failsafe_current_a'] = 0
                station['failsafe_timeout_s'] = 10

        charging = round(sum(s['power_kw'] for s in stations), 3)
        estimated_charging = round(sum(limit * 690 / 1000 for limit in limits), 3)
        total = round(building_kw + charging, 3)
        phases = [round(building_a[p] + sum(s['currents_a'][p] for s in stations), 3) for p in range(3)]
        errors = sum(bool(error) for error in results)
        condition = 'paused' if self.settings.paused else 'degraded' if errors else 'active'
        if condition != self.last_condition:
            messages = {
                'active': 'Aktive lokale Regelung mit Fallback-Gebäudelast.',
                'paused': 'Alle Ladepunkte wurden aktiv auf 0 A gesetzt.',
                'degraded': f'Regelung eingeschränkt: {errors} Ladepunkt(e) nicht steuerbar.',
            }
            await asyncio.to_thread(self.store.event, messages[condition], 'warning' if errors else 'info')
            self.last_condition = condition
        self.state = {
            'mode':'active', 'status':condition, 'timestamp':now, 'meter_timestamp':0,
            'site_name':self.settings.site_name, 'settings':self.settings.model_dump(), 'stations':stations,
            'simulation':self.sim.model_dump(), 'building_kw':building_kw, 'charging_kw':charging,
            'estimated_charging_kw':estimated_charging, 'total_kw':total, 'phase_currents_a':phases,
            'headroom_kw':round(max(0, self.settings.power_limit_kw-self.settings.reserve_kw-building_kw-charging), 3),
            'meter_online':False, 'building_source':'fallback', 'revision':self.revision,
            'overload':total > self.settings.power_limit_kw or any(x > self.settings.phase_limit_a for x in phases),
            'monta_status':'external_unverified', 'commissioned':True,
            'manual_start_ids':list(self.manual_start_until),
        }
        if now-self.last_sample >= 5:
            await asyncio.to_thread(self.store.sample, now, building_kw, charging)
            self.last_sample = now


@asynccontextmanager
async def lifespan(app):
    mode = os.getenv('MODE', 'simulation')
    if mode not in ('simulation', 'commissioning', 'active'):
        raise RuntimeError('MODE muss simulation, commissioning oder active sein.')
    if len(os.getenv('CONTROLLER_TOKEN','')) < 24:
        raise RuntimeError('CONTROLLER_TOKEN mit mindestens 24 Zeichen erforderlich.')
    store = Store(os.getenv('DB_PATH','data/loadmanager.sqlite'))
    controllers = {'simulation': Controller, 'commissioning': CommissioningController, 'active': ActiveController}
    app.state.controller = controllers[mode](store)
    await app.state.controller.tick()
    task = asyncio.create_task(app.state.controller.run())
    app.state.task = task
    try:
        yield
    finally:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        store.db.close()


app = FastAPI(title='KEBA Controller · intern', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware('http')
async def auth(request: Request, call_next):
    from fastapi.responses import JSONResponse
    expected = 'Bearer '+os.getenv('CONTROLLER_TOKEN','')
    if len(expected) < 31 or not hmac.compare_digest(request.headers.get('authorization',''), expected):
        return JSONResponse({'detail':'Nicht autorisiert.'}, status_code=401)
    if request.method == 'POST':
        body = await request.body()
        if len(body)>16384: return JSONResponse({'detail':'Anfrage zu groß.'},status_code=413)
    return await call_next(request)


@app.get('/state')
async def state():
    task = app.state.task
    if task.done(): raise HTTPException(503, 'Regler angehalten.')
    state = app.state.controller.state
    if not state or time.time()-state['timestamp'] > 5:
        raise HTTPException(503, 'Reglerdaten veraltet.')
    return state


@app.get('/healthz')
async def healthz():
    if app.state.task.done():
        raise HTTPException(503, 'Regler angehalten.')
    return {'status':'ok'}


@app.get('/history')
async def history():
    return await asyncio.to_thread(app.state.controller.store.history)


@app.get('/events')
async def events():
    return await asyncio.to_thread(app.state.controller.store.events)


@app.post('/commands')
async def commands(request: Request):
    if app.state.task.done(): raise HTTPException(503, 'Regler angehalten.')
    return await app.state.controller.command(await request.json())
