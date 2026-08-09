# Datacenter Sim — How to Start

One script builds and runs the whole stack (FastAPI, Locust, Prometheus, Node Exporter, Grafana).

## 1. Prerequisites

- **Docker**
  - Linux/Ubuntu: `start.sh` will install it automatically if it's missing.
  - Mac: install [Docker Desktop](https://www.docker.com/products/docker-desktop/) first and make sure it's running — this can't be auto-installed by a script.
- Ports **8000, 8089, 9090, 9100, 3000** free on your machine (the script checks this and tells you exactly what's blocking it, if anything).

## 2. Get the code

**Option A — from the tarball:**
```bash
tar -xzf datacenter-sim-release.tar.gz
cd datacenter-sim
```

**Option B — from GitHub:**
```bash
git clone https://github.com/kashvo/datacenter-sim
cd datacenter-sim
git checkout phase3-infrastructure
```

## 3. Start everything

```bash
./start.sh
```

This will:
1. Check Docker is installed and running (install it on Linux if not)
2. Check the required ports are free
3. Build and start all 6 services
4. Wait until the API is healthy
5. Auto-open browser tabs for you

**First run takes a few minutes** (downloading images). After that it's fast.

## 4. Once it's running, check these

| Service | URL |
|---|---|
| FastAPI health check | http://localhost:8000/health |
| Locust load-test UI | http://localhost:8089 |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 (login: `admin` / `admin`) |

## 5. Run a load test

```bash
./start.sh stress normal_day
```

Other scenarios: `peak_load`, `spike`. Results are saved as CSV files in `loadtest-results/`.

## 6. Stop everything

```bash
./start.sh stop
```

## Troubleshooting

- **"Port already in use" error** → the message tells you which port and what's using it. Stop that process, or free the port, then re-run `./start.sh`.
- **Docker Desktop not installed (Mac)** → install it manually from the link above, start it, then re-run `./start.sh`.
- **Something looks broken** → `./start.sh logs` tails logs from all services.
