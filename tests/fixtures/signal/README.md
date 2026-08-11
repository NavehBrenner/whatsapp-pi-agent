# Envelopes the gate is tested against

Captured from `/run/wpa-signal/socket` on the Pi (signal-cli 0.14.7), 2026-08-11.

| File | Provenance |
|---|---|
| `message.json` | **captured** — a real message from the owner's phone |
| `typing.json` | **captured** — the reason rule 2 exists: a `receive` notification with no `dataMessage` at all |
| `receipt.json` | **captured** — the phone acknowledging the gate's own ack (`sourceDevice: 2`, `isDelivery: true`) |
| `reaction.json` | **captured** — a 👍 on an assistant message. Not used by a test yet; it is what [NVB-16](https://linear.app/naveh-brenner/issue/NVB-16) will build the reaction path on, and it costs a person with a phone to obtain again |
| `quote-reply.json`, `family.json`, `stranger.json` | derived from `message.json` by editing one field, since those actions were not driven on the phone |
| `group.json` | **captured** — a real message in a real group (2026-08-12) |
| `group-quote-reply.json` | **captured** — a real quoted reply to the assistant's own message in a group |
| `group-update.json` | **captured** — a group rename: `type: "UPDATE"`, `message: null`, `revision` bumped |
| `group-family.json`, `group-stranger.json`, `group-unknown.json` | derived from `group.json` by editing the sender uuid or the group id — there was still only one other person to send from |

## What the group capture settled

Both were guesses in the first cut of NVB-12, and both were wrong or incomplete in
ways that mattered:

- **A `listGroups` member carries a uuid *and* a number**, as
  `{"number": …, "uuid": …, "isAdmin": …}`. The first implementation added both to
  the member set, which would have required a pinned list to name each person twice
  to match. It takes the uuid now, falling back to the number.
- **The assistant is itself a member**, so a pinned `members` list must include the
  assistant's own ACI or the group refuses forever. Generate the line rather than
  typing it; the command is in `config/example.config.toml`.
- **`isMember: false` still lists the members.** Being removed from a group must
  therefore be read as drift, or a room the assistant is no longer in looks healthy.
- **A group change arrives as `groupInfo.type: "UPDATE"` with `message: null`** and
  an incremented `revision`. Since it has no body it is dropped as `no body`, which
  is why the membership re-read is triggered on the drop path and not only on an
  accepted command. `revision` is deliberately not tracked: membership is re-read on
  every connect, so a cursor would only cover a change that happened while the gate
  was down.

Still missing, and honest about it: **a message from someone in an allowlisted group
who is not a listed sender.** That is the `unlisted sender` counter, where probing
inside a family room shows up, and it is derived by editing the sender uuid because
there was no second person to send it. Capture it the next time a group has one.

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
