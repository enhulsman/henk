# Apply decisions — `read-depth` §2 (config surface) and §3 (query registry)

**Applied:** 2026-09-02. Task groups §2 (2.1–2.6) and §3 (3.1–3.9).
Baseline before this work: **1503 passed, 12 deselected**.

Standing rule for this change, honoured here: every number and every name that
reaches the registry comes from `notes/backend-probe.md`, and nothing is
transcribed from `design.md`, `tasks.md`, the homelab docs, or the existing
`henk/` code. Where the probe record contradicts the design or the task text, the
probe wins and the contradiction is recorded below.

---

## §2 — Config surface

### What was added

| key | default | why this default |
|---|---|---|
| `homelab_query.enabled` | `true` | the query half rides the existing `tag:henk` egress; there is no host provisioning to stage, so there is nothing for an off state to protect |
| `homelab_query.query_range_max_points` | `60` | task 2.3; 60 points spreads 24h over ~24-minute buckets |
| `homelab_docs.enabled` | `false` | the corpus needs a clone, a timer and an allowlist on rp5 first (migration steps 2–6); enabled sooner it can only answer "corpus unavailable" |
| `homelab_docs.path` | `""` | no plausible-looking default: Docker auto-creates a missing bind source, so a typo would search successfully and find nothing |
| `homelab_docs.stamp_max_age_seconds` | `93600` (26 h) | design D9 — the fleet's own `BackupStale > 26h` convention, not a new number |
| `homelab_docs.read_byte_budget` | `8000` | ~2k tokens, the same per-injection bound `store.recall_render_limit` already uses |
| `homelab_docs.search_result_count` | `5` | several candidates (D8's mitigation for keyword search) and bounded, since each carries a snippet |
| `personal_data.docs_path_allowlist` | `()` | default-deny; an unset allowlist surfaces nothing |

No timeout key was added: the backends' timeouts stay `endpoints.gatus` and
`endpoints.prometheus` (task 2.3), and a test asserts no new key's name contains
`timeout`.

Defaults are read through the loader, not only declared on the dataclass —
`_bounded_settings` reads `sec.get(name, getattr(cls, name))`, the same
table-driven shape `_DELIVERY_SETTINGS` uses. rp5's `config.yaml` is
skip-worktree'd and will never carry a new key, so the loader's fallback *is* the
deployed value; every §2 test therefore goes through `Config.from_dict` rather
than reading dataclass attributes.

### Validation (2.5)

Positivity is enforced for `stamp_max_age_seconds`, `read_byte_budget`,
`search_result_count` **and** `query_range_max_points`, unconditionally (not only
when the capability is enabled), with `ConfigError` naming `<section>.<key>`.
`homelab_docs.enabled` with no `path` is a `ConfigError` naming both keys (design
D11 layer 1). Host state is deliberately not checked at load.

---

## §3 — The query registry

*(filled in when §3 landed — see below.)*

---

## Decisions made alone

1. **`docs_path_allowlist` lives in `personal_data`, not in `homelab_docs`.**
   Task 2.3 lists it among the corpus keys, which reads as a `homelab_docs` key.
   The in-repo precedent points the other way: `todo_read`'s allowlist is
   `personal_data.todo_note_allowlist`, not a key on a todo section, because the
   Tier-W data axis is reviewed as one surface. D13 says explicitly that the
   corpus complies with the *existing* personal-data scoping requirement rather
   than claiming an exemption, so it belongs beside the other two. A test pins
   `PersonalDataConfig`'s field set to exactly the three.

2. **`query_range_max_points` is validated too, and floored at 2.** Task 2.5 names
   only the staleness bound, the byte budget, and the result count. A zero point
   count makes the range step a division by zero on the first trend query, and a
   count of 1 collapses first/last/min/max onto one sample so "direction of
   travel" — which the spec requires — is unanswerable. Strengthening, not
   widening.

3. **The `enabled`-with-no-`path` refusal is implemented in §2, not deferred to
   §7.** Design D11 states it as a `ConfigError` in `from_dict`; task 7.1 tests
   that it kills startup. Implementing the check where the design puts it lets
   §7 assert the behaviour rather than build it.

4. **The two new sections are also written into the repo's `config.yaml`.** The
   sample file documents every other section's keys and rationale, and
   `test_config.py` loads it. Values are identical to the defaults, so the file
   is documentation rather than a second source of truth (a test asserts the two
   agree).

---

## Mutations

Every group's tests were written first and run red before implementation. Where a
test passed on first run, the implementation was mutated to confirm the test
binds, then reverted. A green suite that survives a deliberate defect is not
evidence.

| # | group | mutation | outcome |
|---|---|---|---|
| M1 | §2 | `HomelabQueryConfig.enabled` default flipped `True` → `False` | **caught** — 4 failures, incl. `test_the_corpus_ships_disabled_and_the_queries_ship_enabled` |
| M2 | §2 | `_bounded_settings` stops reading the config key (`sec.get(name, …)` → `getattr(cls, name)`) — the "builder never reads the key" trap task 2.1 names | **caught** — 19 failures |
| M3 | §2 | positivity check disabled (`if value <= 0` → `if False`) | **caught** — 9 failures across all four bounds |
| M4 | §2 | `stamp_max_age_seconds` default 26 h → 24 h | **caught** — 3 failures |
| M5 | §2 | corpus `enabled`-without-`path` refusal disabled | **caught** — 2 failures |
