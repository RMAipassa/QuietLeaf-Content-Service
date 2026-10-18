import asyncio
import copy
import threading
import time

import httpx
from fastapi.testclient import TestClient

from quietleaf_service.adapters import SourceError
from quietleaf_service.app import make_app
from quietleaf_service.home import HomeFeedWorker


CATEGORIES = ("popular", "completed", "fresh", "new", "hot")
HEADERS = {"Authorization": "Bearer local-token"}


def feed(version):
    item = {
        "id": f"book-{version}", "title": f"Book {version}", "author": "Writer",
        "description": "", "coverUrl": "", "genre": "Test",
    }
    return {category: [item] for category in CATEGORIES}


class ControlledHomeSource:
    id = "controlled"
    name = "Controlled"

    def __init__(self):
        self.cache = None
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.first_complete = threading.Event()
        self.second_started = threading.Event()
        self.release_second = threading.Event()
        self.lock = threading.Lock()

    def home_feed(self):
        with self.lock:
            if self.cache is None:
                raise SourceError("Home feed has not been refreshed")
            return copy.deepcopy(self.cache)

    def refresh_home_feed(self, stop_event):
        with self.lock:
            self.calls += 1
            call = self.calls
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if call == 2:
                self.second_started.set()
                while not self.release_second.wait(0.01):
                    if stop_event.is_set():
                        return
            if stop_event.is_set():
                return
            with self.lock:
                self.cache = feed(call)
        finally:
            with self.lock:
                self.active -= 1
            if call == 1:
                self.first_complete.set()


class RestartableHomeSource:
    id = "restartable"
    name = "Restartable"

    def __init__(self):
        self.cache = None
        self.calls = 0
        self.condition = threading.Condition()

    def home_feed(self):
        with self.condition:
            if self.cache is None:
                raise SourceError("Home feed has not been refreshed")
            return copy.deepcopy(self.cache)

    def refresh_home_feed(self, stop_event):
        if stop_event.is_set():
            return
        with self.condition:
            self.calls += 1
            self.cache = feed(self.calls)
            self.condition.notify_all()

    def wait_for_calls(self, count):
        deadline = time.monotonic() + 1
        with self.condition:
            while self.calls < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)
            return True


def test_startup_refresh_is_immediate_and_cold_reads_fail_fast():
    source = ControlledHomeSource()
    original_refresh = source.refresh_home_feed
    startup_started = threading.Event()
    release_startup = threading.Event()

    def blocked_startup_refresh(stop_event):
        startup_started.set()
        while not release_startup.wait(0.01):
            if stop_event.is_set():
                return
        original_refresh(stop_event)

    source.refresh_home_feed = blocked_startup_refresh
    app = make_app([source], "local-token")
    with TestClient(app) as client:
        assert app.state.home_feed_worker.source_count == 1
        assert startup_started.wait(1)
        cold = client.get("/v1/home", headers=HEADERS)
        assert all(cold.json()[category] == [] for category in CATEGORIES)
        assert cold.json()["errors"] == [
            {"source": "controlled", "message": "Source temporarily unavailable"}
        ]

        release_startup.set()
        assert source.first_complete.wait(1)
        warm = client.get("/v1/home", headers=HEADERS).json()
        assert warm["popular"][0]["title"] == "Book 1"


def test_manual_refresh_waits_while_get_serves_previous_snapshot():
    source = ControlledHomeSource()
    app = make_app([source], "local-token")
    responses = []

    with TestClient(app) as client:
        assert source.first_complete.wait(1)

        request = threading.Thread(
            target=lambda: responses.append(client.delete("/v1/home/cache", headers=HEADERS))
        )
        request.start()
        assert source.second_started.wait(1)

        during = client.get("/v1/home", headers=HEADERS).json()
        assert during["popular"][0]["title"] == "Book 1"
        assert request.is_alive()

        source.release_second.set()
        request.join(1)
        assert not request.is_alive()
        assert responses[0].json() == {"clearedSources": 1}
        after = client.get("/v1/home", headers=HEADERS).json()
        assert after["popular"][0]["title"] == "Book 2"
        assert source.max_active == 1


def test_delete_saturation_does_not_starve_get_and_coalesces_one_cycle():
    async def exercise():
        source = ControlledHomeSource()
        app = make_app([source], "local-token")
        request_count = 60

        async with app.router.lifespan_context(app):
            worker = app.state.home_feed_worker
            original_request_refresh = worker.request_refresh
            registrations = 0

            def observed_request_refresh():
                nonlocal registrations
                target = original_request_refresh()
                registrations += 1
                return target

            worker.request_refresh = observed_request_refresh
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                deadline = asyncio.get_running_loop().time() + 1
                while not source.first_complete.is_set():
                    assert asyncio.get_running_loop().time() < deadline
                    await asyncio.sleep(0.005)

                deletes = [asyncio.create_task(client.delete("/v1/home/cache", headers=HEADERS))
                           for _ in range(request_count)]
                deadline = asyncio.get_running_loop().time() + 2
                while registrations < request_count or not source.second_started.is_set():
                    assert asyncio.get_running_loop().time() < deadline
                    await asyncio.sleep(0.005)

                current = await asyncio.wait_for(
                    client.get("/v1/home", headers=HEADERS), timeout=0.5
                )
                assert current.json()["popular"][0]["title"] == "Book 1"
                assert source.calls == 2

                source.release_second.set()
                responses = await asyncio.wait_for(asyncio.gather(*deletes), timeout=2)
                assert all(response.json() == {"clearedSources": 1}
                           for response in responses)
                assert source.calls == 2
                assert source.max_active == 1

    asyncio.run(exercise())


def test_shutdown_is_cooperative_and_join_is_bounded():
    class CooperativeSource:
        id = "cooperative"
        name = "Cooperative"

        def __init__(self):
            self.started = threading.Event()
            self.stopped = threading.Event()

        def refresh_home_feed(self, stop_event):
            self.started.set()
            stop_event.wait()
            self.stopped.set()

    cooperative = CooperativeSource()
    worker = HomeFeedWorker([cooperative], stop_timeout_seconds=0.1)
    worker.start()
    assert cooperative.started.wait(1)
    started = time.monotonic()
    worker.stop()
    assert time.monotonic() - started < 0.5
    assert cooperative.stopped.is_set()
    assert not worker.thread.is_alive()

    release = threading.Event()

    class UncooperativeSource:
        id = "uncooperative"
        name = "Uncooperative"

        def __init__(self):
            self.started = threading.Event()

        def refresh_home_feed(self, _stop_event):
            self.started.set()
            release.wait()

    uncooperative = UncooperativeSource()
    worker = HomeFeedWorker([uncooperative], stop_timeout_seconds=0.05)
    worker.start()
    assert uncooperative.started.wait(1)
    started = time.monotonic()
    worker.stop()
    assert time.monotonic() - started < 0.5
    assert worker.thread.is_alive()
    release.set()
    worker.thread.join(1)
    assert not worker.thread.is_alive()


def test_refresh_failure_does_not_block_other_sources():
    class FailedSource:
        id = "failed"
        name = "Failed"

        def __init__(self):
            self.calls = 0

        def refresh_home_feed(self, _stop_event):
            self.calls += 1
            raise RuntimeError("offline")

    failed = FailedSource()
    healthy = RestartableHomeSource()
    worker = HomeFeedWorker([failed, healthy])
    worker.start()
    try:
        assert healthy.wait_for_calls(1)
        assert failed.calls == 1
    finally:
        worker.stop()


def test_home_worker_can_run_a_second_lifespan():
    source = RestartableHomeSource()
    app = make_app([source])
    worker = app.state.home_feed_worker

    with TestClient(app) as client:
        assert source.wait_for_calls(1)
        first_thread = worker.thread
        first_stop_event = worker.stop_event
        assert client.get("/v1/home").json()["popular"][0]["title"] == "Book 1"

    assert not first_thread.is_alive()
    with TestClient(app) as client:
        assert source.wait_for_calls(2)
        assert worker.thread is not first_thread
        assert worker.stop_event is not first_stop_event
        assert client.get("/v1/home").json()["popular"][0]["title"] == "Book 2"

    assert not worker.thread.is_alive()
