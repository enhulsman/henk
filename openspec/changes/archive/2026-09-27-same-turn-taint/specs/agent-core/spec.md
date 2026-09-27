## ADDED Requirements

### Requirement: Taint raised during a turn persists for the session
When the gate's turn context is tainted at the end of a framed turn because a taint-source tool raised it (approval-gate spec, *Tools may declare themselves taint sources*), the agent core SHALL mark the session tainted for its lifetime and SHALL remember which tool raised it, so every later turn of that session is framed tainted with that source. This SHALL happen on every exit path of the turn, including a turn that errors after the taint was raised. The session's taint and its source SHALL be cleared only where session taint is cleared today: when the session ends (`/new`, idle expiry, or an event turn displacing it) and a new one starts. An event turn that displaces a tool-tainted session SHALL start its incident session with incident taint only, carrying no tool source. Owner commands run outside any turn and SHALL be unaffected. A session in which no taint source was called SHALL be framed exactly as before.

#### Scenario: The next turn of the session is tainted
- **WHEN** an owner turn invokes a taint-source tool, and in the next owner turn of the same session the agent invokes `store_memory`
- **THEN** `store_memory` is denied with outcome `out-of-scope`, the tool result is exactly the tool-taint reason naming the taint source, and the receipt's `detail` names it

#### Scenario: A turn that errors after raising taint still taints the session
- **WHEN** an owner turn invokes a taint-source tool and then fails with an error, and the next owner turn of the same session invokes `store_memory`
- **THEN** `store_memory` is denied with outcome `out-of-scope`

#### Scenario: /new clears a tool-raised taint
- **WHEN** a taint-source tool tainted the session and a write in the next turn is refused, the owner sends `/new`, and the next owner turn invokes `store_memory`
- **THEN** `store_memory` executes

#### Scenario: Idle expiry clears a tool-raised taint
- **WHEN** a taint-source tool tainted the session and a write in the next turn is refused, the idle timeout passes, and the next owner turn invokes `store_memory`
- **THEN** `store_memory` executes

#### Scenario: An incident displacing a tool-tainted session carries no tool source
- **WHEN** a taint-source tool tainted an owner session, an event turn then displaces it, and in the owner follow-up of the incident session the agent invokes `store_memory`
- **THEN** `store_memory` is denied with the existing incident reason and a null receipt `detail`

#### Scenario: Owner commands still write after a tool-raised taint
- **WHEN** a taint-source tool tainted the session and a write in the next turn is refused, and the owner then sends `/remember <fact>`
- **THEN** the fact is stored
