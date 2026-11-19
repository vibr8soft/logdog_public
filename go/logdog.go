// Package logdog ships a program's log/slog output to Log Dog.
//
// It is one file with no dependencies: copy it into your project (for example
// internal/logdog/logdog.go). Source: https://github.com/vibr8soft/logdog_public
//
// Set LOGDOG_URL (your Log Dog origin) and LOGDOG_KEY (the app's API key), and
// optionally LOGDOG_ENV. Then wrap the handler you already use:
//
//	h := logdog.New(slog.NewJSONHandler(os.Stderr, nil))
//	defer h.Close(context.Background())
//	slog.SetDefault(slog.New(h))
//
// Every record still reaches the wrapped handler. Its attrs and groups become
// the entry's attrs; a top-level "trace_id" attr becomes the entry's trace_id.
// Without a key or URL the handler only passes records on, so development
// machines and tests need no setup.
//
// To send an entry with your own fields, bypassing the logger:
//
//	h.Send("error", "payment failed", map[string]any{"order_id": 8812})
//
// Entries are sent in the background in gzip batches. Logging never blocks and
// never fails because of Log Dog: when it cannot be reached, up to 10,000
// entries wait in memory and the oldest are dropped first. The sender reports
// its own problems on stderr, at most one line a minute.
package logdog

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	"unicode/utf8"
)

const userAgent = "logdog-go/0.1"

// closeWait bounds Close when its context has no deadline.
var closeWait = 5 * time.Second

const (
	maxMessage    = 8192
	maxAttrsBytes = 65536
	maxAttrsKeys  = 200
	maxAttrsDepth = 64
	maxHost       = 128
	maxEnv        = 128
	maxTraceID    = 256
)

// stderr is where the sender reports its own problems. Tests swap it.
var (
	stderr   io.Writer = os.Stderr
	stderrMu sync.Mutex
)

// Options configures a Handler. Zero values mean the defaults; empty URL, Key
// and Env mean the LOGDOG_URL, LOGDOG_KEY and LOGDOG_ENV environment variables.
type Options struct {
	URL string // the Log Dog origin, for example https://logs.example.com
	Key string // the app's API key (ld_...)
	Env string // fills the entry's env field

	FlushInterval time.Duration // default 2s
	BatchSize     int           // default 500, at most 1000
	QueueSize     int           // entries held in memory, default 10000
	QueueBytes    int           // bytes held in memory, default 16 MiB
	MaxBody       int           // uncompressed bytes per request, default 900000
	Timeout       time.Duration // per request, default 10s
	BackoffStart  time.Duration // default 1s
	BackoffCap    time.Duration // default 30s
}

type entry struct {
	Level   string         `json:"level"`
	Message string         `json:"message"`
	TS      string         `json:"ts,omitempty"`
	Attrs   map[string]any `json:"attrs,omitempty"`
	Host    string         `json:"host,omitempty"`
	Env     string         `json:"env,omitempty"`
	TraceID string         `json:"trace_id,omitempty"`
}

type sink interface{ enqueue(entry) }

// core is shared by a Handler and every handler derived from it.
type core struct {
	sink sink    // nil when there is no key or URL: nothing is shipped
	snd  *sender // the real sink, when there is one
	host string
	env  string
}

type groupedAttr struct {
	path []string
	attr slog.Attr
}

// Handler is a slog.Handler that ships records to Log Dog and passes them on
// to the handler it wraps.
type Handler struct {
	next   slog.Handler
	c      *core
	groups []string
	attrs  []groupedAttr
}

// New wraps next (which may be nil) and reads LOGDOG_URL, LOGDOG_KEY and
// LOGDOG_ENV. Without a key or URL it only passes records on to next.
func New(next slog.Handler) *Handler { return NewWithOptions(next, Options{}) }

// NewWithOptions is New with explicit settings.
func NewWithOptions(next slog.Handler, o Options) *Handler {
	if o.URL == "" {
		o.URL = os.Getenv("LOGDOG_URL")
	}
	if o.Key == "" {
		o.Key = os.Getenv("LOGDOG_KEY")
	}
	if o.Env == "" {
		o.Env = os.Getenv("LOGDOG_ENV")
	}
	o.URL, o.Key = strings.TrimSpace(o.URL), strings.TrimSpace(o.Key)
	host, _ := os.Hostname()
	c := &core{host: cut(clean(host), maxHost), env: cut(clean(o.Env), maxEnv)}
	if o.URL != "" && o.Key != "" {
		c.snd = newSender(o)
		c.sink = c.snd
	}
	return &Handler{next: next, c: c}
}

// Enabled follows the wrapped handler; with none, info and above are shipped.
func (h *Handler) Enabled(ctx context.Context, level slog.Level) bool {
	if h.next != nil {
		return h.next.Enabled(ctx, level)
	}
	return h.c.sink != nil && level >= slog.LevelInfo
}

// Handle passes the record to the wrapped handler and queues it for Log Dog.
// Shipping never returns an error and never blocks.
func (h *Handler) Handle(ctx context.Context, r slog.Record) error {
	var err error
	if h.next != nil {
		err = h.next.Handle(ctx, r)
	}
	if s := h.c.sink; s != nil {
		s.enqueue(h.safeEntry(r))
	}
	return err
}

func (h *Handler) WithAttrs(attrs []slog.Attr) slog.Handler {
	if len(attrs) == 0 {
		return h
	}
	d := h.derive()
	if d.next != nil {
		d.next = d.next.WithAttrs(attrs)
	}
	for _, a := range attrs {
		d.attrs = append(d.attrs, groupedAttr{path: d.groups, attr: a})
	}
	return d
}

func (h *Handler) WithGroup(name string) slog.Handler {
	if name == "" {
		return h
	}
	d := h.derive()
	if d.next != nil {
		d.next = d.next.WithGroup(name)
	}
	d.groups = append(d.groups, name)
	return d
}

func (h *Handler) derive() *Handler {
	return &Handler{
		next:   h.next,
		c:      h.c,
		groups: append([]string(nil), h.groups...),
		attrs:  append([]groupedAttr(nil), h.attrs...),
	}
}

// Send ships one entry straight to Log Dog with your own fields. It goes to
// Log Dog only (not to the wrapped handler), whatever the logger's level, and
// does nothing without a key. Levels: trace, debug, info, warn, error, fatal.
func (h *Handler) Send(level, message string, attrs map[string]any) {
	s := h.c.sink
	if s == nil {
		return
	}
	defer func() { recover() }()
	var root map[string]any
	if len(attrs) > 0 {
		root = make(map[string]any, len(attrs))
		for k, v := range attrs {
			root[clean(k)] = safePlain(v)
		}
	}
	s.enqueue(h.c.finish(directLevel(level), message, time.Now(), root))
}

// Flush waits until everything queued so far is sent or dropped, or ctx ends.
// While Log Dog is unreachable that can be a long time: pass a ctx with a deadline.
func (h *Handler) Flush(ctx context.Context) error {
	if h.c.snd == nil {
		return nil
	}
	return h.c.snd.flush(ctx)
}

// Close flushes, then stops the background sender. Call it before the program
// exits. Records logged afterwards still reach the wrapped handler. It waits
// until ctx ends, or 5 seconds when ctx has no deadline, so a program never
// hangs at exit because Log Dog is unreachable.
func (h *Handler) Close(ctx context.Context) error {
	if h.c.snd == nil {
		return nil
	}
	return h.c.snd.close(ctx)
}

// Dropped reports how many entries were lost (queue overflow, refused batches).
func (h *Handler) Dropped() int64 {
	if h.c.snd == nil {
		return 0
	}
	return h.c.snd.dropped.Load()
}

func directLevel(level string) string {
	switch l := strings.ToLower(strings.TrimSpace(level)); l {
	case "trace", "debug", "info", "warn", "error", "fatal":
		return l
	case "warning":
		return "warn"
	case "critical":
		return "fatal"
	}
	return "info"
}

// safePlain is plainAny that survives a panicking Error or MarshalJSON method.
func safePlain(v any) (out any) {
	defer func() {
		if recover() != nil {
			out = "<unprintable>"
		}
	}()
	return plainAny(v)
}

// -- mapping -------------------------------------------------------------------

func levelName(l slog.Level) string {
	switch {
	case l < slog.LevelDebug:
		return "trace"
	case l < slog.LevelInfo:
		return "debug"
	case l < slog.LevelWarn:
		return "info"
	case l < slog.LevelError:
		return "warn"
	case l < slog.LevelError+4:
		return "error"
	default:
		return "fatal"
	}
}

// safeEntry is buildEntry that survives a panicking value.
func (h *Handler) safeEntry(r slog.Record) (e entry) {
	defer func() {
		if recover() != nil {
			e = h.c.finish(levelName(r.Level), r.Message, r.Time, nil)
		}
	}()
	return h.buildEntry(r)
}

func (h *Handler) buildEntry(r slog.Record) entry {
	root := map[string]any{}
	for _, ga := range h.attrs {
		put(root, ga.path, ga.attr)
	}
	r.Attrs(func(a slog.Attr) bool {
		put(root, h.groups, a)
		return true
	})
	return h.c.finish(levelName(r.Level), r.Message, r.Time, root)
}

// finish turns a level, message and attrs map into an entry within the limits.
func (c *core) finish(level, message string, at time.Time, attrs map[string]any) entry {
	e := entry{Level: level, Message: cut(clean(message), maxMessage), Host: c.host, Env: c.env}
	if e.Message == "" {
		e.Message = "(empty)" // the server refuses an empty message
	}
	if !at.IsZero() {
		e.TS = at.UTC().Format(time.RFC3339Nano)
	}
	if v, ok := attrs["trace_id"]; ok {
		delete(attrs, "trace_id")
		if v != nil {
			e.TraceID = cut(clean(fmt.Sprint(v)), maxTraceID)
		}
	}
	e.Attrs = shapeAttrs(attrs)
	return e
}

func dropped(why string) map[string]any { return map[string]any{"_logdog_dropped": why} }

func shapeAttrs(attrs map[string]any) map[string]any {
	if len(attrs) == 0 {
		return nil
	}
	if len(attrs) > maxAttrsKeys {
		return dropped("attrs_too_many_keys")
	}
	if depth(attrs, maxAttrsDepth+1) > maxAttrsDepth {
		return dropped("attrs_too_deep")
	}
	raw, err := json.Marshal(attrs)
	if err != nil {
		return dropped("attrs_not_serializable")
	}
	if len(raw) > maxAttrsBytes {
		return dropped("attrs_too_large")
	}
	return attrs
}

// depth counts nesting levels (the value itself is level 1), stopping at stop.
func depth(v any, stop int) int {
	var children []any
	switch x := v.(type) {
	case map[string]any:
		for _, c := range x {
			children = append(children, c)
		}
	case []any:
		children = x
	default:
		return 0
	}
	if stop <= 1 {
		return 1
	}
	deepest := 0
	for _, c := range children {
		if d := depth(c, stop-1); d > deepest {
			deepest = d
		}
	}
	return deepest + 1
}

// put adds one slog attr to root under the given group path. Empty attrs and
// empty groups add nothing, and create no parent objects.
func put(root map[string]any, path []string, a slog.Attr) {
	a.Value = a.Value.Resolve()
	if a.Equal(slog.Attr{}) {
		return
	}
	if a.Value.Kind() == slog.KindGroup {
		members := a.Value.Group()
		if len(members) == 0 {
			return
		}
		if a.Key != "" {
			path = append(append([]string(nil), path...), a.Key)
		}
		for _, m := range members {
			put(root, path, m)
		}
		return
	}
	m := root
	for _, name := range path {
		name = clean(name)
		sub, ok := m[name].(map[string]any)
		if !ok {
			sub = map[string]any{}
			m[name] = sub
		}
		m = sub
	}
	m[clean(a.Key)] = plain(a.Value)
}

// plain turns a resolved slog value into something encoding/json writes safely.
func plain(v slog.Value) any {
	switch v.Kind() {
	case slog.KindString:
		return clean(v.String())
	case slog.KindInt64:
		return v.Int64()
	case slog.KindUint64:
		return v.Uint64()
	case slog.KindBool:
		return v.Bool()
	case slog.KindFloat64:
		f := v.Float64()
		if math.IsNaN(f) || math.IsInf(f, 0) {
			return strconv.FormatFloat(f, 'g', -1, 64)
		}
		return f
	case slog.KindDuration:
		return int64(v.Duration())
	case slog.KindTime:
		return v.Time().UTC().Format(time.RFC3339Nano)
	}
	return plainAny(v.Any())
}

func plainAny(x any) any {
	switch t := x.(type) {
	case nil:
		return nil
	case error:
		return clean(t.Error())
	case string:
		return clean(t)
	}
	// Never print the value itself on failure: fmt recurses without end on a
	// self-referencing map or slice, and that crash cannot be recovered.
	raw, err := json.Marshal(x)
	if err != nil {
		return fmt.Sprintf("<%T: not serializable>", x)
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var out any
	if dec.Decode(&out) != nil {
		return fmt.Sprintf("<%T: not serializable>", x)
	}
	return out
}

func clean(s string) string {
	if utf8.ValidString(s) {
		return s
	}
	return strings.ToValidUTF8(s, "�")
}

// cut shortens s to at most limit bytes, on a rune boundary.
func cut(s string, limit int) string {
	if len(s) <= limit {
		return s
	}
	i := limit
	for i > 0 && !utf8.RuneStart(s[i]) {
		i--
	}
	return s[:i]
}

// -- sender --------------------------------------------------------------------

func endpoint(url string) string {
	url = strings.TrimRight(strings.TrimSpace(url), "/")
	if strings.HasSuffix(url, "/v1/logs") {
		return url
	}
	return url + "/v1/logs"
}

// sender is an in-memory queue drained by one goroutine.
type sender struct {
	o      Options
	url    string
	client *http.Client

	mu       sync.Mutex
	queue    [][]byte
	queued   int // bytes in queue
	oldest   time.Time
	inflight bool
	flushNow bool
	closed   bool
	stopped  bool // a 401 turned sending off

	wake    chan struct{}
	done    chan struct{}
	exited  chan struct{}
	ctx     context.Context
	cancel  context.CancelFunc
	once    sync.Once
	dropped atomic.Int64

	warnMu   sync.Mutex
	lastWarn time.Time
}

func newSender(o Options) *sender {
	if o.FlushInterval <= 0 {
		o.FlushInterval = 2 * time.Second
	}
	if o.BatchSize <= 0 {
		o.BatchSize = 500
	}
	if o.BatchSize > 1000 {
		o.BatchSize = 1000
	}
	if o.QueueSize <= 0 {
		o.QueueSize = 10000
	}
	if o.QueueBytes <= 0 {
		o.QueueBytes = 16 << 20
	}
	if o.MaxBody <= 0 {
		o.MaxBody = 900_000
	}
	if o.Timeout <= 0 {
		o.Timeout = 10 * time.Second
	}
	if o.BackoffStart <= 0 {
		o.BackoffStart = time.Second
	}
	if o.BackoffCap <= 0 {
		o.BackoffCap = 30 * time.Second
	}
	s := &sender{
		o:   o,
		url: endpoint(o.URL),
		client: &http.Client{
			Timeout: o.Timeout,
			// Never follow a redirect: it would send the API key to another URL.
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
		wake:   make(chan struct{}, 1),
		done:   make(chan struct{}),
		exited: make(chan struct{}),
	}
	s.ctx, s.cancel = context.WithCancel(context.Background())
	go s.run()
	return s
}

func (s *sender) signal() {
	select {
	case s.wake <- struct{}{}:
	default:
	}
}

func (s *sender) enqueue(e entry) {
	raw, err := json.Marshal(e)
	if err != nil {
		s.dropped.Add(1)
		return
	}
	s.mu.Lock()
	if s.closed || s.stopped {
		s.mu.Unlock()
		return
	}
	if len(raw)+2 > s.o.MaxBody {
		s.mu.Unlock()
		s.dropped.Add(1)
		return
	}
	for len(s.queue) > 0 && (len(s.queue) >= s.o.QueueSize || s.queued+len(raw) > s.o.QueueBytes) {
		s.queued -= len(s.queue[0])
		s.queue[0] = nil
		s.queue = s.queue[1:]
		s.dropped.Add(1)
	}
	first := len(s.queue) == 0
	if first {
		s.oldest = time.Now()
	}
	s.queue = append(s.queue, raw)
	s.queued += len(raw)
	full := len(s.queue) >= s.o.BatchSize
	s.mu.Unlock()
	if first || full {
		s.signal()
	}
}

func (s *sender) flush(ctx context.Context) error {
	s.mu.Lock()
	s.flushNow = true
	s.mu.Unlock()
	s.signal()
	tick := time.NewTicker(10 * time.Millisecond)
	defer tick.Stop()
	for {
		s.mu.Lock()
		idle := len(s.queue) == 0 && !s.inflight
		s.mu.Unlock()
		if idle {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-s.exited:
			return nil
		case <-tick.C:
		}
	}
}

func (s *sender) close(ctx context.Context) error {
	var err error
	s.once.Do(func() {
		if _, bounded := ctx.Deadline(); !bounded {
			var cancel context.CancelFunc
			ctx, cancel = context.WithTimeout(ctx, closeWait)
			defer cancel()
		}
		err = s.flush(ctx)
		s.mu.Lock()
		s.closed = true
		s.dropped.Add(int64(len(s.queue)))
		s.queue, s.queued = nil, 0
		s.mu.Unlock()
		close(s.done)
		s.cancel()
		select {
		case <-s.exited:
		case <-time.After(time.Second):
		}
	})
	return err
}

func (s *sender) run() {
	defer close(s.exited)
	for {
		s.mu.Lock()
		if s.closed {
			s.mu.Unlock()
			return
		}
		wait := time.Duration(-1)
		ready := false
		if len(s.queue) > 0 {
			wait = s.o.FlushInterval - time.Since(s.oldest)
			ready = s.flushNow || wait <= 0 || len(s.queue) >= s.o.BatchSize
		} else {
			s.flushNow = false
		}
		if !ready {
			s.mu.Unlock()
			s.pause(wait)
			continue
		}
		batch := s.take()
		s.inflight = true
		s.mu.Unlock()

		s.deliver(batch)

		s.mu.Lock()
		s.inflight = false
		s.mu.Unlock()
	}
}

// pause waits for a wake-up, for close, or for d (forever when d is negative).
func (s *sender) pause(d time.Duration) {
	var timer <-chan time.Time
	if d >= 0 {
		t := time.NewTimer(d)
		defer t.Stop()
		timer = t.C
	}
	select {
	case <-s.wake:
	case <-s.done:
	case <-timer:
	}
}

// take removes the next batch from the queue. The caller holds s.mu.
func (s *sender) take() [][]byte {
	var batch [][]byte
	size := 2
	for len(s.queue) > 0 && len(batch) < s.o.BatchSize {
		n := len(s.queue[0]) + 1
		if len(batch) > 0 && size+n > s.o.MaxBody {
			break
		}
		batch = append(batch, s.queue[0])
		s.queued -= len(s.queue[0])
		s.queue[0] = nil
		s.queue = s.queue[1:]
		size += n
	}
	return batch
}

type answer struct {
	Error string `json:"error"`
	Index *int   `json:"index"`
}

func (s *sender) deliver(batch [][]byte) {
	backoff := s.o.BackoffStart
	resent := false
	for len(batch) > 0 {
		select {
		case <-s.done:
			s.dropped.Add(int64(len(batch)))
			return
		default:
		}
		status, retryAfter, ans := s.post(batch)
		switch {
		case status >= 200 && status < 300:
			return
		case status == 0 || status == 429 || status == 503:
			wait := backoff
			if retryAfter >= 0 {
				wait = time.Duration(retryAfter) * time.Second
				if wait < backoff {
					wait = backoff
				}
			}
			if ans.Error != "quota_exceeded" && wait > s.o.BackoffCap {
				wait = s.o.BackoffCap
			}
			if backoff *= 2; backoff > s.o.BackoffCap {
				backoff = s.o.BackoffCap
			}
			if status == 0 {
				s.warn("cannot reach Log Dog; retrying")
			} else {
				s.warn(fmt.Sprintf("refused with %d %s; retrying", status, ans.Error))
			}
			t := time.NewTimer(wait)
			select {
			case <-t.C:
			case <-s.done:
			}
			t.Stop()
		case status == 400 && !resent && ans.Index != nil && *ans.Index >= 0 && *ans.Index < len(batch):
			batch = append(batch[:*ans.Index], batch[*ans.Index+1:]...)
			s.dropped.Add(1)
			resent = true
			s.warn(fmt.Sprintf("entry refused with 400 %s; dropped", ans.Error))
		case status == 401:
			s.mu.Lock()
			s.stopped = true
			s.dropped.Add(int64(len(s.queue) + len(batch)))
			s.queue, s.queued = nil, 0
			s.mu.Unlock()
			s.warn(fmt.Sprintf("401 %s: the API key was refused; sending is off", ans.Error))
			return
		default:
			s.dropped.Add(int64(len(batch)))
			s.warn(fmt.Sprintf("batch refused with %d %s; dropped", status, ans.Error))
			return
		}
	}
}

// post sends one batch. status is 0 on a network error; retryAfter is -1 when
// the header is absent or not a whole number of seconds.
func (s *sender) post(batch [][]byte) (status, retryAfter int, ans answer) {
	retryAfter = -1
	var body bytes.Buffer
	zw := gzip.NewWriter(&body)
	zw.Write([]byte{'['})
	zw.Write(bytes.Join(batch, []byte{','}))
	zw.Write([]byte{']'})
	if zw.Close() != nil {
		return 0, -1, ans
	}
	req, err := http.NewRequestWithContext(s.ctx, http.MethodPost, s.url, &body)
	if err != nil {
		return 0, -1, ans
	}
	req.Header.Set("Authorization", "Bearer "+s.o.Key)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Content-Encoding", "gzip")
	req.Header.Set("User-Agent", userAgent)
	resp, err := s.client.Do(req)
	if err != nil {
		return 0, -1, ans
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	json.Unmarshal(raw, &ans)
	if n, err := strconv.Atoi(strings.TrimSpace(resp.Header.Get("Retry-After"))); err == nil && n >= 0 {
		retryAfter = n
	}
	return resp.StatusCode, retryAfter, ans
}

// warn reports the sender's own problems on stderr, at most once a minute.
func (s *sender) warn(text string) {
	s.warnMu.Lock()
	quiet := !s.lastWarn.IsZero() && time.Since(s.lastWarn) < time.Minute
	if !quiet {
		s.lastWarn = time.Now()
	}
	s.warnMu.Unlock()
	if quiet {
		return
	}
	stderrMu.Lock()
	defer stderrMu.Unlock()
	fmt.Fprintln(stderr, "logdog: "+strings.TrimSpace(text))
}
