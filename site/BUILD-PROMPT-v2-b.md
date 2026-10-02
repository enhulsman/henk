# Launch prompt, round 2, direction B: the tile

Paste verbatim into a fresh Fable general-purpose agent.

---

You are building one of two independent drafts of a public landing page for Henk, a
personal homelab agent. Yours is direction B, "the tile". Work only from
`/home/wsl/Coding/henk/site/BRIEF-v2.md` and the files it names (`henk-public.json`
beside it; `README.md` and `NORTH-STAR.md` in `/home/wsl/Coding/henk`), the reference
image `/home/wsl/Coding/henk/site/design/references/b-chosen-figure-3-delft-274B9F.jpg`,
and the assets in `/home/wsl/Coding/henk/site/design/assets/`:
`b-frame-1.png` to `b-frame-4.png` (the flipbook: at rest, sips, leans to the window,
nods), `b-incident.png` (he turns to the viewer) and `b-ornament.png` (one painted
corner ornament, top-left). Build only in `/home/wsl/Coding/henk/site/builds/b-tile/`.
Do not open any other directory under `site/builds/` or `site/design/`, and do not look
at buzz.xyz or any other site; the brief carries what matters from it.

Decisions already made for you, do not reopen them:

- The page colour is Delft cobalt `#274B9F`, one hex, edge to edge. Strokes and type are
  white or warm off-white. No other colour.
- The assets are white strokes on pure black. Place them as `<img>` with
  `mix-blend-mode: screen` over the cobalt field; the black disappears and the strokes
  sit on the blue with no cut-out edge. Do not remove backgrounds, do not recolour.
- Copy the assets into your directory, downscaled for the web (about 1200px tall for the
  frames, 800px for the ornament, PNG or WebP, under 300 KB each), and reference them by
  relative path. Never reference `design/assets/` from `index.html`.
- The flipbook cycles frames 1 to 4 at about two frames a second on an uneven loop (for
  example 450, 600, 500, 550 ms), forever; it may hold frame 1 a little longer. It is the
  only moving thing on the page. Incident: show `b-incident.png` and a typing bubble; no
  colour change. Reduced motion: frame 1 only.
- The ornament goes in all four corners by mirroring the one image with CSS transforms.
  Scale it so the corners read as tile ornaments, not as a frame; it must never collide
  with the figure or HENK at 375 or 1440 wide.
- HENK is white. Use Dela Gothic One or one other face that sits better with painted
  strokes; one Google Fonts stylesheet at most, and say in NOTES which face and why.

Read the brief in full first. Then, before writing code, settle in your head: figure
size and position (he is the living thing; the page is his tile), where HENK sits
relative to him, ornament scale, loop timings, the incident choreography, the copy
below the fold, and the one thing a visitor will remember. Check the plan against the
brief's register and quality floor. Then build.

Deliver into your directory exactly: `index.html` (self-contained apart from the image
assets it references by relative path; copy `henk-public.json` beside it so the relative
fetch works, keep the inline fallback), the downscaled assets, `NOTES.md` (about ten
lines: what you decided, what you cut, what you would do with another hour, one optional
alternative line), and `shots/desktop-ok.png`, `shots/phone-ok.png`,
`shots/desktop-incident.png` at 1440×900 and 375×812.

Take the screenshots with a local Playwright script, not the shared browser tools: write
a short Node script in your directory that imports
`/home/wsl/Coding/resume-page/node_modules/playwright/index.mjs`, run it with
`/home/wsl/Coding/resume-page` as the working directory, open your `index.html` over
`file://`, set the viewport, wait for the font and images to load, and screenshot into
`shots/`. Preview the incident state with `#incident`. Look at every screenshot yourself
and fix what is wrong before finishing: the figure painted on the blue with no black
box around him, HENK at display scale, the ornaments in four corners, the typing bubble
visible in the incident shot, no horizontal scroll at 375px.

Do not commit, push, or touch any file outside your directory. When done, reply with
the list of files you produced and the three screenshot paths, nothing more.
