from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import http.client
import json
import os
import sqlite3
from threading import Lock, Thread
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from shop import init_database, make_server
from shop.store import Conflict, Order, OrderService, ReceiptJournal, StorageMismatch


class MemoryOrders:
    """Unit substitute for PostgreSQL. The full drill uses real database commits."""
    def __init__(self):
        self.rows = {}
        self.lock = Lock()
        self.available = True

    def ready(self):
        if not self.available:
            raise OSError("offline")

    def insert(self, order):
        with self.lock:
            current = self.rows.get(order.order_id)
            if current is not None and current != order:
                raise Conflict
            self.rows[order.order_id] = order
            return current is None

    def get(self, order_id):
        return self.rows.get(order_id)


@pytest.fixture
def order():
    return Order("ORD-001", 2899, "USD")


def test_retries_preserve_a_single_receipt_and_timestamp(tmp_path, order):
    path = tmp_path / "receipts.sqlite"
    service = OrderService(MemoryOrders(), ReceiptJournal(path))
    assert service.accept(order) is True
    with closing(sqlite3.connect(path)) as conn:
        before = conn.execute("SELECT * FROM receipts").fetchall()
        assert service.accept(order) is False
        assert conn.execute("SELECT * FROM receipts").fetchall() == before
    reopened = ReceiptJournal(path)
    assert reopened.get(order.order_id) == order


def test_journal_failure_after_commit_recovers_on_retry(tmp_path, order, monkeypatch):
    journal = ReceiptJournal(tmp_path / "receipts.sqlite")
    service = OrderService(MemoryOrders(), journal)
    def lose_receipt(_):
        raise sqlite3.OperationalError("unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(journal, "append", lose_receipt)
        with pytest.raises(sqlite3.OperationalError):
            service.accept(order)
    assert service.database.get(order.order_id) == order
    assert journal.get(order.order_id) is None
    assert service.accept(order) is False
    assert journal.get(order.order_id) == order


def test_receipt_conflict_and_missing_source_fail_closed(tmp_path, order):
    service = OrderService(MemoryOrders(), ReceiptJournal(tmp_path / "receipts.sqlite"))
    service.accept(order)
    wrong = Order(order.order_id, 1, "USD")
    with pytest.raises(Conflict):
        service.accept(wrong)
    del service.database.rows[order.order_id]
    with pytest.raises(StorageMismatch):
        service.accept(order)
    assert service.database.get(order.order_id) is None
    assert service.journal.get(order.order_id) == order


def test_concurrent_retries_converge(tmp_path, order):
    service = OrderService(MemoryOrders(), ReceiptJournal(tmp_path / "receipts.sqlite"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        wrote = list(pool.map(service.accept, [order] * 12))
    assert sum(wrote) == 1
    with closing(sqlite3.connect(service.journal.path)) as conn:
        assert conn.execute("SELECT count(*) FROM receipts").fetchone() == (1,)


@contextmanager
def serving(server):
    with server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            thread.join(timeout=2)


def request(server, method, path, data=None):
    with closing(http.client.HTTPConnection(*server.server_address, timeout=3)) as client:
        headers = {"Content-Type": "application/json"}
        client.request(method, path, json.dumps(data) if data is not None else None, headers)
        response = client.getresponse()
        return response.status, json.loads(response.read())


def test_http_creation_replay_conflict_and_readiness(tmp_path):
    server = make_server("unused", tmp_path / "receipts.sqlite")
    server.order_service.database = MemoryOrders()
    data = {"order_id": "ORD-001", "amount_cents": 2899}
    result = {**data, "currency": "USD"}
    with serving(server):
        assert request(server, "GET", "/health/ready") == (200, {"status": "ready"})
        assert request(server, "POST", "/orders", data) == (201, result)
        assert request(server, "POST", "/orders", data) == (200, result)
        assert request(server, "POST", "/orders", {**data, "amount_cents": 99})[0] == 409
        assert request(server, "GET", "/orders/ORD-001") == (200, result)
        assert request(server, "GET", "/orders/missing")[0] == 404
        server.order_service.database.available = False
        assert request(server, "GET", "/health/ready")[0] == 503


def test_recovery_reads_do_not_open_journal_or_mutate(tmp_path, order):
    path = tmp_path / "unopened" / "receipts.sqlite"
    server = make_server("unused", path, read_only=True)
    server.order_service.database = MemoryOrders()
    server.order_service.database.insert(order)
    with serving(server):
        assert request(server, "GET", "/orders/ORD-001")[1]["amount_cents"] == 2899
        assert request(server, "POST", "/orders", {"order_id": "ORD-002", "amount_cents": 10})[0] == 403
        assert request(server, "GET", "/orders/ORD-002")[0] == 404
    assert not path.parent.exists()


@pytest.mark.parametrize("override", [
    {"amount_cents": True}, {"amount_cents": 1.2}, {"amount_cents": 2**63},
    {"amount_cents": 0}, {"order_id": "bad/identifier"}, {"currency": "EUR"},
])
def test_invalid_purchase_is_rejected(override):
    with pytest.raises(ValueError):
        Order.parse({"order_id": "ORD-001", "amount_cents": 10, **override})


def test_live_concurrent_orders_and_read_only_recovery(tmp_path):
    admin = os.environ.get("SHOP_TEST_DSN")
    if not admin:
        pytest.skip("Set SHOP_TEST_DSN to the disposable PostgreSQL admin connection")
    name = "shop_test_" + uuid4().hex
    db_name = sql.Identifier(name)
    isolated = make_conninfo(admin, dbname=name)
    data = {"order_id": "ORD-099", "amount_cents": 1900, "currency": "USD"}
    journal = tmp_path / "source.sqlite"
    recovery_path = tmp_path / "unused-receipts.sqlite"
    with psycopg.connect(admin, autocommit=True, connect_timeout=5) as controller:
        controller.execute(sql.SQL("CREATE DATABASE {}").format(db_name))
        try:
            recovery = make_server(isolated, recovery_path, read_only=True)
            with serving(recovery):
                assert request(recovery, "GET", "/health/ready")[0] == 503
                init_database(isolated)
                source = make_server(isolated, journal)
                with serving(source), ThreadPoolExecutor(max_workers=4) as pool:
                    writes = list(pool.map(lambda _: request(source, "POST", "/orders", data), range(8)))
                    assert sorted(status for status, _ in writes) == [200] * 7 + [201]
                    assert all(order == data for _, order in writes)
                    assert request(source, "POST", "/orders", {**data, "amount_cents": 8})[0] == 409
                assert request(recovery, "GET", "/health/ready")[0] == 200
                assert request(recovery, "GET", "/orders/ORD-099") == (200, data)
                assert request(recovery, "POST", "/orders", data)[0] == 403
                with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                    recovery.order_service.database.insert(Order("ORD-100", 10, "USD"))
            with psycopg.connect(isolated) as database:
                assert database.execute("SELECT count(*) FROM public.orders").fetchone() == (1,)
            with closing(sqlite3.connect(journal)) as receipts:
                assert receipts.execute("SELECT count(*) FROM receipts").fetchone() == (1,)
            assert not recovery_path.exists()
        finally:
            controller.execute(sql.SQL("DROP DATABASE {}").format(db_name))
