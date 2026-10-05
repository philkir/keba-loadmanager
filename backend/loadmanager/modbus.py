"""Commissioning adapters. Deliberately NOT wired into the simulation controller.

Register maps: KEBA P30 V1.07, P40 V1.02. Verify against installed firmware.
No RFID authorization or OCPP settings are written by this adapter.
"""
import asyncio
from pymodbus.client import AsyncModbusTcpClient

DETAIL_REGISTERS = [1014,1016,1018,1040,1042,1044,1036,1502,1046,1100,1500,1550,1552,1600,1602]

class KebaModbus:
    def __init__(self, host, model, *, port=502, device_id=255, writes_enabled=False, max_current_a=16):
        if model not in ['P30 x','P40']:raise ValueError('Unbekanntes Modell')
        self.client=AsyncModbusTcpClient(host,port=port,timeout=1,retries=0)
        self.device_id=device_id
        self.model=model
        self.writes_enabled=writes_enabled
        self.max_current_a=max_current_a

    async def read32(self, register):
        result=await self.client.read_holding_registers(register,count=2,device_id=self.device_id)
        if result.isError():raise IOError(f'Modbus-Lesefehler: {register}')
        return (result.registers[0]<<16)|result.registers[1]

    async def write16(self, register, value):
        if not self.writes_enabled:raise PermissionError('Modbus-Schreibzugriff nicht aktiviert')
        result=await self.client.write_register(register,value,device_id=self.device_id)
        if result.isError():raise IOError(f'Modbus-Schreibfehler: {register}')

    async def connect(self):
        if not await self.client.connect():raise ConnectionError('Wallbox nicht erreichbar')

    def firmware_version(self, raw):
        if self.model == 'P40':
            return f'{raw // 10000}.{raw // 100 % 100}.{raw % 100}'
        return '.'.join(str((raw >> shift) & 0xff) for shift in (24, 16, 8))

    async def optional32(self, register):
        try:
            return await self.read32(register)
        except Exception:
            return None

    async def telemetry(self, full=False, detail_registers=None):
        """Read operational data; full adds slower device and capability registers."""
        # Keep telemetry requests on one connection. Failsafe needs explicit writes.
        state = await self.read32(1000)
        data = {'state':state,
                'cable_state':await self.read32(1004),
                'error_code':await self.read32(1006),
                'currents_a':[await self.read32(r)/1000 for r in [1008,1010,1012]],
                'power_kw':await self.read32(1020)/1_000_000,
                'hardware_limit_a':await self.read32(1110)/1000}
        if not full and detail_registers is None:
            return data
        registers = detail_registers if detail_registers is not None else DETAIL_REGISTERS + ([1200,1700,1702] if self.model == 'P40' else [])
        raw = {}
        # Slow optional registers must not block fresh current measurements or
        # watchdog writes. The controller rotates small groups on later ticks.
        try:
            async with asyncio.timeout(0.6 if detail_registers is not None else 20):
                for register in registers:
                    value = await self.optional32(register)
                    if value is not None:
                        raw[register] = value
        except TimeoutError:
            pass
        fields = {
            'energy_kwh':(1036, lambda x:x/10_000), 'session_kwh':(1502, lambda x:x/10_000),
            'serial':(1014, int), 'product_raw':(1016, int), 'firmware_raw':(1018, int),
            'firmware':(1018, self.firmware_version), 'power_factor_pct':(1046, lambda x:x/10),
            'device_limit_a':(1100, lambda x:x/1000), 'phase_switch_source':(1550, int),
            'phase_count':(1552, int), 'rfid_uid':(1500, lambda x:f'{x:08X}'),
            'failsafe_current_a':(1600, lambda x:x/1000), 'failsafe_timeout_s':(1602, int),
            'fast_charging':(1200, bool), 'hardware_revision':(1700, int), 'meter_hardware_revision':(1702, int),
        }
        data.update({key:convert(raw[register]) for key,(register,convert) in fields.items() if register in raw})
        if all(r in raw for r in [1040,1042,1044]):
            data['voltages_v'] = [raw[r] for r in [1040,1042,1044]]
        return data

    async def set_limit(self, amps):
        if amps != 0 and not 6<=amps<=self.max_current_a:raise ValueError('Ungültiges Stromlimit')
        if self.model=='P40':
            await self.write16(5004,int(amps*1000))
        elif amps==0:
            await self.write16(5014,0)
        else:
            # Apply the limit before resuming. Monta authorization stays separate.
            await self.write16(5004,int(amps*1000))
            await self.write16(5014,1)

    async def configure_failsafe(self, timeout=10):
        if not 5<=timeout<=600:raise ValueError('Ungültiger Timeout')
        # Reconnecting to an already configured device must not generate an
        # artificial 0-A pulse. Verify the safe fallback before reusing it.
        current, actual_timeout = await self.read32(1600), await self.read32(1602)
        if (current, actual_timeout) == (0, timeout):
            await self.refresh_failsafe(timeout)
            return
        await self.set_limit(0)
        await self.write16(5016,0)
        await self.refresh_failsafe(timeout)
        if self.model=='P30 x':await self.write16(5020,1)
        current,actual_timeout=await self.read32(1600),await self.read32(1602)
        if (current,actual_timeout)!=(0,timeout):raise IOError('Failsafe wurde nicht bestätigt')

    async def refresh_failsafe(self, timeout=10):
        """Feed the watchdog without stopping charging or persisting settings."""
        if not 5<=timeout<=600:raise ValueError('Ungültiger Timeout')
        await self.write16(5018,timeout)

    def close(self):self.client.close()
