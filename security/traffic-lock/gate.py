"""Owner-controlled HTTP ingress gate. Python 3.11+, standard library only."""
import argparse
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import posixpath
import secrets
import socket
import sqlite3
import ssl
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

PROJECT_LIMIT = 600
SOURCE_LIMIT = 120
REPEAT_LIMIT = 20
BYTE_LIMIT = 50 * 1024 * 1024
HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
       'te', 'trailer', 'transfer-encoding', 'upgrade'}


def fingerprint(method, path):
    path = urlsplit(path).path
    for _ in range(2):
        path = unquote(path)
    return hashlib.sha256((method.upper() + ' ' + posixpath.normpath('/' + path.lstrip('/'))).encode()).hexdigest()


class Gate:
    def __init__(self, database, projects, clock=time.time):
        self.database = str(database)
        self.projects = set(projects)
        self.clock = clock
        Path(database).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS projects(name TEXT PRIMARY KEY, until REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT 'closed');
            CREATE TABLE IF NOT EXISTS blocked(project TEXT, source TEXT, reason TEXT, PRIMARY KEY(project,source));
            CREATE TABLE IF NOT EXISTS hits(project TEXT, source TEXT, fingerprint TEXT, at REAL);
            CREATE INDEX IF NOT EXISTS hits_time ON hits(project,at);
            CREATE INDEX IF NOT EXISTS hits_source ON hits(project,source,at);
            CREATE INDEX IF NOT EXISTS hits_repeat ON hits(project,source,fingerprint,at);
            CREATE TABLE IF NOT EXISTS bytes(project TEXT, amount INTEGER, at REAL);
            CREATE INDEX IF NOT EXISTS bytes_time ON bytes(project,at);
            CREATE TABLE IF NOT EXISTS audit(at REAL, project TEXT, action TEXT, reason TEXT);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
            ''')
            db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', ('salt', secrets.token_hex(32)))
            for project in projects:
                db.execute('INSERT OR IGNORE INTO projects(name) VALUES (?)', (project,))
        os.chmod(database, 0o600)

    def connect(self):
        return sqlite3.connect(self.database, timeout=2, isolation_level='IMMEDIATE')

    def event(self, db, project, action, reason):
        db.execute('INSERT INTO audit VALUES (?,?,?,?)', (self.clock(), project, action, reason))

    def approve(self, project, minutes, reason):
        if project not in self.projects or not 1 <= minutes <= 1440 or not reason.strip():
            raise ValueError('Known project, reason and 1..1440 minutes required')
        with self.connect() as db:
            db.execute('UPDATE projects SET until=?,reason=? WHERE name=?', (self.clock() + minutes * 60, 'owner-approved', project))
            self.event(db, project, 'open', reason)
        # Approval never silently clears source bans. Use unblock explicitly.

    def close(self, project, reason):
        if project not in self.projects:
            raise ValueError('Unknown project')
        with self.connect() as db:
            db.execute('UPDATE projects SET until=0,reason=? WHERE name=?', (reason, project))
            self.event(db, project, 'close', reason)

    def source(self, db, ip):
        salt = db.execute("SELECT value FROM settings WHERE key='salt'").fetchone()[0]
        return hmac.new(salt.encode(), ipaddress.ip_address(ip).compressed.encode(), hashlib.sha256).hexdigest()

    def unblock(self, project, ip, reason):
        if project not in self.projects or not reason.strip():
            raise ValueError('Known project and reason required')
        with self.connect() as db:
            source = self.source(db, ip)
            db.execute('DELETE FROM blocked WHERE project=? AND source=?', (project, source))
            db.execute('DELETE FROM hits WHERE project=? AND source=?', (project, source))
            self.event(db, project, 'unblock-source', reason)

    def active(self, project):
        if project not in self.projects:
            return False
        with self.connect() as db:
            row = db.execute('SELECT until FROM projects WHERE name=?', (project,)).fetchone()
            return bool(row and row[0] > self.clock())

    def check(self, project, ip, method, path):
        if project not in self.projects:
            return False, 'unknown-project'
        now = self.clock()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT until,reason FROM projects WHERE name=?', (project,)).fetchone()
            if not row or row[0] <= now:
                return False, 'closed'
            source = self.source(db, ip)
            if db.execute('SELECT 1 FROM blocked WHERE project=? AND source=?', (project, source)).fetchone():
                return False, 'source-blocked'
            db.execute('DELETE FROM hits WHERE at<?', (now - 60,))
            db.execute('DELETE FROM bytes WHERE at<?', (now - 60,))
            key = fingerprint(method, path)
            db.execute('INSERT INTO hits VALUES (?,?,?,?)', (project, source, key, now))
            count = lambda sql, args: db.execute(sql, args).fetchone()[0]
            if count('SELECT COUNT(*) FROM hits WHERE project=?', (project,)) > PROJECT_LIMIT:
                db.execute('UPDATE projects SET until=0,reason=? WHERE name=?', ('traffic-spike', project))
                self.event(db, project, 'trip-project', 'request-rate')
                return False, 'traffic-spike'
            total = count('SELECT COUNT(*) FROM hits WHERE project=? AND source=?', (project, source))
            repeat = count('SELECT COUNT(*) FROM hits WHERE project=? AND source=? AND fingerprint=? AND at>=?', (project, source, key, now - 10))
            if total > SOURCE_LIMIT or repeat > REPEAT_LIMIT:
                reason = 'repeated-request' if repeat > REPEAT_LIMIT else 'source-rate'
                db.execute('INSERT OR REPLACE INTO blocked VALUES (?,?,?)', (project, source, reason))
                self.event(db, project, 'block-source', reason)
                return False, reason
            return True, 'approved'

    def transfer(self, project, amount):
        now = self.clock()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT until FROM projects WHERE name=?', (project,)).fetchone()
            if not row or row[0] <= now:
                return False
            db.execute('DELETE FROM bytes WHERE at<?', (now - 60,))
            used = db.execute('SELECT COALESCE(SUM(amount),0) FROM bytes WHERE project=?', (project,)).fetchone()[0]
            if used + amount > BYTE_LIMIT:
                db.execute('UPDATE projects SET until=0,reason=? WHERE name=?', ('bandwidth-spike', project))
                self.event(db, project, 'trip-project', 'bandwidth')
                return False
            db.execute('INSERT INTO bytes VALUES (?,?,?)', (project, amount, now))
            return True

    def status(self):
        with self.connect() as db:
            rows = db.execute('SELECT name,until,reason FROM projects').fetchall()
            return [{'project': p, 'open': until > self.clock(), 'until': until, 'reason': reason} for p, until, reason in rows]


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, gate, project, upstream):
        import threading
        self.slots = threading.BoundedSemaphore(32)
        self.gate, self.project, self.upstream = gate, project, upstream
        self.tls_context = None
        super().__init__(address, Handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(10)
        if self.tls_context:
            request = self.tls_context.wrap_socket(request, server_side=True, do_handshake_on_connect=False)
        return request, address

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 response closes each connection: no unapproved persistent upgrades.
    def setup(self):
        self.request.settimeout(10)
        if isinstance(self.request, ssl.SSLSocket):
            self.request.do_handshake()
        super().setup()

    def log_message(self, *args):
        pass  # Never log request URLs, cookies or credentials.

    def deny(self, status=403):
        self.close_connection = True
        body = b'{"error":"traffic_locked"}'
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def client_ip(self):
        peer = ipaddress.ip_address(self.client_address[0])
        networks = [ipaddress.ip_network(x.strip()) for x in os.getenv('GATE_TRUSTED_PROXIES', '').split(',') if x.strip()]
        if any(peer in network for network in networks):
            # A configured TLS proxy MUST overwrite this header, not append it.
            return ipaddress.ip_address(self.headers.get('X-Real-IP', '')).compressed
        return peer.compressed

    def authorize(self):
        key = os.getenv('GATE_AUTHORIZATION_KEY', '')
        supplied = self.headers.get('Authorization', '')
        if len(key) < 32 or not hmac.compare_digest(supplied, 'Bearer ' + key):
            return self.deny()
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= 4096 or self.headers.get('Transfer-Encoding'):
            return self.deny(400)
        data = json.loads(self.rfile.read(length))
        if not all(isinstance(data.get(k), str) for k in ['project', 'ip', 'method', 'path']):
            return self.deny(400)
        allowed, _ = self.server.gate.check(data['project'], data['ip'], data['method'], data['path'])
        if not allowed:
            return self.deny()
        body = b'{"allow":true}'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def relay(self):
        conn = None
        try:
            if self.path == '/_traffic_lock/authorize':
                return self.authorize() if self.command == 'POST' else self.deny()
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or not self.path.startswith('/'):
                return self.deny(400)
            ip = self.client_ip()
            allowed, _ = self.server.gate.check(self.server.project, ip, self.command, self.path)
            if not allowed:
                return self.deny()
            if self.headers.get('Upgrade') or self.headers.get('Transfer-Encoding'):
                return self.deny(400)
            if len(self.headers.get_all('Content-Length', [])) > 1:
                return self.deny(400)
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 <= length <= 8 * 1024 * 1024:
                return self.deny(413)
            if not self.server.gate.transfer(self.server.project, length):
                return self.deny()
            body = self.rfile.read(length) if length else None
            if body is not None and len(body) != length:
                return self.deny(400)
            upstream = self.server.upstream
            factory = http.client.HTTPSConnection if upstream.scheme == 'https' else http.client.HTTPConnection
            conn = factory(upstream.hostname, upstream.port, timeout=10)
            extra = {v.strip().lower() for v in self.headers.get('Connection', '').split(',')}
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP | extra | {'host', 'x-forwarded-for', 'x-real-ip', 'forwarded'}}
            headers['Host'] = upstream.netloc
            headers['X-Forwarded-For'] = ip
            conn.request(self.command, self.path, body, headers)
            response = conn.getresponse()
            first = b'' if self.command == 'HEAD' else response.read1(65536)
            if not self.server.gate.transfer(self.server.project, len(first)):
                return self.deny()
            self.send_response(response.status)
            extra_response = {v.strip().lower() for v in (response.getheader('Connection') or '').split(',')}
            for k, v in response.getheaders():
                if k.lower() not in HOP | extra_response | {'cache-control'}:
                    self.send_header(k, v)
            # No downstream cache may serve a page after closure without the gate.
            self.send_header('Cache-Control', 'private, no-store')
            self.end_headers()
            if self.command != 'HEAD':
                chunk = first
                deadline = time.monotonic() + 60
                while chunk:
                    if time.monotonic() > deadline:
                        break
                    self.wfile.write(chunk)
                    chunk = response.read1(65536)
                    if chunk and not self.server.gate.transfer(self.server.project, len(chunk)):
                        break
        except (ValueError, KeyError, TypeError, sqlite3.Error, OSError, http.client.HTTPException):
            # If headers were already sent, terminate rather than forward more data.
            self.close_connection = True
            if conn is None:
                try:
                    self.deny(503)
                except OSError:
                    pass
        finally:
            if conn:
                conn.close()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = relay
    do_CONNECT = do_TRACE = lambda self: self.deny(405)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', default=os.getenv('GATE_STATE', '.traffic-lock/state.sqlite'))
    parser.add_argument('--project', required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    serve = sub.add_parser('serve')
    serve.add_argument('--upstream', required=True)
    serve.add_argument('--bind', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8443)
    opened = sub.add_parser('open')
    opened.add_argument('--minutes', type=int, required=True)
    opened.add_argument('--reason', required=True)
    opened.add_argument('--confirm-owner', action='store_true', required=True)
    closed = sub.add_parser('close')
    closed.add_argument('--reason', required=True)
    unblock = sub.add_parser('unblock')
    unblock.add_argument('--ip', required=True)
    unblock.add_argument('--reason', required=True)
    unblock.add_argument('--confirm-owner', action='store_true', required=True)
    sub.add_parser('status')
    args = parser.parse_args()
    gate = Gate(args.state, [args.project])
    if args.command == 'open':
        gate.approve(args.project, args.minutes, args.reason)
    elif args.command == 'close':
        gate.close(args.project, args.reason)
    elif args.command == 'unblock':
        gate.unblock(args.project, args.ip, args.reason)
    elif args.command == 'serve':
        upstream = urlsplit(args.upstream)
        if upstream.scheme not in ['http', 'https'] or not upstream.hostname or upstream.username or upstream.password or upstream.path not in ['', '/'] or upstream.query or upstream.fragment:
            parser.error('Upstream must be a fixed http(s) origin without credentials, path or query')
        server = Server((args.bind, args.port), gate, args.project, upstream)
        cert, key = os.getenv('GATE_TLS_CERT'), os.getenv('GATE_TLS_KEY')
        if cert and key:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(cert, key)
            server.tls_context = context
        elif not ipaddress.ip_address(args.bind).is_loopback:
            parser.error('Public listener requires TLS certificate and key')
        server.serve_forever()
    else:
        print(json.dumps(gate.status(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
