#!/usr/bin/env python3
"""
Tactical VPN Rotator — runs as the normal user; the only root step is the root-owned
/usr/local/sbin/vpnctl helper (sudoers allows that one file, nothing else).

  python3 vpn_rotator.py            rotate to the next server
  python3 vpn_rotator.py proton_jp  connect to a specific server
  python3 vpn_rotator.py --down     disconnect and lift the leak block
  python3 vpn_rotator.py --status

Keys stay in the encrypted vault; the live config is piped straight to vpnctl (never written
to a user-readable file). With the leak block on, the firewall drops all non-tunnel internet
traffic, including during a server switch and if the tunnel dies.
"""
import json
import os
import subprocess
import sys
import time

HOME_DIR = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME_DIR, "vpn_configs")
STATE_FILE = os.path.join(CONFIG_DIR, "rotator_state.json")
PREFS_FILE = os.path.join(HOME_DIR, ".vpn_dashboard.json")
VPNCTL = "/usr/local/sbin/vpnctl"

sys.path.insert(0, os.path.join(HOME_DIR, "LLM-Token-Vault"))
sys.path.insert(1, HOME_DIR)
from lumi_vault import secure_load_keys  # noqa: E402


def _say(log, msg):
    (log or print)(msg)


def vpnctl(*args, stdin=None, timeout=60):
    """Run the root helper; returns its JSON reply (or an error dict)."""
    try:
        r = subprocess.run(["sudo", "-n", VPNCTL, *args], input=stdin, capture_output=True, text=True,
                           timeout=timeout)
        return json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else \
            {"ok": False, "error": r.stderr.strip() or f"vpnctl exited {r.returncode}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"vpnctl {args[0]} timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_configs():
    if not os.path.isdir(CONFIG_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(CONFIG_DIR) if f.endswith(".conf"))


def load_prefs():
    try:
        with open(PREFS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_prefs(**kw):
    p = load_prefs()
    p.update(kw)
    with open(PREFS_FILE, "w") as f:
        json.dump(p, f)


def leak_block_pref():
    return load_prefs().get("leak_block", True)


def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(name, index):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"last_index": index, "last_config": name + ".conf"}, f)
    os.replace(tmp, STATE_FILE)


def next_config():
    names = list_configs()
    if not names:
        return None
    return names[(_load_state().get("last_index", -1) + 1) % len(names)]


def public_ip(family):
    """family 4 or 6. Returns the address, or None if that family can't reach the internet."""
    try:
        r = subprocess.run(["curl", f"-{family}", "-s", "--max-time", "6", "https://ifconfig.me/ip"],
                           capture_output=True, text=True, timeout=10)
        ip = r.stdout.strip()
        return ip if r.returncode == 0 and ip and len(ip) < 64 else None
    except Exception:
        return None


def route_dev(family):
    """Interface the kernel would use to reach the internet for this family (wg0 = tunneled)."""
    target = "1.1.1.1" if family == 4 else "2606:4700:4700::1111"
    try:
        out = subprocess.run(["ip", f"-{family}", "route", "get", target], capture_output=True, text=True,
                             timeout=5).stdout.split()
        return out[out.index("dev") + 1] if "dev" in out else None
    except Exception:
        return None


def leak_report():
    """{4|6: (address or None, verdict)} — verdict: 'tunnel', 'blocked' or 'LEAK'."""
    rep = {}
    for fam in (4, 6):
        ip = public_ip(fam)
        if not ip:
            rep[fam] = (None, "blocked")
        else:
            rep[fam] = (ip, "tunnel" if route_dev(fam) == "wg0" else "LEAK")
    return rep


def describe(rep):
    def one(fam):
        ip, v = rep[fam]
        return {"blocked": "blocked ✓", "tunnel": f"{ip} (via tunnel ✓)", "LEAK": f"{ip}  ⚠ LEAK"}[v]
    return f"IPv4: {one(4)} · IPv6: {one(6)}"


def status():
    return vpnctl("status", timeout=15)


def connect(name, leak_block=None, log=None):
    """Bring up `name`. With the leak block on, nothing leaks while switching. Returns True on success."""
    names = list_configs()
    if name not in names:
        _say(log, f"[!] Unknown server: {name}")
        return False
    if leak_block is None:
        leak_block = leak_block_pref()

    key = secure_load_keys().get(f"VPN_KEY_{name}")
    if not key:
        _say(log, f"[!] No private key in the vault for VPN_KEY_{name}")
        return False
    with open(os.path.join(CONFIG_DIR, name + ".conf")) as f:
        conf = f.read().replace("VAULT_INJECT", key)

    if leak_block:
        r = vpnctl("lock", "on")
        _say(log, "[🛡️] Leak block ON — no traffic outside the tunnel." if r.get("ok")
              else f"[!] Leak block failed: {r.get('error')}")
    _say(log, f"[*] Connecting to {name.upper()}…")
    r = vpnctl("up", name, stdin=conf)
    del conf
    if not r.get("ok"):
        _say(log, f"[!] Tunnel failed: {r.get('error')}")
        if leak_block:
            _say(log, "[🛡️] Leak block stays ON (internet blocked) until you connect or DISCONNECT.")
        return False

    # A WireGuard handshake proves the server answered.
    for _ in range(20):
        st = status()
        if st.get("handshake_age") is not None:
            break
        time.sleep(0.5)
    else:
        _say(log, "[!] No handshake from the server within 10 s (tunnel up but silent).")
        return False

    _say(log, f"[✓] {name.upper()} up. {describe(leak_report())}")
    _save_state(name, names.index(name))
    return True


def disconnect(log=None):
    r1 = vpnctl("down")
    r2 = vpnctl("lock", "off")
    ok = r1.get("ok") and r2.get("ok")
    _say(log, "[*] Disconnected. Leak block off — normal (unprotected) internet." if ok else
         f"[!] Disconnect problem: {r1.get('error') or r2.get('error')}")
    return ok


def main():
    a = sys.argv[1:]
    if a == ["--down"]:
        disconnect()
    elif a == ["--status"]:
        print(json.dumps(status(), indent=2))
    else:
        target = a[0].replace(".conf", "") if a else next_config()
        if not target:
            print(f"[!] No .conf files in {CONFIG_DIR}")
            sys.exit(1)
        sys.exit(0 if connect(target) else 1)


if __name__ == "__main__":
    main()
