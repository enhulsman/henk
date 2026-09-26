# Apply decisions — owner-acknowledgement

Decisions made while applying the change. Each one is either an ambiguity in the spec or tasks
that had to be resolved without the owner, or a build choice a reviewer might read as an
omission. The mutation results for each group are at the end of that group's section.

Suite baseline before group 1: 3478 passed, 4 skipped. After groups 1-3: 3550 passed,
4 skipped.

## Group 1 — Config

- **The acknowledge-timeout cap is a copy in `henk/config.py`
  (`_ACKNOWLEDGE_TIMEOUT_CAP_SECONDS = 7.0`), not an import of `TYPING_REFRESH_SECONDS`.**
  Deviates from task 1.2's "imported from `henk.channel.signal`" wording; decided at the group
  1-3 review gate. There is no import cycle (`signal.py` imports only `henk.channel.base`). The
  conflict is the replay's isolation: `tests/test_replay_isolation.py` `FORBIDDEN` lists
  `henk.channel.signal` as a module "a replay must never load", and the replay calls
  `Config.load` (`henk/replay/__main__.py:89`). A module-level import fails the import-graph
  test. The first attempt, a local import inside the validator, passed that test but was probed
  to load `henk.channel.signal` on every `Config.load`, so every real replay loaded it: the
  guard stayed green while the property it names was lost. The copy keeps the property true.
  Two tests hold it: `test_the_acknowledge_timeout_cap_is_the_typing_refresh_interval` checks
  the cap behaviourally against the adapter's constant, so moving one without the other fails;
  `test_loading_config_does_not_load_the_signal_adapter_module` runs `Config.load` in a
  subprocess and asserts `henk.channel.signal` is not in `sys.modules` (mutation-checked: the
  local import turns it red on its assertion).
- **A strict boolean means `isinstance(value, bool)`.** `"false"`, `"true"`, `1`, `0` and
  `None` (a blank YAML value) are all refused. The blank case is also tested through
  `Config.load` on a real YAML file, as the owner would write it.
- **"A non-number" timeout includes numeric strings.** `"5"` is refused, although the adjacent
  `send_timeout_seconds` goes through `float()` and would accept it. Task 1.1 asks for a
  non-number to be refused, and a quoted number is not a number. Ints are accepted and stored
  as `float` (`7` → `7.0`). NaN and infinity are refused by the positivity and cap comparisons.
- **The cap is inclusive** (`<=`), so exactly `7.0` loads, as task 1.1 requires.
- **`config.yaml` now declares both keys** (`true`, `5.0`), per task 1.3. rp5's file still
  carries neither, so its effective values come from the loader's fallbacks, which the
  absent-keys test pins.
- **1.4 line numbers moved.** The stale comment was at `config.py:352-356` as cited. It is now
  replaced at `:352-359` with the per-phase wording. `config.yaml:31-33` was replaced in place.

### Mutation results (group 1)

| # | Mutant | Caught by | Result |
|---|---|---|---|
| M11 | `from_dict` fallback for `acknowledge_owner` set to `False` | `test_acknowledgement_enabled_by_default_when_the_keys_are_absent` | killed (`assert False is True`) |
| M12 | `from_dict` not passing `acknowledge_owner` at all | `test_an_explicit_false_acknowledge_owner_is_honoured` | killed (`assert True is False`) |
| M20 | timeout cap against the refresh interval removed | `test_an_out_of_range_acknowledge_timeout_is_refused[7.5/8/60]`, `test_the_acknowledge_timeout_cap_is_the_typing_refresh_interval` | killed (`DID NOT RAISE`, 4 failures) |

## Group 2 — Transport

- **`FakeBridge` records an acknowledgement only when it completes**, after any scripted fault
  clears. So "recorded in `typing`" means "the bridge completed it", and group 4's ordering
  tests (stop after an in-flight start) assert completions, not issue order.
- **`FakeBridge.ack_attempts` is added**, although task 2.1 does not name it: `(operation,
  recipient)` for every call, including refused and hung ones. Without it, task 3.1's "the
  bridge saw exactly one attempt" cannot be checked, because a refused call records nothing
  in `receipts` or `typing`.
- **`ack_faults` is persistent per operation, not one-shot.** An exception is raised on every
  call of that operation. An `Event` is awaited on every call until it is set, then passes
  through. A test that needs "one hung refresh, then healthy" sets the event or removes the key.
- **`hold_open`** awaits a fresh `asyncio.Event()` that is never set, once the script is
  exhausted. A test covers both behaviours.
- **The three bridge methods share one private `_acknowledgement(method, path, payload, what)`**
  on `SignalCliRestBridge`. It uses `client.request(...)` for all three verbs, so the DELETE
  carries its body. Each call builds its client through `_build_client()`. There is no retry
  and no bound, and every failure becomes a `SignalBridgeError`.
- **The timeout pin in 2.2 is asserted at the transport.** The handler checks
  `request.extensions["timeout"]`, the per-phase dict httpx hands the transport. The test also
  checks the patched client's `timeout`. A method that bypassed the patched factory would
  reach no handler and fail on the request count.
- The routes (`/v1/receipts/{account}`, `/v1/typing-indicator/{account}`) are the ones task 2.2
  names. They are not verified against rp5's image. Tasks 12.3 and 12.5 do that.

## Group 3 — Contract and Signal adapter

- **Stranger placeholder.** Standing rule 3 says to use the stranger placeholder the existing
  tests already use, a `+31` number distinct from the owner's and the rollback placeholder. The
  pre-commit hook (`.githooks/pre-commit`, phone-number check) refuses every `+31` number in
  added lines except the two it allowlists, so that existing placeholder cannot appear in a new
  line. Where group 3 needed a
  non-owner sender, I used the UUID placeholder `00000000-0000-4000-8000-000000000000` for both
  `source` and `sourceUuid`. **Groups 5 and 8 will hit this too.** Their stranger tests need a
  non-owner identity that the hook accepts, such as the UUID placeholder or a non-`+31` value.
- **A reference is minted only from a JSON integer.** `_mint_channel_ref` returns `None` for an
  absent, zero, boolean or non-integer timestamp. signal-cli-rest-api reports milliseconds as
  an integer. A string timestamp therefore gets no reference and no receipt, rather than a new
  `int()` failure inside `messages()`. `InboundMessage.timestamp` is computed exactly as before.
- **A reference is parsed more strictly than `int()`.** `_parse_channel_ref` accepts only the
  exact form `_mint_channel_ref` produces: ASCII digits, positive, and a round trip through
  `str(int(x))` (so no leading zeros). `" 12"`, `"+12"`, `"1_000"`, `"012"`, non-ASCII digits,
  `"0"` and `"-5"` are refused without a bridge request. This follows the spec's "a channel
  reference that is not one the adapter mints" literally.
- **`acknowledge(None)` returns `False`**, as task 3.1 says. The spec's "no-op" (no request, no
  failure logged) is kept: the adapter never logs, and the dispatcher skips `None` before
  calling (task 5.3).
- **Only `SignalBridgeError` is caught** in the adapter's acknowledgement operations, as
  `_send_chunk` does for sends. Any other exception propagates to the caller. Group 4's
  bounded helpers catch `Exception` there.
- **The helper is named `_to_owner(operation, *args)`.** It passes `self._owner` as the
  recipient, which makes owner-only structural in one place. It takes no lock and has no
  bound, so it adds no `Lock`, `wait_for` or `timeout` call. The lock test and the
  client-construction test pass untouched.
- **3.2 extends the existing inspection test**
  (`test_no_send_operation_exposes_an_arbitrary_recipient`) to iterate `SEND_OPERATIONS` and
  a separate `ACK_OPERATIONS` dict, with the same exact-list and denylist assertions. This
  test and the new top-level `import httpx` are the only edits to existing test code.
- **3.4:** a test for `FakeChannel.acks` sits in `test_channel_adapter.py`. The standing
  rule 6 grep (`Dispatcher(`, `.sent`, `.calls`, `.sends`) was re-run after the change. Sorted,
  and excluding the two files this group edits, its hit set is identical to before, so no
  existing observer changed.
- **The DEPLOY-VERIFY note in `_convert`** now says that a delivered-but-never-read owner
  message means a silent allowlist drop, or a failed receipt, which is logged.

### Mutation results (group 3)

| # | Mutant | Caught by | Result |
|---|---|---|---|
| M9 | acknowledgement operations taking `_send_lock` | `test_an_acknowledgement_does_not_wait_behind_a_send_in_flight` | killed (`TimeoutError` from the 2 s bound) |
| M10 | `_convert` minting `"0"` for a timestamp-less envelope | `test_timestamp_less_envelope_carries_no_channel_reference` (4 cases) | killed (`assert '0' is None`) |
| M14 | `sender` parameter smuggled into `acknowledge` and used as the recipient | `test_no_send_operation_exposes_an_arbitrary_recipient` | killed (`Left contains one more item: 'sender'`) |

Each mutant was applied alone to a scratch-backed copy and reverted by copying the backup back.
A `diff` against the backups confirmed both source files were restored.
