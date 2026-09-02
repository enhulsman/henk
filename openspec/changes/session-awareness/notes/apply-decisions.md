# Apply decisions — `session-awareness`

**Session:** 2026-09-02, apply from the APPROVED proposal (8 scrutiny rounds, see
`scrutiny-record.md`). Groups were worked in `tasks.md` order with a review gate between them,
one implementation subagent per group, and each group mutation-tested before the next began.
Standing rule 1 applies: counts and shapes only, no live estate values.

## Verification apparatus, run first (steering: not at close-out)

- **Two-phase vocabulary sweep (task 9.5)** — run at the start of apply against the five
  artifacts with the retained set applied. Phase 1: **zero hits** outside the retained set
  for all 24 tokens. The one line the mechanical pass flagged was the *A field-selection key
  cannot be configured* scenario's WHEN line in the capability spec, which is the four-keys
  requirement's restatement of the closed-schema refusal and is named in the retained set.
  Phase 2: every new token (`degraded`, `heartbeat_s`, `tick_s`,
  `session_project_allowlist`, `tick_seconds`, `1500`, `backend_failure_reason`) and every
  D10 marker literal is present. Re-run at close-out (9.5) after all edits.
- **Marker self-match and uniqueness (task 6.3's contract)** — pre-checked over the D10
  sample renderings before the renderer existed: every marker is a substring of its own
  rendering, no marker appears in any other row's rendering, and none appears in a
  placeholder session line. **20 rows carry a marker**, matching the design's hand check.
  The binding test against the real renderer is written in group 6.

## §1 — probe outcomes that changed the code

Recorded in full in `estate-probe.md`; the decisions taken from them:

1. The publisher parses herdr's agents from `result.agents` and validates
   `result.type == "agent_list"` (the design's Context described the list as top-level).
2. The pane-id shape regex stays `^[\w:.-]{1,32}$`: the observed character class is
   `[A-Za-z0-9:]`, strictly inside it. A tighter regex was rejected because herdr's
   documentation declines to specify a grammar, so tightening would be the derivation it
   warns against.
3. `foreground_cwd` never differed from `cwd` live (0 of 6). The both-paths gate is kept;
   task 8.5 records the live count again from the dry-run.
4. `git remote get-url origin` distinguishes rc 2 (checkout without `origin` → owner gate
   fails) from rc 128 (not a git work tree → owner gate passes). The design's "a checkout
   with no `origin` fails the owner gate" is implemented on exactly that exit-code split.
5. Python on the workstation is 3.12.3, so `tomllib` is available and the 3.11+ guard passes.

## Group mutation tables

Filled in per group below as each group closes.

### Group 2 — config surface (Henk side)

Suite after the group: **1951 passed / 12 deselected** (+55, all in
`tests/test_config_sessions.py`; task 2.7 amended an existing test rather than adding one).

| Mutation | Caught by |
|---|---|
| `from_dict` reads `sessions.enabled` with default `True` | six new default/loader tests plus the pre-existing prompt byte-identity pin (`tests/test_reminders_inert.py`) |
| delete the `lookback >= stale` check | the two lookback-ordering tests (with the capability on and off) |
| drop strip/discard in `normalise_label_allowlist` | eight normalisation tests |
| `sessions_enabled` never appends the summary | prompt-summary, loader-wiring, and full-capability-count tests |
| remove `13` from `COUNT_WORDS` | the two count-table tests (`KeyError: 13`) |

Decisions taken in the group:

- The two durations are read as `int`, not `float` like the read-depth table, because both
  are interpolated into ntfy's `since=<N>s` and `1500.0s` is not a valid value.
- `normalise_label_allowlist` lives in `henk/config.py` and is applied once by the loader,
  so the tool consumes an already-canonical tuple; it refuses a non-list value and a
  non-string entry by name rather than coercing (a coerced `str(7)` would be a label no
  publisher can emit, with nothing to explain the empty result).
- Task 2.10 is half-done here by design: the prompt side asserts thirteen against the
  summary tuples; the equality against `build_production_registry`'s tool count is added in
  group 7 once `sessions_read` is registered.
- Review-gate fix: the rationale comments for `stale_after_seconds` and `lookback_seconds`
  were corrected to the design's derivation (heartbeat plus two ticks; 6 h lookback inside
  a 72 h cache — the draft comment wrongly said the lookback matched the cache duration).

### Group 3 — publisher classification

`tests/test_session_publisher.py`: **164 passed** after the group (the publisher module is
loaded from `deploy/session-publisher/` by path; an AST test asserts it imports stdlib
modules only).

| Mutation | Failing tests | Caught by (representative) |
|---|---|---|
| owner gate deleted (always admits) | 19 | root-admits-owner-refuses, dry-run names the owner gate, no-origin refused, empty `allow_owners` admits only non-git, case-sensitive owner match |
| deny loop removed from the root gate | 9 | symlink into a denied subtree, scratch worktree under the temp root embedding a denied checkout, deny-below-allow wins, denied foreground path blocks the session, real-symlink canonicalisation |
| classification on the reported path instead of the canonical one | 5 | canonical-vs-reported test, symlink tests |
| loader reads a misspelled `deny_root` so the configured key is silently unread | 12 | every deny classification test plus the `deny_roots` wrong-type refusal |

Finding worth keeping from the fourth mutation: the **closed-schema test does not catch a
renamed reader** — the allowed-key set is unchanged, so a misspelled `deny_root` in the
*config* is still refused by name while the real key goes unread. It is the classification
tests that bind the deny key to its reader; the closed schema alone is not that mechanism.

Decisions taken in the group:

- Root gate runs before the owner gate per path, and a root-denied path never spawns a git
  subprocess; each path carries at most one denial, and a pane at most two (one per
  reported path).
- A `subprocess.TimeoutExpired` or `OSError` from git is a refusal of that path
  (`owner:timeout` / `owner:git-unavailable`), never a crash of the tick.
- Extra load-time refusals beyond the spec's letter, all named: relative root paths,
  non-positive `heartbeat_seconds`/`tick_seconds`, a bool where an int is required,
  non-table `allow_roots` entries, unreadable/invalid-TOML/invalid-UTF-8 files, and a
  non-string `foreground_cwd` in a herdr record.
- `/mnt` immediate children are refused by segment arithmetic, so `/mnt/c` is refused while
  a deeper path under a drive mount loads.
- Review-gate fix: the publisher identity string was aligned to the design's
  `session-publisher/0.1`.

### Group 4 — publisher fields, aggregate, snapshot, budget

`tests/test_session_publisher.py`: **258 passed** after the group (+94, purely additive).

| Mutation | Failures | Caught by (representative) |
|---|---|---|
| fifth key (`cwd`) on the session object | 13 | exactly-four-keys (three configs), documented key order, forbidden-fields on the serialised bytes |
| `project` derived from the cwd basename | 11 | configured-label tests, admitted-label-set counts, forbidden-fields |
| degrade drops from the head | 6 | tail-of-priority-order tests, every-blocked-survives-before-working |
| budget measured before the `degraded` key is added | 3 | degraded-key-inside-the-budget, count-grows-with-its-own-digits |
| `unlisted` emitted regardless of `publish_unlisted` | 4 | no-unlisted-when-off, exact top-level key set |
| `age_source: "claude-estate"` on estate failure | 11 | unusable-estate-payload (five shapes), broken-estate-does-not-fail-the-run |
| `age_source` decided on truthiness instead of `is None` | 1 | zero-row estate is still the claude-estate source |
| the join reads a tenth estate key | 1 | the nine-other-keys-never-accessed recorder test |

Decisions taken in the group:

- The age join is a separate function fed with row mappings, so a recording mapping can
  prove that only `pane_id` and `age_s` are ever indexed (the four never-leave-the-machine
  estate fields are never read, not merely never published).
- An empty estate row list is a working age source with nothing to say
  (`age_source: "claude-estate"`, every age `null`); a missing, non-JSON, or wrong-shaped
  payload is `age_source: "none"`. The two are distinguished by `is None`, not truthiness.
- One `serialise` function is both the measured and the published body, asserted by
  monkeypatching it inside the budget loop.
- Two derived, pinned exemptions in the forbidden-fields byte test: the placeholder path
  component `work` is a substring of the verbatim status `working`, and the workspace id is
  structurally the first segment of the published pane id (probe §1.1). Both exemptions are
  asserted to be exactly that narrow; full paths and `tab_id` stay asserted absent.
- Sessions are ordered by the degrade key whether or not anything is dropped, so the
  comparison key (group 5) cannot depend on herdr's enumeration order.

### Group 6 — `sessions_read`

`tests/test_tools_sessions_read.py`: **90 passed**; the three homelab-query test files that
share the extracted failure helper stay green (161). The marker self-match and uniqueness
test now runs against the real renderer: the `MARKERS` and `RENDERINGS` tables in
`henk/tools/sessions_read.py` are keyed identically and `RENDERINGS` calls the tool's own
render functions, so no sentence exists twice.

| Mutation | Failures | Caught by (representative) |
|---|---|---|
| label filter deleted | 7 | populations partition, everything-filtered body, mixed population, caveat-yields, unusable-survives |
| `project` shape check deleted | 10 | the seven out-of-shape cases, all-unusable body |
| unusable clause suppressed beside filtered/unlisted | 4 | unusable-survives, fixed clause order, partition |
| stage 2 runs despite a stage-1 candidate | 4 | one-request-for-a-fresh-candidate, stage-one-suppresses-stage-two |
| a foreign frame's time wins the newest selection | 1 | foreign-and-unreadable-never-contribute-a-time |

The third mutation was **replanted**: the first attempt referenced a name before its
assignment and crashed the whole file with `UnboundLocalError` — fourteen "failures" that
were a crash, not a bound test. Hoisting the assignment produced the four real failures.

Decisions taken in the group:

- **G2/G3 boundary.** G3's own sentence counts unreadable frames among those *found*, so
  `found = unreadable + foreign + candidates` and G2 fires only when nothing was found at
  all — an empty 200 body or one carrying only `open`/`keepalive` frames, which is exactly
  the shape probe §1.4 measured. A poll of nothing but garbage lines is G3.
- **The caveat needs an emission condition** the delta does not state (it gives only a
  suppression rule, which would make "the notes line is absent when no clause applies"
  unreachable). It fires when at least one session is listed and neither the filtered nor
  the unlisted clause already states partiality. A clean zero-session snapshot renders a
  headline and `No live sessions.` with no notes line.
- Count-bearing sentences are pluralised (`1 session was` / `2 sessions were`) except the
  unlisted and skipped clauses, whose markers contain the plural noun.
- A candidate frame with no usable server `time` renders the unknown headline with
  `an unrecorded time` rather than a fabricated epoch, and sorts below every timed frame.
- The read budget truncates *inside* the chunk that crosses it, so a single-chunk response
  cannot defeat it; the trailing partial line is discarded, not counted as corrupt.
- `pane` is shape-checked but **not rendered** — the D10 body row and the spec's body table
  list label, status, and age only. D4's rationale for publishing `pane` (telling two
  same-label sessions apart) is therefore served on the topic, not in Henk's reply; showing
  it would be a spec amendment and is left for a follow-up, not done here.
- The tool does not re-derive the allowlist strip rule; the loader owns it
  (`normalise_label_allowlist`). It only refuses non-string or empty entries, which can
  arrive solely from a caller that bypassed the loader.
- Extracting the three backend-failure sentences into `henk/tools/backend_failure.py` also
  applies `scrub_addresses` to the `request failed: <reason>` text on the homelab-query
  path, which the design asked for and the old inline code did not do.
