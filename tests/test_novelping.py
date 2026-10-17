import httpx
import pytest
from fastapi.testclient import TestClient

from quietleaf_service.adapters import SourceError
from quietleaf_service.app import make_app
from quietleaf_service.novelping import NovelPingSource


BOOK = "test-novel"
BOOK_HTML = '''
<h1>Test Novel</h1>
<div class="details"><span>Author:</span><a>Demo Writer</a></div>
<div class="description">An original test story.</div>
<a href="/book/test-novel/chapter-1-hello">READ NOW</a>
<section class="chapter-list">
  <a href="/book/test-novel/chapter-12-goodbye">Chapter 12 Goodbye</a>
  <a href="/book/test-novel/chapter-2-second">Chapter 2 Second</a>
  <a href="/book/test-novel/chapter-1-hello">Chapter 1 Hello</a>
  <a href="/book/test-novel/chapter-2-second">Chapter 2 Second</a>
  <a href="https://evil.example/book/test-novel/chapter-3">Evil</a>
</section>
'''
CHAPTER_ARCHIVE_HTML = '''
<template data-chapter-item-template>
  <a href="/book/test-novel/chapter-12-goodbye">Chapter 12 Goodbye</a>
  <a href="/book/test-novel/chapter-2-second">Chapter 2 Second</a>
  <a href="/book/test-novel/chapter-1-hello">Chapter 1 Hello</a>
  <a href="/book/test-novel/chapter-999-archive-only">Chapter 999 Archive Only</a>
</template>
'''
PREVIEW_HTML = '''
<h1>Test Novel</h1>
<div class="details"><span>Author:</span><a>Demo Writer</a></div>
<a href="/book/test-novel/chapter-1-hello">Chapter 1 Hello</a>
'''
CHAPTER_HTML = '''
<h1>Chapter 1</h1>
<div class="chapter-content"><p>This is the first paragraph of our completely original test chapter and
 it is here to exercise ordinary chapter extraction.</p><p>This is another paragraph.</p>
<script>steal_the_reader()</script></div>
'''


def source(responder):
    provider = NovelPingSource(client=httpx.Client(transport=httpx.MockTransport(responder), follow_redirects=False))
    provider.min_interval = 0
    return provider


def routes(request):
    route = request.url.path
    paths = {
        "/robots.txt": "User-agent: *\nDisallow: /private\n",
        "/": '<h3><a href="/book/test-novel">Test Novel</a></h3>',
        "/search": '<div class="novel-item"><h3><a href="/book/test-novel">Test Novel</a></h3><span class="author">Demo Writer</span></div><a href="/book/something-else">Another Book</a>',
        "/book/test-novel": BOOK_HTML,
        "/book/test-novel/chapter-1-hello": CHAPTER_HTML,
    }
    return httpx.Response(200 if route in paths else 404, text=paths.get(route, ""))


def test_novelping_search_and_details():
    adapter = source(routes)
    assert [b["id"] for b in adapter.search("Test")] == [BOOK]
    assert adapter.search("nonexistent") == []
    assert adapter.details(BOOK)["title"] == "Test Novel"
    assert adapter.details(BOOK)["description"] == "An original test story."
    assert [b["id"] for b in adapter.recent()] == [BOOK]


def test_novelping_home_feed_paths_bounds_deduplication_and_cache():
    requested = []

    def listing(section):
        cards = "".join(
            f'<h3 class="novel-title"><a href="/book/{section}-{number}">{section.title()} {number}</a></h3>'
            for number in range(22)
        )
        return f'<div id="search-results">{cards}{cards[:cards.find("</h3>") + 5]}</div>'

    def featured(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        requested.append(str(request.url))
        sections = {
            ("/sort/popular", None): "popular",
            ("/sort/complete", None): "completed",
            ("/sort/updates", None): "fresh",
            ("/sort/popular", "NEW"): "new",
            ("/sort/hot", None): "hot",
        }
        section = sections.get((request.url.path, request.url.params.get("sort")))
        return httpx.Response(200 if section else 404, text=listing(section) if section else "")

    adapter = source(featured)
    first = adapter.home_feed()
    assert requested == [
        "https://novelping.com/sort/popular",
        "https://novelping.com/sort/complete",
        "https://novelping.com/sort/updates",
        "https://novelping.com/sort/popular?sort=NEW",
        "https://novelping.com/sort/hot",
    ]
    assert list(first) == ["popular", "completed", "fresh", "new", "hot"]
    for category, books in first.items():
        assert len(books) == 20
        assert len({book["id"] for book in books}) == 20
        assert all(book["id"].startswith(f"{category}-") for book in books)

    first["popular"][0]["title"] = "Changed by caller"
    second = adapter.home_feed()
    assert len(requested) == 5
    assert second["popular"][0]["title"] == "Popular 0"
    with TestClient(make_app([adapter], "local-token")) as api:
        result = api.get("/v1/home", headers={"Authorization": "Bearer local-token"}).json()
    assert len(requested) == 5
    assert all(book["source"] == "novelping" for category in adapter.HOME_PATHS for book in result[category])


def test_novelping_chapters_order_and_duplicate_rejection():
    adapter = source(routes)
    chapters = adapter.chapters(BOOK)
    assert [c["id"] for c in chapters] == ["chapter-1-hello", "chapter-2-second", "chapter-12-goodbye"]
    assert [c["ordinal"] for c in chapters] == [0, 1, 2]
    assert chapters[0]["title"] == "Chapter 1 Hello"
    assert all(ch["downloadable"] for ch in chapters)


def test_novelping_text_removes_scripts_and_preserves_paragraphs():
    adapter = source(routes)
    text = adapter.text(BOOK, "chapter-1-hello")
    assert "This is the first paragraph" in text
    assert "\n\n" in text
    assert "steal_the_reader" not in text


def test_novelping_rejects_untrusted_host_and_invalid_chapters():
    adapter = source(routes)
    with pytest.raises(SourceError):
        adapter._guard_url("https://evil.example/book/test-novel")
    with pytest.raises(SourceError):
        adapter.text(BOOK, "../../../private")
    with pytest.raises(SourceError):
        adapter.text(BOOK, "other-format")


def test_novelping_obeys_robots_and_never_bypasses_redirects():
    def restricted(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /search\n")
        return httpx.Response(200, text="<h1>Unreachable</h1>")
    adapter = source(restricted)
    with pytest.raises(SourceError, match="robots.txt"):
        adapter.search("anything")

    def redirect(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(302, headers={"Location": "https://elsewhere.example"})
    adapter = source(redirect)
    with pytest.raises(SourceError, match="Redirect blocked"):
        adapter.recent()


def test_authorized_mode_is_explicit_and_uses_the_full_chapter_archive():
    requested = []

    def restricted(request):
        requested.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /search\nDisallow: /book/\n")
        if request.url.path == "/search":
            return httpx.Response(200, text='<a href="/book/test-novel">Test Novel</a>')
        if request.url.path == "/book/test-novel":
            return httpx.Response(200, text=PREVIEW_HTML)
        if request.url.path == "/book/test-novel/chapter-1-hello":
            return httpx.Response(200, text=CHAPTER_HTML)
        if request.url.path == "/ajax/chapter-archive" and request.url.params.get("novelId") == BOOK:
            return httpx.Response(200, text=CHAPTER_ARCHIVE_HTML)
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(restricted), follow_redirects=False)
    blocked = NovelPingSource(client=client)
    blocked.min_interval = 0
    with pytest.raises(SourceError, match="robots.txt"):
        blocked.details(BOOK)

    authorized = NovelPingSource(client=client, access_authorized=True)
    authorized.min_interval = 0
    assert authorized.search("Test")[0]["id"] == BOOK
    assert authorized.details(BOOK)["title"] == "Test Novel"
    chapters = authorized.chapters(BOOK)
    assert len(chapters) == 4
    assert chapters[-1]["id"] == "chapter-999-archive-only"
    assert "first paragraph" in authorized.text(BOOK, "chapter-1-hello")
    assert "https://novelping.com/ajax/chapter-archive?novelId=test-novel" in requested


def test_authorized_mode_rejects_an_unavailable_full_chapter_archive():
    def unavailable(request):
        return httpx.Response(503)

    adapter = NovelPingSource(
        client=httpx.Client(transport=httpx.MockTransport(unavailable), follow_redirects=False),
        access_authorized=True,
    )
    adapter.min_interval = 0
    with pytest.raises(SourceError, match="HTTP 503"):
        adapter.chapters(BOOK)


def test_authorized_mode_rejects_oversized_pages():
    def oversized(_request):
        return httpx.Response(200, text="x" * (2 * 1024 * 1024 + 1))

    adapter = NovelPingSource(
        client=httpx.Client(transport=httpx.MockTransport(oversized), follow_redirects=False),
        access_authorized=True,
    )
    adapter.min_interval = 0
    with pytest.raises(SourceError, match="too large"):
        adapter.details(BOOK)


def test_novelping_available_through_existing_android_api():
    with TestClient(make_app([source(routes)], "local-token")) as api:
        headers = {"Authorization": "Bearer local-token"}
        assert api.get("/v1/health", headers=headers).json()["sources"][0]["id"] == "novelping"
        search = api.get("/v1/search", params={"q": "Test"}, headers=headers)
        assert search.status_code == 200
        assert search.json()["items"][0]["source"] == "novelping"
        chapters = api.get(f"/v1/books/novelping/{BOOK}/chapters", headers=headers)
        assert len(chapters.json()["items"]) == 3
        chapter = api.get(f"/v1/books/novelping/{BOOK}/chapters/chapter-1-hello/text", headers=headers)
        assert chapter.status_code == 200
        assert "first paragraph" in chapter.text


def test_novelping_extracts_cover_urls_and_rejects_offsite_images():
    def covers(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/search":
            return httpx.Response(200, text='''<div class="novel-item">
                <a href="/book/test-novel"><img data-src="/covers/test.jpg" alt="Test Novel">Test Novel</a>
                </div>''')
        if request.url.path == "/book/test-novel":
            return httpx.Response(200, text='''<meta property="og:image" content="/covers/test.jpg">
                <h1>Test Novel</h1>''')
        return httpx.Response(404)

    adapter = source(covers)
    assert adapter.search("Test")[0]["coverUrl"] == "https://novelping.com/covers/test.jpg"
    assert adapter.details("test-novel")["coverUrl"] == "https://novelping.com/covers/test.jpg"
    assert adapter._cover_url(adapter._page("/book/test-novel"))
    with pytest.raises(SourceError, match="Untrusted cover"):
        adapter.image_url("https://evil.example/cover.jpg")
    assert adapter._cover_url(__import__("bs4").BeautifulSoup(
        '<meta property="og:image" content="javascript:alert(1)"><h1>Unsafe</h1>', "html.parser")) == ""


def test_novelping_extracts_live_card_and_detail_metadata():
    def metadata(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/search":
            return httpx.Response(200, text='''<div id="search-results">
                <div class="row">
                    <img class="cover novel-cover-loading"
                         src="https://images.novelping.com/novel_200_89/test-novel.jpg">
                    <h3 class="novel-title"><a href="/book/test-novel">Test Novel</a></h3>
                    <span class="author">Demo Writer</span>
                </div>
            </div>''')
        if request.url.path == "/book/test-novel":
            return httpx.Response(200, text='''
                <meta property="og:image" content="https://images.novelping.com/novel/test-novel.jpg">
                <meta property="og:novel:author" content="Demo Writer">
                <meta property="og:novel:genre" content="FANTASY,ACTION">
                <h1>Test Novel</h1>
                <div id="novel-description-content">A complete description.</div>
                <ul class="info info-meta"><li><h3>Author:</h3><a href="/author/Demo Writer">Demo Writer</a></li></ul>
            ''')
        return httpx.Response(404)

    adapter = source(metadata)
    found = adapter.search("Test")[0]
    assert found["author"] == "Demo Writer"
    assert found["coverUrl"] == "https://images.novelping.com/novel_200_89/test-novel.jpg"
    details = adapter.details(BOOK)
    assert details["author"] == "Demo Writer"
    assert details["genre"] == "Fantasy, Action"
    assert details["description"] == "A complete description."
    assert details["coverUrl"] == "https://images.novelping.com/novel/test-novel.jpg"


def test_cover_is_archived_and_served_after_source_removed(tmp_path):
    from time import monotonic, sleep

    artwork = b"\xff\xd8\xff" + b"quietleaf-fake-jpeg"

    def site(request):
        url = request.url.path
        if url == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if url == "/book/test-novel":
            return httpx.Response(200, text='''<meta property="og:image" content="/cover.jpg">
                <h1>Test Novel</h1>
                <a href="/book/test-novel/chapter-1-hello">Chapter 1</a>''')
        if url == "/cover.jpg":
            return httpx.Response(200, content=artwork, headers={"Content-Type": "image/jpeg"})
        if url == "/book/test-novel/chapter-1-hello":
            return httpx.Response(200, text=CHAPTER_HTML)
        return httpx.Response(404)

    adapter = source(site)
    with TestClient(make_app([adapter], "local-token", tmp_path)) as api:
        headers = {"Authorization": "Bearer local-token"}
        job = api.post("/v1/archive/novelping/test-novel", headers=headers)
        assert job.status_code == 202
        deadline = monotonic() + 3
        while monotonic() < deadline:
            state = api.get(f"/v1/archive/jobs/{job.json()['id']}", headers=headers).json()["status"]
            if state in {"completed", "failed"}:
                break
            sleep(0.02)
        assert state == "completed"
        assert api.get("/v1/books/novelping/test-novel").status_code == 401
        detail = api.get("/v1/books/novelping/test-novel", headers=headers).json()
        assert detail["coverArchived"] is True
        cover = api.get("/v1/books/novelping/test-novel/cover", headers=headers)
        assert cover.status_code == 200
        assert cover.content == artwork
        assert cover.headers["content-type"] == "image/jpeg"

    with TestClient(make_app([], "local-token", tmp_path)) as offline_api:
        cover = offline_api.get("/v1/books/novelping/test-novel/cover", headers=headers)
        assert cover.status_code == 200
        assert cover.content == artwork
        assert offline_api.get("/v1/books/novelping/test-novel", headers=headers).json()["coverArchived"]


def test_cover_cdn_requires_explicit_hostname_and_obeys_robots():
    def site(request):
        if request.url.path == "/robots.txt":
            if request.url.host == "covers.example":
                return httpx.Response(200, text="User-agent: *\nDisallow: /blocked.jpg\n")
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"test-image")

    adapter = NovelPingSource(client=httpx.Client(transport=httpx.MockTransport(site)),
                              image_hosts=("covers.example",))
    adapter.min_interval = 0
    mime, data = adapter.cover_image("https://covers.example/ok.png")
    assert mime == "image/png"
    assert data.startswith(b"\x89PNG")
    with pytest.raises(SourceError, match="robots.txt"):
        adapter.cover_image("https://covers.example/blocked.jpg")
    with pytest.raises(SourceError, match="Untrusted cover"):
        adapter.cover_image("https://untrusted.example/cover.jpg")
