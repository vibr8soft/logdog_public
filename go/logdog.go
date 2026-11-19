package logdog

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"math"
	"os"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

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
var stderr io.Writer = os.Stderr

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
	sink sink // nil when there is no key or URL: nothing is shipped
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
		c.sink = nopSink{}
	}
	return &Handler{next: next, c: c}
}

type nopSink struct{}

func (nopSink) enqueue(entry) {}

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

// Close is completed by the sender; see below.
func (h *Handler) Close(ctx context.Context) error { return nil }

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
	raw, err := json.Marshal(x)
	if err != nil {
		return clean(fmt.Sprint(x))
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var out any
	if dec.Decode(&out) != nil {
		return clean(fmt.Sprint(x))
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
