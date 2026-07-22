"""Event intake: outbound ntfy subscription → identity → debounce/cooldown/cap.

This package is the sensor-facing half of henk-events (v1.2). Nothing here opens
an inbound socket: :class:`~henk.events.intake.EventIntake` *subscribes* to the
deny-all events topic over the same outbound egress the notify tool already uses
(design D1). Event payloads are treated strictly as data — the intake never lets
event text change Henk's toolset or behaviour (design D4); that is the caller's
contract, enforced structurally by the closed-toolset hook regardless.
"""
