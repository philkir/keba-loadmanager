"""Deterministic, phase-aware allocation. No I/O, database or wallbox side effects."""
import math


def allocate(stations, building_a, building_kw, settings, now):
    grants = {s['id']: 0 for s in stations}
    if settings.paused or not all(math.isfinite(v) and v >= 0 for v in [*building_a, building_kw]):
        return grants
    # Reserve is kept both against active power and symmetrically on all phases.
    phase_budget = [max(0, settings.phase_limit_a - x - settings.reserve_kw * 1000 / 690) for x in building_a]
    power_budget = max(0, (settings.power_limit_kw - settings.reserve_kw - building_kw) * 1000)
    eligible = [s for s in stations if s['connected'] and s['online'] and not s['paused']
                and s['max_current_a'] >= 6]
    # Round-robin within each priority tier; minimum dwell set by rotation_seconds.
    order = []
    slot = int(now // settings.rotation_seconds)
    for priority in ['high', 'normal']:
        priority_group = [s for s in eligible if s['priority'] == priority]
        # A vehicle that is already drawing power wins over a merely plugged-in
        # vehicle. Rotation still applies between stations with equal demand.
        for rank in sorted({s.get('allocation_rank', 1) for s in priority_group}, reverse=True):
            group = [s for s in priority_group if s.get('allocation_rank', 1) == rank]
            if group:
                shift = slot % len(group)
                order += group[shift:] + group[:shift]

    def fits(s, delta):
        return (all(phase_budget[p] + 1e-8 >= delta for p in s['phases'])
                and power_budget + 1e-8 >= delta * 230 * len(s['phases']))

    def assign(s, delta):
        nonlocal power_budget
        grants[s['id']] += delta
        for p in s['phases']:
            phase_budget[p] -= delta
        power_budget -= delta * 230 * len(s['phases'])

    # Admit at 6 A first, then distribute remaining amps evenly.
    for s in order:
        if s['max_current_a'] >= 6 and fits(s, 6):
            assign(s, 6)
    while True:
        changed = False
        for s in order:
            if 0 < grants[s['id']] < s['max_current_a'] and fits(s, 1):
                assign(s, 1)
                changed = True
        if not changed:
            return grants
