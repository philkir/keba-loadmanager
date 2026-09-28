import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from loadmanager import controller
from loadmanager.storage import Store


@pytest.fixture
def live_controller(tmp_path, monkeypatch):
    """Wallboxes echo the applied current, independently of their hardware cap."""
    clock = SimpleNamespace(now=100.0)
    devices = {
        '192.168.1.71': {'limit':12, 'hardware':20, 'drawing':True},
        '192.168.1.72': {'limit':12, 'hardware':16, 'drawing':False},
    }

    class Wallbox:
        def __init__(self, host, _model, **_kwargs):
            self.device = devices[host]

        async def connect(self):
            pass

        async def telemetry(self, full=False):
            d = self.device
            current = min(d['limit'], d.get('demand', d['limit'])) if d['drawing'] else 0
            data = {'state':3 if d['drawing'] else 5 if d['limit'] == 0 else 1,
                    'cable_state':7, 'error_code':0, 'currents_a':[current]*3,
                    'power_kw':current*0.69, 'energy_kwh':0, 'session_kwh':0}
            if full:
                data.update(device_limit_a=d['limit'], hardware_limit_a=d['hardware'])
            return data

        async def configure_failsafe(self, timeout=10):
            self.device['limit'] = 0

        async def refresh_failsafe(self, timeout=10):
            pass

        async def set_limit(self, amps):
            self.device['limit'] = amps

        def close(self):
            pass

    monkeypatch.setattr(controller, 'KebaModbus', Wallbox)
    monkeypatch.setattr(controller, 'port_open', AsyncMock(return_value=True))
    monkeypatch.setattr(controller, 'time', SimpleNamespace(
        time=time.time, monotonic=lambda: clock.now))
    store = Store(tmp_path / 'recovery.sqlite')
    active = controller.ActiveController(store)
    active.station_config = active.station_config[:2]
    active.settings.fallback_building_kw = 6
    active.settings.rotation_seconds = 240
    try:
        yield active, clock, devices
    finally:
        store.db.close()


@pytest.mark.parametrize('readback', [0, 6, 12])
@pytest.mark.parametrize('hardware,installation,expected', [
    (20, 32, 20), (32, 16, 16), (0, 32, 0), (5, 32, 5), (12.5, 32, 12),
])
def test_allocation_ceiling_uses_hardware_and_installation(live_controller, readback, hardware, installation, expected):
    active, _clock, devices = live_controller
    setting = {**active.station_config[0], 'max_current_a':installation}
    devices[setting['host']].update(limit=readback, hardware=hardware)

    station = asyncio.run(active.probe(setting))

    assert station['online'] is True
    assert station['device_limit_a'] == readback  # Still visible as telemetry.
    assert station['max_current_a'] == expected


def test_stopped_vehicle_releases_budget_across_suspended_state_and_cached_reads(live_controller):
    active, clock, devices = live_controller

    async def tick_at(second):
        clock.now = second
        await active.tick()
        grants = active.applied_limits
        settings = active.settings
        assert sum(grants.values())*0.69 <= settings.power_limit_kw-settings.reserve_kw-settings.fallback_building_kw
        assert sum(grants.values())+(settings.fallback_building_kw+settings.reserve_kw)*1000/690 <= settings.phase_limit_a
        assert 0 <= grants['cp-1'] <= 20
        assert 0 <= grants['cp-2'] <= 16
        return dict(grants)

    async def scenario():
        # Before this fix both points were stuck at their echoed 12 A limit.
        assert await tick_at(100) == {'cp-1':18, 'cp-2':6}
        assert await tick_at(129) == {'cp-1':18, 'cp-2':6}
        assert await tick_at(131) == {'cp-1':20, 'cp-2':0}
        # Our 0-A command changes the P40 to suspended. It must stay reclaimed,
        # even after the next full read reports 0 A as its current device limit.
        assert await tick_at(134) == {'cp-1':20, 'cp-2':0}
        assert await tick_at(162) == {'cp-1':20, 'cp-2':0}
        # Periodic wake-up offers remain possible within all site limits.
        assert await tick_at(359) == {'cp-1':18, 'cp-2':6}
        assert await tick_at(371) == {'cp-1':20, 'cp-2':0}
        # Once the second vehicle draws current again, both share the budget.
        assert await tick_at(599) == {'cp-1':18, 'cp-2':6}
        devices['192.168.1.72']['drawing'] = True
        assert await tick_at(601) == {'cp-1':12, 'cp-2':12}

    asyncio.run(scenario())


@pytest.mark.parametrize('state', [1, 2, 5])
def test_waiting_states_share_one_grace_period(live_controller, state):
    active, _clock, _devices = live_controller
    station = {'id':'cp-2', 'connected':True, 'online':True, 'state':state,
               'currents_a':[0.004]*3}
    assert active.waiting_cap(station, 100) == 6
    # State transitions during stopping/authorization must not restart grace.
    station['state'] = 2 if state == 5 else 5
    assert active.waiting_cap(station, 131) == 0
    assert station['waiting_reclaimed'] is True
    station['state'] = 1
    assert active.waiting_cap(station, 150) == 0


@pytest.mark.parametrize('state', [1, 2, 5])
def test_measured_current_preserves_grant_during_state_transition(live_controller, state):
    active, _clock, _devices = live_controller
    active.waiting_since['cp-2'] = 100
    station = {'id':'cp-2', 'connected':True, 'online':True, 'state':state,
               'currents_a':[5.9]*3}
    assert active.waiting_cap(station, 131) is None
    assert station['waiting_reclaimed'] is False


def test_learned_vehicle_demand_does_not_oscillate_with_hardware_limit(live_controller):
    active, clock, devices = live_controller
    devices['192.168.1.71']['demand'] = 15.4

    async def scenario():
        for second in range(100, 222, 2):
            clock.now = second
            await active.tick()
            if second >= 130:
                assert active.applied_limits == {'cp-1':16, 'cp-2':0}
                assert active.state['stations'][0]['current_a'] == 15.4

    asyncio.run(scenario())


@pytest.mark.parametrize('reason', ['more_demand', 'periodic_probe', 'new_session'])
def test_learned_demand_can_increase_again(live_controller, reason):
    active, _clock, _devices = live_controller
    active.applied_limits['cp-1'] = 20
    active.limit_changed_at['cp-1'] = 70
    active.underuse_since['cp-1'] = 80
    station = {'id':'cp-1', 'state':3, 'currents_a':[15.4]*3, 'max_current_a':20}
    assert active.adaptive_cap(station, 100) == 16
    active.applied_limits['cp-1'] = 16
    active.limit_changed_at['cp-1'] = 100
    assert active.adaptive_cap(station, 102) == 16
    assert active.adaptive_cap(station, 130) == 16

    if reason == 'more_demand':
        station['currents_a'] = [15.8]*3
        assert active.adaptive_cap(station, 132) == 20
    elif reason == 'periodic_probe':
        assert active.adaptive_cap(station, 100+active.settings.rotation_seconds) == 20
    else:
        station['state'] = 2
        assert active.adaptive_cap(station, 132) == 20
        station['state'] = 3
        assert active.adaptive_cap(station, 134) == 20
