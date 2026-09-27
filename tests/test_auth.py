"""Authentication regressions; use only temporary databases and synthetic inputs.

Also runnable with unittest inside the production image (no pytest dependency).
"""
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from loadmanager.auth import AuthStore


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = str(Path(self.tmp.name) / 'auth.sqlite')
        self.environment = patch.dict(os.environ, {
            'STATIC_DIR': os.environ.get('STATIC_DIR', str(Path(__file__).resolve().parents[1] / 'frontend')),
            'DB_PATH': self.database,
            'API_TOKEN': 'synthetic-test-token-not-a-real-secret',
            'WEB_ALLOWED_ORIGINS': 'https://loadmanager.johann.it',
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        from loadmanager.web import app
        self.client = self.enterContext(TestClient(app, base_url='https://loadmanager.johann.it'))
        self.store = app.state.auth
        # Never contact the real controller, even for a successfully logged-in user.
        self.upstream = self.enterContext(patch.object(
            app.state.client, 'request', new=AsyncMock(return_value=httpx.Response(200, json={'test_state': True}))))
        self.account = {'username': 'synthetic@example.com', 'password': 'hJ!7#xQ%2&kL+9"\\ ä🔒'}
        self.headers = {'Origin': 'https://loadmanager.johann.it'}

    def post(self, url, body):
        return self.client.post(url, json=body, headers=self.headers)

    def setup_account(self):
        response = self.post('/auth/setup', self.account)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()['user']['role'], 'admin')
        return response

    def test_setup_login_session_logout_and_protected_api(self):
        self.assertEqual(self.client.get('/api/state').status_code, 401)
        response = self.setup_account()
        for flag in ('HttpOnly', 'Secure', 'SameSite=strict'):
            self.assertIn(flag, response.headers['set-cookie'])
        self.assertEqual(response.headers['cache-control'], 'no-store')
        status = self.client.get('/auth/status').json()
        self.assertFalse(status['setup_required'])
        self.assertEqual(status['user']['username'], self.account['username'])
        self.assertEqual(self.client.get('/api/state').json(), {'test_state': True})
        self.upstream.assert_awaited_once()
        self.assertEqual(self.post('/auth/setup', self.account).status_code, 409)
        old_token = self.client.cookies.get('keba_session')
        self.assertEqual(self.post('/auth/logout', {}).status_code, 200)
        self.assertIsNone(self.store.session_user(old_token))
        self.assertEqual(self.client.get('/api/state').status_code, 401)
        self.assertEqual(self.post('/auth/login', {**self.account, 'password': 'wrong-password-123'}).status_code, 401)
        self.assertEqual(self.post('/auth/login', self.account).status_code, 200)
        self.assertEqual(self.client.get('/api/state').status_code, 200)

    def test_account_returned_after_commit_and_survives_reopen(self):
        user = self.store.create_user(**self.account, initial=True)
        self.assertIsNotNone(user)
        reloaded = AuthStore(self.database)
        logged_in, token = reloaded.login(**self.account)
        self.assertEqual(logged_in, user)
        self.assertEqual(reloaded.session_user(token), user)
        with closing(sqlite3.connect(self.database)) as db:
            stored_hash = db.execute('SELECT password_hash FROM users').fetchone()[0]
            stored_session = db.execute('SELECT token_hash FROM sessions').fetchone()[0]
        self.assertNotIn(self.account['password'], stored_hash)
        self.assertNotEqual(stored_session, token)

    def test_real_hashing_accepts_password_manager_characters_and_lengths(self):
        for password in ('X!9+aB#2_cD?', 'ß🔒A!"\\ ' * 32, 'p' * 256):
            with self.subTest(length=len(password)):
                encoded = self.store._hash_password(password)
                self.assertTrue(self.store._verify_password(password, encoded))
                self.assertFalse(self.store._verify_password(password + 'different', encoded))

    def test_validation_is_distinct_from_hashing_failure(self):
        short = self.post('/auth/setup', {**self.account, 'password': 'short'})
        self.assertEqual(short.status_code, 422)
        self.assertIn('12–256', short.json()['detail'])
        with patch('loadmanager.auth.hashlib.scrypt', side_effect=ValueError('memory limit exceeded')):
            with self.assertLogs('loadmanager.web', level='ERROR'):
                failed = self.post('/auth/setup', self.account)
        self.assertEqual(failed.status_code, 503, failed.text)
        self.assertIn('Server', failed.json()['detail'])
        self.assertNotIn('12–256', failed.text)
        self.assertTrue(self.store.setup_required())
        self.setup_account()
        self.post('/auth/logout', {})
        with patch('loadmanager.auth.hashlib.scrypt', side_effect=ValueError('memory limit exceeded')):
            with self.assertLogs('loadmanager.web', level='ERROR'):
                failed = self.post('/auth/login', self.account)
        self.assertEqual(failed.status_code, 503)

    def test_viewer_permissions_and_admin_user_creation(self):
        self.setup_account()
        viewer = {'username': 'viewer@example.com', 'password': 'synthetic-Viewer!234', 'role': 'viewer'}
        response = self.post('/auth/users', viewer)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()['user']['role'], 'viewer')
        self.post('/auth/logout', {})
        self.assertEqual(self.post('/auth/login', viewer).status_code, 200)
        self.assertEqual(self.client.get('/api/state').status_code, 200)
        self.assertEqual(self.post('/api/commands', {}).status_code, 403)
        self.assertEqual(self.client.get('/auth/users').status_code, 403)
        self.assertEqual(self.post('/auth/users', viewer).status_code, 403)

    def test_bad_origin_and_malformed_payloads_do_not_create_account(self):
        rejected = self.client.post('/auth/setup', json=self.account, headers={'Origin': 'https://untrusted.example'})
        self.assertEqual(rejected.status_code, 403)
        for body in ([], None, {}, {'username': [], 'password': 42}):
            with self.subTest(body=body):
                self.assertEqual(self.post('/auth/setup', body).status_code, 422)
        self.assertTrue(self.store.setup_required())


if __name__ == '__main__':
    unittest.main(verbosity=2)
