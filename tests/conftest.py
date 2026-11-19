"""Never import the default application against a developer's settings/archive."""
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


_bootstrap = tempfile.TemporaryDirectory(prefix="quietleaf-test-default-")
with patch.dict("os.environ", {
    "PYTHON_DOTENV_DISABLED": "1", "QUIETLEAF_API_TOKEN": "isolated-test-default-token",
    "QUIETLEAF_ADMIN_PASSWORD": "", "QUIETLEAF_ARCHIVE_DIR": _bootstrap.name,
    "QUIETLEAF_CATALOG_DIR": str(Path(__file__).resolve().parents[1] / "catalog"),
    "QUIETLEAF_HTML_SOURCES": "", "QUIETLEAF_ENABLE_WTRLAB": "0", "QUIETLEAF_ENABLE_NOVELPING": "0",
}):
    import quietleaf_service.app  # noqa: F401


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    import os
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("QUIETLEAF_") or key == "PYTHON_DOTENV_DISABLED":
            monkeypatch.delenv(key)


def pytest_unconfigure():
    _bootstrap.cleanup()
