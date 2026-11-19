"""Raster envelope budgets and bounded, credential-free native-parser subprocesses."""
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .adapters import SourceError


MAX_IMAGE_METADATA_BYTES = 256 * 1024
MAX_IMAGE_BLOCKS = 8192
MAX_RENDER_BYTES = 16 * 1024 * 1024
MAX_RENDER_INPUT_BYTES = 32 * 1024 * 1024
DOCUMENT_MEMORY_BYTES = 768 * 1024 * 1024
DOCUMENT_CPU_SECONDS = 20
DOCUMENT_WALL_SECONDS = 35
RENDER_GATE = threading.BoundedSemaphore(2)


def validate_raster(data: bytes) -> str:
    """Check only intended containers before Pillow can select a decoder or parse EXIF."""
    if not data or len(data) > MAX_RENDER_INPUT_BYTES:
        raise SourceError("The image page exceeds the input size limit")
    metadata = blocks = 0

    def budget(size):
        nonlocal metadata, blocks
        metadata += size
        blocks += 1
        if metadata > MAX_IMAGE_METADATA_BYTES or blocks > MAX_IMAGE_BLOCKS:
            raise SourceError("The image page exceeds the metadata/structure limit")

    def need(end):
        if end > len(data):
            raise SourceError("The image page is truncated")

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        position = 8
        while position < len(data):
            need(position + 12)
            size = int.from_bytes(data[position:position + 4], "big")
            kind = data[position + 4:position + 8]
            need(position + 12 + size)
            budget(0 if kind == b"IDAT" else size)
            position += 12 + size
            if kind == b"IEND":
                break
        return "PNG"
    if data.startswith(b"\xff\xd8\xff"):
        position = 2
        while position < len(data):
            need(position + 2)
            if data[position] != 255:
                raise SourceError("Invalid JPEG structure")
            while position < len(data) and data[position] == 255:
                position += 1
            need(position + 1)
            marker = data[position]
            position += 1
            if marker in (0xDA, 0xD9):
                break
            if marker == 0x01 or 0xD0 <= marker <= 0xD7:
                budget(0)
                continue
            need(position + 2)
            size = int.from_bytes(data[position:position + 2], "big")
            if size < 2:
                raise SourceError("Invalid JPEG structure")
            need(position + size)
            budget(size)
            position += size
        return "JPEG"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        need(12)
        end = int.from_bytes(data[4:8], "little") + 8
        need(end)
        position = 12
        while position < end:
            need(position + 8)
            size = int.from_bytes(data[position + 4:position + 8], "little")
            need(position + 8 + size)
            budget(size if data[position:position + 4] in {b"EXIF", b"ICCP", b"XMP "} else 0)
            position += 8 + size + (size % 2)
        return "WEBP"
    if data.startswith((b"GIF87a", b"GIF89a")):
        need(13)
        position = 13 + (3 * (2 ** ((data[10] & 7) + 1)) if data[10] & 128 else 0)
        while position < len(data):
            marker = data[position]
            position += 1
            if marker == 0x3B:
                break
            if marker == 0x21:
                need(position + 1)
                position += 1  # Extension label, followed by bounded sub-blocks.
                is_metadata = True
            elif marker == 0x2C:
                need(position + 9)
                packed = data[position + 8]
                position += 9 + (3 * (2 ** ((packed & 7) + 1)) if packed & 128 else 0)
                need(position + 1)
                position += 1  # LZW minimum code size.
                is_metadata = False
            else:
                raise SourceError("Invalid GIF structure")
            while True:
                need(position + 1)
                size = data[position]
                position += 1
                need(position + size)
                budget(size if is_metadata else 0)
                position += size
                if not size:
                    break
        return "GIF"
    raise SourceError("Only actual JPEG, PNG, WebP, and GIF image pages are supported")


def render_document(operation: str, payload: bytes = b"", *, edge=2400, index=0) -> bytes:
    environment = {key: value for key, value in os.environ.items() if key.upper() in {
        "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "SYSTEMDRIVE", "LANG", "LC_ALL",
    }}
    # No service tokens/cookies, user Python startup configuration, or archive settings are inherited.
    with RENDER_GATE, tempfile.TemporaryDirectory(prefix="quietleaf-render-") as directory:
        output = Path(directory) / "result"
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-m", "quietleaf_service.document_worker", operation,
                 str(output), str(edge), str(index)], input=payload, cwd=directory, env=environment,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=DOCUMENT_WALL_SECONDS,
            )
            if result.returncode or not output.is_file() or not 0 < output.stat().st_size <= MAX_RENDER_BYTES:
                raise SourceError("The document could not be processed within the parser limits")
            return output.read_bytes()
        except subprocess.TimeoutExpired:
            raise SourceError("The document processing time limit was exceeded") from None
        except OSError:
            raise SourceError("The isolated document parser could not start") from None


def document_preflight() -> dict:
    try:
        return {"ready": True, **json.loads(render_document("probe"))}
    except SourceError:
        return {"ready": False, "message": "The isolated document parser resource limits are unavailable"}
