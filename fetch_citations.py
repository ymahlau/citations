#!/usr/bin/env python3
"""Collect the papers that cite your work, from OpenAlex and Semantic Scholar.

Usage:
    python fetch_citations.py --find-author   # print candidate author IDs for config.yml
    python fetch_citations.py                 # update citations.json and feed.xml
    python fetch_citations.py --baseline      # rebuild, treating everything as pre-existing

Environment variables:
    OPENALEX_API_KEY  free key from https://openalex.org/settings/api (required by OpenAlex)
    S2_API_KEY        optional Semantic Scholar key (higher rate limits)
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.yml"
DATA_FILE = ROOT / "citations.json"
FEED_FILE = ROOT / "feed.xml"

OA_BASE = "https://api.openalex.org"
S2_BASE = "https://api.semanticscholar.org/graph/v1"

OA_WORK_FIELDS = "id,doi,display_name,publication_year,publication_date,primary_location,authorships,type"
S2_PAPER_FIELDS = "paperId,title,year,publicationDate,venue,externalIds,authors,url"
SKIP_OA_TYPES = {"paratext", "erratum", "retraction", "peer-review", "supplementary-materials"}

ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$")
ARXIV_URL = re.compile(r"arxiv\.org/(?:abs|pdf)/([^\s?#]+?)(?:\.pdf)?(?:v\d+)?$", re.I)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- HTTP


class Client:
    def __init__(self, oa_key: str | None, s2_key: str | None, email: str | None):
        self.session = requests.Session()
        agent = "citation-tracker (github.com; personal citation list)"
        if email:
            agent += f" mailto:{email}"
        self.session.headers["User-Agent"] = agent
        self.oa_key = oa_key
        self.s2_key = s2_key
        # Unauthenticated Semantic Scholar shares a small pool, so go slowly.
        self.s2_gap = 1.1 if s2_key else 3.5
        self._last_s2 = 0.0
        self.calls = {"openalex": 0, "semanticscholar": 0}

    def _get(self, url: str, params: dict, headers: dict | None = None, retries: int = 6):
        delay = 5.0
        for _ in range(retries):
            try:
                r = self.session.get(url, params=params, headers=headers, timeout=60)
            except requests.RequestException as exc:
                log(f"  network error ({exc.__class__.__name__}), retrying in {delay:.0f}s")
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                retry_after = r.headers.get("Retry-After", "")
                wait = float(retry_after) if retry_after.isdigit() else delay
                log(f"  HTTP {r.status_code} from {url}, retrying in {wait:.0f}s")
                time.sleep(min(wait, 120))
                delay *= 2
                continue
            raise RuntimeError(f"HTTP {r.status_code} from {url}: {r.text[:300]}")
        raise RuntimeError(f"Giving up on {url} after {retries} attempts")

    # OpenAlex ---------------------------------------------------------------
    def oa(self, path: str, **params):
        if self.oa_key:
            params["api_key"] = self.oa_key
        self.calls["openalex"] += 1
        return self._get(OA_BASE + path, params)

    def oa_works(self, filter_: str) -> list[dict]:
        results, cursor = [], "*"
        while cursor:
            data = self.oa("/works", filter=filter_, select=OA_WORK_FIELDS, cursor=cursor, **{"per-page": 200})
            if not data or not data.get("results"):
                break
            results.extend(data["results"])
            cursor = data.get("meta", {}).get("next_cursor")
        return results

    # Semantic Scholar ---------------------------------------------------------
    def s2(self, path: str, **params):
        wait = self.s2_gap - (time.time() - self._last_s2)
        if wait > 0:
            time.sleep(wait)
        self._last_s2 = time.time()
        headers = {"x-api-key": self.s2_key} if self.s2_key else None
        self.calls["semanticscholar"] += 1
        return self._get(S2_BASE + path, params, headers)

    def s2_paged(self, path: str, fields: str, limit: int = 1000) -> list[dict]:
        results, offset = [], 0
        while True:
            data = self.s2(path, fields=fields, limit=limit, offset=offset)
            if not data or not data.get("data"):
                break
            results.extend(data["data"])
            if "next" not in data:
                break
            offset = data["next"]
        return results


# --------------------------------------------------------------------- normalising


def ascii_fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()


def clean_text(text: str | None) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")  # OpenAlex titles sometimes carry <i>, <sub>, ...
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def norm_title(title: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", ascii_fold(clean_text(title)).lower())


def norm_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi.strip().lower())
    return doi or None


def make_keys(doi: str | None, arxiv: str | None, title: str | None) -> set[str]:
    """Identifiers used to recognise the same paper across sources and runs."""
    keys = set()
    doi = norm_doi(doi)
    if doi:
        m = ARXIV_DOI.match(doi)
        if m:
            arxiv = arxiv or m.group(1)
        else:
            keys.add("doi:" + doi)
    if arxiv:
        keys.add("arxiv:" + re.sub(r"v\d+$", "", arxiv.lower()))
    t = norm_title(title)
    if len(t) >= 20:  # short titles ("Introduction") collide too easily
        keys.add("title:" + t)
    return keys


def from_openalex(w: dict) -> dict:
    loc = w.get("primary_location") or {}
    venue = (loc.get("source") or {}).get("display_name")
    doi = norm_doi(w.get("doi"))
    landing = loc.get("landing_page_url") or ""
    m = ARXIV_URL.search(landing)
    arxiv = m.group(1) if m else None
    authors = [(a.get("author") or {}).get("display_name") for a in w.get("authorships") or []]
    return {
        "title": clean_text(w.get("display_name")),
        "authors": [a for a in authors if a],
        "year": w.get("publication_year"),
        "publication_date": w.get("publication_date"),
        "venue": venue,
        "doi": doi,
        "arxiv": arxiv,
        "url": f"https://doi.org/{doi}" if doi else (landing or w.get("id")),
        "openalex": [w["id"].rsplit("/", 1)[-1]],
        "s2": [],
        "type": w.get("type"),
    }


def from_s2(p: dict) -> dict:
    ext = p.get("externalIds") or {}
    doi = norm_doi(ext.get("DOI"))
    arxiv = ext.get("ArXiv")
    if doi:
        url = f"https://doi.org/{doi}"
    elif arxiv:
        url = f"https://arxiv.org/abs/{arxiv}"
    else:
        url = p.get("url")
    return {
        "title": clean_text(p.get("title")),
        "authors": [a.get("name") for a in p.get("authors") or [] if a.get("name")],
        "year": p.get("year"),
        "publication_date": p.get("publicationDate"),
        "venue": p.get("venue") or None,
        "doi": doi,
        "arxiv": arxiv,
        "url": url,
        "openalex": [],
        "s2": [p["paperId"]] if p.get("paperId") else [],
        "type": None,
    }


def is_preprint_venue(venue: str | None) -> bool:
    return not venue or "arxiv" in venue.lower() or "rxiv" in venue.lower()


def merge_into(a: dict, b: dict) -> None:
    """Fold record b into record a, keeping the most informative fields."""
    if not a.get("title"):
        a["title"] = b.get("title")
    if len(b.get("authors") or []) > len(a.get("authors") or []):
        a["authors"] = b["authors"]
    if is_preprint_venue(a.get("venue")) and not is_preprint_venue(b.get("venue")):
        a["venue"] = b["venue"]
    elif not a.get("venue"):
        a["venue"] = b.get("venue")
    for field in ("year", "publication_date"):
        if b.get(field) and (not a.get(field) or str(b[field]) < str(a[field])):
            a[field] = b[field]
    # Prefer a real (non-arXiv) DOI for the link.
    b_doi_real = b.get("doi") and not ARXIV_DOI.match(b["doi"])
    a_doi_real = a.get("doi") and not ARXIV_DOI.match(a["doi"])
    if b_doi_real and not a_doi_real:
        a["doi"], a["url"] = b["doi"], b["url"]
    elif not a.get("url"):
        a["url"] = b.get("url")
    a["arxiv"] = a.get("arxiv") or b.get("arxiv")
    for field in ("openalex", "s2", "sources", "cites", "keys"):
        merged = list(dict.fromkeys((a.get(field) or []) + (b.get(field) or [])))
        a[field] = merged


class Registry:
    """A set of papers, deduplicated by any shared identifier."""

    def __init__(self):
        self.items: dict[int, dict] = {}
        self.index: dict[str, int] = {}
        self._next = 0

    def add(self, rec: dict) -> dict:
        keys = make_keys(rec.get("doi"), rec.get("arxiv"), rec.get("title"))
        rec["keys"] = sorted(keys | set(rec.get("keys") or []))
        hits = sorted({self.index[k] for k in rec["keys"] if k in self.index})
        if not hits:
            iid = self._next
            self._next += 1
            self.items[iid] = rec
        else:
            iid = hits[0]
            merge_into(self.items[iid], rec)
            for other in hits[1:]:  # the new record bridged two existing ones
                merge_into(self.items[iid], self.items.pop(other))
        for k in self.items[iid]["keys"]:
            self.index[k] = iid
        return self.items[iid]

    def values(self) -> list[dict]:
        return list(self.items.values())


# ---------------------------------------------------------------- your own papers


def slug(title: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", ascii_fold(title).lower())).strip("-")[:60]


def matches_exclude(rec: dict, excludes: list[str]) -> bool:
    t = norm_title(rec.get("title"))
    for ex in excludes:
        ex_l = ex.strip().lower()
        if ex_l in rec.get("keys", []) or ex_l in (rec.get("openalex") or []) or (
            norm_title(ex) and norm_title(ex) in t
        ):
            return True
    return False


def collect_own_papers(client: Client, cfg: dict) -> list[dict]:
    reg = Registry()
    oa_ids = [str(x).rsplit("/", 1)[-1] for x in cfg.get("openalex_author_ids") or []]
    s2_ids = [str(x) for x in cfg.get("semantic_scholar_author_ids") or []]

    if oa_ids:
        try:
            works = client.oa_works("author.id:" + "|".join(oa_ids))
            for w in works:
                if w.get("type") in SKIP_OA_TYPES:
                    continue
                rec = from_openalex(w)
                rec["sources"] = ["openalex"]
                reg.add(rec)
            log(f"OpenAlex: {len(works)} works for your author ID(s)")
        except Exception as exc:  # keep going with the other source
            log(f"WARNING: OpenAlex author lookup failed: {exc}")

    for aid in s2_ids:
        try:
            papers = client.s2_paged(f"/author/{aid}/papers", S2_PAPER_FIELDS)
            for p in papers:
                rec = from_s2(p)
                rec["sources"] = ["semanticscholar"]
                reg.add(rec)
            log(f"Semantic Scholar: {len(papers)} papers for author {aid}")
        except Exception as exc:
            log(f"WARNING: Semantic Scholar author lookup failed for {aid}: {exc}")

    for ident in cfg.get("extra_papers") or []:
        ident = str(ident).strip()
        kind, _, value = ident.partition(":")
        kind = kind.lower()
        oa_path, s2_id = None, None
        if kind == "doi":
            oa_path, s2_id = f"/works/doi:{value}", f"DOI:{value}"
        elif kind == "arxiv":
            oa_path, s2_id = f"/works/doi:10.48550/arXiv.{value}", f"ARXIV:{value}"
        else:
            log(f"WARNING: extra_papers entry '{ident}' should start with doi: or arxiv:")
            continue
        try:
            w = client.oa(oa_path)
            if w:
                rec = from_openalex(w)
                rec["sources"] = ["openalex"]
                reg.add(rec)
        except Exception as exc:
            log(f"WARNING: OpenAlex lookup failed for {ident}: {exc}")
        try:
            p = client.s2(f"/paper/{s2_id}", fields=S2_PAPER_FIELDS)
            if p:
                rec = from_s2(p)
                rec["sources"] = ["semanticscholar"]
                reg.add(rec)
        except Exception as exc:
            log(f"WARNING: Semantic Scholar lookup failed for {ident}: {exc}")

    excludes = [str(x) for x in cfg.get("exclude_papers") or []]
    papers = [p for p in reg.values() if p.get("title") and not matches_exclude(p, excludes)]
    used = set()
    for p in papers:
        pid = slug(p["title"]) or "paper"
        while pid in used:
            pid += "-x"
        used.add(pid)
        p["id"] = pid
    return papers


# ------------------------------------------------------------------- citing papers


def is_self_citation(authors: list[str], first: str, last: str) -> bool:
    last_n = ascii_fold(last).lower()
    first_n = ascii_fold(first).lower()[:1]
    for name in authors:
        parts = re.findall(r"[a-z]+", ascii_fold(name).lower())
        if parts and parts[-1] == last_n and parts[0][:1] == first_n:
            return True
    return False


def collect_citations(client: Client, own: list[dict]) -> tuple[list[dict], dict]:
    reg = Registry()
    ok = {"openalex": False, "semanticscholar": False}
    own_keys = {p["id"]: set(p["keys"]) for p in own}

    def add(rec: dict, paper: dict, source: str):
        keys = make_keys(rec.get("doi"), rec.get("arxiv"), rec.get("title"))
        if not rec.get("title") or keys & own_keys[paper["id"]]:
            return  # a version of the same paper "citing" itself
        rec["sources"] = [source]
        rec["cites"] = [paper["id"]]
        reg.add(rec)

    for paper in own:
        for wid in paper.get("openalex") or []:
            try:
                works = client.oa_works(f"cites:{wid}")
                for w in works:
                    if w.get("type") not in SKIP_OA_TYPES:
                        add(from_openalex(w), paper, "openalex")
                ok["openalex"] = True
            except Exception as exc:
                log(f"WARNING: OpenAlex citations failed for {wid}: {exc}")
        for pid in paper.get("s2") or []:
            try:
                rows = client.s2_paged(f"/paper/{pid}/citations", S2_PAPER_FIELDS)
                for row in rows:
                    cp = row.get("citingPaper") or {}
                    if cp.get("paperId"):
                        add(from_s2(cp), paper, "semanticscholar")
                ok["semanticscholar"] = True
            except Exception as exc:
                log(f"WARNING: Semantic Scholar citations failed for {pid}: {exc}")
        log(f"  {paper['title'][:70]}")
    return reg.values(), ok


# --------------------------------------------------------------- state & output


def load_previous() -> dict | None:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return None


def estimated_date(rec: dict, today: str) -> str:
    if rec.get("publication_date"):
        return rec["publication_date"][:10]
    if rec.get("year"):
        return f"{rec['year']}-01-01"
    return today


def merge_with_previous(current: list[dict], previous: dict | None, baseline: bool, today: str) -> list[dict]:
    prev_entries = (previous or {}).get("citations", [])
    prev_index: dict[str, dict] = {}
    for e in prev_entries:
        for k in e.get("keys", []):
            prev_index[k] = e

    matched_prev = set()
    out = []
    for rec in current:
        hit = next((prev_index[k] for k in rec["keys"] if k in prev_index), None)
        if hit is not None:
            matched_prev.add(id(hit))
            rec["uid"] = hit.get("uid") or hit["keys"][0]
            rec["first_seen"] = hit["first_seen"]
            rec["first_seen_estimated"] = hit.get("first_seen_estimated", False)
            rec["keys"] = sorted(set(rec["keys"]) | set(hit.get("keys", [])))
            rec["cites"] = list(dict.fromkeys(hit.get("cites", []) + rec["cites"]))
        elif baseline:
            rec["first_seen"] = estimated_date(rec, today)
            rec["first_seen_estimated"] = True
        else:
            rec["first_seen"] = today
            rec["first_seen_estimated"] = False
        rec.setdefault("uid", rec["keys"][0])
        rec["last_seen"] = today
        out.append(rec)

    # Entries a source stopped returning are kept, since sources occasionally drop
    # records temporarily. They keep their old last_seen date.
    current_keys = {k for r in out for k in r["keys"]}
    for e in prev_entries:
        if id(e) not in matched_prev and not current_keys & set(e.get("keys", [])):
            out.append(e)
    return out


def to_public(rec: dict, self_first: str, self_last: str) -> dict:
    return {
        "title": rec["title"],
        "authors": rec.get("authors") or [],
        "year": rec.get("year"),
        "publication_date": rec.get("publication_date"),
        "venue": rec.get("venue"),
        "url": rec.get("url"),
        "doi": rec.get("doi"),
        "arxiv": rec.get("arxiv"),
        "cites": rec.get("cites") or [],
        "sources": rec.get("sources") or [],
        "self_citation": is_self_citation(rec.get("authors") or [], self_first, self_last),
        "first_seen": rec["first_seen"],
        "first_seen_estimated": rec.get("first_seen_estimated", False),
        "last_seen": rec.get("last_seen"),
        "uid": rec.get("uid") or rec["keys"][0],
        "keys": rec["keys"],
    }


def write_feed(cfg: dict, data: dict) -> None:
    site = (cfg.get("site_url") or "").rstrip("/") + "/"
    papers = {p["id"]: p["title"] for p in data["papers"]}
    items = [c for c in data["citations"] if not c["first_seen_estimated"]][:50]
    updated = data["generated_at"]
    entries = []
    for c in items:
        cited = "; ".join(papers.get(pid, pid) for pid in c["cites"])
        summary = f"{', '.join(c['authors'][:6])}. {c.get('venue') or ''} {c.get('year') or ''}. Cites: {cited}"
        entries.append(
            "  <entry>\n"
            f"    <title>{xml_escape(c['title'])}</title>\n"
            f"    <link href=\"{xml_escape(c.get('url') or site)}\"/>\n"
            f"    <id>{xml_escape(site + '#' + c['uid'])}</id>\n"
            f"    <updated>{c['first_seen']}T00:00:00Z</updated>\n"
            f"    <summary>{xml_escape(summary)}</summary>\n"
            "  </entry>"
        )
    feed = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        f"  <title>New citations of {xml_escape(cfg.get('name', ''))}</title>\n"
        f'  <link href="{xml_escape(site)}"/>\n'
        f"  <id>{xml_escape(site)}</id>\n"
        f"  <updated>{updated}</updated>\n" + "\n".join(entries) + "\n</feed>\n"
    )
    FEED_FILE.write_text(feed, encoding="utf-8")


# ------------------------------------------------------------------------ modes


def find_author(client: Client, cfg: dict) -> None:
    name = cfg.get("name") or input("Author name: ")
    print(f"\nOpenAlex authors matching '{name}':")
    data = client.oa("/authors", search=name, **{"per-page": 10})
    for a in (data or {}).get("results", []):
        aid = a["id"].rsplit("/", 1)[-1]
        inst = ", ".join(i.get("display_name", "") for i in a.get("last_known_institutions") or [])
        print(f"\n  {aid}  {a.get('display_name')}  works={a.get('works_count')}  cited_by={a.get('cited_by_count')}")
        if inst:
            print(f"    institution: {inst}")
        top = client.oa("/works", filter=f"author.id:{aid}", sort="cited_by_count:desc",
                        select="display_name,publication_year", **{"per-page": 3})
        for w in (top or {}).get("results", []):
            print(f"    - {clean_text(w.get('display_name'))} ({w.get('publication_year')})")

    print(f"\nSemantic Scholar authors matching '{name}':")
    data = client.s2("/author/search", query=name, limit=10,
                     fields="name,affiliations,paperCount,citationCount,papers.title,papers.year")
    for a in (data or {}).get("data", []):
        print(f"\n  {a.get('authorId')}  {a.get('name')}  papers={a.get('paperCount')}  citations={a.get('citationCount')}")
        if a.get("affiliations"):
            print(f"    affiliations: {', '.join(a['affiliations'])}")
        for p in (a.get("papers") or [])[:3]:
            print(f"    - {p.get('title')} ({p.get('year')})")
    print("\nCopy the IDs that are yours into config.yml (several per source is fine if your profile is split).")


def update(client: Client, cfg: dict, force_baseline: bool) -> None:
    today = dt.date.today().isoformat()
    previous = None if force_baseline else load_previous()
    baseline = previous is None

    own = collect_own_papers(client, cfg)
    if not own:
        sys.exit("No papers found. Run with --find-author and fill in config.yml first.")
    log(f"Found {len(own)} of your papers. Fetching citations...")

    citing, ok = collect_citations(client, own)
    if not any(ok.values()):
        sys.exit("Both sources failed; leaving citations.json unchanged.")

    merged = merge_with_previous(citing, previous, baseline, today)
    me = cfg.get("self_name") or {}
    first, last = me.get("first", ""), me.get("last", "")
    public = [to_public(r, first, last) for r in merged]
    public.sort(key=lambda c: (c["first_seen"], c.get("publication_date") or ""), reverse=True)

    counts = {p["id"]: 0 for p in own}
    for c in public:
        for pid in c["cites"]:
            if pid in counts:
                counts[pid] += 1

    data = {
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "tracking_since": (previous or {}).get("tracking_since") or today,
        "name": cfg.get("name"),
        "self_name": {"first": first, "last": last},
        "scholar_url": cfg.get("scholar_url"),
        "papers": sorted(
            [
                {"id": p["id"], "title": p["title"], "year": p.get("year"), "url": p.get("url"),
                 "citation_count": counts[p["id"]]}
                for p in own
            ],
            key=lambda p: (-(p["year"] or 0), p["title"]),
        ),
        "citations": public,
    }
    DATA_FILE.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    write_feed(cfg, data)

    new = sum(1 for c in public if c["first_seen"] == today and not c["first_seen_estimated"])
    log(f"Done: {len(public)} citing papers ({new} new today). API calls: {client.calls}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--find-author", action="store_true", help="list candidate author IDs and exit")
    ap.add_argument("--baseline", action="store_true", help="ignore existing citations.json and start over")
    args = ap.parse_args()

    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    oa_key = os.environ.get("OPENALEX_API_KEY") or None
    if not oa_key:
        log("WARNING: OPENALEX_API_KEY is not set; OpenAlex allows only a few test requests without it.")
    client = Client(oa_key, os.environ.get("S2_API_KEY") or None, cfg.get("contact_email"))

    if args.find_author:
        find_author(client, cfg)
    else:
        update(client, cfg, args.baseline)


if __name__ == "__main__":
    main()
