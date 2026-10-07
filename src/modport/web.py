"""Small authenticated, read-only LAN monitor. Never opens a migration session."""
from collections import deque
import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import unquote, urlsplit


_TEMPORARY_PASSWORD = re.compile(r"MP-[0-9]{4}-[0-9]{4}-[0-9]{4}\Z")
_COPY_FORMAT_CHARACTERS = str.maketrans("", "", "\u200b\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\u2066\u2067\u2068\u2069\ufeff")


def copied_temporary_password(value):
    """Decode presentation-only clipboard wrappers around a generated token.

    This is never applied to arbitrary configured passwords. Visible characters,
    case, and internal whitespace are not rewritten, and the original secret's
    hash remains the authority.
    """
    candidate = value.translate(_COPY_FORMAT_CHARACTERS).strip()
    for wrapper in ("```", "`"):
        if (candidate.startswith(wrapper) and candidate.endswith(wrapper)
                and len(candidate) >= 2 * len(wrapper)):
            candidate = candidate[len(wrapper):-len(wrapper)].strip()
            break
    return candidate if _TEMPORARY_PASSWORD.fullmatch(candidate) else None


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store, password, *, clock=time.monotonic):
        if not password or len(password.encode()) > 4096:
            raise ValueError("web password must contain 1–4096 bytes")
        self.store = store
        self.password_hash = hashlib.sha256(password.encode()).digest()
        self.temporary_password = bool(_TEMPORARY_PASSWORD.fullmatch(password))
        self.clock = clock
        self.auth_lock = threading.Lock()
        self.sessions = {}
        self.failures = {}
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(address, WebHandler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def prune_auth(self):
        now = self.clock()
        self.sessions = {k: v for k, v in self.sessions.items() if v > now}
        for ip in list(self.failures):
            attempts = self.failures[ip]
            while attempts and attempts[0] <= now - 60:
                attempts.popleft()
            if not attempts:
                del self.failures[ip]

    def password_matches(self, supplied):
        if not isinstance(supplied, str):
            return False
        if hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(), self.password_hash):
            return True
        if self.temporary_password:
            candidate = copied_temporary_password(supplied)
            if candidate is not None:
                return hmac.compare_digest(hashlib.sha256(candidate.encode()).digest(), self.password_hash)
        return False


class WebHandler(BaseHTTPRequestHandler):
    server_version = "ModPort"
    sys_version = ""

    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, format, *args):
        # Requests and credentials do not belong in migration evidence or stdout.
        pass

    def reply(self, status, body, content_type="application/json; charset=utf-8", **headers):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(body)

    def token(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            return cookie["modport_session"].value if "modport_session" in cookie else ""
        except CookieError:
            return ""

    def authenticated(self):
        with self.server.auth_lock:
            self.server.prune_auth()
            return self.server.sessions.get(self.token(), 0) > self.server.clock()

    def do_GET(self):
        path = urlsplit(self.path).path
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/graph.js": ("graph.js", "text/javascript; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if path in assets:
            name, content_type = assets[path]
            self.reply(200, files("modport").joinpath("web_static", name).read_bytes(), content_type)
            return
        if not self.authenticated():
            self.reply(401, {"error": "请先登录"})
            return
        parts = path.strip("/").split("/")
        try:
            if path == "/api/session":
                data = {"authenticated": True}
            elif path == "/api/runs":
                data = {"runs": self.server.store.list_runs()}
            elif len(parts) == 3 and parts[:2] == ["api", "runs"]:
                data = self.server.store.get_run(unquote(parts[2]))
            elif len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "evidence":
                data = self.server.store.get_evidence(unquote(parts[2]))
            elif len(parts) == 5 and parts[:2] == ["api", "runs"] and parts[3] == "tasks":
                data = self.server.store.get_task(unquote(parts[2]), unquote(parts[4]))
            elif len(parts) == 5 and parts[:2] == ["api", "runs"] and parts[3] == "steps":
                data = self.server.store.get_step(unquote(parts[2]), unquote(parts[4]))
            else:
                self.reply(404, {"error": "页面不存在"})
                return
            self.reply(200, data)
        except (KeyError, FileNotFoundError):
            self.reply(404, {"error": "未找到该运行或步骤"})
        except Exception:
            self.reply(503, {"error": "暂时无法读取进度，请稍后重试"})

    def do_POST(self):
        # JSON and same-origin checks prevent cross-site login/logout requests.
        origin = self.headers.get("Origin")
        if (self.headers.get("Sec-Fetch-Site") == "cross-site"
                or (origin is not None and origin not in {
                    "http://" + self.headers.get("Host", ""),
                    "https://" + self.headers.get("Host", "")})
                or self.headers.get("Content-Type", "").split(";")[0] != "application/json"):
            self.reply(403, {"error": "请求来源或格式不正确"})
            return
        path = urlsplit(self.path).path
        if path not in {"/api/login", "/api/logout"}:
            self.reply(405, {"error": "此页面仅支持查看"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192 or self.headers.get("Transfer-Encoding"):
                raise ValueError()
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, OSError):
            self.reply(400, {"error": "请求格式不正确"})
            return
        with self.server.auth_lock:
            status, response, headers = self.auth_response(path, body)
        self.reply(status, response, **headers)

    def auth_response(self, path, body):
        self.server.prune_auth()
        if path == "/api/logout":
            self.server.sessions.pop(self.token(), None)
            return 200, {}, {"Set_Cookie": "modport_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"}
        ip = self.client_address[0]
        failures = self.server.failures.get(ip, deque())
        if len(failures) >= 5 or len(self.server.failures) >= 4096:
            return 429, {"error": "尝试过于频繁，请一分钟后重试"}, {"Retry_After": "60"}
        password = body.get("password")
        if not self.server.password_matches(password):
            failures.append(self.server.clock())
            self.server.failures[ip] = failures
            return 401, {"error": "密码不正确"}, {}
        self.server.failures.pop(ip, None)
        self.server.sessions.pop(self.token(), None)
        if len(self.server.sessions) >= 1024:
            return 503, {"error": "访问人数过多，请稍后重试"}, {}
        token = secrets.token_urlsafe(32)
        self.server.sessions[token] = self.server.clock() + 12 * 3600
        return 200, {}, {"Set_Cookie": f"modport_session={token}; Path=/; Max-Age=43200; HttpOnly; SameSite=Strict"}


def serve(runs_root, host, port, password_file):
    from .web_data import ProgressStore
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    root = Path(runs_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("runs root must be an existing directory")
    with Path(password_file).expanduser().open("rb") as stream:
        raw = stream.read(4098)
    if len(raw) > 4096:
        raise ValueError("password file must be at most 4096 bytes")
    password = raw.decode("utf-8").rstrip("\r\n")
    with WebServer((host, port), ProgressStore(root), password) as server:
        print(f"ModPort 网页：http://{host}:{server.server_port} （Ctrl+C 停止）", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0
