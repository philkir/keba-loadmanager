"""Stateful current changes; raw limits always override comfort timers."""
import math


class SmoothRegulation:
    reduction_delay_s = 8
    reduction_step_s = 5

    def __init__(self):
        self.up_since = {}
        self.down_since = {}
        self.changed_at = {}
        self.stopped_at = {}
        self.paused = set()

    def apply(self, stations, targets, previous, building_a, building_kw, settings,
              now, voltages_v=None, *, meter=None, hold=False, fault=False):
        voltages = [max(230, v) for v in (voltages_v or [230]*3)]
        grants = {}
        reasons = {}
        power_ceiling = settings.power_limit_kw*(1+settings.power_tolerance_pct/100)

        def fits(values, reserve):
            return (max(0, building_kw)+sum(values[s['id']]*sum(voltages[p] for p in s['phases'])
                                          for s in stations)/1000 <= (power_ceiling if reserve == 0 else settings.power_limit_kw-reserve)+1e-8
                    and all(building_a[p]+sum(values[s['id']] for s in stations if p in s['phases'])
                            <= settings.phase_limit_a-reserve/0.69+1e-8 for p in range(3)))

        def increase_fits(values):
            if not fits(values, settings.reserve_kw):
                return False
            if meter is None:
                return True
            # Use the grid reading plus only positive changes in EV current.
            # A commanded reduction is NOT freed capacity until measured. The
            # triangle inequality bounds added current even with reactive load;
            # RMS subtraction here never claims a physical building current.
            return (all(meter['currents_a'][p]+sum(max(0, values[s['id']]-s['currents_a'][p])
                        for s in stations if p in s['phases']) <= settings.phase_limit_a-settings.reserve_kw/0.69+1e-8
                        for p in range(3))
                    and max(0, meter['power_kw'])+sum(max(0, values[s['id']]*sum(voltages[p] for p in s['phases'])/1000-s['power_kw'])
                        for s in stations) <= settings.power_limit_kw-settings.reserve_kw+1e-8)

        for s in stations:
            key = s['id']
            old = previous.get(key, 0)
            manual_pause = settings.paused or s['paused'] or not s['connected']
            if not manual_pause and key in self.paused:
                self.stopped_at.pop(key, None)  # Explicit resume need not wait.
            if manual_pause:
                self.paused.add(key)
            else:
                self.paused.discard(key)
            maximum = min(32, math.floor(s['max_current_a']))
            allowed = not (manual_pause or fault or not s['online'] or s.get('error_code') or s.get('state') == 4)
            grants[key] = min(old, maximum) if allowed and maximum >= 6 else 0
            reasons[key] = 'Messlücke: Freigabe gehalten' if hold else 'Freigabe stabil'
            if not allowed:
                reasons[key] = 'Sicherer Halt' if fault else 'Pause oder Ladepunkt nicht bereit'
                self.up_since.pop(key, None)
                self.down_since.pop(key, None)
                continue
            if hold:
                self.up_since.pop(key, None)
                continue
            target = targets[key]
            if target < old:
                self.up_since.pop(key, None)
                since = self.down_since.setdefault(key, now)
                if (now-since >= self.reduction_delay_s
                        and now-self.changed_at.get(key, float('-inf')) >= self.reduction_step_s):
                    grants[key] = min(grants[key], old-1 if old > 6 else 0)
                    reasons[key] = 'Last anhaltend erhöht: sanft reduziert'
                else:
                    reasons[key] = 'Kurze Laständerung: Regelreserve puffert'
            else:
                self.down_since.pop(key, None)
                if target <= old:
                    self.up_since.pop(key, None)

        # Starting needs 1 A extra headroom; running sessions need only 0.25 A
        # above the reserve, so they can approach the target without chatter.
        for s in stations:
            key = s['id']
            old, target = previous.get(key, 0), targets[key]
            if (fault or hold or settings.paused or s['paused'] or not s['online']
                    or not s['connected'] or s.get('error_code') or s.get('state') == 4
                    or target <= old or s['max_current_a'] < 6):
                continue
            since = self.up_since.setdefault(key, now)
            if old == 0:
                if now-self.stopped_at.get(key, float('-inf')) < settings.restart_delay_seconds:
                    reasons[key] = 'Wiederanlaufpause'
                    continue
                proposed = 6
            elif now-since >= settings.ramp_up_seconds and now-self.changed_at.get(key, float('-inf')) >= settings.ramp_up_seconds:
                proposed = min(target, old+1)
            else:
                reasons[key] = 'Freies Budget wird bestätigt'
                continue
            trial = {**grants, key:proposed+(1 if old == 0 else 0.25)}
            if increase_fits(trial):
                grants[key] = proposed
                reasons[key] = 'Start mit 6 A' if old == 0 else 'Sanft erhöht: +1 A'
            else:
                self.up_since.pop(key, None)
                reasons[key] = 'Wartet auf ausreichende Reserve'

        # No low-pass filter or timer may conceal an actual limit violation.
        # Shed excess amps first, then as few whole sessions as necessary.
        while not fits(grants, 0):
            overloaded_phases = [p for p in range(3) if building_a[p]+sum(
                grants[s['id']] for s in stations if p in s['phases']) > settings.phase_limit_a+1e-8]
            choices = [s for s in stations if grants[s['id']] > 0
                       and (not overloaded_phases or any(p in s['phases'] for p in overloaded_phases))]
            if not choices:
                break  # Building load alone exceeds the connection limit.
            extras = [s for s in choices if grants[s['id']] > 6]
            selected = max(extras or choices, key=lambda s: (
                s['priority'] != 'high', s.get('state') != 3, grants[s['id']], s['id']))
            key = selected['id']
            grants[key] = grants[key]-1 if grants[key] > 6 else 0
            reasons[key] = 'Anschlussgrenze: sofort reduziert'

        for s in stations:
            key = s['id']
            old = previous.get(key, 0)
            if grants[key] != old:
                self.changed_at[key] = now
                self.up_since.pop(key, None)
            if old >= 6 and grants[key] == 0:
                self.stopped_at[key] = now
            s['control_reason'] = reasons[key]
            s['target_current_a'] = targets[key]
        return grants
