import json
import logging
import sys
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


if __name__ == "__main__":
    unittest.main()
