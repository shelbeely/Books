#!/usr/bin/env python3
"""EXPERIMENTAL: Build native Xteink formats from Markdown sources.

Supported formats:
  xtg   Monochrome image per page (1 bpp, row-major)
  xth   4-level grayscale image per page (2 bpp, column-major)
  xtc   Comic container with multiple XTG pages (XTC magic)
  xtch  Comic container with multiple XTG pages (XTCH magic)

Usage:
    python scripts/build_native.py --format xtc --experimental

WARNING: These formats are experimental. The EPUB (build/book.epub) is the
primary and recommended output. Native exports are image/page-based renderings
and may not reproduce all formatting from the Markdown source.

Format reference:
  https://gist.github.com/CrazyCoder/b125f26d6987c0620058249f59f1327d

Target: CrossPoint firmware (not stock firmware).
"""

import argparse
import glob
import hashlib
import os
import re
import struct
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency: pyyaml\nRun: pip install pyyaml")

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Missing dependency: Pillow\nRun: pip install Pillow")

# ---------------------------------------------------------------------------
# Xteink binary format constants
# ---------------------------------------------------------------------------

# File magic bytes (little-endian uint32, written as raw bytes).
# XTG  0x00475458  →  [0x58, 0x54, 0x47, 0x00]  "XTG\0"
# XTH  0x00485458  →  [0x58, 0x54, 0x48, 0x00]  "XTH\0"
# XTC  0x00435458  →  [0x58, 0x54, 0x43, 0x00]  "XTC\0"
# XTCH 0x48435458  →  [0x58, 0x54, 0x43, 0x48]  "XTCH"

XTG_MAGIC  = b"XTG\x00"
XTH_MAGIC  = b"XTH\x00"
XTC_MAGIC  = b"XTC\x00"
XTCH_MAGIC = b"XTCH"

# XTG/XTH header layout (22 bytes total):
#   mark(4) width(2) height(2) colorMode(1) compression(1) dataSize(4) md5(8)
XTG_HEADER_FMT = "<4sHHBBI8s"
assert struct.calcsize(XTG_HEADER_FMT) == 22

# XTC/XTCH header layout (56 bytes total):
#   mark(4) version(2) pageCount(2) readDirection(1) hasMetadata(1)
#   hasThumbnails(1) hasChapters(1) currentPage(4)
#   metadataOffset(8) indexOffset(8) dataOffset(8)
#   thumbOffset(8) chapterOffset(8)
XTC_HEADER_FMT = "<4sHHBBBBIQQQQQ"
assert struct.calcsize(XTC_HEADER_FMT) == 56

# XTC page index entry (16 bytes):
#   offset(8) size(4) width(2) height(2)
XTC_INDEX_FMT = "<QIHH"
assert struct.calcsize(XTC_INDEX_FMT) == 16

# XTC metadata block (256 bytes):
#   title(128) author(64) publisher(32) language(16) createTime(4)
#   coverPage(2) chapterCount(2) reserved(8)
XTC_META_SIZE = 256

# ---------------------------------------------------------------------------
# Font loading
# ---------------------------------------------------------------------------

_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/Library/Fonts/Times New Roman.ttf",
    "/Library/Fonts/Helvetica.ttc",
    r"C:\Windows\Fonts\times.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]

_BOLD_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/Library/Fonts/Times New Roman Bold.ttf",
    r"C:\Windows\Fonts\timesbd.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
]


def _load_font(paths, size):
    """Try each path in order; fall back to the PIL built-in font."""
    for path in paths:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    # PIL default font — size parameter supported since Pillow 10.1.
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config():
    path = os.path.join(ROOT, "book.yaml")
    if not os.path.exists(path):
        sys.exit("Config file not found: {}".format(path))
    with open(path, "r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    return config or {}


# ---------------------------------------------------------------------------
# Markdown → simple block list
# ---------------------------------------------------------------------------

def parse_blocks(md_text):
    """Parse Markdown into a flat list of (type, content) pairs.

    Recognised types: h1, h2, h3, h4, hr, img, p, blank
    """
    blocks = []
    in_fence = False
    fence_marker = ""
    for line in md_text.splitlines():
        s = line.strip()
        # Track fenced code blocks (``` or ~~~).
        if not in_fence and (s.startswith("```") or s.startswith("~~~")):
            fence_marker = s[:3]
            in_fence = True
            blocks.append(("blank", ""))
            continue
        if in_fence:
            if s.startswith(fence_marker):
                in_fence = False
            # Skip all lines inside a fence.
            continue
        if not s:
            blocks.append(("blank", ""))
            continue
        # Headings
        m = re.match(r'^(#{1,4})\s+(.*)', s)
        if m:
            level = len(m.group(1))
            blocks.append(("h{}".format(level), m.group(2).strip()))
            continue
        # Horizontal rules
        if re.match(r'^[-*_]{3,}$', s):
            blocks.append(("hr", ""))
            continue
        # Standalone images
        m = re.match(r'^!\[([^\]]*)\]\(([^)]+)\)$', s)
        if m:
            blocks.append(("img", m.group(2).strip()))
            continue
        # Normal paragraph line (strip common inline Markdown)
        clean = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', s)
        clean = re.sub(r'`([^`]+)`', r'\1', clean)
        clean = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', clean)
        blocks.append(("p", clean))
    return blocks


# ---------------------------------------------------------------------------
# Page renderer
# ---------------------------------------------------------------------------

class PageRenderer:
    """Renders a block list to one or more grayscale PIL Images."""

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.margin = max(24, width // 22)
        self._text_w = width - 2 * self.margin

        # Font sizes scale with display width.
        base = max(14, width // 32)
        self._font_body = _load_font(_FONT_PATHS, base)
        self._font_h1   = _load_font(_BOLD_FONT_PATHS, int(base * 1.9))
        self._font_h2   = _load_font(_BOLD_FONT_PATHS, int(base * 1.5))
        self._font_h3   = _load_font(_BOLD_FONT_PATHS, int(base * 1.2))
        self._font_h4   = _load_font(_BOLD_FONT_PATHS, base)

    def _line_height(self, font):
        try:
            bb = font.getbbox("Hg")
            return int((bb[3] - bb[1]) * 1.45)
        except Exception:
            return 20

    def _text_width(self, text, font):
        try:
            return int(font.getlength(text))
        except Exception:
            return len(text) * 8

    def _wrap(self, text, font):
        """Word-wrap text to fit self._text_w pixels."""
        words = text.split()
        lines = []
        current = ""
        for word in words:
            candidate = (current + " " + word).strip()
            if self._text_width(candidate, font) <= self._text_w:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines if lines else [""]

    def render(self, blocks, src_dir):
        """Return a list of grayscale PIL Images, one per page."""
        pages = []
        img = Image.new("L", (self.width, self.height), 255)
        draw = ImageDraw.Draw(img)
        y = self.margin

        def flush_page():
            nonlocal img, draw, y
            pages.append(img)
            img = Image.new("L", (self.width, self.height), 255)
            draw = ImageDraw.Draw(img)
            y = self.margin

        def draw_wrapped(text, font, extra_top=0, extra_bot=0):
            nonlocal y
            lh = self._line_height(font)
            if extra_top:
                y += extra_top
            for line in self._wrap(text, font):
                if y + lh > self.height - self.margin:
                    flush_page()
                draw.text((self.margin, y), line, font=font, fill=0)
                y += lh
            if extra_bot:
                y += extra_bot

        def draw_hr():
            nonlocal y
            gap = self._line_height(self._font_body)
            y += gap // 2
            if y + 2 > self.height - self.margin:
                flush_page()
            draw.line(
                [(self.margin, y), (self.width - self.margin, y)],
                fill=0, width=1,
            )
            y += gap // 2

        prev = None
        body_lh = self._line_height(self._font_body)

        for btype, content in blocks:
            if btype == "blank":
                if prev not in ("blank", None):
                    y += body_lh // 2
            elif btype == "h1":
                gap = self._line_height(self._font_h1)
                draw_wrapped(content, self._font_h1,
                             extra_top=(gap if prev not in ("blank", None) else 0),
                             extra_bot=gap // 4)
            elif btype == "h2":
                gap = self._line_height(self._font_h2)
                draw_wrapped(content, self._font_h2,
                             extra_top=(gap // 2 if prev not in ("blank", None) else 0),
                             extra_bot=gap // 4)
            elif btype == "h3":
                draw_wrapped(content, self._font_h3)
            elif btype == "h4":
                draw_wrapped(content, self._font_h4)
            elif btype == "hr":
                draw_hr()
            elif btype == "p":
                draw_wrapped(content, self._font_body)
            elif btype == "img":
                img_abs = os.path.normpath(os.path.join(src_dir, content))
                if not content.startswith(("http://", "https://")) and os.path.exists(img_abs):
                    try:
                        pil_img = Image.open(img_abs).convert("L")
                        max_w = self._text_w
                        max_h = self.height // 3
                        pil_img.thumbnail((max_w, max_h), Image.LANCZOS)
                        iw, ih = pil_img.size
                        if y + ih > self.height - self.margin:
                            flush_page()
                        img.paste(pil_img, (self.margin, y))
                        y += ih + body_lh // 2
                    except Exception as exc:
                        print("Warning: could not load image {}: {}".format(img_abs, exc),
                              file=sys.stderr)
                elif content.startswith(("http://", "https://")):
                    pass  # Skip remote images silently.
                else:
                    print("Warning: image not found: {}".format(img_abs),
                          file=sys.stderr)
            prev = btype

        # Always append the last page (may be partially empty but that is OK).
        pages.append(img)
        return pages


# ---------------------------------------------------------------------------
# Bitmap conversion helpers
# ---------------------------------------------------------------------------

def image_to_xtg(img):
    """Convert a grayscale PIL Image to XTG bitmap bytes.

    Returns (width, height, data_bytes) where data_bytes is the raw 1bpp
    row-major bitmap:
      - Rows stored top to bottom, pixels left to right.
      - 8 pixels per byte, MSB = leftmost pixel.
      - Bit 1 = white, Bit 0 = black.

    data_size = ((width + 7) // 8) * height
    """
    w, h = img.size
    px = img.load()
    row_bytes = (w + 7) // 8
    data = bytearray(row_bytes * h)
    for row in range(h):
        for col in range(w):
            if px[col, row] >= 128:            # white → bit 1
                byte_idx = row * row_bytes + col // 8
                data[byte_idx] |= 1 << (7 - col % 8)
    return w, h, bytes(data)


def image_to_xth(img):
    """Convert a grayscale PIL Image to XTH bitmap bytes.

    Returns (width, height, data_bytes) where data_bytes is the raw 2bpp
    two-bit-plane bitmap in column-major (vertical scan) order:
      - Columns scanned right to left (x = width-1 down to 0).
      - Within each column, 8 vertical pixels packed per byte, MSB = topmost.
      - First plane (bit1, sent via cmd 0x24) then second plane (bit2, 0x26).

    Grayscale → XTH 4-level mapping (non-linear, per spec):
      0=white (≥192), 1=dark grey (64–127), 2=light grey (128–191), 3=black (<64)
    pixelValue = (bit1 << 1) | bit2

    data_size = w * ceil(h / 8) * 2   (actual stored bytes)
    """
    w, h = img.size
    px = img.load()

    def to_level(v):
        if v >= 192:
            return 0   # white
        if v >= 128:
            return 2   # light grey
        if v >= 64:
            return 1   # dark grey
        return 3       # black

    bytes_per_col = (h + 7) // 8
    plane1 = bytearray(w * bytes_per_col)
    plane2 = bytearray(w * bytes_per_col)

    for col_idx in range(w):
        x = w - 1 - col_idx          # right-to-left
        base = col_idx * bytes_per_col
        for grp in range(bytes_per_col):
            b1 = b2 = 0
            for bit_pos in range(8):
                py = grp * 8 + bit_pos
                if py < h:
                    level = to_level(px[x, py])
                    bit1 = (level >> 1) & 1
                    bit2 = level & 1
                else:
                    bit1 = bit2 = 0   # pad with white (level 0)
                shift = 7 - bit_pos   # MSB = topmost pixel
                b1 |= bit1 << shift
                b2 |= bit2 << shift
            plane1[base + grp] = b1
            plane2[base + grp] = b2

    return w, h, bytes(plane1) + bytes(plane2)


def _md5_first8(data):
    return hashlib.md5(data).digest()[:8]


def pack_xtg_file(img):
    """Return the complete XTG file bytes (header + bitmap data)."""
    w, h, bitmap = image_to_xtg(img)
    header = struct.pack(
        XTG_HEADER_FMT,
        XTG_MAGIC, w, h, 0, 0, len(bitmap), _md5_first8(bitmap),
    )
    return header + bitmap


def pack_xth_file(img):
    """Return the complete XTH file bytes (header + bitmap data)."""
    w, h, bitmap = image_to_xth(img)
    header = struct.pack(
        XTG_HEADER_FMT,     # same layout, different magic
        XTH_MAGIC, w, h, 0, 0, len(bitmap), _md5_first8(bitmap),
    )
    return header + bitmap


def _encode_str(s, length):
    """Encode s as UTF-8, truncated and null-padded to exactly length bytes."""
    b = s.encode("utf-8")[: length - 1]
    return b + b"\x00" * (length - len(b))


def pack_xtc_file(pages, magic, config):
    """Return the complete XTC/XTCH file bytes for a list of PIL Images.

    File layout (offsets determined at build time):
      [Header:       56 bytes]
      [Metadata:    256 bytes]  (always present)
      [Page Index:  pageCount × 16 bytes]
      [Data Area:   all XTG page blobs, contiguous]
    """
    title     = (config.get("title") or "")[:127]
    author    = (config.get("author") or "")[:63]
    publisher = (config.get("publisher") or "")[:31]
    language  = (config.get("language") or "en")[:15]
    now_ts    = int(time.time())
    page_count = len(pages)

    # Render all pages to XTG blobs up front so we know sizes.
    xtg_blobs = [pack_xtg_file(img) for img in pages]

    # Compute section offsets.
    metadata_offset  = struct.calcsize(XTC_HEADER_FMT)           # 56
    index_offset     = metadata_offset + XTC_META_SIZE            # 312
    data_offset      = index_offset + page_count * struct.calcsize(XTC_INDEX_FMT)
    thumb_offset     = 0   # no thumbnails
    chapter_offset   = 0   # no chapters

    # Build page index entries (absolute offsets from file start).
    index_data = bytearray()
    current_offset = data_offset
    for blob in xtg_blobs:
        # Width and height from the XTG blob header.
        _magic, w, h = struct.unpack_from("<4sHH", blob)
        index_data += struct.pack(
            XTC_INDEX_FMT,
            current_offset, len(blob), w, h,
        )
        current_offset += len(blob)

    # Build metadata block (exactly 256 bytes).
    meta = (
        _encode_str(title, 128)
        + _encode_str(author, 64)
        + _encode_str(publisher, 32)
        + _encode_str(language, 16)
        + struct.pack("<IHH", now_ts, 0xFFFF, 0)   # createTime, coverPage=none, chapterCount=0
        + struct.pack("<Q", 0)                      # reserved
    )
    assert len(meta) == XTC_META_SIZE, "Metadata block must be 256 bytes"

    # Build header.
    header = struct.pack(
        XTC_HEADER_FMT,
        magic,
        0x0100,        # version 1.0
        page_count,
        0,             # readDirection: L→R
        1,             # hasMetadata
        0,             # hasThumbnails
        0,             # hasChapters
        1,             # currentPage (1-based)
        metadata_offset,
        index_offset,
        data_offset,
        thumb_offset,
        chapter_offset,
    )

    # Concatenate all sections.
    return header + meta + bytes(index_data) + b"".join(xtg_blobs)


# ---------------------------------------------------------------------------
# Build entry points
# ---------------------------------------------------------------------------

def build_xtg(pages, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for i, img in enumerate(pages, 1):
        path = os.path.join(out_dir, "page_{:03d}.xtg".format(i))
        data = pack_xtg_file(img)
        with open(path, "wb") as fh:
            fh.write(data)
        print("  Wrote: {} ({} bytes)".format(path, len(data)))


def build_xth(pages, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for i, img in enumerate(pages, 1):
        path = os.path.join(out_dir, "page_{:03d}.xth".format(i))
        data = pack_xth_file(img)
        with open(path, "wb") as fh:
            fh.write(data)
        print("  Wrote: {} ({} bytes)".format(path, len(data)))


def build_xtc(pages, out_path, magic, config):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    data = pack_xtc_file(pages, magic, config)
    with open(out_path, "wb") as fh:
        fh.write(data)
    print("  Wrote: {} ({} bytes, {} page(s))".format(out_path, len(data), len(pages)))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="EXPERIMENTAL: Build native Xteink formats from Markdown.",
        epilog=(
            "These formats are experimental. "
            "The EPUB (build/book.epub) is the primary output."
        ),
    )
    parser.add_argument(
        "--format",
        choices=["xtg", "xth", "xtc", "xtch"],
        required=True,
        help="Output format: xtg, xth, xtc, or xtch",
    )
    parser.add_argument(
        "--experimental",
        action="store_true",
        help="Acknowledge that native formats are experimental (required)",
    )
    args = parser.parse_args()

    if not args.experimental:
        parser.error(
            "Native Xteink formats are experimental.\n"
            "Pass --experimental to confirm you understand this and proceed."
        )

    print(
        "WARNING: Native Xteink exports are EXPERIMENTAL.\n"
        "         The EPUB (build/book.epub) is the recommended format.\n"
        "         Target: CrossPoint firmware (not stock firmware).\n"
    )

    config = load_config()
    display = config.get("display") or {}
    width  = int(display.get("width", 540))
    height = int(display.get("height", 960))

    src_dir = os.path.join(ROOT, "src")
    src_files = sorted(glob.glob(os.path.join(src_dir, "*.md")))
    if not src_files:
        sys.exit("No Markdown files found in src/")

    renderer = PageRenderer(width, height)
    all_pages = []
    for src_file in src_files:
        with open(src_file, "r", encoding="utf-8") as fh:
            text = fh.read()
        blocks = parse_blocks(text)
        file_pages = renderer.render(blocks, src_dir)
        all_pages.extend(file_pages)

    print("Rendering {} page(s) at {}×{} for format: {}".format(
        len(all_pages), width, height, args.format
    ))

    build_dir = os.path.join(ROOT, "build")
    fmt = args.format

    if fmt == "xtg":
        build_xtg(all_pages, os.path.join(build_dir, "xtg"))
    elif fmt == "xth":
        build_xth(all_pages, os.path.join(build_dir, "xth"))
    elif fmt == "xtc":
        build_xtc(all_pages, os.path.join(build_dir, "book.xtc"), XTC_MAGIC, config)
    elif fmt == "xtch":
        build_xtc(all_pages, os.path.join(build_dir, "book.xtch"), XTCH_MAGIC, config)

    print("Done.")


if __name__ == "__main__":
    main()
