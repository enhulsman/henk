# Launch prompt, round 3, direction B: the tile, boiled

Paste verbatim into a fresh Fable general-purpose agent.

---

You are evolving the winning draft of a public landing page for Henk, a personal homelab
agent, from a four-frame flipbook into a hand-drawn boil animation with real sequences.
Direction B, "the tile", has already won; do not redesign it. Start from the existing
build and change only what this prompt says.

Read first, in this order:

1. `/home/wsl/Coding/henk/site/BRIEF-v2.md` (register, quality floor, shared content).
2. `/home/wsl/Coding/henk/site/BUILD-PROMPT-v2-b.md` (the decisions that produced the
   current build; everything in it still holds unless this prompt overrides it).
3. `/home/wsl/Coding/henk/site/builds/b-tile/index.html`, `NOTES.md` and `shoot.mjs`: the
   build you start from. Copy the whole directory to
   `/home/wsl/Coding/henk/site/builds/b-tile-v3/` and work only there.
4. `/home/wsl/Coding/henk/site/builds/frames3.html`: the sequencing prototype. Its script
   is the specification for the animation; read every line of it.
5. `/home/wsl/Coding/henk/site/builds/frames-boil/manifest.json` and the WebP frames
   beside it (896×1200, white strokes on pure black).
6. `/home/wsl/Coding/henk/site/builds/type4.html`: the hero type pairings; the tile with
   Fredericka the Great over Meddon is your typographic reference.

Do not open any other directory under `site/builds/` or anything under `site/design/`.

## Decisions already made, do not reopen them

**Frames.** The four old frames and `incident.webp` are replaced by the boiled set. Use
exactly these bases from the manifest, all four variants of each:

- rest: `R1`, `R2`, `R3`
- sip: `S1a`, `S1b` (lift), `S2e`, `S2f` (drink)
- window: `W1b` (lean in and out), `W1a`, `W1c` (hold)
- nod: `N1a` (dip), `N2a`, `N2b` (down)
- incident: `I1`, `I2`

That is 15 bases, 60 files. Copy them into `b-tile-v3/frames/`, re-encode if needed so
every file is under 120 KB (quality 80 to 85 is fine; the strokes forgive it), and
reference them by relative path. Never reference `frames-boil/` or `design/` from
`index.html`. Same placement as before: `<img>` with `mix-blend-mode: screen` over the
cobalt, no transform or opacity on the figure's container, the black disappears.

**Sequencing.** Port the state machine from `frames3.html` faithfully:

- A hold shows one random boiled variant of its bases after another, never the same
  file twice in a row, every `step × rand(0.85, 1.25)` ms. The default step is **170 ms**.
- Idle: hold the three rest bases for a random 2.5 to 6 s, then play one weighted
  sequence (sip 4, window 3, nod 2), then idle again, forever.
- sip: S1a/S1b 400–600 ms, then S2e/S2f 900–1500 ms, then S1a/S1b 350–500 ms.
- window: W1b 250–400 ms, then W1a/W1c 1800–3200 ms, then W1b 250–400 ms.
- nod: N1a 300–450 ms, then N2a/N2b 600–900 ms, then N1a 250–400 ms.
- Every hold and every sequence checks a generation counter and stops the moment the
  state changes. The old prototype had an idle hold fighting the incident loop for up to
  six seconds; that bug must not come back. Write the machine so a state change can
  interrupt anything at the next frame.
- The clock is part of the frames and boils with them. There is no separate clock layer.
- Pause the loop while the tab is hidden, as the current build does.

**The look-at-you moment.** In the `ok` state, every 60 to 90 s, Henk turns to the viewer:
hold I1/I2 for 4 to 6 s with the typing bubble appearing about half a second after he
turns, exactly as the current incident choreography does it, then the bubble leaves and
he returns to the rest hold and the idle rhythm resumes. The first one happens between
20 and 35 s after load, so a visitor who stays sees it once. It carries no words.

**Incident from the feed.** `henk-public.json` still feeds `state` and `last_message` and
nothing else. When the feed says `incident`, the figure holds I1/I2 with the typing bubble
and stays there; no colour change, no alerting word above the fold. `#incident` previews
it, `#ok` returns, as now. Fetch relative, keep the inline fallback.

**Loading.** No frame is shown before it is decoded. Decode the 12 rest frames first and
start the idle hold the moment they are in; decode the rest in the background and enable
each sequence (and the look-at-you moment) only once all of its frames have decoded.
Before the rest frames are in, show the first rest frame as a plain `<img>` so the page
never paints without Henk. Set `width` and `height` on the images. The frames are the
only moving thing on the page.

**Reduced motion.** `prefers-reduced-motion`: one rest frame, static, no sequences, no
look-at-you moment; the incident state still swaps to one incident frame with the bubble.

**Type.** HENK is set in **Fredericka the Great**, white, clipped by both viewport edges
as now; retune the size so the word fills the width the way Dela Gothic One did (type4
scaled it by about 1.04 relative to Dela). The tagline is set in **Meddon** and reads,
verbatim and with the full stop:

> Someone has to keep an eye on things.

It sits at his eye level to the right, where his gaze goes, as now; on the phone it moves
under HENK, centred. Meddon runs small; type4 used about 0.78 of the italic's size. One
Google Fonts stylesheet with both faces, `display=swap`, and preconnect. Everything else
stays system.

**Below the fold.** Unchanged, with one fix: "The watchdog write-up" links to
`/blog/henk-exactly-once`, not `/blog/henk-watchdog`.

**Colour, ornament, blend, accessibility.** Unchanged from the current build and the
round-2 prompt: `#274B9F` edge to edge, four mirrored ornaments that never collide with
the figure or HENK at 375 or 1440 wide, visible focus, no horizontal scroll at 375,
`aria-label` on the figure changing with the state.

## Build

Read everything listed above in full first. Then, before writing code, settle: how the
state machine is structured so interruption is trivial; how decoding gates each sequence;
where the look-at-you timer lives so it never fires during an incident or while hidden;
how the Fredericka HENK is sized against the figure; how Meddon sits at his eye level.
Then build.

Deliver into `b-tile-v3/` exactly: `index.html`, `frames/` (60 WebP files), `ornament.webp`,
`henk-public.json`, `shoot.mjs` (updated), `NOTES.md` (about twelve lines: what you
decided, total frame weight, what you cut, what you would do with another hour), and
`shots/desktop-ok.png`, `shots/phone-ok.png`, `shots/desktop-incident.png` at 1440×900
and 375×812, plus `shots/desktop-look.png` taken during a look-at-you moment (force it
with `#look` for the screenshot only).

Take the screenshots with a local Playwright script, not the shared browser tools: adapt
`shoot.mjs`, which imports `/home/wsl/Coding/resume-page/node_modules/playwright/index.mjs`
and runs with `/home/wsl/Coding/resume-page` as the working directory, opening
`index.html` over `file://`. Also make `shoot.mjs` assert, by reading the live DOM at
intervals over 15 s: no two consecutive shown frames are the same file; the shown frame
is always one of the 60 and always fully decoded; forcing `#incident` mid-hold switches
to an incident frame within 400 ms and no rest frame shows afterwards. Print the results.
Look at every screenshot yourself and fix what is wrong before finishing: the figure
painted on the blue with no black box, HENK in Fredericka at display scale filling the
width, the Meddon line legible at his eye level, the ornaments in four corners, the
typing bubble visible in the incident and look shots, no horizontal scroll at 375px.

Do not commit, push, or touch any file outside `b-tile-v3/`. When done, reply with the
list of files you produced, the total weight of `frames/`, the assertion results, and
the four screenshot paths, nothing more.
