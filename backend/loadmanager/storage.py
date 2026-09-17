import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL, level TEXT, message TEXT);
            CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY, ts REAL, building REAL, charging REAL, total REAL);
            CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, result TEXT NOT NULL, ts REAL);
        ''')
        self.db.commit()

    def get(self, key, default):
        row = self.db.execute('SELECT value FROM config WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def commit_command(self, key, value, command_id, result, message):
        # State and acknowledgment are persisted in the same transaction.
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO config VALUES (?,?)', (key, json.dumps(value)))
            self.db.execute('INSERT INTO commands VALUES (?,?,?)', (command_id, json.dumps(result), time.time()))
            self.db.execute('INSERT INTO events(ts,level,message) VALUES (?,?,?)', (time.time(), 'info', message))

    def previous(self, command_id):
        row = self.db.execute('SELECT result FROM commands WHERE id=?', (command_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def event(self, message, level='info'):
        with self.db:
            self.db.execute('INSERT INTO events(ts,level,message) VALUES (?,?,?)', (time.time(), level, message))
            self.db.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 2000)')

    def sample(self, ts, building, charging):
        with self.db:
            self.db.execute('INSERT INTO samples(ts,building,charging,total) VALUES (?,?,?,?)', (ts, building, charging, building+charging))
            self.db.execute('DELETE FROM samples WHERE ts < ?', (ts-86400*7,))
            self.db.execute('DELETE FROM commands WHERE ts < ?', (ts-86400,))

    def history(self):
        rows = self.db.execute('SELECT ts,building,charging,total FROM samples ORDER BY id DESC LIMIT 180').fetchall()
        return [dict(zip(['ts','building_kw','charging_kw','total_kw'], row)) for row in reversed(rows)]

    def events(self):
        return [dict(zip(['id','ts','level','message'], row)) for row in self.db.execute('SELECT id,ts,level,message FROM events ORDER BY id DESC LIMIT 80')]
