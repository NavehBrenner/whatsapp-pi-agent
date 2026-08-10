# Runbook 02 — Waydroid + WhatsApp companion link

# ⚠ THIS IS THE SPIKE

**Execute this manually, before writing any `src/` code.**

It answers [OPEN-QUESTIONS Q1](../OPEN-QUESTIONS.md): does the official WhatsApp app run
acceptably in Waydroid on a Pi 5, and will it complete a companion-device link there?

If the answer is no, [ADR 0002](../decisions/0002-waydroid-companion-device.md) is void and
the architecture changes fundamentally. Nothing downstream is worth building until this is
settled. **Record the outcome in [OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md) either way.**

**Prerequisite:** [01-pi-base-setup.md](01-pi-base-setup.md) complete.
**Time:** 1–3 hours, most of it staring at whether the UI renders.

---

## 0. What "pass" means

Write these down before you start, so you don't rationalise a partial result:

1. Waydroid boots and the Android UI is usable over the display or via `waydroid show-full-ui`.
2. The official WhatsApp APK installs and launches.
3. **Companion-device pairing completes** by scanning the QR from my phone.
4. Messages from a group chat arrive on the Waydroid instance.
5. `msgstore.db` exists on the host filesystem, opens as plain SQLite, and contains those
   messages.
6. It's still working after a reboot and 24 hours idle.

(3) is the risky one. (6) is the one people skip and regret.

---

## 0.5. Pi 5 kernel prerequisites — DO THIS FIRST

**Verified on hardware 2026-08-10.** Two Pi 5-specific kernel issues stop Waydroid dead.
Neither is mentioned in the Waydroid docs, and both produce failure signatures that look
like something else entirely. Fix both before `waydroid init`, then reboot once.

### (a) Page size: 16KB kernel vs 4KB Android images

Raspberry Pi OS boots Pi 5 with `kernel_2712.img`, which uses a **16KB page size**. Every
Android/LineageOS system image is compiled for **4KB pages**. The mismatch means Android's
`/init` segfaults the instant it's exec'd.

*Symptom:* container never reaches RUNNING; `waydroid session start` reports only
`OSError: container failed to start`. The real cause is visible **only** in an LXC debug
log you have to know to turn on:

```bash
sudo lxc-start -P /var/lib/waydroid/lxc -n waydroid -F -l DEBUG -o /tmp/lxc.log
grep -i 'signal' /tmp/lxc.log
#   Child <NNNN> ended on signal Segmentation fault(11)
```

*Check and fix:*

```bash
getconf PAGESIZE                     # 16384 = broken, 4096 = good
sudo cp /boot/firmware/config.txt /boot/firmware/config.txt.bak-preway
# add to /boot/firmware/config.txt:
#   kernel=kernel8.img
```

`kernel8.img` is the stock generic arm64 kernel and is already present. Cost: you lose the
Pi 5-tuned kernel's slightly better TLB behaviour. There is no alternative — Android will
not run on 16KB pages.

### (b) PSI and the memory cgroup are disabled

Android's `lmkd` (lowmemorykiller daemon) requires **PSI** (Pressure Stall Information).
Pi OS ships `CONFIG_PSI=y` with `CONFIG_PSI_DEFAULT_DISABLED=y`, so `/proc/pressure` does
not exist, and the firmware injects `cgroup_disable=memory` into the boot cmdline.

*Symptom:* far more misleading than (a) — Android boots **successfully** through
`system_server`, ActivityManager, and the boot animation, then dies after ~15 seconds. Only
`waydroid logcat` shows why:

```
E libpsi  : No kernel psi monitor support (errno=2)
I lowmemorykiller: No kernel memory.pressure_level support (errno=2)
E lowmemorykiller: Kernel does not support memory pressure events
I lowmemorykiller: exiting
E libc++abi: terminating          ← init dies, container stops
```

*Check and fix:*

```bash
ls /proc/pressure/                   # missing = broken
sudo cp /boot/firmware/cmdline.txt /boot/firmware/cmdline.txt.bak-prepsi
```

Append to the **single line** in `/boot/firmware/cmdline.txt` (it must stay one line — a
newline here makes the Pi unbootable):

```
 psi=1 cgroup_enable=memory cgroup_memory=1
```

`cgroup_enable=memory` overrides the firmware's `cgroup_disable=memory`; both end up on the
cmdline and the enable wins.

### Verify after reboot

```bash
getconf PAGESIZE                     # 4096
ls /proc/pressure/                   # cpu  io  memory
cat /sys/fs/cgroup/cgroup.controllers # must include: memory
```

All three correct → proceed. Any wrong → fix before continuing; the failures downstream
are much harder to read than these checks.

### Note on manual `lxc-start`

While debugging you may be tempted to drive LXC directly. It will fail with
`Failed to attach "vethXXXX" to bridge "waydroid0", bridge interface doesn't exist` —
the `waydroid0` bridge is created by the *session* manager. Always start via
`waydroid session start`, not `lxc-start`.

## 1. Install Waydroid

```bash
sudo apt install -y curl ca-certificates
curl -s https://repo.waydro.id | sudo bash
sudo apt install -y waydroid
```

Initialise. **Choose the image deliberately:**

```bash
# VANILLA — no Google Play Services. Try this first.
sudo waydroid init -s VANILLA
```

Why VANILLA first: fewer moving parts, no Google account, and WhatsApp does not require GMS
for basic messaging. GAPPS adds a Google sign-in requirement and drags in Play Integrity
attestation, which is precisely the thing most likely to fail here
([detection-model.md](../detection-model.md) L6). Only fall back to `-s GAPPS` if WhatsApp
demonstrably refuses to work without Play Services — and note it in OPEN-QUESTIONS if so,
because it changes the risk picture.

Start it:

```bash
sudo systemctl enable --now waydroid-container
waydroid session start &
waydroid show-full-ui
```

**Headless variant.** If running Pi OS Lite with no desktop, you need a Wayland compositor:

```bash
sudo apt install -y cage
# run the UI inside cage on tty1, or use weston --backend=headless-backend.so + VNC
```

For the spike, use a monitor or the Desktop image. Fighting headless rendering while also
testing whether WhatsApp works confuses two failure modes into one. Sort out headless
*after* you know the app runs.

### Checkpoint 1

```bash
waydroid status          # want: Session RUNNING, Container RUNNING
```

Android UI visible and responsive. If not, stop here — nothing downstream matters.

---

## 2. Install the official WhatsApp APK

**Pin the version.** Record what you install; the msgstore schema is tied to it
([OPEN-QUESTIONS Q2](../OPEN-QUESTIONS.md)).

Get the arm64-v8a APK for a specific version from APKMirror (or extract it from your own
phone — cleaner provenance if you have adb access to it).

```bash
# from the Pi, with the apk copied over
waydroid app install ~/WhatsApp-<version>-arm64-v8a.apk

# record what went in
waydroid shell dumpsys package com.whatsapp | grep -E 'versionName|versionCode'
```

Do **not** install a modified build (GB/FM/Yo WhatsApp or similar). That's detection layer
L3 and the hardest-banned category — see [detection-model.md](../detection-model.md).

### Checkpoint 2

WhatsApp launches and shows its welcome screen. If it crashes on launch or complains about
Play Services, note the exact message — that's the L6 answer arriving early.

---

## 3. Link as a companion device

**On the Waydroid instance:** WhatsApp → welcome screen → **"Link this device to a phone
number"** / "Link as companion device" (wording moves between versions). It should display
a QR code.

**On my phone (stays primary):** WhatsApp → Settings → **Linked devices** → **Link a
device** → scan the QR shown in Waydroid.

Notes:

- My phone must stay the primary device. This is a *companion*, not a migration.
- Max 4 companion devices; this consumes one slot.
- If the QR won't render or the camera can't read it off the Pi's screen, screenshot it from
  Waydroid (`waydroid shell screencap -p > /tmp/qr.png`) and display it larger.

### Checkpoint 3 — the actual question

- [ ] Pairing completes without an error.
- [ ] History syncs (companions receive message flow going forward, and some history).
- [ ] A message sent to a group now appears in Waydroid within seconds.

**If pairing fails**, capture everything before changing anything:

```bash
waydroid logcat | grep -iE 'whatsapp|integrity|attest|gms' | tee ~/spike-fail.log
```

Then read [OPEN-QUESTIONS Q1](../OPEN-QUESTIONS.md) for the fallback. **Do not start
spoofing build properties to defeat attestation** — that's the arms race this whole design
exists to avoid, and it converts a clean profile into a modified-client profile. The
sanctioned fallback is a cheap physical Android phone on the LAN as the companion, read over
ADB, which preserves ADRs 0003–0006 and only replaces 0002.

---

## 4. Verify the database on the host

This is [ADR 0003](../decisions/0003-local-db-read.md)'s premise, and it's cheap to confirm.

```bash
sudo ls -la ~/.local/share/waydroid/data/data/com.whatsapp/databases/
# NOTE: path is under the SESSION USER'S HOME, not /var/lib/waydroid (verified 2026-08-10)
# expect: msgstore.db, msgstore.db-wal, msgstore.db-shm, wa.db, ...
```

Snapshot all three msgstore files together — never read the live DB read-write, and never
hold a lock on a file WhatsApp depends on:

```bash
mkdir -p /tmp/spike && cd /tmp/spike
sudo cp ~/.local/share/waydroid/data/data/com.whatsapp/databases/msgstore.db* .
sudo cp ~/.local/share/waydroid/data/data/com.whatsapp/databases/wa.db .
sudo chown $USER: ./*
```

Confirm it's plain SQLite (no key, no crypt14 — that's backups only):

```bash
sqlite3 msgstore.db '.tables'
```

Find the message table — this is the schema-pinning question in the flesh. Modern builds use
`message`; pre-2021 used `messages`:

```bash
sqlite3 msgstore.db "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('message','messages');"
```

Read some rows, joining `wa.db` for names — **`msgstore.db` has JIDs, not names**:

```bash
sqlite3 msgstore.db <<'SQL'
ATTACH '/tmp/spike/wa.db' AS wa;
SELECT datetime(m.timestamp/1000,'unixepoch') AS ts,
       c.raw_string_jid                        AS chat,
       COALESCE(w.display_name, m.sender_jid_row_id) AS sender,
       substr(m.text_data,1,80)                AS excerpt
FROM message m
JOIN chat c   ON c.jid_row_id = m.chat_row_id
LEFT JOIN jid j ON j.`_id` = m.sender_jid_row_id
LEFT JOIN wa.wa_contacts w ON w.jid = j.raw_string
WHERE m.text_data IS NOT NULL
ORDER BY m.timestamp DESC LIMIT 20;
SQL
```

Column names vary by schema version — if this errors, `.schema message` and adjust. Getting
this query right *for the pinned version* is most of the reader's logic, so save the working
version.

### Checkpoint 4 — the WAL check

The one that catches people. Send a message to a group **right now**, then:

```bash
# re-snapshot ALL THREE files, then re-run the query above
sudo cp ~/.local/share/waydroid/data/data/com.whatsapp/databases/msgstore.db* /tmp/spike/
```

The new message should appear. If you copy only `msgstore.db` and omit `-wal`/`-shm`, it
won't — recent writes live in the WAL. That failure looks like "the agent is a few minutes
behind" and is miserable to diagnose later.

Also confirm the read-only URI path works, since that's what the reader will use:

```bash
sqlite3 'file:/tmp/spike/msgstore.db?mode=ro' 'SELECT COUNT(*) FROM message;'
```

`immutable=1` is **wrong** here — it tells SQLite to ignore the WAL.

---

## 5. Soak

Do not declare victory at checkpoint 4.

```bash
sudo systemctl enable waydroid-container
sudo reboot
```

After reboot: does the Waydroid session come back? Is WhatsApp still linked? (Session
auto-start needs a Wayland session — this is where the headless question gets real.)

Then leave it 24 hours and check:

- [ ] Still linked; messages still arriving.
- [ ] Memory stable — `free -h`, and remember signal-cli's JVM has to fit alongside.
- [ ] `vcgencmd measure_temp` sane under sustained load.
- [ ] No throttling: `vcgencmd get_throttled` → `0x0`.

Companion sessions expire if the primary phone is offline for ~14 days. Worth knowing, not
worth testing.

---

## Record the result

Update [OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md) Q1 with: pass/fail, image used
(VANILLA/GAPPS), WhatsApp `versionName`/`versionCode`, the working SQL query, and anything
surprising.

**On pass:** [ADR 0002](../decisions/0002-waydroid-companion-device.md) moves from "pending
spike verification" to Accepted, and `src/` can be written.

**On fail:** write an ADR superseding 0002 before writing any code.

**Next:** [03-signal-cli.md](03-signal-cli.md)
