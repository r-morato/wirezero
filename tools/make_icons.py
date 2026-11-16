"""Draw the WireZero icon (store 256px + launcher 100px). Run: python3 tools/make_icons.py"""

import os

from PIL import Image, ImageDraw

S = 1024
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "packaging")


def draw():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, S - 1, S - 1), fill=(13, 17, 23, 255))  # store adds its own corners
    cx, base = S // 2, 690
    # Three nested arches: a tunnel mouth.
    for r, w, col in ((360, 64, (31, 111, 235)), (255, 56, (56, 139, 253)),
                      (150, 50, (121, 192, 255))):
        d.arc((cx - r, base - r, cx + r, base + r), 180, 360, fill=col, width=w)
        d.line((cx - r + w // 2, base, cx - r + w // 2, base + 90), fill=col, width=w)
        d.line((cx + r - w // 2, base, cx + r - w // 2, base + 90), fill=col, width=w)
    # Green "connected" light inside the tunnel.
    d.ellipse((cx - 62, base - 40, cx + 62, base + 84), fill=(63, 185, 80))
    return img


if __name__ == "__main__":
    big = draw()
    big.resize((256, 256), Image.LANCZOS).save(os.path.join(OUT, "icon-256.png"))
    big.resize((100, 100), Image.LANCZOS).save(os.path.join(OUT, "wirezero.png"))
    print("icons written to", os.path.normpath(OUT))
