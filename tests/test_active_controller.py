import asyncio

from loadmanager import controller
from loadmanager.storage import Store


class FakeKeba:
    configured = []
    limits = []

    def __init__(self, host, model, **kwargs):
        self.host = host

    async def connect(self):
        return None

    async def telemetry(self):
        return {
            'state': 2, 'cable_state': 5, 'error_code': 0,
            'currents_a': [0, 0, 0], 'serial': 1, 'firmware_raw': 1,
            'power_kw': 0, 'energy_kwh': 0,
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
    assert [amps for _host, amps in FakeKeba.limits] == [0, 0, 0, 0]
    store.db.close()
