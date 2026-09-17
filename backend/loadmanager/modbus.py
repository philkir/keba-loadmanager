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

    async def telemetry(self):
        # Sequential requests on one Modbus connection. Never run a second poller
        # independently of the controller: reads can reset the device watchdog.
        return {'state':await self.read32(1000),
                'cable_state':await self.read32(1004),
                'error_code':await self.read32(1006),
                'currents_a':[await self.read32(r)/1000 for r in [1008,1010,1012]],
                'serial':await self.read32(1014),
                'firmware_raw':await self.read32(1018),
                'power_kw':await self.read32(1020)/1_000_000,
                'energy_kwh':await self.read32(1036)/10_000}

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
