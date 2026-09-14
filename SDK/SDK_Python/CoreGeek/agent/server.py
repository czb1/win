import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .brain import Agent
from .commands import EMPTY

LOG = logging.getLogger(__name__)


def reject_constant(value):
    raise ValueError(f"Non-JSON numeric constant: {value}")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, config=None):
        self.agent = Agent(config)
        self.lock = threading.Lock()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(0.5)

    def send_json(self, body, status=200):
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            LOG.warning("client disconnected")

    def do_GET(self):
        if self.path == "/healthz":
            self.send_json({"status": "ok"})
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/":
            self.send_json({"error": "not found"}, 404)
            return
        try:
            if self.headers.get("Transfer-Encoding"):
                self.send_json({"error": "Content-Length required"}, 400)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= self.server.agent.cfg.max_body_bytes:
                self.send_json({"error": "invalid body size"}, 413)
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete body")
            data = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
            if not isinstance(data, dict):
                raise ValueError("body must be an object")
        except (ValueError, UnicodeError, socket.timeout):
            self.send_json({"error": "invalid JSON body"}, 400)
            return
        if not self.server.lock.acquire(timeout=0.05):
            self.send_json(EMPTY)
            return
        try:
            response = self.server.agent.decide(data)
        except Exception:
            LOG.exception("decision failed; sending protocol-compatible empty turn")
            response = EMPTY
        finally:
            self.server.lock.release()
        self.send_json(response)

    def log_message(self, fmt, *args):
        LOG.debug(fmt, *args)


def serve(port, config=None, host="0.0.0.0"):
    if not 0 <= port <= 65535:
        raise ValueError("port must be in 0..65535")
    with Server((host, port), config) as server:
        LOG.info("listening on %s:%s", host, server.server_port)
        server.serve_forever()
