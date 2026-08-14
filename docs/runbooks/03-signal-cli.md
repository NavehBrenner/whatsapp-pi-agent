# Runbook 03 — signal-cli control channel

Sets up the assistant's Signal account and the JSON-RPC daemon it speaks through.
Implements [ADR 0004](../decisions/0004-signal-control-channel.md).

**Prerequisite:** [Q3](../OPEN-QUESTIONS.md) answered — you have a dedicated number.
**Blocked by:** nothing. Can be done in parallel with the spike, but don't bother until the
spike passes.
**Time:** ~30 min.

---

## 0. The number

[ADR 0004](../decisions/0004-signal-control-channel.md) requires a **dedicated number
registered as its own Signal account** — not signal-cli linked as a device on my personal
account. Separate identity, separate keys, revocable independently, and a clean two-party
chat instead of note-to-self.

Signal blocks many VoIP ranges (undocumented, changes). If registration fails with
"invalid number," the number is in a blocked range — get a different one rather than
retrying. See [Q3](../OPEN-QUESTIONS.md).

## 1. Install

**signal-cli 0.14.7 needs JRE 25.** Raspberry Pi OS Bookworm ships openjdk-17 and has no
backports configured, so the JRE comes from Adoptium:

```bash
sudo install -d -m 0755 /etc/apt/keyrings
wget -qO- https://packages.adoptium.net/artifactory/api/gpg/key/public |
  sudo gpg --dearmor -o /etc/apt/keyrings/adoptium.gpg
echo "deb [signed-by=/etc/apt/keyrings/adoptium.gpg] https://packages.adoptium.net/artifactory/deb bookworm main" |
  sudo tee /etc/apt/sources.list.d/adoptium.list
sudo apt update && sudo apt install -y temurin-25-jre
```

```bash
SIGNAL_CLI_VERSION=0.14.7        # pin it; record what you used
cd /tmp
wget "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz"
sudo tar xf "signal-cli-${SIGNAL_CLI_VERSION}.tar.gz" -C /opt
sudo ln -sf "/opt/signal-cli-${SIGNAL_CLI_VERSION}/bin/signal-cli" /usr/local/bin/signal-cli
signal-cli --version
```

### 1a. The aarch64 native library — required, and it fails late

signal-cli bundles native libsignal builds for macOS arm64 and Linux **x86_64 only**. There
is no `libsignal_jni_aarch64.so` in the jar, and the GraalVM native build is x86_64 too.

The trap: `signal-cli --version` and `listAccounts` work fine without it. You find out at
`register`, after you've spent a captcha.

```bash
# 1. Find the EXACT libsignal version this signal-cli expects.
ls /opt/signal-cli-${SIGNAL_CLI_VERSION}/lib | grep libsignal-client   # e.g. 0.99.1

# 2. Fetch the matching prebuilt from exquo/signal-libs-build (needs glibc 2.30+;
#    Bookworm has 2.36).
LIBSIGNAL=0.99.1
cd /tmp
wget "https://github.com/exquo/signal-libs-build/releases/download/libsignal_v${LIBSIGNAL}/libsignal_jni.so-v${LIBSIGNAL}-aarch64-unknown-linux-gnu.tar.gz"
tar xf libsignal_jni.so-v${LIBSIGNAL}-aarch64-unknown-linux-gnu.tar.gz

# 3. Sanity-check it before trusting it.
file libsignal_jni.so     # want: ELF 64-bit LSB shared object, ARM aarch64
ldd  libsignal_jni.so     # every dependency must resolve

# 4. Add it to the jar under the name libsignal looks for, keeping the original.
JAR=/opt/signal-cli-${SIGNAL_CLI_VERSION}/lib/libsignal-client-${LIBSIGNAL}.jar
cp libsignal_jni.so libsignal_jni_aarch64.so
sudo cp "$JAR" "$JAR.orig"
sudo zip -j -q "$JAR" libsignal_jni_aarch64.so
unzip -l "$JAR" | grep -i '\.so'
```

**The version must match the jar exactly.** Every signal-cli upgrade means redoing this with
the new libsignal version, or the daemon stops working.

## 2. Register

```bash
export ASSISTANT_NUMBER="+441234567890"    # the dedicated number, E.164

signal-cli -a "$ASSISTANT_NUMBER" register
# add --voice if SMS doesn't arrive — Signal calls and reads the code aloud.
# Landlines usually work over voice and are usually not in blocked VoIP ranges.
```

You may be given a captcha. If so:

1. Open <https://signalcaptchas.org/registration/generate.html>
2. Solve it, copy the `signalcaptcha://...` link
3. `signal-cli -a "$ASSISTANT_NUMBER" register --captcha "signalcaptcha://..."`

Then verify:

```bash
signal-cli -a "$ASSISTANT_NUMBER" verify 123456
signal-cli -a "$ASSISTANT_NUMBER" updateProfile --given-name "Assistant"
```

**Set a registration PIN** and store it in your password manager. Without it the number can
be re-registered by whoever holds it next.

```bash
signal-cli -a "$ASSISTANT_NUMBER" setPin <pin>
```

### Checkpoint

Send yourself a message and confirm it arrives:

```bash
signal-cli -a "$ASSISTANT_NUMBER" send -m "hello from the pi" "+<my-personal-number>"
```

Reply from your phone, then:

```bash
signal-cli -a "$ASSISTANT_NUMBER" receive
```

Both directions working = channel is live.

## 3. Back up the account state

Do this **after** section 4 — the state has to be at `/var/lib/wpa-signal/` first.

`/var/lib/wpa-signal/` holds the account's key material, and losing it and leaking it are
both bad in ways that don't overlap. Lose it: re-registration, which means another captcha,
another SMS, and a **new identity key that every contact sees as a safety-number change**.
Leak it: someone else can *be* the assistant — read the control channel and send commands
into it.

The mechanism is [`deploy/backup-signal.sh`](../../deploy/backup-signal.sh), run weekly by
[`wpa-signal-backup.timer`](../../deploy/systemd/wpa-signal-backup.timer).

### 3a. A key the Pi cannot read

Generate the keypair **on the machine that would perform a restore** — not on the Pi. A
backup encrypted with a key sitting next to it protects against disk loss and nothing else.

```bash
# On the WSL box.
age-keygen -o ~/.wpa-signal-backup.age.key      # 0600, and it stays here
```

Put the `AGE-SECRET-KEY-1…` line **in the password manager**, next to the registration PIN.
Only the `age1…` public key goes to the Pi. The Pi can then write backups it cannot read,
which is the whole design: a Pi compromise gets the live account, but not the archive of
every previous generation of it.

### 3b. Where it lands

A **Backblaze B2 bucket** — off the Pi and off the WSL box, which is the requirement. The
WSL box is not a backup target; it is a second thing that can die. The bucket is private,
the application key is scoped to it alone, and at 1.6 MB a week the 10 GB free tier holds
roughly a century of generations.

Create the bucket and a bucket-scoped application key, then on the Pi:

```bash
sudo install -m 0600 -o root -g root /dev/null /etc/wpa-signal-backup.env
sudoedit /etc/wpa-signal-backup.env
```

```ini
AGE_RECIPIENT=age1…                     # public key from 3a
BACKUP_REMOTE=wpabackup:<bucket>/signal
RCLONE_CONFIG_WPABACKUP_TYPE=b2
RCLONE_CONFIG_WPABACKUP_ACCOUNT=<keyID>
RCLONE_CONFIG_WPABACKUP_KEY=<applicationKey>
```

rclone builds the remote out of those `RCLONE_CONFIG_WPABACKUP_*` variables, so there is no
second config file to keep `0600` and no interactive `rclone config` to reproduce. One
root-owned file, same pattern as `/etc/wpa-signal.env`.

**None of this is in the repo.** The repo is public and the application key is live.

### 3c. Install the timer

```bash
sudo apt install -y age rclone
sudo install -m 0644 deploy/systemd/wpa-signal-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wpa-signal-backup.timer
sudo systemctl start wpa-signal-backup.service     # take the first one now
```

The service **stops `signal-cli` for the duration of the copy**. The daemon holds the
account lock and rewrites session state as messages arrive, so a live copy is not
guaranteed coherent. That is ~30s of control channel downtime a week — signal-cli needs
~19s to get back to a listening socket, and messages sent meanwhile arrive on reconnect.

Retention is by filename: each blob is dated and nothing is ever overwritten, so a bad
backup cannot destroy the previous good one. Four generations are kept on the Pi; the
remote keeps everything.

**`GODEBUG=netdns=cgo` in the unit is load-bearing on this network.** rclone is Go, and Go's
built-in resolver cannot resolve `api.backblazeb2.com` through this router — every lookup
returns `no such host` while `getent hosts` resolves the same name every time. Verified
2026-08-11: pure-Go failed 3/3, cgo succeeded immediately. Without it the weekly backup fails
with what reads like a Backblaze outage, on a unit nobody watches. If backups start failing
on a different network, check this before believing the error.

### 3d. The restore drill — run it, don't assume it

A backup nobody has restored is a hypothesis. Restoring happens on the WSL box, because that
is where the private key is.

**The restore machine needs three things it does not have by default**, and finding that out
during an actual outage is the bad time to find it out:

| | |
|---|---|
| `age` + `rclone` | single binaries, `~/.local/bin/` — no root needed |
| **JRE 25** + signal-cli 0.14.7 | `~/.local/opt/` — the box had Java 8, which cannot run signal-cli at all |
| The **B2 key**, in the password manager | the age key opens the blob; without bucket credentials you never get the blob |

Unlike the Pi, the x86_64 signal-cli needs no libsignal surgery — the bundled native library
is the right architecture. Section 1a is an arm64 problem only.

```bash
export JAVA_HOME=~/.local/opt/jdk-25.0.4+7-jre
export RCLONE_CONFIG_WPABACKUP_TYPE=b2 \
       RCLONE_CONFIG_WPABACKUP_ACCOUNT=<keyID> RCLONE_CONFIG_WPABACKUP_KEY=<applicationKey>

blob=wpa-signal-2026-08-11.tar.gz.age
scratch=$(mktemp -d); cd "$scratch"
rclone --config "" copy "wpabackup:<bucket>/signal/$blob" .
age -d -i ~/.wpa-signal-backup.age.key "$blob" | tar xzf - -C .
signal-cli --config "$scratch/wpa-signal" listAccounts
rm -rf "$scratch"        # it is the account in plaintext; do not leave it in /tmp
```

Pass the credentials as environment variables rather than writing an rclone config file, so
a bucket key does not outlive the drill in a dotfile.

`listAccounts` reporting the assistant's number is the pass condition. **Record the output** —
"restored successfully" means nothing six months later if nobody wrote down what it looked
like.

**Drill run 2026-08-11**, from the B2 copy rather than a Pi-local file, so the download path
was exercised too:

```
$ rclone --config "" copy wpabackup:pi-agent-signal-backup/signal/wpa-signal-2026-08-11.tar.gz.age .
$ ls -l
wpa-signal-2026-08-11.tar.gz.age  816.7K
$ signal-cli --config "$scratch/wpa-signal" listAccounts
Number: +972552645702
```

That is the whole pass condition: the number, from a blob the Pi wrote and cannot read,
opened on a machine that has never held the account.

**Do not `receive`, `send`, or start a daemon against the restored copy.** Signal allows the
account one primary device. A restored copy running while the Pi still runs is not a hot
spare — it is two clients claiming one identity, and they will fight over the account. A
restore is for when the Pi is *gone*.

**The registration PIN is not in the backup** and is not derivable from it. Losing the PIN
and the number loses the account regardless of how good the backups are. Password manager.

## 4. Run as a daemon

JSON-RPC over a unix socket for the gate, and over loopback HTTP for the gateway — nothing
listens on a routable address, which is not the same as nothing listening (see 4b). The unit lives in the repo
at [`deploy/systemd/signal-cli.service`](../../deploy/systemd/signal-cli.service); install it
with:

```bash
sudo deploy/install-signal.sh +972XXXXXXXXX
```

That creates the `wpa-signal` system user (still the daemon's *group*), migrates the account
state out of whichever home directory `register` left it in, writes the number to
`/etc/wpa-signal.env`, installs the firewall rule, and enables the unit. **It now refuses to
run until the `openclaw` user exists** — since NVB-27 the daemon runs at that uid, so the
gateway (runbook 04) is a prerequisite of the Signal install rather than a later step.

Three things in that unit are load-bearing:

**`--no-receive-stdout`, and it is not optional.** In daemon mode signal-cli prints every
received message — body included — to stdout, and under systemd stdout is journald. Verified
on hardware 2026-08-11: the first test message landed in the system log in plaintext.
Messages still reach JSON-RPC clients; they just stop being logged. `--scrub-log` redacts
identifiers from everything else, so the account number appears as `+**********02`.

This matters more than a tidy log. The reader deliberately writes to a file rather than
stdout to keep message content out of journald ([threat model R4](../threat-model.md)); a
chatty control channel undoes that from the other end. And in M4 this channel carries the
confirmation prompts, which by design describe the action about to be taken with real
credentials.

**The number is in `/etc/wpa-signal.env`, not in the unit.** Root-owned, `0640`, readable by
`wpa-signal`. A phone number is not a secret, but this repo is public and publishing a
working number invites traffic at exactly the endpoint that triggers privileged actions.

**Its own user — until NVB-27, which is the next section.** The account state directory *is*
the account: anyone who can read it can be the assistant, and it has no business sitting in a
human's home directory. It now sits under the *gateway's* uid instead, for a reason worth
reading before assuming that is a mistake.

## 4a. The uid it runs as, and why it is the gateway's (NVB-27)

`User=openclaw`, `Group=wpa-signal`. Only the user moved.

OpenClaw delivers a generated image by handing this daemon a **path** under
`~openclaw/.openclaw/media/outbound`, and re-asserts `0700` on `media` on every generation —
measured, `0710` before a run and `0700` after. So a group grant is undone before the send, a
watcher racing the chmod is not a design, and the only process that can read the attachment is
one running as `openclaw`. Without this, `image_generate` fails at the last hop with
`AttachmentInvalidException … (Permission denied)` after appearing to work.

The cost is real and is recorded in the
[ADR 0006 amendment](../decisions/0006-two-process-privilege-split.md): the gateway uid can
now read the account key material. Two costs it *looks* like it also has were already paid —
the gateway has always received every inbound envelope, and the JSON-RPC port was open to
every local uid until the firewall rule below.

**Keeping `Group=wpa-signal` is what makes this cheap.** `RuntimeDirectory` and
`StateDirectory` follow `User:Group`, so the socket stays `openclaw:wpa-signal` and
`wpa-gate.service` needs no change at all. `StateDirectoryMode=0700` leaves the group no
execute bit, so the account directory is no more readable to the gate than before.

```bash
sudo systemctl stop signal-cli.service          # BEFORE the chown, not after
sudo chown -R openclaw:wpa-signal /var/lib/wpa-signal
sudo chgrp openclaw /etc/wpa-signal.env
sudo systemctl start signal-cli.service
```

Two traps:

- **systemd will not do the chown for you.** It sets ownership on a `StateDirectory` it
  creates, not on a pre-existing tree. Skip the `chown -R` and the daemon starts and cannot
  read its own account.
- **Stop first.** Between the chown and the restart, an inbound message meets `EACCES` on the
  account store. It is a small window and it is on the one directory worth being careful with.

`ProtectHome=yes` is not in the way: it covers `/home`, `/root` and `/run/user`, and
openclaw's `HOME` is `/var/lib/openclaw`.

Verify:

```bash
ps -o user,pid -p "$(systemctl show signal-cli -p MainPID --value)"   # openclaw
sudo ls -l /run/wpa-signal/socket                    # srwxrwx--- openclaw wpa-signal
sudo journalctl -u signal-cli | grep 'Accepted new client'
#   → UnixDomainPrincipal[user=wpa-gate, group=wpa-gate]
```

## 4b. The JSON-RPC port is not protected by being on loopback

The unit also passes `--http 127.0.0.1:8081`, because OpenClaw's Signal plugin speaks nothing
else (`wpa-gate` uses the unix socket on the same daemon). **A loopback TCP port has no owner
check.** From the NVB-20 spike until NVB-27 found it, any local uid could send as the
assistant and read the whole inbound stream, with no credential — on the most trusted channel
in the system. The drop-in that opened the port said so in its own comment and nothing was
done about it for two days.

`wpa-signal-firewall.service` applies `deploy/nftables/wpa-signal-8081.nft`, which admits uid
0 and uid 991 (`openclaw`) and drops everything else. It is a `Wants=` of the daemon, not a
`Requires=`: a firewall failure should be a visible failed unit, not a dead control channel.

Three things in that file are load-bearing, and each fails the other way in silence:

| | |
|---|---|
| **no `flush ruleset`** | Waydroid's bridge lives in `table inet lxc`; flushing takes Android's networking with it |
| **output hook, not input** | `meta skuid` is the originating socket's owner and exists only on locally generated packets — on input the rule matches nothing and allows everything |
| **`oif "lo"`** | without it, every other uid loses outbound connections to port 8081 on *any* host |

```bash
sudo nft list table inet wpa_signal
curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' --max-time 6 \
  http://127.0.0.1:8081/api/v1/rpc -X POST          # as you: 000, times out
sudo -u openclaw curl -s -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1:8081/api/v1/rpc -X POST          # as openclaw: 415, instantly
```

A `415` is a pass: it means the request reached signal-cli and it disliked the content type.

Cap the JVM heap (`-Xmx256m` via `JAVA_OPTS` — the launcher honours `JAVA_OPTS` and
`SIGNAL_CLI_OPTS`, not `JAVA_TOOL_OPTIONS`). It'll happily take more than it needs, and
Waydroid wants the 8GB.

## 5. Trust boundary

The Signal channel is the **trusted** side of the system
([threat-model.md](../threat-model.md)). Three rules for whatever consumes it:

- **Verify the sender.** Only a configured principal triggers the privileged agent — check
  the sender against the allowlist before anything runs, every time. One principal today;
  the list is what refuses everyone else, and it may hold more than one row
  ([ADR 0007](../decisions/0007-principals-on-the-control-channel.md)).
- **A trigger is a `dataMessage` with a real body — not any envelope.** The receive stream
  also carries `typingMessage` and `receiptMessage` envelopes, observed on hardware
  2026-08-11: a plain "someone is typing" arrives as a `receive` notification with no
  `dataMessage` at all. An agent that fires on every `receive` is invocable by a typing
  indicator, which anyone who knows the number can produce at will, and no amount of sender
  checking helps if the check runs on an envelope that carries no command. The daemon cannot
  filter these — it has `--ignore-attachments`, `--ignore-stories`, `--ignore-avatars` and
  `--ignore-stickers`, and nothing for typing or receipts. So the consumer must require
  `envelope.dataMessage.message` to be non-empty before it looks at anything else.
- **Confirmation replies are commands too.** A `YES` that authorises an action must be
  matched to the specific pending action it's answering, not treated as a global "proceed."

All three are implemented by the **trigger gate**, `src/gate/signal.py`, running as
`wpa-gate.service` — deterministic host code with no model in it, the only thing that ever
forwards a message onward. Rules 1 and 2 are hard refusals there, with real-envelope fixture
tests in `tests/fixtures/signal/`; rule 3 survives as `reply_to` on the emitted command, the
id of the quoted message, which is what M4's pending-action registry matches on.

Two consequences for this runbook:

- `signal-cli.service` runs with **`UMask=0007`** so the socket is created `srwxrwx---` and
  the gate can reach it as a member of the `wpa-signal` group. Group-*writable* is the point:
  `connect(2)` on a unix socket needs write permission, and `0027` fails `EACCES` in a way
  that looks exactly like a socket that is not there yet. The gate deliberately does not run
  *as* the daemon's user: `/var/lib/wpa-signal` is the account, and the process parsing
  messages from strangers has no business being able to read it. That still holds after the
  uid move below — the state directory is `0700`, so the group buys the socket and nothing
  else.
- The allowlist lives in `config.toml` under `[[signal.conversations]]` — one entry per room,
  each naming its permitted senders and the profile that applies to each of them *there*
  (ADR 0008). An empty table is a startup refusal, not a permissive default. Group entries
  carry a pinned `members` list; the gate reads the live membership back with `listGroups` on
  connect and every 15 minutes, and a group that differs refuses everything until config is
  corrected. Print what any of it resolves to before restarting:

  ```bash
  # PYTHONPATH because the code is at /opt/wpa/src and there is no installed
  # package — the unit sets the same variable, so this matches what actually runs.
  sudo -u wpa-gate PYTHONPATH=/opt/wpa/src python3 -m gate.signal \
    --check /opt/wpa/config/config.toml
  ```

See also [ADR 0004](../decisions/0004-signal-control-channel.md),
[ADR 0007](../decisions/0007-principals-on-the-control-channel.md),
[ADR 0008](../decisions/0008-authority-is-a-conversation-sender-pair.md) and
[ADR 0010](../decisions/0010-profiles-are-pre-bound-grant-bundles.md).

## Operational notes

**`active` is not `ready`.** `Type=simple` marks the unit started as soon as the JVM
launches, but the socket only appears once signal-cli has initialised — measured at ~19s
after unit start on the Pi, cold. A client connecting at boot must retry rather than assume
the socket is there. Yet another case where "the service is running" answers the wrong
question.

**Messages that arrive with no client attached are lost — unless the receive mode
says otherwise.** Under `--receive-mode on-start` the daemon pulls from Signal
regardless of whether anything is listening, acks the message, and drops it: it
does not replay when a client reconnects. Verified 2026-08-11 by stopping the gate,
detaching every client, sending one message and starting the gate again — it never
appeared, then or later. The unit therefore uses **`--receive-mode on-connection`**,
which fetches only while a client is attached and leaves the rest queued on
Signal's servers; the same experiment then delivered the message one second after
the gate reconnected.

So: a restart of `wpa-gate` costs latency, not commands. A gate that is down for a
long time still receives nothing — the daemon is only as live as its subscriber,
which is why `wpa-gate` is `Restart=always`.

**The daemon holds the account lock.** Once `signal-cli.service` is up, a second `signal-cli`
invocation against the same account will conflict. Talk to it over the socket:

```bash
printf '%s\n' '{"jsonrpc":"2.0","method":"send","params":{"recipient":["+4477..."],"message":"hi"},"id":1}' |
  sudo -u wpa-signal nc -U /run/wpa-signal/socket
```

---

## Done when

- [ ] Dedicated number registered as its own Signal account (not a link to mine)
- [ ] Registration PIN set and stored
- [ ] Messages send and receive both ways
- [ ] Account state backed up off the Pi **and off the dev box**, encrypted with a key that
      is on neither, re-taken on a timer, and **restored at least once with the output
      written down** (section 3)
- [ ] Daemon runs under systemd on a unix socket, restarts on failure
- [ ] signal-cli version recorded, **and the matching libsignal aarch64 build**
- [ ] **No message content in journald**: `sudo journalctl -u signal-cli | grep -i '^.*Body:'`
      returns nothing
- [ ] **Survives a reboot** — not just "the unit is active", but a message sent from the
      phone after the reboot actually arrives. A daemon that starts and never reconnects
      looks identical to one that works until you need it.

**Next:** [04-agent-deploy.md](04-agent-deploy.md)
