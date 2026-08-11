# Envelopes the gate is tested against

**Status: provisional — built from signal-cli 0.14.7's documented shape, NOT yet
captured off the wire.** Replacing them with real captured envelopes is part of
NVB-10 and is not optional: the hole this gate exists to close (a typing indicator
arriving as a `receive` notification with no `dataMessage`) is *not* in the
documentation. It was found by watching the socket, so the regression test for it
has to come from the socket too. Until that swap happens, these files assert that
the gate's logic is self-consistent, not that it matches reality.

Capture procedure, on the Pi:

```bash
sudo -u wpa-signal python3 -c '
import socket, sys
s = socket.socket(socket.AF_UNIX); s.connect("/run/wpa-signal/socket")
for line in s.makefile("r"): sys.stdout.write(line); sys.stdout.flush()
' > ~/wpa-envelopes.jsonl
```

Then, from a phone in the assistant's conversation: send a message; start typing
without sending; open the chat to fire a read receipt; reply quoting an earlier
message; and send one message from a number that is *not* a principal.

**Redact before committing** — this repo is public. Replace numbers, UUIDs, group
ids, profile names and message bodies with the placeholders below; keep every key,
the nesting and the field types exactly as they arrived.

| Placeholder | Stands for |
|---|---|
| `+15555550100` | the owner's phone |
| `+15555550101` | a second principal (family) |
| `+15555550999` | a number that is not a principal |
| `+15555550199` | the assistant's own account |
| `…-1111-…` / `…-2222-…` / `…-9999-…` | the matching UUIDs |
| `ping` | whatever was actually typed |
