# Who cites my work

A self-updating list of papers that cite my work, built from OpenAlex and
Semantic Scholar and published with GitHub Pages. A GitHub Actions job runs
weekly, records the date each citing paper first appears, commits the data,
and redeploys the page. It also writes an Atom feed (`feed.xml`) so new
citations can show up in a feed reader.

## Setup

1. **Create the repository.** Make a new public repo called `citations` and
   add these files, keeping `.github/workflows/update.yml` in place. GitHub
   serves it at `https://<username>.github.io/citations/`.

2. **Add API keys as repository secrets** (Settings, Secrets and variables,
   Actions, New repository secret):
   - `OPENALEX_API_KEY`: required. Free from https://openalex.org/settings/api.
   - `S2_API_KEY`: optional. Free on request from
     https://www.semanticscholar.org/product/api. Without it the script still
     works, just more slowly.

3. **Find your author IDs** on your own machine:

   ```bash
   pip install requests pyyaml
   export OPENALEX_API_KEY=your-key
   python fetch_citations.py --find-author
   ```

   Copy the IDs that are yours into `config.yml`. If your profile is split
   into several IDs, list all of them. Add papers the IDs miss under
   `extra_papers`, and remove misattributed ones with `exclude_papers`.

4. **Turn on Pages.** Settings, Pages, Source: *GitHub Actions*.

5. **Run it once.** Actions tab, *Update citations*, *Run workflow*. After
   that it runs every Monday, and again whenever you push a change to
   `config.yml`, `index.html` or the script.

## How "date added" works

Neither data source records when a citation was added to your profile, so the
script tracks it itself. The first run is a baseline: everything found then is
listed under "Already citing when tracking began", ordered by publication date.
From then on, each newly found paper gets the date of the run that found it.

Papers that a source stops returning are kept rather than deleted, because
sources occasionally drop records for a while. To start over, run
`python fetch_citations.py --baseline` and commit the result.

## Preview locally

```bash
python fetch_citations.py
python -m http.server
# open http://localhost:8000
```

## Notes

- Counts will differ somewhat from Google Scholar, which indexes more
  workshop papers, theses and preprints.
- Self-citations are detected by last name and first initial (set in
  `config.yml`) and can be hidden on the page.
- Change the schedule in `.github/workflows/update.yml` (`cron`). A weekly run
  uses a few dozen API requests, far inside both free allowances.
