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
from .regulation import SmoothRegulation
from .modbus import KebaModbus, DETAIL_REGISTERS
from .shelly import ShellyPro3EM, building_phase_currents
from .keba_rest import KebaRest, LocalStartError, LocalStartUncertain
from .local_access import LocalAccessStore
from .models import LocalAccess, Settings, Simulation, StationPatch, StationSetting, station_defaults_from_file
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
        self.last_sample = 0
        self.meter_identity = None
        self.last_meter_timestamp = 0
        self.meter_changed_at = 0
        self.last_good_meter = None
        self.last_good_site = None
        self.last_good_site_at = float('-inf')
        self.detail_cursor = {}

    async def read_meter(self):
        config = self.settings.meter
        identity = (config.enabled, config.host, config.port, config.device_id)
        if identity != self.meter_identity:
            self.meter_identity = identity
            self.last_meter_timestamp = 0
            self.meter_changed_at = 0
            self.last_good_meter = None
            self.last_good_site = None
        meter = {**(self.last_good_meter or {}), 'online':False, 'timestamp':self.last_meter_timestamp, 'error':None}
        if not config.enabled:
            return meter
        device = ShellyPro3EM(config.host, port=config.port, device_id=config.device_id)
        try:
            async with asyncio.timeout(2):
                for attempt in range(2):
                    try:
                        await device.connect()
                        data = await device.telemetry()
                        break
                    except ValueError:
                        raise
                    except Exception:
                        if attempt:
                            raise
                        device.close()
            now, mono = time.time(), time.monotonic()
            if not now-5 <= data['timestamp'] <= now+3:
                raise ValueError('Shelly-Messwerte veraltet; Gerätezeit/NTP prüfen')
            if data['timestamp'] != self.last_meter_timestamp:
                self.meter_changed_at = mono
            elif mono-self.meter_changed_at > 5:
                raise ValueError('Shelly-Messwerte werden nicht aktualisiert')
            self.last_meter_timestamp = data['timestamp']
            self.last_good_meter = data.copy()
            return {**data, 'online':True, 'error':None}
        except Exception as exc:
            meter['error'] = str(exc) or 'Zeitüberschreitung bei der Shelly-Abfrage'
            # Only communication loss may bridge a short gap. Invalid data or
            # device error flags must cause an immediate safe stop.
            meter['transient'] = not isinstance(exc, ValueError)
            return meter
        finally:
            device.close()

    async def read_site(self):
        stations = await asyncio.gather(*(self.probe(s) for s in self.station_config))
        # Read the main meter last: optional KEBA diagnostics used to age it
        # past the freshness deadline before it could even be used.
        meter = await self.read_meter()
        if meter['online'] and time.time()-meter['timestamp'] > 5:
            meter = {'online':False, 'timestamp':meter['timestamp'], 'error':'Shelly-Messwerte veraltet'}
        charging = sum(s['power_kw'] for s in stations)
        source, error = 'fallback', None
        building_kw = self.settings.fallback_building_kw
        building_a = [building_kw*1000/690]*3
        if self.settings.meter.enabled:
            valid_stations = all(s['online'] and len(s['currents_a']) == 3
                                 and all(math.isfinite(v) and v >= 0 for v in [s['power_kw'], *s['currents_a']])
                                 for s in stations)
            if not meter['online'] or not valid_stations:
                source = 'unavailable'
                error = meter['error'] or 'Ladepunktmessung unvollständig; Gebäudelast nicht bestimmbar'
                building_kw, building_a = 0, [0, 0, 0]
            else:
                source = 'meter'
                building_kw = meter['power_kw']-charging
                building_a = building_phase_currents(meter, stations)
        phases = meter['currents_a'] if meter['online'] else [
            building_a[p]+sum(s['currents_a'][p] for s in stations) for p in range(3)]
        total = meter['power_kw'] if meter['online'] else building_kw+charging
        site = {
            'meter':meter, 'meter_online':meter['online'], 'meter_timestamp':meter['timestamp'],
            'building_source':source, 'measurement_error':error,
            'building_kw':round(building_kw, 3), 'charging_kw':round(charging, 3),
            'total_kw':round(total, 3), 'phase_currents_a':[round(i, 3) for i in phases],
            'building_currents_a':building_a,
            'control_base_a':[max(0, meter['currents_a'][p]-sum(s['currents_a'][p] for s in stations))
                              for p in range(3)] if source == 'meter' else building_a,
            'headroom_kw':round(max(0, self.settings.power_limit_kw-self.settings.reserve_kw-max(0, building_kw)-charging), 3) if source != 'unavailable' else 0,
            'power_ceiling_kw':self.settings.power_limit_kw*(1+self.settings.power_tolerance_pct/100),
            'overload':total > self.settings.power_limit_kw*(1+self.settings.power_tolerance_pct/100) or any(i > self.settings.phase_limit_a for i in phases),
        }
        if source == 'meter':
            self.last_good_site = site.copy()
            self.last_good_site_at = time.monotonic()
        elif (source == 'unavailable' and self.last_good_site is not None
              and time.monotonic()-self.last_good_site_at <= 5
              and time.time()-self.last_good_site['meter_timestamp'] <= 5
              and not self.last_good_site['overload']
              and (meter['online'] or meter.get('transient'))):
            # No new capacity is calculated from mixed old/new samples.
            grid = {k:site[k] for k in ('total_kw', 'phase_currents_a', 'meter_timestamp')} if meter['online'] else {}
            site = {**self.last_good_site, **grid, 'meter':meter, 'meter_online':meter['online'],
                    'building_source':'hold', 'measurement_error':error, 'headroom_kw':0,
                    'overload':site['overload'] if meter['online'] else self.last_good_site['overload']}
            # A fresh grid overload overrides gap tolerance immediately.
            if site['overload']:
                site['building_source'] = 'unavailable'
            building_a = site['building_currents_a']
        return stations, building_a, site

    async def sample_site(self, now, site):
        if now-self.last_sample >= 5:
            if site['building_source'] in ('meter', 'fallback'):
                await asyncio.to_thread(self.store.sample, now, site['building_kw'], site['charging_kw'])
            self.last_sample = now

    async def probe(self, setting):
        station = {**setting, 'connected':False, 'online':False, 'network_online':False,
                   'phases':[0,1,2], 'current_a':0, 'currents_a':[0,0,0], 'power_kw':0,
                   'session_kwh':0, 'energy_kwh':0, 'serial':None, 'firmware_raw':None,
                   'error_code':None, 'status':'offline', 'connection_detail':'IP-Adresse fehlt'}
        if not setting['host']:
            return station
        station['web_url'] = f"https://{setting['host']}:8443" if setting['model'] == 'P30 x' else f"https://{setting['host']}"
        wallbox = KebaModbus(setting['host'], setting['model'], port=setting['port'],
                            device_id=setting['device_id'], writes_enabled=False,
                            max_current_a=setting['max_current_a'])
        try:
            identity = (setting['host'], setting['port'], setting['device_id'], setting['model'])
            key = (setting['id'], identity)
            registers = DETAIL_REGISTERS + ([1200,1700,1702] if setting['model'] == 'P40' else [])
            cursor = self.detail_cursor.get(key, 0)
            self.detail_cursor[key] = (cursor+3) % len(registers)
            async with asyncio.timeout(3):
                await wallbox.connect()
                data = await wallbox.telemetry(detail_registers=registers[cursor:cursor+3])
            data = {**self.telemetry_cache.get(key, {}), **data}
            self.telemetry_cache[key] = data.copy()
            station.update(data)
            station['installation_limit_a'] = setting['max_current_a']
            # Register 1100 includes our own previous grant; using it as a
            # capability ceiling would prevent increasing that grant again.
            # Register 1110 accounts for DIP settings, cable and temperature.
            hardware_limit = data.get('hardware_limit_a')
            station['max_current_a'] = setting['max_current_a']
            if hardware_limit is not None:
                station['max_current_a'] = min(setting['max_current_a'], max(0, math.floor(hardware_limit)))
            station['online'] = True
            station['network_online'] = True
            station['connected'] = data['cable_state'] in (5, 7)
            station['current_a'] = round(max(data['currents_a']), 3)
            station['status'] = ('charging' if data['state'] == 3 else 'safe' if data['state'] == 4
                                 else 'paused' if data['state'] == 5 else 'waiting' if data['state'] == 2
                                 else 'available')
            station['connection_detail'] = 'Modbus TCP verbunden (nur Lesen)'
        except Exception as exc:
            station['status'] = 'setup'
            station['connection_detail'] = 'Modbus-Abfrage fehlgeschlagen: ' + (str(exc) or 'Zeitüberschreitung')[:100]
        finally:
            wallbox.close()
        return station

    async def tick(self):
        now = time.time()
        stations, _, site = await self.read_site()
        snapshot = (tuple((s['id'], s['status'], s.get('serial')) for s in stations), site['meter_online'], site['measurement_error'])
        if snapshot != self.last_snapshot:
            online = sum(s['online'] for s in stations)
            web = sum(s['network_online'] for s in stations)
            await asyncio.to_thread(self.store.event,
                f'Inbetriebnahme: {web} Geräte im Netz, {online} über Modbus lesbar. '
                + ('Shelly-Hauptanschlussmessung verbunden.' if site['meter_online'] else site['measurement_error'] or 'Kein Hauptanschlusszähler aktiviert.'),
                'info' if online == len(stations) else 'warning')
            self.last_snapshot = snapshot
        self.state = {
            'mode':'commissioning', 'status':'commissioning', 'timestamp':time.time(),
            'site_name':self.settings.site_name, 'settings':self.settings.model_dump(), 'stations':stations,
            'simulation':self.sim.model_dump(), **site,
            'revision':self.revision, 'monta_status':'external_unverified',
            'commissioned':False,
        }
        if site['building_source'] == 'meter':
            await self.sample_site(now, site)

    async def run(self):
        while True:
            started = time.monotonic()
            async with self.lock:
                await self.tick()
            await asyncio.sleep(max(0.1, 2-(time.monotonic()-started)))

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
                    self.waiting_since.pop(station_id, None)
            self.revision += 1
            await self.tick()
            return result


class ActiveController(CommissioningController):
    """Live controller with site metering or an explicitly configured fallback."""
    waiting_grace_s = 30
    waiting_probe_s = 12
    failsafe_timeout_s = 10
    control_refresh_s = 6

    def __init__(self, store):
        super().__init__(store)
        self.failsafe_ready = set()
        self.applied_limits = {}
        self.manual_start_until = {}
        self.limit_changed_at = {}
        self.control_refreshed_at = {}
        self.underuse_since = {}
        self.demand_caps = {}
        self.waiting_since = {}
        self.regulation = SmoothRegulation()
        self.last_condition = None
        self.last_sample = 0

        self.local_access = LocalAccessStore(store.path.with_name('wallbox-access.json'))
        self.local_start_tasks = {}
        self.local_start_results = {}
        for station in self.station_config:
            result = store.get('local_start:' + station['id'], None)
            if result:
                if result['status'] == 'pending':
                    result = {**result, 'status':'uncertain', 'ok':False,
                              'message':'Neustart während der Startanfrage. Ladezustand vor erneutem Versuch prüfen.'}
                    store.finish_command(result['id'], result, result['message'], 'warning',
                                         key='local_start:' + station['id'])
                self.local_start_results[station['id']] = result

    async def command(self, body):
        if (isinstance(body, dict) and body.get('kind') == 'station'
                and body.get('station_id') in self.local_start_tasks
                and isinstance(body.get('value'), dict)
                and any(key in body['value'] for key in ('host', 'port', 'device_id', 'model'))):
            raise HTTPException(409, 'Verbindungsdaten können während einer Startanfrage nicht geändert werden.')
        if not isinstance(body, dict) or body.get('kind') not in ('local_access', 'local_start'):
            return await super().command(body)
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
            station_id = body.get('station_id')
            station = next((s for s in self.station_config if s['id'] == station_id), None)
            if not station:
                raise HTTPException(404, 'Ladepunkt nicht gefunden.')
            if station_id in self.local_start_tasks:
                raise HTTPException(409, 'Eine lokale Startanfrage läuft bereits.')
            if body['kind'] == 'local_access':
                try:
                    credentials = LocalAccess.model_validate(body.get('value')).model_dump()
                except ValidationError as exc:
                    raise HTTPException(422, 'KEBA-Benutzername und Passwort erforderlich.') from exc
                if not station['host']:
                    raise HTTPException(409, 'Zuerst die lokale IP-Adresse des Ladepunkts speichern.')
                await asyncio.to_thread(self.local_access.save, station, credentials)
                result = {'ok':True, 'id':command_id, 'applied_at':time.time()}
                await asyncio.to_thread(self.store.commit_command, 'local_access_updated',
                                        result, command_id, result,
                                        f'Lokaler KEBA-Zugang für {station_id} gespeichert.')
            else:
                last_start = self.local_start_results.get(station_id, {})
                if (last_start.get('status') in ('accepted', 'uncertain')
                        and time.time()-last_start.get('applied_at', 0) < 30):
                    raise HTTPException(409, 'Startanfrage bereits gesendet. Bitte 30 Sekunden auf den Ladebeginn warten.')
                if self.settings.paused or station['paused']:
                    raise HTTPException(409, 'Pause zuerst aufheben, dann manuell starten.')
                live = next((s for s in (self.state or {}).get('stations', []) if s['id'] == station_id), {})
                if not self.state or time.time()-self.state['timestamp'] > 5 or not live.get('online') or live.get('control_error'):
                    raise HTTPException(409, 'Ladepunkt hat keine aktuelle Regelverbindung.')
                if not live.get('connected') or not live.get('serial'):
                    raise HTTPException(409, 'Fahrzeug anschließen und Geräteerkennung abwarten.')
                if live.get('state') == 3:
                    raise HTTPException(409, 'Der Ladepunkt lädt bereits.')
                if live.get('state') == 4 or live.get('error_code'):
                    raise HTTPException(409, 'Zuerst den Gerätefehler am Ladepunkt beheben.')
                credentials = self.local_access.get(station)
                if not credentials:
                    raise HTTPException(409, 'Unter Ladepunkte zuerst den lokalen KEBA-Zugang hinterlegen.')
                result = {'ok':True, 'id':command_id, 'status':'pending',
                          'message':'Lokaler Ladestart wird bei KEBA angefragt.', 'applied_at':time.time()}
                await asyncio.to_thread(self.store.commit_command, 'local_start:' + station_id,
                                        result, command_id, result,
                                        f'Lokalen Ladestart für {station_id} angefragt.')
                self.local_start_results[station_id] = result
                self.local_start_tasks[station_id] = asyncio.create_task(
                    self.local_start(station.copy(), credentials, live['serial'], command_id))
            self.revision += 1
            # Refresh public fields immediately; no extra Modbus cycle is needed.
            self.publish_local_starts()
            return result

    def publish_local_starts(self):
        for station in (self.state or {}).get('stations', []):
            station['local_start_configured'] = bool(self.local_access.get(station))
            station['local_start_result'] = self.local_start_results.get(station['id'])

    async def local_start(self, station, credentials, serial, command_id):
        station_id = station['id']
        try:
            # This runs outside the regulator lock, including network timeouts.
            # The 6-second watchdog refresh must continue throughout login/start.
            await asyncio.wait_for(KebaRest(station, credentials).start(serial), timeout=20)
            status, message = 'accepted', 'Lokaler Start von KEBA akzeptiert. Ladebeginn am Messwert prüfen.'
        except (LocalStartUncertain, asyncio.TimeoutError):
            status, message = 'uncertain', 'Keine eindeutige KEBA-Startbestätigung. Ladezustand vor erneutem Versuch prüfen.'
        except LocalStartError as exc:
            status, message = 'failed', str(exc)
        except Exception:
            status, message = 'failed', 'Lokaler KEBA-Start fehlgeschlagen.'
        async with self.lock:
            result = {'id':command_id, 'ok':status == 'accepted', 'status':status,
                      'message':message, 'applied_at':time.time()}
            await asyncio.to_thread(self.store.finish_command, command_id, result,
                                    f'{station_id}: {message}', 'info' if result['ok'] else 'warning',
                                    key='local_start:' + station_id)
            self.local_start_results[station_id] = result
            current = next((s for s in self.station_config if s['id'] == station_id), {})
            if result['ok'] and current == station and not self.settings.paused:
                self.manual_start_until[station_id] = time.monotonic()+max(300, self.settings.rotation_seconds)
                self.waiting_since.pop(station_id, None)
            self.local_start_tasks.pop(station_id, None)
            self.publish_local_starts()

    async def apply_limit(self, setting, amps):
        station_id = setting['id']
        previous = self.applied_limits.get(station_id)
        refresh_age = time.monotonic()-self.control_refreshed_at.get(station_id, float('-inf'))
        if (station_id in self.failsafe_ready and previous == amps
                and refresh_age < self.control_refresh_s):
            return None
        wallbox = KebaModbus(setting['host'], setting['model'], port=setting['port'],
                            device_id=setting['device_id'], writes_enabled=True,
                            max_current_a=setting['max_current_a'])
        try:
            await wallbox.connect()
            if station_id not in self.failsafe_ready:
                await wallbox.configure_failsafe(timeout=self.failsafe_timeout_s)
                self.failsafe_ready.add(station_id)
            else:
                # Read traffic alone does not reliably feed the KEBA watchdog.
                await wallbox.refresh_failsafe(timeout=self.failsafe_timeout_s)
            # Reassert the limit and enable after a possible device-side timeout.
            await wallbox.set_limit(amps)
            self.applied_limits[station_id] = amps
            self.control_refreshed_at[station_id] = time.monotonic()
            # Heartbeats must not perpetually restart adaptive ramp-up timing.
            if previous != amps or refresh_age >= self.failsafe_timeout_s:
                self.limit_changed_at[station_id] = self.control_refreshed_at[station_id]
            return None
        except Exception as exc:
            if time.monotonic()-self.control_refreshed_at.get(station_id, float('-inf')) >= self.failsafe_timeout_s:
                self.failsafe_ready.discard(station_id)
                self.applied_limits.pop(station_id, None)
                self.control_refreshed_at.pop(station_id, None)
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
        if commanded is None or commanded < 6 or station.get('state') != 3:
            self.demand_caps.pop(station_id, None)
            self.underuse_since.pop(station_id, None)
            station['adaptive_limit_a'] = maximum
            return maximum
        learned = self.demand_caps.get(station_id)
        if learned:
            cap, probe_at = learned
            if mono >= probe_at or measured >= cap-0.5:
                self.demand_caps.pop(station_id, None)
                self.underuse_since.pop(station_id, None)
            else:
                maximum = min(maximum, cap)
        # Keep an observed demand cap during ramp-up and in the dead band.
        # Otherwise our own reduction immediately restores the hardware maximum.
        if mono-self.limit_changed_at.get(station_id, mono) < 12:
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
                if cap < maximum:
                    self.demand_caps[station_id] = (cap, mono+self.settings.rotation_seconds)
                station['adaptive_limit_a'] = cap
                return cap
        else:
            self.underuse_since.pop(station_id, None)
        station['adaptive_limit_a'] = maximum
        return maximum

    def waiting_cap(self, station, mono):
        """Keep a continuous minimum offer while capacity permits.

        Repeated 0/6-A wake-up probes can look like charging faults to cars.
        A waiting EV only loses its offer when the real site budget needs it.
        """
        station_id = station['id']
        station['waiting_reclaimed'] = False
        if (self.settings.paused or station.get('paused')
                or not station.get('online', True) or not station.get('connected')
                or station.get('state') not in (1, 2, 5) or max(station.get('currents_a') or [0]) >= 0.5):
            self.waiting_since.pop(station_id, None)
            return None
        self.waiting_since.setdefault(station_id, mono)
        station['waiting_probe_a'] = 6
        return 6

    async def tick(self):
        now, mono = time.time(), time.monotonic()
        stations, building_a, site = await self.read_site()
        measurement_fault = site['building_source'] == 'unavailable'
        if site['building_source'] in ('meter', 'hold'):
            # Feedback coordinate for prospective grants, not a decomposition
            # of RMS building current. Actual grid headroom gates every increase.
            building_a = site['control_base_a']
        self.manual_start_until = {station_id:until for station_id,until in self.manual_start_until.items() if until > mono}
        allocation_stations = []
        for station in stations:
            cap = self.adaptive_cap(station, mono)
            waiting_cap = self.waiting_cap(station, mono)
            if waiting_cap is not None:
                cap = min(cap, waiting_cap)
            allocation_stations.append({**station, 'max_current_a':cap,
                                        'allocation_rank':2 if station.get('state') == 3 or max(station['currents_a']) >= 0.5 else 1,
                                        'priority':'high' if station['id'] in self.manual_start_until else station['priority']})
        allocation_stations.sort(key=lambda station:self.manual_start_until.get(station['id'], 0), reverse=True)
        grants = allocate(allocation_stations, building_a, max(0, site['building_kw']), self.settings,
                          0 if self.manual_start_until else mono,
                          voltages_v=site['meter'].get('voltages_v'), previous=self.applied_limits)
        grants = self.regulation.apply(
            stations, grants, self.applied_limits, building_a, max(0, site['building_kw']),
            self.settings, mono, site['meter'].get('voltages_v'),
            meter=site['meter'] if site['building_source'] == 'meter' else None,
            hold=site['building_source'] == 'hold', fault=measurement_fault)
        if measurement_fault:
            grants = dict.fromkeys(grants, 0)

        limits = [0 if self.settings.paused or s['paused'] or not s['connected'] else grants[s['id']]
                  for s in stations]
        previous_limits = self.applied_limits.copy()
        results = await asyncio.gather(*(self.apply_limit(s, limit) if s['online'] else asyncio.sleep(0, result='nicht erreichbar')
                                         for s, limit in zip(stations, limits)))
        for station, limit, error in zip(stations, limits, results):
            station['commanded_current_a'] = limit
            station['control_error'] = error
            previous = previous_limits.get(station['id'])
            if not error and previous is not None and (previous == 0) != (limit == 0) and station['connected']:
                await asyncio.to_thread(self.store.event,
                    f"{station['name']}: Freigabe {previous} → {limit} A · {station.get('control_reason', '')}",
                    'warning' if limit == 0 and measurement_fault else 'info')
            if error:
                if time.monotonic()-self.control_refreshed_at.get(station['id'], float('-inf')) >= self.failsafe_timeout_s:
                    self.failsafe_ready.discard(station['id'])
                    self.applied_limits.pop(station['id'], None)
                    self.control_refreshed_at.pop(station['id'], None)
                station['status'] = 'offline' if not station['online'] else 'safe'
                station['connection_detail'] = ('Keine Regelverbindung: ' + error)[:180]
            elif station['connected']:
                station['status'] = ('safe' if measurement_fault else 'paused' if self.settings.paused or station['paused'] else
                                     'charging' if station['state'] == 3 and limit else 'waiting')
                adaptive = station.get('adaptive_limit_a', station['max_current_a'])
                detail = f"Aktive Freigabe: {limit} A · {'Shelly-Hauptanschlussmessung' if site['meter_online'] else 'Fallback-Gebäudelast'}"
                if station.get('waiting_reclaimed'):
                    detail = 'Keine Leistungsabnahme · Budget umverteilt'
                elif station.get('waiting_probe_a'):
                    detail = f'Startsignal: {limit} A · wartet auf Ladebeginn'
                elif adaptive < station['max_current_a']:
                    detail += f' · Fahrzeugbedarf auf {adaptive} A erkannt'
                detail += ' · ' + station.get('control_reason', '')
                if measurement_fault:
                    detail = 'Sicherer Halt: ' + site['measurement_error']
                station['connection_detail'] = detail
                station['failsafe_current_a'] = 0
                station['failsafe_timeout_s'] = self.failsafe_timeout_s

        voltage_sum = sum(max(230, v) for v in site['meter'].get('voltages_v', [230]*3))
        estimated_charging = round(sum(limits)*voltage_sum/1000, 3)
        errors = sum(bool(error) for error in results)
        condition = 'safe' if measurement_fault else 'paused' if self.settings.paused else 'holding' if site['building_source'] == 'hold' else 'degraded' if errors else 'active'
        condition_key = (condition, site['building_source'], site['measurement_error'])
        if condition_key != self.last_condition:
            messages = {
                'active': 'Aktive lokale Regelung mit ' + ('Shelly-Hauptanschlussmessung.' if site['meter_online'] else 'Fallback-Gebäudelast.'),
                'safe': 'Sicherer Halt: ' + (site['measurement_error'] or ''),
                'holding': 'Kurze Messlücke: bestehende Freigaben werden höchstens bis zum Messalter von 5 Sekunden gehalten.',
                'paused': 'Alle Ladepunkte wurden aktiv auf 0 A gesetzt.',
                'degraded': f'Regelung eingeschränkt: {errors} Ladepunkt(e) nicht steuerbar.',
            }
            await asyncio.to_thread(self.store.event, messages[condition], 'warning' if errors or measurement_fault else 'info')
            self.last_condition = condition_key
        self.state = {
            'mode':'active', 'status':condition, 'timestamp':time.time(),
            'cycle_duration_s':round(time.monotonic()-mono, 3),
            'site_name':self.settings.site_name, 'settings':self.settings.model_dump(), 'stations':stations,
            'simulation':self.sim.model_dump(), **site,
            'estimated_charging_kw':estimated_charging, 'revision':self.revision,
            'monta_status':'external_unverified', 'commissioned':True,
            'manual_start_ids':list(self.manual_start_until),
        }
        self.publish_local_starts()
        await self.sample_site(now, site)


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
        pending = list(getattr(app.state.controller, 'local_start_tasks', {}).values())
        for operation in pending:
            operation.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
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
