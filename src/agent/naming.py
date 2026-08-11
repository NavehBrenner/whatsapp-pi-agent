"""The two names a container is known by, constructed in exactly one place.

The gate names an agent; it never starts one. What turns that name into something
systemd can run is `deploy/render-agents.py` (NVB-14), and what dispatches to it is
the runner (NVB-15). Three callers, one naming rule — kept here rather than
interpolated three times, because a rule spelled out in three places is a rule that
will disagree with itself the first time it changes.

The charset is enforced where names are read (`gate.signal.load_config`), not here:
these run after validation, and a name that reached them has already been checked.
That check is not cosmetic — an agent name becomes a systemd unit instance and a
directory under the outbox root, so `../` in one is a path escape and a space in one
is a unit that will not start.
"""

from __future__ import annotations


def agent_image(profile: str) -> str:
    """What the agent can do: one image per profile, built with exactly its tools."""
    return f"wpa-agent:{profile}"


def agent_unit(agent: str) -> str:
    """Who the agent is: one unit instance per agent, with its own state and outbox.

    Two agents may share an image and share nothing else. Only an explicit shared
    `agent` name in config puts two senders in one session, and config refuses to let
    that span two conversations (ADR 0010).
    """
    return f"wpa-agent@{agent}.service"
