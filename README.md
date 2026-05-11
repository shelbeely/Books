# Xteink X4 CrossPoint Zine Builder

Build zines and books for the **Xteink X4** running **CrossPoint firmware** from Markdown source files.

## Features

- **EPUB** as the primary stable output (`build/book.epub`)
- **Experimental native exports**: `.xtg`, `.xth`, `.xtc`, `.xtch`
- Simple XHTML/CSS — no JavaScript, no remote assets, no external fonts, no animations, no grid/flexbox
- Optimized for small e-ink screens and grayscale readability
- EPUB remains readable even with CSS disabled
- Supports local images referenced from `src/`
- Builds locally and in GitHub Actions

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Edit book.yaml with your book's metadata

# 3. Add Markdown files to src/ (e.g., 03-my-chapter.md)

# 4. Build the EPUB (primary output)
python scripts/build_epub.py

# 5. (Experimental) Build native Xteink formats
python scripts/build_native.py --format xtc --experimental
```

## Output Files

| File | Format | Status |
|------|--------|--------|
| `build/book.epub` | EPUB 3.0 | **Stable** — primary output |
| `build/book.xtc` | XTC container | Experimental |
| `build/book.xtch` | XTCH container | Experimental |
| `build/xtg/page_NNN.xtg` | XTG monochrome bitmap | Experimental |
| `build/xth/page_NNN.xth` | XTH 4-level grayscale bitmap | Experimental |

## Commands

```bash
# EPUB (primary)
python scripts/build_epub.py

# Native Xteink formats (experimental — requires --experimental flag)
python scripts/build_native.py --format xtg  --experimental
python scripts/build_native.py --format xth  --experimental
python scripts/build_native.py --format xtc  --experimental
python scripts/build_native.py --format xtch --experimental
```

## Source Structure

```
src/
  00-cover.md         # Cover / title page
  01-introduction.md  # Introduction
  02-chapter-one.md   # First chapter
  images/             # Local images — reference as ![alt](images/foo.png)
```

Files in `src/` are processed in alphabetical order. Name them with numeric prefixes to control order.

## Configuration (`book.yaml`)

```yaml
title: "My Zine"
author: "Your Name"
language: "en"
publisher: ""
description: "A short description."
display:
  width: 540    # Xteink X4 display width (pixels)
  height: 960   # Xteink X4 display height (pixels)
```

## Requirements

- Python 3.8+
- [`markdown`](https://python-markdown.github.io/) — Markdown to XHTML conversion
- [`Pillow`](https://pillow.readthedocs.io/) — image processing for native builds
- [`PyYAML`](https://pyyaml.org/) — YAML config loading

Install with:

```bash
pip install -r requirements.txt
```

## Native Format Notes

> **Experimental.** Native formats (`.xtg`, `.xth`, `.xtc`, `.xtch`) are image/page-based
> renderings. Pass `--experimental` to acknowledge this. The EPUB is the recommended
> format for daily use.

Native formats target the **CrossPoint firmware** (not stock firmware) and follow the
[CrazyCoder Xteink format specification](https://gist.github.com/CrazyCoder/b125f26d6987c0620058249f59f1327d).

### Format Summary

| Format | Extension | Description |
|--------|-----------|-------------|
| XTG | `.xtg` | 1-bit monochrome bitmap, 22-byte header |
| XTH | `.xth` | 2-bit 4-level grayscale bitmap, column-major scan |
| XTC | `.xtc` | Multi-page container wrapping XTG pages |
| XTCH | `.xtch` | XTC variant with different file magic |

## GitHub Actions

The workflow at `.github/workflows/build.yml` automatically builds the EPUB (and
experimental native formats) on every push to `main`. Download built artifacts from
the **Actions** tab of the repository.

## Design Goals

- **Simple** — pure Python, minimal dependencies, no build system
- **Offline** — no network calls at build time
- **Durable** — plain text sources, no vendor lock-in
- **Readable** — semantic HTML, EPUB readable without CSS
- **CrossPoint-friendly** — bitmap pages sized for Xteink X4 display