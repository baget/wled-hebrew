# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

WLED Hebrew/Unicode text renderer — a single-file Python CLI tool that renders Hebrew, English, or mixed-script text onto a WLED LED matrix via UDP (DNRGB protocol). Designed for a 32x8 matrix with top-left origin, horizontal non-serpentine wiring.

## Commands

```bash
# Install dependencies (uses uv)
uv sync

# Run the tool
uv run python main.py --host <wled-host> --text "שלום" --fg "255,100,0"

# Save a debug preview image
uv run python main.py --host <wled-host> --text "Hello" --save-preview preview.png
```

## Architecture

Everything lives in `main.py` — a single-file script with these stages:

1. **Font management** (`ensure_fonts`): Auto-downloads NotoSans Hebrew/Latin fonts to `fonts/` on first run
2. **Text rendering** (`render_text_to_image`): Uses Pillow to render text with per-character font selection (Hebrew vs Latin), renders large (48px) then scales to 8px height with binary thresholding for crisp LED output, centers on 32x8 canvas
3. **Pixel mapping** (`image_to_led_buffer`): Maps (x,y) coordinates to LED indices — simple linear layout, all rows left-to-right
4. **UDP sender** (`send_dnrgb`): Sends pixel data via WLED's DNRGB protocol (port 21324)

## WLED Matrix Settings

- 1st LED: Top-Left
- Orientation: Horizontal, Non-serpentine (all rows left-to-right)
- Dimensions: 32x8 (256 LEDs)
- No offsets (X: 0, Y: 0)

These match the constants `MATRIX_W`/`MATRIX_H` and the wiring logic in `image_to_led_buffer` in `main.py`.

## Key Constants

- Render font size: 48px (scaled down to 8px for quality, then thresholded to binary)
- Hebrew Unicode range: U+0590–U+05FF

## Dependencies

- Python 3.12+ (managed via uv, see `.python-version`)
- `pillow` — image rendering
