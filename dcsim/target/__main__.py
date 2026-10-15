"""Run the bundled server under test: python -m dcsim.target --port N --accounts N"""
import argparse

import uvicorn

from .. import config, paths
from .app import create_app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--accounts", type=int, required=True)
    ap.add_argument("--home", default=None)
    ap.add_argument("--grpc-port", type=int, default=0)
    ap.add_argument("--parent-pid", type=int, default=0)
    a = ap.parse_args()
    paths.set_home(a.home)
    from ..lifeline import watch_parent
    watch_parent(a.parent_pid)
    cfg = config.load_config()
    app = create_app(cfg, a.accounts, data_dir=paths.scratch_dir() / "target", log_file=paths.logs_dir() / "target.log",
                     grpc_port=a.grpc_port or None)
    uvicorn.run(app, host=a.host or cfg["target"]["host"], port=a.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
