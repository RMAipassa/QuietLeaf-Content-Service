"""Explicit browser installation/preflight for the AMP service account."""
import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from .adapters import SourceError
from .browser_probe import emit
from .wtrlab_browser import installed_browser_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check an existing install without downloading")
    args = parser.parse_args(argv)
    load_dotenv(Path.cwd() / ".env", override=False)
    if not args.check:
        if importlib.util.find_spec("camoufox") is None:
            emit("browser_setup_failed", reason="Install .[browser-test] in this Python environment first")
            return 1
        if sys.platform == "linux" and shutil.which("Xvfb") is None:
            emit("browser_setup_failed", reason="Install Xvfb on the AMP host before fetching the browser")
            return 1
        environment = {key: value for key, value in os.environ.items() if not key.startswith("QUIETLEAF_")}
        if subprocess.run([sys.executable, "-m", "camoufox", "fetch"], env=environment).returncode:
            emit("browser_setup_failed", reason="browser_download_failed")
            return 1
    try:
        installed_browser_path(os.getenv("QUIETLEAF_WTRLAB_BROWSER_PATH", ""))
        emit("browser_setup_ready", chapter_text_mode="reader-ui")
        return 0
    except SourceError as exc:
        emit("browser_setup_failed", reason=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
