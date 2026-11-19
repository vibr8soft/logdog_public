import json
import logging
import math
import traceback
from datetime import datetime, timezone
from typing import Optional

__version__ = "0.1"

MAX_MESSAGE = 8192
MAX_ATTRS_BYTES = 65536
MAX_ATTRS_KEYS = 200
MAX_ATTRS_DEPTH = 64
MAX_HOST = 128
MAX_ENV = 128
MAX_TRACE_ID = 256
MAX_TRACEBACK = 16384

_STD_RECORD_KEYS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class _TooDeep(Exception):
    pass


class _Cycle(Exception):
    pass


def _clean(s: str) -> str:
    """Make a string safe to encode as UTF-8 (lone surrogates become '?')."""
    return s.encode("utf-8", "replace").decode("utf-8")


def _cut(s: str, limit: int) -> str:
    """Cut a string to at most `limit` UTF-8 bytes, on a character boundary."""
    b = s.encode("utf-8", "replace")
    if len(b) <= limit:
        return b.decode("utf-8")
    return b[:limit].decode("utf-8", "ignore")


def _text(v) -> str:
    try:
        return _clean(str(v))
    except Exception:
        return "<unprintable>"


def _jsonable(v, depth, path):
    """Copy a value into plain JSON types; anything else becomes a string."""
    if v is None or isinstance(v, (bool, int)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else str(v)
    if isinstance(v, str):
        return _clean(v)
    if isinstance(v, (dict, list, tuple)):
        if depth > MAX_ATTRS_DEPTH:
            raise _TooDeep()
        if id(v) in path:
            raise _Cycle()
        path.add(id(v))
        try:
            if isinstance(v, dict):
                return {_text(k): _jsonable(x, depth + 1, path) for k, x in v.items()}
            return [_jsonable(x, depth + 1, path) for x in v]
        finally:
            path.discard(id(v))
    return _text(v)


def _dropped(why: str) -> dict:
    return {"_logdog_dropped": why}


def _shape_attrs(attrs: dict) -> Optional[dict]:
    """Return attrs within Log Dog's limits, a dropped note, or None if empty."""
    if not attrs:
        return None
    if len(attrs) > MAX_ATTRS_KEYS:
        return _dropped("attrs_too_many_keys")
    try:
        clean = _jsonable(attrs, 1, set())
        size = len(json.dumps(clean))
    except _TooDeep:
        return _dropped("attrs_too_deep")
    except (_Cycle, ValueError, TypeError, RecursionError):
        return _dropped("attrs_not_serializable")
    if size > MAX_ATTRS_BYTES:
        return _dropped("attrs_too_large")
    return clean


def _make_entry(level, message, created, attrs, host, env) -> dict:
    entry = {
        "level": level,
        "message": _cut(message, MAX_MESSAGE),
        "ts": datetime.fromtimestamp(created, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ),
    }
    attrs = dict(attrs)
    trace_id = attrs.pop("trace_id", None)
    if trace_id is not None:
        entry["trace_id"] = _cut(_text(trace_id), MAX_TRACE_ID)
    shaped = _shape_attrs(attrs)
    if shaped is not None:
        entry["attrs"] = shaped
    if host:
        entry["host"] = _cut(host, MAX_HOST)
    if env:
        entry["env"] = _cut(env, MAX_ENV)
    return entry


def _level_name(levelno: int) -> str:
    if levelno < logging.DEBUG:
        return "trace"
    if levelno < logging.INFO:
        return "debug"
    if levelno < logging.WARNING:
        return "info"
    if levelno < logging.ERROR:
        return "warn"
    if levelno < logging.CRITICAL:
        return "error"
    return "fatal"


def _exception_attrs(exc_info) -> Optional[dict]:
    if not isinstance(exc_info, tuple) or len(exc_info) != 3 or exc_info[0] is None:
        return None
    etype, value, tb = exc_info
    return {
        "type": getattr(etype, "__name__", _text(etype)),
        "message": _text(value),
        "traceback": _cut(
            "".join(traceback.format_exception(etype, value, tb)), MAX_TRACEBACK
        ),
    }


def build_entry(record: logging.LogRecord, host: str, env: Optional[str]) -> dict:
    """Turn a logging record into a Log Dog entry. Never raises."""
    try:
        try:
            message = record.getMessage()
        except Exception:
            message = _text(record.msg)
        attrs = {
            k: v for k, v in record.__dict__.items() if k not in _STD_RECORD_KEYS
        }
        exc = _exception_attrs(record.exc_info)
        if exc is not None:
            attrs["exc" if "exception" in attrs else "exception"] = exc
        return _make_entry(
            _level_name(record.levelno), message, record.created, attrs, host, env
        )
    except Exception:
        return {
            "level": "error",
            "message": "logdog: could not build entry",
            "host": _cut(_text(host), MAX_HOST),
        }
