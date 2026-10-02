# Launch prompt, round 2, direction A: the clock on the wall

Paste verbatim into a fresh Fable general-purpose agent.

---

You are building one of two independent drafts of a public landing page for Henk, a
personal homelab agent. Yours is direction A, "the clock on the wall". Work only from
`/home/wsl/Coding/henk/site/BRIEF-v2.md` and the files it names (`henk-public.json`
beside it; `README.md` and `NORTH-STAR.md` in `/home/wsl/Coding/henk`), plus the
reference image `/home/wsl/Coding/henk/site/design/references/a-chosen-cobalt-1F3BE0.png`
(the SVG beside it is the same comp as vectors; you may read its shapes but must not
embed or trace it). Build only in `/home/wsl/Coding/henk/site/builds/a-clock/`. Do not
open any other directory under `site/builds/` or `site/design/`, and do not look at
buzz.xyz or any other site; the brief carries what matters from it.

Decisions already made for you, do not reopen them:

- The page colour is cobalt `#1F3BE0`, one hex, edge to edge. Eyes, HENK and all type
  are cream `#F4EEDC`. The cap band and pupils are near-black `#15171A`. No other colour.
- The cap band is CSS, not an image: a dark band fixed to the top of the viewport whose
  lower edge is the brim curve you see in the chosen comp. On scroll the band and the
  eyes stay fixed; content slides under them.
- The tick is `steps()`, not an ease. Left, right, left, right, one step per second.
  Blink about every eight seconds (scaleY on the eye, pupils included). Pointer glance:
  when the pointer moves over the page the pupils break the tick and look toward it for
  about a second, then resume ticking; on touch devices a tap does the same. Incident:
  eyes stop and look down, a typing bubble appears beneath them, nothing changes colour.
- HENK in Dela Gothic One (one Google Fonts stylesheet, the only one), sized by viewport
  width so the H and K are clipped by the viewport edges on desktop. On a phone HENK
  may fit instead of clipping if clipping would cost legibility; say so in NOTES.

Read the brief in full first. Then, before writing code, settle in your head: eye size
and placement relative to the band and HENK, exact tick and blink timings, the glance
mechanism, the incident choreography, the copy below the fold, and the one thing a
visitor will remember. Check the plan against the brief's register and quality floor.
Then build.

Deliver into your directory exactly: `index.html` (self-contained, see the quality
floor; copy `henk-public.json` beside it so the relative fetch works, keep the inline
fallback), `NOTES.md` (about ten lines: what you decided, what you cut, what you would
do with another hour, one optional alternative line), and `shots/desktop-ok.png`,
`shots/phone-ok.png`, `shots/desktop-incident.png` at 1440×900 and 375×812.

Take the screenshots with a local Playwright script, not the shared browser tools: write
a short Node script in your directory that imports
`/home/wsl/Coding/resume-page/node_modules/playwright/index.mjs`, run it with
`/home/wsl/Coding/resume-page` as the working directory, open your `index.html` over
`file://`, set the viewport, wait for the font to load, and screenshot into `shots/`.
Preview the incident state with `#incident`. Look at every screenshot yourself and fix
what is wrong before finishing: HENK at display scale and clipped, the eyes reading as
eyes under a cap, the typing bubble visible in the incident shot, no horizontal scroll
at 375px, cream on cobalt everywhere.

Do not commit, push, or touch any file outside your directory. When done, reply with
the list of files you produced and the three screenshot paths, nothing more.
