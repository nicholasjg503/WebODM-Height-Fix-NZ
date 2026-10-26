"""Draws packaging/icon.ico (and icon.png for the README). Needs Pillow.

    uv run --with pillow packaging/make_icon.py
"""
import os

from PIL import Image, ImageDraw

S = 1024  # draw big, scale down for smooth edges
here = os.path.dirname(os.path.abspath(__file__))

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((32, 32, S - 32, S - 32), radius=200, fill=(18, 92, 140))

# hills
d.polygon([(110, 700), (400, 300), (560, 520), (680, 380), (914, 700)], fill=(255, 255, 255))
d.polygon([(400, 300), (330, 397), (400, 370), (470, 397)], fill=(190, 225, 240))  # snow cap

# sea level line
d.rectangle((110, 700, 914, 760), fill=(110, 205, 230))
for x in range(112, 813, 100):
    d.ellipse((x, 740, x + 100, 800), fill=(18, 92, 140))

# up arrow (heights corrected upwards)
d.polygon([(800, 160), (900, 290), (835, 290), (835, 420), (765, 420), (765, 290), (700, 290)],
          fill=(255, 196, 61))

img.resize((256, 256), Image.LANCZOS).save(os.path.join(here, "icon.png"))
img.save(os.path.join(here, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                                 (64, 64), (128, 128), (256, 256)])
print("wrote icon.ico and icon.png")
