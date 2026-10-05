"""Read-only Shelly Pro 3EM 120A (including v2), triphase profile.

https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/Modbus/
https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/EM/
Shelly's 31000 notation means FC04 input offset 1000. 32-bit values use
big-endian bytes within each register and the low word first (CDAB).
"""
import math
import struct

from pymodbus.client import AsyncModbusTcpClient


class ShellyPro3EM:
    def __init__(self, host, *, port=502, device_id=1):
        self.client = AsyncModbusTcpClient(host, port=port, timeout=1, retries=0)
        self.device_id = device_id

    async def connect(self):
        if not await self.client.connect():
            raise ConnectionError('Shelly nicht erreichbar')

    async def telemetry(self):
        result = await self.client.read_input_registers(1000, count=80, device_id=self.device_id)
        if result.isError() or len(result.registers) != 80:
            raise IOError('Shelly-EM-Register nicht lesbar; Modbus und Profil triphase prüfen')
        registers = result.registers

        def float32(offset):
            value = struct.unpack('>f', struct.pack('>HH', registers[offset+1], registers[offset]))[0]
            if not math.isfinite(value):
                raise ValueError('Shelly liefert ungültige Messwerte')
            return value

        # Neutral current is optional; its absent CT must not invalidate L1–L3.
        if any(registers[offset] for offset in (2, 3, 4, 6, 30, 31, 32, 50, 51, 52, 70, 71, 72)):
            raise ValueError('Shelly meldet einen Phasen- oder Messfehler')
        voltages = [float32(offset) for offset in (20, 40, 60)]
        currents = [float32(offset) for offset in (22, 42, 62)]
        powers = [float32(offset) for offset in (24, 44, 64)]
        total = float32(13)
        for voltage, current, power in zip(voltages, currents, powers):
            if not 1 <= voltage <= 280 or not 0 <= current <= 120:
                raise ValueError('Shelly-Messwerte außerhalb des 120-A-Messbereichs')
            if abs(power) > voltage*current*1.05+10:
                raise ValueError('Shelly-Leistung passt nicht zu Spannung und Strom')
        if abs(total-sum(powers)) > max(30, sum(abs(p) for p in powers)*0.02):
            raise ValueError('Shelly-Gesamtleistung passt nicht zu den Phasenwerten')
        return {
            'timestamp': (registers[1] << 16) | registers[0],
            'power_kw': total/1000,
            'currents_a': currents,
            'voltages_v': voltages,
            'phase_powers_kw': [power/1000 for power in powers],
        }

    def close(self):
        self.client.close()


def building_phase_currents(meter, stations):
    """Conservative RMS bound after removing the measured EV consumption.

    RMS magnitudes cannot simply be subtracted with reactive loads or export.
    KEBA supplies total active power, so bound each phase's active EV current
    by assigning as much of that power as possible to the other phases. Keep
    the worst-case residual reactive current instead of inventing PV headroom.
    All station phases must match the meter's L1/L2/L3 order.
    """
    voltages = meter['voltages_v']
    ev_current = [0.0]*3
    ev_active_min = [0.0]*3
    for station in stations:
        currents = station['currents_a']
        apparent = [v*i for v, i in zip(voltages, currents)]
        power = max(0, station['power_kw']*1000)
        for phase in range(3):
            ev_current[phase] += currents[phase]
            active_min = (power-sum(apparent[p] for p in range(3) if p != phase))/voltages[phase]
            ev_active_min[phase] += min(currents[phase], max(0, active_min))

    result = []
    for phase in range(3):
        grid = meter['currents_a'][phase]
        active = max(-grid, min(grid, meter['phase_powers_kw'][phase]*1000/voltages[phase]))
        reactive = math.sqrt(max(0, grid**2-active**2))
        ev = ev_current[phase]
        ev_min = ev_active_min[phase]
        ev_reactive = math.sqrt(max(0, ev**2-ev_min**2))
        projection = ev_min if active >= 0 else ev
        residual = grid**2+ev**2-2*active*projection+2*reactive*ev_reactive
        result.append(math.sqrt(max(0, residual)))
    return result
