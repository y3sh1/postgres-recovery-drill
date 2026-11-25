"""Durably accept an order and keep a receipt independent of the database backup."""
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3

import psycopg


ORDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


class Conflict(Exception):
    """This order ID already identifies a different purchase."""


class StorageMismatch(Exception):
    """An accepted receipt and the current source database disagree."""


@dataclass(frozen=True)
class Order:
    order_id: str
    amount_cents: int
    currency: str

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or not {"order_id", "amount_cents"} <= data.keys():
            raise ValueError("Expected an order_id and amount_cents")
        if set(data) - {"order_id", "amount_cents", "currency"}:
            raise ValueError("Unexpected order field")
        order_id = data["order_id"]
        amount = data["amount_cents"]
        currency = data.get("currency", "USD")
        if not isinstance(order_id, str) or not ORDER_ID.fullmatch(order_id):
            raise ValueError("Use a 1-64 character order ID with letters, digits, underscores or hyphens")
        if type(amount) is not int or not 1 <= amount <= 2**63 - 1:
            raise ValueError("amount_cents must be a positive 64-bit integer")
        if currency != "USD":
            raise ValueError("This shop accepts USD orders")
        return cls(order_id, amount, currency)

    def values(self):
        return self.order_id, self.amount_cents, self.currency


def connect(dsn, read_only=False):
    options = "-c statement_timeout=5000"
    if read_only:
        options += " -c default_transaction_read_only=on"
    return psycopg.connect(dsn, connect_timeout=3, options=options)


def init_database(database_url):
    """Initialize the source explicitly. Starting a recovery server never runs DDL."""
    with connect(database_url) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS public.orders (
                order_id text PRIMARY KEY,
                amount_cents bigint NOT NULL CHECK (amount_cents > 0),
                currency text NOT NULL CHECK (currency = 'USD')
            )
        """)


class OrderDatabase:
    def __init__(self, dsn, read_only=False):
        self.dsn = dsn
        self.read_only = read_only

    def ready(self):
        with connect(self.dsn, self.read_only) as db:
            db.execute("SELECT order_id, amount_cents, currency FROM public.orders LIMIT 0")

    def get(self, order_id):
        with connect(self.dsn, self.read_only) as db:
            row = db.execute("""
                SELECT order_id, amount_cents, currency FROM public.orders WHERE order_id = %s
            """, (order_id,)).fetchone()
        return Order(*row) if row else None

    def insert(self, order):
        with connect(self.dsn, self.read_only) as db:
            added = db.execute("""
                INSERT INTO public.orders (order_id, amount_cents, currency) VALUES (%s, %s, %s)
                ON CONFLICT (order_id) DO NOTHING RETURNING order_id
            """, order.values()).fetchone()
            if added is None:
                row = db.execute("""
                    SELECT order_id, amount_cents, currency FROM public.orders WHERE order_id = %s
                """, (order.order_id,)).fetchone()
                if row != order.values():
                    raise Conflict
        return added is not None


class ReceiptJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS receipts (
                    order_id TEXT PRIMARY KEY NOT NULL,
                    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                    currency TEXT NOT NULL CHECK (currency = 'USD'),
                    accepted_at TEXT NOT NULL
                )
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def get(self, order_id):
        with closing(self.connect()) as db:
            row = db.execute("""
                SELECT order_id, amount_cents, currency FROM receipts WHERE order_id = ?
            """, (order_id,)).fetchone()
        return Order(*row) if row else None

    def append(self, order):
        with closing(self.connect()) as db, db:
            db.execute("""
                INSERT INTO receipts (order_id, amount_cents, currency, accepted_at)
                VALUES (?, ?, ?, ?) ON CONFLICT (order_id) DO NOTHING
            """, (*order.values(), datetime.now(timezone.utc).isoformat()))
            recorded = db.execute("""
                SELECT order_id, amount_cents, currency FROM receipts WHERE order_id = ?
            """, (order.order_id,)).fetchone()
            if recorded != order.values():
                raise StorageMismatch


class OrderService:
    def __init__(self, database, journal=None):
        self.database = database
        self.journal = journal

    def accept(self, order):
        # A receipt survives an incident. A retry must never hide data loss on the source.
        receipt = self.journal.get(order.order_id)
        if receipt is not None:
            if receipt != order:
                raise Conflict
            if self.database.get(order.order_id) != receipt:
                raise StorageMismatch
            return False

        # If receipt storage fails after this commit, a retry can finish the receipt.
        added = self.database.insert(order)
        self.journal.append(order)
        return added
