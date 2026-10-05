import asyncio
from unittest.mock import AsyncMock

from loadmanager.modbus import KebaModbus


def test_slow_optional_register_cannot_discard_operating_measurements(monkeypatch):
    reads = []

    async def read(_self, register):
        reads.append(register)
        if register == 1014:
            await asyncio.sleep(5)
        return {1000:3,1004:7,1006:0,1008:16000,1010:16000,1012:16000,
                1020:11040000,1110:32000}[register]

    monkeypatch.setattr(KebaModbus, 'read32', read)

    async def scenario():
        box = KebaModbus('127.0.0.1', 'P40')
        try:
            async with asyncio.timeout(1):
                data = await box.telemetry(detail_registers=[1014,1016,1018])
            assert data['currents_a'] == [16]*3
            assert data['power_kw'] == 11.04
            assert data['hardware_limit_a'] == 32
            assert 'serial' not in data
            assert 1016 not in reads
        finally:
            box.close()
    asyncio.run(scenario())


def test_existing_safe_watchdog_is_reused_without_zero_pulse(monkeypatch):
    writes = AsyncMock()
    monkeypatch.setattr(KebaModbus, 'read32', AsyncMock(side_effect=[0,10]))
    monkeypatch.setattr(KebaModbus, 'write16', writes)

    async def scenario():
        box = KebaModbus('127.0.0.1', 'P30 x', writes_enabled=True)
        try:
            await box.configure_failsafe(10)
        finally:
            box.close()
    asyncio.run(scenario())
    writes.assert_awaited_once_with(5018,10)


def test_unknown_watchdog_is_initialized_with_safe_zero_limit(monkeypatch):
    writes = AsyncMock()
    monkeypatch.setattr(KebaModbus, 'read32', AsyncMock(side_effect=[16000,60,0,10]))
    monkeypatch.setattr(KebaModbus, 'write16', writes)

    async def scenario():
        box = KebaModbus('127.0.0.1', 'P40', writes_enabled=True)
        try:
            await box.configure_failsafe(10)
        finally:
            box.close()
    asyncio.run(scenario())
    assert [call.args for call in writes.await_args_list] == [(5004,0),(5016,0),(5018,10)]
