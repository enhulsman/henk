# Henk's public page: the brief

Status: agreed with Ezra on 2026-10-01. This file is the contract for the page. It is
deliberately not a token file: it fixes the thesis, the rules, the signature, the data
and the quality floor, and leaves palette, typeface, creature drawing, copy and timing
to the builder. Three builders (Fable 5.1, Opus 5.5, Sonnet 5.5) each produce one
version from this brief alone; Ezra compares them. Where this brief says "default",
the builder may replace it with something better and must say so in `NOTES.md`.

## What Henk is

Henk is Ezra's personal homelab agent (`README.md`, `NORTH-STAR.md` in this repo). He
runs on a Raspberry Pi 5, is reached over Signal, and subscribes to the homelab's alert
stream. When something breaks he triages it and messages Ezra the way a colleague
would: what broke, whether it matters, what he already checked, how to pick it up. He
has a closed toolset, an audit log that records every decision as a receipt, and an
attention contract: every message must either genuinely need the owner or be something
the owner asked for. Digests, heartbeats and "all is well" messages are banned.

Three statements that no other product would produce:

1. **His world:** a Pi on a shelf, one Signal thread with one contact, read ticks and
   the "typing…" indicator, a deny-all ntfy topic, Gatus green and red, an append-only
   log of receipts, Allow/Deny cards, and a plain Dutch first name.
2. **The page's one job:** make a visitor believe that an agent which is visibly busy
   and keeps deciding not to interrupt you is the interesting kind, then send them to
   the repo or the blog post.
3. **Audience and feeling:** agent builders and recruiters arriving from GitHub or
   hulsman.dev. First three seconds: calm, and a small grin at the name.

## The thesis

**You are watching Henk watch the homelab, and watching him choose not to interrupt
you.** Not "he says nothing when nothing is wrong"; every cron job does that. The page
shows the deciding.

## The register (from buzz.xyz, 2026-10-01)

Buzz's landing page is four decisions, not a design system. Carry the decisions, not
the brand:

1. **One colour owns the page**, edge to edge, a single hex. Buzz: chartreuse. Henk: see
   "the live colour".
2. **One word at absurd scale.** Buzz sets its name at about 227px. Henk's page sets
   HENK at display scale, 180px or more on desktop. The name carries the brand; there
   is no logo lockup, no tagline inside the hero type.
3. **One creature, repeated.** Buzz scatters 161 small bee sprites like confetti and
   its logo bee blinks. Henk's creature is defined below.
4. **Everything else is default.** System font for body text, default pill buttons, one
   form field at most, no cards, no icons, no gradients, no illustrations beyond the
   creature. Buzz loads one brand face; this page may load at most one display face.

What not to carry: bees, yellow, Buzz's copy, Buzz's section structure.

## The signature (mandatory)

**Typing, then nothing.** A few seconds after load, a typing indicator appears under
HENK, bubbles for a couple of seconds, and disappears. No message arrives. Beside it a
small monospace tally ticks up: `considered 1 · sent 0`. The gag repeats at random
intervals and may roam the flock (a random creature wakes, types, stops). The tally
makes it honest rather than cute: it is the attention contract as a number.

Two supporting elements make the signature true instead of decorative:

- **The live colour.** The page colour is the homelab's health state from the data feed.
  `ok` is one colour; `incident` is another. In `incident` the page changes colour and
  the one creature that wakes up does not stop: it gets the only speech line on the
  page, which says the owner has been told. This is the one case where Henk does send.
- **The today line.** One monospace line from the feed: alerts seen, triaged, sent to
  the owner. A visitor who reads only this line understands the contract.

The joke is never explained in prose. If a sentence on the page explains why the typing
indicator vanished, the page has failed.

## The creature

A tiny Henk pictogram, about 24 to 30px, scattered across the page in a pale tint of the
page colour, dozens to a hundred or more, with slight random rotation and spacing like
confetti. Drawn as a round face with a flat cap and two open eyes, nothing more. The
flock is awake, not asleep: eyes open, blinking at random every so often, one at a time.
"Considering" is eyes open plus a three-dot typing bubble. A red page gives one creature a
speech line. The creature drawing is the builder's; cap, two eyes and a round face are the
only fixed features. Inline SVG, so eyes can animate.

## Content inventory

Everything the page must contain, and nothing it may not:

- The name HENK at display scale.
- A tagline in system type. Default: "Henk knows what's going on in the homelab. He'll
  tell you if it matters." Replaceable; stay in this register, plain and dry, never
  explaining the joke.
- A small status label near the name. Default: `owner-only · v1.2`.
- The typing indicator and the `considered · sent` tally (the signature).
- The today line from the feed.
- One real Henk message, verbatim, from `last_message.text` in the feed, shown as a
  single chat bubble. It is the only speech on the page. Keep its four lines as lines;
  the Diagnosis / Fix / Pickup arc is Henk's real format.
- Three short plain-text blocks, no cards, no icons: what he watches, what he does with
  an alert, what he will never do without asking. Draw the facts from `README.md`
  "Security posture"; keep each under forty words.
- Links: "Read the code" to `https://github.com/enhulsman/henk`; the blog post, label
  "The watchdog write-up", href `/blog/henk-watchdog` (not live yet; keep the href);
  a one-line run instruction in monospace, `docker compose up -d`, labelled honestly as
  needing the owner's own config.
- A footer: "Built by Ezra Hulsman" linking to `https://hulsman.dev`, "open source", and
  a back link to hulsman.dev. No other navigation.
- No uptime percentage, no graphs, no stat strip, no feature grid, no testimonials, no
  numbered steps, no eyebrow labels, no second colour family.

## The data contract

The page fetches `./henk-public.json` relative to itself and also embeds the same
object inline as a fallback, so the file renders correctly from disk. Shape, with the
mock values the three builds must render against (the file beside this brief is the
source of truth):

```json
{ "state": "ok" | "incident",
  "incident": null | { "name": "...", "since": "<iso>", "owner_told": true },
  "today": { "alerts_seen": 212, "triaged": 3, "sent_to_owner": 1 },
  "since_launch": { "days": 70, "alerts_seen": 9114, "sent_to_owner": 41 },
  "last_message": { "sent_at": "<iso>", "text": "four lines, \n separated" } }
```

`incident_example` in the mock file is for testing the red state: the build must render
correctly when `state` is swapped to `incident` and `incident` is filled from it. Add a
way to preview the incident state without editing the file (a `?state=incident` query
parameter is fine). The live feed does not exist yet; whether the daily counts may be
public is an open decision of Ezra's and does not block the builds. Numbers in the mock
are illustrative; the shape is binding.

## Defaults the builder may replace

Record every replacement in `NOTES.md` with one line of why.

- Page colour `ok`: `#9FE06A`. Page colour `incident`: `#F0553A`. Ink: `#141414`.
  Creature tint: a pale mix of the page colour and white. One white for the bubble.
- Display face: Bricolage Grotesque 800 for HENK only, from Google Fonts. Body:
  `system-ui`. Mono: `ui-monospace`.
- Timing: first typing moment 3 s after load, 2.4 s long; then every 25 to 45 s at random.
  Blink: one creature every 4 to 9 s. Colour change on incident: 600 ms cross-fade.
- Sections separated by whitespace only, 120px or more on desktop.

## Quality floor (not negotiable)

- One self-contained `index.html`: CSS and JS inline, inline SVG creature, no framework,
  no build step, no CDN script. One Google Fonts stylesheet at most. Under 120 KB
  before the font.
- Works at 375px and 1440px wide with no horizontal scroll; the name scales fluidly.
- `prefers-reduced-motion`: the typing indicator is shown once, statically, the tally is
  static, nothing blinks, the colour change is instant.
- Visible keyboard focus on every link and control. WCAG AA contrast for all text on
  both page colours (ink on `#9FE06A` passes; check any replacement, including the
  incident colour).
- Semantic HTML: one `h1`, a `main`, a `footer`, `aria-hidden` on the decorative flock,
  `aria-live="polite"` on the tally.
- The `ok` and `incident` states both render correctly from the mock file.

## Deliverables per build

In `site/builds/<model>/`:

- `index.html`
- `NOTES.md`: ten lines or so. What you changed from the defaults and why, what you
  cut, what you would do with another hour.
- `shots/desktop-ok.png`, `shots/phone-ok.png`, `shots/desktop-incident.png`, taken at
  1440×900 and 375×812.

Builders do not read each other's directories. The winning build is later ported into
the resume-page repo as the `/henk` route under hulsman.dev, full-bleed, without the
site chrome. `site/builds/` is gitignored until that choice is made.

## Roads not taken (for the builder's judgement, not for copying)

Three directions were proposed before this brief, and this brief is a merge of the
first two:

- **Groen licht.** The page as a status light: live colour, dozing creatures, no motion
  while green. Kept: live colour, creature flock. Dropped: dozing (Henk is awake), and
  stillness as the only idea.
- **Typing….** An amber night-light page where the typing indicator appears and no
  message arrives; typing ellipses as the scattered creature. Kept: the gag as the
  signature, now with the tally. Dropped: amber (the colour must be live), ellipses as
  creature (the same idea twice).
- **Mag ik?** A work-coat blue page whose hero is an interactive Allow/Deny approval card
  that prints receipts. Dropped for this page, because the permission model deserves
  its own demo rather than sharing a page with the attention contract. A builder who
  finds a one-line way to hint at it (a receipt line, a tier label) may, if it costs
  nothing in quiet.

## Origins

Ezra saw buzz.xyz again on 2026-10-01 and wanted something as simple and as on-brand
for one of his own projects. Henk was chosen because the repo is about to go public,
blog post #5 about Henk's watchdog is due 2026-10-09 and needs a hero, and Henk had no
face. The three-build comparison is for the fun of seeing what each model does with
the same room to interpret.
