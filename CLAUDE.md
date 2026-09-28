# Notes for Claude sessions

Context for a Claude session picking up work on this site. It records what exists, why, and the
working conventions agreed with Stefan (the owner). Keep it current: add to it when something
nontrivial changes. This repository is public, so write nothing here that shouldn't be public.

## The site

Personal academic website of Stefan Vlaski, built with the [al-folio](https://github.com/alshedivat/al-folio)
Jekyll template and deployed to GitHub Pages from `master` by `.github/workflows/deploy.yml`.

- Publications page: `_pages/publications.md`, generated from `_bibliography/papers.bib` by
  jekyll-scholar, grouped by year. `_layouts/bib.liquid` renders each entry.
- Group pages: `_pages/about_*.md`, `group.md`; news items: `_news/`.
- `_posts/` and `_projects/` hold the template's demo content. They are excluded from the build in
  `_config.yml` and skipped by the link check, but **kept on purpose** (Stefan may use them later).
  Don't delete them. To use them: remove them from `exclude:` in `_config.yml` and from the
  `--exclude-path` list in `.github/workflows/broken-links.yml`, and fix their demo image references.

## Publication automation

`.github/workflows/scholar-sync.yml` runs every Monday (06:17 UTC) and on demand. It opens a pull
request (branch `scholar-sync`) only when something changed. Steps:

1. `bin/scholar_sync.py` adds Google Scholar publications missing from `papers.bib` (title
   matching tolerant to case/punctuation/LaTeX; items sharing an arXiv id count as the same paper).
   It uses SerpAPI via the `SERPAPI_API_KEY` secret (set); direct Scholar scraping is the fallback
   and is usually CAPTCHA-blocked on GitHub runners. The PR body notes existing preprints that
   Scholar shows as published.
2. `bin/arxiv_links.py` adds `arxiv={id}` to entries without one: fetches all arXiv papers by
   "Vlaski" and matches by DOI or ≥ 92% title similarity; each arXiv id goes to at most one entry.
   Close or conflicting matches, and existing links whose arXiv title disagrees, are only reported.
3. `bin/arxiv_figures.py --auto` adds a preview figure for entries with an arXiv id and no
   `preview`: extracts every figure from the arXiv source (PDF crops above captions for TikZ
   figures), picks one, saves `assets/img/publication_preview/<key>.png`, sets `preview={<key>.png}`.
   The pick uses Claude if an `ANTHROPIC_API_KEY` secret exists (**not set**; Stefan prefers
   asking a Claude session instead), otherwise a scoring rule (refs, filled area, aspect ratio, early figures) that agreed with
   hand-picked previews on ~60% of papers. Stefan will ask a Claude session to improve figures
   when needed (see below); no scheduled session.

Settings: `_data/scholar_sync.yml` (`min_year: 2024`, `ignore:` list of Scholar ids/titles never to
propose; currently the arXiv version of the TSIPN weighted-mean paper, whose journal version is kept).

## Conventions for `papers.bib`

- Preprints: `journal={available as arXiv:ID}`, `abbr={preprint}`, `arxiv={ID}`.
- `arxiv` holds the bare id (e.g. `2406.18418`), never `arxiv:2406.18418` (the template appends it
  to `https://arxiv.org/abs/`).
- `abbr` is the venue badge (TSP, TIT, TSIPN, ICASSP, EUSIPCO, …). Titles in title case.
- `scholar_id` records the Scholar item an entry came from (hidden from rendered BibTeX).
- When a preprint gets published, update that entry to the journal version (keep `arxiv`) rather
  than adding a second one. Check for an existing journal entry first; duplicates have happened.
- `preview={<key>.png}` points into `assets/img/publication_preview/`. Thumbnails are enabled
  (`enable_publication_thumbnails: true`). Entries without an arXiv version have no preview.

## Updating preview figures (when Stefan asks)

Aim: per paper, one of the more important figures that is also colourful and legible at ~200 px.
Avoid the same figure on a conference paper and its journal version. The 19 older previews were
hand-cropped by Stefan; keep them unless asked.

arXiv is blocked from the Claude sandbox, so extraction runs on GitHub:

1. On the working branch, add a temporary workflow triggered by `push` to that branch that runs
   `python bin/arxiv_figures.py --out _figure_candidates KEY ...` (needs `apt-get update` then
   `ghostscript fonts-dejavu-core`; `pip install requests pymupdf pillow numpy beautifulsoup4 pyyaml`)
   and commits `_figure_candidates/` back to the branch.
2. Fetch it and look at each `<key>/sheet.jpg` (numbered candidates with figure number, reference
   count and caption). Check the chosen `NN.png` for orientation; some sources are stored sideways.
3. Copy choices to `assets/img/publication_preview/<key>.png`, then rewrite the branch so the
   candidates and the temporary workflow never reach `master` (e.g. `git reset --soft origin/master`,
   unstage them, commit, force-push the working branch).

The same "temporary workflow on the branch" pattern works for anything else needing arXiv,
Scholar, Crossref, etc.

## Checks and local builds

CI checks: Prettier (`prettier.yml`), link check on Markdown (`broken-links.yml`, accepts 403/418/429
because Scholar/IEEE/imperial.ac.uk block bots), link check on the built site
(`broken-links-site.yml`, after each deploy). All three passed on `master` as of 2026-09-28.

- Prettier runs the versions pinned in `package.json` (prettier 3.1.1, liquid plugin 1.4.0).
  Use `npm ci` with the repo's `package.json`/`package-lock.json`; the latest Prettier formats
  differently and fails CI.
- Local build works in the sandbox: `BUNDLE_PATH=<scratch>/bundle bundle install`,
  `pip install nbconvert`, then
  `LANG=C.UTF-8 JEKYLL_ENV=production bundle exec jekyll build -d <scratch>/_site` (~5 min). No ImageMagick, so resized `.webp` files are missing locally only. Chromium +
  Playwright can screenshot pages served from `_site`.

## Working with Stefan

- Work on the assigned `claude/...` branch and open a PR; Stefan reviews and merges.
- PR descriptions and commit messages: short, no Claude session links (the repo is public). Keep
  the "Generated with Claude Code" line / `Co-Authored-By` credit.
- Stefan sometimes merges a PR before the last pushed commits; after a merge, check
  `git log origin/master..origin/<branch>` and open a follow-up PR for anything left over.
- Ask before deleting files or content.

## History (Sept 2026)

- #1 Weekly Scholar sync; fixed malformed author/title fields in `papers.bib`.
- #2 First sync run (22 publications from 2024–2026); #4 and #5 fixed its issues: ICC venue,
  duplicate preprints, JMLR version of the mean-aggregator paper, TSIPN version of the
  weighted-mean paper, title case, five preprints updated to journal versions, pre-existing
  duplicate entries removed.
- #6 Link checks fixed (demo content excluded, bot-blocking accepted, two LinkedIn links missing
  `https://` fixed, dead ICASSP 2022 tutorial link removed). #7/#8 Prettier formatting (no change
  to the built HTML); duplicate coauthor removed from `_data/coauthors.yml`.
- #9 arXiv links: 61 of 95 entries linked (every arXiv paper by Vlaski); the adversarial-training
  journal paper had the ICASSP paper's arXiv id and was corrected.
- #10 Preview figures for all 65 entries with arXiv versions, automatic previews in the weekly sync,
  this file.

## Open items

- ICASSP 2022 tutorial news item (`_news/2022-05-icassp.md`) has no link; a replacement (recording,
  slides) would be welcome.
- JMLR mean-aggregator entry (`Peng2025Mean`) lacks article number/pages (jmlr.org was unreachable).
- The two papers converted from preprint in #5 have volume/pages from Scholar but no DOI.
- Some conference papers have no arXiv version of their own; the arXiv link script suggests their
  journal versions' ids (e.g. EUSIPCO "Finite Bit Quantization…", SAM "Differential Error
  Feedback… Optimization"). These were deliberately left unlinked.
