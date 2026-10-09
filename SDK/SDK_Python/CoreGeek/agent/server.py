import json
import logging
import socket
import threading
from time import monotonic
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .brain import Agent
from .commands import EMPTY
from .logging_system import request_context, bind_request, emit_event

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

    def handle_error(self, request, client_address):
        # socketserver's default handler prints a plaintext traceback to stderr.
        LOG.exception("HTTP handler failed")


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
            emit_event("http_response", {"status": status, "bytes": len(encoded), "sent": True,
                       "elapsed_ms": (monotonic() - getattr(self, "request_started", monotonic())) * 1000}, "protocol")
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            emit_event("client_disconnected", {"status": status, "sent": False}, "protocol", level=logging.WARNING)
            LOG.warning("client disconnected")

    def do_GET(self):
        if self.path == "/healthz":
            self.send_json({"status": "ok"})
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        self.request_started = monotonic()
        with request_context(source="http"):
            self._do_POST()

    def _do_POST(self):
        if self.path != "/":
            self.send_json({"error": "not found"}, 404)
            return
        try:
            if self.headers.get("Transfer-Encoding"):
                emit_event("request_rejected", {"reason": "content_length_required"}, "protocol")
                self.send_json({"error": "Content-Length required"}, 400)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= self.server.agent.cfg.max_body_bytes:
                emit_event("request_rejected", {"reason": "body_size", "bytes": length}, "protocol")
                self.send_json({"error": "invalid body size"}, 413)
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete body")
            data = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
            if not isinstance(data, dict):
                raise ValueError("body must be an object")
            bind_request(data)
            emit_event("request_received", {"bytes": length}, "protocol")
        except (ValueError, UnicodeError, socket.timeout) as error:
            emit_event("request_rejected", {"reason": "invalid_body", "error": str(error)}, "protocol")
            self.send_json({"error": "invalid JSON body"}, 400)
            return
        if not self.server.lock.acquire(timeout=0.05):
            emit_event("lock_busy", {"lock_wait_ms": 50, "fallback_response": EMPTY}, "protocol", level=logging.WARNING)
            self.send_json(EMPTY)
            return
        try:
            response = self.server.agent.decide(data)
        except Exception:
            LOG.exception("decision failed; sending protocol-compatible empty turn")
            emit_event("empty_response_fallback", {"response": EMPTY}, "protocol", level=logging.ERROR)
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

