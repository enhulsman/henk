# channel-adapter Specification (delta)

## ADDED Requirements

### Requirement: Outbound sends are serialized
The adapter SHALL serialize outbound sends: at most one send sequence — reply or proactive — SHALL be in flight at a time, and a send that begins while another is in flight SHALL wait for it to complete rather than interleave with it. The chunks of one message SHALL therefore reach the owner contiguous and in order regardless of how many tasks send concurrently, and the failure notice SHALL fire inside the same serialized sequence as the chunks it describes, as the notice contract already states. Serialization SHALL NOT drop, reorder within, or truncate any waiting send: every accepted send runs to its own outcome.

The wait is deliberately unbounded by any hold timer or chunk cap — both were designed and rejected (channel-integrity design D5) — and is bounded in fact by the in-flight message's chunk count times the per-chunk retry ceiling (`max_send_attempts × send_timeout` plus backoff). The healthy-path cost is priced by measurement, not estimate: `reminder-delivery`'s send-latency measurement on the deployed host puts the worst observed per-chunk send at ~1.1 seconds.

#### Scenario: Concurrent sends do not interleave
- **WHEN** two tasks send multi-chunk messages concurrently
- **THEN** every chunk of one message is delivered before any chunk of the other, and both report their own outcomes

#### Scenario: A waiting send is delivered, not dropped
- **WHEN** a send begins while another send is in flight
- **THEN** it waits for the in-flight sequence (including any failure notice) to finish, then runs to completion and reports its outcome

#### Scenario: The notice cannot be separated from its chunks
- **WHEN** a send fails partway while another sender is waiting
- **THEN** the failure notice is emitted before the waiting sender's first chunk

#### Scenario: Serialization is enforced by the adapter, not by caller convention
- **WHEN** the serialization guarantee is exercised in the contract tests
- **THEN** it is exercised against the adapter's real lock with a slow transport double, not against a cooperative channel double that never yields mid-send
