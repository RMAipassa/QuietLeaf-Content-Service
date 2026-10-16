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


def test_authorized_browser_mode_is_explicit_and_limited_to_book_pages():
    def restricted(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /search\nDisallow: /book/\n")
        if request.url.path == "/search":
            return httpx.Response(200, text='<a href="/book/test-novel">Test Novel</a>')
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(restricted), follow_redirects=False)
    blocked = NovelPingSource(client=client)
    blocked.min_interval = 0
    with pytest.raises(SourceError, match="robots.txt"):
        blocked.details(BOOK)

    rendered = []

    def render(url):
        rendered.append(url)
        return CHAPTER_HTML if url.endswith("chapter-1-hello") else BOOK_HTML

    authorized = NovelPingSource(client=client, access_authorized=True, renderer=render)
    authorized.min_interval = 0
    assert authorized.search("Test")[0]["id"] == BOOK
    assert authorized.details(BOOK)["title"] == "Test Novel"
    assert len(authorized.chapters(BOOK)) == 3
    assert "first paragraph" in authorized.text(BOOK, "chapter-1-hello")
    assert rendered == [
        "https://novelping.com/book/test-novel",
        "https://novelping.com/book/test-novel/chapter-1-hello",
    ]


def test_authorized_browser_mode_rejects_oversized_pages():
    adapter = NovelPingSource(access_authorized=True, renderer=lambda _: "x" * (2 * 1024 * 1024 + 1))
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
