import os
import yaml # type: ignore
from locust import LoadTestShape # type: ignore

class MyShape(LoadTestShape):
    def __init__(self):
        super().__init__()
        profile_path = os.environ.get("LOCUST_PROFILE", "profiles/normal_day.yaml")
        with open(profile_path) as f:
            config = yaml.safe_load(f)
        self.stages = config["stages"]

    def tick(self):
        run_time = self.get_run_time()
        elapsed = 0
        for stage in self.stages:
            elapsed += stage["duration"]
            if run_time < elapsed:
                return (stage["users"], stage["spawn_rate"])
        return None