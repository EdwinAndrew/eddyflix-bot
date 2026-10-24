"""Watch-time tiers, from Corn at 10 h to Bottomless Bucket at 1,000 h, and their badges.

Badges are drawn here with Pillow: a rosette in the tier's metal (bronze, silver,
gold, platinum, diamond, then a purple legend finish) with a 3D icon in the
middle and the tier's name on a ribbon. Icons are Microsoft's Fluent Emoji
(MIT) except the kernels, which are drawn below. The font is Bebas Neue (OFL).
Both are in assets/, with their licences in assets/LICENSES.md.
"""
import hashlib
import io
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ASSETS = Path(__file__).parent / "assets"


@dataclass(frozen=True)
class Tier:
    hours: int
    key: str
    name: str
    emoji: str                          # for text: the card, the digest
    metal: tuple[str, str, str]         # light, mid, dark
    glow: str
    points: int                         # spikes on the rosette
    icon: str                           # assets/tiers/<icon>.png, or "kernel" (drawn)
    extra: str | None = None            # a smaller icon in front, bottom right

    @property
    def colour(self) -> int:
        return int(self.metal[1].lstrip("#"), 16)


TIERS = [
    Tier(10, "corn", "Corn", "🌽", ("#F6CFA8", "#C27A44", "#5E3116"), "#C27A44", 12, "corn"),
    Tier(50, "kernel", "Kernel", "🟡", ("#FFFFFF", "#BFC8D2", "#5C6672"), "#BFC8D2", 14, "kernel"),
    Tier(100, "popcorn", "Popcorn", "🍿", ("#FFF6C2", "#F2BE32", "#8A5A00"), "#F2BE32", 16, "popcorn"),
    Tier(250, "salted", "Salted", "🧂", ("#F2FFFD", "#9FE3DC", "#2F6F78"), "#7FE0D6", 18, "popcorn", "salt"),
    Tier(500, "buttery", "Buttery Popcorn", "🧈", ("#EAF9FF", "#5CC8FF", "#1A4DB3"), "#4FB8FF", 20, "popcorn", "butter"),
    Tier(1000, "bottomless", "Bottomless Bucket", "♾️", ("#FFE08A", "#D2338A", "#3A0A5E"), "#B04DFF", 24, "popcorn", "infinity"),
]
MILESTONES = [t.hours for t in TIERS]
_BY_HOURS = {t.hours: t for t in TIERS}


def tier_for(hours: int) -> Tier | None:
    """The tier awarded at exactly this milestone, e.g. 100 -> Gold."""
    return _BY_HOURS.get(hours)


def current_tier(hours: float) -> Tier | None:
    """The highest tier these hours reach, or None below the first."""
    reached = [t for t in TIERS if hours >= t.hours]
    return reached[-1] if reached else None


def next_tier(hours: float) -> Tier | None:
    return next((t for t in TIERS if hours < t.hours), None)


def progress_bar(hours: float, width: int = 10) -> str:
    """How far from the current tier to the next one, e.g. ▰▰▰▱▱▱▱▱▱▱."""
    nxt = next_tier(hours)
    if not nxt:
        return "▰" * width
    cur = current_tier(hours)
    start = cur.hours if cur else 0
    filled = int(width * (hours - start) / (nxt.hours - start))
    return "▰" * filled + "▱" * (width - filled)


# --- Badge art ----------------------------------------------------------------

def _rgb(hex_colour: str) -> np.ndarray:
    h = hex_colour.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float32)


def _metal(size: int, light: str, mid: str, dark: str, flip: bool = False) -> Image.Image:
    """A brushed-metal fill: a diagonal light-dark-light sweep with a bright band."""
    y, x = np.mgrid[0:size, 0:size].astype(np.float32) / (size - 1)
    t = (x * 0.35 + y * 0.65)
    if flip:
        t = 1 - t
    stops = [(0.0, _rgb(light)), (0.30, _rgb(mid)), (0.55, _rgb(dark)), (0.78, _rgb(mid)), (1.0, _rgb(light))]
    out = np.zeros((size, size, 3), dtype=np.float32)
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        m = (t >= a) & (t <= b)
        k = ((t - a) / (b - a))[..., None]
        out = np.where(m[..., None], ca * (1 - k) + cb * k, out)
    return Image.fromarray(out.clip(0, 255).astype(np.uint8), "RGB")


def _rosette(size: int, points: int, outer: float, inner: float) -> list[tuple[float, float]]:
    c = size / 2
    pts = []
    for i in range(points * 2):
        r = outer if i % 2 == 0 else inner
        a = math.pi * i / points - math.pi / 2
        pts.append((c + r * math.cos(a), c + r * math.sin(a)))
    return pts


def _mask(size: int, draw_fn) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    draw_fn(ImageDraw.Draw(m))
    return m


def _kernel(size: int) -> Image.Image:
    """One popcorn kernel: round on top, a pale point below, lit from the top left."""
    W = size * 4
    pts = []
    for i in range(400):
        t = 2 * math.pi * i / 400
        x = math.sin(t) * math.sin(t / 2) ** 0.45
        pts.append((W / 2 + x * W * 0.42, W / 2 + math.cos(t) * W * 0.36))
    mask = _mask(W, lambda d: d.polygon(pts, fill=255))
    yy, xx = np.mgrid[0:W, 0:W].astype(np.float32) / W
    d = np.clip(np.sqrt((xx - 0.40) ** 2 + (yy - 0.30) ** 2) / 0.55, 0, 1)[..., None]
    light, mid, dark = _rgb("#FFEC8C"), _rgb("#F7B222"), _rgb("#B05C08")
    body = np.where(d < 0.5, light + (mid - light) * (d / 0.5), mid + (dark - mid) * ((d - 0.5) / 0.5))
    tip = np.clip((yy - 0.66) / 0.18, 0, 1)[..., None] * 0.85
    body = body * (1 - tip) + _rgb("#FFF4D6") * tip
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    img.paste(Image.fromarray(body.astype(np.uint8), "RGB"), (0, 0), mask)
    shine = _mask(W, lambda d: d.ellipse([W * 0.30, W * 0.14, W * 0.47, W * 0.36], fill=200))
    shine = ImageChops.darker(shine.filter(ImageFilter.GaussianBlur(W * 0.03)), mask)
    img.paste(Image.new("RGBA", (W, W), "white"), (0, 0), shine)
    return img.resize((size, size), Image.LANCZOS)


def _kernels(size: int) -> Image.Image:
    """Three kernels in a little heap: the icon for the Kernel tier."""
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    for angle, scale, dx, dy in ((40, 0.52, 0.30, 0.10), (-36, 0.52, -0.30, 0.12), (-4, 0.66, 0.0, -0.06)):
        k = _kernel(int(size * scale)).rotate(angle, Image.BICUBIC, expand=True)
        x, y = int(size / 2 + dx * size - k.width / 2), int(size / 2 + dy * size - k.height / 2)
        shadow = Image.new("RGBA", k.size, (0, 0, 0, 0))
        shadow.paste(Image.new("RGBA", k.size, (60, 25, 0, 110)), (0, 0), k.getchannel("A"))
        out.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(size * 0.015)), (x, y + int(size * 0.02)))
        out.alpha_composite(k, (x, y))
    return out


def _icon(name: str, size: int) -> Image.Image:
    if name == "kernel":
        return _kernels(size)
    return Image.open(ASSETS / "tiers" / f"{name}.png").convert("RGBA").resize((size, size), Image.LANCZOS)


@lru_cache(maxsize=1)
def _font_path() -> str:
    return str(ASSETS / "fonts" / "BebasNeue-Regular.ttf")


def _fit_font(text: str, max_width: int, start: int) -> ImageFont.FreeTypeFont:
    size = start
    while size > 10:
        font = ImageFont.truetype(_font_path(), size)
        if font.getlength(text) <= max_width:
            return font
        size -= 4
    return ImageFont.truetype(_font_path(), size)


def _render(tier: Tier, size: int) -> Image.Image:
    S = size * 2                                      # draw at 2x, shrink for smooth edges
    c = S / 2
    light, mid, dark = tier.metal
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # Glow behind everything
    glow = _mask(S, lambda d: d.ellipse([c - S * 0.40, c - S * 0.40, c + S * 0.40, c + S * 0.40], fill=200))
    glow = glow.filter(ImageFilter.GaussianBlur(S * (0.06 if tier.hours < 250 else 0.09)))
    img.paste(Image.new("RGBA", (S, S), tier.glow), (0, 0), glow)

    # Rosette (spiky outer ring), metal
    ros = _mask(S, lambda d: d.polygon(_rosette(S, tier.points, S * 0.44, S * 0.385), fill=255))
    img.paste(_metal(S, light, mid, dark), (0, 0), ros)
    # A dark line just inside it, then a polished rim
    r_rim = S * 0.36
    rim_edge = _mask(S, lambda d: d.ellipse([c - r_rim - S * 0.012] * 2 + [c + r_rim + S * 0.012] * 2, fill=255))
    img.paste(Image.new("RGBA", (S, S), dark), (0, 0), rim_edge)
    rim = _mask(S, lambda d: d.ellipse([c - r_rim] * 2 + [c + r_rim] * 2, fill=255))
    img.paste(_metal(S, light, mid, dark, flip=True), (0, 0), rim)

    # Inner face: dark, tinted with the tier, lit from the top
    r_face = S * 0.30
    face = _mask(S, lambda d: d.ellipse([c - r_face] * 2 + [c + r_face] * 2, fill=255))
    y, x = np.mgrid[0:S, 0:S].astype(np.float32)
    dist = np.sqrt((x - c) ** 2 + (y - c * 0.75) ** 2) / r_face
    k = np.clip(dist / 1.6, 0, 1)[..., None]
    shade = _rgb(mid) * 0.38 * (1 - k) + np.array([14, 15, 22], np.float32) * k
    img.paste(Image.fromarray(shade.astype(np.uint8), "RGB"), (0, 0), face)

    # The icon (and a smaller one in front of it), each with a soft shadow
    isz = int(S * 0.34)
    ix, iy = int(c - isz / 2), int(c - isz / 2 - S * 0.065)
    layers = [(_icon(tier.icon, isz), ix, iy)]
    if tier.extra:
        esz = int(isz * 0.52)
        layers.append((_icon(tier.extra, esz), int(ix + isz - esz * 0.70), int(iy + isz - esz * 0.85)))
    for icon, x, y in layers:
        shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        shadow.paste(Image.new("RGBA", icon.size, (0, 0, 0, 160)), (x, y + int(S * 0.015)), icon.getchannel("A"))
        img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(S * 0.012)))
        img.alpha_composite(icon, (x, y))

    # Buttery Popcorn and Bottomless Bucket get sparkles on the rim
    if tier.hours >= 500:
        d = ImageDraw.Draw(img)
        for ang, rr, s in ((-50, 0.42, 0.040), (205, 0.40, 0.030), (25, 0.36, 0.022), (140, 0.43, 0.026)):
            sx = c + S * rr * math.cos(math.radians(ang))
            sy = c + S * rr * math.sin(math.radians(ang))
            r = S * s
            star = [(sx, sy - r), (sx + r * 0.22, sy - r * 0.22), (sx + r, sy), (sx + r * 0.22, sy + r * 0.22),
                    (sx, sy + r), (sx - r * 0.22, sy + r * 0.22), (sx - r, sy), (sx - r * 0.22, sy - r * 0.22)]
            d.polygon(star, fill=(255, 255, 255, 235))

    # Ribbon with the tier's name
    rw, rh = S * 0.80, S * 0.125
    ry = c + S * 0.20
    tail = S * 0.075
    d = ImageDraw.Draw(img)
    for side in (-1, 1):                              # folded tails, behind the band
        x0 = c + side * rw / 2
        d.polygon([(x0 - side * tail * 0.4, ry + rh * 0.25), (x0 + side * tail, ry + rh * 0.25),
                   (x0 + side * tail * 0.6, ry + rh * 0.75), (x0 + side * tail, ry + rh * 1.25),
                   (x0 - side * tail * 0.4, ry + rh * 1.25)], fill=dark)
    band = _mask(S, lambda m: m.rounded_rectangle([c - rw / 2, ry, c + rw / 2, ry + rh], radius=S * 0.012, fill=255))
    img.paste(_metal(S, light, mid, dark), (0, 0), band)
    d.rounded_rectangle([c - rw / 2, ry, c + rw / 2, ry + rh], radius=S * 0.012, outline=dark, width=max(2, S // 200))

    text = tier.name.upper()
    font = _fit_font(text, int(rw * 0.86), int(rh * 0.95))
    tx = c - font.getlength(text) / 2
    top = ry + (rh - (font.getbbox(text)[3] - font.getbbox(text)[1])) / 2 - font.getbbox(text)[1]
    d.text((tx, top + S * 0.004), text, font=font, fill=(0, 0, 0, 110))
    d.text((tx, top), text, font=font, fill=(255, 255, 255) if tier.hours >= 500 else (32, 22, 12))

    # The hours, on the face under the icon
    hours = f"{tier.hours:,} HOURS"
    hfont = ImageFont.truetype(_font_path(), int(S * 0.065))
    d.text((c - hfont.getlength(hours) / 2, ry - S * 0.083), hours, font=hfont, fill=(255, 255, 255, 230))

    return img.resize((size, size), Image.LANCZOS)


def _render_mini(tier: Tier, size: int) -> Image.Image:
    """The emoji version: rosette and icon only, the icon as big as it fits.
    Discord shows custom emojis at about 22 px inline, so no text."""
    S = size * 4
    c = S / 2
    light, mid, dark = tier.metal
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ros = _mask(S, lambda d: d.polygon(_rosette(S, tier.points, S * 0.50, S * 0.455), fill=255))
    img.paste(_metal(S, light, mid, dark), (0, 0), ros)
    r_face = S * 0.39
    edge = _mask(S, lambda d: d.ellipse([c - r_face - S * 0.025] * 2 + [c + r_face + S * 0.025] * 2, fill=255))
    img.paste(Image.new("RGBA", (S, S), dark), (0, 0), edge)
    face = _mask(S, lambda d: d.ellipse([c - r_face] * 2 + [c + r_face] * 2, fill=255))
    y, x = np.mgrid[0:S, 0:S].astype(np.float32)
    k = np.clip(np.sqrt((x - c) ** 2 + (y - c * 0.75) ** 2) / r_face / 1.6, 0, 1)[..., None]
    shade = _rgb(mid) * 0.38 * (1 - k) + np.array([14, 15, 22], np.float32) * k
    img.paste(Image.fromarray(shade.astype(np.uint8), "RGB"), (0, 0), face)
    isz = int(S * 0.62)
    ix = iy = int(c - isz / 2)
    img.alpha_composite(_icon(tier.icon, isz), (ix, iy))
    if tier.extra:
        esz = int(isz * 0.55)
        img.alpha_composite(_icon(tier.extra, esz), (int(ix + isz - esz * 0.75), int(iy + isz - esz * 0.80)))
    return img.resize((size, size), Image.LANCZOS)


def _locked(img: Image.Image) -> Image.Image:
    """Greyed out and dimmed: a tier not reached yet."""
    grey = img.convert("LA").convert("RGBA")
    grey = Image.eval(grey.convert("RGB"), lambda v: int(v * 0.7)).convert("RGBA")
    grey.putalpha(img.getchannel("A").point(lambda a: int(a * 0.75)))
    return grey


@lru_cache(maxsize=None)
def emoji_png(tier_key: str, locked: bool = False, size: int = 128) -> bytes:
    """The tier as a small square PNG for a Discord custom emoji (256 KB max)."""
    tier = next(t for t in TIERS if t.key == tier_key)
    img = _render_mini(tier, size)
    buf = io.BytesIO()
    (_locked(img) if locked else img).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


@lru_cache(maxsize=None)
def badge_png(tier_key: str, size: int = 512) -> bytes:
    """The tier's badge as PNG bytes. Drawn once per size."""
    tier = next(t for t in TIERS if t.key == tier_key)
    buf = io.BytesIO()
    _render(tier, size).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def badge_file(tier: Tier, size: int = 512):
    """A discord.File of the badge, named so an embed can show it."""
    import discord
    return discord.File(io.BytesIO(badge_png(tier.key, size)), filename=f"tier-{tier.key}.png")


# --- Custom emojis -------------------------------------------------------------
# The bot uploads the small badges as its own application emojis at start-up
# (cogs/achievements.py), so text can show the art inline. Until then, or if
# the upload fails, text falls back to plain emojis.

EMOJI_PREFIXES = ("tier_", "lock_")
_tags: dict[str, str] = {}              # emoji name -> "<:name:id>"


def emoji_name(tier: Tier, locked: bool = False) -> str:
    """Named after the art, so changed art gets a fresh emoji."""
    digest = hashlib.sha1(emoji_png(tier.key, locked)).hexdigest()[:6]
    return f"{EMOJI_PREFIXES[locked]}{tier.key}_{digest}"


def wanted_emojis() -> dict[str, bytes]:
    return {emoji_name(t, locked): emoji_png(t.key, locked) for t in TIERS for locked in (False, True)}


def set_emoji_tags(tags: dict[str, str]) -> None:
    _tags.clear()
    _tags.update(tags)


def icon(tier: Tier, locked: bool = False) -> str:
    """The tier's badge as an inline emoji, or its plain emoji as a fallback."""
    return _tags.get(emoji_name(tier, locked)) or ("▫️" if locked else tier.emoji)


def collection(hours: float) -> str:
    """All six badges in a row: reached ones in colour, the rest greyed out."""
    return "".join(icon(t, locked=hours < t.hours) for t in TIERS)
