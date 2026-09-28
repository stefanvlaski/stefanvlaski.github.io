#!/usr/bin/env python3
"""Add arXiv links to publications in _bibliography/papers.bib.

Run from the repository root:

    python bin/arxiv_links.py              # add arxiv={...} fields to papers.bib
    python bin/arxiv_links.py --dry-run    # only report what would be added

Fetches every arXiv paper with the configured author surname in a single paginated query
(export.arxiv.org API) and matches it to entries that have no `arxiv` field:

* by DOI, when the arXiv record lists the published version's DOI, or
* by title, when the normalized titles are near-identical.

Close title matches below the threshold are only reported, never added, so a human can check
them. Existing `arxiv` values written as "arxiv:XXXX.XXXXX" are normalized to the bare id,
which is what the publication template appends to https://arxiv.org/abs/.
"""

import argparse
import difflib
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scholar_sync import BIB_PATH, _read_braced, normalize_title  # noqa: E402

ARXIV_API = "http://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
AUTHOR_SURNAME = "Vlaski"
TITLE_MATCH_RATIO = 0.92  # add automatically at or above this similarity
REVIEW_RATIO = 0.75  # report for manual review between this and TITLE_MATCH_RATIO
PAGE_SIZE = 200
SKIP_ABBR = {"patent"}


def fetch_arxiv_papers(surname):
    """All arXiv records with an author of this surname: [{id, title, doi, authors}]."""
    papers, start = [], 0
    while True:
        resp = requests.get(
            ARXIV_API,
            params={"search_query": f"au:{surname}", "start": start, "max_results": PAGE_SIZE},
            timeout=60,
        )
        resp.raise_for_status()
        entries = ET.fromstring(resp.content).findall(f"{ATOM}entry")
        for e in entries:
            authors = [a.findtext(f"{ATOM}name", "") for a in e.findall(f"{ATOM}author")]
            if not any(surname.lower() in a.lower() for a in authors):
                continue
            arxiv_url = e.findtext(f"{ATOM}id", "")
            m = re.search(r"abs/(.+?)(v\d+)?$", arxiv_url)
            if not m:
                continue
            papers.append(
                {
                    "id": m.group(1),
                    "title": " ".join(e.findtext(f"{ATOM}title", "").split()),
                    "doi": (e.findtext(f"{ARXIV_NS}doi") or "").strip().lower(),
                    "authors": authors,
                }
            )
        if len(entries) < PAGE_SIZE:
            break
        start += PAGE_SIZE
        time.sleep(3)  # arXiv asks for at most one request every 3 seconds
    if not papers:
        raise RuntimeError(f"arXiv returned no papers for author {surname!r}")
    return papers


def bib_entries_with_spans(text):
    """[(start, end, key, fields)] for every entry; text[start:end] is the whole entry."""
    out = []
    for m in re.finditer(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", text):
        open_brace = text.index("{", m.start())
        _, end = _read_braced(text, open_brace)
        body = text[m.end() : end - 1]
        fields = {}
        for fm in re.finditer(r"(\w+)\s*=\s*", body):
            if fm.start() and body[: fm.start()].rstrip()[-1:] not in (",", ""):
                continue
            try:
                value, _ = _read_braced(body, fm.end())
            except (ValueError, AttributeError):
                continue
            fields.setdefault(fm.group(1).lower(), value.strip())
        out.append((m.start(), end, m.group(2), fields))
    return out


def add_field(entry_text, name, value):
    """Append `name={value}` just before the entry's closing brace."""
    inner = entry_text[:-1].rstrip()
    sep = "\n" if inner.endswith(",") else ",\n"
    return f"{inner}{sep}  {name}={{{value}}}\n}}"


def best_title_match(title, papers):
    norm = normalize_title(title)
    best, best_ratio = None, 0.0
    for p in papers:
        r = difflib.SequenceMatcher(None, norm, p["norm"]).ratio()
        if r > best_ratio:
            best, best_ratio = p, r
    return best, best_ratio


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="report only, do not modify papers.bib")
    parser.add_argument("--report", help="append a markdown summary to this file")
    args = parser.parse_args(argv)

    text = BIB_PATH.read_text(encoding="utf-8")
    papers = fetch_arxiv_papers(AUTHOR_SURNAME)
    for p in papers:
        p["norm"] = normalize_title(p["title"])
    by_doi = {p["doi"]: p for p in papers if p["doi"]}
    print(f"arXiv lists {len(papers)} papers by {AUTHOR_SURNAME}.")

    added, review, missing, normalized = [], [], [], []
    edits = []  # (start, end, new_text), applied back to front
    for start, end, key, fields in bib_entries_with_spans(text):
        entry = text[start:end]
        current = fields.get("arxiv", "")
        if current:
            bare = re.sub(r"^arxiv:\s*", "", current, flags=re.I)
            if bare != current:
                entry = re.sub(r"(arxiv\s*=\s*\{)arxiv:\s*", r"\1", entry, count=1, flags=re.I)
                edits.append((start, end, entry))
                normalized.append((key, bare))
            continue
        if fields.get("abbr", "").lower() in SKIP_ABBR or not fields.get("title"):
            continue

        doi = fields.get("doi", "").strip().lower()
        match, how = None, None
        if doi and doi in by_doi:
            match, how = by_doi[doi], "DOI"
        else:
            cand, ratio = best_title_match(fields["title"], papers)
            if cand and ratio >= TITLE_MATCH_RATIO:
                match, how = cand, f"title ({ratio:.2f})"
            elif cand and ratio >= REVIEW_RATIO:
                review.append((key, fields["title"], cand, ratio))
        if match:
            edits.append((start, end, add_field(entry, "arxiv", match["id"])))
            added.append((key, fields["title"], match, how))
        elif not any(k == key for k, *_ in review):
            missing.append((key, fields["title"]))

    for start, end, new in sorted(edits, reverse=True):
        text = text[:start] + new + text[end:]
    if edits and not args.dry_run:
        BIB_PATH.write_text(text, encoding="utf-8")

    print(f"Added {len(added)}, normalized {len(normalized)}, to review {len(review)}, not found {len(missing)}.")
    lines = [f"### arXiv links", "", f"Added arXiv links to **{len(added)}** publication(s)."]
    if added:
        lines += [""] + [
            f"- `{k}`: {t} → [arXiv:{p['id']}](https://arxiv.org/abs/{p['id']}) (matched by {how})" for k, t, p, how in added
        ]
    if normalized:
        lines += ["", f"Normalized {len(normalized)} existing link(s) written as `arxiv:ID` to the bare id: "
                  + ", ".join(f"`{k}`" for k, _ in normalized) + "."]
    if review:
        lines += ["", "**Possible matches, not added** (titles differ; add `arxiv={id}` by hand if correct):", ""]
        lines += [
            f"- `{k}`: {t}  \n  ↔ [arXiv:{p['id']}](https://arxiv.org/abs/{p['id']}) *{p['title']}* (similarity {r:.2f})"
            for k, t, p, r in review
        ]
    if missing:
        lines += ["", f"<details><summary>No arXiv version found for {len(missing)} publication(s)</summary>", ""]
        lines += [f"- `{k}`: {t}" for k, t in missing] + ["", "</details>"]
    report = "\n".join(lines) + "\n"
    print(report)
    if args.report:
        with open(args.report, "a", encoding="utf-8") as fh:
            fh.write("\n" + report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
