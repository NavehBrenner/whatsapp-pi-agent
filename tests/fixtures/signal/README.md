# Envelopes the gate is tested against

Captured from `/run/wpa-signal/socket` on the Pi (signal-cli 0.14.7), 2026-08-11.

| File | Provenance |
|---|---|
| `message.json` | **captured** — a real message from the owner's phone |
| `typing.json` | **captured** — the reason rule 2 exists: a `receive` notification with no `dataMessage` at all |
| `receipt.json` | **captured** — the phone acknowledging the gate's own ack (`sourceDevice: 2`, `isDelivery: true`) |
| `reaction.json` | **captured** — a 👍 on an assistant message. Not used by a test yet; it is what [NVB-16](https://linear.app/naveh-brenner/issue/NVB-16) will build the reaction path on, and it costs a person with a phone to obtain again |
| `quote-reply.json`, `group.json`, `family.json`, `stranger.json` | derived from `message.json` by editing one field, since those actions were not driven on the phone |
| `group-family.json`, `group-stranger.json`, `group-unknown.json`, `group-quote-reply.json`, `group-update.json` | **derived** from `group.json` (see below) |

## The group fixtures are derived, and that is a debt

`listGroups` returned `[]` on 2026-08-11: the assistant is in no Signal group, so no
real group envelope could be captured and the five `group-*` files above were built
by editing `group.json`. They are enough to test the gate's logic and **not** enough
to trust it on hardware, because the two things most likely to be wrong are exactly
the parts that were guessed:

- **The member shape in a `listGroups` result.** `_members_of` accepts both a list of
  strings and a list of objects carrying `uuid`/`number`, and refuses when it
  recognises neither. One capture settles which it is.
- **What a group membership change looks like on the wire.** `_is_group_update`
  treats any `groupInfo.type` other than `DELIVER` as a hint to re-read membership.
  If that never fires, drift is still caught — by the 15-minute refresh instead of in
  seconds — so the guess is cheap either way, but it is a guess.

Replace them with captured envelopes before relying on this in production: create a
group with the assistant, the owner and one other person, run the capture recipe
below, then send a message from each, add someone, and record a real `listGroups`
response.

A reaction arrives as a `dataMessage` with `message: null` and a `reaction` block —
`emoji`, `targetAuthorUuid`, `targetSentTimestamp`, `isRemove`. So the gate drops
it as `no body` today, and honouring it later is a deliberate narrow exception:
`targetSentTimestamp` names one prompt, and `isRemove: true` (un-reacting) must
never count as approval ([ADR 0008](../../../docs/decisions/0008-authority-is-a-conversation-sender-pair.md)).

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
