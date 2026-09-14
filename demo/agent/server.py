import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .brain import decide

LOGGER = logging.getLogger(__name__)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
            response = decide(payload)
            LOGGER.info("round %s -> %s", payload.get("roundNo"), response)
            body = json.dumps(
                {"roleCommandMap": response}, ensure_ascii=False,
            ).encode("utf-8")
        except Exception:
            LOGGER.exception("decision failed")
            body = b'{"roleCommandMap":{}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve(port: int) -> None:
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
