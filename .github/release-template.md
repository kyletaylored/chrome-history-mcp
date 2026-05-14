## Install

Download `chrome-history-mcp.mcpb` below and double-click it. Claude
Desktop will walk you through:

1. **Chrome Profile** — defaults to `Default`. Change to `Profile 1`,
   `Profile 2`, etc. if you use a separate work profile.
2. **Install** — confirms and registers the `query-chrome-history` tool.

Requires `uv` on your `PATH` — `brew install uv` on macOS, or see
[astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/).

## What it does

Exposes your local Chrome browsing history to Claude through a single
read-only SQL tool against a lock-safe snapshot of the History DB. Useful
for activity reconstruction, time-tracking backfill, and answering
"what did I research last week?" — without ever leaving your machine.

See the [README](https://github.com/kyletaylored/chrome-history-mcp#readme)
for full usage, the schema the model sees, and timestamp conversion tips.
