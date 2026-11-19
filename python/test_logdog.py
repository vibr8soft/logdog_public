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

    def test_enqueue_never_blocks_while_server_is_slow(self):
        port = free_port()                      # nothing listens: every send fails
        s = logdog._Sender("http://127.0.0.1:%d" % port, "ld_test", flush_interval=0.01)
        self.addCleanup(s.close, 0.1)
        with contextlib.redirect_stderr(io.StringIO()):
            t = time.monotonic()
            for i in range(20000):
                s.enqueue(E(str(i)))
            self.assertLess(time.monotonic() - t, 2.0)


if __name__ == "__main__":
    unittest.main()
