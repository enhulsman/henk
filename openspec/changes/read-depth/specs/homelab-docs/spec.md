## ADDED Requirements

### Requirement: homelab_docs tool over a host-delivered read-only corpus
The system SHALL provide a `homelab_docs` tool (class: read-only) that answers questions
from the homelab documentation corpus, read from a directory mounted read-only into the
container. The tool SHALL support exactly two actions: `search`, which returns ranked
candidate sections, and `read`, which returns one section's text. The tool SHALL make no
network request of any kind — the corpus reaches the container as files, and keeping it
current is the host's responsibility. The tool SHALL NOT write to the corpus directory.

#### Scenario: A search returns ranked candidates
- **WHEN** the agent invokes `homelab_docs` with the `search` action and a question
- **THEN** the result lists candidate sections with their heading path, source file, and section id, ordered by score

#### Scenario: A read returns one section
- **WHEN** the agent invokes `homelab_docs` with the `read` action and a section id from a prior search
- **THEN** the result contains that section's text

#### Scenario: The tool makes no network call
- **WHEN** any `homelab_docs` invocation executes
- **THEN** it issues no HTTP request, DNS lookup, or other network operation

### Requirement: Default-deny path allowlist enforced at index build
The tool SHALL enforce a default-deny **path allowlist that is authoritative inside Henk's
own process**, using folder-boundary path-prefix matching, because the corpus mixes personal
content with the owner's work/Anamata metadata and therefore falls under the existing
default-deny personal-data scoping requirement.
An empty or unset allowlist SHALL surface **nothing** (fail closed);
there SHALL be no configuration or code path in which an absent allowlist surfaces all
files. An allowlist entry that is empty after normalization SHALL be discarded and SHALL
NOT broaden scope.

The allowlist SHALL be applied **at index build**, not at read time. A read-time filter is
insufficient: `search` ranks over the index and returns snippets, so a non-allowlisted
file's text would reach the owner through search results even though `read` would refuse
its section id.

Allowlist entries SHALL be interpreted relative to the **documentation root**, not the
mount root, so an entry names a path as it appears in the documentation tree. Filtering
SHALL compose as: file-type and subpath glob first, then the allowlist.

#### Scenario: A non-allowlisted file contributes no search candidate
- **WHEN** the allowlist excludes a file and a search matches text inside it
- **THEN** no candidate from that file appears in the result, and none of its text appears as a snippet

#### Scenario: A non-allowlisted file has no readable section
- **WHEN** the agent attempts to read a section id belonging to a non-allowlisted file
- **THEN** the id is not in the index and the read is refused

#### Scenario: Empty allowlist surfaces nothing
- **WHEN** the allowlist is empty or unset and the tool is invoked
- **THEN** the result states that no documentation is in scope and surfaces no file content

#### Scenario: Entries are relative to the documentation root
- **WHEN** an allowlist entry names a path as it appears in the documentation tree
- **THEN** it matches that file, without the owner needing to include the mount's internal prefix

### Requirement: Reads are addressed by discovered section id, never by path
The `read` action SHALL accept a **section id drawn from the tool's own index** and SHALL
NOT accept a filesystem path, a path fragment, a glob, or any other value resolved against
the filesystem. A section id absent from the index SHALL be refused with an explicit error.
Path traversal SHALL be closed by construction — because no parameter is resolved as a path
— rather than by sanitising a supplied path.

#### Scenario: An unknown section id is refused
- **WHEN** the agent invokes the `read` action with a section id that is not in the index
- **THEN** the tool returns an explicit "unknown section" error and reads no file

#### Scenario: A traversal attempt is not expressible
- **WHEN** the agent supplies a value such as `../../etc/passwd` as the section id
- **THEN** the value fails index lookup, and the tool at no point joins it to a filesystem path

### Requirement: The index covers only documentation files and follows no symlinks
The index SHALL be built only from documentation source files under the documentation
subpath of the mounted clone, matched by file extension, so that repository metadata, build
configuration, and dependency directories are never indexed. The indexer SHALL NOT follow
symbolic links, so no index entry can resolve outside the corpus regardless of what the
repository stores. Every index entry SHALL resolve inside the documentation subpath.

#### Scenario: Repository and build files are not indexed
- **WHEN** the corpus clone contains version-control metadata, build configuration, or dependency directories
- **THEN** no section in the index derives from them

#### Scenario: A symlink yields no index entry
- **WHEN** the corpus contains a symbolic link pointing outside the documentation subpath
- **THEN** the indexer does not follow it and no index entry derives from its target

### Requirement: Retrieval is section-level with stable, content-derived ids
The tool SHALL index the corpus at section granularity by splitting each document at its
markdown heading boundaries, recording each section's heading path, and SHALL NOT return
whole files as results — the corpus contains individual documents in excess of 50 KB whose
whole-file text is too large to be a usable tool result. Frontmatter SHALL be excluded from
section text.

Section ids SHALL be **derived from the section's heading path**, not from its ordinal
position, so that an id remains stable across index rebuilds for a section whose heading is
unchanged. Ordinal ids would silently remap when the corpus changes, so a search followed by
a read across an update would return different content under the same id — a case that
refusing unknown ids does not catch, because the id is reused rather than absent.

#### Scenario: A large document yields sections, not itself
- **WHEN** the corpus contains a document larger than 50 KB and a search matches within it
- **THEN** the candidates are sections of that document identified by heading path, and no result contains the whole document

#### Scenario: Frontmatter is not returned as content
- **WHEN** a document begins with frontmatter
- **THEN** the frontmatter is excluded from section text

#### Scenario: Ids survive a rebuild
- **WHEN** the corpus is updated and the index rebuilt, and a section's heading path is unchanged
- **THEN** that section's id is unchanged, so an id obtained before the rebuild still addresses the same section

### Requirement: Search treats the query as literal tokens
The `search` action SHALL treat the supplied query as literal search tokens and SHALL NOT
compile it as a regular expression, a glob, or any other pattern language.

#### Scenario: A pattern-shaped query is matched literally
- **WHEN** the agent supplies a query containing regular-expression metacharacters (for example `(a+)+$`)
- **THEN** the tool matches the characters literally, returns normally, and does not compile or evaluate the query as a pattern

### Requirement: Read results are byte-capped and announce truncation
The `read` action SHALL cap the returned section text at a configured byte budget. When a
section exceeds the budget, the result SHALL be truncated **and SHALL state that it was
truncated**, naming the section. The tool SHALL NOT return a silently shortened section.

#### Scenario: An oversized section is truncated visibly
- **WHEN** the agent reads a section whose text exceeds the configured byte budget
- **THEN** the result contains the leading portion and an explicit statement that the section was truncated

#### Scenario: A section within budget is returned whole
- **WHEN** the agent reads a section smaller than the budget
- **THEN** the result contains the full section text and no truncation notice

### Requirement: Every result carries a freshness stamp and stale is marked, never hidden
The corpus SHALL carry a stamp written by the host updater recording the source commit, the
commit's timestamp, and the time of the last successful pull. Every `homelab_docs` result —
both actions — SHALL carry the age of the last successful pull. When that age exceeds a
configured bound, or the stamp is missing, or it cannot be parsed, the result SHALL carry an
explicit staleness or "freshness unknown" marker. The tool SHALL still serve content in
those cases rather than refusing: documentation slightly out of date remains largely
correct, and a tool that goes dark on a missed pull is less useful than one that answers
with a caveat. The tool SHALL NOT present content whose stamp is absent, unparseable, or
beyond the bound as current.

The bound SHALL apply to the **last-pull** time only. The commit timestamp SHALL be reported
and SHALL **not** be bounded — a repository nobody has pushed to for weeks is healthy, while
an updater that has stopped pulling is not.

The stamp SHALL be written by the updater **after** the corpus content is updated, so a
reader never observes a fresh stamp against superseded content.

#### Scenario: A fresh result carries its stamp
- **WHEN** the stamp is present, parseable, and its last-pull time is within the bound
- **THEN** the result states the corpus commit and the age of the last pull

#### Scenario: A stale corpus is served with a marker
- **WHEN** the last-pull time is older than the configured bound
- **THEN** the result carries an explicit staleness marker naming the age, and the requested content is still returned

#### Scenario: A missing or unparseable stamp is not silently fresh
- **WHEN** the stamp is absent or cannot be parsed
- **THEN** every result carries an explicit "freshness unknown" marker, and no result presents the content as current

#### Scenario: An old commit is not treated as staleness
- **WHEN** the last pull was recent but the source commit is months old
- **THEN** the result is not marked stale, and the commit age is reported without a staleness claim

#### Scenario: A silently stopped pull is distinguishable from fresh docs
- **WHEN** the host's pull timer has stopped updating the corpus and the last-pull time therefore ages past the bound
- **THEN** results become marked stale, so a dead updater is visible from the tool output alone

### Requirement: The index follows the stamp
Because the host writes into the mounted directory while the container runs, the tool SHALL
detect that the corpus has changed and rebuild its index rather than serving an index built
from superseded content. Detection SHALL be based on the stamp's last-pull value.

#### Scenario: A pull invalidates the index
- **WHEN** the host completes a pull that changes the corpus and updates the stamp, and the tool is then invoked
- **THEN** the tool rebuilds its index and its results reflect the new content

#### Scenario: An unchanged corpus is not re-indexed per call
- **WHEN** the tool is invoked repeatedly with no change to the stamp
- **THEN** the existing index is reused

### Requirement: Corpus failures are honest, and host state never kills the agent
A configuration error — the corpus tool enabled with no path configured — SHALL be a
startup failure naming the missing setting, consistent with how other invalid configuration
is handled.

**Host state SHALL NOT be a startup failure.** When the corpus directory is missing, empty,
unreadable, or unstamped, the tool SHALL still be **registered**, and every invocation SHALL
return an explicit error naming the configured path and the specific condition. The tool
SHALL NOT be silently absent from the toolset: an absent tool produces no honest failure at
all, leaving the agent to answer documentation questions from its own priors with no
indication that the documentation was unreachable. Runtime loss of the corpus SHALL behave
identically to startup absence, so one broken-corpus condition does not produce two
different behaviours depending on when it broke.

The two default-deny conditions — corpus unavailable and allowlist empty — SHALL produce
**distinct** diagnostics, so an unset allowlist is never reported as a missing corpus.

#### Scenario: A configuration error fails startup
- **WHEN** the corpus tool is enabled and no corpus path is configured
- **THEN** startup fails with an error naming the missing setting

#### Scenario: A missing corpus degrades one tool, not the agent
- **WHEN** the corpus tool is enabled and the configured directory is absent or unreadable
- **THEN** the agent starts normally and continues serving its other capabilities, the corpus tool is registered, and each invocation returns an explicit error naming the path and condition

#### Scenario: An empty corpus is never mistaken for no match
- **WHEN** the corpus directory is present but contains no indexable documentation
- **THEN** an invocation states that the corpus is empty, distinguishably from a search that found no match

#### Scenario: Runtime loss behaves like startup absence
- **WHEN** the corpus becomes unavailable while the process is running
- **THEN** invocations return the same explicit error they would return had it been unavailable at startup

#### Scenario: The two fail-closed conditions are distinguishable
- **WHEN** the corpus is present and readable but the allowlist is empty
- **THEN** the result names the empty allowlist as the cause, and does not report a missing or unreadable corpus

#### Scenario: A file becomes unreadable mid-operation
- **WHEN** an indexed file cannot be read during a `read` invocation
- **THEN** the tool returns an explicit error naming the failure, and does not return partial or substituted content

#### Scenario: Safe defaults apply without a config file entry
- **WHEN** the deployed configuration file carries no corpus keys at all
- **THEN** the corpus settings resolve to their safe defaults through the configuration loader, and startup succeeds

### Requirement: Corpus text is excluded from audit records
The `homelab_docs` tool SHALL NOT opt into audit result capture, so no section text and no
search snippet reaches an audit record. Result capture is already a global, default-deny
property of the audit path — capture is opt-in per tool and only the handoff tool opts in —
so this requirement is an assertion about that global mechanism, not a per-tool
redaction step. The search
query SHALL likewise not be written into an audit record: it is model-authored free text
that can quote owner-personal content, which the audit log's existing exclusion of
owner-personal free text already covers.

#### Scenario: The corpus tool does not opt into result capture
- **WHEN** the set of tools whose results are captured into audit records is inspected
- **THEN** `homelab_docs` is absent from it

#### Scenario: No corpus text reaches a record
- **WHEN** a `read` invocation returns a section containing an internal address
- **THEN** no audit record written for that session contains any substring of the section text

### Requirement: The corpus is never committed to this repository
The documentation corpus SHALL reach the container at runtime and SHALL NOT be vendored
into this repository or baked into the container image. No corpus file, and no excerpt of
one, SHALL be added to version control by this capability. Test fixtures standing in for
corpus content SHALL use placeholder addresses only.

#### Scenario: The image contains no corpus
- **WHEN** the built container image is inspected
- **THEN** it contains no documentation corpus files, and the corpus is present only via the runtime mount

#### Scenario: The repository contains no corpus
- **WHEN** this repository's tracked files are inspected
- **THEN** no documentation corpus file is present, and no fixture contains a real internal address
