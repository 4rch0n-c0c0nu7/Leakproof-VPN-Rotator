# 🪟 90s Windows-Style VPN Dashboard & Rotator

A lightweight, retro-styled Python control center designed for embedded cyberdecks and Linux field units. It provides a classic Windows-inspired graphical interface to manage your VPN connections, execute automated IP/config rotation, and handle robust network kill-switch recovery on the fly.

## 🚀 Features

* **Retro 90s UI Aesthetic:** Classic Windows desktop styling built to run smoothly on small-form-factor screens and custom hardware displays (like 7-inch HDMI displays).
* **Automated VPN Rotator:** Backend rotation scripts (`vpn_rotator.py`) to cycle through configs and maintain stealth.
* **Kill-Switch Recovery:** Built-in safeguards and network recovery handlers to ensure your real IP never leaks if a tunnel drops.
* **NetworkManager Integration:** Seamlessly interfaces with `nmcli` and system network utilities on Raspberry Pi OS.

## 🛠️ Components

* `vpn_dashboard.py` — The core 90s-styled graphical interface script.
* `vpn_rotator.py` — The background service handling connection health and configuration rotation.

## ⚙️ Quick Start

1. Clone the repository:
   ```bash
   git clone [https://github.com/4rch0n-c0c0nu7/vpn-dashboard.git](https://github.com/4rch0n-c0c0nu7/vpn-dashboard.git)
   cd vpn-dashboard
