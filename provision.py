#!/usr/bin/env python3
"""
Provision a Debian/Ubuntu VM as a Tailscale exit node.
Runs on the target VM as root. Idempotent — safe to re-run.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def log(msg):
    print(f"[provision] {msg}", flush=True)


def run(cmd, **kwargs):
    kwargs.setdefault("check", True)
    return subprocess.run(cmd, **kwargs)


def run_out(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def check_root():
    if os.geteuid() != 0:
        sys.exit("Error: must run as root (e.g. sudo python3 provision.py ...).")


def read_os_release():
    path = Path("/etc/os-release")
    if not path.exists():
        sys.exit("Error: /etc/os-release not found. Only Debian/Ubuntu is supported.")
    data = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            data[k.strip()] = v.strip().strip('"')
    return data


def check_os(os_data):
    os_id = os_data.get("ID", "").lower()
    id_like = os_data.get("ID_LIKE", "").lower()
    if os_id not in ("debian", "ubuntu") and not any(
        x in id_like for x in ("debian", "ubuntu")
    ):
        sys.exit(f"Error: unsupported OS '{os_id}'. Only Debian/Ubuntu is supported.")


def install_prereqs():
    log("Installing prerequisites (curl, gnupg, ethtool)...")
    run(["apt-get", "update", "-qq"])
    run(["apt-get", "install", "-y", "-qq", "curl", "gnupg", "ethtool"])


def add_tailscale_repo(distro, codename):
    keyring = Path("/usr/share/keyrings/tailscale-archive-keyring.gpg")
    sources = Path("/etc/apt/sources.list.d/tailscale.list")

    if keyring.exists() and sources.exists():
        log("Tailscale apt repo already configured.")
        return

    log(f"Adding Tailscale apt repo for {distro}/{codename}...")
    gpg_url = f"https://pkgs.tailscale.com/stable/{distro}/{codename}.gpg"
    result = run(["curl", "-fsSL", gpg_url], capture_output=True)
    keyring.write_bytes(result.stdout)
    keyring.chmod(0o644)

    sources.write_text(
        f"deb [signed-by={keyring}] "
        f"https://pkgs.tailscale.com/stable/{distro} {codename} main\n"
    )
    run(["apt-get", "update", "-qq"])


def install_tailscale():
    if shutil.which("tailscale"):
        log("tailscale already installed.")
        return
    log("Installing tailscale...")
    run(["apt-get", "install", "-y", "-qq", "tailscale"])


def configure_sysctl(ipv6):
    log("Configuring sysctl for IP forwarding...")
    conf = Path("/etc/sysctl.d/99-tailscale.conf")
    lines = ["net.ipv4.ip_forward = 1\n"]
    if ipv6:
        lines.append("net.ipv6.conf.all.forwarding = 1\n")
    conf.write_text("".join(lines))
    run(["sysctl", "--system"], capture_output=True)


def install_udp_gro_tweak():
    if not shutil.which("ethtool"):
        log("Skipping UDP GRO tweak (ethtool not found).")
        return
    try:
        out = run_out(["ip", "route", "show", "default"])
        parts = out.split()
        iface = parts[parts.index("dev") + 1] if "dev" in parts else None
    except (subprocess.CalledProcessError, ValueError, IndexError):
        iface = None

    if not iface:
        log("Skipping UDP GRO tweak (no default route interface detected).")
        return

    log(f"Installing UDP GRO throughput tweak for interface {iface}...")
    unit = f"""[Unit]
Description=Tailscale UDP GRO throughput tweak
After=network.target

[Service]
Type=oneshot
ExecStart=/sbin/ethtool -K {iface} rx-udp-gro-forwarding on rx-gro-list off
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""
    Path("/etc/systemd/system/tailscale-udp-gro.service").write_text(unit)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", "tailscale-udp-gro.service"])


def enable_tailscaled():
    log("Enabling and starting tailscaled...")
    run(["systemctl", "enable", "--now", "tailscaled"])


def tailscale_up(authkey, hostname, tags, routes, ssh, reset):
    log("Running tailscale up...")
    cmd = ["tailscale", "up", f"--auth-key={authkey}", "--advertise-exit-node"]
    if ssh:
        cmd.append("--ssh")
    if hostname:
        cmd.append(f"--hostname={hostname}")
    if tags:
        cmd.append(f"--advertise-tags={','.join(tags)}")
    if routes:
        cmd.append(f"--advertise-routes={','.join(routes)}")
    if reset:
        cmd.append("--reset")
    safe = [a.replace(authkey, "***") if authkey in a else a for a in cmd]
    log(f"Executing: {' '.join(safe)}")
    run(cmd)


def verify():
    log("Verifying exit node status...")
    try:
        out = run_out(["tailscale", "status"])
        log("tailscale status:")
        for line in out.splitlines():
            log(f"  {line}")
    except subprocess.CalledProcessError:
        log("Warning: could not retrieve tailscale status.")
    log("Provisioning complete.")


def resolve_authkey(args):
    if getattr(args, "authkey", None):
        return args.authkey
    if getattr(args, "authkey_file", None):
        return Path(args.authkey_file).read_text().strip()
    env_val = os.environ.get("TS_AUTHKEY", "")
    if env_val:
        return env_val
    sys.exit(
        "Error: no auth key provided. Use --authkey, --authkey-file, or set TS_AUTHKEY."
    )


def main():
    p = argparse.ArgumentParser(
        description="Provision a Debian/Ubuntu VM as a Tailscale exit node."
    )
    key_group = p.add_mutually_exclusive_group()
    key_group.add_argument(
        "--authkey", help="Tailscale auth key (or TS_AUTHKEY env var)"
    )
    key_group.add_argument(
        "--authkey-file", metavar="PATH", help="Path to file containing the auth key"
    )
    p.add_argument(
        "--hostname", default=None, help="Tailscale hostname (default: system hostname)"
    )
    p.add_argument(
        "--tag",
        action="append",
        dest="tags",
        default=[],
        metavar="TAG",
        help="Advertise tag, e.g. tag:exit (repeatable)",
    )
    p.add_argument(
        "--advertise-route",
        action="append",
        dest="routes",
        default=[],
        metavar="CIDR",
        help="Subnet route to advertise (repeatable)",
    )
    p.add_argument(
        "--no-ssh",
        action="store_false",
        dest="ssh",
        help="Disable Tailscale SSH (enabled by default)",
    )
    p.add_argument(
        "--no-ipv6-forwarding",
        action="store_false",
        dest="ipv6",
        help="Disable IPv6 forwarding (enabled by default)",
    )
    p.add_argument(
        "--reset", action="store_true", help="Pass --reset to tailscale up"
    )
    p.set_defaults(ssh=True, ipv6=True)
    args = p.parse_args()

    check_root()
    os_data = read_os_release()
    check_os(os_data)

    os_id = os_data.get("ID", "ubuntu").lower()
    if os_id not in ("debian", "ubuntu"):
        id_like = os_data.get("ID_LIKE", "").lower()
        os_id = "ubuntu" if "ubuntu" in id_like else "debian"

    codename = os_data.get("VERSION_CODENAME", "")
    if not codename:
        sys.exit("Error: could not determine VERSION_CODENAME from /etc/os-release.")

    authkey = resolve_authkey(args)

    install_prereqs()
    add_tailscale_repo(os_id, codename)
    install_tailscale()
    configure_sysctl(args.ipv6)
    install_udp_gro_tweak()
    enable_tailscaled()
    tailscale_up(authkey, args.hostname, args.tags, args.routes, args.ssh, args.reset)
    verify()


if __name__ == "__main__":
    main()
