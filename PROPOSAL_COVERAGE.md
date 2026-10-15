# How datacenter-sim meets the project proposal

Each row maps a requirement in *CPP_Project_Proposal.docx* to where it is
implemented and how it was checked. "Test" means an automated test in
`tests/`. "Run" means a recorded end-to-end run with the built-in server.

## 2–3. Problem statement and multi-user gap

| Requirement | Implementation | Checked by |
|---|---|---|
| Ramp-up → peak → sustain → cooldown instead of flat load | Stages with phases `idle / ramp_up / spike / peak / steady / cooldown`; the controller moves users at each stage's spawn rate | Test `test_controller_moves_each_interface_toward_its_own_target`; every run |
| Behaviour changes with time of day, business cycles and events | `day_night_cycle`, `weekday_weekend`, `month_end_spike`, `flash_event` profiles | Run (all profiles exercised) |
| Thousands or millions of concurrent users | Capacity scales with each machine. `dcsim worker` adds more computers, and users are split by capacity | Run with a worker agent: 3 workers, users split 30/14/14 by capacity |
| Many interfaces at once | Profiles mix any number of interfaces simultaneously | Every run |
| Memory leaks during ramp-up and session creation | Rule "server memory grew and did not come back" | Test `test_flags_transition_errors_leak_and_sessions` |
| Cleanup and flush storms at cooldown and mass logout | Mass logout at cooldown, session TTL sweeper, rule "sessions left open" | Same test |
| Race conditions and lock contention at peak | Shared inventory and ledger locks on the server; rule "interface slowed most at peak" | Analysis test, banking and e-commerce runs |
| Backups = large sequential writes | `batch` / `settlement_batch` upload with fsync; `storage_mixed` backup window | Run: 20 MB uploaded in a short test |
| Peak = small random reads and writes | `/storage/random` (4 KB blocks, fsync) and the `storage_client` interface | Test `test_page_load_and_random_storage`; run: 7,365 random reads and 1,995 writes |
| CPU-, memory-, network- and storage-heavy phases differ | Per-endpoint work (hashing, locks, streaming, random I/O); host disk and network metrics per phase | `phase_summary.csv` includes disk read/write per phase |
| Repeatable, evidence-driven testing | Saved profiles, per-run seed, saved samples, events, reports and CSVs | Test `test_same_seed_same_user_behaviour`; "Run again with seed" |
| Login/logout, session lifetime, idle periods, think-time, heavy vs light users | Per interface: `login`, `session_lifetime_sec` (log out and back in), `wait_sec`, `heavy_user_fraction`, `heavy_wait_multiplier` | Every run (login and logout counts) |
| Websites: latency-sensitive, bursty, short-lived | `web` interface with page loads (HTML plus assets) | Run: `GET /web/home` + `GET /static/*` |
| Mobile: frequent calls, spikes, background sync | `mobile` interface with short sessions and frequent syncs | Run |
| APIs: sustained machine concurrency | `api`, `inventory_api`, `recommendation_api` | Run |
| Background jobs: batch, I/O heavy | `batch`, `settlement_batch`, `analytics_batch` | Run |
| Cross-interface contention and starvation | Interfaces share one server, locks and CPU; starvation rule in analysis | Analysis test |

## 5–6. Objectives and scope

| Requirement | Implementation | Checked by |
|---|---|---|
| 6.1 Day/night, weekday/weekend, month/quarter-end, event surges | The time-based profiles above. They run on compressed time; set longer stage durations for real-time runs | Run |
| 6.2 Ramp-up, peak, steady, cooldown modelled explicitly | Stage `phase` field | Every run |
| 6.2 Metrics and failures correlated to phases | Every sample carries stage and phase; reports show per-phase results, "p95 entering vs settled" and transition-defect counts | Analysis tests |
| 6.3 Configurable number of concurrent users | Total or per interface, absolute or % of capacity; hold-at-N live command | Tests and UI |
| 6.3 Session lifecycle (login, activity, logout) | Every login-enabled interface | Run |
| 6.3 Think-time and randomness | `wait_sec` ranges, weighted random actions, per-user seeded generator | Test |
| 6.3 User-to-workload affinity | Each user belongs to one interface and has its own account for its lifetime | Code: `dcsim/load/users.py` |
| 6.4 Browser-like web traffic | Page loads with assets | Test and run |
| 6.4 Mobile access patterns | `mobile`, `shopper_mobile` | Run |
| 6.4 Backend APIs (REST and gRPC) | REST interfaces plus real gRPC over HTTP/2 (`grpc_telemetry`, `grpc_payments`) | Test `test_grpc_services`; run: 68,659 gRPC calls, 0 errors |
| 6.4 Background and batch services | Batch interfaces | Run |
| 6.4 Interfaces scaled independently and blended | Per-stage `users: {web: 30%, api: 20}`, per-stage `mix`, `only`; each interface has its own curve | Test `test_per_interface_users_and_stage_mix`; `interface_curves` run, where actual users matched each interface's target |

## 7. Workload archetypes

| Archetype | Profile | Interfaces and server behaviour |
|---|---|---|
| E-commerce flash sale | `ecommerce_flash_sale` | web and mobile shoppers, checkout under an inventory lock, inventory API |
| Banking and payments | `banking_payments`, `month_end_spike` | transactions with CPU hashing and a ledger lock, strict SLA, settlement batch |
| Video streaming | `video_streaming` | large sequential reads, high read concurrency, recommendation service |
| IoT sensor fleet | `iot_sensor_fleet` | machine ingest in periodic bursts, aggregation job |
| Social media | `social_media` | feed refresh, posts, media uploads |

## 8. Architecture and phased modules

| Component | Implementation |
|---|---|
| User behaviour engine | `dcsim/load/users.py`, `controller.py` |
| Interface-specific load generators | Locust user classes built per interface (HTTP, gRPC, page loads), local and remote workers |
| Workload orchestrator | `controller.py` (timing, phases, blending, intensity) plus live commands from the panel |
| Telemetry and validation layer | `dcsim/control/manager.py` (system, process and load metrics, watchdog), `analysis.py` |
| Phase 1: design and planning | Profiles and Interfaces pages, YAML files, Git-friendly |
| Phase 2: user and interface simulation | Load generator and built-in FastAPI + gRPC server |
| Phase 3: execution and monitoring (CPU, memory, storage, network, errors) | Live test page; psutil sampling; optional Prometheus + Node Exporter + Grafana |
| Phase 4: analysis and reporting (bottlenecks, comparison, reports) | Results page, HTML report, run comparison, Pandas CSVs, Matplotlib PNGs |

## 8.2 Suggested technologies

| Suggested | Used |
|---|---|
| Python | Yes |
| JSON / YAML profiles | YAML profiles, interfaces and settings |
| Git | Repository layout, `.gitignore` |
| Locust or k6 | Locust, used as a library |
| Flask or FastAPI | FastAPI, plus gRPC |
| Docker | `docker/Dockerfile`, `docker/docker-compose.yml` |
| Prometheus, Node Exporter | `/metrics` on the panel and server; compose stack with Prometheus and Node Exporter |
| Grafana | Provisioned dashboard with 20 panels |
| Pandas, Matplotlib | `dcsim/control/charts.py`, run on every finished test |

## 9–10. Expected outcomes and success metrics

| Metric | Where it is reported |
|---|---|
| Number of defects detected during phase transitions | "Defects at phase transitions" in the report's Coverage and defects section |
| Ability to reproduce issues with the same profile | Seed per run; "Run again with seed N"; `dcsim run <profile> --seed N` |
| Coverage across users, interfaces and time windows | Coverage section: interfaces %, endpoints %, time windows %, phase types %, peak users reached vs planned |
| Repeatable, comparable results across platforms and configurations | Run comparison (phase by phase); reports record computer, capacity, settings and seed |
| Use for broader server certification | `dcsim run` exit codes (0 = PASS, 2 = FAIL) for CI and certification scripts; external-server mode |

## What was and was not verified in development

- **Verified:**
  - 35 automated tests.
  - End-to-end runs of every built-in profile on Linux with the built-in server.
  - A run with a worker agent.
  - The real Prometheus 2.53 and Node Exporter 1.8.1 binaries scraping the panel and server. All three targets were up, and all 29 Grafana panel queries returned data.
- **Not run in development** (no access from the build machine):
  - Building the Docker image and running Grafana itself.
  - First runs of the Windows and macOS launchers.

  Run these once on your own machines.
- **Seed reproducibility** covers every simulated user's decisions and the server's randomness. Exact request timing still depends on the network and operating system, so latency figures vary slightly between repeats.
