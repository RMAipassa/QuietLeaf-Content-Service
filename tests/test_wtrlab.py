import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient

from quietleaf_service.adapters import SourceError
from quietleaf_service.app import build_from_env, make_app
from quietleaf_service.archive import ArchiveStore
from quietleaf_service.metadata import display_title
from quietleaf_service import wtrlab
from quietleaf_service.wtrlab import GLOSSARY_KEY, WtrLabSource


BOOK_ID = "113338"
SLUG = "test-novel"
RECORD = {
    "id": 100,
    "raw_id": 113338,
    "slug": SLUG,
    "chapter_count": 3,
    "genres": [1, 9, 38],
    "data": {
        "title": "Test Novel",
        "author": "Demo Writer",
        "description": "<p>An original <b>test</b> story.</p>",
        "image": "https://img.wtr-lab.com/cdn/series/test",
        "raw": {"title": "Raw Test Novel", "author": "Raw Writer"},
    },
}


def next_page(key, value):
    payload = {"props": {"pageProps": {key: value}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'


def routes(request):
    path = request.url.path
    if path == "/api/search":
        assert request.method == "POST"
        assert json.loads(request.content) == {"text": "test novel"}
        return httpx.Response(200, json={"success": True, "data": [RECORD]})
    if path == "/api/novel/get-by-raw-id":
        assert request.method == "POST"
        assert json.loads(request.content) == {"raw_id": 113338}
        return httpx.Response(200, json={"success": True, "novel": {
            "id": 100, "raw_id": 113338, "slug": SLUG,
        }})
    if path == f"/en/novel/{BOOK_ID}/{SLUG}":
        return httpx.Response(200, text=next_page("serie", {"serie_data": RECORD}))
    if path == f"/api/chapters/{BOOK_ID}":
        assert dict(request.url.params) == {"start": "1", "end": "3"}
        return httpx.Response(200, json={"chapters": [
            {"id": 30, "order": 3, "title": "Third", "locked": True},
            {"id": 10, "order": 1, "title": "First", "locked": False},
            {"id": 20, "order": 2, "title": "Second", "locked": False},
        ]})
    if path == "/api/reader/get":
        assert request.method == "POST"
        assert json.loads(request.content) == {
            "translate": "ai", "language": "en", "raw_id": 113338,
            "chapter_no": 1, "retry": False, "force_retry": False,
        }
        return httpx.Response(200, json={"success": True, "data": {
            "content_url": "https://wtr-lab.com/api/v2/reader/content?token=signed-value",
        }})
    if path == "/api/v2/reader/content" and request.url.params.get("token") == "signed-value":
        return httpx.Response(200, json={"data": {"data": {
            "body": ["The »14⛬ appeared.", "<p>Second paragraph.</p>"],
            "glossary_data": {"terms": {"14": ["translated term", {}]}},
        }}})
    if path == "/en/novel-list":
        return httpx.Response(200, text=next_page("series", [RECORD]))
    if path in {"/en/ranking/all_time", "/en/trending"}:
        return httpx.Response(200, text=next_page("list", [RECORD]))
    if path == "/api/v2/img" and request.url.params.get("src") == "series/test":
        return httpx.Response(307, headers={"Location": "https://img.wtr-lab.com/signed-cover"})
    if request.url.host == "img.wtr-lab.com" and path == "/signed-cover":
        return httpx.Response(200, content=b"\xff\xd8\xfffixture")
    return httpx.Response(404)


def source(responder=routes, **kwargs):
    adapter = WtrLabSource(
        client=httpx.Client(transport=httpx.MockTransport(responder), follow_redirects=False),
        access_authorized=True,
        **kwargs,
    )
    adapter.min_interval = 0
    return adapter


def test_requires_explicit_authorization_and_configures_shared_rate_limit():
    with pytest.raises(SourceError, match="authorization"):
        WtrLabSource()
    adapter = WtrLabSource(access_authorized=True)
    assert adapter.id == "wtr-lab"
    assert adapter.name == "WTR-LAB"
    assert adapter.min_interval == 2


def test_search_recent_details_and_numeric_stable_ids():
    adapter = source()
    found = adapter.search("test novel", "Fantasy")
    assert [book["id"] for book in found] == [BOOK_ID]
    assert found[0]["genre"] == "Action, Fantasy, Xuanhuan"
    assert found[0]["coverUrl"] == "https://wtr-lab.com/api/v2/img?src=series/test"
    assert adapter.search("ab") == []
    assert adapter.recent()[0]["title"] == "Test Novel"

    direct = source()
    details = direct.details(BOOK_ID)
    assert details["author"] == "Demo Writer"
    assert details["description"] == "An original\ntest\nstory."
    assert details["chapterCount"] == 3
    with pytest.raises(SourceError, match="identifier"):
        direct.details("not-numeric")


@pytest.mark.parametrize(("query", "book_id"), [
    ("113338", "113338"),
    ("  #113338  ", "113338"),
    ("id:113338", "113338"),
    ("ID: 113338", "113338"),
    ("000113338", "113338"),
    ("2", "2"),
])
def test_search_resolves_exact_wtrlab_novel_ids_including_short_ids(query, book_id):
    requested = []
    record = json.loads(json.dumps(RECORD))
    record["raw_id"] = int(book_id)

    def responder(request):
        requested.append((request.method, request.url.path))
        if request.url.path == "/api/novel/get-by-raw-id":
            assert json.loads(request.content) == {"raw_id": int(book_id)}
            return httpx.Response(200, json={"success": True, "novel": {
                "id": 100, "raw_id": int(book_id), "slug": SLUG,
            }})
        if request.url.path == f"/en/novel/{book_id}/{SLUG}":
            return httpx.Response(200, text=next_page("serie", {"serie_data": record}))
        pytest.fail(f"Unexpected request: {request.method} {request.url}")

    adapter = source(responder)
    found = adapter.search(query)
    assert [book["id"] for book in found] == [book_id]
    assert found[0]["title"] == "Test Novel"
    assert found[0]["author"] == "Demo Writer"
    assert requested == [
        ("POST", "/api/novel/get-by-raw-id"),
        ("GET", f"/en/novel/{book_id}/{SLUG}"),
    ]
    assert adapter.search(query, "Fantasy")[0]["id"] == book_id
    assert adapter.search(query, "Romance") == []


def test_unknown_novel_id_returns_no_results_but_upstream_errors_still_surface():
    def missing(request):
        assert request.url.path == "/api/novel/get-by-raw-id"
        return httpx.Response(200, json={"success": False, "error": "Novel not found"})

    assert source(missing).search("999999") == []
    assert source(missing).search("0") == []

    with pytest.raises(SourceError, match="HTTP 503"):
        source(lambda _request: httpx.Response(503)).search("113338")

    def mismatched(_request):
        return httpx.Response(200, json={"success": True, "novel": {"raw_id": 999, "slug": SLUG}})

    with pytest.raises(SourceError, match="mismatched"):
        source(mismatched).search("113338")


@pytest.mark.parametrize(("title", "expected"), [
    ("%{Soul Land|RG91bHVvIERhbHU}: I Adopted Qian Renxue", "Soul Land: I Adopted Qian Renxue"),
    ("%{Soul Land|RG91bHVvIERhbHU} and %{Soul Land|RG91bHVv}", "Soul Land and Soul Land"),
    ("Chapter 1: %{千仞雪|UWlhbiBSZW54dWU=}", "Chapter 1: 千仞雪"),
    ("%{Visible Name|not valid base64!}", "Visible Name"),
    ("A normal title: 100% {complete}", "A normal title: 100% {complete}"),
    ("Unfinished %{Soul Land|RG91bHVv", "Unfinished %{Soul Land|RG91bHVv"),
    ("%{ |metadata}", "%{ |metadata}"),
])
def test_wtrlab_title_annotations_keep_the_visible_name(title, expected):
    assert display_title("wtr-lab", title) == expected
    assert display_title("novelping", title) == title


def test_annotated_titles_are_clean_in_all_live_metadata_paths(monkeypatch):
    title = "%{Soul Land|RG91bHVvIERhbHU}: I Adopted Qian Renxue"
    monkeypatch.setitem(RECORD["data"], "title", title)

    def responder(request):
        response = routes(request)
        if request.url.path == f"/api/chapters/{BOOK_ID}":
            payload = response.json()
            payload["chapters"][1]["title"] = "Chapter 1: %{Soul Land|RG91bHVvIERhbHU}"
            return httpx.Response(200, json=payload)
        return response

    adapter = source(responder)
    expected = "Soul Land: I Adopted Qian Renxue"
    assert adapter.search("test novel")[0]["title"] == expected
    assert adapter.recent()[0]["title"] == expected
    assert adapter.details(BOOK_ID)["title"] == expected
    assert adapter.chapters(BOOK_ID)[0]["title"] == "Chapter 1: Soul Land"
    adapter.refresh_home_feed()
    assert all(items[0]["title"] == expected for items in adapter.home_feed().values())


def test_existing_wtrlab_archives_display_clean_titles_without_the_adapter(tmp_path):
    title = "%{Soul Land|RG91bHVvIERhbHU}: I Adopted Qian Renxue"
    store = ArchiveStore(tmp_path)
    for provider in ("wtr-lab", "novelping"):
        store.save_book(provider, {"id": BOOK_ID, "title": title})
        store.save_chapters(provider, BOOK_ID, [{"id": "chapter-1", "title": title, "ordinal": 0}])
        store.save_text(provider, BOOK_ID, "chapter-1", "Original stored chapter text.")

    with TestClient(make_app([], "local-token", tmp_path)) as api:
        headers = {"Authorization": "Bearer local-token"}
        base = f"/v1/books/wtr-lab/{BOOK_ID}"
        assert api.get(base, headers=headers).json()["title"] == "Soul Land: I Adopted Qian Renxue"
        chapter = api.get(base + "/chapters", headers=headers).json()["items"][0]
        assert chapter["title"] == "Soul Land: I Adopted Qian Renxue"
        assert chapter["id"] == "chapter-1"
        assert api.get(base + "/chapters/chapter-1/text", headers=headers).text == "Original stored chapter text."
        results = api.get("/v1/search", params={"source": "wtr-lab", "q": "Soul Land"}, headers=headers).json()["items"]
        assert results[0]["title"] == "Soul Land: I Adopted Qian Renxue"
        assert api.get(f"/v1/books/novelping/{BOOK_ID}", headers=headers).json()["title"] == title


def test_chapters_text_glossary_and_cover():
    adapter = source()
    chapters = adapter.chapters(BOOK_ID)
    assert [chapter["id"] for chapter in chapters] == ["chapter-1", "chapter-2", "chapter-3"]
    assert [chapter["ordinal"] for chapter in chapters] == [0, 1, 2]
    assert [chapter["downloadable"] for chapter in chapters] == [True, True, False]
    assert adapter.text(BOOK_ID, "chapter-1") == "The translated term appeared.\n\nSecond paragraph."
    assert adapter.cover_image(RECORD["data"]["image"]) == ("image/jpeg", b"\xff\xd8\xfffixture")


def test_wtrlab_filters_its_own_notices_after_glossary_hydration():
    def respond(request):
        response = routes(request)
        if request.url.path == "/api/v2/reader/content":
            payload = response.json()
            payload["data"]["data"]["body"].insert(0, "This chapter is hosted on WTR-LAB.")
            return httpx.Response(200, json=payload)
        return response

    assert source(respond).text(BOOK_ID, "chapter-1") == "The translated term appeared.\n\nSecond paragraph."


def test_wtrlab_html_body_does_not_include_invisible_text():
    def respond(request):
        response = routes(request)
        if request.url.path == "/api/v2/reader/content":
            payload = response.json()
            payload["data"]["data"]["body"].append('<p>Visible paragraph.<span style="display:none">An unknown hidden notice.</span></p>')
            return httpx.Response(200, json=payload)
        return response

    assert source(respond).text(BOOK_ID, "chapter-1") == "The translated term appeared.\n\nSecond paragraph.\n\nVisible paragraph."


def test_encrypted_glossary_fallback():
    nonce = bytes(range(12))
    ciphertext = AESGCM(GLOSSARY_KEY).encrypt(nonce, "decrypted term".encode(), None)
    entries = {"21": ["", {
        "iv": base64.b64encode(nonce).decode(),
        "ct": base64.b64encode(ciphertext).decode(),
    }]}
    assert WtrLabSource._decode_glossary(entries) == {"21": "decrypted term"}
    entries["21"][1]["ct"] = base64.b64encode(ciphertext[:-1] + b"x").decode()
    assert WtrLabSource._decode_glossary(entries) == {}


@pytest.mark.parametrize("terminator", ["⛬", "〓"])
def test_indexed_v3_glossary_restores_the_reported_novels_names(terminator):
    # Current Game Arrival chapter 1 uses a zero-based array and ※13 for Lin Feng.
    terms = [["Term " + str(index), "原文"] for index in range(16)]
    terms[0] = ["Canglan Third Vocational College", "沧澜第三职业学院"]
    terms[13] = ["Lin Feng", "林峰"]
    terms[14] = ["Li Ao", "李傲"]

    def respond(request):
        response = routes(request)
        if request.url.path == "/api/v2/reader/content":
            payload = response.json()
            payload["data"]["data"] = {
                "body": [f'"※13{terminator}! Stand up!"', f"※14{terminator} waited at ※0{terminator}."],
                "glossary_data": {"version": 3, "terms": terms},
            }
            return httpx.Response(200, json=payload)
        return response

    assert source(respond).text(BOOK_ID, "chapter-1") == (
        '"Lin Feng! Stand up!"\n\nLi Ao waited at Canglan Third Vocational College.'
    )


def test_indexed_glossary_handles_outer_metadata_and_raw_fallback():
    def respond(request):
        response = routes(request)
        if request.url.path == "/api/v2/reader/content":
            return httpx.Response(200, json={"data": {
                "data": {"body": ["※0⛬ met ※1⛬."]},
                "glossary_data": {"version": 3, "terms": [["Lin Feng", "林峰"], ["", "_李傲_"]]},
            }})
        return response

    assert source(respond).text(BOOK_ID, "chapter-1") == "Lin Feng met 李傲."


@pytest.mark.parametrize("marker", ["※99⛬", "»missing⛬"])
def test_missing_glossary_terms_fail_instead_of_returning_corrupt_names(marker):
    def respond(request):
        response = routes(request)
        if request.url.path == "/api/v2/reader/content":
            payload = response.json()
            payload["data"]["data"]["body"] = [f"The hero {marker} returned."]
            return httpx.Response(200, json=payload)
        return response

    with pytest.raises(SourceError, match="glossary is incomplete"):
        source(respond).text(BOOK_ID, "chapter-1")


@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize("http_status", [200, 401])
def test_account_requirement_is_actionable_and_stops_before_signed_content(authenticated, http_status):
    requested = []

    def respond(request):
        if request.url.path != "/api/reader/get":
            return routes(request)
        requested.append(request.url.path)
        assert json.loads(request.content)["chapter_no"] == 11
        return httpx.Response(http_status, json={"success": False, "error": "You are not logged in!", "code": 1401})

    adapter = source(respond, session_cookie="better-auth.session_token=private-test-value" if authenticated else "")
    with TestClient(make_app([adapter], "local-token")) as api:
        response = api.get(f"/v1/books/wtr-lab/{BOOK_ID}/chapters/chapter-11/text",
                           headers={"Authorization": "Bearer local-token"})
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert ("expired or was rejected" if authenticated else "first 10") in detail
        assert "QUIETLEAF_WTRLAB_COOKIE" in detail
        assert "private-test-value" not in detail
        assert requested == ["/api/reader/get"]


def test_configured_account_can_read_later_chapter_without_exposing_cookie_to_api_clients():
    secret = "better-auth.session_token=private-test-value"
    reader_requests = []

    def respond(request):
        assert request.headers.get("Cookie") == secret
        if request.url.path == "/api/reader/get":
            assert json.loads(request.content)["chapter_no"] == 11
            reader_requests.append(request.url.path)
            return httpx.Response(200, json={"success": True, "chapter": {"order": 11, "locked": False},
                                             "content_url": "/api/v2/reader/content?token=signed-value"})
        if request.url.path == "/api/v2/reader/content":
            reader_requests.append(request.url.path)
        return routes(request)

    with TestClient(make_app([source(respond, session_cookie=secret)], "local-token")) as api:
        headers = {"Authorization": "Bearer local-token"}
        response = api.get(f"/v1/books/wtr-lab/{BOOK_ID}/chapters/chapter-11/text", headers=headers)
        assert response.status_code == 200
        assert response.text == "The translated term appeared.\n\nSecond paragraph."
        health = api.get("/v1/health", headers=headers)
        assert "private-test-value" not in health.text + response.text
        assert "set-cookie" not in health.headers and "set-cookie" not in response.headers
    assert reader_requests == ["/api/reader/get", "/api/v2/reader/content"]


def test_session_cookie_reaches_reader_but_not_cover_cdn_or_redirects():
    secret = "better-auth.session_token=private-test-value"
    requested = []

    def respond(request):
        requested.append(request.url)
        if request.url.host == "wtr-lab.com":
            assert request.headers.get("Cookie") == secret
        else:
            assert not request.headers.get("Cookie")
        return routes(request)

    adapter = source(respond, session_cookie=secret)
    # Even cookies refreshed by the source with a parent-domain scope must stay off the CDN.
    adapter.client.cookies.set("better-auth.session_token", "jar-secret", domain=".wtr-lab.com", path="/")
    assert "translated term" in adapter.text(BOOK_ID, "chapter-1")
    assert adapter.cover_image(RECORD["data"]["image"])[0] == "image/jpeg"
    assert [url.host for url in requested] == ["wtr-lab.com"] * 3 + ["img.wtr-lab.com"]

    def redirect(_request):
        return httpx.Response(307, headers={"Location": "https://evil.example/capture"})

    adapter = source(redirect, session_cookie=secret)
    adapter.client.follow_redirects = True
    with pytest.raises(SourceError, match="redirect blocked"):
        adapter.text(BOOK_ID, "chapter-1")


@pytest.mark.parametrize("cookie", ["not-a-cookie", "token=secret\r\nOther: header", "token=secret\n", "token=" + "a" * 8192])
def test_invalid_session_cookie_is_rejected_without_echoing_it(cookie):
    with pytest.raises(SourceError, match="^Invalid WTR-LAB session cookie$"):
        source(session_cookie=cookie)


@pytest.mark.parametrize("base_url", ["https://evil.example", "http://wtr-lab.com", "https://wtr-lab.com:444"])
def test_account_cookie_cannot_be_configured_for_another_origin(base_url):
    with pytest.raises(SourceError, match="account sessions require"):
        source(base_url=base_url, session_cookie="token=private-test-value")


def test_locked_chapter_is_not_automatically_unlocked():
    requested = []

    def respond(request):
        requested.append(request.url.path)
        return httpx.Response(200, json={"success": False, "code": "CHAPTER_LOCKED", "cost": 10})

    with pytest.raises(SourceError, match="Unlock it on WTR-LAB"):
        source(respond, session_cookie="token=private-test-value").text(BOOK_ID, "chapter-11")
    assert requested == ["/api/reader/get"]


def test_home_feed_uses_distinct_current_paths_and_last_good_cache():
    requested = []

    def responder(request):
        requested.append(str(request.url))
        return routes(request)

    adapter = source(responder)
    with pytest.raises(SourceError, match="not been refreshed"):
        adapter.home_feed()
    adapter.refresh_home_feed()
    feed = adapter.home_feed()
    assert list(feed) == ["popular", "completed", "fresh", "new", "hot"]
    assert all(section[0]["id"] == BOOK_ID for section in feed.values())
    assert requested == [
        "https://wtr-lab.com/en/ranking/all_time",
        "https://wtr-lab.com/en/novel-list?status=completed",
        "https://wtr-lab.com/en/novel-list?orderBy=update&order=desc",
        "https://wtr-lab.com/en/novel-list?orderBy=date&order=desc",
        "https://wtr-lab.com/en/trending",
    ]
    feed["popular"][0]["title"] = "Changed"
    assert adapter.home_feed()["popular"][0]["title"] == "Test Novel"


def test_reader_rejects_interactive_verification_and_unsafe_content_url():
    def turnstile(request):
        if request.url.path == "/api/reader/get":
            return httpx.Response(200, json={"success": True, "requireTurnstile": True})
        return routes(request)

    with pytest.raises(SourceError, match="interactive verification"):
        source(turnstile).text(BOOK_ID, "chapter-1")

    def off_site(request):
        if request.url.path == "/api/reader/get":
            return httpx.Response(200, json={"success": True, "data": {
                "content_url": "https://evil.example/api/v2/reader/content/value",
            }})
        return routes(request)

    with pytest.raises(SourceError, match="unsafe|off-site"):
        source(off_site).text(BOOK_ID, "chapter-1")


def test_chapter_index_is_bounded_and_must_be_complete():
    too_many = json.loads(json.dumps(RECORD))
    too_many["chapter_count"] = 10_001

    def oversized(request):
        if request.url.path == f"/en/novel/{BOOK_ID}/{SLUG}":
            return httpx.Response(200, text=next_page("serie", {"serie_data": too_many}))
        return routes(request)

    with pytest.raises(SourceError, match="supported limit"):
        source(oversized).chapters(BOOK_ID)

    def incomplete(request):
        if request.url.path == f"/api/chapters/{BOOK_ID}":
            return httpx.Response(200, json={"chapters": [
                {"id": 10, "order": 1, "title": "First", "locked": False},
                {"id": 20, "order": 2, "title": "Second", "locked": False},
            ]})
        return routes(request)

    with pytest.raises(SourceError, match="incomplete"):
        source(incomplete).chapters(BOOK_ID)


def test_only_one_validated_cover_redirect_is_allowed():
    def off_site(request):
        if request.url.path == "/api/v2/img":
            return httpx.Response(307, headers={"Location": "https://evil.example/signed-cover"})
        return routes(request)

    with pytest.raises(SourceError, match="redirect blocked"):
        source(off_site).cover_image(RECORD["data"]["image"])

    def second_redirect(request):
        if request.url.path == "/api/v2/img":
            return httpx.Response(307, headers={"Location": "https://img.wtr-lab.com/first"})
        if request.url.path == "/first":
            return httpx.Response(307, headers={"Location": "https://img.wtr-lab.com/second"})
        return routes(request)

    with pytest.raises(SourceError, match="redirect blocked"):
        source(second_redirect).cover_image(RECORD["data"]["image"])


def test_api_redirects_are_blocked_and_rate_limit_is_shared(monkeypatch):
    def redirected(_request):
        return httpx.Response(307, headers={"Location": "https://wtr-lab.com/api/search-again"})

    with pytest.raises(SourceError, match="redirect blocked"):
        source(redirected).search("test novel")

    adapter = source()
    adapter.min_interval = 2
    adapter._last_request = 9
    clock = [10]
    sleeps = []

    monkeypatch.setattr(wtrlab.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(wtrlab.time, "sleep", sleep)
    adapter.search("test novel")
    adapter.cover_image(RECORD["data"]["image"])
    assert sleeps == [1, 2, 2]


def test_wtrlab_available_through_existing_android_api():
    adapter = source()
    with TestClient(make_app([adapter], "local-token")) as api:
        headers = {"Authorization": "Bearer local-token"}
        health = api.get("/v1/health", headers=headers).json()
        assert health["sources"] == [{"id": "wtr-lab", "name": "WTR-LAB"}]
        result = api.get("/v1/search", params={"q": "test novel"}, headers=headers).json()
        assert result["items"][0]["source"] == "wtr-lab"
        result = api.get("/v1/search", params={"q": BOOK_ID, "source": "wtr-lab"}, headers=headers).json()
        assert result["errors"] == []
        assert result["items"][0]["id"] == BOOK_ID
        assert result["items"][0]["source"] == "wtr-lab"


def test_build_from_env_requires_authorization_flag(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "catalog.json").write_text('{"name":"Test","books":[]}', encoding="utf-8")
    monkeypatch.setenv("QUIETLEAF_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("QUIETLEAF_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("QUIETLEAF_ENABLE_WTRLAB", "1")
    monkeypatch.delenv("QUIETLEAF_WTRLAB_COOKIE", raising=False)
    monkeypatch.delenv("QUIETLEAF_WTRLAB_ACCESS_AUTHORIZED", raising=False)
    with pytest.raises(SourceError, match="authorization"):
        build_from_env()

    monkeypatch.setenv("QUIETLEAF_WTRLAB_ACCESS_AUTHORIZED", "1")
    app = build_from_env()
    assert any(route.path == "/v1/search" for route in app.routes)

    configured = []

    def capture(**kwargs):
        configured.append(kwargs)
        return source()

    monkeypatch.setattr("quietleaf_service.app.WtrLabSource", capture)
    monkeypatch.setenv("QUIETLEAF_WTRLAB_COOKIE", "token=private-test-value")
    build_from_env()
    assert configured[0]["session_cookie"] == "token=private-test-value"
