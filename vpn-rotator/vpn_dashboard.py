#!/usr/bin/env python3
"""
Tactical VPN Telemetry & Rotator (Win95 edition), v2.

- Status is read from the real tunnel (via vpnctl), not from the last-saved state file.
- LEAK BLOCK = a real kill switch: the firewall drops all non-tunnel internet traffic while
  connected, while switching servers, and if the tunnel dies. DISCONNECT lifts it.
- IPv4/IPv6 are checked by route: "via tunnel", "blocked", or "LEAK".
- No root Python: everything privileged goes through the root-owned /usr/local/sbin/vpnctl.
"""
import os
import queue
import threading
import time
import tkinter as tk
from tkinter.scrolledtext import ScrolledText

import vpn_rotator as rot

# --- 1990s WINDOWS SYSTEM PALETTE ---
WIN_GRAY = "#C0C0C0"
TITLE_BAR_BG = "#000080"
TITLE_BAR_FG = "#FFFFFF"
TEXT_DARK = "#000000"
TERM_BG = "#FFFFFF"
TERM_FG = "#000000"
OK_GREEN = "#006400"
BAD_RED = "#A00000"
NAVY = "#000080"

# "MS Sans Serif"/"Courier New" don't exist on Linux (Tk fell back to a tiny font).
FONT_TITLE = ("DejaVu Sans", 12, "bold")
FONT_MAIN = ("DejaVu Sans", 11)
FONT_BOLD = ("DejaVu Sans", 11, "bold")
FONT_TERM = ("DejaVu Sans Mono", 10)

STATUS_POLL_MS = 5000
IP_POLL_S = 60
ROTATE_TIMEOUT_S = 120


class VPNDashboard:
    def __init__(self, root):
        self.root = root
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.title("Tactical VPN Telemetry & Rotator")
        self.root.geometry("760x820")
        self.root.minsize(680, 700)
        self.root.configure(bg=WIN_GRAY)

        self.auto_active = False
        self.interval_options = [5, 15, 30, 60]
        self.current_interval_idx = 1
        self.remaining_seconds = 0
        self.is_busy = False
        self.busy_since = 0.0
        self.node_buttons = {}
        self.leak_block = rot.leak_block_pref()
        self.last_ip_check = 0.0
        self.ui_q = queue.Queue()

        self._build_ui()
        self._drain_ui()
        self._clock_loop()
        self.poll_status(force_ip=True)

    # ---------- thread-safe UI ----------

    def ui(self, fn, *args):
        self.ui_q.put((fn, args))

    def _drain_ui(self):
        while True:
            try:
                fn, args = self.ui_q.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception as e:
                print(f"[!] UI error: {e}")
        self.root.after(100, self._drain_ui)

    def log(self, message):
        self.term.config(state=tk.NORMAL)
        self.term.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {message}\n")
        self.term.see(tk.END)
        self.term.config(state=tk.DISABLED)

    def tlog(self, message):  # from worker threads
        self.ui(self.log, message)

    # ---------- layout ----------

    def _row(self, parent, r, label):
        tk.Label(parent, text=label, bg=WIN_GRAY, fg=TEXT_DARK, font=FONT_MAIN).grid(
            row=r, column=0, sticky="w", padx=6, pady=2)
        v = tk.Label(parent, text="…", bg=WIN_GRAY, fg=NAVY, font=FONT_BOLD, anchor="w")
        v.grid(row=r, column=1, sticky="w", padx=10, pady=2)
        return v

    def _btn(self, parent, text, cmd, width=12, fg=TEXT_DARK, font=FONT_MAIN):
        return tk.Button(parent, text=text, bg=WIN_GRAY, fg=fg, width=width, font=font, command=cmd,
                         relief="raised", bd=2, activebackground="#D4D0C8")

    def _build_ui(self):
        title_bar = tk.Frame(self.root, bg=TITLE_BAR_BG)
        title_bar.pack(fill="x", padx=2, pady=2)
        tk.Label(title_bar, text=" 🖥️ Global Network Rotator - vpn_dashboard.py", bg=TITLE_BAR_BG,
                 fg=TITLE_BAR_FG, font=FONT_TITLE).pack(side="left", padx=4, pady=3)

        main = tk.Frame(self.root, bg=WIN_GRAY, bd=2, relief="sunken")
        main.pack(fill="both", expand=True, padx=6, pady=6)

        tel = tk.LabelFrame(main, text=" Active Telemetry Status ", bg=WIN_GRAY, fg=TEXT_DARK,
                            font=FONT_MAIN, bd=2, relief="groove")
        tel.pack(fill="x", padx=10, pady=8)
        self.lbl_node = self._row(tel, 0, "Tunnel:")
        self.lbl_hs = self._row(tel, 1, "Handshake:")
        self.lbl_ip4 = self._row(tel, 2, "Public IPv4:")
        self.lbl_ip6 = self._row(tel, 3, "Public IPv6:")
        self.lbl_dns = self._row(tel, 4, "DNS:")
        self.lbl_lock = self._row(tel, 5, "Leak block:")

        ctl = tk.LabelFrame(main, text=" Autopilot & Protection ", bg=WIN_GRAY, fg=TEXT_DARK,
                            font=FONT_MAIN, bd=2, relief="groove", padx=8, pady=8)
        ctl.pack(fill="x", padx=10, pady=6)
        for i, t in enumerate(["MODE", "TIMER", "EXTEND", "LEAK BLOCK", "STOP"]):
            tk.Label(ctl, text=t, bg=WIN_GRAY, fg=TEXT_DARK, font=FONT_MAIN).grid(row=0, column=i, padx=6)
        self.btn_auto = self._btn(ctl, "AUTO: OFF", self.toggle_auto, width=10)
        self.btn_auto.grid(row=1, column=0, padx=4)
        self.btn_interval = self._btn(ctl, f"⏱️ {self._interval()}M", self.cycle_interval, width=10)
        self.btn_interval.grid(row=1, column=1, padx=4)
        self.btn_add_time = self._btn(ctl, "+5 MINS", self.add_time, width=9, fg="#707070")
        self.btn_add_time.config(state=tk.DISABLED)
        self.btn_add_time.grid(row=1, column=2, padx=4)
        self.btn_lock = self._btn(ctl, "", self.toggle_leak_block, width=11, font=FONT_BOLD)
        self.btn_lock.grid(row=1, column=3, padx=4)
        self.btn_disc = self._btn(ctl, "DISCONNECT", self.start_disconnect, width=12, fg=BAD_RED, font=FONT_BOLD)
        self.btn_disc.grid(row=1, column=4, padx=4)
        self._paint_lock_button()

        man = tk.LabelFrame(main, text=" Manual Override (Select Node) ", bg=WIN_GRAY, fg=TEXT_DARK,
                            font=FONT_MAIN, bd=2, relief="groove", padx=8, pady=8)
        man.pack(fill="x", padx=10, pady=6)
        names = rot.list_configs()
        if not names:
            tk.Label(man, text="No .conf files discovered in ~/vpn_configs/", bg=WIN_GRAY, fg="red",
                     font=FONT_MAIN).pack(pady=8)
        for i, name in enumerate(names):
            b = self._btn(man, name.upper().replace("PROTON_", ""),
                          lambda n=name: self.start_rotation(target=n), width=10)
            b.grid(row=i // 5, column=i % 5, padx=4, pady=4, sticky="ew")
            self.node_buttons[name] = b

        lh = tk.Frame(main, bg=WIN_GRAY)
        lh.pack(fill="x", padx=10, pady=(6, 2))
        tk.Label(lh, text="System Output Log:", bg=WIN_GRAY, fg=TEXT_DARK, font=FONT_MAIN).pack(side="left")
        self.btn_copy = self._btn(lh, "Copy Log", self.copy_logs, width=9)
        self.btn_copy.pack(side="right")
        self._btn(lh, "Repair Network", self.start_netreset, width=14).pack(side="right", padx=6)
        self._btn(lh, "Check IP", lambda: self.poll_status(force_ip=True, reschedule=False), width=9).pack(
            side="right")

        wrap = tk.Frame(main, bg=WIN_GRAY, bd=2, relief="sunken")
        wrap.pack(fill="both", expand=True, padx=10, pady=(2, 10))
        self.term = ScrolledText(wrap, height=10, bg=TERM_BG, fg=TERM_FG, font=FONT_TERM, bd=0,
                                 highlightthickness=0, wrap="word")
        self.term.pack(fill="both", expand=True, padx=2, pady=2)
        self.log("Win32 Tactical Subsystem v2 initialized. Reading live tunnel state…")

    # ---------- status ----------

    def poll_status(self, force_ip=False, reschedule=True):
        def worker():
            st = rot.status()
            rep = None
            if force_ip or time.time() - self.last_ip_check > IP_POLL_S:
                self.last_ip_check = time.time()
                rep = rot.leak_report()
            self.ui(self._paint_status, st, rep)

        threading.Thread(target=worker, daemon=True).start()
        if reschedule:
            self.root.after(STATUS_POLL_MS, self.poll_status)

    def _paint_status(self, st, rep):
        if not st.get("ok"):
            self.lbl_node.config(text=f"helper error: {st.get('error', '?')}"[:60], fg=BAD_RED)
            return
        up, name, age = st.get("up"), st.get("name"), st.get("handshake_age")
        if up:
            stale = age is None or age > 180
            self.lbl_node.config(text=f"{(name or 'wg0').upper()}  ●  CONNECTED", fg=BAD_RED if stale else OK_GREEN)
            self.lbl_hs.config(text="none yet" if age is None else f"{age}s ago" + ("  ⚠ stale" if stale else ""),
                               fg=BAD_RED if stale else NAVY)
        else:
            self.lbl_node.config(text="DISCONNECTED", fg=BAD_RED)
            self.lbl_hs.config(text="—", fg=NAVY)
        self.lbl_dns.config(text=", ".join(st.get("dns", [])) + ("  (through tunnel)" if up else ""), fg=NAVY)
        lock = st.get("lock")
        if lock and not up:
            self.lbl_lock.config(text="ON — internet BLOCKED (no tunnel)", fg=BAD_RED)
        elif lock:
            self.lbl_lock.config(text="ON — nothing can leak", fg=OK_GREEN)
        else:
            self.lbl_lock.config(text="OFF" + ("  — traffic leaks if tunnel drops" if up else ""),
                                 fg=BAD_RED if up else NAVY)
        for n, b in self.node_buttons.items():
            active = up and n == name
            b.config(relief="sunken" if active else "raised", font=FONT_BOLD if active else FONT_MAIN,
                     bg="#E0E0E0" if active else WIN_GRAY)
        if rep:
            for fam, lbl in ((4, self.lbl_ip4), (6, self.lbl_ip6)):
                ip, v = rep[fam]
                if v == "blocked":
                    lbl.config(text="blocked" if up or lock else "unreachable", fg=OK_GREEN if up else NAVY)
                elif v == "tunnel":
                    lbl.config(text=f"{ip}  (via tunnel ✓)", fg=OK_GREEN)
                else:
                    lbl.config(text=f"{ip}  {'⚠ LEAK' if up else '(no VPN)'}", fg=BAD_RED)

    # ---------- actions ----------

    def _interval(self):
        return self.interval_options[self.current_interval_idx]

    def _set_busy(self, busy):
        self.is_busy = busy
        self.busy_since = time.time() if busy else 0.0
        state = tk.DISABLED if busy else tk.NORMAL
        for b in list(self.node_buttons.values()) + [self.btn_disc, self.btn_lock]:
            b.config(state=state)

    def _run_bg(self, label, fn):
        if self.is_busy:
            self.log("[!] Operation already in progress.")
            return
        self._set_busy(True)

        def worker():
            try:
                fn()
            except Exception as e:
                self.tlog(f"[ERROR] {label}: {e}")
            finally:
                self.ui(self._set_busy, False)
                self.last_ip_check = 0
                self.ui(lambda: self.poll_status(force_ip=True, reschedule=False))

        threading.Thread(target=worker, daemon=True).start()

    def start_rotation(self, target=None):
        if target and self.auto_active:
            self.log("[!] Manual override selected. Disengaging Autopilot.")
            self.toggle_auto(rotate=False)
        name = target or rot.next_config()
        if not name:
            self.log("[!] No servers configured.")
            return
        self.log(f"[⚡] {'Override' if target else 'Scheduled rotation'} → {name.upper()}")
        self._run_bg("rotation", lambda: rot.connect(name, leak_block=self.leak_block, log=self.tlog))

    def start_disconnect(self):
        if self.auto_active:
            self.toggle_auto(rotate=False)
        self.log("[*] Disconnecting…")
        self._run_bg("disconnect", lambda: rot.disconnect(log=self.tlog))

    def start_netreset(self):
        self.log("[*] Restarting NetworkManager (tunnel and leak block untouched)…")
        self._run_bg("netreset", lambda: self.tlog(
            "[*] Network restarted." if rot.vpnctl("netreset", timeout=90).get("ok") else "[!] Restart failed."))

    def toggle_leak_block(self):
        self.leak_block = not self.leak_block
        rot.save_prefs(leak_block=self.leak_block)
        self._paint_lock_button()

        def apply():
            st = rot.status()
            if st.get("up"):  # connected: apply now; disconnected: just remember for next connect
                r = rot.vpnctl("lock", "on" if self.leak_block else "off")
                self.tlog(f"[🛡️] Leak block {'ON' if self.leak_block else 'OFF'}"
                          + ("" if r.get("ok") else f" FAILED: {r.get('error')}"))
            elif not self.leak_block and st.get("lock"):
                rot.vpnctl("lock", "off")
                self.tlog("[*] Leak block OFF — internet restored.")
            else:
                self.tlog(f"[*] Leak block will be {'ON' if self.leak_block else 'OFF'} on next connect.")

        self._run_bg("leak block", apply)

    def _paint_lock_button(self):
        self.btn_lock.config(text="ON 🛡️" if self.leak_block else "OFF",
                             fg=OK_GREEN if self.leak_block else BAD_RED,
                             relief="sunken" if self.leak_block else "raised")

    def toggle_auto(self, rotate=True):
        self.auto_active = not self.auto_active
        if self.auto_active:
            self.remaining_seconds = self._interval() * 60
            self.btn_auto.config(text="AUTO: ON", fg="white", bg=NAVY, relief="sunken")
            self.btn_add_time.config(state=tk.NORMAL, fg=TEXT_DARK)
            self.log(f"[⚡] Autopilot ENGAGED. Every {self._interval()} minutes.")
            if rotate:
                self.start_rotation()
        else:
            self.btn_auto.config(text="AUTO: OFF", fg=TEXT_DARK, bg=WIN_GRAY, relief="raised")
            self.btn_add_time.config(state=tk.DISABLED, fg="#707070")
            self.btn_interval.config(text=f"⏱️ {self._interval()}M", fg=TEXT_DARK)
            self.log("[*] Autopilot DISENGAGED.")

    def cycle_interval(self):
        if not self.auto_active:
            self.current_interval_idx = (self.current_interval_idx + 1) % len(self.interval_options)
            self.btn_interval.config(text=f"⏱️ {self._interval()}M")

    def add_time(self):
        if self.auto_active:
            self.remaining_seconds += 300
            m, s = divmod(self.remaining_seconds, 60)
            self.log(f"[+] Added 5 minutes. Next rotation in {m}m {s}s")

    def _clock_loop(self):
        if self.is_busy and time.time() - self.busy_since > ROTATE_TIMEOUT_S:
            self.log("[!] Operation timed out; controls re-enabled.")
            self._set_busy(False)
        if self.auto_active:
            if self.is_busy:
                self.btn_interval.config(text="[ SYNCING ]", fg="blue")
            else:
                if self.remaining_seconds > 0:
                    self.remaining_seconds -= 1
                    m, s = divmod(self.remaining_seconds, 60)
                    color = "red" if self.remaining_seconds <= 60 else ("#B8860B" if self.remaining_seconds <= 180
                                                                       else TEXT_DARK)
                    self.btn_interval.config(text=f"⏱️ {m:02d}:{s:02d}", fg=color)
                if self.remaining_seconds == 0:
                    self.remaining_seconds = self._interval() * 60
                    self.start_rotation()
        self.root.after(1000, self._clock_loop)

    def copy_logs(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.term.get("1.0", tk.END))
        self.btn_copy.config(text="Copied!")
        self.root.after(2000, lambda: self.btn_copy.config(text="Copy Log"))

    def on_close(self):
        # Same as before: closing the dashboard drops the tunnel — and lifts the leak block so the
        # machine isn't left offline.
        print("[*] Dashboard closing. Disconnecting VPN and lifting leak block…")
        rot.disconnect()
        self.root.destroy()
        os._exit(0)


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    root = tk.Tk()
    VPNDashboard(root)
    root.mainloop()
