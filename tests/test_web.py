"""HTTP boundary tests for the read-only progress monitor."""
import http.client
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from modport.cli import parser, main
from modport.web import WebServer, serve


class WebTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.store = Mock()
        self.store.list_runs.return_value = [{"id": "one", "mod_id": "example"}]
        self.store.get_run.return_value = {"id": "one", "groups": []}
        self.store.get_step.return_value = {"id": "step", "result": "<script>alert(1)</script>"}
        try:
            self.server = WebServer(("127.0.0.1", 0), self.store, "查看密码", clock=lambda: self.now)
        except PermissionError:
            self.skipTest("network sandbox prohibits loopback sockets")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        supplied = {"Content-Type": "application/json"}
        if self.cookie:
            supplied["Cookie"] = self.cookie
        supplied.update(headers or {})
        connection.request(method, path, None if body is None else json.dumps(body), supplied)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def login(self):
        status, headers, _ = self.request("POST", "/api/login", {"password": "查看密码"})
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";")[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])

    def test_static_assets_work_without_login_but_all_data_require_session(self):
        for path in ("/", "/app.js", "/graph.js", "/style.css"):
            status, headers, data = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertTrue(data)
            self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
            self.assertEqual(headers["Cache-Control"], "no-store")
        for path in ("/api/session", "/api/runs", "/api/runs/one", "/api/runs/one/steps/step"):
            self.assertEqual(self.request("GET", path)[0], 401)
        self.store.list_runs.assert_not_called()

    def test_authenticated_read_logout_and_expiry(self):
        self.login()
        self.assertEqual(json.loads(self.request("GET", "/api/runs")[2])["runs"][0]["id"], "one")
        self.assertEqual(self.request("GET", "/api/runs/one")[0], 200)
        self.assertEqual(self.request("GET", "/api/runs/one/steps/step")[0], 200)
        self.store.get_step.assert_called_once_with("one", "step")
        self.assertEqual(self.request("POST", "/api/logout", {})[0], 200)
        self.assertEqual(self.request("GET", "/api/runs")[0], 401)
        self.login()
        self.now += 43201
        self.assertEqual(self.request("GET", "/api/runs")[0], 401)

    def test_failed_login_rate_limit_expires(self):
        for _ in range(5):
            self.assertEqual(self.request("POST", "/api/login", {"password": "wrong"})[0], 401)
        self.assertEqual(self.request("POST", "/api/login", {"password": "查看密码"})[0], 429)
        self.now += 61
        self.login()

    def test_reject_cross_site_and_non_json_login(self):
        for headers in ({"Origin": "http://evil.example"}, {"Sec-Fetch-Site": "cross-site"},
                        {"Content-Type": "text/plain"}, {"Origin": "null"}):
            self.assertEqual(self.request("POST", "/api/login", {"password": "查看密码"}, headers)[0], 403)
        self.assertFalse(self.server.sessions)

    def test_invalid_body_and_no_mutation_routes(self):
        self.assertEqual(self.request("POST", "/api/login", ["not a mapping"])[0], 400)
        self.assertEqual(self.request("POST", "/api/login", {"password": "x" * 9000})[0], 400)
        self.login()
        self.assertEqual(self.request("POST", "/api/runs/one/cancel", {})[0], 405)
        self.assertEqual(self.request("GET", "/../../run.json")[0], 404)
        self.assertEqual(self.request("GET", "/api/runs/one/../../run.json")[0], 404)
        self.store.get_run.assert_not_called()

    def test_errors_hide_paths_and_provider_exceptions(self):
        self.login()
        self.store.get_run.side_effect = KeyError("/private/secret")
        self.assertEqual(self.request("GET", "/api/runs/missing")[0], 404)
        self.store.get_run.side_effect = RuntimeError("password=hidden /private/file")
        status, _, data = self.request("GET", "/api/runs/one")
        self.assertEqual(status, 503)
        self.assertNotIn(b"hidden", data)
        self.assertNotIn(b"private", data)


class WebCLITests(unittest.TestCase):
    def test_temporary_token_tolerates_only_copy_formatting(self):
        server = object.__new__(WebServer)
        token = 'MP-2345-6789-2345'
        server.password_hash = hashlib.sha256(token.encode()).digest()
        server.temporary_password = True
        for value in (token, '\u2068`' + token + '`\u2069', '\ufeff\u00a0' + token + '\n',
                      '```\n' + token + '\n```', token[:3] + '\u200b' + token[3:]):
            self.assertTrue(server.password_matches(value))
        for value in (token.lower(), token + '9', '`' + token, token.replace('-', ' '),
                      token.replace('-', '\u2011'), token[:5] + ' ' + token[5:], '密码：' + token):
            self.assertFalse(server.password_matches(value))

    def test_arbitrary_passwords_remain_exact(self):
        server = object.__new__(WebServer)
        server.temporary_password = False
        for token in (' 查看密码 ', '`MP-2345-6789-2345`', 'abc\u200bdef'):
            server.password_hash = hashlib.sha256(token.encode()).digest()
            self.assertTrue(server.password_matches(token))
            self.assertFalse(server.password_matches(token.strip().replace('`', '').replace('\u200b', '')))

    def test_cli_defaults_and_dispatch_do_not_use_operations(self):
        args = parser().parse_args(["web", "--password-file", "/tmp/password"])
        self.assertEqual(args.port, 8765)
        self.assertEqual(args.host, "0.0.0.0")
        with patch("modport.web.serve", return_value=0) as server:
            self.assertEqual(main(["web", "--runs-root", "/tmp/runs", "--password-file", "/tmp/password"]), 0)
        server.assert_called_once_with("/tmp/runs", "0.0.0.0", 8765, "/tmp/password")

    def test_password_file_empty_or_oversize_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            password = Path(directory) / "password"
            for content in ("\n", "x" * 4097):
                password.write_text(content)
                with self.assertRaises(ValueError):
                    serve(directory, "127.0.0.1", 0, password)
