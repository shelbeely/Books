#!/usr/bin/env python3
"""Build EPUB from Markdown sources for the Xteink X4 / CrossPoint firmware.

Output: build/book.epub

Usage:
    python scripts/build_epub.py

Sources are all *.md files in src/, processed in alphabetical order.
Configuration is read from book.yaml at the repository root.
"""

import datetime
import glob
import html
import os
import re
import sys
import uuid
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency: pyyaml\nRun: pip install pyyaml")

try:
    import markdown as md_lib
except ImportError:
    sys.exit("Missing dependency: markdown\nRun: pip install markdown")

# Media types for supported image formats.
MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    path = os.path.join(ROOT, "book.yaml")
    if not os.path.exists(path):
        sys.exit(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    return config or {}


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------

def extract_title(md_text, fallback="Untitled"):
    """Return the first H1 heading found in md_text, or fallback."""
    for line in md_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return fallback


def _strip_code_blocks(md_text):
    """Remove fenced code blocks and inline code to avoid false image matches."""
    # Remove fenced code blocks (``` or ~~~)
    text = re.sub(r'```.*?```', '', md_text, flags=re.DOTALL)
    text = re.sub(r'~~~.*?~~~', '', text, flags=re.DOTALL)
    # Remove inline code
    text = re.sub(r'`[^`]+`', '', text)
    return text


def find_local_images(md_text, src_dir):
    """Return a list of (src_attr, abs_path, epub_rel_path) for local images.

    Remote URLs (http/https/data) are ignored.
    """
    results = []
    seen = set()
    for match in re.finditer(r'!\[[^\]]*\]\(([^)]+)\)', _strip_code_blocks(md_text)):
        raw = match.group(1).strip()
        if raw.startswith(("http://", "https://", "data:")):
            continue
        abs_path = os.path.normpath(os.path.join(src_dir, raw))
        if abs_path in seen:
            continue
        seen.add(abs_path)
        epub_rel = "images/" + os.path.basename(abs_path)
        results.append((raw, abs_path, epub_rel))
    return results


def fix_img_src(body_html, image_map):
    """Replace image src values using image_map (src_attr -> epub_rel_path)."""
    def _replace(m):
        src = m.group(1)
        return 'src="{}"'.format(image_map.get(src, src))
    return re.sub(r'src="([^"]+)"', _replace, body_html)


def md_to_xhtml(md_text, title, image_map):
    """Convert Markdown text to a complete EPUB XHTML document string."""
    body_html = md_lib.markdown(
        md_text,
        extensions=["extra", "sane_lists"],
        output_format="xhtml",
    )
    body_html = fix_img_src(body_html, image_map)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en">\n'
        '<head>\n'
        '  <meta charset="utf-8"/>\n'
        '  <title>{title}</title>\n'
        '  <link rel="stylesheet" type="text/css" href="../styles/epub.css"/>\n'
        '</head>\n'
        '<body>\n'
        '{body}\n'
        '</body>\n'
        '</html>\n'
    ).format(title=html.escape(title), body=body_html)


# ---------------------------------------------------------------------------
# EPUB assembly
# ---------------------------------------------------------------------------

def build_epub(config, src_files, output_path):
    title = config.get("title") or "Untitled"
    author = config.get("author") or "Unknown"
    language = config.get("language") or "en"
    publisher = config.get("publisher") or ""
    description = config.get("description") or ""
    book_id = "urn:uuid:{}".format(uuid.uuid4())
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    src_dir = os.path.join(ROOT, "src")

    chapters = []
    all_images = {}  # src_attr -> (abs_path, epub_rel_path)

    for src_file in src_files:
        with open(src_file, "r", encoding="utf-8") as fh:
            text = fh.read()

        ch_id = re.sub(
            r"[^a-zA-Z0-9_-]", "_",
            os.path.splitext(os.path.basename(src_file))[0]
        )
        ch_title = extract_title(text, ch_id)

        # Collect images for this chapter.
        image_map = {}
        for raw, abs_path, epub_rel in find_local_images(text, src_dir):
            if os.path.exists(abs_path):
                all_images[raw] = (abs_path, epub_rel)
                image_map[raw] = epub_rel
            else:
                print(
                    "Warning: image not found: {}".format(abs_path),
                    file=sys.stderr
                )

        xhtml = md_to_xhtml(text, ch_title, image_map)
        chapters.append({
            "id": ch_id,
            "title": ch_title,
            "xhtml": xhtml,
            "filename": "{}.xhtml".format(ch_id),
        })

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:

        # mimetype MUST be first in the ZIP and stored uncompressed.
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, "application/epub+zip")

        # META-INF/container.xml
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<container version="1.0"'
            ' xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '  <rootfiles>\n'
            '    <rootfile full-path="OEBPS/content.opf"'
            ' media-type="application/oebps-package+xml"/>\n'
            '  </rootfiles>\n'
            '</container>\n',
        )

        # Stylesheet
        css_path = os.path.join(ROOT, "styles", "epub.css")
        with open(css_path, "rb") as fh:
            zf.writestr("OEBPS/styles/epub.css", fh.read())

        # Chapter XHTML documents
        for ch in chapters:
            zf.writestr(
                "OEBPS/{}".format(ch["filename"]),
                ch["xhtml"].encode("utf-8"),
            )

        # Local images
        image_manifest_lines = []
        for raw, (abs_path, epub_rel) in all_images.items():
            ext = os.path.splitext(abs_path)[1].lower()
            media_type = MEDIA_TYPES.get(ext, "application/octet-stream")
            with open(abs_path, "rb") as fh:
                zf.writestr("OEBPS/{}".format(epub_rel), fh.read())
            img_id = re.sub(r"[^a-zA-Z0-9_-]", "_", epub_rel.replace("/", "_"))
            image_manifest_lines.append(
                '    <item id="{}" href="{}" media-type="{}"/>'.format(
                    img_id, epub_rel, media_type
                )
            )

        # content.opf (package document)
        chapter_manifest = "\n".join(
            '    <item id="{id}" href="{fn}" media-type="application/xhtml+xml"/>'.format(
                id=ch["id"], fn=ch["filename"]
            )
            for ch in chapters
        )
        image_manifest = "\n".join(image_manifest_lines)
        spine = "\n".join(
            '    <itemref idref="{}"/>'.format(ch["id"])
            for ch in chapters
        )

        publisher_line = (
            '    <dc:publisher>{}</dc:publisher>\n'.format(html.escape(publisher))
            if publisher else ""
        )
        description_line = (
            '    <dc:description>{}</dc:description>\n'.format(html.escape(description))
            if description else ""
        )

        content_opf = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package version="3.0" xmlns="http://www.idpf.org/2007/opf"'
            ' unique-identifier="BookId">\n'
            '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            '    <dc:title>{title}</dc:title>\n'
            '    <dc:creator>{author}</dc:creator>\n'
            '    <dc:language>{lang}</dc:language>\n'
            '{publisher}'
            '{description}'
            '    <dc:identifier id="BookId">{book_id}</dc:identifier>\n'
            '    <meta property="dcterms:modified">{now}</meta>\n'
            '  </metadata>\n'
            '  <manifest>\n'
            '    <item id="nav" href="nav.xhtml"'
            ' media-type="application/xhtml+xml" properties="nav"/>\n'
            '    <item id="ncx" href="toc.ncx"'
            ' media-type="application/x-dtbncx+xml"/>\n'
            '    <item id="css" href="styles/epub.css" media-type="text/css"/>\n'
            '{chapter_manifest}\n'
            '{image_manifest}\n'
            '  </manifest>\n'
            '  <spine toc="ncx">\n'
            '{spine}\n'
            '  </spine>\n'
            '</package>\n'
        ).format(
            title=html.escape(title),
            author=html.escape(author),
            lang=html.escape(language),
            publisher=publisher_line,
            description=description_line,
            book_id=html.escape(book_id),
            now=now,
            chapter_manifest=chapter_manifest,
            image_manifest=image_manifest,
            spine=spine,
        )
        zf.writestr("OEBPS/content.opf", content_opf.encode("utf-8"))

        # nav.xhtml (EPUB 3 navigation document)
        nav_items = "\n".join(
            '      <li><a href="{fn}">{title}</a></li>'.format(
                fn=ch["filename"], title=html.escape(ch["title"])
            )
            for ch in chapters
        )
        nav_xhtml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml"'
            ' xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en">\n'
            '<head>\n'
            '  <meta charset="utf-8"/>\n'
            '  <title>{title}</title>\n'
            '  <link rel="stylesheet" type="text/css" href="styles/epub.css"/>\n'
            '</head>\n'
            '<body>\n'
            '  <nav epub:type="toc" id="toc">\n'
            '    <h1>Table of Contents</h1>\n'
            '    <ol>\n'
            '{items}\n'
            '    </ol>\n'
            '  </nav>\n'
            '</body>\n'
            '</html>\n'
        ).format(title=html.escape(title), items=nav_items)
        zf.writestr("OEBPS/nav.xhtml", nav_xhtml.encode("utf-8"))

        # toc.ncx (EPUB 2 compatibility)
        nav_points = ""
        for i, ch in enumerate(chapters, 1):
            nav_points += (
                '  <navPoint id="navPoint-{i}" playOrder="{i}">\n'
                '    <navLabel><text>{title}</text></navLabel>\n'
                '    <content src="{fn}"/>\n'
                '  </navPoint>\n'
            ).format(i=i, title=html.escape(ch["title"]), fn=ch["filename"])

        toc_ncx = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
            '  <head>\n'
            '    <meta name="dtb:uid" content="{book_id}"/>\n'
            '  </head>\n'
            '  <docTitle><text>{title}</text></docTitle>\n'
            '  <navMap>\n'
            '{nav_points}'
            '  </navMap>\n'
            '</ncx>\n'
        ).format(
            book_id=html.escape(book_id),
            title=html.escape(title),
            nav_points=nav_points,
        )
        zf.writestr("OEBPS/toc.ncx", toc_ncx.encode("utf-8"))

    size = os.path.getsize(output_path)
    print("Built: {} ({} bytes, {} chapter(s))".format(
        output_path, size, len(chapters)
    ))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    config = load_config()
    src_pattern = os.path.join(ROOT, "src", "*.md")
    src_files = sorted(glob.glob(src_pattern))
    if not src_files:
        sys.exit("No Markdown files found in src/")
    output_path = os.path.join(ROOT, "build", "book.epub")
    build_epub(config, src_files, output_path)


if __name__ == "__main__":
    main()
