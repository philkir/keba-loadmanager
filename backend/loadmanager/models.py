import json
from ipaddress import IPv4Address
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Settings(StrictModel):
    site_name: str = Field(default='Unser Ladepark', min_length=1, max_length=60)
    power_limit_kw: float = Field(default=25, ge=5, le=25)
    phase_limit_a: float = Field(default=35, ge=6, le=35)
    reserve_kw: float = Field(default=1, ge=0.5, le=5)
    fallback_building_kw: float = Field(default=8, ge=0, le=20)
    rotation_seconds: int = Field(default=120, ge=30, le=900)
    paused: bool = False


class StationSetting(StrictModel):
    id: str
    name: str
    model: Literal['P30 x', 'P40']
    max_current_a: int = Field(default=32, ge=6, le=32)
    priority: Literal['normal', 'high'] = 'normal'
    paused: bool = False
    host: str = ''
    port: int = Field(default=502, ge=1, le=65535)
    device_id: int = Field(default=255, ge=0, le=255)

    @field_validator('host')
    @classmethod
    def private_ipv4(cls, value):
        if not value:
            return value
        address = IPv4Address(value)
        if not address.is_private:
            raise ValueError('Nur private IPv4-Adressen sind erlaubt.')
        return str(address)


class StationPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    model: Literal['P30 x', 'P40'] | None = None
    priority: Literal['normal', 'high'] | None = None
    paused: bool | None = None
    max_current_a: int | None = Field(default=None, ge=6, le=32)
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    device_id: int | None = Field(default=None, ge=0, le=255)

    @field_validator('host')
    @classmethod
    def private_ipv4(cls, value):
        if value in (None, ''):
            return value
        address = IPv4Address(value)
        if not address.is_private:
            raise ValueError('Nur private IPv4-Adressen sind erlaubt.')
        return str(address)


class Simulation(StrictModel):
    building_kw: float = Field(default=7, ge=0, le=30)
    profile: Literal['balanced', 'uneven'] = 'balanced'
    meter_online: bool = True
    disconnected: list[str] = Field(default_factory=list, max_length=4)
    offline: list[str] = Field(default_factory=list, max_length=4)


def default_stations():
    hosts = ['192.168.1.71', '192.168.1.72', '192.168.1.73', '192.168.1.74']
    return [StationSetting(id=f'cp-{i+1}', name=f'Ladepunkt {i+1:02}', model='P30 x' if i == 0 else 'P40', host=hosts[i]).model_dump() for i in range(4)]


def station_defaults_from_file(path: str | None):
    """Load deployment defaults without making the JSON file mutable at runtime."""
    if not path:
        return default_stations()
    document = json.loads(Path(path).read_text(encoding='utf-8'))
    values = document.get('stations') if isinstance(document, dict) else document
    if not isinstance(values, list) or not values:
        raise ValueError('Die Stationskonfiguration muss eine nicht leere Liste enthalten.')
    stations = [StationSetting.model_validate(value).model_dump() for value in values]
    ids = [station['id'] for station in stations]
    if len(ids) != len(set(ids)):
        raise ValueError('Stations-IDs müssen eindeutig sein.')
    return stations
