"""ctypes wrapper around libtrace_decoder_ctypes.so.

The .so is built by Bazel from //platform/runtime/trace:libtrace_decoder_ctypes.so.
It bakes in libprotobuf descriptors for every message type the
trace_decoder_protos.cc shim registers at static init, so callers
just hand it a (msg_type_name, payload_bytes) pair and get JSON
back -- no per-type setup on the Python side.

Pairs with tracer_jsonl.TraceRecord: when the runtime's Tracer.hh
emits a line, TraceRecord.payload_hex carries the raw proto-wire-v3
bytes hex-encoded. Feed that hex into :meth:`TraceDecoder.decode_hex`
to get a structured dict.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
from typing import Optional, Union


# How big a JSON output the .so should be ready to write into. 4 KiB
# is comfortably larger than any single proto record the runtime
# would actually emit (Tracer.hh truncates payloads at 256 B).
_DEFAULT_JSON_CAP = 4096


def _default_so_path() -> Path:
    """Locate libtrace_decoder_ctypes.so under bazel-bin/.

    Two search strategies, in order:

      1. Honour ``RF_THEIA_TRACE_DECODER_SO`` (an absolute path) if
         set -- lets CI override the path without code changes.
      2. Walk up from this file looking for ``bazel-bin/`` and check
         the canonical location.

    Both strategies return a :class:`Path`; the caller decides if it
    exists. Raising for missing files is left to ``open_default()``
    so callers can construct ``TraceDecoder`` with a custom path.
    """
    env = os.environ.get("RF_THEIA_TRACE_DECODER_SO")
    if env:
        return Path(env)

    here = Path(__file__).resolve()
    for parent in here.parents:
        bb = parent / "bazel-bin"
        if bb.is_dir() or bb.is_symlink():
            return bb / "platform" / "runtime" / "trace" / "libtrace_decoder_ctypes.so"

    # Fall back to a path that doesn't exist; caller will get a
    # clear OSError on dlopen.
    return Path("libtrace_decoder_ctypes.so")


class TraceDecoder:
    """ctypes binding around the C ABI in trace_decoder.hh.

    One process should usually have ONE TraceDecoder -- the .so
    carries a process-global registry, so opening it twice doesn't
    give you separate state, just two handles to the same singleton.
    """

    def __init__(self, so_path: Optional[Union[str, Path]] = None) -> None:
        path = Path(so_path) if so_path is not None else _default_so_path()
        if not path.exists():
            raise FileNotFoundError(
                f"libtrace_decoder_ctypes.so not found at {path}. "
                "Build it via `bazel build "
                "//platform/runtime/trace:libtrace_decoder_ctypes.so`."
            )
        self._path = path
        self._lib = ctypes.CDLL(str(path))

        self._lib.trace_decode.restype = ctypes.c_int
        self._lib.trace_decode.argtypes = [
            ctypes.c_char_p,                    # msg_type_name
            ctypes.POINTER(ctypes.c_ubyte),     # payload
            ctypes.c_ulong,                     # payload_len
            ctypes.c_char_p,                    # out_json buffer
            ctypes.c_ulong,                     # out_cap
        ]
        self._lib.trace_decoder_size.restype = ctypes.c_ulong
        self._lib.trace_decoder_size.argtypes = []

    @property
    def so_path(self) -> Path:
        return self._path

    def registered_count(self) -> int:
        """How many message types the .so was built with."""
        return int(self._lib.trace_decoder_size())

    def decode(self, msg_type_name: str, payload: bytes) -> dict:
        """Decode raw proto-wire-v3 bytes into a Python dict.

        Raises :class:`TraceDecodeError` on unknown type / parse
        failure / output buffer overflow -- the C error message is
        attached.
        """
        return json.loads(self.decode_json(msg_type_name, payload))

    def decode_hex(self, msg_type_name: str, payload_hex: str) -> dict:
        """Hex-encoded variant. The runtime's Tracer.hh emits
        payload bytes as lowercase hex (no separator) -- pass that
        string directly."""
        if payload_hex:
            payload = bytes.fromhex(payload_hex)
        else:
            payload = b""
        return self.decode(msg_type_name, payload)

    def decode_json(self, msg_type_name: str, payload: bytes) -> str:
        """Like :meth:`decode` but returns the raw JSON string. Use
        when you want to skip the json.loads round-trip."""
        out = ctypes.create_string_buffer(_DEFAULT_JSON_CAP)
        if payload:
            buf = (ctypes.c_ubyte * len(payload))(*payload)
            buf_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        else:
            buf_ptr = None
        n = self._lib.trace_decode(
            msg_type_name.encode("utf-8"),
            buf_ptr,
            ctypes.c_ulong(len(payload)),
            out,
            ctypes.c_ulong(_DEFAULT_JSON_CAP),
        )
        if n <= 0:
            raise TraceDecodeError(
                f"decode({msg_type_name!r}, {len(payload)} bytes): "
                f"{out.value.decode('utf-8', errors='replace')}"
            )
        return out.value.decode("utf-8")


class TraceDecodeError(RuntimeError):
    """Raised by TraceDecoder when the .so returns a 0 length."""


_SINGLETON: Optional[TraceDecoder] = None


def open_default() -> TraceDecoder:
    """Return a cached TraceDecoder pointing at the default .so.

    Convenient for test fixtures and Robot keywords that don't need
    a custom path. Idempotent — the first call constructs, the rest
    return the same instance."""
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = TraceDecoder()
    return _SINGLETON
