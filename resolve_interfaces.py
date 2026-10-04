#!/usr/bin/env python3
"""
resolve_interfaces.py

Reads MAC address assignments from mac_mapping.env, finds which network
interface CURRENTLY owns each MAC address (works on both Mac and Linux,
and re-checks fresh every run so a post-reboot interface rename is caught),
and writes interface_map.json for assign_interfaces.sh to consume.

Usage:
    python3 resolve_interfaces.py mac_mapping.env
"""
import sys
import os
import json
import re

try:
    import psutil
except ImportError:
    print("This script needs psutil. Install it with:  pip3 install psutil")
    sys.exit(1)

MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")
SERVICES = ["FASTAPI", "LOCUST", "GRAFANA", "PROMETHEUS"]


def load_env(path):
    values = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip()
    return values


def save_env(path, values):
    with open(path, "w") as f:
        for k, v in values.items():
            f.write(f"{k}={v}\n")


def normalize_mac(mac):
    return mac.strip().lower()


def find_interface_by_mac(target_mac):
    """Search every interface currently on this machine for one matching
    this MAC address. Returns the interface name, or None if not found."""
    target = normalize_mac(target_mac)
    for iface, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            # psutil reports MAC as AF_LINK on macOS, AF_PACKET on Linux
            fam_name = str(addr.family)
            if "AF_LINK" in fam_name or "AF_PACKET" in fam_name:
                if addr.address and normalize_mac(addr.address) == target:
                    return iface
    return None


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <mac_mapping.env>")
        sys.exit(1)

    env_path = sys.argv[1]
    env = load_env(env_path)
    changed = False
    result = {}

    for service in SERVICES:
        mac_key = f"{service}_MAC"
        container_key = f"{service}_CONTAINER"

        mac = env.get(mac_key, "").strip()
        container = env.get(container_key, "").strip()

        while not mac or not MAC_RE.match(mac):
            mac = input(f"Enter MAC address for {service} (format aa:bb:cc:dd:ee:ff): ").strip()
            changed = True

        if not container:
            container = input(f"Enter container name for {service}: ").strip()
            changed = True

        env[mac_key] = mac
        env[container_key] = container

        iface = find_interface_by_mac(mac)
        if iface is None:
            print(f"[WARNING] No interface currently found with MAC {mac} for {service}.")
        else:
            print(f"{service}: MAC {mac} -> interface {iface}")

        result[service] = {
            "mac_address": mac,
            "container_name": container,
            "interface": iface,
        }

    if changed:
        save_env(env_path, env)

    out_path = os.path.join(os.path.dirname(os.path.abspath(env_path)), "interface_map.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
