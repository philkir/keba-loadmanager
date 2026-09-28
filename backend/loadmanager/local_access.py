"""Server-only credentials for the KEBA local API, separate from public state."""
import json
import os
import tempfile
from pathlib import Path


class LocalAccessStore:
    def __init__(self, path):
        self.path = Path(path)
        self.values = json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self, station):
        entry = self.values.get(station['id'])
        if entry and (entry['host'], entry['model']) == (station['host'], station['model']):
            return entry.copy()
        return None

    def save(self, station, credentials):
        values = {**self.values, station['id']: {
            **credentials, 'host': station['host'], 'model': station['model'],
        }}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix='.wallbox-access-')
        try:
            with os.fdopen(fd, 'w') as file:
                json.dump(values, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
            self.values = values
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
