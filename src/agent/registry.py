"""The vocabulary of grants: every tool instance a profile may name.

This is the list config is checked against, so a typo in a profile's `tools` is a
refusal to start rather than a capability that silently is not there. It holds
**declarations, not implementations** — the name, the credential the container will
be given for it, the verb, and one line of plain language for the family-facing
"what can you do" reply. NVB-18 binds implementations to these names; nothing here
executes anything.

Three rules for adding a row, each paid for elsewhere in this repo:

1. **The verb is the narrow one.** `draft`, never `send`; `create_event`, never one
   entry that also cancels. Every tool is evaluated as "what does a successful
   injection do with this" (AGENTS.md), and the answer has to stay boring.
2. **One row per account, not per capability class.** `calendar.family.rw` and
   `calendar.personal.rw` are two grants because they are two calendars — that
   separation is the whole point of a fine-grained profile, and a single
   `calendar.rw` would collapse it.
3. **The credential is the grant.** A container gets exactly the credentials of the
   tools in its profile, so a row whose `credential` is wrong grants the wrong thing
   no matter how narrow its name reads.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tool:
    """One tool instance: what it is called, what it costs to grant, what it does."""

    name: str
    # systemd LoadCredential= id the container is given for this tool. None for tools
    # that need nothing mounted — reading what the assistant already has on disk.
    credential: str | None
    verb: str
    label: str


# ponytail: a dict literal, not a plugin registry. It is a closed list by design —
# the point of the grant vocabulary is that nothing can extend it at runtime.
TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in (
        Tool(
            name="whatsapp.read",
            credential=None,
            verb="read",
            label="read the WhatsApp chats it has been given",
        ),
        Tool(
            name="calendar.family.rw",
            credential="calendar-family",
            verb="create",
            label="see and add events on the family calendar",
        ),
        Tool(
            name="calendar.family.create_event",
            credential="calendar-family",
            verb="create",
            label="add an event to the family calendar",
        ),
        Tool(
            name="calendar.personal.rw",
            credential="calendar-personal",
            verb="create",
            label="see and add events on your own calendar",
        ),
        Tool(
            name="email.personal.draft",
            credential="gmail-personal",
            verb="draft",
            label="write an email draft for you to send yourself",
        ),
        Tool(
            name="notes.rw",
            credential="notes",
            verb="create",
            label="keep notes",
        ),
    )
}
