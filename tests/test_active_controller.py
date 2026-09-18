import asyncio

from loadmanager import controller
from loadmanager.allocator import allocate
from loadmanager.models import Settings
from loadmanager.storage import Store


class FakeKeba:
    configured = []
    limits = []

    def __init__(self, host, model, **kwargs):
        self.host = host

    async def connect(self):
        return None

    async def telemetry(self, full=False):
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


def test_waiting_vehicle_releases_budget_and_is_probed_again(tmp_path):
    store = Store(tmp_path / 'waiting.sqlite')
    active = controller.ActiveController(store)
    waiting = {'id':'cp-1', 'state':2, 'connected':True}

    assert active.waiting_cap(waiting, 100) == 6
    assert active.waiting_cap(waiting, 131) == 0
    assert waiting['waiting_reclaimed'] is True
    assert active.waiting_cap(
        waiting, 100+active.waiting_grace_s+active.settings.rotation_seconds-5
    ) == 6
    store.db.close()


def test_active_vehicle_gets_reclaimed_capacity(tmp_path):
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

    assert grants == {'cp-1':0, 'cp-2':16}
    store.db.close()
