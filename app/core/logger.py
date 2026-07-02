import logging
import json
from datetime import datetime, timezone
from pathlib import Path

Path("logs").mkdir(exist_ok=True)

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        # attach any extra fields passed in
        for key in ["endpoint", "method", "status_code",
                    "latency_ms", "user", "user_type"]:
            if hasattr(record, key):
                log[key] = getattr(record, key)

        if record.exc_info:
            log["exception"] = self.formatException(record.exc_info)

        return json.dumps(log)

def get_logger(name: str = "datacenter-sim") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured

    logger.setLevel(logging.INFO)

    # file handler — persisted to host via Docker volume
    file_handler = logging.FileHandler("logs/server.log")
    file_handler.setFormatter(JSONFormatter())

    # console handler — shows in Docker logs too
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(JSONFormatter())

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger