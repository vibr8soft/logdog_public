---
name: logdog
description: Use when asked to add, integrate or send logs to Log Dog in a Python or Go project, or to improve what a project's Log Dog entries contain.
---

# Log Dog

## What this does

Adds one sender file to a Python or Go project. The file ships what the project already logs through standard `logging` (Python) or `log/slog` (Go) to Log Dog; nothing else about the project's logging changes.

## Steps

1. **Detect the stack.** `go.mod` means Go. `pyproject.toml`, `requirements.txt`, `setup.py` or a `*.py` entry point means Python. For anything else, say that Log Dog has senders for Python and Go only, show the `curl` example at the end of this file, and stop.
2. **Check the logger.** Standard `logging` or `log/slog`: continue. Another logger (loguru, structlog, zap, zerolog, logrus): say the sender needs the standard logger and ask before changing anything.
3. **Download the sender file.** If the file already exists, replace it (this is how updates happen) and say so.
   - Python, beside the app's entry module or inside its package:
     `curl -fsSL https://raw.githubusercontent.com/vibr8soft/logdog_public/main/python/logdog.py -o logdog.py`
   - Go: `mkdir -p internal/logdog && curl -fsSL https://raw.githubusercontent.com/vibr8soft/logdog_public/main/go/logdog.go -o internal/logdog/logdog.go`
4. **Add the setup lines** at the entry point, after the project's own logging setup.

   Python:

   ```python
   import logdog
   logdog.install()          # adds the handler to the root logger; no-op without a key
   ```

   Go (wrap the handler the project already builds; if it builds none, wrap `slog.NewTextHandler(os.Stderr, nil)`; import the file by the project's own module path):

   ```go
   h := logdog.New(slog.NewJSONHandler(os.Stderr, nil)) // no-op pass-through without a key
   defer h.Close(context.Background())
   slog.SetDefault(slog.New(h))
   ```

   In Go, `Close` must run before the program exits or the last entries are lost. A `defer` in `main` does not run on `os.Exit` or `log.Fatal`; call `h.Close` before those.
5. **Add the variables** `LOGDOG_KEY=`, `LOGDOG_URL=` and `LOGDOG_ENV=` with empty values to the project's env example file (`.env.example`, or the one it has). Never write a real key into any file and never print one.
6. **Test it.** If `LOGDOG_KEY` and `LOGDOG_URL` are set in the shell, run the app's quickest path that logs one line (or a three-line script) and report the result: no `logdog:` line on stderr means the entry was accepted. If they are not set, tell the user to set them. `LOGDOG_URL` is the Log Dog origin, for example `https://logs.example.com`. The key comes from the Log Dog dashboard: Apps, create an app, copy the key (shown once).
7. **Review the project's log calls** and propose fields worth adding, using the rules below. Propose; do not apply unasked.

## Entry format

| Field | Required | Limit |
|---|---|---|
| `level` | yes | `trace`, `debug`, `info`, `warn`, `error`, `fatal` |
| `message` | yes | 8,192 bytes |
| `ts` | no | RFC 3339; within the last 7 days, at most 1 hour ahead |
| `attrs` | no | a JSON object: 65,536 bytes, 200 top-level keys, 64 nesting levels |
| `host` | no | 128 bytes |
| `env` | no | 128 bytes |
| `trace_id` | no | 256 bytes |

No other field exists. The sender fills `ts`, `host` (the machine's hostname) and `env` (from `LOGDOG_ENV`) by itself and cuts anything over a limit.

## Direct calls

Use a direct call when the user wants an entry with its own fields that should not go through the logger:

```python
logdog.error("payment failed", order_id=8812, err="card_declined")
logdog.send("info", "checkout started", trace_id="4f1c9a", cart={"items": 3})
```

```go
h.Send("error", "payment failed", map[string]any{"order_id": 8812, "err": "card_declined"})
```

Python has `send(level, message, **attrs)` and `trace`, `debug`, `info`, `warn`, `error`, `fatal`; they work with or without `install()`. Go has `Send(level, message, attrs)` on the handler, so keep the handler reachable where it is needed.

A direct call goes to Log Dog only, not to the console, and is sent whatever the logger's level. Prefer the standard logger with extra fields unless the user asks for a direct call.

## How records map

- **Level.** Python: `DEBUG`, `INFO`, `WARNING`, `ERROR` map to `debug`, `info`, `warn`, `error`; `CRITICAL` becomes `fatal`; below `DEBUG` becomes `trace`. Go: `slog` Debug, Info, Warn, Error map directly; below Debug becomes `trace`; Error+4 and above becomes `fatal`.
- **Which records.** Whatever the project's logger lets through. Python: the logger's level decides. Go: the wrapped handler's level decides.
- **Fields.** Python `extra={...}` and Go slog attrs and groups (nested) become `attrs`.
- **Trace id.** A field named `trace_id` (top level) becomes the entry's `trace_id`.
- **Exceptions.** Python `logging.exception(...)` or `exc_info=True` adds `attrs.exception` with `type`, `message` and `traceback`. A Go `error` attr becomes its message string.
- **Odd values.** Anything that is not JSON becomes a string.

## Choosing good attrs

- Put ids as their own fields (`order_id`, `user_id`, `request_id`), never only inside the message: fields are searchable as `key:value`.
- Keep the message a constant sentence; put the variable parts in fields.
- Use numbers for numbers (`latency_ms: 812`, not `"812ms"`).
- Reuse the same field name for the same thing everywhere.
- Pass a request or trace id as `trace_id` so one request's lines can be found together.
- Never log secrets, tokens, passwords or full card or personal data.
- Stay well under the limits: 200 top-level keys and 64 KB per entry; past either, the fields are replaced by a `_logdog_dropped` note.

In Python, `extra` keys must not reuse a `logging` record name (`message`, `name`, `args`, `module`, `asctime` and the like): `logging` itself raises on those.

## What the sender does on its own

- Sends in the background in gzip batches, every 2 seconds or at 500 entries; logging never blocks.
- Retries when Log Dog is busy or unreachable, waiting as the server asks.
- Holds up to 10,000 entries (16 MB) in memory while it cannot send, dropping the oldest first.
- Reports its own problems on stderr only, at most one `logdog:` line a minute, never through the logger.
- Does nothing, silently, when `LOGDOG_KEY` or `LOGDOG_URL` is not set.
- Never follows a redirect: `LOGDOG_URL` must be the final origin (use `https://` if the site redirects to it).
- Python flushes at exit by itself; Go needs `h.Close(ctx)`.

## Not supported yet

JavaScript and browsers, nginx logs, and third-party loggers. For those, any HTTP client can post to the API:

```sh
curl -X POST "$LOGDOG_URL/v1/logs" \
  -H "Authorization: Bearer $LOGDOG_KEY" \
  -H "Content-Type: application/json" \
  -d '{"level": "error", "message": "payment failed", "attrs": {"order_id": 8812}}'
```
