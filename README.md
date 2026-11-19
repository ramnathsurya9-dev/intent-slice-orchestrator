# Intent-Based Network Slice Orchestrator

A FastAPI control plane that shares one link between Docker containers with Linux `tc` (HTB),
driven by application *intent* (`idle`, `4k_stream`, `cloud_gaming`). Each slice has a
**guaranteed** rate and a **ceiling**: it may use other slices' idle bandwidth up to the ceiling,
and gets its guarantee back the moment its owner needs it. Commercial slices (eMBB + URLLC) are
capped at **80%** of the link in total, so the other **20%** stays reserved for `public_slice`.

The dashboard is server-rendered by FastAPI (Python + Jinja2, no JavaScript).

## Status: what is verified

| Part | Status |
|---|---|
| Policy engine (80% cap on guarantees, scale-down by priority) | Tested, including exhaustive invariant tests |
| Borrowing rules (model of HTB sharing) | Tested as a model: `tests/test_htb_model.py` |
| REST API, server-rendered dashboard, monitor/self-healing loop | Tested (64 tests) and run live in dry-run mode |
| `tc` command generation, veth discovery, `tc -s class` parsing | Tested against a recording runner and canned output |
| **Real HTB on a kernel, Docker Compose stack, Redis lock** | **Not run yet. Needs Linux (see below)** |

## How the sharing works

```
link 100 Mbit/s                                  (1:1)
├─ commercial class: hard cap 80                 (1:2)  eMBB and URLLC share this
│    ├─ URLLC  guarantee 25, ceiling 80, prio 0  (1:20) served first when borrowing
│    └─ eMBB   guarantee 50, ceiling 80, prio 1  (1:10)
└─ public     guarantee 20, ceiling 100, prio 2  (1:30) unmatched traffic lands here
```

- A tenant that is busy while others are idle gets up to its ceiling. When the idle tenant wakes
  up it gets its guarantee back immediately (HTB semantics).
- The sum of **guarantees** of eMBB + URLLC is limited to 80 by the policy engine (rejected, or
  other lower-priority guarantees scaled down). Their **ceilings** can never pass 80 because they
  share the 80 Mbit/s class, so commercial traffic can never take the public reserve.
- `public_slice` may use idle commercial bandwidth (set `PUBLIC_CAN_BORROW=0` to pin it to its 20).
- Intent profiles: `idle` 2 Mbit/s fixed (50 ms added delay); `4k_stream` 50 guaranteed, up to 80,
  20 ms delay; `cloud_gaming` 25 guaranteed, up to 80, **no artificial delay** (its low latency
  comes from top priority, not from `netem`). Any value can be overridden per request.

## Run without Linux (dry-run, e.g. Windows)

No kernel changes: `tc` commands are only recorded, and telemetry is *simulated* by a model of the
sharing rules (labelled as such on the page).

```powershell
python -m venv .venv
.venv\Scripts\pip install -r core-orchestrator\requirements.txt -r requirements-dev.txt
powershell -ExecutionPolicy Bypass -File .\run-dry.ps1     # http://localhost:8000   API docs: /docs
.venv\Scripts\python -m pytest
```

## Run for real (Ubuntu, or WSL2 with Docker Engine)

```bash
docker compose up --build        # dashboard: http://<laptop-ip>:8000
```

The kernel needs the `sch_htb`, `sch_netem` and `cls_u32` modules. The default WSL2 kernel may lack
some of them; a normal Ubuntu machine or VM works. The HTB tree is attached to the Docker bridge
`br-telecom` (named in `docker-compose.yml`) and matches packets by destination IP, so it shapes
traffic from the host (or routed through it) toward the containers. Traffic between two containers
on the same bridge is switched, not routed, and is not shaped.

Try it: `docker compose exec orchestrator iperf3 -c 192.168.50.10 -t 20` while another slice is
idle, then start `cloud_gaming` and watch the guarantee being reclaimed.

## API

| Method and path | Purpose |
|---|---|
| `POST /api/v1/intent` | `{"intent":"cloud_gaming"}`; optional `slice`, `rate_mbps` (guarantee), `ceiling_mbps`, `latency_ms`, `on_exceed` (`reject`/`scale_down`) |
| `GET /api/v1/stats` | Slices (guarantee, ceiling, telemetry, borrowing), capacity split, event log |
| `POST /api/v1/congestion/simulate` | Push demand over the cap and trigger the safeguard |
| `POST /api/v1/reset` | Back to default profiles |

```bash
curl -X POST localhost:8000/api/v1/intent -H 'content-type: application/json' -d '{"intent":"4k_stream"}'
curl -X POST localhost:8000/api/v1/intent -H 'content-type: application/json' -d '{"intent":"4k_stream","rate_mbps":95}'   # 409
```

## Monitor and fail-safe

- Every 2 s it reads `tc -s class show` for each slice. If the public slice drops more than 2% of
  packets (or the link is saturated *and* public is losing packets), eMBB is pinned to 10 Mbit/s
  with no borrowing, which frees bandwidth for public; it is restored after 3 clean samples.
- On startup and shutdown the whole `tc` tree is flushed, so nothing stays throttled.
- Every kernel change runs under a Redis lock (in-memory in dry-run).

## Limitations

- Traffic is matched per container IP; the intent is declared by the caller, not detected.
- Real kernel behaviour (borrowing, priority, latency) is unverified until run on Linux. HTB only
  shares meaningfully when the link is actually congested.
- No API authentication. The orchestrator container is privileged and mounts the Docker socket,
  so only run it on a machine you control.
