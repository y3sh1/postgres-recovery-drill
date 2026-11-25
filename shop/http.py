"""A small loopback HTTP service for the recovery exercise."""
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
from urllib.parse import urlsplit

import psycopg

from .store import Conflict, ORDER_ID, Order, OrderDatabase, OrderService, ReceiptJournal, StorageMismatch


STORAGE_ERRORS = (psycopg.Error, sqlite3.Error, OSError, StorageMismatch)


class OrdersHandler(BaseHTTPRequestHandler):
    def respond(self, status, body):
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_request(self, code="-", size="-"):
        # The driver records the measured requests. Keep 100 seed writes out of its output.
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        service = self.server.order_service
        try:
            if path == "/health/ready":
                service.database.ready()
                return self.respond(200, {"status": "ready"})
            order_id = path.removeprefix("/orders/")
            if not path.startswith("/orders/") or not ORDER_ID.fullmatch(order_id):
                return self.respond(404, {"error": "Unknown route"})
            order = service.database.get(order_id)
        except STORAGE_ERRORS:
            return self.respond(503, {"error": "Data store unavailable"})
        if order is None:
            return self.respond(404, {"error": "Order not found"})
        self.respond(200, asdict(order))

    def do_POST(self):
        if urlsplit(self.path).path != "/orders":
            return self.respond(404, {"error": "Unknown route"})
        service = self.server.order_service
        if service.journal is None:
            return self.respond(403, {"error": "Recovery service is read-only"})
        if self.headers.get_content_type() != "application/json":
            return self.respond(415, {"error": "Use application/json"})
        if self.headers.get("Transfer-Encoding"):
            return self.respond(400, {"error": "Expected a single Content-Length"})
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not lengths[0].isdecimal():
                raise ValueError("Expected a single numeric Content-Length")
            length = int(lengths[0])
            if length > 4096:
                return self.respond(413, {"error": "Order payload exceeds 4096 bytes"})
            order = Order.parse(json.loads(self.rfile.read(length).decode("utf-8")))
            added = service.accept(order)
        except (ValueError, UnicodeError, RecursionError):
            return self.respond(400, {"error": "Invalid order payload"})
        except Conflict:
            return self.respond(409, {"error": "The order ID belongs to a different purchase"})
        except STORAGE_ERRORS:
            return self.respond(503, {"error": "Acceptance could not be confirmed"})
        self.respond(201 if added else 200, asdict(order))


def make_server(database_url, journal_path=None, host="127.0.0.1", port=0, read_only=False):
    if not read_only and journal_path is None:
        raise ValueError("A writable source needs an independent JOURNAL_PATH")
    journal = None if read_only else ReceiptJournal(journal_path)
    server = ThreadingHTTPServer((host, port), OrdersHandler)
    server.order_service = OrderService(OrderDatabase(database_url, read_only), journal)
    return server
