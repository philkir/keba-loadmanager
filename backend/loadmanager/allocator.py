"""Deterministic, phase-aware allocation. No I/O, database or wallbox side effects."""
import math


def allocate(stations, building_a, building_kw, settings, now, *, voltages_v=None, previous=None):
    grants = {s['id']: 0 for s in stations}
    voltages = [max(230, v) for v in voltages_v] if voltages_v is not None else [230]*3
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
                # Never rotate charging sessions out of their minimum grant.
                shift = 0 if previous is not None and rank >= 2 else slot % len(group)
                order += group[shift:] + group[:shift]
    if previous is not None:
        order.sort(key=lambda s: previous.get(s['id'], 0) < 6)

    def fits(s, delta):
        return (all(phase_budget[p] + 1e-8 >= delta for p in s['phases'])
                and power_budget + 1e-8 >= delta * sum(voltages[p] for p in s['phases']))

    def assign(s, delta):
        nonlocal power_budget
        grants[s['id']] += delta
        for p in s['phases']:
            phase_budget[p] -= delta
        power_budget -= delta * sum(voltages[p] for p in s['phases'])

    # Admit at 6 A first, then distribute remaining amps evenly.
    for s in order:
        if s['max_current_a'] >= 6 and fits(s, 6):
            assign(s, 6)
    while True:
        changed = False
        # Keep a leftover amp at its current station instead of moving it on
        # each rotation boundary. Equal grants still share larger budgets.
        distribution = sorted(order, key=lambda s: grants[s['id']]) if previous is None else sorted(
            order, key=lambda s: (grants[s['id']], grants[s['id']] >= previous.get(s['id'], 0)))
        for s in distribution:
            if 0 < grants[s['id']] < s['max_current_a'] and fits(s, 1):
                assign(s, 1)
                changed = True
        if not changed:
            return grants
