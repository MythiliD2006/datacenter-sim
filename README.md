# datacenter-sim

**Real-world, multi-user data centre workload simulation for server testing.**

Download it, run one launcher, and a control panel opens in your browser. From
the panel you start and steer load tests, edit workload profiles, client
behaviour and every setting, watch live charts, and read a report with a
pass/fail verdict, coverage and likely defects when the test ends.

Load size is never hard-coded. datacenter-sim checks the computer it runs on
(CPU cores, free memory, open-file limit) and works out how many simulated
users that machine can drive. Other computers can join with `dcsim worker`
and add their capacity. Profiles give users as a percentage of that total, so
the same profile runs 120 users on a laptop and tens of thousands across a lab.

See [PROPOSAL_COVERAGE.md](PROPOSAL_COVERAGE.md) for how each requirement of
the project proposal is met.

---

## Quick start

You need **Python 3.10 or newer**. Nothing else: Docker, Prometheus and
Grafana are optional extras.

| System | Do this |
|---|---|
| Windows | Double-click `start.bat` |
| macOS | Double-click `start.command` (or run `./start.sh` in Terminal) |
| Linux | `./start.sh` |

The first run creates a private Python environment in `.venv` and installs the
dependencies. This needs internet once and takes a minute or two. After that the
panel opens in a few seconds and works offline.

The panel opens at `http://127.0.0.1:8765`. If that port is busy, the next free
one is used and printed in the terminal. Stop with **Ctrl+C**.

Prefer pip? `pip install .` and then run `dcsim`.

---

## What you can do in the panel

| Page | What it is for |
|---|---|
| **Live test** | Pick a profile and press **Start test**. You see the phase plan with a live cursor, users per interface, the users actually running, req/s, p50/p95/p99, failures, and CPU and memory for this computer and the server under test. Tables show each interface (users vs target) and each endpoint, and an activity feed logs events. While a test runs you can **skip to the next phase**, **hold at N users** or **stop**. Type a **seed** to repeat an earlier run exactly. Start, restart or stop the server under test. **Measure capacity** finds this setup's real limit. |
| **Profiles** | Create, duplicate, edit and delete workload profiles: interface mix, stages (phase, length, users in total or per interface, per-stage weights, spawn rate, "only these interfaces") and a fixed seed. A preview shows the plan for this computer, both the total and each interface's own curve. |
| **Interfaces** | How each kind of client behaves: think-time, session length (log out and back in), share of heavy users, and its actions. Each action has HTTP or gRPC, method, path, weight, query, JSON body, upload size and page assets. |
| **Results** | Every run is saved with its verdict, findings, coverage and defect counts, interactive charts, Pandas + Matplotlib PNG charts, and tables by phase, interface and endpoint. You can download the HTML report (prints to PDF), the raw CSV and a per-phase CSV. **Run again with seed N** repeats a run. Tick two runs to **compare** them. |
| **Settings** | Every number the simulator uses, with an explanation under each field. |
| **Logs** | Activity, server log, server errors, load generator and control-panel logs. |

---

## How the number of users is decided

```
capacity = min( cores × users_per_core,
                free RAM × (1 − reserve) ÷ mb_per_user,
                open-file limit × 0.8 ÷ fds_per_user,
                max_users )
         + the capacity of every connected worker computer
```

All coefficients live in **Settings → User capacity of this computer**. There
are three modes:

- **auto** (default): the formula, recomputed from the live machine each time a test starts.
- **measured**: set by **Measure capacity**, which steps users up until p95, errors or CPU break the SLA.
- **fixed**: a number you type.

The load generator scales too. It starts one process per `users_per_worker`
users, up to the number of cores, and splits users between processes and
computers in proportion to each one's capacity. The built-in server creates
`capacity × accounts_per_user` login accounts, so every simulated user has its
own.

### More computers: `dcsim worker`

On the main computer, open the panel to your network:

```bash
./start.sh --host 0.0.0.0           # Windows: start.bat --host 0.0.0.0
```

On every extra computer (with this project unzipped there too):

```bash
./start.sh worker --panel http://MAIN-COMPUTER-IP:8765
```

Each worker reports its own capacity and appears in the panel's status line.
When a test starts, it launches worker processes that connect to the main
computer. Open TCP ports 8765, 5557 (`load.master_port`) and the built-in
server's ports in the main computer's firewall.

---

## Built-in profiles

| Profile | What it simulates |
|---|---|
| `quick_smoke` | Two-minute check that everything works |
| `normal_day` | Gradual ramp, short peak, long steady state, cooldown |
| `flash_event` | Users arrive within seconds, hold, then leave together (mass logout) |
| `day_night_cycle` | 24 hours compressed to 6 minutes; night hours run backup jobs only |
| `weekday_weekend` | A week compressed to 7 minutes with nightly batch windows |
| `month_end_spike` | Normal load, then month-end close with settlement and reporting |
| `interface_curves` | Every interface on its own curve: mobile peaks early, web at midday, batch at night |
| `ecommerce_flash_sale` | Web and mobile shoppers, bursty checkout, inventory API updates |
| `banking_payments` | Transaction-heavy customers on a shared ledger lock, then batch settlement |
| `video_streaming` | Large sequential reads with high concurrency, plus recommendations |
| `iot_sensor_fleet` | Machine-generated ingest in periodic bursts with aggregation |
| `social_media` | Feed refreshes, posts and media uploads |
| `storage_mixed` | Backup window (large sequential writes), then peak small random reads/writes |
| `grpc_microservices` | Service-to-service gRPC calls alongside REST clients |

Profiles are YAML files in your data folder. Edit them in the panel or by hand:

```yaml
name: Flash event
mix: {web: 50, mobile: 35, api: 15}            # interface weights
seed: 4242                                     # optional: repeat exactly
stages:
  - {phase: idle,     duration: 20,  users: 5%,  label: Quiet before}
  - {phase: spike,    duration: 20,  users: 90%, label: Surge}
  - {phase: peak,     duration: 160, users: 90%, mix: {web: 70, mobile: 30}}   # this stage's own split
  - {phase: cooldown, duration: 40,  users: 5%,  label: Mass logout}
  - {phase: steady,   duration: 60,  users: {api: 10%, batch: 15%}}           # a number per interface
```

- `users` is a percentage of capacity (`60%`), an absolute number (`120`), or a map with a value per interface.
- `mix` on a stage changes the split for that stage only.
- `only` limits a stage to some interfaces.
- `spawn_rate` is `auto` (reach the target within the stage) or users per second.
- Phases are `idle`, `ramp_up`, `spike`, `peak`, `steady` and `cooldown`. Every metric is tagged with its phase.

## Interfaces (who the users are)

| Interface | Traffic |
|---|---|
| `web` | Browser page loads (HTML plus 5 assets), profile reads and writes |
| `mobile` | Short sessions, frequent small API syncs |
| `api` | Sustained machine traffic on one long session |
| `batch` | Backups: 1 MB uploads written to disk, 4 MB sequential reads |
| `storage_client` | Database-style random 4 KB reads and writes, flushed to disk |
| `grpc_telemetry`, `grpc_payments` | Real gRPC calls (HTTP/2) to the built-in gRPC services |
| `shopper_web`, `shopper_mobile`, `inventory_api` | E-commerce |
| `payments_client`, `settlement_batch` | Banking |
| `viewer`, `recommendation_api` | Video streaming |
| `iot_device`, `analytics_batch` | IoT |
| `social_user`, `social_uploader` | Social media |

## Reproducible runs

Every run records a random seed, shown in the panel, the report and the
results list. Each simulated user's choices come from its own generator seeded
from the run seed and its account number, and the built-in server's
randomness is seeded too. These choices are:

- which action to take
- think-time
- heavy or light user
- session length

So starting a run with the same seed and profile repeats the same behaviour.
Network timing still varies slightly. Use **Run again with seed N** on a
result, type a seed before **Start test**, put `seed:` in a profile, set
`load.seed` in Settings, or run `dcsim run normal_day --seed N`.

---

## Testing your own server

In **Settings → Server under test**, set `mode` to `external`, set
`external_url` (for example `http://10.0.0.25:8000`) and, if you have one,
`external_grpc` (`10.0.0.25:50051`). Point the paths in **Interfaces** at your
API. The **Login** section describes your login (paths, field names, token
field, header). Interfaces with login turned off send requests without
signing in.

## The built-in server under test

When `target.mode` is `builtin`, the panel starts a FastAPI server and a gRPC
server. Their delays, sizes, locks and limits are under **Settings → Built-in
server behaviour**.

| Endpoint | Stresses |
|---|---|
| `POST /login`, `POST /logout` | bcrypt password check (CPU), session creation and cleanup |
| `GET /web/home`, `GET /static/*` | Browser page loads |
| `GET /me`, `POST /profile/update`, `GET /api/data` | Small reads and writes |
| `GET /catalog`, `POST /cart/checkout`, `POST /inventory/update` | Shared inventory lock (contention at peak) |
| `POST /transactions`, `GET /transactions/history` | CPU hashing plus a shared ledger lock |
| `POST /batch/upload`, `GET /media/stream` | Large sequential writes (fsync) and reads |
| `GET/POST /storage/random` | Small random block reads and writes (fsync) |
| `GET /recommendations`, `GET /feed`, `POST /posts` | CPU and mixed read/write |
| `POST /iot/ingest`, `GET /analytics/aggregate` | Buffered small writes, aggregation |
| gRPC `dcsim.Telemetry/GetData`, `/Ingest`, `dcsim.Payments/Transfer` | Machine-to-machine RPC |
| `GET /health`, `GET /stats`, `GET /metrics` | Health, counters, Prometheus metrics |

Sessions expire after `session_ttl_sec` and are swept in batches, which
reproduces cleanup storms.

---

## Reports, coverage and defect detection

Each finished run gets a verdict against the SLA: p95 latency, error rate, CPU
95th percentile and memory. **Coverage** shows how much of the plan ran: which
interfaces and endpoints were exercised, how many time windows (stages) had
traffic, which phase types ran, and the peak users reached. The report counts
**defects**, including how many happened at phase transitions. Rules, with
thresholds under **Settings → Defect detection rules**:

- latency jumps when a phase starts (transition defects)
- error bursts in a phase, especially ramp-up, spike and cooldown
- failing logins (rate limit, missing accounts)
- server memory that grows and does not come back (possible leak)
- sessions left open after users leave (cleanup issues)
- one interface slowing far more than others at peak (contention or starvation)
- CPU saturation
- server crashes. A watchdog checks health every second and saves a crash snapshot with recent logs.
- an overloaded load generator

Each run folder holds:

- `report.html`, a self-contained report that opens offline and prints to PDF
- `timeseries.csv` and `phase_summary.csv`, built with Pandas
- `chart_*.png`, drawn with Matplotlib
- the raw `samples.jsonl` and `events.jsonl`

## Optional: Prometheus + Grafana

The panel exposes load-side metrics at `/metrics`: users, plan target, phase,
req/s, latency, and per-interface users, req/s and p95. The built-in server
exposes HTTP and gRPC metrics. A ready stack is in `docker/`:

```bash
cd docker
docker compose up --build
```

| | |
|---|---|
| Control panel | http://localhost:8765 |
| Grafana | http://localhost:3000 (admin / admin). The "datacenter-sim" dashboard has 20 panels |
| Prometheus | http://localhost:9090 |

The stack includes Node Exporter for host CPU, memory, disk and network.

---

## Command line

```
dcsim                         open the control panel
dcsim --host 0.0.0.0          open it to other computers (needed for dcsim worker)
dcsim --port 9000             use another port
dcsim info                    show this computer's resources and capacity
dcsim profiles                list profiles
dcsim run normal_day          run a profile without the panel; exit code 0 = PASS, 2 = FAIL
dcsim run normal_day --seed 7 repeat a run
dcsim run calibrate           measure capacity without the panel
dcsim worker --panel URL      lend this computer's capacity to a panel elsewhere
dcsim --reset                 restore default settings, interfaces and profiles
dcsim --home D:\dcsim         keep data somewhere else (or set DCSIM_HOME)
```

Use `start.sh` / `start.bat` in place of `dcsim` if you have not installed the
package with pip; they accept the same options.

Any setting can also come from an environment variable named
`DCSIM__SECTION__KEY`, for example `DCSIM__TARGET__PORT=8000` or
`DCSIM__SLA__P95_MS=250`. This is handy for containers and CI.

## Where your data lives

Everything you can change is in `~/.dcsim` (Windows: `C:\Users\<you>\.dcsim`):

```
config.yaml        settings
interfaces.yaml    client behaviour library
profiles/          workload profiles
runs/<run id>/     meta, samples, events, summary, report.html, CSV, PNG charts, crash snapshots
logs/              server, control-panel and error logs
```

## Running the tests

```bash
.venv/bin/pip install pytest      # Windows: .venv\Scripts\pip install pytest
.venv/bin/python -m pytest
```

---

## Architecture

```
 Browser control panel (dcsim/ui: HTML + JS + Chart.js, no build step)
        │ REST + WebSocket
        ▼
 Control server (dcsim/control) ─────────────── /metrics ──► Prometheus ► Grafana (optional)
   manager.py   server-under-test and load processes, monitoring (CPU, memory,
                disk, network), health watchdog, crash snapshots, run recording,
                worker-computer registry, seeds
   analysis.py  per-phase results, SLA verdict, defect rules, coverage, comparisons
   charts.py    Pandas tables + Matplotlib charts
   report.py    self-contained HTML report
        │ starts                          │ starts
        ▼                                 ▼
 Server under test (dcsim/target)     Load generator (dcsim/load)
   FastAPI + gRPC, config-driven        controller.py   per-interface user targets per stage
   endpoints, accounts sized from       users.py        Locust users built from interfaces.yaml,
   capacity                                             seeded per user, HTTP + gRPC + page loads
                                        runner.py       1..N local workers + workers on other
                                                        computers (dcsim worker → agent.py)
```

| Proposal phase | Where it lives |
|---|---|
| 1. Workload design and planning | `profiles/*.yaml`, `interfaces.yaml`, Profiles and Interfaces pages |
| 2. User and interface simulation | `dcsim/load` and `dcsim/target` |
| 3. Execution and monitoring | `dcsim/control/manager.py`, Live test page, optional Prometheus/Grafana |
| 4. Analysis and reporting | `analysis.py`, `charts.py` (Pandas/Matplotlib), `report.py`, Results page |

## License

MIT
