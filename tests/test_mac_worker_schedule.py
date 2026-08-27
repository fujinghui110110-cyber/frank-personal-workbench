from __future__ import annotations

import threading
import time

import sys

import pytest

from scripts import mac_worker
from scripts.mac_worker import (
    DEFAULT_ANALYSIS_CONCURRENCY,
    MAX_ANALYSIS_CONCURRENCY,
    SYNC_INTERVAL_SECONDS,
    error_text,
    scheduled_sync_reason,
)


def test_analysis_queue_uses_bounded_concurrency_and_reconciles_once(monkeypatch) -> None:
    state = {"remaining": 50, "active": 0, "max_active": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(50)

    def fake_process_once(_client, _worker_id):
        with lock:
            if state["remaining"] <= 0:
                return False
            state["remaining"] -= 1
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        barrier.wait(timeout=5)
        with lock:
            state["active"] -= 1
        return True

    class Client:
        calls = 0

        def request_json(self, method, path, payload=None):
            if (method, path) == ("GET", "/api/analysis/status"):
                return {"pending": 50}
            assert (method, path) == ("POST", "/api/analysis/reconcile")
            self.calls += 1
            return {"merged": 2, "ignored": 1, "continued": 1}

    client = Client()
    monkeypatch.setattr("scripts.mac_worker.process_once", fake_process_once)
    completed = __import__("scripts.mac_worker", fromlist=["drain_pending_work"]).drain_pending_work(
        client, "mac-test", DEFAULT_ANALYSIS_CONCURRENCY
    )
    assert completed == 50
    assert state["max_active"] == 50
    assert client.calls == 1


def test_analysis_concurrency_defaults_to_300_and_caps_at_500(monkeypatch) -> None:
    captured: list[int] = []

    class FakePool:
        def __init__(self, max_workers, **_kwargs):
            captured.append(max_workers)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def map(self, fn, items):
            return [fn(item) for item in items]

    monkeypatch.setattr(mac_worker, "ThreadPoolExecutor", FakePool)
    monkeypatch.setattr(mac_worker, "process_once", lambda *_: False)

    class Client:
        def request_json(self, method, path, payload=None):
            assert (method, path) == ("GET", "/api/analysis/status")
            return {"pending": 1000}

    assert DEFAULT_ANALYSIS_CONCURRENCY == 300
    assert MAX_ANALYSIS_CONCURRENCY == 500
    assert mac_worker.drain_pending_work(Client(), "mac-test", 500) == 0
    assert mac_worker.drain_pending_work(Client(), "mac-test", 501) == 0
    assert captured == [500, 500]


def test_worker_error_report_is_bounded() -> None:
    assert len(error_text(RuntimeError("x" * 2000))) == 1000


def test_workday_sync_schedule_covers_startup_wake_and_two_hours() -> None:
    now = 10_000.0
    assert scheduled_sync_reason(now, None, None, 0) == "startup"
    assert scheduled_sync_reason(now, now - 61, now - 30, 0) == "wake"
    assert (
        scheduled_sync_reason(now, now - 15, now - SYNC_INTERVAL_SECONDS, 0)
        == "interval"
    )
    assert scheduled_sync_reason(now, now - 15, now - 60, 0) is None
    assert scheduled_sync_reason(now, None, None, 5) is None


def test_failed_chat_sync_does_not_starve_manual_analysis(monkeypatch) -> None:
    sync_waiting = True
    job_claims = 0

    class Client:
        def request_json(self, method, path, payload=None):
            nonlocal sync_waiting, job_claims
            if path in {"/api/nodes/heartbeat", "/api/wechat/export/report"}:
                return {}
            if path == "/api/wechat/sync/claim":
                if not sync_waiting:
                    return {"request": None}
                sync_waiting = False
                return {
                    "request": {
                        "id": "sync-1",
                        "source": "personal_wechat",
                        "mode": "incremental",
                    }
                }
            if path == "/api/wechat/sync/sync-1/finish":
                return {}
            if path == "/api/wechat/sync/run":
                sync_waiting = True
                return {}
            if path == "/api/jobs/claim":
                job_claims += 1
                return {"job": None}
            raise AssertionError(f"未预期的请求：{method} {path}")

    monkeypatch.setattr(mac_worker, "email_configured", lambda: False)
    monkeypatch.setattr(
        mac_worker,
        "run_ciphertalk_sync",
        lambda *_: (_ for _ in ()).throw(RuntimeError("CipherTalk 尚未准备好")),
    )

    assert mac_worker.process_once(Client(), "mac-test") is True
    assert mac_worker.process_once(Client(), "mac-test") is False
    assert job_claims == 1


def test_email_worker_maps_processed_to_scanned_count(monkeypatch) -> None:
    finished: list[dict] = []

    class Client:
        def request_json(self, method, path, payload=None):
            if path == "/api/nodes/heartbeat":
                return {}
            if path == "/api/email/sync/claim":
                return {
                    "request": {"id": "email-sync-1"},
                }
            if path == "/api/email/sync/email-sync-1/finish":
                finished.append(payload)
                return {}
            raise AssertionError((method, path, payload))

    monkeypatch.setattr(mac_worker, "email_configured", lambda: True)
    monkeypatch.setattr(
        mac_worker,
        "run_email_sync",
        lambda *_: {
            "processed": 3,
            "pending_count": 2,
            "ignored_count": 1,
        },
    )

    assert mac_worker.process_once(Client(), "mac-test") is True
    assert finished[0]["scanned_count"] == 3


def test_worker_waits_when_service_is_not_ready(monkeypatch, capsys) -> None:
    class Client:
        def request_json(self, *_args, **_kwargs):
            raise RuntimeError("连接被拒绝")

    monkeypatch.setattr(mac_worker, "WorkbenchClient", lambda *_: Client())
    monkeypatch.setattr(
        mac_worker,
        "scan_icloud_inbox",
        lambda *_: (_ for _ in ()).throw(RuntimeError("服务未就绪")),
    )
    monkeypatch.setattr(
        mac_worker,
        "process_once",
        lambda *_: (_ for _ in ()).throw(RuntimeError("服务未就绪")),
    )
    monkeypatch.setattr(mac_worker.time, "sleep", lambda *_: (_ for _ in ()).throw(StopIteration))
    monkeypatch.setattr(sys, "argv", ["mac_worker", "--once", "--poll-seconds", "2"])

    with pytest.raises(StopIteration):
        mac_worker.main()

    output = capsys.readouterr().out
    assert "工作台暂未就绪" in output
    assert "Traceback" not in output
