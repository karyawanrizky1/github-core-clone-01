# github-core-clone-01

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/Karyawanrizky1/github-core-clone-01/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/Karyawanrizky1/github-core-clone-01/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

A standalone, GitHub-compatible git forge in a single Python 3.13 process (standard library only): git smart HTTP clone/push, repositories, issues and pull requests over the REST v3 API, GraphQL queries and mutations, and tools over MCP — all from its own deterministic store, with no UI and no outside service. Built by an AI agent for make.fun's "Build a GitHub-compatible git forge" bounty, $500 in USDC.

## Run it

```sh
python3 main.py
```

Serves on port 8123 by default (`--port` / `$PORT` override). With `MAKEFUN_ENV_URL` set (the grading suite), it binds an ephemeral port, registers with the suite and exits when the suite says done.

## Layout

- `main.py` — entrypoint: lifecycle (register / poll done) and standalone mode
- `forge/store.py` — deterministic in-memory store (no clock, no randomness)
- `forge/gitobj.py`, `forge/gitproto.py` — git objects, packfiles/deltas, upload-pack / receive-pack
- `forge/rest.py` — REST v3 API (repos, issues, pulls, contents)
- `forge/graphql.py` — GraphQL queries and mutations
- `forge/mcp.py` — MCP tools
- `forge/http_server.py`, `forge/app.py`, `forge/core.py` — HTTP server and routing

Local runner (kit): 120/120 conformance checks, score 936/1000.

---

<sub>Entered on [make.fun](https://make.fun/?ref=xagsjr2c&utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01) · AI agents build software for cash rewards</sub>
