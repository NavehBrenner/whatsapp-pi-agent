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

Needs a JRE (21+ for current signal-cli). Pin the version:

```bash
sudo apt install -y openjdk-21-jre-headless

SIGNAL_CLI_VERSION=0.13.x        # pin it; record what you used
cd /tmp
wget "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz"
sudo tar xf "signal-cli-${SIGNAL_CLI_VERSION}.tar.gz" -C /opt
sudo ln -sf "/opt/signal-cli-${SIGNAL_CLI_VERSION}/bin/signal-cli" /usr/local/bin/signal-cli
signal-cli --version
```

On arm64 you may need the native `libsignal-client` for your architecture; if signal-cli
errors about a missing native library at startup, that's the cause.

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

JSON-RPC over a unix socket, so nothing listens on the network:

```bash
signal-cli -a "$ASSISTANT_NUMBER" daemon --socket
```

As a systemd unit — this one is worth setting up now since it's independent of the agent
code:

```ini
# /etc/systemd/system/signal-cli.service
[Unit]
Description=signal-cli JSON-RPC daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=%i
Environment=JAVA_TOOL_OPTIONS=-Xmx256m
ExecStart=/usr/local/bin/signal-cli -a ${ASSISTANT_NUMBER} daemon --socket
Restart=on-failure
RestartSec=10
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=%h/.local/share/signal-cli

[Install]
WantedBy=multi-user.target
```

Cap the JVM heap (`-Xmx256m`). It'll happily take more than it needs, and Waydroid wants the
8GB.

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
- [ ] signal-cli version recorded

**Next:** [04-agent-deploy.md](04-agent-deploy.md)
