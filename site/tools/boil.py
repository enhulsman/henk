#!/usr/bin/env python3
"""Stroke-level boil for Henk's redrawn frames. Zero credits.

Each output frame takes one redraw and applies three independent variance sources:
  1. per-part rigid jitter (clock, window, cup, chair, figure), blended by soft masks:
     each part is redrawn a few pixels and a fraction of a degree off, but straight stays straight
  2. fine hand tremor: a low-amplitude, short-correlation displacement field along every line
  3. line-weight variation: a slowly varying local gamma on the grey strokes (keeps the brush texture)

Usage (from site/):
  uv run --with pillow,numpy,scipy python tools/boil.py --variants 4 \
      --src design/assets/v2 --out design/assets/boil-v3 --webp builds/frames-boil
Writes <out>/<name>-<k>.png at source resolution and <webp>/<name>-<k>.webp at half size,
plus <webp>/manifest.json mapping each source name to its variant files.
"""
import argparse
import json
import os
import sys
import zlib

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, map_coordinates

# part regions in the 1792x2400 frame (x0, y0, x1, y1); jitter sigma in px at full res
PARTS = {
    'clock':  ((900, 90, 1200, 560),    2.8),
    'window': ((1200, 60, 1792, 1500),  2.4),
    'cup':    ((960, 1320, 1230, 1560), 2.0),
    'chair':  ((150, 1050, 1792, 2400), 2.4),
    'figure': ((380, 560, 1400, 2400),  1.6),
}
ROT_SIGMA = 0.28      # degrees
SCALE_SIGMA = 0.003
TREMOR_AMP = 1.1      # px at full res (round 2 was 1.8 and read "a bit wobbly")
TREMOR_SIGMA = 18     # correlation length, px at full res
WEIGHT_SIGMA = 50     # px at full res
WEIGHT_AMP = 0.35     # gamma spread
BG_FLOOR = 0.06       # pin near-black to black before the gamma


def boil_one(src, rng, scale):
    H, W = src.shape
    yy, xx = np.mgrid[0:H, 0:W].astype(float)
    weights = {}
    for k, (r, _) in PARTS.items():
        x0, y0, x1, y1 = [v * scale for v in r]
        m = ((xx >= x0) & (xx <= x1) & (yy >= y0) & (yy <= y1)).astype(float)
        weights[k] = gaussian_filter(m, 40 * scale)
    wsum = sum(weights.values()) + 1e-6
    DX = np.zeros((H, W))
    DY = np.zeros((H, W))
    for k, (r, sig) in PARTS.items():
        w = weights[k] / wsum
        cx, cy = ((r[0] + r[2]) / 2) * scale, ((r[1] + r[3]) / 2) * scale
        dx, dy = rng.normal(0, sig * scale, 2)
        th = np.deg2rad(rng.normal(0, ROT_SIGMA))
        sc = 1 + rng.normal(0, SCALE_SIGMA)
        x = xx - cx
        y = yy - cy
        nx = cx + sc * (np.cos(th) * x - np.sin(th) * y) + dx
        ny = cy + sc * (np.sin(th) * x + np.cos(th) * y) + dy
        DX += w * (nx - xx)
        DY += w * (ny - yy)
    for F in (DX, DY):
        t = gaussian_filter(rng.standard_normal((H, W)), TREMOR_SIGMA * scale)
        F += t * (TREMOR_AMP * scale / (t.std() + 1e-9))
    warped = map_coordinates(src, [yy + DY, xx + DX], order=1, mode='constant', cval=0)
    g = gaussian_filter(rng.standard_normal((H, W)), WEIGHT_SIGMA * scale)
    g /= (g.std() + 1e-9)
    gamma = np.clip(1.0 + WEIGHT_AMP * g, 0.6, 1.6)
    lifted = np.clip((warped - BG_FLOOR) / (1 - BG_FLOOR), 0, 1)
    return (lifted ** gamma * 255).astype(np.uint8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default='design/assets/v2')
    ap.add_argument('--out', default='design/assets/boil-v3')
    ap.add_argument('--webp', default='builds/frames-boil')
    ap.add_argument('--variants', type=int, default=4)
    ap.add_argument('--names', nargs='*', help='subset of source names without extension')
    ap.add_argument('--seed', type=int, default=11)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    os.makedirs(a.webp, exist_ok=True)
    names = a.names or sorted(f[:-4] for f in os.listdir(a.src) if f.endswith('.png'))
    manifest = {}
    for n in names:
        im = Image.open(f'{a.src}/{n}.png').convert('L')
        scale = im.width / 1792
        src = np.asarray(im).astype(float) / 255
        files = []
        for k in range(a.variants):
            rng = np.random.default_rng(a.seed * 100000 + zlib.crc32(n.encode()) % 9973 * 10 + k)
            out = Image.fromarray(boil_one(src, rng, scale))
            out.save(f'{a.out}/{n}-{k}.png')
            out.resize((im.width // 2, im.height // 2), Image.LANCZOS).save(
                f'{a.webp}/{n}-{k}.webp', quality=88)
            files.append(f'{n}-{k}.webp')
        manifest[n] = files
        print(n, len(files), file=sys.stderr)
    json.dump(manifest, open(f'{a.webp}/manifest.json', 'w'), indent=1)


if __name__ == '__main__':
    main()
