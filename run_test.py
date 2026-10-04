import sys
import os
import subprocess
import yaml

def main():
    # Check if user wants CLI input or a profile
    if "--users" in sys.argv:
        # Direct input mode - just ask for users
        try:
            users_idx = sys.argv.index("--users")
            users = int(sys.argv[users_idx + 1])
            
            # Auto-calculate everything else
            spawn_rate = 5  # default spawn rate
            ramp_duration = int(users / spawn_rate)  # auto-calc ramp time
            peak_duration = 300  # default 5 minutes at peak
            cooldown_duration = int(users / spawn_rate)  # auto-calc cooldown time
            
            print(f"Running test: {users} users")
            print(f"Ramp-up: {ramp_duration}s | Peak: {peak_duration}s | Cooldown: {cooldown_duration}s")
            print(f"Total duration: {(ramp_duration + peak_duration + cooldown_duration) / 60:.1f} minutes\n")
            
            # Create ad-hoc profile
            profile = {
                "stages": [
                    {"duration": ramp_duration, "users": users, "spawn_rate": spawn_rate},
                    {"duration": peak_duration, "users": users, "spawn_rate": spawn_rate},
                    {"duration": cooldown_duration, "users": 5, "spawn_rate": spawn_rate},
                ]
            }
            
            profile_path = os.path.join(os.getcwd(), "adhoc_profile.yaml")
            with open(profile_path, "w") as f:
                yaml.dump(profile, f)
            
            csv_prefix = f"results_{users}users"
            
        except (ValueError, IndexError):
            print("Usage: python run_test.py --users 100")
            print("   or: python run_test.py normal_day")
            sys.exit(1)
    
    else:
        # Profile mode (existing behavior)
        if len(sys.argv) != 2:
            print("Usage: python run_test.py <profile_name>")
            print("   or: python run_test.py --users 100")
            sys.exit(1)
        
        profile_name = sys.argv[1]
        profile_path = os.path.join("profiles", f"{profile_name}.yaml")
        
        if not os.path.exists(profile_path):
            print(f"Profile not found: {profile_path}")
            sys.exit(1)
        
        csv_prefix = f"results_{profile_name}"
    
    # Load and run
    env = os.environ.copy()
    env["LOCUST_PROFILE"] = profile_path
    
    with open(profile_path) as f:
        config = yaml.safe_load(f)
    
    total_duration = sum(stage["duration"] for stage in config["stages"])
    
    cmd = [
        "locust",
        "-f", "locustfile.py,loadshapes.py",
        "--host", "http://127.0.0.1:8000",
        "--headless",
        "--csv", csv_prefix,
        "--run-time", f"{total_duration + 30}s",
    ]
    
    subprocess.run(cmd, env=env)

if __name__ == "__main__":
    main()