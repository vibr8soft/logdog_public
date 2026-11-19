import contextlib
import gzip
import http.server
import io
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import unittest

import logdog


def rec(level, msg, *args, exc_info=None, **extra):
    return logging.getLogger("t").makeRecord(
        "t", level, __file__, 1, msg, args, exc_info, extra=extra or None
    )


class BuildEntryTest(unittest.TestCase):
    def test_basic(self):
        e = logdog.build_entry(
            rec(logging.ERROR, "payment %s", "failed", order_id=8812), "web-3", "prod"
        )
        self.assertEqual(e["level"], "error")
        self.assertEqual(e["message"], "payment failed")
        self.assertEqual(e["attrs"], {"order_id": 8812})
        self.assertEqual((e["host"], e["env"]), ("web-3", "prod"))
        self.assertRegex(e["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")
        self.assertLessEqual(
            set(e), {"level", "message", "ts", "attrs", "host", "env", "trace_id"}
        )

    def test_levels(self):
        cases = [(5, "trace"), (10, "debug"), (20, "info"), (25, "info"),
                 (30, "warn"), (40, "error"), (50, "fatal"), (60, "fatal")]
        for py, ld in cases:
            self.assertEqual(logdog.build_entry(rec(py, "x"), "h", None)["level"], ld)

    def test_no_env_no_attrs_keys_absent(self):
        e = logdog.build_entry(rec(logging.INFO, "x"), "h", None)
        self.assertNotIn("env", e)
        self.assertNotIn("attrs", e)
        self.assertNotIn("trace_id", e)

    def test_message_cut_on_character_boundary(self):
        e = logdog.build_entry(rec(logging.INFO, "é" * 5000), "h", None)
        b = e["message"].encode("utf-8")
        self.assertLessEqual(len(b), 8192)
        self.assertGreater(len(b), 8000)
        b.decode("utf-8")

    def test_bad_format_args_do_not_raise(self):
        e = logdog.build_entry(rec(logging.INFO, "%d items", "abc"), "h", None)
        self.assertEqual(e["message"], "%d items")

    def test_trace_id_moves_out_of_attrs(self):
        e = logdog.build_entry(rec(logging.INFO, "x", trace_id="4f1c9a", a=1), "h", None)
        self.assertEqual(e["trace_id"], "4f1c9a")
        self.assertEqual(e["attrs"], {"a": 1})

    def test_exception(self):
        try:
            raise ValueError("boom")
        except ValueError:
            r = rec(logging.ERROR, "x", exc_info=sys.exc_info())
        ex = logdog.build_entry(r, "h", None)["attrs"]["exception"]
        self.assertEqual((ex["type"], ex["message"]), ("ValueError", "boom"))
        self.assertIn("Traceback", ex["traceback"])

    def test_extra_named_exception_is_kept(self):
        try:
            raise ValueError("boom")
        except ValueError:
            r = rec(logging.ERROR, "x", exc_info=sys.exc_info(), exception="mine")
        a = logdog.build_entry(r, "h", None)["attrs"]
        self.assertEqual(a["exception"], "mine")
        self.assertEqual(a["exc"]["type"], "ValueError")

    def test_odd_values_become_strings(self):
        e = logdog.build_entry(
            rec(logging.INFO, "x", o=object(), b=b"\xff", s={1}, bad="\ud800"), "h", None
        )
        json.dumps(e).encode("utf-8")
        self.assertIsInstance(e["attrs"]["o"], str)
        self.assertIsInstance(e["attrs"]["s"], str)

    def test_lone_surrogate_in_message(self):
        e = logdog.build_entry(rec(logging.INFO, "a\ud800b"), "h", None)
        e["message"].encode("utf-8")

    def test_circular_attrs_dropped(self):
        d = {}
        d["self"] = d
        e = logdog.build_entry(rec(logging.INFO, "x", d=d), "h", None)
        self.assertEqual(e["attrs"], {"_logdog_dropped": "attrs_not_serializable"})

    def test_attrs_too_large(self):
        e = logdog.build_entry(rec(logging.INFO, "x", big="a" * 70000), "h", None)
        self.assertEqual(e["attrs"], {"_logdog_dropped": "attrs_too_large"})

    def test_attrs_too_many_keys(self):
        extra = {"k%d" % i: i for i in range(201)}
        e = logdog.build_entry(rec(logging.INFO, "x", **extra), "h", None)
        self.assertEqual(e["attrs"], {"_logdog_dropped": "attrs_too_many_keys"})

    def test_attrs_too_deep(self):
        d = cur = {}
        for _ in range(70):
            cur["n"] = {}
            cur = cur["n"]
        e = logdog.build_entry(rec(logging.INFO, "x", d=d), "h", None)
        self.assertEqual(e["attrs"], {"_logdog_dropped": "attrs_too_deep"})

    def test_empty_message_gets_a_placeholder(self):
        e = logdog.build_entry(rec(logging.INFO, ""), "h", None)
        self.assertEqual(e["message"], "(empty)")
        e = logdog.build_direct_entry("info", "", {}, "h", None)
        self.assertEqual(e["message"], "(empty)")

    def test_integers_the_server_refuses_become_strings(self):
        e = logdog.build_entry(
            rec(logging.INFO, "x", big=2**70, neg=-2**63 - 1, top=2**64 - 1, low=-2**63),
            "h", None,
        )
        self.assertEqual(e["attrs"], {"big": str(2**70), "neg": str(-2**63 - 1),
                                      "top": 2**64 - 1, "low": -2**63})

    def test_host_env_trace_cut(self):
        e = logdog.build_entry(
            rec(logging.INFO, "x", trace_id="t" * 300), "h" * 200, "e" * 200
        )
        self.assertEqual(
            (len(e["host"]), len(e["env"]), len(e["trace_id"])), (128, 128, 256)
        )


class Req:
    def __init__(self, headers, body, raw_len):
        self.headers = headers
        self.body = body
        self.raw_len = raw_len


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeServer:
    """A local stand-in for POST /v1/logs that answers from a script."""

    def __init__(self, port=0):
        self.requests = []
        self.script = []
        self.lock = threading.Lock()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                body = json.loads(raw)
                with outer.lock:
                    outer.requests.append(Req(self.headers, body, len(raw)))
                    answer = outer.script.pop(0) if outer.script else None
                if answer is None:
                    n = len(body) if isinstance(body, list) else 1
                    answer = (202, {}, {"accepted": n})
                status, headers, payload = answer
                data = json.dumps(payload).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                with outer.lock:
                    outer.requests.append(Req(self.headers, None, 0))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reset(self):
        with self.lock:
            self.requests.clear()
            self.script.clear()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.addCleanup(self.server.close)
        quiet = contextlib.redirect_stderr(io.StringIO())
        self.err = quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def sender(self, **kw):
        opts = dict(flush_interval=0.05, backoff_start=0.05, backoff_cap=0.2)
        opts.update(kw)
        s = logdog._Sender(self.server.url, "ld_test", **opts)
        self.addCleanup(s.close, 0.1)
        return s


def E(message):
    return {"level": "info", "message": message}


class SenderTest(ServerCase):
    def test_endpoint(self):
        for u in ["http://h", "http://h/", "http://h//", "http://h/v1/logs",
                  "http://h/v1/logs/"]:
            self.assertEqual(logdog._endpoint(u), "http://h/v1/logs")

    def test_posts_gzip_batch_with_headers(self):
        s = self.sender()
        s.enqueue(E("a"))
        s.enqueue(E("b"))
        self.assertTrue(s.flush(2))
        r = self.server.requests[0]
        self.assertEqual(r.headers["Authorization"], "Bearer ld_test")
        self.assertEqual(r.headers["Content-Encoding"], "gzip")
        self.assertEqual(r.headers["Content-Type"], "application/json")
        self.assertEqual(r.headers["User-Agent"], "logdog-python/0.1")
        self.assertEqual([e["message"] for e in r.body], ["a", "b"])

    def test_sends_after_interval_without_flush(self):
        s = self.sender()
        s.enqueue(E("a"))
        time.sleep(0.5)
        self.assertEqual(len(self.server.requests), 1)

    def test_second_entry_after_first_batch_is_sent_on_the_interval(self):
        s = self.sender()
        s.enqueue(E("first"))
        time.sleep(0.4)
        s.enqueue(E("second"))
        time.sleep(0.6)
        sent = [e["message"] for r in self.server.requests for e in r.body]
        self.assertEqual(sent, ["first", "second"])

    def test_url_without_scheme_is_reported_on_stderr(self):
        s = logdog._Sender("logs.example.invalid", "ld_test", flush_interval=0.05,
                           backoff_start=0.05, backoff_cap=0.2)
        self.addCleanup(s.close, 0.1)
        s.enqueue(E("a"))
        time.sleep(0.4)
        self.assertIn("logdog: ", self.err.getvalue())

    def test_batch_size_and_body_cap(self):
        s = self.sender(batch_size=3, max_body=10_000)
        for _ in range(7):
            s.enqueue(E("x" * 3000))
        self.assertTrue(s.flush(2))
        for r in self.server.requests:
            self.assertLessEqual(len(r.body), 3)
            self.assertLessEqual(r.raw_len, 10_000)
        self.assertEqual(sum(len(r.body) for r in self.server.requests), 7)

    def test_429_waits_retry_after_then_resends_same_batch(self):
        self.server.script = [(429, {"Retry-After": "1"}, {"error": "rate_limited"})]
        s = self.sender(backoff_cap=30)
        s.enqueue(E("a"))
        t = time.monotonic()
        self.assertTrue(s.flush(5))
        self.assertGreaterEqual(time.monotonic() - t, 1.0)
        self.assertEqual(self.server.requests[0].body, self.server.requests[1].body)

    def test_quota_exceeded_waits_past_the_cap(self):
        self.server.script = [(429, {"Retry-After": "1"}, {"error": "quota_exceeded"})]
        s = self.sender(backoff_cap=0.1)
        s.enqueue(E("a"))
        t = time.monotonic()
        self.assertTrue(s.flush(5))
        self.assertGreaterEqual(time.monotonic() - t, 1.0)

    def test_rate_limited_retry_after_is_capped(self):
        self.server.script = [(429, {"Retry-After": "60"}, {"error": "rate_limited"})]
        s = self.sender(backoff_cap=0.1)
        s.enqueue(E("a"))
        self.assertTrue(s.flush(3))

    def test_503_without_retry_after_backs_off_and_doubles(self):
        self.server.script = [
            (503, {}, {"error": "backpressure"}),
            (503, {"Retry-After": "soon"}, {"error": "backpressure"}),
        ]
        s = self.sender()
        s.enqueue(E("a"))
        self.assertTrue(s.flush(3))
        self.assertEqual(len(self.server.requests), 3)

    def test_network_error_retries(self):
        port = free_port()
        s = logdog._Sender("http://127.0.0.1:%d" % port, "ld_test", flush_interval=0.05,
                           backoff_start=0.05, backoff_cap=0.2, timeout=0.5)
        self.addCleanup(s.close, 0.1)
        with contextlib.redirect_stderr(io.StringIO()):
            s.enqueue(E("a"))
            time.sleep(0.3)
            srv = FakeServer(port=port)
            self.addCleanup(srv.close)
            self.assertTrue(s.flush(3))
        self.assertEqual(len(srv.requests), 1)

    def test_400_with_index_drops_that_entry_and_resends_rest_once(self):
        self.server.script = [(400, {}, {"error": "message_too_long", "index": 1})]
        s = self.sender()
        for m in "abc":
            s.enqueue(E(m))
        self.assertTrue(s.flush(2))
        self.assertEqual([e["message"] for e in self.server.requests[1].body], ["a", "c"])
        self.assertEqual(s.dropped, 1)

    def test_second_400_drops_batch(self):
        self.server.script = [(400, {}, {"error": "x", "index": 0}),
                              (400, {}, {"error": "x", "index": 0})]
        s = self.sender()
        for m in "abc":
            s.enqueue(E(m))
        self.assertTrue(s.flush(2))
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(s.dropped, 3)

    def test_400_without_index_drops_batch(self):
        self.server.script = [(400, {}, {"error": "invalid_json"})]
        s = self.sender()
        s.enqueue(E("a"))
        self.assertTrue(s.flush(2))
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(s.dropped, 1)

    def test_401_stops_for_good(self):
        self.server.script = [(401, {}, {"error": "invalid_api_key"})]
        s = self.sender()
        with contextlib.redirect_stderr(io.StringIO()):
            s.enqueue(E("a"))
            s.flush(2)
            s.enqueue(E("b"))
            s.flush(1)
        self.assertTrue(s.stopped)
        self.assertEqual(len(self.server.requests), 1)

    def test_500_and_other_4xx_drop_batch_without_retry(self):
        for code in (500, 413, 415):
            self.server.reset()
            self.server.script = [(code, {}, {"error": "x"})]
            s = self.sender()
            with contextlib.redirect_stderr(io.StringIO()):
                s.enqueue(E("a"))
                self.assertTrue(s.flush(2))
            self.assertEqual(len(self.server.requests), 1)

    def test_overflow_drops_oldest(self):
        self.server.script = [(503, {"Retry-After": "1"}, {})]
        s = self.sender(queue_size=5, batch_size=1)
        with contextlib.redirect_stderr(io.StringIO()):
            for i in range(20):
                s.enqueue(E(str(i)))
            self.assertTrue(s.flush(5))
        sent = [e["message"] for r in self.server.requests for e in r.body]
        self.assertEqual(sent[-1], "19")
        self.assertGreater(s.dropped, 0)
        self.assertLessEqual(len(set(sent)), 6)

    def test_stderr_is_rate_limited_and_hides_key(self):
        self.server.script = [(500, {}, {})] * 5
        with contextlib.redirect_stderr(io.StringIO()) as err:
            s = self.sender(batch_size=1)
            for _ in range(5):
                s.enqueue(E("a"))
            s.flush(3)
        out = err.getvalue()
        self.assertEqual(out.count("logdog: "), 1)
        self.assertIn("500", out)
        self.assertNotIn("ld_test", out)

    def test_enqueue_after_close_is_silent(self):
        s = self.sender()
        s.close(1)
        s.enqueue(E("a"))
        time.sleep(0.2)
        self.assertEqual(len(self.server.requests), 0)

    def test_redirect_is_not_followed_so_the_key_stays_home(self):
        other = FakeServer()
        self.addCleanup(other.close)
        self.server.script = [(302, {"Location": other.url + "/v1/logs"}, {})]
        s = self.sender()
        s.enqueue(E("a"))
        self.assertTrue(s.flush(2))
        self.assertEqual(other.requests, [])
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(s.dropped, 1)

    def test_queue_byte_cap_drops_oldest(self):
        s = self.sender(queue_bytes=10_000, flush_interval=30)
        for i in range(10):
            s.enqueue({"level": "info", "message": "x" * 3000, "attrs": {"i": i}})
        self.assertTrue(s.flush(2))
        sent = [e["attrs"]["i"] for r in self.server.requests for e in r.body]
        self.assertEqual(sent, [7, 8, 9])
        self.assertEqual(s.dropped, 7)

    def test_entry_larger_than_max_body_is_dropped(self):
        s = self.sender(max_body=1000)
        s.enqueue(E("x" * 5000))
        s.enqueue(E("ok"))
        self.assertTrue(s.flush(2))
        self.assertEqual([e["message"] for r in self.server.requests for e in r.body],
                         ["ok"])
        self.assertEqual(s.dropped, 1)

    def test_enqueue_never_blocks_while_server_is_slow(self):
        port = free_port()                      # nothing listens: every send fails
        s = logdog._Sender("http://127.0.0.1:%d" % port, "ld_test", flush_interval=0.01)
        self.addCleanup(s.close, 0.1)
        with contextlib.redirect_stderr(io.StringIO()):
            t = time.monotonic()
            for i in range(20000):
                s.enqueue(E(str(i)))
            self.assertLess(time.monotonic() - t, 2.0)


HERE = os.path.dirname(os.path.abspath(__file__))


@contextlib.contextmanager
def env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _mp_child(method):
    logdog.error("child " + method)


class HandlerCase(ServerCase):
    def logger(self, name, level=logging.INFO):
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.propagate = False
        self.addCleanup(lambda: [lg.removeHandler(h) for h in list(lg.handlers)])
        return lg

    def handler(self):
        h = logdog.LogDogHandler(self.server.url, "ld_test", flush_interval=0.05,
                                 backoff_start=0.05, backoff_cap=0.2)
        self.addCleanup(h.close)
        return h

    def messages(self):
        return [e["message"] for r in self.server.requests for e in r.body]


class HandlerTest(HandlerCase):
    def test_install_without_key_is_a_silent_noop(self):
        lg = self.logger("noop")
        cap = Capture()
        lg.addHandler(cap)
        with env(LOGDOG_KEY=None, LOGDOG_URL=None):
            self.assertIsNone(logdog.install(lg))
            lg.error("nothing happens")
        self.assertEqual(lg.handlers, [cap])
        self.assertEqual(self.err.getvalue(), "")

    def test_install_without_url_is_a_silent_noop(self):
        lg = self.logger("nourl")
        with env(LOGDOG_KEY="ld_test", LOGDOG_URL=None):
            self.assertIsNone(logdog.install(lg))
        self.assertEqual(lg.handlers, [])

    def test_install_ships_existing_log_calls(self):
        lg = self.logger("ship")
        with env(LOGDOG_KEY="ld_test", LOGDOG_URL=self.server.url, LOGDOG_ENV="dev"):
            h = logdog.install(lg, flush_interval=0.05)
        self.addCleanup(h.close)
        lg.info("checkout started", extra={"order_id": 8812})
        lg.debug("hidden")
        h.flush()
        body = self.server.requests[0].body
        self.assertEqual(len(body), 1)
        self.assertEqual(
            (body[0]["message"], body[0]["attrs"], body[0]["env"]),
            ("checkout started", {"order_id": 8812}, "dev"),
        )
        self.assertEqual(body[0]["host"], socket.gethostname()[:128])

    def test_install_twice_adds_one_handler(self):
        lg = self.logger("twice")
        with env(LOGDOG_KEY="ld_test", LOGDOG_URL=self.server.url):
            a = logdog.install(lg)
            b = logdog.install(lg)
        self.assertIs(a, b)
        self.assertEqual(len(lg.handlers), 1)

    def test_handler_level_filters(self):
        lg = self.logger("lvl")
        with env(LOGDOG_KEY="ld_test", LOGDOG_URL=self.server.url):
            h = logdog.install(lg, level=logging.ERROR, flush_interval=0.05)
        self.addCleanup(h.close)
        lg.info("skipped")
        lg.error("kept")
        h.flush()
        self.assertEqual(self.messages(), ["kept"])

    def test_handler_without_key_does_nothing(self):
        with env(LOGDOG_KEY=None, LOGDOG_URL=None):
            h = logdog.LogDogHandler()
        lg = self.logger("nokey")
        lg.addHandler(h)
        lg.error("x")
        h.flush()
        h.close()
        self.assertEqual(self.err.getvalue(), "")

    def test_records_from_the_sender_thread_are_ignored(self):
        h = self.handler()
        r = logging.makeLogRecord(
            {"msg": "x", "levelno": 20, "threadName": "logdog-sender"}
        )
        h.emit(r)
        h.flush()
        self.assertEqual(self.server.requests, [])

    def test_emit_never_raises(self):
        h = self.handler()
        h.emit("not a record")
        h.emit(None)

    def test_exit_flushes(self):
        code = ("import logging, logdog; logging.getLogger().setLevel(20); "
                "logdog.install(); logging.info('bye')")
        subprocess.run([sys.executable, "-c", code], cwd=HERE, check=True, timeout=10,
                       env=dict(os.environ, LOGDOG_KEY="ld_test",
                                LOGDOG_URL=self.server.url))
        self.assertEqual(self.server.requests[0].body[0]["message"], "bye")

    def test_exit_flushes_direct_calls(self):
        code = "import logdog; logdog.info('direct bye', n=1)"
        subprocess.run([sys.executable, "-c", code], cwd=HERE, check=True, timeout=10,
                       env=dict(os.environ, LOGDOG_KEY="ld_test",
                                LOGDOG_URL=self.server.url))
        self.assertEqual(self.server.requests[0].body[0]["message"], "direct bye")

    def test_exit_with_server_down_returns_within_6_seconds(self):
        code = "import logging, logdog; logdog.install(); logging.error('x')"
        t = time.monotonic()
        subprocess.run([sys.executable, "-c", code], cwd=HERE, timeout=15,
                       stderr=subprocess.DEVNULL,
                       env=dict(os.environ, LOGDOG_KEY="ld_test",
                                LOGDOG_URL="http://127.0.0.1:9"))
        self.assertLess(time.monotonic() - t, 6.5)

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork")
    def test_child_after_fork_sends_its_own_logs(self):
        h = self.handler()
        lg = self.logger("fork")
        lg.addHandler(h)
        lg.info("parent")
        h.flush()
        pid = os.fork()
        if pid == 0:
            try:
                lg.info("child")
                h.flush()
            finally:
                os._exit(0)
        os.waitpid(pid, 0)
        self.assertEqual(sorted(self.messages()), ["child", "parent"])

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork")
    def test_child_does_not_resend_what_the_parent_queued(self):
        h = logdog.LogDogHandler(self.server.url, "ld_test", flush_interval=0.5)
        self.addCleanup(h.close)
        lg = self.logger("fork2")
        lg.addHandler(h)
        lg.info("parent queued")              # still in the queue at fork time
        pid = os.fork()
        if pid == 0:
            try:
                lg.info("child")
                h.flush()
            finally:
                os._exit(0)
        os.waitpid(pid, 0)
        h.flush()
        self.assertEqual(sorted(self.messages()), ["child", "parent queued"])

    def test_short_lived_multiprocessing_child_delivers_its_logs(self):
        import multiprocessing
        methods = [m for m in ("fork", "spawn")
                   if m in multiprocessing.get_all_start_methods()]
        with env(LOGDOG_KEY="ld_test", LOGDOG_URL=self.server.url):
            for method in methods:
                p = multiprocessing.get_context(method).Process(
                    target=_mp_child, args=(method,))
                p.start()
                p.join(15)
                self.assertEqual(p.exitcode, 0)
        self.assertEqual(sorted(self.messages()), sorted("child " + m for m in methods))

    def test_many_threads_and_emit_after_close(self):
        h = self.handler()
        lg = self.logger("mt")
        lg.addHandler(h)
        ts = [threading.Thread(target=lambda: [lg.info("m") for _ in range(200)])
              for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        h.flush()
        self.assertEqual(sum(len(r.body) for r in self.server.requests), 1600)
        h.close()
        lg.info("late")


class DirectTest(HandlerCase):
    def test_direct_call_without_install(self):
        with env(LOGDOG_KEY="ld_test", LOGDOG_URL=self.server.url, LOGDOG_ENV="dev"):
            logdog.error("payment failed", order_id=8812, err="card_declined",
                         trace_id="4f1c9a")
            self.assertTrue(logdog.flush())
        e = self.server.requests[0].body[0]
        self.assertEqual(
            (e["level"], e["message"], e["trace_id"], e["env"]),
            ("error", "payment failed", "4f1c9a", "dev"),
        )
        self.assertEqual(e["attrs"], {"order_id": 8812, "err": "card_declined"})
        self.assertRegex(e["ts"], r"Z$")
        self.assertEqual(e["host"], socket.gethostname()[:128])

    def test_every_shorthand_and_send(self):
        with env(LOGDOG_KEY="ld_test", LOGDOG_URL=self.server.url):
            for name in ("trace", "debug", "info", "warn", "error", "fatal"):
                getattr(logdog, name)(name)
            logdog.send("warning", "sent", level=3, message="m")
            logdog.flush()
        got = [(e["level"], e["message"]) for r in self.server.requests for e in r.body]
        self.assertEqual(got[:6], [(n, n) for n in
                                   ("trace", "debug", "info", "warn", "error", "fatal")])
        self.assertEqual(got[6], ("warn", "sent"))
        last = [e for r in self.server.requests for e in r.body][6]
        self.assertEqual(last["attrs"], {"level": 3, "message": "m"})

    def test_direct_and_installed_share_one_sender(self):
        lg = self.logger("share", logging.WARNING)
        with env(LOGDOG_KEY="ld_test", LOGDOG_URL=self.server.url):
            h = logdog.install(lg)
            lg.warning("via logging")
            logdog.debug("via direct")
            h.flush()
            self.assertIs(h._sender, logdog._shared_sender())
        self.assertEqual(sorted(self.messages()), ["via direct", "via logging"])

    def test_direct_does_not_reach_the_logger(self):
        cap = Capture()
        root = logging.getLogger()
        root.addHandler(cap)
        self.addCleanup(root.removeHandler, cap)
        with env(LOGDOG_KEY="ld_test", LOGDOG_URL=self.server.url):
            logdog.info("quiet")
            logdog.flush()
        self.assertEqual(cap.records, [])
        self.assertEqual(self.messages(), ["quiet"])

    def test_levels_and_aliases(self):
        cases = [("trace", "trace"), ("WARN", "warn"), ("warning", "warn"),
                 ("critical", "fatal"), ("nonsense", "info"), (None, "info")]
        for given, sent in cases:
            self.assertEqual(
                logdog.build_direct_entry(given, "x", {}, "h", None)["level"], sent
            )

    def test_direct_without_key_is_silent_noop(self):
        with env(LOGDOG_KEY=None, LOGDOG_URL=None):
            logdog.error("x", a=object())
            self.assertTrue(logdog.flush())
        self.assertEqual(self.err.getvalue(), "")

    def test_direct_limits_and_odd_values_never_raise(self):
        e = logdog.build_direct_entry("info", "é" * 5000, {"big": "a" * 70000}, "h", None)
        self.assertLessEqual(len(e["message"].encode()), 8192)
        self.assertEqual(e["attrs"], {"_logdog_dropped": "attrs_too_large"})
        e = logdog.build_direct_entry("info", 12345, {"o": object()}, "h", None)
        self.assertEqual(e["message"], "12345")
        self.assertIsInstance(e["attrs"]["o"], str)


if __name__ == "__main__":
    unittest.main()
