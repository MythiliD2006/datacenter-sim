from prometheus_client import Counter, Histogram, Gauge

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["endpoint", "method", "status_code"]
)

REQUEST_LATENCY = Histogram(
    "http_request_duration_ms",
    "Request latency in milliseconds",
    ["endpoint"],
    buckets=[10, 25, 50, 100, 200, 500, 1000, 2000, 5000]
)

ACTIVE_SESSIONS = Gauge(
    "active_sessions_total",
    "Number of currently active sessions"
)

LOGIN_SUCCESS = Counter(
    "login_success_total",
    "Successful logins"
)

LOGIN_FAILURE = Counter(
    "login_failure_total",
    "Failed login attempts"
)