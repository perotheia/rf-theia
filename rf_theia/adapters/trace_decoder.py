"""ctypes wrapper around the PLUGGABLE trace decoders.

The decoder is split into plugins, each a `libtrace_decoder_*.so`:

  * ``libtrace_decoder_system.so`` — the FRAMEWORK plugin (sm + other
    framework wire types), built by Bazel from
    ``//platform/runtime/trace:libtrace_decoder_system.so``.
  * ``libtrace_decoder_apps.so`` — a consuming workspace's APP plugin
    (its own ``system_app_*`` types), built from ``//trace:libtrace_decoder_apps.so``.

Each .so carries its OWN process-global registry + its OWN ``trace_decode``
C ABI. This adapter dlopen()s EVERY plugin it finds and, to decode a record,
tries each plugin's ``trace_decode`` in turn until one returns >0. It also
reads each plugin's ``trace_decoder_release_ver()`` and logs a WARNING (it
does NOT hard-fail) when an app plugin's version disagrees with the framework
system plugin's.

Each plugin bakes in libprotobuf descriptors for the types its
``*_protos.cc`` shim registers at static init, so callers just hand a
(msg_type_name, payload_bytes) pair and get JSON back -- no per-type setup
on the Python side.

Pairs with tracer_jsonl.TraceRecord: when the runtime's Tracer.hh
emits a line, TraceRecord.payload_hex carries the raw proto-wire-v3
bytes hex-encoded. Feed that hex into :meth:`TraceDecoder.decode_hex`
to get a structured dict.

Plugin-dir discovery, in order:

  1. ``THEIA_TRACE_DECODER_PATH`` — colon-separated DIRS; every
     ``libtrace_decoder_*.so`` in each dir is loaded.
  2. Legacy single-.so envs (``RF_THEIA_TRACE_DECODER_SO`` /
     ``THEIA_TRACE_DECODER``) — treated as one explicit plugin.
  3. Well-known ``bazel-bin/`` locations discovered by walking up from this
     file, for BOTH the framework system plugin and an app plugin.
"""
from __future__ import annotations

import ctypes
import glob
import json
import logging
import os
from pathlib import Path
from typing import Iterable, List, Optional, Union


_log = logging.getLogger(__name__)


# How big a JSON output the .so should be ready to write into. 4 KiB
# is comfortably larger than any single proto record the runtime
# would actually emit (Tracer.hh truncates payloads at 256 B).
_DEFAULT_JSON_CAP = 4096

# Plugin filename convention.
_PLUGIN_GLOB = "libtrace_decoder_*.so"
# The framework (reference) plugin's filename — its version is the baseline
# the others are compared against.
_SYSTEM_PLUGIN_NAME = "libtrace_decoder_system.so"
# Build byproducts that share the libtrace_decoder_* prefix but are NOT
# loadable plugins (registrar TU with undefined symbols / the decoder-core lib).
_PLUGIN_NOT = ("_protos.so",)


def _is_plugin_filename(name: str) -> bool:
    """True for a real plugin .so (libtrace_decoder_<world>.so), False for the
    cc_library/cc_binary byproducts the _PLUGIN_GLOB also matches."""
    return name.startswith("libtrace_decoder_") and not name.endswith(_PLUGIN_NOT)


def _walk_bazel_bin_candidates() -> List[Path]:
    """Well-known bazel-bin plugin locations, walking up from this file.

    Returns both the FRAMEWORK system plugin and an app plugin under
    every ``bazel-bin/`` ancestor (framework root + a sibling consuming
    workspace's bazel-bin).
    """
    out: List[Path] = []
    here = Path(__file__).resolve()
    for parent in here.parents:
        bb = parent / "bazel-bin"
        if bb.is_dir() or bb.is_symlink():
            out.append(bb / "platform" / "runtime" / "trace" / _SYSTEM_PLUGIN_NAME)
            out.append(bb / "trace" / "libtrace_decoder_apps.so")
    return out


def _discover_plugins() -> List[Path]:
    """Resolve the ordered, de-duplicated list of plugin .so paths."""
    seen: set = set()
    found: List[Path] = []

    def _add(p: Path) -> None:
        try:
            key = p.resolve()
        except OSError:
            key = p
        if key in seen:
            return
        if p.exists():
            seen.add(key)
            found.append(p)

    # 1. THEIA_TRACE_DECODER_PATH — colon-separated plugin DIRS.
    path_env = os.environ.get("THEIA_TRACE_DECODER_PATH", "")
    for d in path_env.split(os.pathsep):
        if not d:
            continue
        for so in sorted(glob.glob(os.path.join(d, _PLUGIN_GLOB))):
            # The glob also matches cc_library/cc_binary BYPRODUCTS that share
            # the libtrace_decoder_* prefix but are NOT loadable plugins — the
            # `*_protos.so` registrar TU (undefined symbols until linked into a
            # plugin) and the bare `libtrace_decoder.so` decoder-core lib. A
            # real plugin is `libtrace_decoder_<world>.so` (system / apps). Skip
            # the byproducts so a stray one in bazel-bin can't shadow / break
            # discovery.
            if _is_plugin_filename(Path(so).name):
                _add(Path(so))

    # 2. Legacy single-.so envs — one explicit plugin each.
    for env_name in ("RF_THEIA_TRACE_DECODER_SO", "THEIA_TRACE_DECODER"):
        v = os.environ.get(env_name)
        if v:
            _add(Path(v))

    # 3. Well-known bazel-bin paths for BOTH plugins.
    for p in _walk_bazel_bin_candidates():
        _add(p)

    return found


class _Plugin:
    """One dlopen'd decoder plugin .so + its bound C ABI."""

    def __init__(self, path: Path) -> None:
        self.path = path
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

        self.release_ver: str = ""
        try:
            self._lib.trace_decoder_release_ver.restype = ctypes.c_char_p
            self._lib.trace_decoder_release_ver.argtypes = []
            v = self._lib.trace_decoder_release_ver()
            if v:
                self.release_ver = v.decode("utf-8", errors="replace")
        except AttributeError:
            pass  # older plugin without the version ABI

    def registered_count(self) -> int:
        return int(self._lib.trace_decoder_size())

    def decode_json(self, msg_type_name: str, payload: bytes) -> Optional[str]:
        """Return JSON, or None if THIS plugin doesn't know the type."""
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
            return None
        return out.value.decode("utf-8")


class TraceDecoder:
    """ctypes binding around the pluggable decoder C ABI in trace_decoder.hh.

    Loads ALL discovered plugins; decode tries each until one succeeds. Each
    .so carries a process-global registry, so opening one twice just yields
    two handles to the same singleton — the loader de-dups by realpath.
    """

    def __init__(
        self,
        so_path: Optional[Union[str, Path]] = None,
        *,
        plugin_paths: Optional[Iterable[Union[str, Path]]] = None,
    ) -> None:
        # Back-compat: an explicit so_path means "use exactly this one
        # plugin" (the legacy single-.so behaviour).
        if so_path is not None:
            paths = [Path(so_path)]
        elif plugin_paths is not None:
            paths = [Path(p) for p in plugin_paths]
        else:
            paths = _discover_plugins()

        if not paths:
            raise FileNotFoundError(
                "no libtrace_decoder_*.so plugins found. Set "
                "THEIA_TRACE_DECODER_PATH (colon-separated dirs) or build "
                "//platform/runtime/trace:libtrace_decoder_system.so "
                "(and the app's //trace:libtrace_decoder_apps.so)."
            )

        self._plugins: List[_Plugin] = []
        for p in paths:
            if not p.exists():
                raise FileNotFoundError(
                    f"libtrace_decoder plugin not found at {p}."
                )
            # A single unloadable .so (missing C ABI symbol, stale build
            # byproduct that slipped through the filename filter) must NOT take
            # the whole decoder down — skip it with a warning and keep the
            # plugins that DO load.
            try:
                self._plugins.append(_Plugin(p))
            except OSError as e:
                _log.warning("trace decoder: skipping unloadable plugin %s (%s)",
                             p, e)

        self._check_versions()

    def _check_versions(self) -> None:
        """WARN (never fail) if an app plugin's version differs from the
        framework system plugin's."""
        fw_ver = ""
        for pl in self._plugins:
            if pl.path.name == _SYSTEM_PLUGIN_NAME:
                fw_ver = pl.release_ver
                break
        if not fw_ver:
            return
        for pl in self._plugins:
            if pl.path.name == _SYSTEM_PLUGIN_NAME:
                continue
            if pl.release_ver and pl.release_ver != fw_ver:
                _log.warning(
                    "trace decoder plugin %s release_ver=%s differs from "
                    "framework system plugin %s -- wire format may have drifted.",
                    pl.path, pl.release_ver, fw_ver,
                )

    @property
    def so_path(self) -> Path:
        """Path of the FIRST loaded plugin (back-compat accessor)."""
        return self._plugins[0].path

    @property
    def plugin_paths(self) -> List[Path]:
        return [pl.path for pl in self._plugins]

    def registered_count(self) -> int:
        """Total message types across all loaded plugins."""
        return sum(pl.registered_count() for pl in self._plugins)

    def decode(self, msg_type_name: str, payload: bytes) -> dict:
        """Decode raw proto-wire-v3 bytes into a Python dict.

        Raises :class:`TraceDecodeError` if NO plugin knows the type / all
        fail to parse.
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
        """Like :meth:`decode` but returns the raw JSON string. Tries each
        plugin until one decodes the type."""
        for pl in self._plugins:
            out = pl.decode_json(msg_type_name, payload)
            if out is not None:
                return out
        raise TraceDecodeError(
            f"decode({msg_type_name!r}, {len(payload)} bytes): no loaded "
            f"plugin ({', '.join(p.path.name for p in self._plugins)}) "
            "could decode this type"
        )


class TraceDecodeError(RuntimeError):
    """Raised by TraceDecoder when no plugin can decode the record."""


_SINGLETON: Optional[TraceDecoder] = None


def open_default() -> TraceDecoder:
    """Return a cached TraceDecoder loading all discovered plugins.

    Convenient for test fixtures and Robot keywords that don't need
    a custom path. Idempotent — the first call constructs, the rest
    return the same instance."""
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = TraceDecoder()
    return _SINGLETON
