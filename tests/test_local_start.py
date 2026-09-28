"""Local authorization must neither bypass power limits nor block the regulator."""
import asyncio
import json
import stat
import time
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from loadmanager import controller, keba_rest
from loadmanager.keba_rest import KebaRest, LocalStartError, LocalStartUncertain
from loadmanager.storage import Store


def command(kind, value=None):
    return {'id':str(uuid.uuid4()), 'issued_at':time.time(), 'kind':kind,
            'station_id':'cp-1', 'value':value or {}}


@pytest.fixture
def active(tmp_path, monkeypatch):
    store = Store(tmp_path / 'state.sqlite')
    active = controller.ActiveController(store)
    active.station_config = active.station_config[:1]
    async def probe(setting):
        return {**setting, 'state':1, 'connected':True, 'online':True,
                'serial':12345, 'error_code':0, 'control_error':None,
                'phases':[0,1,2], 'currents_a':[0,0,0], 'power_kw':0}
    monkeypatch.setattr(active, 'probe', probe)
    monkeypatch.setattr(active, 'apply_limit', AsyncMock(return_value=None))
    asyncio.run(active.tick())
    yield active
    store.db.close()


def configure(active):
    return active.command(command('local_access', {'username':'admin', 'password':'synthetic-KEBA-secret'}))


def test_credentials_never_appear_in_public_state_events_or_sqlite(active):
    asyncio.run(configure(active))
    assert active.state['stations'][0]['local_start_configured']
    assert stat.S_IMODE(active.local_access.path.stat().st_mode) == 0o600
    assert 'synthetic-KEBA-secret' not in json.dumps(active.state)
    assert 'synthetic-KEBA-secret' not in json.dumps(active.store.events())
    assert 'synthetic-KEBA-secret' not in '\n'.join(active.store.db.iterdump())
    reloaded = controller.ActiveController(active.store)
    assert reloaded.local_access.get(active.station_config[0])['password'] == 'synthetic-KEBA-secret'
    assert reloaded.local_access.get({**active.station_config[0], 'host':'192.168.1.80'}) is None


def test_slow_start_keeps_regulator_running_and_replay_does_not_repeat(active, monkeypatch):
    async def scenario():
        await configure(active)
        entered, release = asyncio.Event(), asyncio.Event()
        async def start(_self, serial):
            assert serial == 12345
            entered.set()
            await release.wait()
        mock = AsyncMock(side_effect=start)
        monkeypatch.setattr(KebaRest, 'start', lambda self, serial: mock(self, serial))
        request = command('local_start')
        result = await active.command(request)
        assert result['status'] == 'pending'
        await entered.wait()
        async with asyncio.timeout(0.5):
            async with active.lock:
                await active.tick()
        active.apply_limit.assert_awaited()
        assert await active.command(request) == result
        with pytest.raises(HTTPException) as duplicate:
            await active.command(command('local_start'))
        assert duplicate.value.status_code == 409
        with pytest.raises(HTTPException) as changed_host:
            await active.command(command('station', {'host':'192.168.1.80'}))
        assert changed_host.value.status_code == 409
        task = active.local_start_tasks['cp-1']
        release.set()
        await task
        assert active.local_start_results['cp-1']['status'] == 'accepted'
        assert (await active.command(request))['status'] == 'accepted'
        with pytest.raises(HTTPException) as too_soon:
            await active.command(command('local_start'))
        assert too_soon.value.status_code == 409
        assert mock.await_count == 1
        assert 'cp-1' in active.manual_start_until
        assert not active.local_start_tasks
    asyncio.run(scenario())


@pytest.mark.parametrize('condition', ['global_pause', 'station_pause', 'disconnected', 'offline', 'stale', 'charging', 'fault', 'missing_access'])
def test_invalid_start_has_no_wallbox_side_effect(active, monkeypatch, condition):
    async def scenario():
        await configure(active)
        station = active.state['stations'][0]
        if condition == 'global_pause': active.settings.paused = True
        elif condition == 'station_pause': active.station_config[0]['paused'] = True
        elif condition == 'disconnected': station['connected'] = False
        elif condition == 'offline': station['online'] = False
        elif condition == 'stale': active.state['timestamp'] -= 6
        elif condition == 'charging': station['state'] = 3
        elif condition == 'fault': station['error_code'] = 1
        elif condition == 'missing_access': active.local_access.values.clear()
        mock = AsyncMock()
        monkeypatch.setattr(KebaRest, 'start', mock)
        with pytest.raises(HTTPException) as error:
            await active.command(command('local_start'))
        assert error.value.status_code == 409
        mock.assert_not_awaited()
        assert not active.local_start_tasks
    asyncio.run(scenario())


def test_pause_during_network_request_remains_in_force(active, monkeypatch):
    async def scenario():
        await configure(active)
        release = asyncio.Event()
        monkeypatch.setattr(KebaRest, 'start', lambda *_args: release.wait())
        await active.command(command('local_start'))
        task = active.local_start_tasks['cp-1']
        await active.command(command('settings', {**active.settings.model_dump(), 'paused':True}))
        release.set()
        await task
        assert active.settings.paused
        assert active.state['stations'][0]['commanded_current_a'] == 0
        assert 'cp-1' not in active.manual_start_until
    asyncio.run(scenario())


@pytest.mark.parametrize('error,status', [(LocalStartError('Zugang abgelehnt.'),'failed'), (LocalStartUncertain('Timeout'),'uncertain')])
def test_failure_is_visible_and_does_not_claim_charging(active, monkeypatch, error, status):
    async def scenario():
        await configure(active)
        monkeypatch.setattr(KebaRest, 'start', AsyncMock(side_effect=error))
        await active.command(command('local_start'))
        await active.local_start_tasks['cp-1']
        result = active.state['stations'][0]['local_start_result']
        assert not result['ok']
        assert result['status'] == status
        assert 'cp-1' not in active.manual_start_until
    asyncio.run(scenario())


def test_restart_marks_pending_request_uncertain_without_resending(active):
    request = command('local_start')
    result = {'id':request['id'], 'status':'pending', 'ok':True}
    active.store.commit_command('local_start:cp-1', result, request['id'], result, 'Test')
    reloaded = controller.ActiveController(active.store)
    assert not reloaded.local_start_tasks
    assert reloaded.local_start_results['cp-1']['status'] == 'uncertain'
    assert active.store.previous(request['id'])['status'] == 'uncertain'


@pytest.mark.parametrize('suffix', ['startCharging', 'start-charging'])
@pytest.mark.parametrize('outcome', ['accepted', 'rejected', 'timeout', 'wrong_serial', 'bad_login', 'invalid_response'])
def test_rest_sends_only_one_local_start_and_no_configuration_writes(monkeypatch, suffix, outcome):
    requests = []
    async def handle(request):
        requests.append(request)
        path = request.url.path
        if path == '/v2/jwt/login':
            return httpx.Response(401 if outcome == 'bad_login' else 200, json={'accessToken':'synthetic-token'})
        assert request.headers['Authorization'] == 'Bearer synthetic-token'
        if path == '/openapi.json':
            return httpx.Response(200, json={'paths':{f'/v2/wallboxes/{{serialNumber}}/{suffix}':{'post':{}}}})
        if path == '/v2/wallboxes':
            return httpx.Response(200, json={'wallboxes':[{'serialNumber':'99999' if outcome == 'wrong_serial' else '12345'}]})
        assert path == f'/v2/wallboxes/12345/{suffix}' and request.method == 'POST'
        if outcome == 'timeout': raise httpx.ReadTimeout('synthetic')
        if outcome == 'invalid_response': return httpx.Response(200, text='not-json')
        return httpx.Response(202, json={'acceptStatus':'REJECTED' if outcome == 'rejected' else 'ACCEPTED'})
    client_class = httpx.AsyncClient
    monkeypatch.setattr(keba_rest.httpx, 'AsyncClient', lambda **kw: client_class(**kw, transport=httpx.MockTransport(handle)))
    rest = KebaRest({'host':'192.168.1.71'}, {'username':'admin','password':'synthetic'})
    if outcome == 'accepted': asyncio.run(rest.start(12345))
    else:
        with pytest.raises(LocalStartUncertain if outcome in ('timeout','invalid_response') else LocalStartError):
            asyncio.run(rest.start(12345))
    writes = [r.url.path for r in requests if r.method != 'GET']
    assert writes == (['/v2/jwt/login'] if outcome in ('bad_login','wrong_serial') else ['/v2/jwt/login', f'/v2/wallboxes/12345/{suffix}'])
