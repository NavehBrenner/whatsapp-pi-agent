# Runbook 01 — Pi base setup

**Target:** Raspberry Pi 5, 8GB, 64-bit Raspberry Pi OS (Bookworm or later), always-on,
residential connection.

**Time:** ~45 min, mostly waiting on writes and updates.

---

## Why these choices

**64-bit OS is mandatory.** Waydroid needs arm64; a 32-bit userland won't run the Android
images at all.

**Residential IP is a design requirement, not a convenience.** See
[detection-model.md](../detection-model.md) L5 — a datacenter IP is a connection-level flag,
and it would be the only anomalous signal in an otherwise clean profile. Do not "temporarily"
move this to a VPS. Also: **don't route the Pi's traffic through a commercial VPN** — those
exit IPs are datacenter ranges and land in the same bucket.

**SSD or good USB3 storage, not a microSD.** Waydroid plus a live SQLite DB with WAL writes
continuously. SD cards die under this, and they die by corrupting rather than by stopping.

---

## 1. Flash and first boot

Raspberry Pi Imager → **Raspberry Pi OS (64-bit)**, Lite is fine (Waydroid needs a Wayland
compositor, but a headless-with-cage setup is covered in runbook 02; Desktop is easier for
the spike and you can strip it later).

In Imager's advanced options, set: hostname, your SSH public key, **disable password
auth**, locale/timezone, and Wi-Fi if not using Ethernet.

Prefer **Ethernet**. This box runs 24/7 and a dropped Wi-Fi link means missed messages and
possibly an expired companion session.

```bash
ssh <user>@<pi-host>
uname -m          # must print: aarch64
free -h           # confirm ~8GB
```

## 2. Update, and set a fixed address

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

Give the Pi a **DHCP reservation on the router** rather than a static IP on the Pi. Same
result, one place to change it, and no risk of a misconfigured static address locking you
out of a headless box.

## 3. Harden

Password auth is already off if you set it in Imager. Verify rather than assume:

```bash
sudo sshd -T | grep -E 'passwordauthentication|permitrootlogin|pubkeyauthentication'
# want: passwordauthentication no / permitrootlogin no (or prohibit-password) / pubkeyauthentication yes
```

Unattended security updates:

```bash
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
```

**Do not forward any ports from the router to this Pi.** Nothing in this design needs
inbound access from the internet. If you need to reach it away from home, use Tailscale or
WireGuard — not a port forward.

Optional but cheap:

```bash
sudo apt install -y ufw
sudo ufw allow from 192.168.0.0/16 to any port 22 proto tcp   # adjust to your LAN
sudo ufw --force enable
```

The bridge listens on localhost only, so it needs no rule.

## 4. Storage

If booting from SSD (recommended), you're done. If you must run rootfs on SD, at minimum
put the Waydroid data directory and any snapshots on external storage.

Reduce needless writes:

```bash
# journald: cap on-disk logs
sudo sed -i 's/^#\?SystemMaxUse=.*/SystemMaxUse=200M/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald
```

**No message content in logs, ever** — that rule starts here and is enforced in the reader.
See [threat-model.md](../threat-model.md) R4.

## 5. Power and reliability

Use the **official 27W USB-C PSU**. Undervoltage on a Pi 5 with an SSD attached causes
intermittent, extremely confusing failures.

Check afterwards:

```bash
vcgencmd get_throttled     # want throttled=0x0
```

Disable sleep/suspend so an always-on box stays on:

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

## 6. Time

signal-cli and WhatsApp both care about clock accuracy. Confirm NTP is syncing:

```bash
timedatectl status         # want: System clock synchronized: yes, NTP service: active
```

## 7. Base packages

```bash
sudo apt install -y git sqlite3 python3 python3-venv rsync
```

`sqlite3` is worth having on the box for poking at msgstore by hand during the spike.

## 8. Get the repo on the box

```bash
git clone <repo-url> ~/whatsapp-pi-agent
cd ~/whatsapp-pi-agent
./deploy/bootstrap.sh
```

`bootstrap.sh` is idempotent — safe to re-run.

---

## Done when

- [ ] `uname -m` → `aarch64`
- [ ] SSH key-only, password auth off, no inbound port forwards
- [ ] `vcgencmd get_throttled` → `0x0`
- [ ] `timedatectl` → synchronized
- [ ] Rootfs on SSD, or Waydroid data on external storage
- [ ] DHCP reservation set
- [ ] Repo cloned, `bootstrap.sh` run clean

**Next:** [02-waydroid-whatsapp.md](02-waydroid-whatsapp.md) — the spike. Do this before
writing any code.
