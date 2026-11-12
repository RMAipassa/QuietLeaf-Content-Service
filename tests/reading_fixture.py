"""Opt-in local fixture for real browser and Android progress-exchange tests."""
import argparse
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ["QUIETLEAF_ARCHIVE_DIR"] = str(root / "unused-default")
    os.environ["QUIETLEAF_API_TOKEN"] = "reading-fixture-token"
    os.environ["QUIETLEAF_ADMIN_PASSWORD"] = "reading-fixture-password"
    os.environ["QUIETLEAF_ENABLE_WTRLAB"] = "0"
    os.environ["QUIETLEAF_ENABLE_NOVELPING"] = "0"
    os.environ["QUIETLEAF_HTML_SOURCES"] = ""
    from quietleaf_service.app import make_app
    from quietleaf_service.archive import ArchiveStore
    import uvicorn

    class Source:
        id = "fixture"
        name = "Original fixture"

        def details(self, *_):
            raise RuntimeError("Reader must use stored metadata")

        def chapters(self, *_):
            raise RuntimeError("Reader must use stored chapter lists")

        def text(self, *_):
            raise RuntimeError("Reader must not fetch missing chapters")

    archive = root / "archive"
    store = ArchiveStore(archive)
    store.save_book("fixture", {"id": "web-book", "title": "Original browser fixture", "author": "Fixture Writer"})
    store.save_chapters("fixture", "web-book", [{"id": name, "title": title, "ordinal": index}
                        for index, (name, title) in enumerate((("one", "First chapter"), ("two", "Second chapter"), ("missing", "Not saved")))])
    for name in ("one", "two"):
        store.save_text("fixture", "web-book", name,
                       '<img src=x onerror="window.readerInjected=true">\n\n' +
                       (f"This is original fixture chapter {name}, describing a quiet village and a long walk home.\n\n" * 100))
    app = make_app([Source()], "reading-fixture-token", archive, admin_password="reading-fixture-password")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
