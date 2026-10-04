#!/usr/bin/env python3
"""
setup_network.py

Reads ONE config file (network_config.json) and does everything:
  1. For each service, finds which interface currently has its MAC address
  2. Creates the macvlan network (once) on that interface
  3. Connects each service's container to it
  4. Prints the resulting IP/MAC for each container

This replaces typing the interface name by hand into docker commands.
Run with sudo, on the Linux machine (VM or real server) where the
containers are already running.

Usage:
    sudo python3 setup_network.py network_config.json
"""
import sys
import os
import json
import subprocess

try:
    import psutil
except ImportError:
    print("This script needs psutil. Install it with: sudo apt install -y python3-psutil")
    sys.exit(1)


def normalize_mac(mac):
    return mac.strip().lower()


def find_interface_by_mac(target_mac):
    target = normalize_mac(target_mac)
    for iface, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family == psutil.AF_LINK:
                if addr.address and normalize_mac(addr.address) == target:
                    return iface
    return None


def run(cmd, check=True):
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr.strip())
        if check:
            sys.exit(1)
    return result


def network_exists(name):
    result = run(["docker", "network", "inspect", name], check=False)
    return result.returncode == 0


def container_running(name):
    result = run(["docker", "ps", "--format", "{{.Names}}"], check=False)
    return name in result.stdout.split()


def main():
    if len(sys.argv) != 2:
        print(f"Usage: sudo python3 {sys.argv[0]} network_config.json")
        sys.exit(1)

    config_path = sys.argv[1]
    with open(config_path) as f:
        config = json.load(f)

    net = config["network"]
    services = config["services"]
    net_name = net["name"]

    # Step 1: resolve each service's interface, fresh, by MAC
    print("== Step 1: resolving interfaces by MAC ==")
    interfaces_needed = set()
    for service, info in services.items():
        iface = find_interface_by_mac(info["mac_address"])
        info["resolved_interface"] = iface
        if iface is None:
            print(f"[WARNING] {service}: no interface found for MAC {info['mac_address']}")
        else:
            print(f"{service}: MAC {info['mac_address']} -> interface {iface}")
            interfaces_needed.add(iface)

    if not interfaces_needed:
        print("No interfaces resolved. Nothing to do.")
        sys.exit(1)

    # Step 2: create the macvlan network once, on the resolved interface
    # (if multiple services resolve to different interfaces, this simple
    # version uses the first one found — ask sir if services should be
    # split across separate macvlan networks instead)
    parent_iface = sorted(interfaces_needed)[0]
    print(f"\n== Step 2: ensuring macvlan network '{net_name}' on {parent_iface} ==")
    if network_exists(net_name):
        print(f"  Network '{net_name}' already exists — reusing.")
    else:
        run([
            "docker", "network", "create", "-d", "macvlan",
            "--subnet", net["subnet"],
            "--gateway", net["gateway"],
            "--ip-range", net["ip_range"],
            "-o", f"parent={parent_iface}",
            net_name,
        ])

    # Step 3: connect each service's container
    print(f"\n== Step 3: connecting containers ==")
    for service, info in services.items():
        container = info["container_name"]
        if not container_running(container):
            print(f"[WARNING] {service}: container '{container}' is not running — skipping.")
            continue
        run(["docker", "network", "connect", net_name, container], check=False)

    # Step 4: report results
    print(f"\n== Step 4: results ==")
    result = run([
        "docker", "network", "inspect", net_name,
        "-f", "{{range .Containers}}{{.Name}} {{.IPv4Address}} {{.MacAddress}}\n{{end}}",
    ])

    # Save the resolved interfaces back into the config file for the record
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    print(f"\nUpdated {config_path} with resolved interfaces.")


if __name__ == "__main__":
    main()
