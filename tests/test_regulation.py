import random

import pytest

from loadmanager.allocator import allocate
from loadmanager.models import Settings
from loadmanager.regulation import SmoothRegulation


def station(key='a', **patch):
    return dict(id=key, online=True, connected=True, paused=False, priority='normal',
                max_current_a=32, phases=[0, 1, 2], state=3, **patch)


def regulate(regulator, now, previous, building=10, stations=None, settings=None, **kwargs):
    stations = stations or [station()]
    settings = settings or Settings()
    targets = allocate(stations, [building]*3, building*0.69, settings, now, previous=previous)
    return regulator.apply(stations, targets, previous, [building]*3, building*0.69,
                           settings, now, **kwargs)


def test_brief_peak_uses_reserve_without_stopping_six_amp_session():
    regulator = SmoothRegulation()
    grants = {'a':6}
    for t in range(20):
        # Soft budget falls below 6 A, but 6 A still fits the hard connection.
        grants = regulate(regulator, t, grants, building=28 if 5 <= t <= 10 else 27)
        assert grants == {'a':6}


def test_sustained_peak_reduces_smoothly_but_hard_limit_overrides_timer():
    regulator = SmoothRegulation()
    grants = {'a':20}
    for t in range(8):
        grants = regulate(regulator, t, grants, building=14)
        assert grants == {'a':20}
    assert regulate(regulator, 8, grants, building=14) == {'a':19}
    # A genuine load step may never wait for a smoothing timer.
    assert regulate(regulator, 9, grants, building=27) == {'a':8}
    assert regulate(regulator, 10, {'a':8}, building=30) == {'a':0}


def test_increases_are_one_amp_per_interval_and_require_stable_headroom():
    regulator = SmoothRegulation()
    grants = {'a':10}
    last_increase = 0
    for t in range(61):
        new = regulate(regulator, t, grants, building=5)
        if new['a'] > grants['a']:
            assert new['a'] == grants['a']+1
            assert t-last_increase >= 10
            last_increase = t
        grants = new
    assert 14 <= grants['a'] <= 16


def test_no_on_off_chatter_around_minimum_and_restart_delay():
    regulator = SmoothRegulation()
    grants = regulate(regulator, 0, {'a':6}, building=30)
    assert grants == {'a':0}
    for t in range(1, 30):
        grants = regulate(regulator, t, grants, building=25)
        assert grants == {'a':0}
    assert regulate(regulator, 30, grants, building=25) == {'a':6}
    # Headroom of only 6 A does not restart a session into another immediate stop.
    regulator = SmoothRegulation()
    for t in range(200):
        assert regulate(regulator, t, {'a':0}, building=27) == {'a':0}


def test_rotation_never_replaces_running_session_with_waiter():
    settings = Settings(rotation_seconds=30)
    stations = [station('a'), station('b')]
    previous = {'a':6, 'b':0}
    for t in range(0, 601, 15):
        assert allocate(stations, [26]*3, 26*0.69, settings, t, previous=previous) == {'a':7, 'b':0}


def test_leftover_amp_does_not_rotate_between_running_sessions():
    settings = Settings(rotation_seconds=30)
    stations = [station('a'), station('b')]
    for t in range(0, 601, 15):
        assert allocate(stations, [20]*3, 20*0.69, settings, t,
                        previous={'a':6, 'b':7}) == {'a':6, 'b':7}


def test_only_necessary_session_stops_and_pause_is_immediate():
    regulator = SmoothRegulation()
    stations = [station('a'), station('b')]
    grants = regulate(regulator, 0, {'a':6, 'b':6}, building=25, stations=stations)
    assert sorted(grants.values()) == [0, 6]
    settings = Settings(paused=True)
    assert regulate(regulator, 1, grants, building=0, stations=stations, settings=settings) == {'a':0, 'b':0}


def test_hold_never_increases_and_fault_always_stops():
    regulator = SmoothRegulation()
    for t in range(20):
        assert regulate(regulator, t, {'a':10}, building=0, hold=True) == {'a':10}
    assert regulate(regulator, 20, {'a':10}, building=0, fault=True) == {'a':0}


def test_random_load_traces_respect_power_phases_and_hardware():
    rng = random.Random(217)
    regulator = SmoothRegulation()
    settings = Settings(power_limit_kw=18, phase_limit_a=30)
    stations = [station(str(i)) for i in range(4)]
    stations[0]['max_current_a'] = 16
    stations[1]['phases'] = [0]
    grants = dict.fromkeys([s['id'] for s in stations], 0)
    for t in range(1200):
        building = [rng.uniform(0, 29) for _ in range(3)]
        kw = sum(building)*0.2
        volts = [250, 231, 225]
        targets = allocate(stations, building, kw, settings, t, voltages_v=volts, previous=grants)
        grants = regulator.apply(stations, targets, grants, building, kw, settings, t, volts)
        assert kw+sum(grants[s['id']]*sum(max(230, volts[p]) for p in s['phases']) for s in stations)/1000 <= 18+1e-8
        for p in range(3):
            assert building[p]+sum(grants[s['id']] for s in stations if p in s['phases']) <= 30+1e-8
        for s in stations:
            assert grants[s['id']] == 0 or 6 <= grants[s['id']] <= s['max_current_a']


def test_ten_percent_power_buffer_does_not_relax_phase_limit():
    regulator = SmoothRegulation()
    settings = Settings(power_limit_kw=10, power_tolerance_pct=10)
    # 14 A + 1.0 kW building = 10.66 kW: absorb a short peak, then reduce.
    assert regulate(regulator, 0, {'a':14}, building=1/0.69, settings=settings) == {'a':14}
    assert regulate(regulator, 8, {'a':14}, building=1/0.69, settings=settings) == {'a':13}
    settings.phase_limit_a = 14
    assert regulate(regulator, 9, {'a':14}, building=1/0.69, settings=settings) == {'a':12}
    assert regulate(regulator, 10, {'a':14}, building=4/0.69, settings=Settings(
        power_limit_kw=10, power_tolerance_pct=10)) == {'a':10}


def test_measured_headroom_prevents_reactive_feedback_oscillation():
    """Similar to the real 20–25 A oscillation at only 16–18 kW grid draw."""
    from loadmanager.shelly import building_phase_currents
    settings = Settings()
    regulator = SmoothRegulation()
    grants = {'a':22}
    changes = []
    for t in range(180):
        measured = grants['a']-0.3
        # Small power-factor noise used to create huge swings in the derived
        # RMS building bound, even though the mains had ample actual headroom.
        pf = 0.98 if t % 2 else 0.995
        s = station()
        s.update(currents_a=[measured]*3, power_kw=measured*0.69*pf)
        meter = {'currents_a':[measured+5, measured+3, measured+1], 'voltages_v':[230]*3,
                 'power_kw':s['power_kw']+2, 'phase_powers_kw':[(measured+v)*0.23 for v in (5,3,1)]}
        assert max(building_phase_currents(meter, [s])) > 5
        targets = allocate([s], [5,3,1], 2, settings, t, previous=grants)
        new = regulator.apply([s], targets, grants, [5,3,1], 2, settings, t, meter=meter)
        if new != grants:
            changes.append((t, new['a']))
        assert new['a'] >= grants['a']  # Noise never makes it oscillate down.
        assert meter['currents_a'][0]+max(0, new['a']-measured) <= settings.phase_limit_a
        grants = new
    assert grants['a'] >= 28
    assert all(changes[i][0]-changes[i-1][0] >= 10 for i in range(1,len(changes)))


def test_reductions_cannot_finance_increases_before_meter_catches_up():
    regulator = SmoothRegulation()
    settings = Settings(phase_limit_a=30)
    a, b = station('a'), station('b')
    a.update(currents_a=[20]*3, power_kw=13.8)
    b.update(currents_a=[0]*3, power_kw=0)
    meter = {'currents_a':[29]*3, 'power_kw':20.01}
    grants = regulator.apply([a,b], {'a':14,'b':6}, {'a':14,'b':0}, [9]*3, 6.21,
                             settings, 100, meter=meter)
    assert grants == {'a':14,'b':0}
