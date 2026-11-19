package logdog

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"os"
	"strings"
	"sync"
	"testing"
	"time"
	"unicode/utf8"
)

type capture struct {
	mu  sync.Mutex
	got []entry
}

func (c *capture) enqueue(e entry) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.got = append(c.got, e)
}

func (c *capture) entries() []entry {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]entry(nil), c.got...)
}

func (c *capture) one(t *testing.T) entry {
	t.Helper()
	got := c.entries()
	if len(got) != 1 {
		t.Fatalf("want 1 entry, got %d", len(got))
	}
	return got[0]
}

// newTest builds a handler whose entries land in a capture instead of the network.
func newTest(t *testing.T, next slog.Handler) (*slog.Logger, *capture, *Handler) {
	t.Helper()
	h := NewWithOptions(next, Options{URL: "http://x.invalid", Key: "ld_test", Env: "prod"})
	t.Cleanup(func() { h.Close(context.Background()) })
	c := &capture{}
	h.c.sink = c
	return slog.New(h), c, h
}

func attrsJSON(t *testing.T, e entry) string {
	t.Helper()
	if e.Attrs == nil {
		return ""
	}
	b, err := json.Marshal(e.Attrs)
	if err != nil {
		t.Fatalf("attrs do not marshal: %v", err)
	}
	return string(b)
}

func wantAttrs(t *testing.T, e entry, want string) {
	t.Helper()
	if got := attrsJSON(t, e); got != want {
		t.Fatalf("attrs:\n got %s\nwant %s", got, want)
	}
}

func TestBasic(t *testing.T) {
	lg, c, _ := newTest(t, nil)
	lg.Error("payment failed", "order_id", 8812, slog.Group("customer", "id", "c_19"))
	e := c.one(t)
	host, _ := os.Hostname()
	if e.Level != "error" || e.Message != "payment failed" || e.Env != "prod" || e.Host != host {
		t.Fatalf("entry: %+v", e)
	}
	wantAttrs(t, e, `{"customer":{"id":"c_19"},"order_id":8812}`)
	ts, err := time.Parse(time.RFC3339Nano, e.TS)
	if err != nil || ts.Location() != time.UTC || time.Since(ts) > time.Minute {
		t.Fatalf("ts %q: %v", e.TS, err)
	}
	raw, _ := json.Marshal(e)
	var keys map[string]json.RawMessage
	json.Unmarshal(raw, &keys)
	for k := range keys {
		switch k {
		case "level", "message", "ts", "attrs", "host", "env", "trace_id":
		default:
			t.Fatalf("unknown field %q in %s", k, raw)
		}
	}
}

func TestLevels(t *testing.T) {
	cases := []struct {
		l    slog.Level
		want string
	}{{-8, "trace"}, {-4, "debug"}, {0, "info"}, {2, "info"}, {4, "warn"}, {8, "error"}, {11, "error"}, {12, "fatal"}, {16, "fatal"}}
	for _, tc := range cases {
		if got := levelName(tc.l); got != tc.want {
			t.Errorf("level %d: got %s want %s", tc.l, got, tc.want)
		}
	}
}

func TestWithAttrsAndGroups(t *testing.T) {
	lg, c, _ := newTest(t, nil)
	lg.With("svc", "api").WithGroup("req").With("id", 7).Info("x", "ms", 12)
	wantAttrs(t, c.one(t), `{"req":{"id":7,"ms":12},"svc":"api"}`)
}

func TestDerivedHandlersDoNotShareState(t *testing.T) {
	lg, c, _ := newTest(t, nil)
	base := lg.With("a", 1)
	b1 := base.With("b", 1)
	b2 := base.With("c", 2)
	b1.Info("one")
	b2.Info("two")
	got := c.entries()
	if attrsJSON(t, got[0]) != `{"a":1,"b":1}` || attrsJSON(t, got[1]) != `{"a":1,"c":2}` {
		t.Fatalf("got %s and %s", attrsJSON(t, got[0]), attrsJSON(t, got[1]))
	}
}

func TestEmptyGroupAndEmptyAttrOmitted(t *testing.T) {
	lg, c, _ := newTest(t, nil)
	lg.WithGroup("g").Info("x", slog.Attr{}, slog.Group("empty"))
	e := c.one(t)
	raw, _ := json.Marshal(e)
	if strings.Contains(string(raw), "attrs") {
		t.Fatalf("attrs should be absent: %s", raw)
	}
}

func TestInlineGroup(t *testing.T) {
	lg, c, _ := newTest(t, nil)
	lg.Info("x", slog.Group("", "a", 1, "b", 2))
	wantAttrs(t, c.one(t), `{"a":1,"b":2}`)
}

func TestTraceIDMovesOut(t *testing.T) {
	lg, c, _ := newTest(t, nil)
	lg.Info("x", "trace_id", "4f1c9a", "a", 1, slog.Group("g", "trace_id", "inner"))
	e := c.one(t)
	if e.TraceID != "4f1c9a" {
		t.Fatalf("trace_id %q", e.TraceID)
	}
	wantAttrs(t, e, `{"a":1,"g":{"trace_id":"inner"}}`)
}

type valuer struct{}

func (valuer) LogValue() slog.Value { return slog.StringValue("resolved") }

type panicky struct{}

func (panicky) Error() string { panic("no") }

func TestValueKinds(t *testing.T) {
	lg, c, _ := newTest(t, nil)
	lg.Info("x",
		"err", errors.New("boom"),
		"t", time.Date(2026, 11, 5, 12, 0, 0, 0, time.UTC),
		"d", 1500*time.Millisecond,
		"lv", valuer{},
		"ch", make(chan int),
		"st", struct{ A int }{1},
		"nan", math.NaN(),
		"nil", nil,
		"f", 1.5,
		"b", true,
	)
	e := c.one(t)
	a := e.Attrs
	if a["err"] != "boom" || a["t"] != "2026-11-05T12:00:00Z" || a["lv"] != "resolved" || a["nan"] != "NaN" {
		t.Fatalf("attrs: %v", a)
	}
	if _, ok := a["ch"].(string); !ok {
		t.Fatalf("chan should be a string: %T", a["ch"])
	}
	raw, err := json.Marshal(e)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{`"d":1500000000`, `"st":{"A":1}`, `"nil":null`, `"f":1.5`, `"b":true`} {
		if !strings.Contains(string(raw), want) {
			t.Errorf("missing %s in %s", want, raw)
		}
	}
}

func TestPanickingValueStillShipsTheMessage(t *testing.T) {
	lg, c, _ := newTest(t, nil)
	lg.Warn("still here", "err", panicky{})
	e := c.one(t)
	if e.Level != "warn" || e.Message != "still here" {
		t.Fatalf("entry: %+v", e)
	}
}

func TestMessageCutOnRuneBoundary(t *testing.T) {
	lg, c, _ := newTest(t, nil)
	lg.Info(strings.Repeat("é", 5000))
	m := c.one(t).Message
	if len(m) > 8192 || len(m) < 8000 || !utf8.ValidString(m) {
		t.Fatalf("len %d valid %v", len(m), utf8.ValidString(m))
	}
}

func TestInvalidUTF8Cleaned(t *testing.T) {
	lg, c, _ := newTest(t, nil)
	lg.Info("a\xffb", "k", "\xff", "bad\xffkey", 1)
	e := c.one(t)
	raw, err := json.Marshal(e)
	if err != nil || !utf8.Valid(raw) || !utf8.ValidString(e.Message) {
		t.Fatalf("not clean: %q %v", raw, err)
	}
	if strings.Contains(e.Message, "\xff") {
		t.Fatalf("message kept the bad byte")
	}
}

func TestAttrsLimits(t *testing.T) {
	lg, c, _ := newTest(t, nil)
	lg.Info("large", "big", strings.Repeat("a", 70000))
	many := make([]any, 0, 402)
	for i := 0; i < 201; i++ {
		many = append(many, fmt.Sprintf("k%d", i), i)
	}
	lg.Info("many", many...)
	deep := slog.Int("x", 1)
	for i := 0; i < 70; i++ {
		deep = slog.Group("n", deep)
	}
	lg.Info("deep", deep)
	got := c.entries()
	wantAttrs(t, got[0], `{"_logdog_dropped":"attrs_too_large"}`)
	wantAttrs(t, got[1], `{"_logdog_dropped":"attrs_too_many_keys"}`)
	wantAttrs(t, got[2], `{"_logdog_dropped":"attrs_too_deep"}`)
}

func TestHostEnvTraceCut(t *testing.T) {
	h := NewWithOptions(nil, Options{URL: "http://x.invalid", Key: "ld_test", Env: strings.Repeat("e", 200)})
	defer h.Close(context.Background())
	c := &capture{}
	h.c.sink = c
	slog.New(h).Info("x", "trace_id", strings.Repeat("t", 300))
	e := c.one(t)
	if len(e.Env) != 128 || len(e.TraceID) != 256 {
		t.Fatalf("env %d trace %d", len(e.Env), len(e.TraceID))
	}
}

func TestZeroTimeOmitsTS(t *testing.T) {
	_, c, h := newTest(t, nil)
	h.Handle(context.Background(), slog.Record{Message: "x"})
	raw, _ := json.Marshal(c.one(t))
	if strings.Contains(string(raw), `"ts"`) {
		t.Fatalf("ts should be absent: %s", raw)
	}
}

func TestNextStillReceivesRecords(t *testing.T) {
	var buf bytes.Buffer
	lg, c, _ := newTest(t, slog.NewJSONHandler(&buf, nil))
	lg.With("svc", "api").WithGroup("req").Info("x", "a", 1)
	if !strings.Contains(buf.String(), `"svc":"api"`) || !strings.Contains(buf.String(), `"req":{"a":1}`) {
		t.Fatalf("next got %s", buf.String())
	}
	wantAttrs(t, c.one(t), `{"req":{"a":1},"svc":"api"}`)
}

type failing struct{ slog.Handler }

func (failing) Handle(context.Context, slog.Record) error { return errors.New("next failed") }

func TestNextErrorIsReturnedAndEntryStillShips(t *testing.T) {
	_, c, h := newTest(t, failing{slog.NewTextHandler(&bytes.Buffer{}, nil)})
	err := h.Handle(context.Background(), slog.NewRecord(time.Now(), slog.LevelInfo, "x", 0))
	if err == nil || err.Error() != "next failed" {
		t.Fatalf("err %v", err)
	}
	c.one(t)
}

func TestEnabled(t *testing.T) {
	ctx := context.Background()
	var buf bytes.Buffer
	lg, c, h := newTest(t, slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelWarn}))
	if h.Enabled(ctx, slog.LevelInfo) || !h.Enabled(ctx, slog.LevelWarn) {
		t.Fatal("Enabled should follow next")
	}
	lg.Info("skipped")
	if len(c.entries()) != 0 {
		t.Fatal("info should not ship when next is at warn")
	}
	_, _, alone := newTest(t, nil)
	if !alone.Enabled(ctx, slog.LevelInfo) || alone.Enabled(ctx, slog.LevelDebug) {
		t.Fatal("without next: info and above")
	}
}

func TestNoKeyIsPassThrough(t *testing.T) {
	t.Setenv("LOGDOG_KEY", "")
	t.Setenv("LOGDOG_URL", "http://x.invalid")
	var buf, errOut bytes.Buffer
	old := stderr
	stderr = &errOut
	defer func() { stderr = old }()
	h := New(slog.NewTextHandler(&buf, nil))
	slog.New(h).Info("hello")
	if !strings.Contains(buf.String(), "hello") {
		t.Fatal("next did not get the record")
	}
	if h.c.sink != nil {
		t.Fatal("sink should be nil without a key")
	}
	if errOut.Len() != 0 {
		t.Fatalf("stderr: %s", errOut.String())
	}
	alone := New(nil)
	for _, l := range []slog.Level{slog.LevelDebug, slog.LevelInfo, slog.LevelError} {
		if alone.Enabled(context.Background(), l) {
			t.Fatal("no key and no next: nothing is enabled")
		}
	}
	if err := h.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestEnvVarsAreRead(t *testing.T) {
	t.Setenv("LOGDOG_KEY", "ld_env")
	t.Setenv("LOGDOG_URL", "http://x.invalid")
	t.Setenv("LOGDOG_ENV", "staging")
	h := New(nil)
	defer h.Close(context.Background())
	c := &capture{}
	h.c.sink = c
	slog.New(h).Info("x")
	if c.one(t).Env != "staging" {
		t.Fatal("LOGDOG_ENV not used")
	}
}
