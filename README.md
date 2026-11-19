# Log Dog senders

One file per stack that ships your program's logs to Log Dog. This is v1, before official packages: copy the file into your project. Each file uses the standard library only.

- **Python:** [`python/logdog.py`](python/logdog.py), a handler for the standard `logging` module (Python 3.9+).
- **Go:** [`go/logdog.go`](go/logdog.go), a handler for `log/slog` (Go 1.21+).
- **Claude Code skill:** [`skill/logdog`](skill/logdog/SKILL.md), which adds the right file to a project for you.

## Configuration

| Variable | Meaning |
|---|---|
| `LOGDOG_URL` | Your Log Dog origin, for example `https://logs.example.com` |
| `LOGDOG_KEY` | The app's API key (`ld_…`), from the dashboard |
| `LOGDOG_ENV` | Optional. Fills the entry's `env` field (`prod`, `staging`, …) |

Without `LOGDOG_URL` or `LOGDOG_KEY` the sender does nothing, silently, so development machines and tests need no setup.

## Python

```sh
curl -fsSL https://raw.githubusercontent.com/vibr8soft/logdog_public/main/python/logdog.py -o logdog.py
```

Once, where your program starts:

```python
import logdog
logdog.install()
```

Everything `logging` emits is now also sent to Log Dog. Your existing handlers are untouched. The logger's level decides what is sent: Python's default is `WARNING`, so set the level to `INFO` to send info lines. Extra fields become searchable attrs:

```python
logging.info("checkout started", extra={"order_id": 8812, "trace_id": "4f1c9a"})
```

To send an entry with your own fields, bypassing `logging`:

```python
logdog.error("payment failed", order_id=8812, err="card_declined")
```

## Go

```sh
mkdir -p internal/logdog
curl -fsSL https://raw.githubusercontent.com/vibr8soft/logdog_public/main/go/logdog.go -o internal/logdog/logdog.go
```

Wrap the handler you already use:

```go
h := logdog.New(slog.NewJSONHandler(os.Stderr, nil))
defer h.Close(context.Background())
slog.SetDefault(slog.New(h))

slog.Info("checkout started", "order_id", 8812, "trace_id", "4f1c9a")
```

Every record still reaches the wrapped handler. To send an entry with your own fields, bypassing the logger:

```go
h.Send("error", "payment failed", map[string]any{"order_id": 8812, "err": "card_declined"})
```

Call `h.Close` before the program exits, or the last entries are lost. It waits for the context you pass, or at most 5 seconds when the context has no deadline.

## Claude Code skill

```sh
git clone https://github.com/vibr8soft/logdog_public
mkdir -p ~/.claude/skills && cp -r logdog_public/skill/logdog ~/.claude/skills/
```

Then, in a project, ask Claude Code: "add Log Dog to this project".

## What the sender does

- Sends in the background in gzip batches, every 2 seconds or at 500 entries. Logging never blocks.
- Retries when Log Dog is busy or unreachable, waiting as the server asks.
- Holds up to 10,000 entries (16 MB) in memory while it cannot send, dropping the oldest first.
- Cuts anything over Log Dog's limits, so one oversized entry never refuses a batch.
- Reports its own problems on stderr only, at most one `logdog:` line a minute.
- Never follows a redirect, so a redirect cannot forward the API key to another address.

A direct call (`logdog.error(…)`, `h.Send(…)`) goes to Log Dog only, not to the console, and is sent whatever the logger's level.

## Tests

```sh
cd python && python3 -m unittest -v
cd go && go test -race ./...
```

## Licence

MIT. See [LICENSE](LICENSE).
