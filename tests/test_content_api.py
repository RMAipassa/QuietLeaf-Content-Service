import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from quietleaf_service.adapters import CatalogSource, HtmlSource, SourceError
from quietleaf_service.app import make_app


@pytest.fixture
def client():
    catalog = Path(__file__).parent.parent / "catalog"
    return TestClient(make_app([CatalogSource(catalog)], "local-dev-token"))


def auth():
    return {"Authorization": "Bearer local-dev-token"}


def test_auth(client):
    assert client.get("/v1/health").status_code == 401
    health = client.get("/v1/health", headers=auth())
    assert health.status_code == 200
    assert health.json()["service"] == "quietleaf-content-api"


def test_search_details_and_sorted_chapters(client):
    found = client.get("/v1/search", params={"q": "lighthouse"}, headers=auth()).json()["items"]
    assert len(found) == 1
    assert found[0]["source"] == "collection"
    assert found[0]["id"] == "lighthouse-demo"
    assert client.get("/v1/search", params={"genre": "Romance"}, headers=auth()).json()["items"] == []
    url = "/v1/books/collection/lighthouse-demo"
    assert client.get(url, headers=auth()).json()["title"] == "At the Lighthouse"
    chapters = client.get(url + "/chapters", headers=auth()).json()["items"]
    assert [c["ordinal"] for c in chapters] == [0, 1, 2]
    assert [c["downloadable"] for c in chapters] == [True, True, False]
    assert "Mara" in client.get(url + "/chapters/chapter-1/text", headers=auth()).text
    assert "sunrise" in client.get(url + "/chapters/chapter-2/text", headers=auth()).text
    assert client.get(url + "/chapters/chapter-3/text", headers=auth()).status_code == 422


def test_source_error_and_unknown_id(client):
    assert client.get("/v1/books/nope/book", headers=auth()).status_code == 404
    assert client.get("/v1/books/collection/unknown", headers=auth()).status_code == 422
    assert client.get("/v1/books/collection/%2E%2E", headers=auth()).status_code in {404, 422}


def test_catalog_prevents_path_escape(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET")
    root = tmp_path / "safe"
    root.mkdir()
    (root / "catalog.json").write_text(json.dumps({"books": [{"id": "book", "title": "Book", "chapters": [
        {"id": "ch", "title": "One", "file": "../outside.txt", "copyPermitted": True}]}]}))
    source = CatalogSource(root)
    with pytest.raises(SourceError):
        source.text("book", "ch")


def make_html_adapter(responder):
    config = {
        "id": "authorized", "name": "Authorized sample", "baseUrl": "https://allowed.example",
        "accessAuthorized": True, "minIntervalSeconds": 1,
        "paths": {"search": "/search?q={query}", "recent": "/recent", "book": "/books/{id}", "chapter": "/books/{id}/{chapter}"},
        "selectors": {"result": ".book", "resultLink": "a.title", "resultAuthor": ".author", "title": "h1", "author": ".author", "chapterLink": ".chapters a", "chapterBody": "article"}
    }
    adapter = HtmlSource(config, httpx.Client(transport=httpx.MockTransport(responder), follow_redirects=False))
    adapter.min_interval = 0
    return adapter


def test_html_adapter_fixture_and_stable_ids():
    def respond(request):
        paths = {
            "/robots.txt": "User-agent: *\nDisallow: /private\n",
            "/search": '<div class="book"><a class="title" href="/books/book1">Book One</a><span class="author">A. Writer</span></div>',
            "/books/book1": '<h1>Book One</h1><div class="author">A. Writer</div><div class="chapters"><a href="/books/book1/ch-1">First chapter</a><a href="/books/book1/ch-2">Second</a><a href="/books/book1/ch-2">Second again</a></div>',
            "/books/book1/ch-1": "<article><p>Original paragraph.</p><script>junk</script></article>"
        }
        return httpx.Response(200 if request.url.path in paths else 404, text=paths.get(request.url.path, ""))
    source = make_html_adapter(respond)
    assert source.search("book")[0]["id"] == "book1"
    assert source.details("book1")["title"] == "Book One"
    assert [c["id"] for c in source.chapters("book1")] == ["ch-1", "ch-2"]
    assert source.text("book1", "ch-1") == "Original paragraph."
    assert source._stable_id("https://allowed.example/books/book1") == "book1"
    with pytest.raises(SourceError):
        source._guard_url("https://wrong.example/steal")


def test_html_adapter_obeys_robots():
    def respond(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /search\n")
        return httpx.Response(200, text="OK")
    source = make_html_adapter(respond)
    with pytest.raises(SourceError, match="robots.txt"):
        source.search("blocked")


def test_html_source_requires_authorization():
    with pytest.raises(SourceError, match="authorization"):
        HtmlSource({"id": "bad", "name": "No", "baseUrl": "https://allowed.example", "accessAuthorized": False})


def test_aggregate_search_survives_failed_source():
    class OfflineSource:
        id = "offline"
        name = "Offline"

        def search(self, query, genre=""):
            raise RuntimeError("Connection refused")

        def recent(self):
            raise RuntimeError("Connection refused")

    catalog = CatalogSource(Path(__file__).parent.parent / "catalog")
    client = TestClient(make_app([catalog, OfflineSource()]))
    result = client.get("/v1/search", params={"q": "lighthouse"}).json()
    assert result["items"][0]["id"] == "lighthouse-demo"
    assert result["errors"] == [{"source": "offline", "message": "Source temporarily unavailable"}]
