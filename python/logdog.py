"""Ship a program's logs to Log Dog.

One file, standard library only: copy it next to your code.
Source: https://github.com/vibr8soft/logdog_public

Set LOGDOG_URL (your Log Dog origin) and LOGDOG_KEY (the app's API key), and
optionally LOGDOG_ENV. Then, once, where your program starts:

    import logdog
    logdog.install()

Everything the standard ``logging`` module emits is now also sent to Log Dog;
your existing handlers are untouched. Extra fields become searchable attrs:

    logging.info("checkout started", extra={"order_id": 8812, "trace_id": "4f1c9a"})

To send an entry with your own fields, bypassing ``logging``:

    logdog.error("payment failed", order_id=8812, err="card_declined")
    logdog.send("info", "checkout started", cart={"items": 3})

Without a key or URL every call does nothing, so development machines and
tests need no setup. Entries are sent in the background in gzip batches;
logging never blocks and never fails because of Log Dog. When it cannot be
reached, up to 10,000 entries wait in memory and the oldest are dropped first.
The sender reports its own problems on stderr, at most one line a minute.
"""

import atexit
import collections
import gzip
import http.client
import json
import logging
import math
import os
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import weakref
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


def _endpoint(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.endswith("/v1/logs"):
        return url
    return url + "/v1/logs"


class _Sender:
    """In-memory queue drained by one background thread. Never blocks callers."""

    def __init__(self, url: str, key: str, *, flush_interval: float = 2.0,
                 batch_size: int = 500, queue_size: int = 10000,
                 timeout: float = 10.0, max_body: int = 900_000,
                 backoff_start: float = 1.0, backoff_cap: float = 30.0,
                 queue_bytes: int = 16 * 1024 * 1024):
        self._url = _endpoint(url)
        self._key = key
        self._flush_interval = flush_interval
        self._batch_size = max(1, min(batch_size, 1000))
        self._queue_size = max(1, queue_size)
        self._timeout = timeout
        self._max_body = max_body
        self._backoff_start = backoff_start
        self._backoff_cap = backoff_cap
        self._queue_bytes = queue_bytes
        self.dropped = 0
        self.stopped = False
        self._last_warn = None
        self._reset()

    def _reset(self):
        """Fresh queue, lock and (no) thread. Also used in a forked child."""
        self._cond = threading.Condition()
        self._queue = collections.deque()
        self._queued = 0
        self._oldest = 0.0
        self._inflight = False
        self._flush_now = False
        self._closed = False
        self._thread = None
        _all_senders.add(self)

    # -- callers -----------------------------------------------------------

    def enqueue(self, entry: dict) -> None:
        try:
            raw = json.dumps(entry, separators=(",", ":")).encode("utf-8")
        except Exception:
            return
        with self._cond:
            if self._closed or self.stopped:
                return
            if len(raw) + 2 > self._max_body:
                self.dropped += 1
                return
            while self._queue and (len(self._queue) >= self._queue_size
                                   or self._queued + len(raw) > self._queue_bytes):
                self._queued -= len(self._queue.popleft())
                self.dropped += 1
            if not self._queue:
                self._oldest = time.monotonic()
            self._queue.append(raw)
            self._queued += len(raw)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="logdog-sender", daemon=True
                )
                self._thread.start()
            if len(self._queue) >= self._batch_size:
                self._cond.notify_all()

    def flush(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._cond:
            self._flush_now = True
            self._cond.notify_all()
            while self._queue or self._inflight:
                left = deadline - time.monotonic()
                if left <= 0 or self._closed:
                    return False
                self._cond.wait(left)
            return True

    def close(self, timeout: float = 5.0) -> None:
        with self._cond:
            if self._closed:
                return
        self.flush(timeout)
        with self._cond:
            self._closed = True
            self.dropped += len(self._queue)
            self._queue.clear()
            self._queued = 0
            self._cond.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(0.2)

    # -- worker ------------------------------------------------------------

    def _run(self):
        cond = self._cond
        while True:
            with cond:
                while True:
                    if self._closed or self._cond is not cond:
                        return
                    if self._queue:
                        wait = self._oldest + self._flush_interval - time.monotonic()
                        if (self._flush_now or wait <= 0
                                or len(self._queue) >= self._batch_size):
                            break
                        cond.wait(wait)
                    else:
                        self._flush_now = False
                        cond.wait()
                batch = self._take()
                self._inflight = True
            try:
                self._deliver(batch)
            except Exception:
                self.dropped += len(batch)
            finally:
                with cond:
                    self._inflight = False
                    cond.notify_all()

    def _take(self):
        batch, size = [], 2
        while self._queue and len(batch) < self._batch_size:
            n = len(self._queue[0]) + 1
            if batch and size + n > self._max_body:
                break
            batch.append(self._queue.popleft())
            self._queued -= n - 1
            size += n
        return batch

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        with self._cond:
            while not self._closed:
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                self._cond.wait(left)

    def _deliver(self, batch):
        backoff = self._backoff_start
        resent = False
        while batch and not self._closed:
            status, headers, body = self._post(batch)
            error = body.get("error") if isinstance(body, dict) else None
            if status is not None and 200 <= status < 300:
                return
            if status is None or status in (429, 503):
                wait = backoff
                after = _retry_after(headers)
                if after is not None:
                    wait = max(after, backoff)
                if error != "quota_exceeded":
                    wait = min(wait, self._backoff_cap)
                backoff = min(backoff * 2, self._backoff_cap)
                if status is None:
                    self._warn("cannot reach Log Dog; retrying")
                else:
                    self._warn("refused with %d %s; retrying" % (status, error or ""))
                self._sleep(wait)
                continue
            if status == 400 and not resent:
                index = body.get("index") if isinstance(body, dict) else None
                if (isinstance(index, int) and not isinstance(index, bool)
                        and 0 <= index < len(batch)):
                    del batch[index]
                    self.dropped += 1
                    resent = True
                    self._warn("entry refused with 400 %s; dropped" % (error or ""))
                    continue
            if status == 401:
                with self._cond:
                    self.stopped = True
                    self.dropped += len(self._queue)
                    self._queue.clear()
                    self._queued = 0
                self.dropped += len(batch)
                self._warn("401 %s: the API key was refused; sending is off"
                           % (error or ""))
                return
            self.dropped += len(batch)
            self._warn("batch refused with %d %s; dropped" % (status, error or ""))
            return
        self.dropped += len(batch)

    def _post(self, batch):
        data = gzip.compress(b"[" + b",".join(batch) + b"]")
        req = urllib.request.Request(self._url, data=data, method="POST", headers={
            "Authorization": "Bearer " + self._key,
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
            "User-Agent": "logdog-python/" + __version__,
        })
        try:
            with _opener.open(req, timeout=self._timeout) as resp:
                return resp.status, resp.headers, _json_body(resp.read(65536))
        except urllib.error.HTTPError as e:
            try:
                raw = e.read(65536)
            except Exception:
                raw = b""
            return e.code, e.headers, _json_body(raw)
        except (OSError, http.client.HTTPException, ValueError):
            return None, None, {}

    def _warn(self, text: str) -> None:
        now = time.monotonic()
        if self._last_warn is not None and now - self._last_warn < 60:
            return
        self._last_warn = now
        try:
            print("logdog: " + text.strip(), file=sys.stderr)
        except Exception:
            pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: following one would send the API key to another URL."""

    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)
_all_senders = weakref.WeakSet()


def _after_fork_in_child():
    for sender in list(_all_senders):
        sender._reset()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def _retry_after(headers) -> Optional[int]:
    try:
        value = int(headers.get("Retry-After", "").strip())
    except Exception:
        return None
    return value if value >= 0 else None


def _json_body(raw: bytes) -> dict:
    try:
        body = json.loads(raw)
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


# -- shared sender, direct calls ---------------------------------------------

_shared = None
_shared_lock = threading.Lock()
_hostname = None


def _host() -> str:
    global _hostname
    if _hostname is None:
        try:
            _hostname = socket.gethostname()
        except Exception:
            _hostname = ""
    return _hostname


def _shared_sender() -> Optional[_Sender]:
    """The process-wide sender built from LOGDOG_URL and LOGDOG_KEY, or None."""
    global _shared
    url = os.environ.get("LOGDOG_URL", "").strip()
    key = os.environ.get("LOGDOG_KEY", "").strip()
    if not url or not key:
        return None
    with _shared_lock:
        current = _shared
        if current is not None and current._id == (url, key) and not current._closed:
            return current
        sender = _Sender(url, key)
        sender._id = (url, key)
        _shared = sender
    if current is not None:
        current.close(0.5)
    return sender


def _close_shared() -> None:
    sender = _shared
    if sender is not None:
        sender.close(5.0)


atexit.register(_close_shared)

_LEVELS = ("trace", "debug", "info", "warn", "error", "fatal")
_LEVEL_ALIASES = {"warning": "warn", "critical": "fatal"}


def build_direct_entry(level, message, attrs: dict, host: str,
                       env: Optional[str]) -> dict:
    """Build an entry from a direct call. Never raises."""
    try:
        name = _text(level).strip().lower()
        name = _LEVEL_ALIASES.get(name, name)
        if name not in _LEVELS:
            name = "info"
        return _make_entry(name, _text(message), time.time(), attrs, host, env)
    except Exception:
        return {"level": "error", "message": "logdog: could not build entry"}


def send(level, message, /, **attrs) -> None:
    """Send one entry straight to Log Dog, with your own fields.

    Goes to Log Dog only (not to the console or the logging module) and is
    sent whatever the logger's level. Does nothing without LOGDOG_KEY/URL.
    """
    try:
        sender = _shared_sender()
        if sender is None:
            return
        sender.enqueue(build_direct_entry(
            level, message, attrs, _host(), os.environ.get("LOGDOG_ENV")
        ))
    except Exception:
        pass


def trace(message, /, **attrs) -> None:
    send("trace", message, **attrs)


def debug(message, /, **attrs) -> None:
    send("debug", message, **attrs)


def info(message, /, **attrs) -> None:
    send("info", message, **attrs)


def warn(message, /, **attrs) -> None:
    send("warn", message, **attrs)


def error(message, /, **attrs) -> None:
    send("error", message, **attrs)


def fatal(message, /, **attrs) -> None:
    send("fatal", message, **attrs)


def flush(timeout: float = 5.0) -> bool:
    """Wait until everything queued so far is sent. True if it was."""
    sender = _shared
    if sender is None:
        return True
    return sender.flush(timeout)


# -- logging handler -----------------------------------------------------------

class LogDogHandler(logging.Handler):
    """Ships logging records to Log Dog. Does nothing without a key and URL."""

    def __init__(self, url: Optional[str] = None, key: Optional[str] = None,
                 env: Optional[str] = None, level: int = logging.NOTSET,
                 **sender_options):
        super().__init__(level)
        self._env = env if env is not None else os.environ.get("LOGDOG_ENV")
        self._owns_sender = False
        if url is None and key is None and not sender_options:
            self._sender = _shared_sender()
        else:
            url = (url if url is not None else os.environ.get("LOGDOG_URL", "")).strip()
            key = (key if key is not None else os.environ.get("LOGDOG_KEY", "")).strip()
            self._sender = _Sender(url, key, **sender_options) if url and key else None
            self._owns_sender = self._sender is not None
        if self._owns_sender:
            atexit.register(self.close)

    def emit(self, record) -> None:
        try:
            sender = self._sender
            if sender is None or record.threadName == "logdog-sender":
                return
            sender.enqueue(build_entry(record, _host(), self._env))
        except Exception:
            pass

    def flush(self) -> None:
        try:
            if self._sender is not None:
                self._sender.flush(5.0)
        except Exception:
            pass

    def close(self) -> None:
        try:
            if self._sender is not None:
                if self._owns_sender:
                    self._sender.close(5.0)
                else:
                    self._sender.flush(5.0)
        except Exception:
            pass
        super().close()


def install(logger: Optional[logging.Logger] = None, level: int = logging.NOTSET,
            **options) -> Optional[LogDogHandler]:
    """Add a Log Dog handler to `logger` (the root logger by default).

    Returns the handler, or None (adding nothing) when LOGDOG_KEY or
    LOGDOG_URL is not set. Safe to call more than once.
    """
    if logger is None:
        logger = logging.getLogger()
    for existing in logger.handlers:
        if isinstance(existing, LogDogHandler):
            return existing
    handler = LogDogHandler(level=level, **options)
    if handler._sender is None:
        return None
    logger.addHandler(handler)
    return handler
