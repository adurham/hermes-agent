#!/usr/bin/env python3
"""Generate every app icon in the repo from the nous-girl art + platform backgrounds.

Usage (from repo root):
    .venv/Scripts/python.exe scripts/generate_icons.py           # write
    .venv/Scripts/python.exe scripts/generate_icons.py --check   # verify only

Sources of truth — two axes, composed per target:
  Girl art (vector):  assets/nous-girl-black.svg  (black positive space)
                      assets/nous-girl-white.svg  (white positive space)
                      straight from the Nous brand kit (Inkscape exports,
                      5487^2 viewBox, one path each).

  Backgrounds (per platform surface, light/dark):
                      assets/backgrounds/squircle-light.svg    white rounded
                      assets/backgrounds/squircle-dark.svg     #0d1117 rounded
                      assets/backgrounds/logo-frame.svg        black frame +
                                                               white interior (website)
                      assets/backgrounds/logo-frame-dark.svg   white frame +
                                                               #0d1117 interior
                      assets/backgrounds/tile-light.svg        solid white (BrandMark)
                      assets/backgrounds/tile-dark.svg         solid #0d1117

  The master SVGs (assets/icon-master.svg light, assets/icon-master-dark.svg
  dark) are GENERATED artifacts — squircle background + girl nested into the
  824px HIG content safe zone — not hand-edited sources. The 1024 master is
  what every squircle target renders from.

If the girl SVGs are missing a red-circle placeholder master is written so a
partial checkout still generates; --check then flags the drift.

--check mode regenerates every target in memory and byte-compares it
against the committed file, exiting nonzero on any drift. This is what
CI runs (see .github/workflows/icons-freshness-check.yml) so the repo
can never hold a hand-edited or stale generated icon.

Rendering: resvg (resvg-py) for SVG -> PNG fidelity at every size.
Containers: Pillow for multi-size .ico and .icns.

Deps:
    Pillow (core dependency), resvg-py (dev extra):
    uv sync --extra dev

Outputs (35 files):
  assets/icon-master.svg                              generated light master
  assets/icon-master-dark.svg                         generated dark master
  apps/desktop/assets/icon.png                        1024x1024 squircle (light)
  apps/desktop/assets/icon.ico                        16,24,32,48,64,128,256
  apps/desktop/assets/icon.icns                       16..1024 (real ICNS)
  apps/desktop/assets/icon-dark.png                   1024x1024 squircle (dark)
  apps/desktop/assets/icon-dark.ico                   16,24,32,48,64,128,256
  apps/desktop/assets/icon-dark.icns                  16..1024 (real ICNS)
  apps/desktop/assets/appx/Wide310x150Logo.png        310x150, squircle 100 centered
  apps/desktop/assets/appx/StoreLogo.png              50x50 squircle
  apps/desktop/assets/appx/Square44x44Logo.png        44x44 squircle
  apps/desktop/assets/appx/Square150x150Logo.png      150x150 squircle
  apps/desktop/assets/appx/*-dark.png                 dark-appearance logos (ready
                                                      to wire into the MSIX
                                                      manifest when app-builder-lib
                                                      supports contrast images)
  apps/desktop/public/apple-touch-icon.png            1024x1024 squircle
  apps/desktop/public/nous-girl.png                   256x256 PNG, black girl on white (light)
  apps/desktop/public/nous-girl-dark.png              256x256 PNG, white girl on #0d1117 (dark)
  apps/bootstrap-installer/src-tauri/icons/32x32.png       32x32
  apps/bootstrap-installer/src-tauri/icons/128x128.png     128x128
  apps/bootstrap-installer/src-tauri/icons/128x128@2x.png  256x256
  apps/bootstrap-installer/src-tauri/icons/icon.ico        16,32,64,128,256
  apps/bootstrap-installer/src-tauri/icons/icon.icns       16..1024
  apps/bootstrap-installer/public/nous-girl.png   256x256 PNG, black girl on white (light)
  website/static/img/logo.png                     1772x1799 black-frame wordmark
  website/static/img/logo-dark.png                1772x1799 white-frame wordmark
  website/static/img/nous-logo.png                150x150 on white (opaque)
  website/static/img/nous-logo-dark.png           150x150 on #0d1117 (opaque)
  website/static/img/favicon-16x16.png            16x16
  website/static/img/favicon-32x32.png            32x32
  website/static/img/apple-touch-icon.png         180x180
  website/static/img/favicon.ico                  16,32,48
  website/static/img/favicon.svg                  copy of the light master
  web/public/favicon.ico                          16,32,48
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

from PIL import Image

try:
    import resvg_py
except ImportError:
    sys.exit(
        "resvg-py is missing. Install it with:\n"
        "  uv sync --extra dev"
    )

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
BACKGROUNDS = ASSETS / "backgrounds"
GIRL_BLACK = ASSETS / "nous-girl-black.svg"
GIRL_WHITE = ASSETS / "nous-girl-white.svg"
GIRLS = {"black": GIRL_BLACK, "white": GIRL_WHITE}
MASTER = ASSETS / "icon-master.svg"
MASTER_DARK = ASSETS / "icon-master-dark.svg"

# The squircle corner radius measured on the real 1024 icon (~245px).
SQUIRCLE_RADIUS = 245
# The nous dark background (#0d1117) — fixed dark tile/background everywhere.
DARK_HEX = "#0d1117"
DARK_RGB = (13, 17, 23)

# Girl placement per background: (x, y, w, h) in that background's coordinate
# space. Squircle backgrounds put the girl in the 824px HIG content safe zone
# (centered, 100px pad on a 1024 canvas); logo frames fill the white/dark
# interior at ~1.7x; BrandMark tiles take the girl height-fitted (meet).
GIRL_BOXES = {
    "squircle-light.svg": (100, 100, 824, 824),
    "squircle-dark.svg": (100, 100, 824, 824),
    "logo-frame.svg": (14, 17, 1743, 1766),
    "logo-frame-dark.svg": (14, 17, 1743, 1766),
    "tile-light.svg": (0, 0, 256, 256),
    "tile-dark.svg": (0, 0, 256, 256),
}
# The brand-kit SVG canvas (both girl svgs share this viewBox).
GIRL_VIEWBOX = 5487.0615

PLACEHOLDER = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
  <rect x="0" y="0" width="1024" height="1024" rx="{SQUIRCLE_RADIUS}" fill="#ffffff"/>
  <circle cx="512" cy="512" r="420" fill="#e34c4c"/>
</svg>
'''

# (relpath, kind, arg)
TARGETS: list[tuple[str, str, object]] = [
    ("assets/icon-master.svg", "svg", None),
    ("assets/icon-master-dark.svg", "svg_dark", None),
    ("apps/desktop/assets/icon.png", "png", 1024),
    ("apps/desktop/assets/icon.ico", "ico", [16, 24, 32, 48, 64, 128, 256]),
    ("apps/desktop/assets/icon.icns", "icns", None),
    ("apps/desktop/assets/icon-dark.png", "png_dark", 1024),
    ("apps/desktop/assets/icon-dark.ico", "ico_dark", [16, 24, 32, 48, 64, 128, 256]),
    ("apps/desktop/assets/icon-dark.icns", "icns_dark", None),
    ("apps/desktop/assets/appx/Wide310x150Logo.png", "wide", (310, 150)),
    ("apps/desktop/assets/appx/StoreLogo.png", "png", 50),
    ("apps/desktop/assets/appx/Square44x44Logo.png", "png", 44),
    ("apps/desktop/assets/appx/Square150x150Logo.png", "png", 150),
    ("apps/desktop/assets/appx/Wide310x150Logo-dark.png", "wide_dark", (310, 150)),
    ("apps/desktop/assets/appx/StoreLogo-dark.png", "png_dark", 50),
    ("apps/desktop/assets/appx/Square44x44Logo-dark.png", "png_dark", 44),
    ("apps/desktop/assets/appx/Square150x150Logo-dark.png", "png_dark", 150),
    ("apps/desktop/public/apple-touch-icon.png", "png", 1024),
    ("apps/desktop/public/nous-girl.png", "girl_light", 256),
    ("apps/desktop/public/nous-girl-dark.png", "girl_dark", 256),
    ("apps/bootstrap-installer/src-tauri/icons/32x32.png", "png", 32),
    ("apps/bootstrap-installer/src-tauri/icons/128x128.png", "png", 128),
    ("apps/bootstrap-installer/src-tauri/icons/128x128@2x.png", "png", 256),
    ("apps/bootstrap-installer/src-tauri/icons/icon.ico", "ico", [16, 32, 64, 128, 256]),
    ("apps/bootstrap-installer/src-tauri/icons/icon.icns", "icns", None),
    ("apps/bootstrap-installer/public/nous-girl.png", "girl_light", 256),
    ("website/static/img/logo.png", "logo", None),
    ("website/static/img/logo-dark.png", "logo_dark", None),
    ("website/static/img/nous-logo.png", "png_white", 150),
    ("website/static/img/nous-logo-dark.png", "png_dark_white", 150),
    ("website/static/img/favicon-16x16.png", "png", 16),
    ("website/static/img/favicon-32x32.png", "png", 32),
    ("website/static/img/apple-touch-icon.png", "png", 180),
    ("website/static/img/favicon.ico", "ico", [16, 32, 48]),
    ("website/static/img/favicon.svg", "svg_copy", None),
    ("web/public/favicon.ico", "ico", [16, 32, 48]),
]

# ─── girl art extraction ────────────────────────────────────────────────────

_path_cache: dict[str, str] = {}
_bbox_cache: dict[str, tuple[float, float, float, float]] = {}


def girl_path(girl: str) -> str:
    """The girl `<path>` element with editor metadata stripped (resvg rejects
    undeclared inkscape/sodipodi prefixes)."""
    if girl not in _path_cache:
        src = GIRLS[girl].read_text(encoding="utf-8")
        m = re.search(r"<path\b.*?/>", src, re.S)
        assert m, f"no <path> found in {GIRLS[girl].name}"
        path = re.sub(r'\s+(inkscape|sodipodi):[a-zA-Z-]+="[^"]*"', "", m.group(0))
        _path_cache[girl] = path
    return _path_cache[girl]


def girl_bbox(girl: str) -> tuple[float, float, float, float]:
    """Art bounding box in the girl SVG's coordinate space, measured by
    rendering once and taking the alpha bbox (robust to art changes)."""
    if girl not in _bbox_cache:
        data = resvg_py.svg_to_bytes(svg_path=str(GIRLS[girl]), width=512, height=512)
        im = Image.open(io.BytesIO(data))
        bx, by, bx2, by2 = im.getchannel("A").point(lambda v: 255 if v > 0 else 0).getbbox()
        s = GIRL_VIEWBOX / 512.0
        _bbox_cache[girl] = (bx * s, by * s, (bx2 - bx) * s, (by2 - by) * s)
    return _bbox_cache[girl]


def girl_layer(girl: str, box: tuple[float, float, float, float]) -> str:
    """Nested-svg layer: girl art (bbox as viewBox) placed into `box` — the
    box's aspect is preserved via 'meet', so the girl never distorts."""
    bx, by, bw, bh = girl_bbox(girl)
    x, y, w, h = box
    return (
        f'<svg x="{x}" y="{y}" width="{w}" height="{h}" viewBox="{bx} {by} {bw} {bh}">\n'
        f"    {girl_path(girl)}\n"
        "  </svg>"
    )


def background_inner(name: str) -> tuple[str, int, int]:
    """Inner content + (width, height) of a background SVG asset."""
    text = (BACKGROUNDS / name).read_text(encoding="utf-8")
    m = re.search(r"<svg\b[^>]*viewBox=\"0 0 (\d+(?:\.\d+)?) (\d+(?:\.\d+)?)\"[^>]*>", text)
    assert m, f"cannot parse viewBox of {name}"
    w, h = float(m.group(1)), float(m.group(2))
    inner = re.sub(r"^.*?>\s*", "", text, count=1, flags=re.S)
    inner = re.sub(r"\s*</svg>\s*$", "", inner, flags=re.S)
    return inner, int(w), int(h)


def compose_master(girl: str, bg: str) -> str:
    """Composed master SVG: background + girl layer."""
    inner, w, h = background_inner(bg)
    x, y, bx, bh = GIRL_BOXES[bg]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}">\n'
        f"  {inner.strip()}\n"
        f"  {girl_layer(girl, (x, y, bx, bh))}\n"
        "</svg>\n"
    )


def ensure_masters(check: bool = False) -> None:
    """Write the generated light/dark masters; placeholder fallback if the
    girl art is missing."""
    missing = [p for p in (GIRL_BLACK, GIRL_WHITE) if not p.exists()]
    if missing:
        if check:
            sys.exit(f"[check] girl art missing: {[p.name for p in missing]}")
        for path, content in ((MASTER, PLACEHOLDER), (MASTER_DARK, PLACEHOLDER)):
            path.write_text(content, encoding="utf-8")
        print(f"[master] wrote placeholder masters ({[p.name for p in missing]})")
        return
    MASTER.write_text(compose_master("black", "squircle-light.svg"), encoding="utf-8")
    MASTER_DARK.write_text(compose_master("white", "squircle-dark.svg"), encoding="utf-8")


# ─── rendering ──────────────────────────────────────────────────────────────

def render(master: Path, size: int, *, background: str | None = None) -> Image.Image:
    """Render a master to an RGBA PNG of `size`x`size`."""
    data = resvg_py.svg_to_bytes(
        svg_path=str(master), width=size, height=size, background=background
    )
    return Image.open(io.BytesIO(data)).convert("RGBA")


def paste_centered(canvas: Image.Image, img: Image.Image) -> None:
    x = (canvas.width - img.width) // 2
    y = (canvas.height - img.height) // 2
    canvas.alpha_composite(img, (x, y))


def girl_mark(kind: str, size: int) -> Image.Image:
    """Composite the girl SVG onto a fixed tile (BrandMark asset), rendered
    straight from the vector art (crisp at any size).

    girl_light: black girl on white tile.  girl_dark: white girl on #0d1117.
    """
    if kind == "girl_light":
        girl, bg = "black", "tile-light.svg"
    else:
        girl, bg = "white", "tile-dark.svg"
    if not GIRLS[girl].exists():
        print(f"  [girl] {GIRLS[girl].name} missing — placeholder fallback")
        return render(MASTER if kind == "girl_light" else MASTER_DARK, size)

    inner, w, h = background_inner(bg)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}">\n'
        f"  {inner.strip()}\n"
        f"  {girl_layer(girl, GIRL_BOXES[bg])}\n"
        "</svg>\n"
    )
    data = resvg_py.svg_to_bytes(svg_string=svg, width=size, height=size)
    return Image.open(io.BytesIO(data)).convert("RGBA")


def build_logo_image(master: Path, dark: bool = False) -> Image.Image:
    """1772x1799 wordmark: frame (light: black / dark: white, ~14-17px) +
    interior (light: white / dark: #0d1117) + art at ~1.7x.

    Measured from the real logo.png: border L/R 14, T 17, B 16; interior
    spans (15,18)..(1758,1784) = 1743x1766.
    """
    from PIL import ImageDraw

    W, H = 1772, 1799
    art_w, art_h = 1743, 1766
    border = 14
    top, bottom = 17, 16
    if dark:
        frame, interior = DARK_RGB, (255, 255, 255)
    else:
        frame, interior = (0, 0, 0), (255, 255, 255)

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((0, 0, W - 1, H - 1), radius=2, fill=frame)
    draw.rectangle(
        (border, top, W - 1 - border, H - 1 - bottom),
        fill=interior,
    )

    # Art: render the master square at ~1.7x, tiny vertical stretch to fill.
    art = render(master, art_w).resize((art_w, art_h), Image.LANCZOS)
    canvas.alpha_composite(art, (border, top))
    return canvas


def target_bytes(kind: str, arg: object) -> bytes:
    """Produce the exact bytes for one target. Shared by write + check."""
    if kind == "svg":
        return MASTER.read_bytes()
    if kind == "svg_dark":
        return MASTER_DARK.read_bytes()
    if kind == "svg_copy":
        return MASTER.read_bytes()

    buf = io.BytesIO()
    if kind == "png":
        render(MASTER, arg).save(buf, "PNG")
    elif kind == "png_dark":
        render(MASTER_DARK, arg).save(buf, "PNG")
    elif kind == "png_white":
        render(MASTER, arg, background="#ffffff").save(buf, "PNG")
    elif kind == "png_dark_white":
        render(MASTER_DARK, arg, background=DARK_HEX).save(buf, "PNG")
    elif kind == "girl_light" or kind == "girl_dark":
        # Flat 2-tone vector-style art: <256 distinct colors, so an 8-bit
        # palette PNG holds it pixel-identically. Saved with compress_level=0
        # (stored deflate blocks): the icons-freshness lane byte-compares
        # against an ubuntu regen, and compressed PNG bytes drift per host
        # zlib — stored blocks are spec-locked, identical on every platform.
        # ~66KB vs ~15KB compressed is a non-issue for a bundled asset.
        girl_mark(kind, arg).convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=256).save(
            buf, "PNG", compress_level=0
        )
    elif kind == "ico":
        img = render(MASTER, max(arg))
        img.save(buf, format="ICO", sizes=[(s, s) for s in arg])
    elif kind == "ico_dark":
        img = render(MASTER_DARK, max(arg))
        img.save(buf, format="ICO", sizes=[(s, s) for s in arg])
    elif kind == "icns":
        img = render(MASTER, 1024)
        frames = [img.resize((s, s), Image.LANCZOS) for s in (16, 32, 64, 128, 256, 512, 1024)]
        img.save(buf, format="ICNS", append_images=frames[1:])
    elif kind == "icns_dark":
        img = render(MASTER_DARK, 1024)
        frames = [img.resize((s, s), Image.LANCZOS) for s in (16, 32, 64, 128, 256, 512, 1024)]
        img.save(buf, format="ICNS", append_images=frames[1:])
    elif kind == "wide":
        w, h = arg
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        paste_centered(canvas, render(MASTER, 100))
        canvas.save(buf, "PNG")
    elif kind == "wide_dark":
        w, h = arg
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        paste_centered(canvas, render(MASTER_DARK, 100))
        canvas.save(buf, "PNG")
    elif kind == "logo":
        build_logo_image(MASTER, dark=False).save(buf, "PNG")
    elif kind == "logo_dark":
        build_logo_image(MASTER_DARK, dark=True).save(buf, "PNG")
    else:
        raise ValueError(f"unknown kind {kind!r}")
    return buf.getvalue()


def cmd_write() -> None:
    ensure_masters()
    written = 0
    for rel, kind, arg in TARGETS:
        path = ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_bytes(target_bytes(kind, arg))
            written += 1
        except Exception as exc:  # noqa: BLE001 - report and keep going
            print(f"  !! {rel}: FAILED ({exc})")
    print(f"[ok] wrote {written}/{len(TARGETS)} files")

    print("\n[verify]")
    for rel, kind, arg in TARGETS:
        path = ROOT / rel
        try:
            if kind in ("svg", "svg_dark", "svg_copy"):
                print(f"  {rel}: {path.stat().st_size} bytes SVG")
                continue
            im = Image.open(path)
            if kind in ("ico", "ico_dark"):
                sizes = []
                try:
                    for i in range(im.n_frames):
                        im.seek(i)
                        sizes.append(im.size)
                except Exception:
                    sizes = [im.size]
                print(f"  {rel}: ICO {sorted(set(sizes))}")
            elif kind in ("icns", "icns_dark"):
                print(f"  {rel}: ICNS {im.size} (container)")
            else:
                print(f"  {rel}: {im.format} {im.size}")
        except Exception as exc:
            print(f"  {rel}: VERIFY FAILED ({exc})")


def cmd_check() -> int:
    ensure_masters(check=True)
    drifted: list[str] = []
    for rel, kind, arg in TARGETS:
        path = ROOT / rel
        if not path.exists():
            drifted.append(f"{rel}: MISSING (expected generated file)")
            continue
        try:
            expected = target_bytes(kind, arg)
        except Exception as exc:  # noqa: BLE001
            drifted.append(f"{rel}: REGENERATE FAILED ({exc})")
            continue
        actual = path.read_bytes()
        if actual != expected:
            drifted.append(f"{rel}: drift ({len(actual)} bytes on disk vs {len(expected)} generated)")

    if not drifted:
        print(f"[ok] all {len(TARGETS)} generated icons match the girl art + backgrounds")
        return 0

    print(f"[check] {len(drifted)} file(s) out of sync:")
    for line in drifted:
        print(f"  - {line}")
    print(
        "\nFix: run `.venv/Scripts/python.exe scripts/generate_icons.py` "
        "and commit the regenerated files."
    )
    return 1


def main() -> None:
    if "--check" in sys.argv:
        sys.exit(cmd_check())
    cmd_write()


if __name__ == "__main__":
    main()
