#!/usr/bin/env python3
"""Print the `label`/`id`/`members` block to paste into a group's config entry.

    sudo -u wpa-signal python3 deploy/pin-group.py

Three things make this worth a script rather than a paragraph telling you to copy
them out of `listGroups`, all of them found on hardware 2026-08-12:

* **The assistant is itself a member of the group.** Leave its ACI out of `members`
  and the group refuses every message forever, which reads exactly like a bug.
* **A member carries both a uuid and a number.** The uuid is the one to pin — it
  names the account rather than the line, and a re-registered number belongs to
  whoever holds it next.
* Typing a base64 group id by hand goes wrong once and then costs an evening.

Read the printed `label` before pasting it. It is the group's *current* name, which
is chosen by whoever is in the group and is precisely the thing not to key on — it is
there to tell the entries apart, and the `id` beneath it is the identity.

Run it as `wpa-signal`: the daemon holds the account, and this connects to its socket
as any other client would.
"""

from __future__ import annotations

import json
import socket
import sys

SOCKET = "/run/wpa-signal/socket"


def main() -> int:
    conn = socket.socket(socket.AF_UNIX)
    try:
        conn.connect(SOCKET)
    except OSError as exc:
        print(f"cannot reach {SOCKET}: {exc.strerror}", file=sys.stderr)
        return 1

    conn.sendall(b'{"jsonrpc":"2.0","id":"pin","method":"listGroups"}\n')
    for line in conn.makefile("r"):
        reply = json.loads(line)
        if reply.get("id") != "pin":
            continue  # the daemon broadcasts inbound traffic to every client
        groups = reply.get("result") or []
        if not groups:
            print("the assistant is in no groups", file=sys.stderr)
            return 1
        for group in groups:
            members = [member["uuid"] or member["number"] for member in group["members"]]
            print()
            print("[[signal.conversations]]")
            print('label   = "%s"' % group["name"])
            print('id      = "%s"' % group["id"])
            print("members = [%s]" % ", ".join('"%s"' % member for member in members))
            if not group.get("isMember", True):
                print("# the assistant is NOT a member of this group", file=sys.stderr)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
