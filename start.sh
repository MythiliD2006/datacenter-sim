#!/usr/bin/env bash
#
# start.sh — one script to run the entire datacenter-sim stack.
#
# What it does:
#   1. Detects your OS
#   2. Checks if Docker + Docker Compose are installed
#   3. If missing on Linux (Debian/Ubuntu or RHEL/CentOS), installs them automatically
#   4. If missing on macOS, tells you to install Docker Desktop (can't be silently
#      auto-installed — it's a GUI app, not a command-line package)
#   5. Builds and starts all 6 services (FastAPI, Locust Master/Worker,
#      Prometheus, Node Exporter, Grafana)
#
# Usage:
#   ./start.sh -l normal_day              → FULL PLAYBOOK: baseline network stats, start
#                                            all services, wait healthy, start crash watchdog,
#                                            run the load test, capture final network stats,
#                                            print a summary (requests, failures, p95 login
#                                            latency, network RX/TX, SLA verdict)
#   ./start.sh -l normal_day,flash_event  → same playbook, runs both scenarios in sequence
#   ./start.sh start                      → just start services, no load test
#   ./start.sh stop                       → stops everything
#   ./start.sh status                     → shows which containers are running
#   ./start.sh stress normal_day          → runs a load test only (services must already be up)
#
# Any issues — contact Kavesha (Member 4 / Infrastructure).

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$REPO_ROOT/infra"

info()  { echo -e "\033[1;34m[start.sh]\033[0m $1"; }
error() { echo -e "\033[1;31m[start.sh ERROR]\033[0m $1"; }

detect_os() {
    case "$(uname -s)" in
        Linux*)  OS="linux" ;;
        Darwin*) OS="mac" ;;
        *)       OS="unknown" ;;
    esac
}

check_docker_installed() {
    if command -v docker &> /dev/null && docker compose version &> /dev/null 2>&1; then
        return 0
    elif command -v docker &> /dev/null && command -v docker-compose &> /dev/null; then
        return 0
    else
        return 1
    fi
}

install_docker_linux() {
    info "Docker not found. Attempting automatic install on Linux..."

    if command -v apt-get &> /dev/null; then
        info "Detected Debian/Ubuntu — installing via apt..."
        sudo apt-get update
        sudo apt-get install -y ca-certificates curl gnupg
        sudo install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        sudo chmod a+r /etc/apt/keyrings/docker.gpg
        echo \
          "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
          $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
          sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
        sudo apt-get update
        sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        sudo usermod -aG docker "$USER" || true
        info "Docker installed. NOTE: you may need to log out and back in for group permissions to apply."

    elif command -v yum &> /dev/null; then
        info "Detected RHEL/CentOS — installing via yum..."
        sudo yum install -y yum-utils
        sudo yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
        sudo yum install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        sudo systemctl enable --now docker
        sudo usermod -aG docker "$USER" || true
        info "Docker installed. NOTE: you may need to log out and back in for group permissions to apply."

    else
        error "Could not detect a supported package manager (apt or yum)."
        error "Please install Docker manually: https://docs.docker.com/engine/install/"
        exit 1
    fi
}

ensure_docker() {
    if check_docker_installed; then
        info "Docker + Docker Compose already installed. Skipping install."
        return
    fi

    detect_os
    if [ "$OS" = "linux" ]; then
        install_docker_linux
    elif [ "$OS" = "mac" ]; then
        error "Docker Desktop is required but not found."
        error "Please install it manually (it's a GUI app, can't be auto-installed by script):"
        error "  https://www.docker.com/products/docker-desktop/"
        error "After installing, open Docker Desktop once, then re-run ./start.sh"
        exit 1
    else
        error "Unsupported OS. Please install Docker manually: https://docs.docker.com/get-docker/"
        exit 1
    fi

    if ! check_docker_installed; then
        error "Docker installation did not complete successfully. Please check the errors above."
        exit 1
    fi
}

check_ports() {
    if ! command -v lsof &> /dev/null; then
        info "Skipping port pre-check (lsof not available) — Docker will report conflicts directly if any occur."
        return
    fi

    local ports=(8000 8089 9090 9100 3000)
    local names=("FastAPI" "Locust" "Prometheus" "Node Exporter" "Grafana")
    local conflicts=()

    for i in "${!ports[@]}"; do
        local port="${ports[$i]}"
        local name="${names[$i]}"
        if lsof -i ":$port" -sTCP:LISTEN &> /dev/null; then
            local process
            process=$(lsof -i ":$port" -sTCP:LISTEN -t | head -1 | xargs -I{} ps -p {} -o comm= 2>/dev/null)
            conflicts+=("Port $port ($name) is already in use${process:+ by: $process}")
        fi
    done

    if [ ${#conflicts[@]} -gt 0 ]; then
        error "Cannot start — the following ports are already taken:"
        for c in "${conflicts[@]}"; do
            echo "    - $c"
        done
        error "Stop whatever is using these ports, or edit the port mappings in infra/docker-compose.yml, then retry."
        exit 1
    fi
}

detect_iface() {
    detect_os
    if [ "$OS" = "linux" ]; then
        if ip link show eth0 &> /dev/null; then
            echo "eth0"
        else
            # fall back to the first non-loopback interface
            ip -o link show | awk -F': ' '$2 != "lo" {print $2; exit}'
        fi
    elif [ "$OS" = "mac" ]; then
        if ifconfig en0 &> /dev/null; then
            echo "en0"
        else
            echo ""
        fi
    else
        echo ""
    fi
}

# Prints "RX_BYTES TX_BYTES" for the given interface, or "0 0" if it can't be read.
get_iface_bytes() {
    local iface="$1"
    if [ -z "$iface" ]; then
        echo "0 0"
        return
    fi

    detect_os
    if [ "$OS" = "linux" ]; then
        if ! command -v ip &> /dev/null; then
            echo "0 0"
            return
        fi
        local out rx tx
        out=$(ip -s link show "$iface" 2>/dev/null)
        rx=$(echo "$out" | awk '/RX:/{getline; print $1; exit}')
        tx=$(echo "$out" | awk '/TX:/{getline; print $1; exit}')
        echo "${rx:-0} ${tx:-0}"
    elif [ "$OS" = "mac" ]; then
        if ! command -v netstat &> /dev/null; then
            echo "0 0"
            return
        fi
        local line rx tx
        line=$(netstat -bI "$iface" 2>/dev/null | grep "Link#" | head -1)
        rx=$(echo "$line" | awk '{print $7}')
        tx=$(echo "$line" | awk '{print $10}')
        echo "${rx:-0} ${tx:-0}"
    else
        echo "0 0"
    fi
}

bytes_to_mb() {
    awk -v b="$1" 'BEGIN { printf "%.2f", b / 1024 / 1024 }'
}

start_stack() {
    check_ports
    info "Starting datacenter-sim (this may take a few minutes on first run)..."
    cd "$INFRA_DIR"
    docker compose up --build -d
    info "All services starting in the background. Check status with: ./start.sh status"
    echo ""
    echo "  FastAPI:     http://localhost:8000/health"
    echo "  FastAPI docs: http://localhost:8000/docs"
    echo "  Locust:      http://localhost:8089"
    echo "  Prometheus:  http://localhost:9090"
    echo "  Grafana:     http://localhost:3000  (login: admin / admin)"
    echo ""
    info "To view live logs: cd infra && docker compose logs -f"
    info "To stop everything: ./start.sh stop"

    info "Waiting for FastAPI to become healthy before opening browser tabs..."
    for i in $(seq 1 30); do
        if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
            break
        fi
        sleep 2
    done

    open_url() {
        local url="$1"
        if command -v open &> /dev/null; then
            open "$url" 2>/dev/null            # macOS
        elif command -v xdg-open &> /dev/null; then
            xdg-open "$url" 2>/dev/null        # Linux desktop
        fi
    }

    if command -v open &> /dev/null || command -v xdg-open &> /dev/null; then
        info "Opening service URLs in your browser..."
        open_url "http://localhost:8000/docs"
        sleep 1
        open_url "http://localhost:8089"
        sleep 1
        open_url "http://localhost:9090"
        sleep 1
        open_url "http://localhost:3000"
    else
        info "No browser opener found (likely a headless VM) — open the URLs above manually."
    fi
}

stop_stack() {
    info "Stopping datacenter-sim..."
    cd "$INFRA_DIR"
    docker compose down
    info "Stopped."
}

status_stack() {
    cd "$INFRA_DIR"
    docker compose ps
}

run_stress_test() {
    local profile="${1:-normal_day}"

    if [ ! -f "$REPO_ROOT/profiles/${profile}.yaml" ]; then
        error "Unknown profile '$profile'. Available profiles:"
        ls "$REPO_ROOT/profiles" | sed 's/\.yaml$//' | sed 's/^/  - /'
        exit 1
    fi

    info "Checking FastAPI is up before starting stress test..."
    if ! curl -sf http://localhost:8000/health > /dev/null; then
        error "FastAPI is not responding at http://localhost:8000/health"
        error "Run ./start.sh first, wait for it to be healthy, then retry."
        exit 1
    fi

    info "Running stress test with profile: $profile (inside Docker, against fastapi service)"
    mkdir -p "$REPO_ROOT/results"
    cd "$INFRA_DIR"
    local run_log="$REPO_ROOT/results/run_${profile}.log"
    docker compose run --rm -T \
        -e LOCUST_PROFILE="profiles/${profile}.yaml" \
        -v "$REPO_ROOT/results:/code/results" \
        locust-worker \
        -f locustfile.py,loadshapes.py \
        --host http://fastapi:8000 \
        --headless \
        --csv "results/results_${profile}" 2>&1 | tee "$run_log"
    info "Stress test finished. Results saved to: $REPO_ROOT/results/"
}

run_playbook() {
    # Full playbook, per spec: baseline -> start -> healthy -> watchdog -> load -> final stats -> summary
    local profiles_csv="$1"
    IFS=',' read -ra PROFILES <<< "$profiles_csv"

    for p in "${PROFILES[@]}"; do
        if [ ! -f "$REPO_ROOT/profiles/${p}.yaml" ]; then
            error "Unknown profile '$p'. Available profiles:"
            ls "$REPO_ROOT/profiles" | sed 's/\.yaml$//' | sed 's/^/  - /'
            exit 1
        fi
    done

    IFACE=$(detect_iface)
    if [ -z "$IFACE" ]; then
        info "Could not detect a real network interface (eth0/en0) — network stats will show as 0."
    else
        info "Using network interface: $IFACE"
    fi

    ensure_docker
    start_stack
    start_watchdog

    for p in "${PROFILES[@]}"; do
        info "=== Running scenario: $p ==="
        read -r rx_before tx_before <<< "$(get_iface_bytes "$IFACE")"
        run_stress_test "$p"
        read -r rx_after tx_after <<< "$(get_iface_bytes "$IFACE")"
        print_summary "$p" "$REPO_ROOT/results/run_${p}.log" "$rx_before" "$tx_before" "$rx_after" "$tx_after"
    done

    stop_watchdog
    info "Playbook complete. Ran: $profiles_csv"
}

WATCHDOG_PID=""

start_watchdog() {
    if [ -f "$REPO_ROOT/monitor/crash_watch.py" ]; then
        info "Starting crash watchdog in the background..."
        python3 "$REPO_ROOT/monitor/crash_watch.py" &
        WATCHDOG_PID=$!
    else
        info "monitor/crash_watch.py not found — skipping watchdog."
    fi
}

stop_watchdog() {
    if [ -n "$WATCHDOG_PID" ]; then
        kill "$WATCHDOG_PID" 2>/dev/null
        wait "$WATCHDOG_PID" 2>/dev/null
        info "Crash watchdog stopped."
    fi
}

# SLA thresholds — placeholder defaults, confirm actual values with the team.
SLA_MAX_P95_LOGIN_MS=3000
SLA_MAX_FAILURE_RATE=5.0

print_summary() {
    local profile="$1"
    local run_log="$2"
    local rx_before="$3" tx_before="$4" rx_after="$5" tx_after="$6"
    local stats_csv="$REPO_ROOT/results/results_${profile}_stats.csv"
    local summary_file="$REPO_ROOT/results/summary_${profile}.txt"

    local req_count="N/A" fail_count="N/A" fail_rate="N/A" p95_login="N/A"

    if [ -f "$stats_csv" ]; then
        local agg_line
        agg_line=$(grep "^Aggregated\|,Aggregated," "$stats_csv" | tail -1)
        if [ -z "$agg_line" ]; then
            agg_line=$(tail -1 "$stats_csv")
        fi
        req_count=$(echo "$agg_line" | awk -F',' '{print $3}')
        fail_count=$(echo "$agg_line" | awk -F',' '{print $4}')
        if [ -n "$req_count" ] && [ "$req_count" != "0" ]; then
            fail_rate=$(awk -v f="$fail_count" -v r="$req_count" 'BEGIN { printf "%.2f", (f/r)*100 }')
        fi
    fi

    if [ -f "$run_log" ]; then
        p95_login=$(grep -E "^POST[[:space:]]+/login[[:space:]]" "$run_log" | tail -1 | awk '{print $8}')
        p95_login="${p95_login:-N/A}"
    fi

    local rx_mb tx_mb
    rx_mb=$(bytes_to_mb "$(( rx_after - rx_before ))")
    tx_mb=$(bytes_to_mb "$(( tx_after - tx_before ))")

    local sla_verdict="PASS"
    local sla_reason=""
    if [ "$p95_login" != "N/A" ] && awk -v p="$p95_login" -v max="$SLA_MAX_P95_LOGIN_MS" 'BEGIN{exit !(p>max)}'; then
        sla_verdict="FAIL"
        sla_reason="p95 login latency (${p95_login}ms) exceeded ${SLA_MAX_P95_LOGIN_MS}ms"
    fi
    if [ "$fail_rate" != "N/A" ] && awk -v f="$fail_rate" -v max="$SLA_MAX_FAILURE_RATE" 'BEGIN{exit !(f>max)}'; then
        sla_verdict="FAIL"
        sla_reason="${sla_reason:+$sla_reason; }failure rate (${fail_rate}%) exceeded ${SLA_MAX_FAILURE_RATE}%"
    fi

    {
        echo "======================================"
        echo " Stress test summary — $profile"
        echo "======================================"
        echo "Total requests:        $req_count"
        echo "Total failures:        $fail_count  (${fail_rate}%)"
        echo "p95 login latency:     ${p95_login} ms"
        echo "Network RX (${IFACE:-unknown}):     ${rx_mb} MB"
        echo "Network TX (${IFACE:-unknown}):     ${tx_mb} MB"
        echo "SLA verdict:           $sla_verdict${sla_reason:+ — $sla_reason}"
        echo "Results saved to:      $REPO_ROOT/results/"
        echo "======================================"
        echo "Note: SLA thresholds (p95 < ${SLA_MAX_P95_LOGIN_MS}ms, failure rate < ${SLA_MAX_FAILURE_RATE}%)"
        echo "      are placeholder defaults — confirm real values with the team."
        echo "Note: network stats use the real host interface (${IFACE:-none detected}),"
        echo "      not the Docker bridge — see known limitation on per-container breakdown."
    } | tee "$summary_file"

    info "Summary saved to: $summary_file"
}

case "${1:-start}" in
    -l)
        if [ -z "$2" ]; then
            error "Usage: ./start.sh -l <profile>[,<profile2>,...]"
            error "Example: ./start.sh -l normal_day"
            error "Example: ./start.sh -l normal_day,flash_event"
            exit 1
        fi
        run_playbook "$2"
        ;;
    start)
        ensure_docker
        start_stack
        ;;
    stop)
        stop_stack
        ;;
    status)
        status_stack
        ;;
    stress)
        run_stress_test "$2"
        ;;
    *)
        echo "Usage:"
        echo "  ./start.sh -l <profile>[,<profile2>,...]  → full playbook: baseline, start, watchdog, load, summary"
        echo "  ./start.sh start                          → just start services, no load test"
        echo "  ./start.sh stop                           → stop everything"
        echo "  ./start.sh status                         → show container status"
        echo "  ./start.sh stress <profile>                → run a load test only (services must already be up)"
        exit 1
        ;;
esac
