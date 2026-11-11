import json
from urllib.parse import parse_qs, urlsplit

import pytest

from quietleaf_service.adapters import SourceError
from quietleaf_service.archive import ArchiveStore, ArchiveWorker
from quietleaf_service.browser_archive_probe import BOOK_ID, BrowserArchiveSource


class Transport:
    def __init__(self, fail_at=None, html_challenge=False):
        self.fail_at = fail_at
        self.html_challenge = html_challenge
        self.chapters_requested = []
        self.closed = False

    def request(self, method, target, body, max_bytes, accept):
        parsed = urlsplit(target)
        if parsed.path.startswith("/en/novel/"):
            data = {"props": {"pageProps": {"serie": {"serie_data": {
                "raw_id": int(BOOK_ID), "chapter_count": 45,
                "data": {"title": "Archive browser fixture", "author": "Test Writer"},
            }}}}}
            return {"status": 200, "text": '<script id="__NEXT_DATA__">' + json.dumps(data) + "</script>"}
        if parsed.path == f"/api/chapters/{BOOK_ID}":
            return {"status": 200, "text": json.dumps({"chapters": [
                {"order": n, "title": f"Chapter {n}", "locked": False} for n in range(1, 46)
            ]})}
        if parsed.path == "/api/reader/get":
            assert method == "POST"
            number = body["chapter_no"]
            self.chapters_requested.append(number)
            if number == self.fail_at:
                if self.html_challenge:
                    return {"status": 403, "challenge": True, "text": "<title>Just a moment...</title>"}
                return {"status": 200, "text": json.dumps({"success": False, "requireTurnstile": True})}
            return {"status": 200, "text": json.dumps({"success": True,
                "content_url": f"/api/v2/reader/content?chapter={number}"})}
        if parsed.path == "/api/v2/reader/content":
            number = int(parse_qs(parsed.query)["chapter"][0])
            return {"status": 200, "text": json.dumps({"data": {"data": {
                "body": [f"※0⛬ crossed the village in original fixture chapter {number}."],
                "glossary_data": {"version": 3, "terms": [["Lin Feng", "林峰"]]},
            }}})}
        pytest.fail("Unexpected browser request")

    def close(self):
        self.closed = True


def process(store, source):
    claim = store.next_job()
    assert claim is not None
    ArchiveWorker(store, [source], concurrency=1).process(claim)
    return store.job(claim["id"])


def test_real_archive_worker_saves_forty_consecutive_browser_chapters(tmp_path):
    transport = Transport()
    source = BrowserArchiveSource("__Secure-better-auth.session_token=test-session", 40, transport=transport)
    store = ArchiveStore(tmp_path)
    store.enqueue(source.id, BOOK_ID)
    result = process(store, source)
    assert result["status"] == "completed"
    assert result["downloaded"] == result["total"] == 40
    assert transport.chapters_requested == list(range(1, 41))
    assert store.stats(source.id, BOOK_ID)["downloadedChapters"] == 40
    for number in range(1, 41):
        assert store.text(source.id, BOOK_ID, f"chapter-{number}") == (
            f"Lin Feng crossed the village in original fixture chapter {number}."
        )
    source.close()
    assert transport.closed


@pytest.mark.parametrize("html_challenge", [False, True])
def test_challenge_at_twenty_six_stops_and_retry_retains_checkpoint(tmp_path, html_challenge):
    transport = Transport(fail_at=26, html_challenge=html_challenge)
    source = BrowserArchiveSource("__Secure-better-auth.session_token=test-session", 40, transport=transport)
    store = ArchiveStore(tmp_path)
    queued = store.enqueue(source.id, BOOK_ID)
    result = process(store, source)
    assert result["status"] == "failed"
    assert result["downloaded"] == 25
    assert "verification" in result["error"]
    assert transport.chapters_requested == list(range(1, 27))
    assert source.last_attempted_chapter == "chapter-26"
    assert store.text(source.id, BOOK_ID, "chapter-26") is None
    assert store.stats(source.id, BOOK_ID)["downloadedChapters"] == 25

    # Restart the store to exercise its durable leases/checkpoints before explicit retry.
    restarted = ArchiveStore(tmp_path)
    restarted.control(queued["id"], "retry")
    transport.fail_at = None
    result = process(restarted, source)
    assert result["status"] == "completed" and result["downloaded"] == 40
    assert transport.chapters_requested == list(range(1, 27)) + list(range(26, 41))
    source.close()


def test_browser_transport_cannot_be_redirected_to_another_host():
    source = BrowserArchiveSource("__Secure-better-auth.session_token=test-session", 40, transport=Transport())
    with pytest.raises(SourceError, match="unsafe|off-site"):
        source._request("GET", "https://evil.example/capture")
    source.close()
