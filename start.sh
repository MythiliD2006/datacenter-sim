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
#   ./start.sh                    → installs prerequisites if needed, then starts everything
#   ./start.sh stop               → stops everything
#   ./start.sh status             → shows which containers are running
#   ./start.sh stress normal_day  → runs the normal-day load test profile
#   ./start.sh stress flash_event → runs the flash-event (spike) load test profile
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

start_stack() {
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
    docker compose run --rm \
        -e LOCUST_PROFILE="profiles/${profile}.yaml" \
        -v "$REPO_ROOT/results:/code/results" \
        locust-worker \
        -f locustfile.py,loadshapes.py \
        --host http://fastapi:8000 \
        --headless \
        --csv "results/results_${profile}"
    info "Stress test finished. Results saved to: $REPO_ROOT/results/"
}

case "${1:-start}" in
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
        echo "Usage: ./start.sh [start|stop|status|stress <profile>]"
        exit 1
        ;;
esac
