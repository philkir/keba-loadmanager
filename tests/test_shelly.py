import asyncio
import math
import random
import struct
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from loadmanager import controller, shelly
from loadmanager.allocator import allocate
from loadmanager.models import MeterSettings, Settings
from loadmanager.storage import Store


def registers():
    values = [0]*80
    # Literal CDAB words for timestamp 0x6380b3cd, 230 V, 10 A,
    # 2300 W per phase and 6900 W total.
    values[:2] = [0xb3cd, 0x6380]
    values[13:15] = [0xa000, 0x45d7]
    for base in (20, 40, 60):
        values[base:base+2] = [0, 0x4366]
        values[base+2:base+4] = [0, 0x4120]
        values[base+4:base+6] = [0xc000, 0x450f]
    return values


def adapter(monkeypatch, values):
    client = SimpleNamespace(connect=AsyncMock(return_value=True), close=Mock(),
                             read_input_registers=AsyncMock(return_value=SimpleNamespace(
                                 isError=lambda: False, registers=values)))
    constructor = Mock(return_value=client)
    monkeypatch.setattr(shelly, 'AsyncModbusTcpClient', constructor)
    device = shelly.ShellyPro3EM('192.168.1.217', port=1502, device_id=7)
    return device, client, constructor


def test_shelly_uses_input_offsets_and_low_word_first(monkeypatch):
    device, client, constructor = adapter(monkeypatch, registers())
    data = asyncio.run(device.telemetry())
    constructor.assert_called_once_with('192.168.1.217', port=1502, timeout=1, retries=0)
    client.read_input_registers.assert_awaited_once_with(1000, count=80, device_id=7)
    assert data == {'timestamp':0x6380b3cd, 'power_kw':6.9,
                    'currents_a':[10]*3, 'voltages_v':[230]*3,
                    'phase_powers_kw':[2.3]*3}


@pytest.mark.parametrize('offset,value', [(22, float('nan')), (42, float('inf')),
                                         (62, -1), (22, 121), (20, 0), (40, 281),
                                         (24, 5000), (13, 0)])
def test_invalid_measurements_are_rejected(monkeypatch, offset, value):
    values = registers()
    high, low = struct.unpack('>HH', struct.pack('>f', value))
    values[offset:offset+2] = [low, high]
    device, _, _ = adapter(monkeypatch, values)
    with pytest.raises(ValueError):
        asyncio.run(device.telemetry())


@pytest.mark.parametrize('offset', [2, 3, 4, 6, 30, 31, 32, 50, 51, 52, 70, 71, 72])
def test_phase_errors_stop_metering(monkeypatch, offset):
    values = registers()
    values[offset] = 1
    device, _, _ = adapter(monkeypatch, values)
    with pytest.raises(ValueError, match='Phasen- oder Messfehler'):
        asyncio.run(device.telemetry())


def test_optional_neutral_ct_is_not_required(monkeypatch):
    values = registers()
    values[5] = 1
    values[7:9] = [0, 0x7fc0]  # NaN for the unconnected neutral CT.
    device, _, _ = adapter(monkeypatch, values)
    assert asyncio.run(device.telemetry())['power_kw'] == 6.9


@pytest.mark.parametrize('failure', ['short', 'exception', 'connection'])
def test_modbus_failures_are_not_zero_measurements(monkeypatch, failure):
    device, client, _ = adapter(monkeypatch, registers()[:10] if failure == 'short' else registers())
    if failure == 'connection':
        client.connect.return_value = False
        with pytest.raises(ConnectionError):
            asyncio.run(device.connect())
    else:
        if failure == 'exception':
            client.read_input_registers.return_value = SimpleNamespace(isError=lambda: True)
        with pytest.raises(IOError):
            asyncio.run(device.telemetry())


def test_export_remains_signed(monkeypatch):
    values = registers()
    for offset in (13, 24, 44, 64):
        values[offset+1] |= 0x8000
    device, _, _ = adapter(monkeypatch, values)
    assert asyncio.run(device.telemetry())['power_kw'] == -6.9


@pytest.mark.parametrize('value', [{'enabled':True, 'host':''}, {'host':'8.8.8.8'},
                                  {'host':'not-an-ip'}, {'port':0}, {'port':65536},
                                  {'device_id':0}, {'device_id':256}, {'unknown':True}])
def test_meter_config_validation(value):
    with pytest.raises(ValidationError):
        MeterSettings.model_validate(value)


def test_old_settings_migrate_to_disabled_meter():
    settings = Settings.model_validate({'site_name':'Bestehender Ladepark'})
    assert settings.meter == MeterSettings(enabled=False, host='192.168.1.217', port=502, device_id=1)


def meter_data(currents=(10, 10, 10), active=None, timestamp=None):
    active = currents if active is None else active
    return {'timestamp':time.time() if timestamp is None else timestamp,
            'power_kw':sum(active)*0.23, 'currents_a':list(currents),
            'voltages_v':[230]*3, 'phase_powers_kw':[i*0.23 for i in active]}


def test_building_currents_remove_ev_but_keep_reactive_load_and_export():
    station = {'currents_a':[10]*3, 'power_kw':6.9}
    assert shelly.building_phase_currents(meter_data([20, 15, 12]), [station]) == pytest.approx([10, 5, 2])
    # 10 A active EV plus 10 A reactive building: magnitude subtraction would
    # leave only 4.14 A, incorrectly freeing almost 6 A on every phase.
    assert shelly.building_phase_currents(meter_data([math.sqrt(200)]*3, [10]*3), [station]) == pytest.approx([10]*3)
    # 10 A EV at a site exporting 5 A means the building exports 15 A.
    assert shelly.building_phase_currents(meter_data([5]*3, [-5]*3), [station]) == pytest.approx([15]*3)


def test_unknown_ev_power_factor_does_not_create_headroom():
    # Opposite reactive currents can cancel at the site meter. Retain a
    # conservative bound on the building current when KEBA only has total W.
    station = {'currents_a':[10]*3, 'power_kw':3.45}
    result = shelly.building_phase_currents(meter_data([5]*3), [station])
    assert all(value >= math.sqrt(75) for value in result)


def test_phase_bound_covers_asymmetric_import_export_and_reactive_currents():
    rng = random.Random(217)
    for _ in range(100):
        building = [complex(rng.uniform(-20, 20), rng.uniform(-10, 10)) for _ in range(3)]
        evs = [[complex(rng.uniform(0, 16), rng.uniform(-3, 3)) for _ in range(3)] for _ in range(2)]
        grid = [building[p]+sum(ev[p] for ev in evs) for p in range(3)]
        data = meter_data([abs(i) for i in grid], [i.real for i in grid])
        stations = [{'currents_a':[abs(i) for i in ev], 'power_kw':sum(i.real for i in ev)*0.23} for ev in evs]
        bound = shelly.building_phase_currents(data, stations)
        assert all(bound[p]+1e-8 >= abs(building[p]) for p in range(3))


@pytest.fixture
def metered_controller(tmp_path, monkeypatch):
    store = Store(tmp_path/'shelly.sqlite')
    active = controller.ActiveController(store)
    active.settings.meter = MeterSettings(enabled=True)
    active.station_config = active.station_config[:2]
    measurement = SimpleNamespace(data=meter_data([20, 15, 12]), fail=False, closed=0, endpoints=[])

    class Meter:
        def __init__(self, host, **kwargs):
            measurement.endpoints.append((host, kwargs))

        async def connect(self):
            if measurement.fail:
                raise ConnectionError('Shelly nicht erreichbar')

        async def telemetry(self):
            return measurement.data.copy()

        def close(self):
            measurement.closed += 1

    async def probe(setting):
        amps = 10 if setting['id'] == 'cp-1' else 0
        return {**setting, 'online':True, 'network_online':True, 'connected':True,
                'currents_a':[amps]*3, 'power_kw':amps*0.69, 'phases':[0, 1, 2],
                'state':3 if amps else 2, 'status':'charging' if amps else 'waiting'}

    async def apply_limit(setting, amps):
        active.applied_limits[setting['id']] = amps
        return None

    monkeypatch.setattr(controller, 'ShellyPro3EM', Meter)
    active.probe = AsyncMock(side_effect=probe)
    active.apply_limit = AsyncMock(side_effect=apply_limit)
    try:
        yield active, measurement
    finally:
        store.db.close()


def test_main_meter_is_not_counted_twice_and_limits_each_phase(metered_controller):
    active, measurement = metered_controller
    asyncio.run(active.tick())
    state = active.state
    assert state['building_source'] == 'meter'
    assert state['meter_online'] is True
    assert state['building_kw'] == pytest.approx(3.91)
    assert state['charging_kw'] == pytest.approx(6.9)
    assert state['total_kw'] == pytest.approx(10.81)
    assert state['phase_currents_a'] == [20, 15, 12]
    assert state['building_currents_a'] == pytest.approx([10, 5, 2])
    granted = sum(active.applied_limits.values())
    assert granted == 12  # Both admissions start at 6 A; later ticks may increase.
    assert granted+10+active.settings.reserve_kw/0.69 <= active.settings.phase_limit_a
    assert state['building_kw']+granted*0.69+active.settings.reserve_kw <= active.settings.power_limit_kw
    assert measurement.closed == 1
    assert active.store.history()[-1]['total_kw'] == pytest.approx(10.81)


def test_export_is_visible_but_does_not_increase_the_power_budget(metered_controller):
    active, measurement = metered_controller
    measurement.data = meter_data([5]*3, [-5]*3)
    asyncio.run(active.tick())
    assert active.state['total_kw'] == -3.45
    assert active.state['building_kw'] == -10.35
    assert active.state['headroom_kw'] == 17.1
    assert active.store.history()[-1]['total_kw'] == pytest.approx(-3.45)
    assert active.state['estimated_charging_kw'] <= active.settings.power_limit_kw-active.settings.reserve_kw


def test_meter_gap_is_bridged_then_stops_and_recovers(metered_controller, monkeypatch):
    active, measurement = metered_controller
    clock = SimpleNamespace(now=100.)
    epoch = measurement.data['timestamp']-clock.now
    monkeypatch.setattr(controller, 'time', SimpleNamespace(
        time=lambda:epoch+clock.now, monotonic=lambda:clock.now))
    asyncio.run(active.tick())
    previous = active.applied_limits.copy()
    measurement.fail = True
    for second in [102, 104]:
        clock.now = second
        active.last_sample = 0
        asyncio.run(active.tick())
        assert active.applied_limits == previous
        assert active.state['status'] == 'holding'
        assert active.state['meter_online'] is False
        assert active.state['meter']['currents_a'] == measurement.data['currents_a']
        assert len(active.store.history()) == 1  # Cached values are never history.
    clock.now = 106
    asyncio.run(active.tick())
    assert set(active.applied_limits.values()) == {0}
    assert active.state['status'] == 'safe'
    assert active.state['building_source'] == 'unavailable'
    assert 'Shelly nicht erreichbar' in active.state['measurement_error']
    measurement.fail = False
    clock.now = 108
    measurement.data['timestamp'] = epoch+clock.now
    asyncio.run(active.tick())
    assert active.applied_limits['cp-1'] == 0  # Restart hysteresis after a real stop.
    clock.now = 137
    measurement.data['timestamp'] = epoch+clock.now
    asyncio.run(active.tick())
    assert active.state['status'] == 'active'
    assert active.applied_limits['cp-1'] == 6
    assert any('Sicherer Halt' in event['message'] for event in active.store.events())


@pytest.mark.parametrize('age', [6, 100, -10])
def test_stale_or_future_device_time_stops_on_first_read(metered_controller, age):
    active, measurement = metered_controller
    measurement.data['timestamp'] = time.time()-age
    asyncio.run(active.tick())
    assert active.state['status'] == 'safe'
    assert set(active.applied_limits.values()) == {0}
    assert active.store.history() == []


def test_frozen_timestamp_is_rejected_independently_of_wall_clock(metered_controller, monkeypatch):
    active, measurement = metered_controller
    clock = SimpleNamespace(now=100.)
    fixed = measurement.data['timestamp']
    monkeypatch.setattr(controller, 'time', SimpleNamespace(time=lambda:fixed, monotonic=lambda:clock.now))
    asyncio.run(active.tick())
    clock.now += 6
    asyncio.run(active.tick())
    assert active.state['status'] == 'safe'
    assert 'nicht aktualisiert' in active.state['measurement_error']


def test_slow_wallbox_reads_cannot_reuse_a_now_stale_meter(metered_controller, monkeypatch):
    active, measurement = metered_controller
    clock = SimpleNamespace(now=measurement.data['timestamp'])
    monkeypatch.setattr(controller, 'time', SimpleNamespace(time=lambda:clock.now, monotonic=time.monotonic))
    original = active.probe.side_effect

    async def delayed_probe(setting):
        station = await original(setting)
        clock.now = measurement.data['timestamp']+6
        return station

    active.probe.side_effect = delayed_probe
    asyncio.run(active.tick())
    assert active.state['status'] == 'safe'
    assert set(active.applied_limits.values()) == {0}


def test_main_meter_is_read_after_wallboxes_so_delay_does_not_age_it(metered_controller, monkeypatch):
    active, measurement = metered_controller
    clock = SimpleNamespace(now=measurement.data['timestamp'])
    monkeypatch.setattr(controller, 'time', SimpleNamespace(time=lambda:clock.now, monotonic=time.monotonic))
    original = active.probe.side_effect

    async def delayed(setting):
        station = await original(setting)
        clock.now += 3
        measurement.data['timestamp'] = clock.now
        return station

    active.probe.side_effect = delayed
    asyncio.run(active.tick())
    assert active.state['meter_online']
    assert active.state['timestamp'] == clock.now
    assert active.state['meter_timestamp'] == clock.now


def test_invalid_meter_values_never_use_communication_gap_tolerance(metered_controller, monkeypatch):
    active, _ = metered_controller
    asyncio.run(active.tick())
    monkeypatch.setattr(controller.ShellyPro3EM, 'telemetry', AsyncMock(side_effect=ValueError('Phasenfehler')))
    asyncio.run(active.tick())
    assert active.state['status'] == 'safe'
    assert set(active.applied_limits.values()) == {0}


def test_meter_timeout_is_bounded_and_closes_connection(metered_controller, monkeypatch):
    active, measurement = metered_controller

    async def stuck(_self):
        await asyncio.sleep(10)

    monkeypatch.setattr(controller.ShellyPro3EM, 'telemetry', stuck)

    async def scenario():
        await asyncio.wait_for(active.tick(), timeout=3)

    asyncio.run(scenario())
    assert active.state['status'] == 'safe'
    assert measurement.closed == 1


def test_missing_wallbox_measurement_stops_even_with_meter_online(metered_controller):
    active, _ = metered_controller
    original_probe = active.probe.side_effect

    async def offline_probe(setting):
        station = await original_probe(setting)
        if setting['id'] == 'cp-2':
            station['online'] = False
        return station

    active.probe.side_effect = offline_probe
    asyncio.run(active.tick())
    assert active.state['meter_online'] is True
    assert active.state['status'] == 'safe'
    assert active.state['building_source'] == 'unavailable'
    assert all(call.args[1] == 0 for call in active.apply_limit.await_args_list)


def test_disabled_meter_never_connects_and_keeps_fallback(metered_controller):
    active, measurement = metered_controller
    active.settings.meter.enabled = False
    measurement.fail = True
    asyncio.run(active.tick())
    assert measurement.endpoints == []
    assert active.state['building_source'] == 'fallback'
    assert active.state['building_kw'] == active.settings.fallback_building_kw
    assert active.state['status'] == 'active'


def test_config_is_persisted_reloaded_and_new_endpoint_used(metered_controller):
    active, measurement = metered_controller
    settings = active.settings.model_dump()
    settings['meter'] = {'enabled':True, 'host':'192.168.1.218', 'port':1502, 'device_id':7}
    command = {'id':str(uuid.uuid4()), 'issued_at':time.time(), 'kind':'settings', 'value':settings}
    asyncio.run(active.command(command))
    assert measurement.endpoints[-1] == ('192.168.1.218', {'port':1502, 'device_id':7})
    assert controller.ActiveController(active.store).settings.meter.model_dump() == settings['meter']
    asyncio.run(active.command(command))
    assert len(measurement.endpoints) == 1  # Replayed command is idempotent.


def test_commissioning_reads_meter_without_writing_limits(metered_controller):
    active, _ = metered_controller
    commissioning = controller.CommissioningController(active.store)
    commissioning.settings = active.settings
    commissioning.station_config = active.station_config
    commissioning.probe = active.probe
    asyncio.run(commissioning.tick())
    assert commissioning.state['meter_online'] is True
    assert commissioning.state['total_kw'] == pytest.approx(10.81)
    active.apply_limit.assert_not_called()


def test_simulation_never_connects_to_configured_meter(metered_controller):
    active, measurement = metered_controller
    simulation = controller.Controller(active.store)
    simulation.settings = active.settings
    asyncio.run(simulation.tick())
    assert simulation.state['mode'] == 'simulation'
    assert measurement.endpoints == []


def test_redistribution_waits_for_measured_current_to_drop(metered_controller):
    active, measurement = metered_controller
    active.applied_limits = {'cp-1':10, 'cp-2':0}
    active.station_config[0]['paused'] = True
    measurement.data = meter_data([33,28,25])
    asyncio.run(active.tick())
    assert active.applied_limits == {'cp-1':0, 'cp-2':0}
    original = active.probe.side_effect

    async def stopped_probe(setting):
        station = await original(setting)
        station.update(currents_a=[0]*3, power_kw=0)
        return station

    active.probe.side_effect = stopped_probe
    measurement.data = meter_data([23,18,15])
    asyncio.run(active.tick())
    assert active.applied_limits['cp-2'] >= 6


def test_high_voltage_counts_toward_power_limit():
    settings = Settings(power_limit_kw=10, reserve_kw=1)
    stations = [{'id':'cp-1', 'connected':True, 'online':True, 'paused':False,
                 'priority':'normal', 'phases':[0, 1, 2], 'max_current_a':32}]
    grants = allocate(stations, [0]*3, 0, settings, 0, voltages_v=[250]*3)
    assert grants == {'cp-1':12}
