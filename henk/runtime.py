"""Production wiring: assemble the full runtime from config.

Kept separate from ``app.py`` (the reusable Dispatcher/App) so the concrete
choices — Signal transport, SDK factory, httpx client, ntfy event stream — live
in one place and ``build_runtime`` can be constructed in a test without
connecting anything.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import httpx

from henk.agent.commands import OwnerCommands
from henk.agent.core import AgentCore
from henk.agent.maintenance import MaintenanceNote
from henk.agent.recall import MemoryRecall
from henk.agent.sdk_session import SdkSessionFactory
from henk.agent.session import CHAT_PROFILE, EVENT_PROFILE
from henk.app import App, Dispatcher
from henk.audit import (
    AuditLog,
    CalendarReceipts,
    HoldReceipts,
    MutationReceipts,
    ReminderReceipts,
    read_audit_records,
)
from henk.calendar.windows import MaintenanceWindows
from henk.channel.acknowledge import OwnerAcknowledgement
from henk.channel.allowlist import AllowlistFilter
from henk.channel.base import ChannelAdapter, SendOutcome
from henk.channel.signal import (
    TYPING_REFRESH_SECONDS,
    SignalAdapter,
    SignalCliRestBridge,
)
from henk.config import GOOGLE_CALENDAR_KEY_ENV, Config
from henk.events.checkpoint import OffsetCheckpoint
from henk.events.coordinator import EventCoordinator
from henk.events.holds import HoldLedger
from henk.events.incident_context import IncidentContextProvider
from henk.events.intake import (
    SINCE_REJECTED_NOTICE,
    EventIntake,
    NtfyEventStream,
)
from henk.events.pipeline import EventPipeline, PipelineConfig
from henk.events.release import HoldReleaser
from henk.gate.approval import ApprovalGate
from henk.reminders.note import DeliveredReminderNote
from henk.reminders.scheduler import ReminderScheduler
from henk.reminders.timeparse import TimeResolver
from henk.replay.recorder import TriageRecorder
from henk.store import build_stores
from henk.tools import (
    ApprovalReferences,
    build_calendar_client,
    build_production_registry,
    build_time_resolver,
)

logger = logging.getLogger("henk.runtime")

#: Bounded tail read for cadence rehydration. Far above any plausible 24h window
#: at Henk's few-events-per-week cadence, so rehydration stays a bounded startup
#: cost even without log rotation. (read_text still loads the file; a seek-based
#: tail is the future optimization if the log ever grows large.)
_REHYDRATE_LIMIT = 10_000

#: The transport read timeout is a redundant FLOOR under intake's own per-frame
#: liveness budget, not a replacement for it: httpx's read timeout resets on any
#: received bytes, so a peer dribbling newlines defeats it. Set above the deadline
#: so intake's watchdog is always what fires first, leaving this as the backstop
#: that turns a broken hand-rolled watchdog into a bounded reconnect.
_READ_TIMEOUT_MULTIPLE = 2


def _checkpoint_path(audit_path: str) -> Path:
    """The intake-offset cursor lives beside the audit log, on the same volume."""
    return Path(audit_path).parent / "intake-offset"


def build_runtime(config: Config) -> tuple[App, httpx.AsyncClient]:
    """Construct the wired App and the shared HTTP client (caller closes it).

    Nothing network-facing is opened here: the Signal bridge and the ntfy event
    stream connect lazily on first receive, and the SDK session is created
    per-conversation on demand.
    """
    # The calendar key has been decoded into `config` by now (or ignored, when the
    # calendar is off). Remove it from the process environment before anything
    # that can start a subprocess exists: the SDK's CLI inherits `os.environ`
    # (sdk_session.py:335-347). Unconditional, because the rollback path keeps the
    # `.env` line and a disabled runtime must not hand it on either (D1).
    os.environ.pop(GOOGLE_CALENDAR_KEY_ENV, None)
    # NOTE: this shared tool client carries no explicit timeout, so it takes
    # httpx's 5s PER-PHASE default. That is the same defect the Signal bridge's
    # explicit total fixes, left in place deliberately: the tool endpoints have
    # their own per-endpoint `timeout_seconds` in config and no measured budget
    # here, so choosing one is a separate change rather than a silent guess.
    client = httpx.AsyncClient()
    # One store, shared: the tools, the owner commands and recall must all read and
    # write the same repositories, or `/remember` and `store_memory` would disagree
    # about what Henk knows. Nothing is opened here — Store connects lazily.
    stores = build_stores(config.store, config.reminders)

    # Audit is constructed UNCONDITIONALLY (design D11) — see the note below — but
    # the reminder receipts need it before the registry is built, because the tools
    # take the lifecycle-record writer as a constructor argument.
    audit = AuditLog(config.audit.path)
    receipts = MutationReceipts(audit)
    reminder_receipts = ReminderReceipts(audit)

    # ONE resolver for the whole runtime, or None when reminders are disabled. The
    # tools, the owner commands and the per-turn time header all close over this
    # same instance: a due time rendered by two resolvers could differ, and the owner
    # would be the one left adjudicating which is right.
    resolver = build_time_resolver(config)
    # ONE incident context for the whole runtime (triage-quality D8):
    # `publish_handoff` reads it to archive a handoff with its session's incidents,
    # and the agent core is its only writer. The core below is handed this SAME
    # instance; a second provider would leave the tool's copy empty forever, and
    # nothing would ever be archived.
    incident_context = IncidentContextProvider()
    # ONE calendar client, or None when `calendar.enabled` is false (maintenance-
    # calendar D2): the two tools and the window provider share its one token
    # cache. Building it contacts nobody. With the calendar enabled, the gate's
    # recorder becomes the reference tap (still forwarding every decision to the
    # same receipts), so the create tool's `calendar` record names the approval
    # that authorized it; disabled, both stay exactly as before.
    calendar_client = build_calendar_client(config, client)
    calendar_references = (
        ApprovalReferences(receipts) if calendar_client is not None else None
    )
    registry = build_production_registry(
        config,
        client,
        stores=stores,
        resolver=resolver,
        reminder_receipts=reminder_receipts,
        incident_context=incident_context,
        calendar_client=calendar_client,
        calendar_receipts=(
            CalendarReceipts(audit) if calendar_client is not None else None
        ),
        calendar_references=calendar_references,
    )

    # Every timeout comes from config, none from a constructor default: an
    # unsupplied default is how the receive path's 30s went unchosen.
    bridge = SignalCliRestBridge(
        config.signal.bridge_url,
        config.signal.account,
        send_timeout=config.signal.send_timeout_seconds,
        open_timeout=config.signal.open_timeout_seconds,
    )
    adapter = SignalAdapter(
        bridge,
        account=config.signal.account,
        owner=config.owner.id,
        safe_length=config.signal.safe_length,
    )

    # Audit was constructed above, UNCONDITIONALLY (design D11). It used to appear
    # only when events were enabled — a leftover of arriving with that change. With
    # mutating tools in the registry, every supported configuration must produce
    # receipts, the rollback path (`events.enabled: false`) included.
    #
    # The gate sends approval prompts over the same channel the owner uses. The
    # demotion flag is the only config input it takes, and it only narrows; the
    # recorder is what makes every decision it takes durable at decision time.
    gate = ApprovalGate(
        adapter,
        timeout_seconds=config.agent.approval_timeout_seconds,
        demote_standing=config.gate.demote_standing,
        recorder=calendar_references if calendar_references is not None else receipts,
    )
    # Owner acknowledgement (owner-acknowledgement D1-D6): ONE object, or None when
    # `signal.acknowledge_owner` is false (the rollback; absent means true). The
    # dispatcher sends its read receipts and the core enters its `working` bracket,
    # both under the one configured bound. It is handed the SAME adapter the
    # core and the scheduler hold, and is paused by the SAME gate the core frames:
    # a second gate's `has_pending` would never be true, and the indicator would
    # keep asserting "typing" next to an approval prompt.
    acknowledgement = (
        OwnerAcknowledgement(
            adapter,
            timeout=config.signal.acknowledge_timeout_seconds,
            refresh_seconds=TYPING_REFRESH_SECONDS,
            paused=gate.has_pending,
        )
        if config.signal.acknowledge_owner
        else None
    )
    factory = SdkSessionFactory(
        registry,
        gate,
        model=config.agent.model,
        system_prompt=config.agent.system_prompt,
        effort=config.agent.effort,
        thinking=config.agent.thinking,
        profile=CHAT_PROFILE,
    )
    # The triage profile (triage-quality D11): a second factory over the SAME
    # registry and gate, differing only in model, effort and thinking. Each event
    # key absent from config already equals the resolved chat value
    # (`Config.from_dict`), so with none configured the two build identically.
    event_factory = SdkSessionFactory(
        registry,
        gate,
        model=config.agent.event_model,
        system_prompt=config.agent.system_prompt,
        effort=config.agent.event_effort,
        thinking=config.agent.event_thinking,
        profile=EVENT_PROFILE,
    )

    # Durability wiring (design D1/D2): only when events are enabled. The
    # checkpoint store + cadence rehydration read the existing audit volume; both
    # reads are non-fatal if the volume is absent (fresh install / test env).
    checkpoint = None
    pipeline = None
    if config.events.enabled:
        checkpoint = OffsetCheckpoint(_checkpoint_path(config.audit.path))
        pipeline = _build_pipeline(config)
        # Reconstruct cooldown/cap/recurrence from the persisted log before the
        # coordinator consumes any event, so a restart does not re-arm cooldowns
        # or reset the daily cap. Best-effort: rehydration must never crash
        # startup, so an unreadable log or an unforeseen record shape logs and
        # falls back to empty cadence state (worst case: one restart re-alerts).
        try:
            pipeline.rehydrate(
                read_audit_records(config.audit.path, limit=_REHYDRATE_LIMIT),
                now=time.time(),
            )
        except Exception:
            logger.warning(
                "cadence rehydration failed; starting with empty cadence state",
                exc_info=True,
            )

    # The maintenance-window provider, only when the calendar AND events are
    # enabled, over the ONE calendar client above. With events off `holds()` is
    # false for every event, so its snapshot would always be empty and it would poll
    # Google for nothing; `calendar_upcoming` reads live and does not need it. Its
    # persisted snapshot is loaded here, before the coordinator (and with it intake)
    # exists, let alone runs (D6): a restart during a patch round holds from the
    # first event, with Google unreachable or not.
    windows = None
    if calendar_client is not None and config.events.enabled:
        windows = MaintenanceWindows(
            calendar_client,
            config.calendar,
            audit_path=config.audit.path,
            events_enabled=config.events.enabled,
        )
        windows.load()
    # The hold ledger (D7/D8): built before the core, whose release sink is its
    # `mark_released`, and handed to the coordinator and the releaser after it. ONE
    # ledger, because its in-flight set is what closes a releasing window to new
    # holds. Only with events enabled: holding and releasing need the pipeline.
    ledger = (
        HoldLedger(stores.holds, HoldReceipts(audit))
        if config.events.enabled
        else None
    )

    core = AgentCore(
        factory,
        adapter,
        idle_timeout_seconds=config.agent.idle_timeout_seconds,
        audit=audit,
        model=config.agent.model,
        checkpoint=checkpoint,
        # Wire recurrence framing live: a triage that publishes a handoff records
        # its id for the next re-fire of that identity (removes dead note_handoff).
        handoff_sink=pipeline.note_handoff if pipeline is not None else None,
        # The core frames every agent turn for the gate (turn type, announceability,
        # session taint) — without this the gate cannot enforce turn scope (D10).
        gate=gate,
        # Fans model-initiated receipts into the session record's approvals[].
        receipts=receipts,
        # Owner commands run app-side, before any session exists, and write their
        # own receipts — they are owner-authored input, so no gate is involved (D8).
        commands=OwnerCommands(
            memories=stores.memories,
            inbox=stores.inbox,
            # The SAME repository and resolver instances the tools got. Passed only
            # when the capability is enabled, so all four reminder commands reply
            # honestly rather than half-working when it is off.
            reminders=stores.reminders if config.reminders.enabled else None,
            resolver=resolver,
            receipts=receipts,
            reminder_receipts=reminder_receipts,
            inbox_page_size=config.store.inbox_page_size,
        ),
        # Memory recall for the first turn of each session, owner or event (D3;
        # triage-quality D7).
        recall=MemoryRecall(stores.memories, limit=config.store.recall_render_limit),
        # The per-turn current-time header, composed from the same resolver — so the
        # time the model reasons from and the time the owner is told read identically.
        # None when reminders are disabled, and then owner-turn composition is
        # byte-identical to before this change.
        time_header=_time_header(resolver),
        # The delivered-reminder block: what the scheduler sent, told back to Henk on
        # the owner's next turn. Same repository and resolver as everything else.
        deliveries=_delivery_note(config, stores, resolver),
        # The core publishes an event session's incidents here, and clears them on
        # session close; `publish_handoff` reads them (triage-quality D8).
        incident_context=incident_context,
        # The event framing names only tools this registry holds (triage-quality
        # D6): the same registry, built from the same config, that the system
        # prompt enumerates.
        tool_names=registry.names(),
        # The SAME archive `publish_handoff` writes, read by the event path only
        # for the related-handoff digest (triage-quality D9).
        handoff_archive=stores.handoffs,
        # Event sessions only; owner sessions, and every session after `/new` or
        # idle expiry, come from `factory` above.
        event_factory=event_factory,
        # One recording per event triage (triage-quality D13), or None when
        # `triage_recording.enabled` is false (the rollback; absent means true).
        recorder=_triage_recorder(config),
        # The working-indicator bracket around owner agent turns and sent triages,
        # or None when acknowledgement is off, leaving those turns unchanged.
        working_indicator=(
            acknowledgement.working if acknowledgement is not None else None
        ),
        # Marks a released window once its triage record is durable (D8).
        release_sink=ledger.mark_released if ledger is not None else None,
        # The owner-turn maintenance block (D10), and the zone the release line's
        # HH:MM is read in.
        maintenance=_maintenance_note(config, stores, windows),
        owner_zone=config.owner.zone,
    )
    dispatcher = Dispatcher(
        AllowlistFilter(config.owner.id), gate, core, acknowledgement=acknowledgement
    )

    coordinator = (
        _build_coordinator(
            config, core, audit, pipeline, checkpoint, adapter,
            holds=ledger, windows=windows,
        )
        if config.events.enabled
        else None
    )
    # The release task (D8): whenever events are enabled, calendar on or off, so
    # rows held before a rollback are still released (the extension check needs
    # the provider and is skipped without it). With no held rows each tick is one
    # read of `hold_windows` and does nothing.
    releaser = (
        HoldReleaser(
            ledger,
            coordinator,
            grace_seconds=config.calendar.grace_minutes * 60,
            windows=windows,
        )
        if coordinator is not None and ledger is not None
        else None
    )
    # The scheduler is handed `adapter` — the SAME instance the core holds, not a
    # second one over the same bridge. The send lock is instance state, so a second
    # adapter would serialize nothing while passing every serialization test.
    scheduler = (
        ReminderScheduler(
            stores.reminders,
            adapter,
            config=config.reminders,
            resolver=resolver,
            receipts=reminder_receipts,
        )
        if config.reminders.enabled and resolver is not None
        else None
    )
    return (
        App(
            adapter,
            dispatcher,
            core,
            coordinator=coordinator,
            scheduler=scheduler,
            windows=windows,
            releaser=releaser,
        ),
        client,
    )


def _maintenance_note(
    config: Config, stores, windows: MaintenanceWindows | None
) -> MaintenanceNote | None:
    """The owner-turn maintenance block's composer, or None without an owner zone.

    Built whatever ``calendar.enabled`` says: windows released from earlier holds
    are reported after a rollback too, since release itself still runs (D8). Active
    windows need the provider, so with the calendar off only released ones show.
    With no rows and no provider it composes nothing, and owner turns are unchanged.
    The zone is required by the shared renderer; the calendar cannot be enabled
    without one, so None only on a deploy that never enabled it.
    """
    if config.owner.zone is None:
        return None
    return MaintenanceNote(
        stores.holds,
        zone=config.owner.zone,
        grace_seconds=config.calendar.grace_minutes * 60,
        windows=windows,
    )


def _triage_recorder(config: Config) -> TriageRecorder | None:
    """The triage recorder, or None when recording is turned off (D13).

    Its directories are the ones derived from `audit.path`, beside the audit log on
    the same volume and backup; no key can place them elsewhere. Constructing it
    does no I/O: the directory appears with the first recording.
    """
    if not config.triage_recording.enabled:
        return None
    return TriageRecorder(
        config.audit.triage_recordings_dir,
        replays_dir=config.audit.triage_replays_dir,
    )


def _delivery_note(config: Config, stores, resolver: TimeResolver | None):
    """The owner-turn delivered-reminder provider, or None when reminders are off.

    Off means off: with no provider the core's owner-turn composition is byte-identical
    to what it was before this capability existed, which is what makes the disabled
    deploy a genuine no-op rather than a nearly-no-op.
    """
    if not config.reminders.enabled or resolver is None:
        return None
    return DeliveredReminderNote(
        stores.reminders,
        resolver,
        window_seconds=config.reminders.note_window_seconds,
        max_items=config.reminders.note_max_items,
        clock=resolver.current_instant,
    )


def _time_header(resolver: TimeResolver | None):
    """A zero-argument header composer, or None when reminders are disabled.

    Reads the clock once per call, which is once per owner turn — the header has to
    reflect the moment of *this* turn, not the session's start, or a relative time
    the model composes from it means something the owner did not say.
    """
    if resolver is None:
        return None
    return lambda: resolver.time_header(resolver.current_instant())


def _build_pipeline(config: Config) -> EventPipeline:
    ev = config.events
    return EventPipeline(
        PipelineConfig(
            debounce_seconds=ev.debounce_seconds,
            cooldown_seconds=ev.cooldown_seconds,
            recurrence_window_seconds=ev.recurrence_window_seconds,
            cap_per_24h=ev.cap_per_24h,
            cooldown_overrides=ev.cooldown_overrides,
        )
    )


def _build_coordinator(
    config: Config,
    core: AgentCore,
    audit: AuditLog,
    pipeline: EventPipeline,
    checkpoint: OffsetCheckpoint,
    channel: ChannelAdapter,
    *,
    holds: HoldLedger | None = None,
    windows: MaintenanceWindows | None = None,
) -> EventCoordinator:
    ev = config.events
    stream = NtfyEventStream(
        config.ntfy.base_url,
        ev.events_topic,
        token=config.secrets.ntfy_token,
        read_timeout=ev.liveness_deadline_seconds * _READ_TIMEOUT_MULTIPLE,
    )

    async def _notify_since_rejected() -> None:
        # Proactive: an unprompted operator alert, not a reply. Single chunk, so
        # there is nothing to be cut off and no failure notice to supply.
        outcome = await channel.send_proactive(SINCE_REJECTED_NOTICE)
        if outcome != SendOutcome.DELIVERED:
            logger.error(
                "since-rejected notice not delivered (outcome=%s)",
                getattr(outcome, "value", outcome),
            )

    # Seed intake from the durable checkpoint so the first subscribe resumes with
    # since=<offset> and replays events published while Henk was stopped (D1). If
    # the server rejects that cursor, intake replays all retained events rather
    # than retrying a value it can never resume from — and tells the owner.
    #
    # The liveness watchdog: intake abandons a subscription that delivers no
    # proof-of-life frame within the deadline, so a half-open socket stops being
    # observationally identical to a healthy quiet tailnet. Config-driven, and the
    # deadline's ordering against the server's keepalive interval is validated at
    # load time.
    intake = EventIntake(
        stream,
        initial_offset=checkpoint.read(),
        on_since_rejected=_notify_since_rejected,
        liveness_deadline=ev.liveness_deadline_seconds,
        liveness_report_interval=ev.liveness_report_interval_seconds,
    )
    # Holds (D7): classified through the ONE ledger the core's sink and the releaser
    # use. Without a window source (calendar disabled) it still drops replays of
    # held events but holds nothing new.
    return EventCoordinator(
        intake,
        pipeline,
        core,
        debounce_seconds=ev.debounce_seconds,
        audit=audit,
        holds=holds,
        windows=windows,
    )
