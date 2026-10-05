import asyncio
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from loadmanager import controller
from loadmanager.allocator import allocate
from loadmanager.models import Settings
from loadmanager.modbus import KebaModbus
from loadmanager.storage import Store


class FakeKeba:
    configured = []
    limits = []
    refreshed = []

    def __init__(self, host, model, **kwargs):
        self.host = host

    async def connect(self):
        return None

    async def telemetry(self, full=False, detail_registers=None):
        return {
            'state': 2, 'cable_state': 5, 'error_code': 0,
            'currents_a': [0, 0, 0], 'serial': 1, 'firmware_raw': 1,
            'power_kw': 0, 'energy_kwh': 0, 'session_kwh': 0,
            'device_limit_a': 32, 'hardware_limit_a': 32,
            'rfid_uid': 'D4CD7650',
        }

    async def configure_failsafe(self, timeout=10):
        self.configured.append((self.host, timeout))

    async def set_limit(self, amps):
        self.limits.append((self.host, amps))

    async def refresh_failsafe(self, timeout=10):
        self.refreshed.append((self.host, timeout))

    def close(self):
        return None


def test_active_controller_uses_fallback_and_writes_limits(tmp_path, monkeypatch):
    FakeKeba.configured.clear()
    FakeKeba.limits.clear()
    monkeypatch.setattr(controller, 'KebaModbus', FakeKeba)

    async def reachable(*_args):
        return True

    monkeypatch.setattr(controller, 'port_open', reachable)
    store = Store(tmp_path / 'state.sqlite')
    active = controller.ActiveController(store)

    asyncio.run(active.tick())

    assert active.state['mode'] == 'active'
    assert active.state['building_source'] == 'fallback'
    assert active.state['building_kw'] == active.settings.fallback_building_kw
    assert len(FakeKeba.configured) == 4
    grants = [amps for _host, amps in FakeKeba.limits]
    assert any(amps >= 6 for amps in grants)
    assert sum(grants) * 690 <= (
        active.settings.power_limit_kw
        - active.settings.reserve_kw
        - active.settings.fallback_building_kw
    ) * 1000

    FakeKeba.limits.clear()
    active.settings.paused = True
    asyncio.run(active.tick())
    assert all(amps == 0 for _host, amps in FakeKeba.limits)
    assert set(active.applied_limits.values()) == {0}
    store.db.close()


def test_adaptive_cap_reclaims_only_stable_unused_current(tmp_path):
    store = Store(tmp_path / 'adaptive.sqlite')
    active = controller.ActiveController(store)
    mono = 100.0
    active.applied_limits = {'cp-1':10, 'cp-2':9}
    active.limit_changed_at = {'cp-1':70, 'cp-2':70}
    active.underuse_since = {'cp-1':80}

    limited = {'id':'cp-1', 'state':3, 'currents_a':[8.087,8.01,8.02], 'max_current_a':32}
    requesting = {'id':'cp-2', 'state':3, 'currents_a':[8.851,8.82,8.80], 'max_current_a':32}

    assert active.adaptive_cap(limited, mono) == 9
    assert active.adaptive_cap(requesting, mono) == 32
    assert limited['unused_grant_a'] == 1.913
    store.db.close()


def test_waiting_vehicle_keeps_continuous_start_offer(tmp_path):
    store = Store(tmp_path / 'waiting.sqlite')
    active = controller.ActiveController(store)
    waiting = {'id':'cp-1', 'state':2, 'connected':True}

    assert active.waiting_cap(waiting, 100) == 6
    assert active.waiting_cap(waiting, 131) == 6
    assert waiting['waiting_reclaimed'] is False
    assert active.waiting_cap(
        waiting, 100+active.waiting_grace_s+active.settings.rotation_seconds-5
    ) == 6
    store.db.close()


def test_waiting_offer_and_active_vehicle_share_available_capacity(tmp_path):
    store = Store(tmp_path / 'demand.sqlite')
    active = controller.ActiveController(store)
    active.waiting_since = {'cp-1':100}
    waiting = {'id':'cp-1', 'state':2, 'connected':True, 'online':True, 'paused':False,
               'priority':'normal', 'phases':[0,1,2], 'max_current_a':32}
    charging = {'id':'cp-2', 'state':3, 'connected':True, 'online':True, 'paused':False,
                'priority':'normal', 'phases':[0,1,2], 'max_current_a':16}
    waiting['max_current_a'] = active.waiting_cap(waiting, 131)
    waiting['allocation_rank'] = 1
    charging['allocation_rank'] = 2
    settings = Settings(fallback_building_kw=8)
    building_a = [settings.fallback_building_kw*1000/690]*3

    grants = allocate([waiting, charging], building_a, settings.fallback_building_kw,
                      settings, 131)

    assert grants == {'cp-1':6, 'cp-2':15}
    store.db.close()


@pytest.mark.parametrize('pause_kind', ['settings', 'station'])
@pytest.mark.parametrize('pause_duration', [25, 150])
def test_resume_gives_vehicle_a_fresh_start_window(tmp_path, monkeypatch, pause_kind, pause_duration):
    """Time spent paused must not shorten the EV's next wake-up attempt."""
    FakeKeba.configured.clear()
    FakeKeba.limits.clear()
    monkeypatch.setattr(controller, 'KebaModbus', FakeKeba)
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(controller, 'time', SimpleNamespace(
        time=time.time, monotonic=lambda: clock.now))
    store = Store(tmp_path / 'resume.sqlite')
    active = controller.ActiveController(store)
    active.station_config = active.station_config[:1]
    ev = SimpleNamespace(state=3)

    async def probe(setting):
        amps = 16 if ev.state == 3 else 0
        return {**setting, 'connected':True, 'online':True, 'state':ev.state,
                'phases':[0,1,2], 'currents_a':[amps]*3, 'power_kw':amps*0.69,
                'status':'charging' if amps else 'waiting'}

    monkeypatch.setattr(active, 'probe', probe)

    async def pause(paused):
        value = ({**active.settings.model_dump(), 'paused':paused}
                 if pause_kind == 'settings' else {'paused':paused})
        await active.command({'id':str(uuid.uuid4()), 'issued_at':time.time(),
                              'kind':pause_kind, 'station_id':'cp-1', 'value':value})

    async def scenario():
        await active.tick()
        assert active.applied_limits['cp-1'] >= 6
        await pause(True)
        assert active.applied_limits['cp-1'] == 0
        ev.state = 2
        clock.now = 102
        await active.tick()
        resumed_at = 102+pause_duration
        clock.now = resumed_at
        await pause(False)
        assert active.applied_limits['cp-1'] >= 6
        # A 25-second pause used to leave only five seconds for the EV.
        clock.now = resumed_at+6
        await active.tick()
        assert active.applied_limits['cp-1'] >= 6
        clock.now = resumed_at+24
        await active.tick()
        assert active.applied_limits['cp-1'] >= 6
        ev.state = 3
        clock.now = resumed_at+26
        await active.tick()
        assert active.applied_limits['cp-1'] >= 6
        assert active.state['stations'][0]['status'] == 'charging'
        clock.now = resumed_at+53
        await active.tick()
        assert active.applied_limits['cp-1'] >= 6

    try:
        asyncio.run(scenario())
    finally:
        store.db.close()


def test_waiting_state_does_not_reclaim_when_current_already_flows(tmp_path):
    """Sequential Modbus reads can straddle the transition into charging."""
    store = Store(tmp_path / 'starting.sqlite')
    active = controller.ActiveController(store)
    active.waiting_since['cp-1'] = 100
    starting = {'id':'cp-1', 'state':2, 'connected':True, 'online':True,
                'currents_a':[5.9, 5.8, 5.9]}
    try:
        assert active.waiting_cap(starting, 131) is None
        assert starting['waiting_reclaimed'] is False
        # If the vehicle subsequently stops again, it gets a new grace period.
        starting['currents_a'] = [0, 0, 0]
        assert active.waiting_cap(starting, 133) == 6
        assert active.waiting_cap(starting, 162) == 6
        assert active.waiting_cap(starting, 164) == 6
    finally:
        store.db.close()


@pytest.mark.parametrize('amps', [0, 16])
def test_unchanged_limit_keeps_watchdog_alive_and_recovers_after_timeout(tmp_path, monkeypatch, amps):
    """Model the P30 watchdog which expires despite successful telemetry reads."""
    clock = SimpleNamespace(now=100.0)
    device = SimpleNamespace(current=0, deadline=0)
    monkeypatch.setattr(controller, 'time', SimpleNamespace(
        time=time.time, monotonic=lambda: clock.now))
    FakeKeba.configured.clear()
    FakeKeba.limits.clear()
    FakeKeba.refreshed.clear()

    class WatchdogKeba(FakeKeba):
        async def configure_failsafe(self, timeout=10):
            await super().configure_failsafe(timeout)
            device.current = 0
            device.deadline = clock.now+timeout

        async def refresh_failsafe(self, timeout=10):
            await super().refresh_failsafe(timeout)
            device.deadline = clock.now+timeout

        async def set_limit(self, value):
            await super().set_limit(value)
            device.current = value

    monkeypatch.setattr(controller, 'KebaModbus', WatchdogKeba)
    store = Store(tmp_path / 'watchdog.sqlite')
    active = controller.ActiveController(store)
    setting = active.station_config[0]

    async def scenario():
        assert await active.apply_limit(setting, amps) is None
        for second in range(102, 130, 2):
            clock.now = second
            assert device.deadline > clock.now, 'Device watchdog expired'
            assert await active.apply_limit(setting, amps) is None
            assert device.current == amps
        assert len(FakeKeba.configured) == 1
        assert len(FakeKeba.refreshed) == 4
        assert active.limit_changed_at['cp-1'] == 100

        # After a genuine interruption, merely feeding the watchdog does not
        # undo its stop: the unchanged current/enable must be sent again too.
        clock.now = 150
        device.current = 0
        assert await active.apply_limit(setting, amps) is None
        assert device.current == amps
        assert device.deadline == 160
        assert active.limit_changed_at['cp-1'] == 150

        # A pause takes effect immediately, even just after a heartbeat.
        clock.now = 151
        assert await active.apply_limit(setting, 0) is None
        assert device.current == 0

    try:
        asyncio.run(scenario())
    finally:
        store.db.close()


def test_failed_heartbeat_is_reconfigured_on_next_attempt(tmp_path, monkeypatch):
    FakeKeba.configured.clear()
    FakeKeba.limits.clear()
    monkeypatch.setattr(controller, 'KebaModbus', FakeKeba)
    store = Store(tmp_path / 'heartbeat-error.sqlite')
    active = controller.ActiveController(store)
    setting = active.station_config[0]
    active.failsafe_ready.add('cp-1')
    active.applied_limits['cp-1'] = 16
    monkeypatch.setattr(FakeKeba, 'refresh_failsafe', AsyncMock(side_effect=OSError('timeout')))

    async def scenario():
        assert await active.apply_limit(setting, 16) == 'timeout'
        assert await active.apply_limit(setting, 16) is None
        assert FakeKeba.configured == [(setting['host'], 10)]
        assert FakeKeba.limits == [(setting['host'], 16)]

    try:
        asyncio.run(scenario())
    finally:
        store.db.close()


def test_failsafe_refresh_only_writes_timeout_register(monkeypatch):
    write = AsyncMock()
    monkeypatch.setattr(KebaModbus, 'write16', write)

    async def refresh():
        wallbox = KebaModbus('127.0.0.1', 'P30 x', writes_enabled=True)
        try:
            await wallbox.refresh_failsafe(10)
        finally:
            wallbox.close()

    asyncio.run(refresh())
    write.assert_awaited_once_with(5018, 10)
