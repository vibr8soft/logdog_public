package logdog

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestMain(m *testing.M) {
	setStderr(io.Discard)
	os.Exit(m.Run())
}

func setStderr(w io.Writer) io.Writer {
	stderrMu.Lock()
	defer stderrMu.Unlock()
	old := stderr
	stderr = w
	return old
}

type syncBuf struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuf) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuf) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

func captureStderr(t *testing.T) *syncBuf {
	t.Helper()
	buf := &syncBuf{}
	old := setStderr(buf)
	t.Cleanup(func() { setStderr(old) })
	return buf
}

type recorded struct {
	method string
	header http.Header
	body   []map[string]any
	rawLen int
}

type resp struct {
	status int
	header map[string]string
	body   string
}

type fake struct {
	*httptest.Server
	mu     sync.Mutex
	reqs   []recorded
	script []resp
	hang   time.Duration
}

func (f *fake) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	rec := recorded{method: r.Method, header: r.Header.Clone()}
	if r.Method == http.MethodPost {
		var rd io.Reader = r.Body
		if r.Header.Get("Content-Encoding") == "gzip" {
			zr, err := gzip.NewReader(r.Body)
			if err != nil {
				http.Error(w, "bad gzip", 400)
				return
			}
			rd = zr
		}
		raw, _ := io.ReadAll(rd)
		rec.rawLen = len(raw)
		json.Unmarshal(raw, &rec.body)
	}
	f.mu.Lock()
	f.reqs = append(f.reqs, rec)
	var a *resp
	if len(f.script) > 0 {
		a = &f.script[0]
		f.script = f.script[1:]
	}
	hang := f.hang
	f.mu.Unlock()
	if hang > 0 {
		select {
		case <-r.Context().Done():
			return
		case <-time.After(hang):
		}
	}
	if a == nil {
		w.WriteHeader(202)
		w.Write([]byte(`{"accepted":` + strconv.Itoa(len(rec.body)) + `}`))
		return
	}
	for k, v := range a.header {
		w.Header().Set(k, v)
	}
	w.WriteHeader(a.status)
	w.Write([]byte(a.body))
}

func (f *fake) requests() []recorded {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]recorded(nil), f.reqs...)
}

func (f *fake) messages() []string {
	var out []string
	for _, r := range f.requests() {
		for _, e := range r.body {
			out = append(out, e["message"].(string))
		}
	}
	return out
}

func fakeServer(t *testing.T, script ...resp) *fake {
	t.Helper()
	f := &fake{script: script}
	f.Server = httptest.NewServer(f)
	t.Cleanup(f.Close)
	return f
}

func live(t *testing.T, url string, next slog.Handler, o Options) (*slog.Logger, *Handler) {
	t.Helper()
	o.URL, o.Key = url, "ld_test"
	if o.FlushInterval == 0 {
		o.FlushInterval = 50 * time.Millisecond
	}
	if o.BackoffStart == 0 {
		o.BackoffStart = 50 * time.Millisecond
	}
	if o.BackoffCap == 0 {
		o.BackoffCap = 200 * time.Millisecond
	}
	h := NewWithOptions(next, o)
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
		defer cancel()
		h.Close(ctx)
	})
	return slog.New(h), h
}

func flush(t *testing.T, h *Handler, d time.Duration) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), d)
	defer cancel()
	if err := h.Flush(ctx); err != nil {
		t.Fatalf("flush: %v", err)
	}
}

func eq(t *testing.T, got, want any) {
	t.Helper()
	g, _ := json.Marshal(got)
	w, _ := json.Marshal(want)
	if string(g) != string(w) {
		t.Fatalf("got %s want %s", g, w)
	}
}

func TestEndpoint(t *testing.T) {
	for _, u := range []string{"http://h", "http://h/", "http://h//", "http://h/v1/logs", "http://h/v1/logs/"} {
		if got := endpoint(u); got != "http://h/v1/logs" {
			t.Errorf("%s → %s", u, got)
		}
	}
}

func TestPostsGzipBatchWithHeaders(t *testing.T) {
	f := fakeServer(t)
	lg, h := live(t, f.URL, nil, Options{})
	lg.Info("a")
	lg.Info("b")
	flush(t, h, 2*time.Second)
	r := f.requests()[0]
	eq(t, []string{r.header.Get("Authorization"), r.header.Get("Content-Encoding"), r.header.Get("Content-Type"), r.header.Get("User-Agent")},
		[]string{"Bearer ld_test", "gzip", "application/json", "logdog-go/0.1"})
	eq(t, f.messages(), []string{"a", "b"})
}

func TestSendsAfterIntervalWithoutFlush(t *testing.T) {
	f := fakeServer(t)
	lg, _ := live(t, f.URL, nil, Options{})
	lg.Info("a")
	time.Sleep(500 * time.Millisecond)
	eq(t, f.messages(), []string{"a"})
}

func TestBatchSizeAndBodyCap(t *testing.T) {
	f := fakeServer(t)
	lg, h := live(t, f.URL, nil, Options{BatchSize: 3, MaxBody: 10_000})
	for i := 0; i < 7; i++ {
		lg.Info(strings.Repeat("x", 3000))
	}
	flush(t, h, 2*time.Second)
	total := 0
	for _, r := range f.requests() {
		if len(r.body) > 3 || r.rawLen > 10_000 {
			t.Fatalf("batch of %d entries, %d bytes", len(r.body), r.rawLen)
		}
		total += len(r.body)
	}
	eq(t, total, 7)
}

func TestRetryAfterThenSameBatch(t *testing.T) {
	f := fakeServer(t, resp{429, map[string]string{"Retry-After": "1"}, `{"error":"rate_limited"}`})
	lg, h := live(t, f.URL, nil, Options{BackoffCap: 30 * time.Second})
	lg.Info("a")
	start := time.Now()
	flush(t, h, 5*time.Second)
	if time.Since(start) < time.Second {
		t.Fatal("did not wait Retry-After")
	}
	rs := f.requests()
	eq(t, rs[0].body, rs[1].body)
}

func TestQuotaExceededWaitsPastCap(t *testing.T) {
	f := fakeServer(t, resp{429, map[string]string{"Retry-After": "1"}, `{"error":"quota_exceeded"}`})
	lg, h := live(t, f.URL, nil, Options{BackoffCap: 100 * time.Millisecond})
	lg.Info("a")
	start := time.Now()
	flush(t, h, 5*time.Second)
	if time.Since(start) < time.Second {
		t.Fatal("quota wait was shortened to the cap")
	}
}

func TestRateLimitedRetryAfterIsCapped(t *testing.T) {
	f := fakeServer(t, resp{429, map[string]string{"Retry-After": "60"}, `{"error":"rate_limited"}`})
	lg, h := live(t, f.URL, nil, Options{BackoffCap: 100 * time.Millisecond})
	lg.Info("a")
	flush(t, h, 3*time.Second)
}

func TestNoRetryAfterBacksOff(t *testing.T) {
	f := fakeServer(t, resp{503, nil, `{"error":"backpressure"}`},
		resp{503, map[string]string{"Retry-After": "soon"}, `{"error":"backpressure"}`})
	lg, h := live(t, f.URL, nil, Options{})
	lg.Info("a")
	flush(t, h, 3*time.Second)
	eq(t, len(f.requests()), 3)
}

func TestNetworkErrorRetries(t *testing.T) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := l.Addr().String()
	l.Close()
	lg, h := live(t, "http://"+addr, nil, Options{Timeout: 500 * time.Millisecond})
	lg.Info("a")
	time.Sleep(300 * time.Millisecond)
	l2, err := net.Listen("tcp", addr)
	if err != nil {
		t.Skipf("port taken: %v", err)
	}
	f := &fake{}
	f.Server = httptest.NewUnstartedServer(f)
	f.Server.Listener.Close()
	f.Server.Listener = l2
	f.Server.Start()
	defer f.Close()
	flush(t, h, 3*time.Second)
	eq(t, f.messages(), []string{"a"})
}

func Test400IndexDropsEntryResendsOnce(t *testing.T) {
	f := fakeServer(t, resp{400, nil, `{"error":"message_too_long","index":1}`})
	lg, h := live(t, f.URL, nil, Options{})
	lg.Info("a")
	lg.Info("b")
	lg.Info("c")
	flush(t, h, 2*time.Second)
	rs := f.requests()
	eq(t, []any{rs[1].body[0]["message"], rs[1].body[1]["message"], len(rs[1].body)}, []any{"a", "c", 2})
	eq(t, h.Dropped(), 1)
}

func TestSecond400DropsBatch(t *testing.T) {
	f := fakeServer(t, resp{400, nil, `{"error":"x","index":0}`}, resp{400, nil, `{"error":"x","index":0}`})
	lg, h := live(t, f.URL, nil, Options{})
	lg.Info("a")
	lg.Info("b")
	lg.Info("c")
	flush(t, h, 2*time.Second)
	eq(t, len(f.requests()), 2)
	eq(t, h.Dropped(), 3)
}

func Test400WithoutIndexDropsBatch(t *testing.T) {
	f := fakeServer(t, resp{400, nil, `{"error":"invalid_json"}`})
	lg, h := live(t, f.URL, nil, Options{})
	lg.Info("a")
	flush(t, h, 2*time.Second)
	eq(t, len(f.requests()), 1)
	eq(t, h.Dropped(), 1)
}

func Test401StopsForGood(t *testing.T) {
	f := fakeServer(t, resp{401, nil, `{"error":"invalid_api_key"}`})
	errOut := captureStderr(t)
	lg, h := live(t, f.URL, nil, Options{})
	lg.Info("a")
	flush(t, h, 2*time.Second)
	lg.Info("b")
	flush(t, h, time.Second)
	eq(t, len(f.requests()), 1)
	if !strings.Contains(errOut.String(), "401") || strings.Contains(errOut.String(), "ld_test") {
		t.Fatalf("stderr: %q", errOut.String())
	}
}

func Test500AndOther4xxDropWithoutRetry(t *testing.T) {
	for _, code := range []int{500, 413, 415} {
		f := fakeServer(t, resp{code, nil, `{"error":"x"}`})
		lg, h := live(t, f.URL, nil, Options{})
		lg.Info("a")
		flush(t, h, 2*time.Second)
		eq(t, len(f.requests()), 1)
	}
}

func TestRedirectIsNotFollowed(t *testing.T) {
	other := fakeServer(t)
	f := fakeServer(t, resp{302, map[string]string{"Location": other.URL + "/v1/logs"}, `{}`})
	lg, h := live(t, f.URL, nil, Options{})
	lg.Info("a")
	flush(t, h, 2*time.Second)
	eq(t, len(other.requests()), 0)
	eq(t, len(f.requests()), 1)
	eq(t, h.Dropped(), 1)
}

func TestOverflowDropsOldest(t *testing.T) {
	f := fakeServer(t, resp{503, map[string]string{"Retry-After": "1"}, `{}`})
	lg, h := live(t, f.URL, nil, Options{QueueSize: 5, BatchSize: 1})
	for i := 0; i < 20; i++ {
		lg.Info(strconv.Itoa(i))
	}
	flush(t, h, 5*time.Second)
	sent := f.messages()
	if sent[len(sent)-1] != "19" || h.Dropped() == 0 {
		t.Fatalf("sent %v dropped %d", sent, h.Dropped())
	}
}

func TestQueueByteCapDropsOldest(t *testing.T) {
	f := fakeServer(t)
	lg, h := live(t, f.URL, nil, Options{QueueBytes: 10_000, FlushInterval: 30 * time.Second})
	for i := 0; i < 10; i++ {
		lg.Info(strconv.Itoa(i) + strings.Repeat("x", 3000))
	}
	flush(t, h, 2*time.Second)
	var first []string
	for _, m := range f.messages() {
		first = append(first, m[:1])
	}
	eq(t, first, []string{"7", "8", "9"})
	eq(t, h.Dropped(), 7)
}

func TestEntryLargerThanMaxBodyIsDropped(t *testing.T) {
	f := fakeServer(t)
	lg, h := live(t, f.URL, nil, Options{MaxBody: 1000})
	lg.Info(strings.Repeat("x", 5000))
	lg.Info("ok")
	flush(t, h, 2*time.Second)
	eq(t, f.messages(), []string{"ok"})
	eq(t, h.Dropped(), 1)
}

func TestStderrRateLimitedAndHidesKey(t *testing.T) {
	f := fakeServer(t, resp{500, nil, `{}`}, resp{500, nil, `{}`}, resp{500, nil, `{}`}, resp{500, nil, `{}`}, resp{500, nil, `{}`})
	errOut := captureStderr(t)
	lg, h := live(t, f.URL, nil, Options{BatchSize: 1})
	for i := 0; i < 5; i++ {
		lg.Info("a")
	}
	flush(t, h, 3*time.Second)
	out := errOut.String()
	if strings.Count(out, "logdog: ") != 1 || !strings.Contains(out, "500") || strings.Contains(out, "ld_test") {
		t.Fatalf("stderr: %q", out)
	}
}

func TestCloseFlushesAndIsIdempotent(t *testing.T) {
	f := fakeServer(t)
	var buf bytes.Buffer
	lg, h := live(t, f.URL, slog.NewTextHandler(&buf, nil), Options{FlushInterval: 30 * time.Second})
	lg.Info("before")
	if err := h.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	eq(t, f.messages(), []string{"before"})
	if err := h.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	lg.Info("after")
	h.Send("info", "direct after", nil)
	time.Sleep(100 * time.Millisecond)
	eq(t, f.messages(), []string{"before"})
	if !strings.Contains(buf.String(), "after") {
		t.Fatal("next should still get records after Close")
	}
	if err := h.Flush(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestCloseHonoursContext(t *testing.T) {
	lg, h := live(t, "http://127.0.0.1:9", nil, Options{})
	lg.Info("a")
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	start := time.Now()
	err := h.Close(ctx)
	if !errors.Is(err, context.DeadlineExceeded) || time.Since(start) > time.Second {
		t.Fatalf("err %v after %v", err, time.Since(start))
	}
	eq(t, h.Dropped(), 1)
}

func TestConcurrentLogging(t *testing.T) {
	f := fakeServer(t)
	lg, h := live(t, f.URL, nil, Options{})
	var wg sync.WaitGroup
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < 200; i++ {
				lg.With("g", 1).Info("m", "i", i)
			}
		}()
	}
	wg.Wait()
	flush(t, h, 5*time.Second)
	eq(t, len(f.messages()), 1600)
}

func TestHandleNeverBlocks(t *testing.T) {
	f := fakeServer(t)
	f.hang = 2 * time.Second
	lg, _ := live(t, f.URL, nil, Options{FlushInterval: time.Millisecond})
	start := time.Now()
	for i := 0; i < 1000; i++ {
		lg.Info("m")
	}
	if d := time.Since(start); d > 500*time.Millisecond {
		t.Fatalf("1000 calls took %v", d)
	}
}

// -- direct calls ---------------------------------------------------------------

func TestSendDirect(t *testing.T) {
	f := fakeServer(t)
	_, h := live(t, f.URL, nil, Options{Env: "prod"})
	h.Send("error", "payment failed", map[string]any{"order_id": 8812, "trace_id": "4f1c9a"})
	flush(t, h, 2*time.Second)
	e := f.requests()[0].body[0]
	host, _ := os.Hostname()
	eq(t, []any{e["level"], e["message"], e["trace_id"], e["env"], e["host"]},
		[]any{"error", "payment failed", "4f1c9a", "prod", host})
	eq(t, e["attrs"], map[string]any{"order_id": 8812})
	if _, err := time.Parse(time.RFC3339Nano, e["ts"].(string)); err != nil {
		t.Fatal(err)
	}
}

func TestSendDoesNotReachNext(t *testing.T) {
	f := fakeServer(t)
	var buf bytes.Buffer
	_, h := live(t, f.URL, slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelError}), Options{})
	h.Send("debug", "quiet", nil)
	flush(t, h, 2*time.Second)
	eq(t, buf.Len(), 0)
	eq(t, f.messages(), []string{"quiet"})
}

func TestSendIgnoresHandlerGroupsAndAttrs(t *testing.T) {
	f := fakeServer(t)
	lg, h := live(t, f.URL, nil, Options{})
	derived := lg.With("svc", "api").WithGroup("g").Handler().(*Handler)
	derived.Send("info", "x", map[string]any{"a": 1})
	flush(t, h, 2*time.Second)
	eq(t, f.requests()[0].body[0]["attrs"], map[string]any{"a": 1})
}

func TestSendLevelsAndAliases(t *testing.T) {
	cases := map[string]string{"trace": "trace", "WARN": "warn", "warning": "warn", "critical": "fatal", " Error ": "error", "nonsense": "info", "": "info"}
	for in, want := range cases {
		if got := directLevel(in); got != want {
			t.Errorf("%q → %s, want %s", in, got, want)
		}
	}
}

func TestSendLimitsAndOddValues(t *testing.T) {
	f := fakeServer(t)
	_, h := live(t, f.URL, nil, Options{})
	in := map[string]any{"big": strings.Repeat("a", 70000)}
	h.Send("info", "large", in)
	h.Send("info", "odd", map[string]any{"ch": make(chan int), "err": errors.New("boom"), "bad\xff": "\xff", "p": panicky{}})
	h.Send("info", "nil", nil)
	flush(t, h, 2*time.Second)
	var all []map[string]any
	for _, r := range f.requests() {
		all = append(all, r.body...)
	}
	eq(t, len(all), 3)
	eq(t, all[0]["attrs"], map[string]any{"_logdog_dropped": "attrs_too_large"})
	eq(t, len(in), 1) // the caller's map is not modified
	if _, has := all[2]["attrs"]; has {
		t.Fatal("nil attrs should send no attrs")
	}
	if all[1]["message"] != "odd" {
		t.Fatalf("odd entry: %v", all[1])
	}
}

func TestSendDoesNotModifyTheCallersMap(t *testing.T) {
	f := fakeServer(t)
	_, h := live(t, f.URL, nil, Options{})
	in := map[string]any{"trace_id": "t1", "a": 1}
	h.Send("info", "x", in)
	eq(t, in, map[string]any{"trace_id": "t1", "a": 1})
}

func TestSendWithoutKey(t *testing.T) {
	t.Setenv("LOGDOG_KEY", "")
	errOut := captureStderr(t)
	h := New(nil)
	h.Send("error", "x", map[string]any{"a": 1})
	if err := h.Flush(context.Background()); err != nil {
		t.Fatal(err)
	}
	eq(t, h.Dropped(), 0)
	eq(t, errOut.String(), "")
}

func TestCloseWithoutDeadlineIsBounded(t *testing.T) {
	old := closeWait
	closeWait = 200 * time.Millisecond
	defer func() { closeWait = old }()
	lg, h := live(t, "http://127.0.0.1:9", nil, Options{})
	lg.Info("a")
	start := time.Now()
	err := h.Close(context.Background())
	if !errors.Is(err, context.DeadlineExceeded) || time.Since(start) > 2*time.Second {
		t.Fatalf("err %v after %v", err, time.Since(start))
	}
}
