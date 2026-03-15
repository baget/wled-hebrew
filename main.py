#!/usr/bin/env python3
"""
WLED Hebrew/Unicode Text Renderer
==================================
Renders Hebrew, English, or mixed text onto a WLED LED matrix via UDP (DNRGB).

Designed for: 32x8 matrix | TOP-LEFT origin | Horizontal | Non-serpentine

Usage:
  python3 wled_hebrew_text.py --host wled1.local --text "שלום עולם" --fg "255,100,0"
  python3 wled_hebrew_text.py --host wled1.local --text "Hello!" --fg "0,255,0"
  python3 wled_hebrew_text.py --host 192.168.1.50 --text "שלום" --fg "FF6400" --timeout 255

Dependencies:
  pip install Pillow

Install (Home Assistant):
  Place this file + fonts/ folder in /config/scripts/
"""

import argparse
import hashlib
import json
import socket
import struct
import sys
import time
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ==============================================================================
# Configuration — adjust these if your matrix is different
# ==============================================================================
MATRIX_W = 32        # columns
MATRIX_H = 8         # rows
TOTAL_LEDS = MATRIX_W * MATRIX_H  # 256

UDP_PORT = 21324     # WLED default UDP port
DNRGB_PROTOCOL = 4   # DNRGB protocol byte
DEFAULT_TIMEOUT = 5  # seconds before WLED reverts to normal mode

# Font settings
FONT_DIR = Path(__file__).parent / "fonts"
HEBREW_FONTS = {
    "noto-sans": {
        "file": "NotoSansHebrew-Bold.ttf",
        "url": "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansHebrew/NotoSansHebrew-Bold.ttf",
    },
    "noto-serif": {
        "file": "NotoSerifHebrew.ttf",
        "url": "https://github.com/google/fonts/raw/main/ofl/notoserifhebrew/NotoSerifHebrew%5Bwdth%2Cwght%5D.ttf",
    },
    "frank-ruhl": {
        "file": "FrankRuhlLibre.ttf",
        "url": "https://github.com/google/fonts/raw/main/ofl/frankruhllibre/FrankRuhlLibre%5Bwght%5D.ttf",
    },
    "rubik": {
        "file": "Rubik-Bold.ttf",
        "url": "https://github.com/google/fonts/raw/main/ofl/rubik/Rubik%5Bwght%5D.ttf",
    },
    "heebo": {
        "file": "Heebo-Bold.ttf",
        "url": "https://github.com/google/fonts/raw/main/ofl/heebo/Heebo%5Bwght%5D.ttf",
    },
    "secular-one": {
        "file": "SecularOne-Regular.ttf",
        "url": "https://github.com/google/fonts/raw/main/ofl/secularone/SecularOne-Regular.ttf",
    },
}
LATIN_FONT = {
    "file": "NotoSans-Bold.ttf",
    "url": "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSans/NotoSans-Bold.ttf",
}
RENDER_FONT_SIZE = 48  # Render large, then scale down to 8px for quality

# Cache settings
CACHE_DIR = Path(__file__).parent / ".cache"


# ==============================================================================
# Font Management
# ==============================================================================
def ensure_fonts(hebrew_font: str = "noto-sans") -> dict:
    """Download fonts if missing. Returns dict of {name: Path}."""
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    paths = {}

    fonts_to_get = {
        "hebrew": HEBREW_FONTS[hebrew_font],
        "latin": LATIN_FONT,
    }
    for name, info in fonts_to_get.items():
        path = FONT_DIR / info["file"]
        if not path.exists():
            print(f"Downloading {name} font ({info['file']})...", file=sys.stderr)
            try:
                urllib.request.urlretrieve(info["url"], str(path))
            except Exception as e:
                print(f"ERROR downloading {name} font: {e}", file=sys.stderr)
                sys.exit(1)
        paths[name] = path
    return paths


# ==============================================================================
# Render Cache — disk-based cache to skip Pillow rendering on repeat calls
# ==============================================================================
def _cache_key(text: str, fg: tuple, bg: tuple, hebrew_font: str) -> str:
    """Build a deterministic hash from render parameters."""
    raw = f"{text}|{fg}|{bg}|{hebrew_font}|{MATRIX_W}x{MATRIX_H}|{RENDER_FONT_SIZE}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _cache_path(key: str, kind: str) -> Path:
    """Return the cache file path for a given key and kind (strip or static)."""
    return CACHE_DIR / f"{kind}_{key}.bin"


def cache_get_image(key: str, kind: str) -> Image.Image | None:
    """Load a cached image from disk, or return None on miss."""
    path = _cache_path(key, kind)
    if not path.exists():
        return None
    try:
        data = path.read_bytes()
        w = int.from_bytes(data[:2], "little")
        h = int.from_bytes(data[2:4], "little")
        img = Image.frombytes("RGB", (w, h), data[4:])
        return img
    except Exception:
        path.unlink(missing_ok=True)
        return None


def cache_put_image(key: str, kind: str, img: Image.Image) -> None:
    """Store a rendered image to disk cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    w, h = img.size
    header = w.to_bytes(2, "little") + h.to_bytes(2, "little")
    _cache_path(key, kind).write_bytes(header + img.tobytes())


def is_hebrew_char(ch: str) -> bool:
    """Check if a character is in the Hebrew Unicode block."""
    return '\u0590' <= ch <= '\u05FF'


# ==============================================================================
# Text Rendering — per-character font selection for mixed scripts
# ==============================================================================
def render_text_strip(
    text: str,
    font_paths: dict,
    fg_color: tuple,
    bg_color: tuple,
    cache_key: str | None = None,
) -> Image.Image:
    """
    Render text into a full-width strip scaled to MATRIX_H pixels tall.

    Returns the thresholded strip (may be wider than MATRIX_W for scrolling).
    """
    if cache_key:
        cached = cache_get_image(cache_key, "strip")
        if cached is not None:
            print("Using cached render", file=sys.stderr)
            return cached

    he_font = ImageFont.truetype(str(font_paths["hebrew"]), RENDER_FONT_SIZE)
    en_font = ImageFont.truetype(str(font_paths["latin"]), RENDER_FONT_SIZE)

    # --- Pass 1: measure total width ---
    dummy = Image.new("RGB", (1, 1))
    ddraw = ImageDraw.Draw(dummy)

    total_w = 0
    char_info = []  # list of (char, font, width, bbox)
    for ch in text:
        font = he_font if is_hebrew_char(ch) else en_font
        bbox = ddraw.textbbox((0, 0), ch, font=font)
        w = bbox[2] - bbox[0]
        char_info.append((ch, font, w, bbox))
        total_w += w + 1  # +1 for spacing

    # --- Pass 2: render onto a wide canvas ---
    render_h = RENDER_FONT_SIZE + 8
    big_img = Image.new("RGB", (total_w + 4, render_h), bg_color)
    draw = ImageDraw.Draw(big_img)

    x_cursor = 2
    for ch, font, w, bbox in char_info:
        # Vertically center each character
        y_offset = (render_h - (bbox[3] - bbox[1])) // 2 - bbox[1]
        draw.text((x_cursor - bbox[0], y_offset), ch, font=font, fill=fg_color)
        x_cursor += w + 1

    # --- Crop to actual content ---
    content_bbox = big_img.getbbox()
    if content_bbox:
        big_img = big_img.crop(content_bbox)

    # --- Scale to matrix height ---
    src_w, src_h = big_img.size
    scale = MATRIX_H / src_h
    new_w = max(1, int(src_w * scale))
    scaled = big_img.resize((new_w, MATRIX_H), Image.LANCZOS)

    # --- Threshold: crisp pixels for LED display (no dim anti-aliased dots) ---
    pixels = scaled.load()
    for x in range(new_w):
        for y in range(MATRIX_H):
            r, g, b = pixels[x, y]
            if max(r, g, b) > 64:
                pixels[x, y] = fg_color
            else:
                pixels[x, y] = bg_color

    if cache_key:
        cache_put_image(cache_key, "strip", scaled)

    return scaled


def render_text_to_image(
    text: str,
    font_paths: dict,
    fg_color: tuple,
    bg_color: tuple,
    cache_key: str | None = None,
) -> Image.Image:
    """Render text centered on a MATRIX_W x MATRIX_H canvas (static display)."""
    strip = render_text_strip(text, font_paths, fg_color, bg_color, cache_key=cache_key)
    new_w = strip.width

    canvas = Image.new("RGB", (MATRIX_W, MATRIX_H), bg_color)
    if new_w > MATRIX_W:
        crop_x = (new_w - MATRIX_W) // 2
        cropped = strip.crop((crop_x, 0, crop_x + MATRIX_W, MATRIX_H))
        canvas.paste(cropped, (0, 0))
    else:
        x_offset = (MATRIX_W - new_w) // 2
        canvas.paste(strip, (x_offset, 0))

    return canvas


# ==============================================================================
# Pixel Mapping — Horizontal, Top-Left origin, no serpentine
# ==============================================================================
#
#  Physical wiring:
#    All rows go left-to-right, top-to-bottom
#    Row 0: LED 0..31, Row 1: LED 32..63, etc.
#
def image_to_led_buffer(img: Image.Image) -> bytearray:
    """Convert a 32x8 RGB image to a flat byte buffer ordered by LED index."""
    buf = bytearray(TOTAL_LEDS * 3)
    pixels = img.load()

    for y in range(MATRIX_H):
        for x in range(MATRIX_W):
            idx = y * MATRIX_W + x
            r, g, b = pixels[x, y]
            offset = idx * 3
            buf[offset] = r
            buf[offset + 1] = g
            buf[offset + 2] = b

    return buf


# ==============================================================================
# WLED Preparation — ensure device is ready to receive realtime data
# ==============================================================================
def prepare_wled(host: str):
    """Turn on WLED, disable effects, and set live override off via HTTP API."""
    try:
        url = f"http://{host}/json/state"
        payload = json.dumps({
            "on": True,
            "bri": 255,
            "lor": 0,
            "seg": [{"fx": 0, "bri": 255}],
        }).encode()
        req = urllib.request.Request(url, data=payload, method="POST",
                                    headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3)
        print("WLED prepared (on, effect cleared)", file=sys.stderr)
    except Exception as e:
        print(f"WARNING: Could not prepare WLED via HTTP: {e}", file=sys.stderr)


def restore_wled(host: str):
    """Tell WLED to exit realtime mode so the REST API / WebUI works again."""
    try:
        url = f"http://{host}/json/state"
        payload = json.dumps({"on": False, "lor": 2}).encode()
        req = urllib.request.Request(url, data=payload, method="POST",
                                    headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3)
        print("WLED restored (live override cleared)", file=sys.stderr)
    except Exception as e:
        print(f"WARNING: Could not restore WLED via HTTP: {e}", file=sys.stderr)


# ==============================================================================
# Scrolling
# ==============================================================================
def scroll_text(
    host: str,
    text: str,
    font_paths: dict,
    fg_color: tuple,
    bg_color: tuple,
    speed: float,
    loops: int,
    port: int,
    timeout: int,
    save_preview: str | None = None,
    cache_key: str | None = None,
):
    """
    Scroll text across the matrix.

    The full rendered strip is padded with MATRIX_W blank columns on each side
    so the text scrolls in from one edge and fully off the other.
    Text scrolls left-to-right (natural for Hebrew RTL text).
    """
    strip = render_text_strip(text, font_paths, fg_color, bg_color, cache_key=cache_key)

    # Pad strip with blank space on both sides so text scrolls fully on/off
    padded_w = strip.width + 2 * MATRIX_W
    padded = Image.new("RGB", (padded_w, MATRIX_H), bg_color)
    padded.paste(strip, (MATRIX_W, 0))

    total_frames = padded_w - MATRIX_W + 1
    frame_delay = 1.0 / speed

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        ip = socket.gethostbyname(host)
    except socket.gaierror:
        print(f"ERROR: Cannot resolve hostname '{host}'", file=sys.stderr)
        sys.exit(1)

    preview_frames = []
    loop_count = 0
    try:
        while loops == 0 or loop_count < loops:
            for frame_idx in range(total_frames):
                x_start = total_frames - 1 - frame_idx
                frame = padded.crop((x_start, 0, x_start + MATRIX_W, MATRIX_H))

                if save_preview and loop_count == 0:
                    preview_frames.append(frame.copy())

                buf = image_to_led_buffer(frame)
                header = struct.pack("BBBB", DNRGB_PROTOCOL, timeout, 0, 0)
                packet = header + buf
                sock.sendto(packet, (ip, port))
                time.sleep(frame_delay)

            loop_count += 1
    finally:
        sock.close()

    if save_preview and preview_frames:
        # Save animated GIF of one full scroll cycle
        scaled = [f.resize((MATRIX_W * 10, MATRIX_H * 10), Image.NEAREST)
                  for f in preview_frames]
        scaled[0].save(
            save_preview,
            save_all=True,
            append_images=scaled[1:],
            duration=int(frame_delay * 1000),
            loop=0,
        )
        print(f"Scroll preview saved: {save_preview}", file=sys.stderr)


# ==============================================================================
# UDP DNRGB Sender
# ==============================================================================
def send_dnrgb(host: str, led_buffer: bytearray, port: int = UDP_PORT,
               timeout: int = DEFAULT_TIMEOUT):
    """
    Send pixel data via WLED DNRGB protocol.

    Packet: [4, timeout, startHi, startLo, R, G, B, R, G, B, ...]
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        ip = socket.gethostbyname(host)
        header = struct.pack("BBBB", DNRGB_PROTOCOL, timeout, 0, 0)
        packet = header + led_buffer
        sock.sendto(packet, (ip, port))
        print(f"OK: Sent {len(packet)} bytes -> {ip}:{port}", file=sys.stderr)
    except socket.gaierror:
        print(f"ERROR: Cannot resolve hostname '{host}'", file=sys.stderr)
        sys.exit(1)
    finally:
        sock.close()


# ==============================================================================
# CLI
# ==============================================================================
def parse_color(color_str: str) -> tuple:
    """Parse 'R,G,B' or hex 'RRGGBB' into (R, G, B)."""
    s = color_str.strip().strip("\"'")
    if "," in s:
        parts = [int(x.strip()) for x in s.split(",")]
        return (parts[0], parts[1], parts[2])
    s = s.lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def main():
    parser = argparse.ArgumentParser(
        description="Render Hebrew/English text on a WLED LED matrix via UDP"
    )
    parser.add_argument("--host",
                        help="WLED hostname or IP (e.g. wled1.local)")
    parser.add_argument("--text",
                        help="Text to display")
    parser.add_argument("--fg", default="255,255,255",
                        help="Foreground color: R,G,B or RRGGBB hex (default: white)")
    parser.add_argument("--bg", default="0,0,0",
                        help="Background color: R,G,B or RRGGBB hex (default: black)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"Seconds before WLED reverts to normal "
                             f"(default: {DEFAULT_TIMEOUT}, use 255 for permanent)")
    parser.add_argument("--port", type=int, default=UDP_PORT,
                        help=f"WLED UDP port (default: {UDP_PORT})")
    parser.add_argument("--save-preview", metavar="FILE",
                        help="Save a scaled-up preview (PNG for static, GIF for scroll)")
    parser.add_argument("--scroll", action="store_true",
                        help="Enable scrolling mode (text moves right-to-left)")
    parser.add_argument("--speed", type=float, default=15.0,
                        help="Scroll speed in frames per second (default: 15)")
    parser.add_argument("--loops", type=int, default=1,
                        help="Number of scroll cycles (0 = infinite, default: 1)")
    parser.add_argument("--hebrew-font", default="noto-sans",
                        choices=list(HEBREW_FONTS.keys()),
                        help="Hebrew font to use (default: noto-sans)")
    parser.add_argument("--reverse", action="store_true",
                        help="Reverse text order (use for Hebrew to display naturally L-to-R)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Disable disk cache for rendered text")
    parser.add_argument("--clear-cache", action="store_true",
                        help="Clear the render cache and exit")

    args = parser.parse_args()

    if args.clear_cache:
        import shutil
        if CACHE_DIR.exists():
            shutil.rmtree(CACHE_DIR)
            print("Cache cleared.", file=sys.stderr)
        else:
            print("No cache to clear.", file=sys.stderr)
        return

    if not args.host or not args.text:
        parser.error("--host and --text are required (unless using --clear-cache)")

    text = args.text[::-1] if args.reverse else args.text

    fg = parse_color(args.fg)
    bg = parse_color(args.bg)

    key = None if args.no_cache else _cache_key(text, fg, bg, args.hebrew_font)

    font_paths = ensure_fonts(hebrew_font=args.hebrew_font)

    prepare_wled(args.host)

    try:
        if args.scroll:
            scroll_text(
                host=args.host,
                text=text,
                font_paths=font_paths,
                fg_color=fg,
                bg_color=bg,
                speed=args.speed,
                loops=args.loops,
                port=args.port,
                timeout=args.timeout,
                save_preview=args.save_preview,
                cache_key=key,
            )
        else:
            img = render_text_to_image(text, font_paths, fg, bg, cache_key=key)

            if args.save_preview:
                preview = img.resize((MATRIX_W * 10, MATRIX_H * 10), Image.NEAREST)
                preview.save(args.save_preview)
                print(f"Preview saved: {args.save_preview}", file=sys.stderr)

            led_buf = image_to_led_buffer(img)
            send_dnrgb(args.host, led_buf, port=args.port, timeout=args.timeout)

            if args.timeout != 255:
                time.sleep(args.timeout)
    except KeyboardInterrupt:
        print("\nInterrupted — cleaning up...", file=sys.stderr)
    finally:
        restore_wled(args.host)


if __name__ == "__main__":
    main()
