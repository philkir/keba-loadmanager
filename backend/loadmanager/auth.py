"""Small local account store for the browser-facing application."""
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager


SESSION_SECONDS = 12 * 60 * 60
ROLES = {'admin', 'operator', 'viewer'}
# OpenSSL's default cap is 32 MiB. N=32768/r=8 needs that plus overhead.
SCRYPT_MAXMEM = 64 * 1024 * 1024


class AuthInputError(ValueError):
    """An account field failed validation."""


class SetupAlreadyCompleted(RuntimeError):
    """The initial administrator already exists."""


class PasswordHashError(RuntimeError):
    """A server-side hashing failure, never a password validation error."""


class AuthStore:
    def __init__(self, path):
        self.path = path
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL, role TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    expires_at REAL NOT NULL, created_at REAL NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
                CREATE TABLE IF NOT EXISTS login_attempts (
                    key TEXT NOT NULL, attempted_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_expires ON sessions(expires_at);
                CREATE INDEX IF NOT EXISTS attempts_key_time ON login_attempts(key, attempted_at);
            ''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def valid_username(username):
        return isinstance(username, str) and 3 <= len(username) <= 254 and username == username.strip() and all(ord(char) >= 32 for char in username)

    @staticmethod
    def valid_password(password):
        return isinstance(password, str) and len(password) >= 12 and len(password) <= 256

    @staticmethod
    def _hash_password(password, salt=None):
        salt = salt or os.urandom(16)
        try:
            derived = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=2**15,
                                     r=8, p=1, dklen=32, maxmem=SCRYPT_MAXMEM)
        except ValueError as exc:
            raise PasswordHashError('Passwort-Hashing auf dem Server fehlgeschlagen.') from exc
        return salt.hex() + '$' + derived.hex()

    @classmethod
    def _verify_password(cls, password, encoded):
        try:
            salt_hex, expected = encoded.split('$', 1)
            salt = bytes.fromhex(salt_hex)
        except (TypeError, ValueError):
            return False
        actual = cls._hash_password(password, salt).split('$', 1)[1]
        return hmac.compare_digest(actual, expected)

    def setup_required(self):
        with self._db() as db:
            return db.execute('SELECT 1 FROM users LIMIT 1').fetchone() is None

    def create_user(self, username, password, role='operator', initial=False):
        if not self.valid_username(username):
            raise AuthInputError('Benutzername oder E-Mail: 3–254 Zeichen, ohne Leerzeichen am Anfang oder Ende.')
        if not self.valid_password(password):
            raise AuthInputError('Passwort: 12–256 beliebige Zeichen.')
        if not isinstance(role, str) or role not in ROLES:
            raise AuthInputError('Ungültige Benutzerrolle.')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if initial and db.execute('SELECT 1 FROM users LIMIT 1').fetchone():
                raise SetupAlreadyCompleted('Die Ersteinrichtung wurde bereits abgeschlossen.')
            try:
                cursor = db.execute('INSERT INTO users(username,password_hash,role,created_at) VALUES (?,?,?,?)',
                                    (username, self._hash_password(password), 'admin' if initial else role, time.time()))
            except sqlite3.IntegrityError as exc:
                raise AuthInputError('Benutzername ist bereits vergeben.') from exc
            user_id = cursor.lastrowid
        # A second connection can only see the new account after the commit.
        return self.user_by_id(user_id)

    def user_by_id(self, user_id):
        with self._db() as db:
            row = db.execute('SELECT id,username,role,created_at FROM users WHERE id=?', (user_id,)).fetchone()
        return dict(zip(('id', 'username', 'role', 'created_at'), row)) if row else None

    def users(self):
        with self._db() as db:
            rows = db.execute('SELECT id,username,role,created_at FROM users ORDER BY username').fetchall()
        return [dict(zip(('id', 'username', 'role', 'created_at'), row)) for row in rows]

    def login_allowed(self, key):
        cutoff = time.time() - 15 * 60
        with self._db() as db:
            db.execute('DELETE FROM login_attempts WHERE attempted_at < ?', (cutoff,))
            return db.execute('SELECT count(*) FROM login_attempts WHERE key=?', (key,)).fetchone()[0] < 5

    def failed_login(self, key):
        with self._db() as db:
            db.execute('INSERT INTO login_attempts(key,attempted_at) VALUES (?,?)', (key, time.time()))

    def login(self, username, password):
        if not self.valid_username(username) or not self.valid_password(password):
            return None, None
        with self._db() as db:
            row = db.execute('SELECT id,username,password_hash,role,created_at FROM users WHERE username=?', (username,)).fetchone()
        if not row or not self._verify_password(password, row[2]):
            return None, None
        user = dict(zip(('id', 'username', 'password_hash', 'role', 'created_at'), row))
        token = secrets.token_urlsafe(32)
        with self._db() as db:
            db.execute('INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES (?,?,?,?)',
                       (hashlib.sha256(token.encode()).hexdigest(), user['id'], time.time()+SESSION_SECONDS, time.time()))
        user.pop('password_hash')
        return user, token

    def session_user(self, token):
        if not token:
            return None
        now = time.time()
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._db() as db:
            db.execute('DELETE FROM sessions WHERE expires_at < ?', (now,))
            row = db.execute('''SELECT u.id,u.username,u.role,u.created_at FROM sessions s
                                JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?''', (digest, now)).fetchone()
        return dict(zip(('id', 'username', 'role', 'created_at'), row)) if row else None

    def logout(self, token):
        if token:
            with self._db() as db:
                db.execute('DELETE FROM sessions WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),))
