# Test B guides from R1: full skeleton (dense centrelines) and a sparse gesture guide.
# Usage: uv run --with pillow,numpy,scikit-image,scipy python guides.py
import numpy as np
from PIL import Image, ImageDraw
from skimage.morphology import skeletonize, binary_dilation, disk
R1 = 'design/assets/v2/R1.png'; OUT = 'design/assets/guides/'  # run from site/
im = np.asarray(Image.open(R1).convert('L')).astype(float) / 255
mask = im > 0.45
sk = skeletonize(mask)
sk = binary_dilation(sk, disk(1))
Image.fromarray((sk * 255).astype(np.uint8)).save(OUT + 'R1-skeleton.png')
print('skeleton px', int(sk.sum()), 'of', mask.size)

# sparse gesture guide, ~14 lines, coordinates in the 1792x2400 frame
W, H = 1792, 2400
g = Image.new('L', (W, H), 0); d = ImageDraw.Draw(g); w = 3
L = lambda *p: d.line(p, fill=255, width=w)
# window
L((1300, 100), (1300, 1370))            # outer jamb
L((1630, 400), (1630, 1370))            # inner right
L((1370, 455), (1680, 385))             # top slope
L((1415, 905), (1655, 905))             # cross bar
L((1200, 1355), (1680, 1415))           # sill
d.ellipse((1050-85, 250-115, 1050+85, 250+115), outline=255, width=w)  # clock
d.ellipse((696-180, 816-155, 696+180, 816+155), outline=255, width=w)  # head
L((576, 960), (456, 1800))              # back / spine
L((840, 1080), (960, 1476))             # arm
L((504, 1572), (1200, 1596))            # thigh
L((1200, 1596), (1150, 2110), (1345, 2195))  # shin + foot
L((228, 1104), (312, 2244))             # chair back
L((312, 1776), (910, 1776))             # seat
L((768, 1800), (790, 2255))             # front leg
d.ellipse((1080-72, 1430-60, 1080+72, 1430+60), outline=255, width=w)  # cup
L((996, 1512), (1200, 1512))            # saucer
g.save(OUT + 'R1-gesture.png')
# overlay for sanity: guide in red over dimmed R1
ov = Image.merge('RGB', (Image.fromarray((im*120).astype(np.uint8)),)*3)
ovd = np.asarray(ov).copy(); ga = np.asarray(g) > 0; ovd[ga] = (255, 60, 60)
Image.fromarray(ovd).resize((W//2, H//2)).save(OUT + 'R1-gesture-overlay.jpg', quality=85)
# chair-stroke crop as a style swatch (no pose information)
Image.open(R1).crop((180, 1700, 780, 2300)).save(OUT + 'style-swatch-chair.png')
print('done')
