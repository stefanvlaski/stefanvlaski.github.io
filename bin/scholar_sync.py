#!/usr/bin/env python3
"""Sync new publications from a Google Scholar profile into _bibliography/papers.bib.

Run from the repository root:

    python bin/scholar_sync.py                 # add new entries to papers.bib
    python bin/scholar_sync.py --dry-run       # only report what would be added

Google Scholar has no official API. Two backends are supported:

* SerpAPI (https://serpapi.com), used whenever SERPAPI_API_KEY is set. Reliable from CI.
* Direct fetching of the public Scholar profile page. Free, but Google often answers requests
  from cloud/CI IP ranges (including GitHub Actions) with a CAPTCHA. When that happens the
  script stops with a clear error instead of writing anything.

Only publications that are not already in papers.bib (matched by title, tolerant to case,
punctuation and LaTeX braces) are added. Settings and an ignore list live in
_data/scholar_sync.yml.
"""

import argparse
import difflib
import json
import os
import random
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parent.parent
BIB_PATH = REPO_ROOT / "_bibliography" / "papers.bib"
CONFIG_PATH = REPO_ROOT / "_data" / "scholar_sync.yml"
SOCIALS_PATH = REPO_ROOT / "_data" / "socials.yml"

SCHOLAR_URL = "https://scholar.google.com/citations"
SERPAPI_URL = "https://serpapi.com/search.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TITLE_MATCH_RATIO = 0.93

# Venue substring -> badge abbreviation shown on the publications page. First match wins.
VENUE_ABBREVIATIONS = [
    ("signal and information processing over networks", "TSIPN"),
    ("transactions on signal processing", "TSP"),
    ("transactions on information theory", "TIT"),
    ("transactions on automatic control", "TAC"),
    ("transactions on control of network systems", "TCNS"),
    ("transactions on neural networks and learning systems", "TNNLS"),
    ("transactions on machine learning research", "TMLR"),
    ("open journal of signal processing", "OJSP"),
    ("open journal of control systems", "OJCSYS"),
    ("signal processing letters", "SPL"),
    ("signal processing magazine", "SPM"),
    ("proceedings of the ieee", "Proc. IEEE"),
    ("journal of machine learning research", "JMLR"),
    ("siam journal on optimization", "SIOPT"),
    ("acoustics, speech", "ICASSP"),
    ("european signal processing conference", "EUSIPCO"),
    ("asilomar", "Asilomar"),
    ("conference on decision and control", "CDC"),
    ("american control conference", "ACC"),
    ("european control conference", "ECC"),
    ("neural information processing systems", "NeurIPS"),
    ("international conference on machine learning", "ICML"),
    ("learning representations", "ICLR"),
    ("artificial intelligence and statistics", "AISTATS"),
    ("machine learning for signal processing", "MLSP"),
    ("signal processing advances in wireless", "SPAWC"),
    ("statistical signal processing", "SSP"),
    ("computational advances in multi-sensor", "CAMSAP"),
    ("sensor array and multichannel", "SAM"),
    ("data science and learning workshop", "DSLW"),
    ("global conference on signal and information processing", "GlobalSIP"),
    ("international conference on communications", "ICC"),
]


class ScholarBlocked(RuntimeError):
    pass


# --------------------------------------------------------------------------------------------
# papers.bib parsing
# --------------------------------------------------------------------------------------------


def _read_braced(text, i):
    """Return (value, end_index) for a {...} or "..." value starting at text[i]."""
    if text[i] == "{":
        depth, j = 0, i
        while j < len(text):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    return text[i + 1 : j], j + 1
            j += 1
        raise ValueError("unbalanced braces")
    if text[i] == '"':
        j = text.index('"', i + 1)
        return text[i + 1 : j], j + 1
    m = re.match(r"[^,}\s]+", text[i:])
    return m.group(0), i + m.end()


def parse_bib(text):
    """Minimal BibTeX reader: list of {'type', 'key', 'fields'} dicts (field names lowercased)."""
    entries = []
    for m in re.finditer(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", text):
        body_start = m.end()
        # find the end of this entry by brace matching from the opening brace
        _, body_end = _read_braced(text, text.index("{", m.start()))
        body = text[body_start : body_end - 1]
        fields = {}
        for fm in re.finditer(r"(\w+)\s*=\s*", body):
            if fm.start() and body[: fm.start()].rstrip()[-1:] not in (",", ""):
                continue  # '=' inside a value, not a field boundary
            try:
                value, _ = _read_braced(body, fm.end())
            except (ValueError, AttributeError):
                continue
            fields.setdefault(fm.group(1).lower(), value.strip())
        entries.append({"type": m.group(1).lower(), "key": m.group(2), "fields": fields})
    return entries


def normalize_title(title):
    title = unicodedata.normalize("NFKD", title)
    title = re.sub(r"\\[a-zA-Z]+", " ", title)  # LaTeX commands
    title = re.sub(r"[^a-z0-9]+", " ", title.lower())
    return " ".join(title.split())


def find_match(title, known_titles):
    norm = normalize_title(title)
    if not norm:
        return None
    if norm in known_titles:
        return norm
    close = difflib.get_close_matches(norm, list(known_titles), n=1, cutoff=TITLE_MATCH_RATIO)
    return close[0] if close else None


# --------------------------------------------------------------------------------------------
# Scholar backends. Both return normalized dicts:
#   list:   {"id", "title", "authors", "venue", "year"}
#   detail: {"title", "link", "authors", "publication_date", "journal", "conference", ...}
# --------------------------------------------------------------------------------------------


class DirectScholar:
    def __init__(self, user_id, delay=(3.0, 7.0)):
        self.user_id = user_id
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
        self._first = True

    def _get(self, params):
        if not self._first:
            time.sleep(random.uniform(*self.delay))
        self._first = False
        resp = self.session.get(SCHOLAR_URL, params=params, timeout=30)
        body = resp.text
        if (
            resp.status_code in (403, 429)
            or "/sorry/" in resp.url
            or "gs_captcha" in body
            or "unusual traffic" in body
        ):
            raise ScholarBlocked(
                f"Google Scholar refused the request (HTTP {resp.status_code}, likely a CAPTCHA). "
                "Set SERPAPI_API_KEY to use SerpAPI instead."
            )
        resp.raise_for_status()
        return BeautifulSoup(body, "html.parser")

    def list_publications(self):
        pubs, start, page_size = [], 0, 100
        while True:
            soup = self._get(
                {"user": self.user_id, "hl": "en", "cstart": start, "pagesize": page_size, "sortby": "pubdate"}
            )
            rows = soup.select("tr.gsc_a_tr")
            for row in rows:
                link = row.select_one("a.gsc_a_at")
                if link is None:
                    continue
                m = re.search(r"citation_for_view=[^:&]+:([^&]+)", link.get("href", "") or link.get("data-href", ""))
                gray = row.select("div.gs_gray")
                for year_suffix in row.select("div.gs_gray span.gs_oph"):
                    year_suffix.decompose()
                year_el = row.select_one("td.gsc_a_y span")
                year_text = year_el.get_text(strip=True) if year_el else ""
                pubs.append(
                    {
                        "id": m.group(1) if m else None,
                        "title": link.get_text(" ", strip=True),
                        "authors": gray[0].get_text(" ", strip=True) if gray else "",
                        "venue": gray[1].get_text(" ", strip=True) if len(gray) > 1 else "",
                        "year": int(year_text) if year_text.isdigit() else None,
                    }
                )
            if len(rows) < page_size:
                break
            start += page_size
        if not pubs:
            raise ScholarBlocked("Scholar returned a profile page with no publications; the page layout may have changed.")
        return pubs

    def publication_detail(self, pub_id):
        soup = self._get(
            {"view_op": "view_citation", "hl": "en", "user": self.user_id, "citation_for_view": f"{self.user_id}:{pub_id}"}
        )
        detail = {}
        title_el = soup.select_one("#gsc_oci_title")
        if title_el:
            detail["title"] = title_el.get_text(" ", strip=True)
            link = title_el.select_one("a")
            if link and link.get("href"):
                detail["link"] = link["href"]
        for row in soup.select("div.gs_scl"):
            field = row.select_one(".gsc_oci_field")
            value = row.select_one(".gsc_oci_value")
            if field and value:
                name = re.sub(r"\W+", "_", field.get_text(strip=True).lower()).strip("_")
                detail[name] = value.get_text(" ", strip=True)
        return detail


class SerpApiScholar:
    def __init__(self, user_id, api_key):
        self.user_id = user_id
        self.api_key = api_key

    def _get(self, params):
        resp = requests.get(
            SERPAPI_URL,
            params={"engine": "google_scholar_author", "hl": "en", "api_key": self.api_key, **params},
            timeout=60,
        )
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"SerpAPI error: {data['error']}")
        resp.raise_for_status()
        return data

    def list_publications(self):
        pubs, start = [], 0
        while True:
            data = self._get({"author_id": self.user_id, "sort": "pubdate", "num": 100, "start": start})
            articles = data.get("articles", [])
            for a in articles:
                year = str(a.get("year") or "")
                pubs.append(
                    {
                        "id": (a.get("citation_id") or "").split(":")[-1] or None,
                        "title": a.get("title", ""),
                        "authors": a.get("authors", ""),
                        "venue": a.get("publication", ""),
                        "year": int(year) if year.isdigit() else None,
                    }
                )
            if len(articles) < 100:
                break
            start += 100
        return pubs

    def publication_detail(self, pub_id):
        data = self._get({"view_op": "view_citation", "citation_id": f"{self.user_id}:{pub_id}"})
        citation = data.get("citation", {})
        return {k: (str(v) if not isinstance(v, (dict, list)) else v) for k, v in citation.items()}


# --------------------------------------------------------------------------------------------
# Building BibTeX entries
# --------------------------------------------------------------------------------------------


def arxiv_id(*texts):
    for t in texts:
        m = re.search(r"arXiv[:\s]*(?:preprint\s+arXiv:)?\s*(\d{4}\.\d{4,5})", t or "", re.I)
        if m:
            return m.group(1)
        m = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", t or "", re.I)
        if m:
            return m.group(1)
    return None


def venue_abbreviation(venue):
    low = venue.lower()
    for needle, abbr in VENUE_ABBREVIATIONS:
        if needle in low:
            return abbr
    m = re.search(r"\(([A-Z][A-Za-z\-]{1,12})\)\s*$", venue) or re.match(r"([A-Z]{2,}[A-Za-z]*)\s+\d{4}\b", venue)
    return m.group(1) if m else ""


def clean_conference(name):
    # Scholar often writes "ICASSP 2025-2025 IEEE International ..." or "ICC 2026-IEEE International ..."
    return re.sub(r"^[A-Za-z][\w'&\- ]*?\s+(\d{4})\s*-\s*(?:\1\s+)?", r"\1 ", name.strip())


def bib_escape(value):
    return value.replace("&", r"\&").replace("%", r"\%").replace("#", r"\#")


def make_key(authors, year, title, taken):
    first_author = authors[0] if authors else "Anon"
    last = re.sub(r"[^A-Za-z]", "", unicodedata.normalize("NFKD", first_author.split()[-1])) or "Anon"
    words = [w for w in re.findall(r"[A-Za-z]+", title) if w.lower() not in {"a", "an", "the", "on", "of", "for"}]
    base = f"{last}{year or ''}{words[0].capitalize() if words else ''}"
    key, n = base, 2
    while key in taken:
        key, n = f"{base}{chr(ord('a') + n - 2)}", n + 1
    taken.add(key)
    return key


def build_entry(pub, detail, taken_keys):
    title = detail.get("title") or pub["title"]
    authors_raw = detail.get("authors") or detail.get("inventors") or pub["authors"]
    authors = [a.strip() for a in authors_raw.split(",") if a.strip() and a.strip() != "..."]
    date = detail.get("publication_date", "")
    year = pub["year"] or (int(date[:4]) if date[:4].isdigit() else None)
    link = detail.get("link", "")
    journal = detail.get("journal", "")
    conference = detail.get("conference", "")
    book = detail.get("book", "")
    source = detail.get("source", "")
    arx = arxiv_id(journal, source, pub.get("venue", ""), link)

    fields = {"title": title, "author": " and ".join(authors)}
    patent_number = detail.get("patent_number", "")
    if patent_number or detail.get("patent_office"):
        entry_type = "article"
        office = detail.get("patent_office", "")
        fields["journal"] = f"{office} Patent {patent_number}".strip()
        abbr = "patent"
    elif arx and not conference and (not journal or "arxiv" in journal.lower()):
        entry_type = "article"
        fields["journal"] = f"available as arXiv:{arx}"
        abbr = "preprint"
    elif conference:
        entry_type = "inproceedings"
        conf = clean_conference(conference)
        fields["booktitle"] = conf if conf.lower().startswith("proc") else f"Proc. of {conf}"
        abbr = venue_abbreviation(conference)
    elif journal:
        entry_type = "article"
        fields["journal"] = journal
        abbr = venue_abbreviation(journal)
    elif book:
        entry_type = "incollection"
        fields["booktitle"] = book
        abbr = venue_abbreviation(book)
    else:
        entry_type = "misc"
        venue = source or pub.get("venue", "")
        if venue:
            fields["howpublished"] = re.sub(r",?\s*\d{4}\s*$", "", venue)
        abbr = venue_abbreviation(venue)

    if year:
        fields["year"] = str(year)
    note = None
    if abbr != "preprint":
        for src, dst in (("volume", "volume"), ("issue", "number"), ("pages", "pages"), ("publisher", "publisher")):
            if detail.get(src) and entry_type != "misc":
                fields[dst] = detail[src]
    if arx:
        fields["arxiv"] = arx
    if link and not re.search(r"scholar\.google|arxiv\.org|adsabs\.harvard\.edu", link):
        fields["html"] = link
        if abbr == "preprint":
            note = f"`{{key}}` is listed on Scholar as an arXiv preprint but links to {link}; it may be published there."
    if abbr:
        fields["abbr"] = abbr
    if pub.get("id"):
        fields["scholar_id"] = pub["id"]

    key = make_key(authors, year, title, taken_keys)
    lines = [f"@{entry_type}{{{key},"]
    for name, value in fields.items():
        value = value if name in ("html",) else bib_escape(value)
        lines.append(f"  {name}={{{value}}},")
    lines.append("}")
    return key, "\n".join(lines), note.format(key=key) if note else None


# --------------------------------------------------------------------------------------------
# Main sync logic
# --------------------------------------------------------------------------------------------


def load_config():
    config = {}
    if CONFIG_PATH.exists():
        config = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    user_id = config.get("scholar_user_id")
    if not user_id and SOCIALS_PATH.exists():
        socials = yaml.safe_load(SOCIALS_PATH.read_text()) or {}
        user_id = str(socials.get("scholar_userid", ""))
    config["scholar_user_id"] = (user_id or "").split("&")[0].strip()
    config.setdefault("min_year", None)
    config["ignore"] = [str(x) for x in (config.get("ignore") or [])]
    return config


def is_arxiv_pub(pub):
    return "arxiv" in (pub.get("venue") or "").lower()


def plan_sync(pubs, bib_entries, config):
    """Decide which Scholar publications are new. Returns (new_pubs, skipped_counts, notes)."""
    known = {}
    for e in bib_entries:
        t = normalize_title(e["fields"].get("title", ""))
        if t:
            known[t] = e
    known_ids = {e["fields"].get("scholar_id") for e in bib_entries if e["fields"].get("scholar_id")}
    known_arxiv = {}
    for e in bib_entries:
        a = arxiv_id(e["fields"].get("arxiv", "").replace("arxiv:", "arXiv:"), e["fields"].get("journal", ""))
        if not a and re.fullmatch(r"\d{4}\.\d{4,5}", e["fields"].get("arxiv", "")):
            a = e["fields"]["arxiv"]
        if a:
            known_arxiv[a] = e
    seen_arxiv = {}
    ignore_ids = {x for x in config["ignore"]}
    ignore_titles = {normalize_title(x) for x in config["ignore"]}

    counts = {"already_in_bib": 0, "ignored": 0, "too_old": 0, "duplicate_on_scholar": 0}
    notes = []
    candidates = {}
    for pub in pubs:
        norm = normalize_title(pub["title"])
        if pub["id"] in ignore_ids or norm in ignore_titles:
            counts["ignored"] += 1
            continue
        if pub["id"] and pub["id"] in known_ids:
            counts["already_in_bib"] += 1
            continue
        match = find_match(pub["title"], known)
        if match:
            counts["already_in_bib"] += 1
            existing = known[match]
            if existing["fields"].get("abbr", "").lower() == "preprint" and not is_arxiv_pub(pub) and pub["venue"]:
                notes.append(
                    f"`{existing['key']}` is listed as a preprint, but Scholar also has a version in "
                    f"*{pub['venue']}*. You may want to update it."
                )
            continue
        # Same arXiv id as an existing entry or an earlier new item: a renamed version of the same paper.
        arx = arxiv_id(pub.get("venue", ""))
        if arx and arx in known_arxiv:
            counts["already_in_bib"] += 1
            continue
        if arx and arx in seen_arxiv:
            counts["duplicate_on_scholar"] += 1
            notes.append(f"Skipped *{pub['title']}*: same arXiv id ({arx}) as *{seen_arxiv[arx]}*.")
            continue
        if config["min_year"] and (pub["year"] is None or pub["year"] < int(config["min_year"])):
            counts["too_old"] += 1
            continue
        # Scholar sometimes lists the arXiv and published versions separately; keep the published one.
        if norm in candidates:
            counts["duplicate_on_scholar"] += 1
            if is_arxiv_pub(candidates[norm]) and not is_arxiv_pub(pub):
                candidates[norm] = pub
            continue
        candidates[norm] = pub
        if arx:
            seen_arxiv[arx] = pub["title"]
    return list(candidates.values()), counts, notes


def insert_entries(bib_text, new_blocks):
    """Insert new entries right after the Jekyll front matter, newest first."""
    block = "\n\n".join(new_blocks) + "\n\n"
    m = re.match(r"\s*---\s*\n.*?---\s*\n", bib_text, re.S)
    if m:
        return bib_text[: m.end()] + block + bib_text[m.end() :]
    return block + bib_text


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="report only, do not modify papers.bib")
    parser.add_argument("--report", help="write a markdown summary to this file")
    parser.add_argument("--min-year", type=int, help="override min_year from _data/scholar_sync.yml")
    args = parser.parse_args(argv)

    config = load_config()
    if args.min_year:
        config["min_year"] = args.min_year
    user_id = config["scholar_user_id"]
    if not user_id:
        sys.exit("No Scholar user id: set scholar_user_id in _data/scholar_sync.yml")

    api_key = os.environ.get("SERPAPI_API_KEY", "").strip()
    backend = SerpApiScholar(user_id, api_key) if api_key else DirectScholar(user_id)
    print(f"Backend: {'SerpAPI' if api_key else 'direct Google Scholar'}; profile {user_id}")

    bib_text = BIB_PATH.read_text(encoding="utf-8")
    bib_entries = parse_bib(bib_text)

    try:
        pubs = backend.list_publications()
    except ScholarBlocked as exc:
        sys.exit(f"ERROR: {exc}")
    print(f"Scholar lists {len(pubs)} publications; papers.bib has {len(bib_entries)} entries.")

    new_pubs, counts, notes = plan_sync(pubs, bib_entries, config)
    print(f"Skipped: {json.dumps(counts)}. New: {len(new_pubs)}.")

    taken = {e["key"] for e in bib_entries}
    added = []
    for pub in new_pubs:
        detail = {}
        if pub["id"]:
            try:
                detail = backend.publication_detail(pub["id"])
            except ScholarBlocked as exc:
                print(f"WARNING: {exc} Using the less complete profile-list data for the remaining entries.")
                backend.publication_detail = lambda _id: {}  # stop hammering a blocked endpoint
        key, block, note = build_entry(pub, detail, taken)
        added.append((key, pub, block))
        if note:
            notes.append(note)
        print(f"  + {key}: {pub['title']}")

    if added and not args.dry_run:
        BIB_PATH.write_text(insert_entries(bib_text, [b for _, _, b in added]), encoding="utf-8")
        print(f"Wrote {len(added)} new entries to {BIB_PATH.relative_to(REPO_ROOT)}")

    if args.report:
        lines = [f"Found **{len(added)}** new publication(s) on Google Scholar.", ""]
        for key, pub, _ in added:
            lines.append(f"- `{key}` ({pub['year'] or 'n.d.'}): {pub['title']} — *{pub['venue'] or 'no venue'}*")
        if notes:
            lines += ["", "### Notes", ""] + [f"- {n}" for n in notes]
        lines += [
            "",
            "Please check author names, venue names and badges (`abbr`) before merging. "
            "To stop an item from being proposed again, add its `scholar_id` or title to "
            "`ignore:` in `_data/scholar_sync.yml`.",
        ]
        Path(args.report).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
