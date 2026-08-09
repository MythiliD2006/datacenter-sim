# datacenter-sim

**Real-World, Multi-User Data Centre Workload Simulation for Server Testing**

A scalable, open-source framework for simulating realistic multi-user login traffic against a server — built to test how authentication systems behave under ramp-up, peak, sustained, and cooldown conditions, from hundreds to millions of users.

![Python](https://img.shields.io/badge/Python-3.11-blue)
![Locust](https://img.shields.io/badge/Load%20Gen-Locust-green)
![FastAPI](https://img.shields.io/badge/Server-FastAPI-teal)
![Docker](https://img.shields.io/badge/Infra-Docker-blue)
![Prometheus](https://img.shields.io/badge/Metrics-Prometheus-orange)
![Grafana](https://img.shields.io/badge/Dashboard-Grafana-red)
![License](https://img.shields.io/badge/License-MIT-lightgrey)

---

## Quick Start

One script builds and runs the whole stack — FastAPI, Locust, Prometheus, Node Exporter, Grafana.

### Prerequisites

- **Docker**
  - Linux/Ubuntu: `start.sh` installs it automatically if missing
  - Mac: install [Docker Desktop](https://www.docker.com/products/docker-desktop/) first and make sure it is running
  - Windows: use WSL and run `start.sh` inside Ubuntu, or run `docker-compose up --build` manually
- Ports **8000, 8089, 9090, 9100, 3000** must be free on your machine
- Python 3.11+
- Git

### Get the code

**Option A — from GitHub:**
```bash
git clone https://github.com/subhasreelk/datacenter-sim.git
cd datacenter-sim
```

**Option B — from tarball:**
```bash
tar -xzf datacenter-sim-release.tar.gz
cd datacenter-sim
```

**Option C — from zip:**
```bash
unzip datacenter-sim-release.zip
cd datacenter-sim
```

### Start everything

```bash
chmod +x start.sh
./start.sh
```

This will:
1. Detect your operating system
2. Check Docker is installed — installs it automatically on Linux if missing
3. Check required ports are free — tells you exactly what is blocking if any
4. Build and start all 6 services in the background
5. Wait until FastAPI is healthy before proceeding
6. Auto-open browser tabs for all services

**First run takes a few minutes** — Docker downloads images. After that it is fast.

### Service URLs

| Service | URL | Notes |
|---|---|---|
| FastAPI docs | http://localhost:8000/docs | Swagger UI — test endpoints manually |
| FastAPI health | http://localhost:8000/health | Liveness check |
| FastAPI stats | http://localhost:8000/stats | Active sessions and uptime |
| Locust UI | http://localhost:8089 | Load test control panel |
| Prometheus | http://localhost:9090 | Raw metrics database |
| Grafana | http://localhost:3000 | Live dashboards (login: admin / admin) |

### Run a load test

```bash
# steady baseline — 100 users, ramp up over 60s, hold 300s, cooldown 60s
./start.sh stress normal_day

# flash event spike — 200 users, spike in 20s, hold 180s, cooldown 40s
./start.sh stress flash_event
```

Results are saved automatically to `results/` as CSV files.

### Other commands

```bash
./start.sh stop      # stop all services
./start.sh status    # show which containers are running
./start.sh start     # start everything (default)
```

### Troubleshooting

- **Port already in use** — the error message tells you which port and what process is using it. Stop that process then retry.
- **Docker Desktop not installed (Mac)** — install from the link above, open Docker Desktop, then re-run `./start.sh`.
- **Permission denied on start.sh** — run `chmod +x start.sh` first.
- **Something looks broken** — run `cd infra && docker compose logs -f` to tail live logs from all services.
- **Windows users** — `start.sh` is a bash script. Use WSL (Ubuntu) to run it, or run manually: `cd infra && docker-compose up --build`

---

## What this is

Most server load tests send flat, constant traffic — but real production traffic ramps up, spikes, sustains, and cools down, often through multiple interfaces at once, with different user types competing for the same server resources.

This framework reproduces that realism around a login/authentication system — one of the most concurrency-sensitive parts of any application. It simulates three interface types (web, mobile, API), each with light and heavy user variants, generating realistic session lifecycles alongside sustained machine traffic. This creates genuine cross-interface contention and exposes defects that flat-load tests miss entirely.

---

## Table of contents

- [Architecture](#architecture)
- [Tech stack](#tech-stack)
- [Folder structure](#folder-structure)
- [Team and responsibilities](#team-and-responsibilities)
- [Workload profiles](#workload-profiles)
- [Simulated user types](#simulated-user-types)
- [Server endpoints](#server-endpoints)
- [Running tests](#running-tests)
- [Monitoring](#monitoring)
- [Crash logging](#crash-logging)
- [SLA thresholds](#sla-thresholds)
- [Contributing](#contributing)

---

## Architecture

```
profiles/
  normal_day.yaml        ← steady baseline pattern
  flash_event.yaml       ← sudden spike pattern
        |
        | read by run_test.py
        v
Locust (master + workers)
  locustfile.py + loadshapes.py
  WebUser 50%, MobileUser 35%, APIUser 15%
        |
        | HTTP requests
        v
FastAPI server
  /login  /logout  /me
  /profile/update  /api/data
  /health  /metrics  /stats
        |
        | scrapes /metrics every 5s
   Prometheus ──────────► Grafana dashboards
   + Node Exporter         live visualisation
        |
        v
  analysis/analyse.py
  Bottleneck detection
  SLA verdict + PDF report
```

---

## Tech stack

| Layer | Tools |
|---|---|
| Language | Python 3.11 |
| Test target server | FastAPI, Uvicorn |
| Load generation | Locust (distributed master/worker) |
| Infrastructure | Docker, Docker Compose |
| Monitoring | Prometheus, Node Exporter, Grafana |
| Analysis and reporting | Pandas, Matplotlib, Seaborn, WeasyPrint |

All tools are 100% open source — zero licensing cost.

---

## Folder structure

```
datacenter-sim/
├── app/                          # FastAPI server — test target (Phase 1)
│   ├── main.py                   # Entry point, middleware, /health /stats /metrics
│   ├── Dockerfile                # python:3.11-slim container
│   ├── requirements.txt          # All Python dependencies
│   ├── core/
│   │   ├── logger.py             # Structured JSON logging to logs/server.log
│   │   ├── metrics.py            # Prometheus counters, histograms, gauges
│   │   ├── rate_limiter.py       # 20 login attempts per IP per 60 seconds → 429
│   │   └── security.py           # Token generation via secrets.token_hex(32)
│   ├── db/
│   │   └── fake_db.py            # 100 seeded users, in-memory session store
│   ├── models/
│   │   └── schemas.py            # Pydantic request and response models
│   ├── routers/
│   │   ├── auth.py               # POST /login, POST /logout
│   │   ├── data.py               # GET /api/data — machine traffic endpoint
│   │   └── profile.py            # GET /me, POST /profile/update
│   └── tests/
│       └── test_endpoints.py     # 21 automated tests covering all endpoints
│
├── infra/                        # Infrastructure (Phase 3)
│   ├── docker-compose.yml        # All 6 services on one Docker network
│   └── prometheus.yml            # Prometheus scrape config — 5s interval
│
├── locust/                       # Locust container definition
│   ├── Dockerfile                # Locust container — python:3.11-slim
│   └── requirements.txt          # locust + pyyaml
│
├── monitor/                      # Crash detection
│   └── crash_watch.py            # Polls /health — saves full snapshot on crash
│
├── profiles/                     # Traffic scenario configs (Phase 2)
│   ├── normal_day.yaml           # 100 users, 420s total duration
│   └── flash_event.yaml          # 200 users, aggressive 20s ramp
│
├── locustfile.py                 # WebUser, MobileUser, APIUser classes
├── loadshapes.py                 # Ramp-up, peak, cooldown curves from YAML
├── run_test.py                   # Profile-driven headless test runner
├── start.sh                      # One command to install, build and start stack
├── .gitignore                    # Excludes venv, logs, results CSVs, pycache
└── README.md                     # This file
```

---

## Team and responsibilities

| Member | Name | Role | Owns |
|---|---|---|---|
| Member 1 | Subhasree | App developer | FastAPI server — all 7 endpoints, auth, metrics, logging, Dockerfile |
| Member 2 | Mythili | App developer | Rate limiting, 100 seeded users, /stats endpoint, 21 automated tests |
| Member 3 | Harshitha | Simulation | locustfile.py, loadshapes.py, run_test.py, YAML profiles |
| Member 4 | Kavesha | Infrastructure | docker-compose.yml, prometheus.yml, start.sh, crash_watch.py |
| Member 5 | — | Monitoring | Grafana dashboards, Node Exporter alerts |
| Member 6 | — | Analysis | analyse.py, PDF report, SLA verdict, charts |

---

## Workload profiles

All test parameters live in a single YAML file. Change the file — change the entire test. No code changes needed.

| Profile | Users | Ramp | Hold | Cooldown | Use for |
|---|---|---|---|---|---|
| normal_day | 100 | 60s | 300s | 60s | Steady baseline testing |
| flash_event | 200 | 20s | 180s | 40s | Sudden spike simulation |

To run a profile:
```bash
./start.sh stress normal_day
./start.sh stress flash_event
```

---

## Simulated user types

| User type | Weight | Wait time | Primary behaviour |
|---|---|---|---|
| WebUser | 50% | 2-5s | Login → read profile → update → logout |
| MobileUser | 35% | 1-3s | Login → poll /me repeatedly (background sync) |
| APIUser | 15% | 0.1-0.5s | Login → hammer /api/data continuously |

All three types run simultaneously creating cross-interface contention on the server.

---

## Server endpoints

| Method | Endpoint | Auth required | Purpose |
|---|---|---|---|
| POST | /login | No | Authenticate user, return session token |
| POST | /logout | Yes | Invalidate session token |
| GET | /me | Yes | Read current user profile and role |
| POST | /profile/update | Yes | Update profile — write-heavy, 50ms delay |
| GET | /api/data | Yes | Machine traffic — random 10-100ms delay |
| GET | /health | No | Server liveness check |
| GET | /metrics | No | Prometheus scrape target |
| GET | /stats | No | Total users, active sessions, uptime |

Login credentials for testing:
- `webuser_0` / `pass` — role: web
- `mobileuser_1` / `pass` — role: mobile
- `apiuser_2` / `pass` — role: api

100 users total: webuser_0 to webuser_99, mobileuser_1 to mobileuser_97, apiuser_2 to apiuser_98.

---

## Running tests

### Automated tests

```bash
source venv/bin/activate
python -m pytest app/tests/ -v
```

Expected: 21 passed

### Manual testing

Open http://localhost:8000/docs → click Authorize → paste token → test any endpoint.

### Load test (without Docker)

```bash
source venv/bin/activate
pip install locust pyyaml
uvicorn app.main:app --port 8000 &
python run_test.py normal_day
```

---

## Monitoring

While a test runs open Grafana at http://localhost:3000 (admin / admin) to see live panels:

- Requests per second
- p95 login latency
- Error rate
- Active concurrent sessions
- CPU utilisation
- Memory utilisation

Prometheus metrics available at http://localhost:9090.

Prometheus metric names:
```
http_requests_total          {endpoint, method, status_code}
http_request_duration_ms     {endpoint} — histogram
active_sessions_total        — gauge
login_success_total          — counter
login_failure_total          — counter
```

---

## Crash logging

Logs persist via Docker volume mounts — they survive container crashes and restarts.

```
logs/fastapi/server.log       ← structured JSON logs from FastAPI
logs/crash_<timestamp>.txt    ← full snapshot saved by crash watchdog
```

The crash watchdog (`monitor/crash_watch.py`) polls `/health` every 3 seconds. The moment the server stops responding it saves a full snapshot including the last 200 log lines and Docker container status.

Run alongside load tests:
```bash
python monitor/crash_watch.py &
./start.sh stress flash_event
```

---

## SLA thresholds

| Metric | Target |
|---|---|
| p95 login latency | < 500ms |
| Error rate | < 1% |
| CPU utilisation | < 80% |
| Memory utilisation | < 90% |

---

## Contributing

1. Never push directly to main
2. Create a feature branch from main
3. Open a pull request when done
4. At least one member reviews before merge

```bash
git checkout main
git pull origin main
git checkout -b feature/your-name-description
# make your changes
git add .
git commit -m "feat: describe your change"
git push origin feature/your-name-description
# then open a PR on GitHub
```

---

*Built with Python · FastAPI · Locust · Docker · Prometheus · Grafana*