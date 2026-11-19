"""One-shot native parser worker; no application/environment loader or persistent state."""
import json
import os
import sys
from pathlib import Path

from .document_limits import (
    DOCUMENT_CPU_SECONDS, DOCUMENT_MEMORY_BYTES, DOCUMENT_WALL_SECONDS,
    MAX_RENDER_BYTES, MAX_RENDER_INPUT_BYTES,
)

_job_handle = None


def resource_limits():
    global _job_handle
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                        ("max_working", ctypes.c_size_t), ("active", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class IOCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("read", "write", "other", "read_bytes", "write_bytes", "other_bytes")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IOCounters),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        _job_handle = kernel.CreateJobObjectW(None, None)
        limits = ExtendedLimits()
        # Per-process user CPU, active process count, committed memory, and kill-on-close.
        limits.basic.flags = 0x2 | 0x8 | 0x100 | 0x2000
        limits.basic.process_time = DOCUMENT_CPU_SECONDS * 10_000_000
        limits.basic.active = 1
        limits.process_memory = DOCUMENT_MEMORY_BYTES
        if (not _job_handle or not kernel.SetInformationJobObject(_job_handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not kernel.AssignProcessToJobObject(_job_handle, kernel.GetCurrentProcess())):
            raise RuntimeError("Unable to enforce parser limits")
    else:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (DOCUMENT_MEMORY_BYTES, DOCUMENT_MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (DOCUMENT_CPU_SECONDS, DOCUMENT_CPU_SECONDS))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_RENDER_BYTES, MAX_RENDER_BYTES))
    return {"memoryBytes": DOCUMENT_MEMORY_BYTES, "cpuSeconds": DOCUMENT_CPU_SECONDS,
            "wallSeconds": DOCUMENT_WALL_SECONDS}


def main():
    try:
        operation, destination, edge, index = sys.argv[1:]
        limits = resource_limits()  # Refuse processing if OS resource limits cannot be installed.
        if operation == "probe":
            data = json.dumps(limits).encode()
        else:
            from .private_library import _decode_jpeg_page, _render_pdf_page, _pdf_page_count
            payload = sys.stdin.buffer.read(MAX_RENDER_INPUT_BYTES + 1)
            if len(payload) > MAX_RENDER_INPUT_BYTES or not 1 <= int(edge) <= 2400:
                return 1
            if operation == "image":
                data = _decode_jpeg_page(payload, int(edge))
            elif operation in {"pdf-page", "pdf-info"}:
                path = Path(json.loads(payload)["path"])
                data = (_render_pdf_page(path, int(index), int(edge)) if operation == "pdf-page" else
                        json.dumps({"pageCount": _pdf_page_count(path)}).encode())
            else:
                return 1
        if not 0 < len(data) <= MAX_RENDER_BYTES:
            return 1
        Path(destination).write_bytes(data)
        return 0
    except Exception:
        return 1  # Never emit native errors, paths, file contents, or environment details.


if __name__ == "__main__":
    raise SystemExit(main())
