"""Commissioning adapters. Deliberately NOT wired into the simulation controller.

Register maps: KEBA P30 V1.07, P40 V1.02. Verify against installed firmware.
No RFID authorization or OCPP settings are written by this adapter.
"""
from pymodbus.client import AsyncModbusTcpClient


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

    async def telemetry(self, full=False):
        """Read operational data; full adds slower device and capability registers."""
        # Keep all requests on this single connection: reads also feed the device watchdog.
        state = await self.read32(1000)
        data = {'state':state,
                'cable_state':await self.read32(1004),
                'error_code':await self.read32(1006),
                'currents_a':[await self.read32(r)/1000 for r in [1008,1010,1012]],
                'power_kw':await self.read32(1020)/1_000_000,
                'energy_kwh':await self.read32(1036)/10_000,
                'session_kwh':(await self.read32(1502))/10_000}
        if not full:
            return data
        raw = {register: await self.optional32(register) for register in
               [1014,1016,1018,1040,1042,1044,1046,1100,1110,1500,1550,1552,1600,1602]}
        if self.model == 'P40':
            raw.update({register: await self.optional32(register) for register in [1200,1700,1702]})
        data.update({
            'serial':raw[1014], 'product_raw':raw[1016], 'firmware_raw':raw[1018],
            'firmware':self.firmware_version(raw[1018]) if raw[1018] is not None else None,
            'voltages_v':[raw[r] for r in [1040,1042,1044]],
            'power_factor_pct':raw[1046]/10 if raw[1046] is not None else None,
            'device_limit_a':raw[1100]/1000 if raw[1100] is not None else None,
            'hardware_limit_a':raw[1110]/1000 if raw[1110] is not None else None,
            'phase_switch_source':raw[1550], 'phase_count':raw[1552],
            'rfid_uid':f'{raw[1500]:08X}' if raw[1500] is not None else None,
            'failsafe_current_a':raw[1600]/1000 if raw[1600] is not None else None,
            'failsafe_timeout_s':raw[1602],
            'fast_charging':bool(raw.get(1200)) if raw.get(1200) is not None else None,
            'hardware_revision':raw.get(1700), 'meter_hardware_revision':raw.get(1702),
        })
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
        await self.set_limit(0)
        await self.write16(5016,0)
        await self.write16(5018,timeout)
        if self.model=='P30 x':await self.write16(5020,1)
        current,actual_timeout=await self.read32(1600),await self.read32(1602)
        if (current,actual_timeout)!=(0,timeout):raise IOError('Failsafe wurde nicht bestätigt')

    def close(self):self.client.close()
