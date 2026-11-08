import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from quietleaf_service.app import make_app


TOKEN = "private-library-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def image_bytes(color="red"):
    output = io.BytesIO()
    Image.new("RGB", (24, 32), color).save(output, "PNG")
    return output.getvalue()


def zip_bytes(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, body in entries:
            archive.writestr(name, body)
    return output.getvalue()


def epub(images=False):
    container = '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>'
    package = '''<package xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata><dc:title>Owned Story</dc:title><dc:creator>A. Writer</dc:creator></metadata><manifest>
      <item id="second" href="second.xhtml" media-type="application/xhtml+xml"/>
      <item id="first" href="first.xhtml" media-type="application/xhtml+xml"/>
      </manifest><spine><itemref idref="first"/><itemref idref="second"/></spine></package>'''
    first = '<html><body><img src="images/10.png"/></body></html>' if images else '<html><body><h1>Opening</h1><p>' + "Original chapter one. " * 100 + '</p></body></html>'
    second = '<html><body><img src="images/2.png"/></body></html>' if images else '<html><body><h1>Ending</h1><p>Original chapter two.</p></body></html>'
    return zip_bytes([("META-INF/container.xml", container), ("OPS/book.opf", package),
                      ("OPS/first.xhtml", first), ("OPS/second.xhtml", second),
                      ("OPS/images/10.png", image_bytes()), ("OPS/images/2.png", image_bytes("blue"))])


def upload(api, name, body, **params):
    return api.post("/v1/private/books", params={"filename": name, **params}, content=body,
                    headers={**AUTH, "Content-Type": "application/octet-stream"})


def test_authenticated_text_upload_discovery_original_and_restart(tmp_path):
    body = b"An original purchased book fixture.\n\nSecond paragraph."
    with TestClient(make_app([], TOKEN, tmp_path)) as api:
        assert api.get("/v1/private/books").status_code == 401
        assert api.post("/v1/private/books?filename=test.txt", content=body).status_code == 401
        response = upload(api, "My book.txt", body, title="My private book", author="Writer")
        assert response.status_code == 201, response.text
        book = response.json()["book"]
        book_id = book["id"]
        assert book["source"] == "private"
        assert book["mediaType"] == "text"
        assert book["title"] == "My private book"
        assert api.get("/v1/health", headers=AUTH).json()["sources"] == [{"id": "private", "name": "Private library"}]
        assert api.get("/v1/search", params={"source": "private", "q": "private"}, headers=AUTH).json()["items"][0]["id"] == book_id
        chapters = api.get(f"/v1/books/private/{book_id}/chapters", headers=AUTH).json()["items"]
        text = api.get(f"/v1/books/private/{book_id}/chapters/{chapters[0]['id']}/text", headers=AUTH)
        assert text.content == body
        original = api.get(f"/v1/private/books/{book_id}/file", headers=AUTH)
        assert original.content == body
        assert "no-store" in original.headers["cache-control"]
        assert api.get(f"/v1/private/books/{book_id}/file").status_code == 401
        assert api.post(f"/v1/archive/private/{book_id}", headers=AUTH).status_code == 409
        assert upload(api, "another-name.txt", body).json()["duplicate"] is True
        assert api.get("/v1/private/books", headers=AUTH).json()["total"] == 1
    with TestClient(make_app([], TOKEN, tmp_path)) as reopened:
        assert reopened.get(f"/v1/books/private/{book_id}", headers=AUTH).json()["title"] == "My private book"
        assert reopened.delete(f"/v1/private/books/{book_id}", headers=AUTH).json()["deleted"] is True
        assert reopened.get("/v1/private/books", headers=AUTH).json()["total"] == 0


def test_epub_metadata_spine_order_and_image_epub_pages(tmp_path):
    with TestClient(make_app([], TOKEN, tmp_path)) as api:
        response = upload(api, "story.epub", epub())
        assert response.status_code == 201, response.text
        book = response.json()["book"]
        assert (book["title"], book["author"], book["mediaType"]) == ("Owned Story", "A. Writer", "text")
        chapters = api.get(f"/v1/books/private/{book['id']}/chapters", headers=AUTH).json()["items"]
        assert [chapter["title"] for chapter in chapters] == ["Opening", "Ending"]
        response = upload(api, "manga.epub", epub(images=True))
        assert response.status_code == 201, response.text
        manga = response.json()["book"]
        assert manga["mediaType"] == "epub-images"
        assert manga["pageCount"] == 2
        page = api.get(f"/v1/private/books/{manga['id']}/pages/0", headers=AUTH)
        assert page.headers["content-type"] == "image/jpeg"
        assert Image.open(io.BytesIO(page.content)).getpixel((5, 5))[0] > 200
        offline = api.get(f"/v1/private/books/{manga['id']}/download", headers=AUTH)
        with zipfile.ZipFile(io.BytesIO(offline.content)) as ordered:
            assert ordered.namelist() == ["000001.png", "000002.png"]
            assert Image.open(io.BytesIO(ordered.read("000001.png"))).getpixel((5, 5))[0] > 200


def test_cbz_natural_page_order_cover_and_original_ranges(tmp_path):
    body = zip_bytes([("10.png", image_bytes("blue")), ("2.png", image_bytes("red"))])
    with TestClient(make_app([], TOKEN, tmp_path)) as api:
        response = upload(api, "manga.cbz", body)
        assert response.status_code == 201, response.text
        book = response.json()["book"]
        assert book["mediaType"] == "cbz" and book["pageCount"] == 2
        page = api.get(f"/v1/private/books/{book['id']}/pages/0", headers=AUTH)
        assert Image.open(io.BytesIO(page.content)).getpixel((5, 5))[0] > 200
        assert api.get(f"/v1/books/private/{book['id']}/cover", headers=AUTH).status_code == 200
        ranged = api.get(f"/v1/private/books/{book['id']}/file", headers={**AUTH, "Range": "bytes=0-3"})
        assert ranged.status_code == 206 and ranged.content == body[:4]
        assert api.get(f"/v1/private/books/{book['id']}/pages/2", headers=AUTH).status_code == 422


def test_pdf_upload_and_page_rendering(tmp_path):
    output = io.BytesIO()
    Image.new("RGB", (30, 40), "red").save(output, "PDF", save_all=True, append_images=[Image.new("RGB", (30, 40), "blue")])
    with TestClient(make_app([], TOKEN, tmp_path)) as api:
        response = upload(api, "volume.pdf", output.getvalue())
        assert response.status_code == 201, response.text
        book = response.json()["book"]
        assert book["mediaType"] == "pdf" and book["pageCount"] == 2
        assert api.get(f"/v1/private/books/{book['id']}/pages/1", headers=AUTH).status_code == 200


def test_image_epub_accepts_svg_wrappers_and_direct_image_spine_items(tmp_path):
    body = zip_bytes([
        ("META-INF/container.xml", '<container><rootfile full-path="OPS/book.opf"/></container>'),
        ("OPS/book.opf", '<package><manifest><item id="svg" href="pages/first.svg" media-type="image/svg+xml"/><item id="image" href="images/10.png" media-type="image/png"/></manifest><spine><itemref idref="svg"/><itemref idref="image"/></spine></package>'),
        ("OPS/pages/first.svg", '<svg xmlns:xlink="http://www.w3.org/1999/xlink"><image xlink:href="../images/2.png"/></svg>'),
        ("OPS/images/2.png", image_bytes("red")), ("OPS/images/10.png", image_bytes("blue")),
    ])
    with TestClient(make_app([], TOKEN, tmp_path)) as api:
        response = upload(api, "fixed-layout.epub", body)
        assert response.status_code == 201, response.text
        book = response.json()["book"]
        assert book["mediaType"] == "epub-images" and book["pageCount"] == 2
        rendered = api.get(f"/v1/private/books/{book['id']}/pages/0", headers=AUTH)
        assert Image.open(io.BytesIO(rendered.content)).getpixel((5, 5))[0] > 200


def test_upload_validation_quota_and_disabled_unauthenticated_library(tmp_path):
    with TestClient(make_app([], TOKEN, tmp_path, private_max_bytes=100, private_max_upload_bytes=80)) as api:
        assert upload(api, "file.txt", b"x" * 81).status_code == 413
        assert upload(api, "file.txt", b"").status_code == 422
        assert upload(api, "file.cbr", b"rar").status_code == 422
        assert upload(api, "first.txt", b"a" * 30).status_code == 201
        assert upload(api, "second.txt", b"b" * 30).status_code == 422
    with TestClient(make_app([], archive_dir=tmp_path)) as api:
        assert api.get("/v1/health").json()["privateLibraryEnabled"] is False
        assert api.get("/v1/private/books").status_code == 503


@pytest.mark.parametrize("body", [b"not a zip", zip_bytes([("../escape.png", image_bytes())]), zip_bytes([("page.png", b"not an image")])])
def test_rejects_invalid_comics_without_partial_library_entries(tmp_path, body):
    with TestClient(make_app([], TOKEN, tmp_path)) as api:
        assert upload(api, "bad.cbz", body).status_code == 422
        assert api.get("/v1/private/books", headers=AUTH).json()["total"] == 0


def test_dashboard_session_upload_requires_the_existing_csrf_header(tmp_path):
    with TestClient(make_app([], TOKEN, tmp_path), base_url="https://testserver") as api:
        assert api.post("/admin/login", json={"password": TOKEN}).status_code == 200
        assert api.post("/v1/private/books?filename=test.txt", content=b"original text").status_code == 403
        response = api.post("/v1/private/books?filename=test.txt", content=b"original text", headers={"X-QuietLeaf-Admin": "1"})
        assert response.status_code == 201, response.text


def test_long_unicode_txt_splits_without_losing_text_and_original_is_retained(tmp_path):
    text = ("Original text 千仞雪.\n\n" * 30000)
    with TestClient(make_app([], TOKEN, tmp_path)) as api:
        result = upload(api, "long.txt", text.encode()).json()["book"]
        chapters = api.get(f"/v1/books/private/{result['id']}/chapters", headers=AUTH).json()["items"]
        assert len(chapters) > 1
        restored = "".join(api.get(f"/v1/books/private/{result['id']}/chapters/{chapter['id']}/text", headers=AUTH).text for chapter in chapters)
        assert restored == text
        assert api.get(f"/v1/private/books/{result['id']}/file", headers=AUTH).content == text.encode()


def test_rejects_encrypted_epub_content_and_xml_entity_expansion(tmp_path):
    with zipfile.ZipFile(io.BytesIO(epub())) as original:
        entries = [(item.filename, original.read(item)) for item in original.infolist()]
    encrypted = zip_bytes(entries + [("META-INF/encryption.xml", '<encryption><CipherReference URI="OPS/first.xhtml"/></encryption>')])
    malicious = zip_bytes([("META-INF/container.xml", '<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><container>&x;</container>')])
    with TestClient(make_app([], TOKEN, tmp_path)) as api:
        response = upload(api, "locked.epub", encrypted)
        assert response.status_code == 422 and "encrypted" in response.json()["detail"]
        assert upload(api, "bad.epub", malicious).status_code == 422
        assert api.get("/v1/private/books", headers=AUTH).json()["total"] == 0
