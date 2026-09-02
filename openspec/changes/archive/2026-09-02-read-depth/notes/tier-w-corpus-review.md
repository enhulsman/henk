# Tier W review of the documentation corpus (task 8.5)

Reviewed 2026-09-02 against the local checkout of the docs repository at its
2026-08-21 head. Owner decision of 2026-08-22: initial allowlist membership is
**all 17 files**. This note records the reasoning behind that decision, not just
the outcome, so it can be re-run when the corpus changes.

## What was checked

All 17 files under the documentation root (about 297 KB), by full read of the
structure and a pattern scan for credential *values*: private-key blocks, GitHub
and ntfy token prefixes, cloud access-key shapes, bearer strings, inline
`password:`/`passphrase:` values, 40+-character hex secrets, Slack token shapes.

| finding | count | verdict |
|---|---|---|
| credential values of any shape | **0** | nothing to exclude |
| files carrying tailnet addresses | 12 of 17 | owner-scoped infrastructure facts; permitted |
| files mentioning the owner's employer or its work systems | 7 of 17 | owner work *metadata*; allowlist-eligible under the 2026-08-07 refinement |
| client data | **0** | none exists here — it lives only on client-issued machines |

## Where Tier W's line falls, applied

- **Client data is the hard wall**, and it is physical: nothing client-owned is on
  the homelab or in this repository. Not present, so not at issue.
- **Owner work metadata** (work repository paths, the work GitHub account name, a
  work-owned VPS tunnel) is owner-scoped and shareable by explicit allowlist. It is
  present in the seven files, and it is also among the content most useful to the
  job — the tunnels and the deploy paths are exactly what a "where does X run"
  question needs.
- **Credentials are out regardless of tier.** None are in the corpus. Credential
  *locations* (`.env` paths, key filenames, the note that several SSH keys share a
  passphrase, the break-glass account's name) are present and are **not**
  credentials; they are the map, not the keys. The security-model files that hold
  them are the ones Henk needs most when reasoning about an incident.

## Why all 17 rather than a subset

Excluding the security-model and network files would remove the content that
distinguishes Henk from a generic assistant while protecting nothing that is not
already protected by the output constraints: Henk's replies reach one allowlisted
Signal identity, his notifications reach deny-all ntfy topics, and no tool takes a
recipient. The corpus does not raise what Henk can *do*; it raises what he can
*see*, and the North Star ties that to how far his output reaches — which this
change does not widen.

## Standing conditions

The mechanism stays default-deny. A new file added upstream is invisible until it
is added to `personal_data.docs_path_allowlist` on rp5. Re-run the scan above when
a file is added; if a credential value ever appears in the docs, the fix is
upstream (remove it from the docs), not a Henk exclusion.

## Allowlist as deployed (paths relative to the documentation root)

```yaml
personal_data:
  docs_path_allowlist:
    - architecture.md
    - index.mdx
    - devices/pi2.md
    - devices/pi5.md
    - devices/vps.md
    - devices/workstation.md
    - network/cloudflare.md
    - network/ports.md
    - network/security.md
    - network/tailscale.md
    - operations/backup-recovery.md
    - operations/future-improvements.md
    - operations/maintenance.md
    - operations/troubleshooting.md
    - services/applications.md
    - services/dns.md
    - services/monitoring.md
```
