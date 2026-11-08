import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from quietleaf_service.archive import ArchiveStore
from quietleaf_service.app import make_app
from quietleaf_service.metadata import publication_status
from quietleaf_service.novelping import NovelPingSource
from quietleaf_service.wtrlab import WtrLabSource
from bs4 import BeautifulSoup


@pytest.mark.parametrize(("value", "expected"), [
    ("OnGoing", "ongoing"), ("On-going", "ongoing"), ("In progress", "ongoing"),
    ("Completed", "completed"), ("Complete", "completed"), ("Finished", "completed"),
    (None, "unknown"), (True, "unknown"), (0, "unknown"), ("Hiatus", "unknown"),
])
def test_status_normalization_does_not_guess_when_unknown(value, expected):
    assert publication_status(value) == expected


@pytest.mark.parametrize(("raw", "expected"), [(0, "ongoing"), (1, "completed"), (2, "unknown"), (True, "unknown"), (None, "unknown")])
def test_wtrlab_uses_its_verified_numeric_status_contract(raw, expected):
    source = WtrLabSource(access_authorized=True)
    assert source._book({"raw_id": 2, "status": raw, "data": {"title": "Original test book"}})["publicationStatus"] == expected


def test_novelping_search_card_context_preserves_author_and_explicit_status():
    def respond(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/search":
            return httpx.Response(200, text='<div class="novel-item"><h3 class="novel-title"><a href="/book/test">Test Book</a></h3><span class="author">Demo Writer</span><span class="badge">Completed</span></div>')
        return httpx.Response(200, text='<meta property="og:novel:status" content="OnGoing"><h1>Test Book</h1>')
    source = NovelPingSource(client=httpx.Client(transport=httpx.MockTransport(respond)), access_authorized=True)
    source.min_interval = 0
    result = source.search("Demo Writer")[0]
    assert result["author"] == "Demo Writer" and result["publicationStatus"] == "completed"
    assert source.details("test")["publicationStatus"] == "ongoing"


def test_novelping_does_not_treat_words_in_a_book_title_as_status_metadata():
    source = NovelPingSource(access_authorized=True)
    page = BeautifulSoup('<div class="novel-item"><h3 class="novel-title"><a href="/book/test">Status: Completed</a></h3></div>', "html.parser")
    assert source._book_entries(page)[0]["publicationStatus"] == "unknown"


def test_archive_publication_status_migrates_and_persists_without_changing_chapters(tmp_path):
    with sqlite3.connect(tmp_path / "archive.sqlite3") as db:
        db.execute("""CREATE TABLE books(source TEXT NOT NULL,id TEXT NOT NULL,title TEXT NOT NULL,
            author TEXT NOT NULL DEFAULT '',description TEXT NOT NULL DEFAULT '',cover_url TEXT NOT NULL DEFAULT '',
            genre TEXT NOT NULL DEFAULT '',updated_at INTEGER NOT NULL,PRIMARY KEY(source,id))""")
        db.execute("INSERT INTO books(source,id,title,updated_at) VALUES('fixture','book','Original title',1)")
    store = ArchiveStore(tmp_path)
    assert store.book("fixture", "book")["publicationStatus"] == "unknown"
    store.save_chapters("fixture", "book", [{"id": "one", "title": "One", "ordinal": 0}])
    store.save_text("fixture", "book", "one", "Original stored text.")
    job = store.enqueue("fixture", "book")
    claim = store.next_job()
    assert store.save_metadata_if_running(job["id"], claim["run_token"], "fixture", {
        "id": "book", "title": "Original title", "publicationStatus": "completed"}, [{"id": "one", "title": "One", "ordinal": 0}])
    store.update_job_if_running(job["id"], claim["run_token"], status="completed")
    restarted = ArchiveStore(tmp_path)
    assert restarted.book("fixture", "book")["publicationStatus"] == "completed"
    assert restarted.text("fixture", "book", "one") == "Original stored text."


def test_metadata_refresh_updates_status_without_rearchiving_and_cards_reuse_archived_status(tmp_path):
    class Source:
        id = "fixture"
        name = "Fixture"
        supports_genre_search = False
        calls = 0
        offline = False

        def details(self, book_id):
            self.calls += 1
            if self.offline:
                raise RuntimeError("offline")
            return {"id": book_id, "title": "Book", "publicationStatus": "completed"}

        def search(self, *_):
            return [{"id": "book", "title": "Book", "publicationStatus": "unknown"}]

        def home_feed(self):
            return {"new": self.search()}

    source = Source()
    app = make_app([source], "test-token", tmp_path)
    auth = {"Authorization": "Bearer test-token"}
    store = app.state.archive_store
    store.save_book(source.id, {"id": "book", "title": "Book"})
    store.save_chapters(source.id, "book", [{"id": "one", "title": "One", "ordinal": 0}])
    store.save_text(source.id, "book", "one", "Original stored text.")
    with TestClient(app) as api:
        assert api.get("/v1/books/fixture/book", headers=auth).json()["publicationStatus"] == "unknown"
        assert source.calls == 0
        refreshed = api.get("/v1/books/fixture/book?refresh=true", headers=auth)
        assert refreshed.status_code == 200 and refreshed.json()["publicationStatus"] == "completed"
        assert store.jobs() == [] and store.text(source.id, "book", "one") == "Original stored text."
        assert api.get("/v1/search?q=Book&source=fixture", headers=auth).json()["items"][0]["publicationStatus"] == "completed"
        assert api.get("/v1/home?source=fixture", headers=auth).json()["new"][0]["publicationStatus"] == "completed"
        assert api.get("/v1/health", headers=auth).json()["sourceCapabilities"]["fixture"]["genreSearch"] is False
        source.offline = True
        assert api.get("/v1/books/fixture/book?refresh=true", headers=auth).status_code == 502
        assert api.get("/v1/books/fixture/book", headers=auth).json()["publicationStatus"] == "completed"
