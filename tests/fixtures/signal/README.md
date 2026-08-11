# Envelopes the gate is tested against

Captured from `/run/wpa-signal/socket` on the Pi (signal-cli 0.14.7), 2026-08-11.

| File | Provenance |
|---|---|
| `message.json` | **captured** — a real message from the owner's phone |
| `typing.json` | **captured** — the reason rule 2 exists: a `receive` notification with no `dataMessage` at all |
| `receipt.json`, `quote-reply.json`, `group.json`, `family.json`, `stranger.json` | derived from `message.json` by editing one field, since those actions were not driven on the phone |

The captured pair is what makes the two facts below load-bearing rather than
assumed, and both were invisible in the documentation:

- **`sourceNumber` is `null`.** Signal does not share phone numbers by default, so
  the sender is identified by ACI: `source` and `sourceUuid` carry the UUID. An
  allowlist keyed on phone numbers matches nothing.
- **A typing indicator is a `receive` notification.** Keying on the method name
  makes the assistant invocable by anyone who can type at the number.

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
