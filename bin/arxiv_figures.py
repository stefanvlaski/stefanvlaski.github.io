#!/usr/bin/env python3
"""Extract candidate preview figures for publications from their arXiv versions.

Run from the repository root:

    python bin/arxiv_figures.py --out _figure_candidates [KEY ...]

For every papers.bib entry with an `arxiv` field and no `preview` (or only the given keys), downloads
the arXiv source, renders each figure environment (all its \\includegraphics files side by side) to a
PNG, and writes per paper:

    <out>/<key>/NN.png       one candidate per figure
    <out>/<key>/index.json   figure number, caption, how often it is referenced, colourfulness
    <out>/<key>/sheet.jpg    all candidates on one numbered sheet, for choosing by eye

Figures drawn in LaTeX (TikZ/pgfplots) have no image file; they are cropped from the arXiv PDF
instead, using the region above each caption.

With --auto (used by the weekly sync), one figure per paper is chosen and installed as
assets/img/publication_preview/<key>.png, and preview={<key>.png} is added to papers.bib:

* if ANTHROPIC_API_KEY is set, Claude looks at the candidate sheet and picks the figure that best
  balances importance with being colourful and legible as a small thumbnail (and says whether it
  needs rotating);
* otherwise a scoring rule picks it (how often the paper references the figure, how much of the
  image is filled, a thumbnail-friendly aspect ratio, earlier figures preferred). The rule was
  tuned on hand-picked previews and agrees with them on about 60% of papers.

Either way the choice is listed in the sync pull request; to change it, replace the PNG.
"""

import argparse
import base64
import gzip
import io
import json
import math
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import pymupdf as fitz
import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from arxiv_links import add_field, bib_entries_with_spans  # noqa: E402
from scholar_sync import BIB_PATH  # noqa: E402

PREVIEW_DIR = BIB_PATH.parent.parent / "assets" / "img" / "publication_preview"
CLAUDE_MODEL = "claude-opus-5"
UA = {"User-Agent": "stefanvlaski.github.io publication previews (arxiv_figures.py)"}
EXTS = ["", ".pdf", ".png", ".jpg", ".jpeg", ".eps", ".PDF", ".PNG", ".JPG"]
MAX_W = 1000  # candidate width in pixels
Image.MAX_IMAGE_PIXELS = None


# ---------------------------------------------------------------------------------------------
# LaTeX source
# ---------------------------------------------------------------------------------------------


def strip_comments(tex):
    return re.sub(r"(?<!\\)%.*", "", tex)


def braced(text, i):
    """Content of the {...} group starting at text[i] == '{'."""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1 : j]
    return text[i + 1 :]


def fetch_source(arxiv_id, dest):
    """Download and unpack the arXiv source into dest. Returns True if LaTeX source was found."""
    r = requests.get(f"https://arxiv.org/e-print/{arxiv_id}", headers=UA, timeout=120)
    r.raise_for_status()
    data = r.content
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if data[:4] == b"%PDF":
        return False
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            members = [m for m in tar.getmembers() if m.isfile() and ".." not in m.name and not m.name.startswith("/")]
            tar.extractall(dest, members=members)
    except tarfile.ReadError:
        (dest / "main.tex").write_bytes(data)
    return any(dest.rglob("*.tex"))


def main_tex(src):
    texs = sorted(src.rglob("*.tex"))
    for t in texs:
        s = t.read_text(errors="ignore")
        if "\\documentclass" in s and "\\begin{document}" in s:
            return t
    return texs[0] if texs else None


def expand_inputs(path, root, depth=0):
    text = strip_comments(path.read_text(errors="ignore"))
    if depth > 5:
        return text

    def sub(m):
        name = m.group(2).strip()
        for cand in (root / name, root / (name + ".tex"), path.parent / name, path.parent / (name + ".tex")):
            if cand.is_file():
                return expand_inputs(cand, root, depth + 1)
        return ""

    return re.sub(r"\\(input|include)\s*\{([^}]*)\}", sub, text)


def figures_from_tex(tex):
    """[(number, caption, [files], labels)] in document order."""
    figs = []
    body = tex.split("\\begin{document}", 1)[-1]
    for n, m in enumerate(re.finditer(r"\\begin\{figure\*?\}(.*?)\\end\{figure\*?\}", body, re.S), start=1):
        env = m.group(1)
        files = [g.group(1).strip() for g in re.finditer(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", env)]
        caps = [c.end() for c in re.finditer(r"\\caption\s*(?:\[[^\]]*\])?\s*(?=\{)", env)]
        caption = braced(env, caps[-1]) if caps else ""
        caption = " ".join(re.sub(r"\\[a-zA-Z]+\*?|[{}$]", " ", caption).split())
        labels = re.findall(r"\\label\{([^}]*)\}", env)
        figs.append((n, caption, files, labels))
    return figs


def resolve(name, dirs):
    for d in dirs:
        for ext in EXTS:
            p = d / (name + ext)
            if p.is_file():
                return p
    return None


# ---------------------------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------------------------


def trim(im, pad=8):
    rgb = np.asarray(im.convert("RGB"), dtype=np.int16)
    mask = (rgb < 245).any(axis=2)
    if not mask.any():
        return None
    ys, xs = np.where(mask)
    box = (max(xs.min() - pad, 0), max(ys.min() - pad, 0), min(xs.max() + pad + 1, im.width), min(ys.max() + pad + 1, im.height))
    return im.crop(box)


def on_white(im):
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, "white")
        bg.alpha_composite(im)
        return bg.convert("RGB")
    return im.convert("RGB")


def render_pdf_page(path, width=1600, clip=None):
    doc = fitz.open(path)
    page = doc[0] if clip is None else doc[clip[0]]
    rect = page.rect if clip is None else clip[1]
    zoom = min(width / max(rect.width, 1), 8)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def render_file(path, tmp):
    suffix = path.suffix.lower()
    if suffix == ".eps":
        out = tmp / (path.stem + "_eps.pdf")
        subprocess.run(["ps2pdf", "-dEPSCrop", str(path), str(out)], check=True, capture_output=True, timeout=120)
        path, suffix = out, ".pdf"
    if suffix == ".pdf":
        im = render_pdf_page(path)
    else:
        im = Image.open(path)
        im.load()
    im = on_white(im)
    return trim(im)


def compose(images, gap=24):
    """Lay subfigures out in one row (or two rows for more than three)."""
    rows = [images] if len(images) <= 3 else [images[: (len(images) + 1) // 2], images[(len(images) + 1) // 2 :]]
    h = 500
    rendered = []
    for row in rows:
        scaled = [im.resize((max(1, int(im.width * h / im.height)), h), Image.LANCZOS) for im in row]
        w = sum(s.width for s in scaled) + gap * (len(scaled) - 1)
        canvas = Image.new("RGB", (w, h), "white")
        x = 0
        for s in scaled:
            canvas.paste(s, (x, 0))
            x += s.width + gap
        rendered.append(canvas)
    w = max(r.width for r in rendered)
    canvas = Image.new("RGB", (w, sum(r.height for r in rendered) + gap * (len(rendered) - 1)), "white")
    y = 0
    for r in rendered:
        canvas.paste(r, ((w - r.width) // 2, y))
        y += r.height + gap
    return canvas


def finalize(im):
    if im.width > MAX_W:
        im = im.resize((MAX_W, max(1, int(im.height * MAX_W / im.width))), Image.LANCZOS)
    return im


def colourfulness(im):
    """Hasler & Suesstrunk colourfulness of the non-white part of the image."""
    a = np.asarray(im.convert("RGB").resize((200, max(1, int(200 * im.height / im.width)))), dtype=float)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    rg, yb = r - g, 0.5 * (r + g) - b
    return round(float(np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean())), 1)


def ink(im):
    """Fraction of the image that is not (near-)white: filled diagrams score high, thin line plots low."""
    a = np.asarray(im.convert("RGB").resize((200, max(1, int(200 * im.height / im.width)))))
    return round(float((a < 235).any(axis=2).mean()), 3)


# ---------------------------------------------------------------------------------------------
# PDF fallback: crop the region above each caption
# ---------------------------------------------------------------------------------------------


def figures_from_pdf(arxiv_id, tmp):
    r = requests.get(f"https://arxiv.org/pdf/{arxiv_id}", headers=UA, timeout=120)
    r.raise_for_status()
    pdf = tmp / "paper.pdf"
    pdf.write_bytes(r.content)
    doc = fitz.open(pdf)
    out, seen = [], set()
    for pno, page in enumerate(doc):
        W, H = page.rect.width, page.rect.height
        blocks = [b for b in page.get_text("blocks") if b[6] == 0]
        for b in blocks:
            m = re.match(r"\s*(Fig\.|Figure)\s*(\d+)", b[4])
            if not m or int(m.group(2)) in seen:
                continue
            x0, y0, x1, y1 = b[:4]
            if x0 < 0.45 * W and x1 > 0.55 * W:
                cx0, cx1 = 0.05 * W, 0.95 * W
            elif (x0 + x1) / 2 < W / 2:
                cx0, cx1 = 0.05 * W, 0.5 * W
            else:
                cx0, cx1 = 0.5 * W, 0.95 * W
            top = 0.05 * H
            for o in blocks:
                if o is b or o[3] > y0 or o[2] < cx0 or o[0] > cx1:
                    continue
                if len(o[4]) > 80:  # body text above the figure
                    top = max(top, o[3])
            if y0 - top < 40:
                continue
            seen.add(int(m.group(2)))
            im = trim(render_pdf_page(pdf, width=2000, clip=(pno, fitz.Rect(cx0, top + 2, cx1, y0 - 2))))
            if im is not None:
                out.append((int(m.group(2)), " ".join(b[4].split())[:300], im))
    return out


# ---------------------------------------------------------------------------------------------
# Contact sheet
# ---------------------------------------------------------------------------------------------


def sheet(cands, title, path):
    tw, th, cols = 420, 280, 3
    rows = (len(cands) + cols - 1) // cols
    canvas = Image.new("RGB", (tw * cols, 40 + rows * (th + 58)), "white")
    d = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 15)
        big = ImageFont.truetype("DejaVuSans-Bold.ttf", 18)
    except OSError:
        font = big = ImageFont.load_default()
    d.text((8, 10), title[:110], fill="black", font=big)
    for i, c in enumerate(cands):
        im = c["image"].copy()
        im.thumbnail((tw - 16, th - 8))
        x, y = (i % cols) * tw, 40 + (i // cols) * (th + 58)
        canvas.paste(im, (x + (tw - im.width) // 2, y + (th - im.height) // 2))
        d.rectangle([x + 2, y, x + tw - 3, y + th + 54], outline=(200, 200, 200))
        d.text((x + 8, y + th), f"#{c['id']}  Fig. {c['figure']}  refs={c['refs']}  colour={c['colour']}", fill=(180, 0, 0), font=font)
        d.text((x + 8, y + th + 20), c["caption"][:52], fill=(60, 60, 60), font=font)
    canvas.save(path, quality=80)


# ---------------------------------------------------------------------------------------------


def process(key, arxiv_id, title, out_dir):
    out = out_dir / key
    out.mkdir(parents=True, exist_ok=True)
    cands, source, missing = [], "latex", {}
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        src = tmp / "src"
        src.mkdir()
        has_tex = fetch_source(arxiv_id, src)
        if has_tex and (main := main_tex(src)):
            tex = expand_inputs(main, main.parent)
            dirs = [main.parent] + [p for p in src.rglob("*") if p.is_dir()]  # covers \graphicspath too
            for number, caption, files, labels in figures_from_tex(tex):
                images = []
                for f in files[:8]:
                    p = resolve(f, dirs)
                    if p:
                        try:
                            im = render_file(p, tmp)
                            if im is not None:
                                images.append(im)
                        except Exception as exc:  # a broken figure file should not stop the paper
                            print(f"  {key}: could not render {f}: {exc}")
                refs = sum(len(re.findall(r"\\(?:ref|cref|Cref|autoref)\{[^}]*" + re.escape(l), tex)) for l in labels)
                if images:
                    cands.append({"figure": number, "caption": caption, "refs": refs, "image": compose(images) if len(images) > 1 else images[0]})
                else:
                    missing[number] = refs  # e.g. drawn in TikZ: crop it from the PDF below
        if missing or not cands:
            source = "latex+pdf" if cands else "pdf"
            time.sleep(3)
            for number, caption, im in figures_from_pdf(arxiv_id, tmp):
                if number in missing or not has_tex or source == "pdf":
                    cands.append({"figure": number, "caption": caption, "refs": missing.get(number, 0), "image": im})
        cands.sort(key=lambda c: c["figure"])
    index = []
    for i, c in enumerate(cands, start=1):
        c["id"] = f"{i:02d}"
        c["image"] = finalize(c["image"])
        c["colour"] = colourfulness(c["image"])
        c["ink"] = ink(c["image"])
        c["image"].save(out / f"{c['id']}.png", optimize=True)
        index.append({k: c[k] for k in ("id", "figure", "caption", "refs", "colour", "ink")} | {"size": c["image"].size})
    (out / "index.json").write_text(json.dumps({"key": key, "arxiv": arxiv_id, "title": title, "source": source, "figures": index}, indent=1))
    if cands:
        sheet(cands, f"{key}  arXiv:{arxiv_id}  {title}", out / "sheet.jpg")
    return len(cands), source


# ---------------------------------------------------------------------------------------------
# Choosing a preview
# ---------------------------------------------------------------------------------------------


def heuristic_choice(figures):
    """Scoring rule tuned on hand-picked previews. Returns (id, rotation, reason)."""
    max_refs = max(f["refs"] for f in figures) or 1
    n = len(figures)

    def score(f):
        w, h = f["size"]
        return (
            0.5 * f["refs"] / max_refs
            - abs(math.log((w / h) / 1.4))  # thumbnails are about 1.4:1
            - 0.5 * (f["figure"] - 1) / max(n - 1, 1)
            + 2 * min(f.get("ink", 0), 0.5)
        )

    best = max(figures, key=score)
    return best["id"], 0, "scoring rule"


PICK_PROMPT = """These are the figures of the paper "{title}", numbered on the sheet (#01, #02, ...). \
Each tile is labelled with its figure number and how often the paper refers to it; captions:

{captions}

Pick the one figure to show as the paper's preview on a publication list, where it is displayed \
about 200 pixels wide. Balance being one of the paper's more important figures (a key result or \
the central idea) against being colourful and easy to read at that small size; prefer a figure \
that is not mostly text or tiny sub-panels. If the figure you pick is shown sideways or upside \
down, give the clockwise rotation that would make it upright, else 0."""


def claude_choice(title, figures, sheet_path, api_key):
    """Ask Claude to pick from the candidate sheet. Returns (id, rotation, reason) or None."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    ids = [f["id"] for f in figures]
    captions = "\n".join(f"#{f['id']} (Fig. {f['figure']}, referenced {f['refs']}x): {f['caption'][:300]}" for f in figures)
    schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "enum": ids},
            "rotate_clockwise": {"type": "integer", "enum": [0, 90, 180, 270]},
            "reason": {"type": "string"},
        },
        "required": ["id", "rotate_clockwise", "reason"],
        "additionalProperties": False,
    }
    response = client.beta.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        thinking={"type": "adaptive"},
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.standard_b64encode(sheet_path.read_bytes()).decode(),
                        },
                    },
                    {"type": "text", "text": PICK_PROMPT.format(title=title, captions=captions)},
                ],
            }
        ],
    )
    if response.stop_reason == "refusal":
        return None
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    return data["id"], data["rotate_clockwise"], "Claude: " + data["reason"]


def auto(todo, report_path):
    """Extract, choose and install a preview for each (key, arxiv_id, title)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    print(f"Choosing previews with {'Claude (' + CLAUDE_MODEL + ')' if api_key else 'the scoring rule'}.")
    text = BIB_PATH.read_text(encoding="utf-8")
    added, failed = {}, []
    with tempfile.TemporaryDirectory() as t:
        out_dir = Path(t)
        for key, arxiv_id, title in todo:
            try:
                count, _ = process(key, arxiv_id, title, out_dir)
                if not count:
                    failed.append((key, "no figures found"))
                    continue
                index = json.loads((out_dir / key / "index.json").read_text())
                figures = index["figures"]
                choice = None
                if api_key:
                    try:
                        choice = claude_choice(title, figures, out_dir / key / "sheet.jpg", api_key)
                    except Exception as exc:  # fall back to the scoring rule rather than skip the paper
                        print(f"  {key}: Claude choice failed ({exc}); using the scoring rule")
                fig_id, rotate, reason = choice or heuristic_choice(figures)
                im = Image.open(out_dir / key / f"{fig_id}.png")
                if rotate:
                    im = im.rotate(-rotate, expand=True)  # PIL rotates counter-clockwise
                PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
                im.save(PREVIEW_DIR / f"{key}.png", optimize=True)
                fig = next(f for f in figures if f["id"] == fig_id)
                added[key] = (fig, rotate, reason)
                print(f"  {key}: Fig. {fig['figure']} ({reason})", flush=True)
            except Exception as exc:
                failed.append((key, str(exc)))
            time.sleep(3)  # arXiv asks for at most one request every 3 seconds

    edits = {}
    for start, end, key, fields in bib_entries_with_spans(text):
        if key in added and not fields.get("preview"):
            edits[start] = (end, add_field(text[start:end], "preview", f"{key}.png"))
    for start in sorted(edits, reverse=True):
        end, new = edits[start]
        text = text[:start] + new + text[end:]
    if edits:
        BIB_PATH.write_text(text, encoding="utf-8")

    if report_path and (added or failed):
        lines = ["", "### Preview figures", ""]
        for key, (fig, rotate, reason) in added.items():
            turn = f", rotated {rotate}°" if rotate else ""
            lines.append(f"- `{key}`: Fig. {fig['figure']}{turn} ({reason})")
        lines += [f"- `{key}`: no preview ({why})" for key, why in failed]
        lines += ["", "To use a different figure, replace `assets/img/publication_preview/<key>.png`."]
        with open(report_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    print(f"Added {len(added)} preview(s); {len(failed)} without.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="_figure_candidates")
    parser.add_argument("--auto", action="store_true", help="choose and install previews (weekly sync)")
    parser.add_argument("--report", help="with --auto: append a markdown summary to this file")
    parser.add_argument("keys", nargs="*", help="only these entries (default: all with arxiv and no preview)")
    args = parser.parse_args(argv)
    out_dir = Path(args.out)
    entries = bib_entries_with_spans(BIB_PATH.read_text(encoding="utf-8"))
    todo = [
        (k, f["arxiv"], f.get("title", ""))
        for _, _, k, f in entries
        if f.get("arxiv") and (k in args.keys if args.keys else not f.get("preview"))
    ]
    if args.auto:
        return auto(todo, args.report)
    summary = {}
    for n, (key, arxiv_id, title) in enumerate(todo, start=1):
        try:
            count, source = process(key, arxiv_id, title, out_dir)
            summary[key] = {"arxiv": arxiv_id, "figures": count, "source": source}
        except Exception as exc:
            summary[key] = {"arxiv": arxiv_id, "error": str(exc)}
        print(f"[{n}/{len(todo)}] {key} ({arxiv_id}): {summary[key]}", flush=True)
        time.sleep(3)  # arXiv asks for at most one request every 3 seconds
    out_dir.mkdir(exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
