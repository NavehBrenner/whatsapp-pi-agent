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

`~/.local/share/signal-cli/data/` holds the account's key material. Losing it means
re-registering; leaking it means someone can be the assistant.

```bash
chmod -R go-rwx ~/.local/share/signal-cli
tar czf ~/signal-cli-backup-$(date +%F).tar.gz -C ~/.local/share signal-cli
```

Store that backup somewhere encrypted and **off the Pi**. It is in `.gitignore`
(`signal-data/`) — keep it that way, and never commit it.

## 4. Run as a daemon

JSON-RPC over a unix socket, so nothing listens on the network. The unit lives in the repo
at [`deploy/systemd/signal-cli.service`](../../deploy/systemd/signal-cli.service); install it
with:

```bash
sudo deploy/install-signal.sh +972XXXXXXXXX
```

That creates the `wpa-signal` system user, migrates the account state out of whichever home
directory `register` left it in, writes the number to `/etc/wpa-signal.env`, and enables the
unit.

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

**Its own user.** The account state directory *is* the account — anyone who can read it can
be the assistant. It has no business sitting in a human's home directory.

Cap the JVM heap (`-Xmx256m` via `JAVA_OPTS` — the launcher honours `JAVA_OPTS` and
`SIGNAL_CLI_OPTS`, not `JAVA_TOOL_OPTIONS`). It'll happily take more than it needs, and
Waydroid wants the 8GB.

## 5. Trust boundary

The Signal channel is the **trusted** side of the system
([threat-model.md](../threat-model.md)). Two rules for whatever consumes it:

- **Verify the sender.** Only messages from my personal number trigger the privileged agent.
  Check the sender against a configured allowlist before anything runs, every time.
- **Confirmation replies are commands too.** A `YES` that authorises an action must be
  matched to the specific pending action it's answering, not treated as a global "proceed."

Both belong in the agent code, not here — noted so they don't get lost between documents.

---

## Done when

- [ ] Dedicated number registered as its own Signal account (not a link to mine)
- [ ] Registration PIN set and stored
- [ ] Messages send and receive both ways
- [ ] Account state backed up off the Pi, encrypted
- [ ] Daemon runs under systemd on a unix socket, restarts on failure
- [ ] signal-cli version recorded, **and the matching libsignal aarch64 build**
- [ ] **No message content in journald**: `sudo journalctl -u signal-cli | grep -i '^.*Body:'`
      returns nothing
- [ ] **Survives a reboot** — not just "the unit is active", but a message sent from the
      phone after the reboot actually arrives. A daemon that starts and never reconnects
      looks identical to one that works until you need it.

**Next:** [04-agent-deploy.md](04-agent-deploy.md)
