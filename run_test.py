import sys
import os
import subprocess

profile_name = sys.argv[1]
profile_path = os.path.join("profiles", f"{profile_name}.yaml")

if not os.path.exists(profile_path):
    print(f"Profile not found: {profile_path}")
    sys.exit(1)

print(f"Found profile at: {profile_path}")

# Copy the current environment variables, then add/override LOCUST_PROFILE
# so the subprocess we launch below (and loadshapes.py inside it) can read it
env = os.environ.copy()
env["LOCUST_PROFILE"] = profile_path

cmd = [
    "locust",
    "-f", "locustfile.py,loadshapes.py",
    "--host", "http://127.0.0.1:8000",
    "--headless",
    "--csv", f"results_{profile_name}",
]

subprocess.run(cmd, env=env)