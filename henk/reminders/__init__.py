"""Reminder time resolution and rendering.

The whole of the clock lives here, and nothing else in Henk does time arithmetic on
a reminder. The organising principle (reminders design D3): **the app owns every
arithmetic a DST transition can perturb; the model owns only naming the target — a
wall clock or a duration — and never converting between them.**
"""

from henk.reminders.timeparse import (
    AMBIGUOUS,
    COMMAND,
    IMAGINARY,
    NORMAL,
    TOOL,
    Resolution,
    TimeResolutionError,
    TimeResolver,
    classify_local,
    current_time_header,
    render_instant,
)

__all__ = [
    "AMBIGUOUS",
    "COMMAND",
    "IMAGINARY",
    "NORMAL",
    "Resolution",
    "TOOL",
    "TimeResolutionError",
    "TimeResolver",
    "classify_local",
    "current_time_header",
    "render_instant",
]
