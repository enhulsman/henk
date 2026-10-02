# Henk's public page: brief, version 2

Status: direction agreed with Ezra on 2026-10-01 evening. Supersedes `BRIEF.md` for
anything it contradicts; the register rules and quality floor from version 1 still
apply and are repeated here where they matter. Two fresh builders each take one
direction (A or B) and work from this file, the references in `design/references/`
and the assets in `design/assets/` only.

## What changed since version 1, and why

Version 1 made Henk's alert pipeline the hero and tried to prove it with numbers. The
numbers are real but they are not the page: Henk is used lightly, and a tally of
incidents is a blog post, not a poster. The page's job is what buzz.xyz's page does for
Buzz: one colour you cannot stop looking at, one word, one living thing, and the name's
personality carried by the whole field, so that no sentence has to explain him.

**Out:** the live health colour, the funnel tally, any count from the audit log, the
flock of small Henks, the typing-then-nothing gag as a hero motion, any mention of
alerts, Gatus, Prometheus, tiers or receipts above the fold.

**In:** Henk as a character. A Dutch neighbour: flat cap, the living room with the
clock on the wall, coffee, looks out of the window, knows everyone's business,
dependable, a little funny. Not a server.

## The register (unchanged)

1. One colour owns the page, edge to edge, one hex.
2. One word at absurd scale: HENK, large enough to clip the viewport edges on desktop.
3. One living thing. Not many; one, moving.
4. Everything else default: system font for body, default pill buttons, no cards,
   icons, gradients or second colour family. At most one display face loaded.

## The line

"Henk keeps an eye on things." Under it, smaller, the Dutch stamp: "Henk let op."
Builders may propose one alternative in NOTES.md but build with this one.

## Shared content below the fold

Keep it to what version 1 allowed, minus the numbers: three short plain-text blocks
(what he watches, what he does when something breaks, what he will never do without
asking; under forty words each, from `README.md` "Security posture"), the real Henk
message from `henk-public.json` as one chat bubble, "Read the code" to
`https://github.com/enhulsman/henk`, "The watchdog write-up" to `/blog/henk-watchdog`,
the honest `docker compose up -d` line, and the footer. The hero carries everything;
the rest should feel like an afterthought the visitor scrolls into.

## Direction A: the clock on the wall (`builds/a-clock/`)

**Concept.** The page is Henk's face, and Henk is a Kit-Cat clock. Two huge eyes near
the top of the field tick left, right, left, right, once a second, forever.

**The living thing.** The eyes. They blink about every eight seconds. When the pointer
crosses the page they break the tick for a moment and follow it, then return to
ticking, like a neighbour noticing someone walk past the window. On touch devices the
glance follows a tap. When the feed says `incident`, the eyes stop ticking and look
down, and a typing bubble appears beneath them; that is the entire incident state, no
colour change.

**The cap.** The top edge of the viewport is a dark band with a brim, so the whole
screen reads as a face under a flat cap without a face being drawn. On scroll the cap
and the eyes stay fixed and keep ticking; the content below slides past under them.

**Colour.** Decided by the comps in `design/references/a-*.png`: deep electric cobalt
with cream eyes and cream type, or brick terracotta with the same. Read the chosen
comp's filename; `a-chosen-*.png` is the one. One hex, edge to edge.

**Type.** Dela Gothic One for HENK (the face that won round one), system-ui for the
rest. HENK sits under the eyes and is clipped by the viewport edges on desktop.

**Motion rules.** The tick is a CSS animation with a hard step, not an ease: a clock,
not a pendulum. Blink and glance are the only other motions. `prefers-reduced-motion`:
eyes centred and still, one blink on load, no glance.

## Direction B: the tile (`builds/b-tile/`)

**Concept.** A Delft tile the size of the screen. Cobalt field, white strokes. In the
middle a brush-painted Henk at a window, animated as a flipbook at about two frames a
second from the frames in `design/assets/b-frame-*.png`: sips coffee, looks out, nods.
Classic corner ornaments, painted, not vector-perfect.

**The living thing.** The flipbook Henk. Nothing else moves. The loop is slow and a
little uneven on purpose; a hand-painted figure that moves like a GIF is the charm.

**Colour.** The cobalt of real Delft tiles, warm off-white strokes. Decided by the comp
`design/references/b-chosen-*.png`; take the hex from it.

**Type.** HENK in white, either Dela Gothic One or a face that sits with the painted
strokes; the builder decides and says why. Corner ornaments and figure are images,
everything else is HTML.

**Incident.** The painted Henk turns from the window toward the viewer and a typing
bubble appears; no colour change.

## Data

`henk-public.json` beside this file still feeds two things: `state` (`ok` or
`incident`) for the eye or figure behaviour, and `last_message` for the one chat
bubble below the fold. Nothing else from it is shown. Fetch relative, fall back to the
inline copy, preview the incident state with `#incident`.

## Quality floor (unchanged from version 1)

Single self-contained `index.html` plus the image assets it references by relative
path; no framework, no build step, at most one Google Fonts stylesheet; works at 375
and 1440 wide with no horizontal scroll; reduced motion honoured; visible focus; WCAG
AA for text on the page colour; one `h1`, `main`, `footer`; decorative images
`aria-hidden`, the eyes or figure described once in an `aria-label`.

## Deliverables per build

`index.html`, `NOTES.md` (about ten lines: what you decided, what you cut, what you
would do with another hour), `shots/desktop-ok.png`, `shots/phone-ok.png`,
`shots/desktop-incident.png` at 1440×900 and 375×812. Screenshot with a local Playwright
script, not the shared browser.
