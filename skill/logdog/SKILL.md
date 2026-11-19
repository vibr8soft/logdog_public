---
name: logdog
description: Use when asked to add, integrate or send logs to Log Dog in a Python or Go project, or to improve what a project's Log Dog entries contain.
---

# Log Dog

## What this does

Adds one sender file to a Python or Go project. The file ships what the project already logs through standard `logging` (Python) or `log/slog` (Go) to Log Dog; nothing else about the project's logging changes. The file uses the standard library only (Python 3.9+, Go 1.21+), so no dependency and no `go.mod` change is needed.

If the user only asks to improve what their entries contain and the sender is already in place, do step 7 only.

## Steps

1. **Detect the stack.** `go.mod` means Go. `pyproject.toml`, `requirements.txt`, `setup.py` or a `*.py` entry point means Python. For anything else, say that Log Dog has senders for Python and Go only, show the `curl` example at the end of this file, and stop.
2. **Check the logger.** Standard `logging` or `log/slog`: continue. Another logger (loguru, structlog, zap, zerolog, logrus): say the sender needs the standard logger and ask before changing anything.
3. **Download the sender file.** If the Log Dog sender file already exists, replace it (this is how updates happen) and say so; if a file of that name is something else, stop and ask. If the download fails, stop and tell the user; do not write the sender yourself.
   - Python, run in the folder of the app's entry module so that `import logdog` works (inside a package, import it as `from <package> import logdog` instead):
     `curl -fsSL https://raw.githubusercontent.com/vibr8soft/logdog_public/main/python/logdog.py -o logdog.py`
   - Go, from the module root (the folder with `go.mod`): `mkdir -p internal/logdog && curl -fsSL https://raw.githubusercontent.com/vibr8soft/logdog_public/main/go/logdog.go -o internal/logdog/logdog.go`
4. **Add the setup lines** at the entry point (every entry point, if the project has several programs), where the project sets up its logging.

   Python:

   ```python
   import logdog
   logdog.install()          # adds the handler to the root logger; no-op without a key and URL
   ```

   `install()` is safe to call more than once, and works before or after a server forks its workers. It ships what the root logger lets through: if the project never sets a level, Python's default is `WARNING`, so say that `info` lines will not be sent until the level is lowered.

   Go: change the line where the project builds its handler so that `logdog.New` wraps it; do not add a second `slog.SetDefault`. If the project builds no handler, wrap `slog.NewTextHandler(os.Stderr, nil)` and tell the user the console format changes slightly. The import is `"<module path from go.mod>/internal/logdog"`, the package is `logdog`, and `logdog.New` returns a `*logdog.Handler`.

   ```go
   h := logdog.New(slog.NewJSONHandler(os.Stderr, nil)) // no-op pass-through without a key
   defer h.Close(context.Background())
   slog.SetDefault(slog.New(h))
   ```

   In Go, `Close` must run before the program exits or the last entries are lost. A `defer` in `main` does not run on `os.Exit`, `log.Fatal` or a signal; call `h.Close` on those paths too. `Close` returns an error, is safe to call more than once, and stops waiting when its context ends: in a program that must exit promptly, pass a context with a timeout of about 5 seconds.
5. **Add the variables** `LOGDOG_KEY=`, `LOGDOG_URL=` and `LOGDOG_ENV=` with empty values to the project's env example file (`.env.example`, or the one it has; with none, list them in the README instead). `LOGDOG_ENV` is optional (`prod`, `staging`, …). The sender reads the process environment; it does not read `.env` files itself. Never write a real key into any file and never print one.
6. **Test it.** First run the project with the variables unset and confirm it builds and behaves as before. Then, if `LOGDOG_KEY` and `LOGDOG_URL` are set in the shell (check with `test -n "$LOGDOG_KEY"`, never by printing it), run the app's quickest path that logs one line (or `python3 -c "import logging, logdog; logdog.install(); logging.error('logdog test')"` beside the file) and report the result: a `logdog:` line on stderr means a problem; with none, ask the user to confirm the entry in the dashboard's Live tail. If they are not set, tell the user to set them. `LOGDOG_URL` is the Log Dog origin, for example `https://logs.example.com`. The key comes from the Log Dog dashboard: Apps, create an app, copy the key (shown once).
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

No other field exists. The sender fills `ts`, `host` (the machine's hostname) and `env` (from `LOGDOG_ENV`) by itself. It cuts `message`, `host`, `env` and `trace_id` to their limits; `attrs` over a limit are replaced whole by a `_logdog_dropped` note.

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

A direct call goes to Log Dog only, not to the console, and is sent whatever the logger's level. `warning` and `critical` are accepted as `warn` and `fatal`; any other unknown level is sent as `info`. Prefer the standard logger with extra fields unless the user asks for a direct call.

## How records map

- **Level.** Python: `DEBUG`, `INFO`, `WARNING`, `ERROR` map to `debug`, `info`, `warn`, `error`; `CRITICAL` becomes `fatal`; below `DEBUG` becomes `trace`. Go: `slog` Debug, Info, Warn, Error map directly; below Debug becomes `trace`; Error+4 and above becomes `fatal`.
- **Which records.** Whatever the project's logger lets through. Python: the logger's level decides. Go: the wrapped handler's level decides.
- **Fields.** Python `extra={...}` and Go slog attrs and groups (nested) become `attrs`.
- **Trace id.** A top-level field named `trace_id` becomes the entry's `trace_id` and is removed from `attrs`.
- **Fatal in Go.** `slog` has no fatal level: use `h.Send("fatal", …)` or `slog.Log(ctx, slog.LevelError+4, …)`.
- **Exceptions.** Python `logging.exception(...)` or `exc_info=True` adds `attrs.exception` with `type`, `message` and `traceback`. A Go `error` attr becomes its message string.
- **Odd values.** Anything that is not JSON becomes a string.

## Choosing good attrs

- Put ids as their own fields (`order_id`, `user_id`, `request_id`), never only inside the message: fields are searchable as `key:value`.
- Keep the message a constant sentence; put the variable parts in fields.
- Use numbers for numbers (`latency_ms: 812`, not `"812ms"`).
- Reuse the same field name for the same thing everywhere.
- Pass a request or trace id as `trace_id` so one request's lines can be found together.
- Never log secrets, tokens, passwords or full card or personal data.
- Stay well under the attrs limits: 200 top-level keys and 65,536 bytes; past either, all the fields are replaced by a `_logdog_dropped` note.
- Fields are not part of the message, so a console format that prints only the message stops showing them. Tell the user when a proposal moves a value out of the message.

In Python, `extra` keys must not reuse a `logging` record name (`message`, `msg`, `name`, `args`, `module`, `filename`, `lineno`, `levelname`, `created`, `process`, `thread`, `asctime` and the other `LogRecord` attributes): `logging` itself raises on those.

## What the sender does on its own

- Sends in the background in gzip batches, every 2 seconds or at 500 entries; logging never blocks.
- Retries when Log Dog is busy or unreachable, waiting as the server asks.
- Holds up to 10,000 entries (16 MB) in memory while it cannot send, dropping the oldest first.
- Reports its own problems on stderr only, at most one `logdog:` line a minute, never through the logger.
- Does nothing, silently, when `LOGDOG_KEY` or `LOGDOG_URL` is not set.
- Never follows a redirect: `LOGDOG_URL` must be the final origin (use `https://` if the site redirects to it).
- Python flushes at exit by itself, waiting at most 5 seconds; Go needs `h.Close(ctx)`.

## Not supported yet

JavaScript and browsers, nginx logs, and third-party loggers. For those, any HTTP client can post to the API:

```sh
curl -X POST "$LOGDOG_URL/v1/logs" \
  -H "Authorization: Bearer $LOGDOG_KEY" \
  -H "Content-Type: application/json" \
  -d '{"level": "error", "message": "payment failed", "attrs": {"order_id": 8812}}'
```
