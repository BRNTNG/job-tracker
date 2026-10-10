#!/usr/bin/env python3
"""Job posting tracker: polls LinkedIn (guest), Job Bank (federal), and
We Work Remotely RSS for junior BA/PM roles, stores them in SQLite,
reports new postings, and renders dashboard.html.

Usage:
    python3 tracker.py            # fetch + store + print JSON summary + render
    python3 tracker.py --render   # only re-render the dashboard
"""
import html as htmllib
import json
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "jobs.db")
DASHBOARD = os.path.join(BASE, "dashboard.html")
CONFIG = os.path.join(BASE, "config.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Keep only listings in Toronto / GTA for location-based searches.
# Explicit GTA municipalities and regions only - no bare "ontario"/"canada",
# which let non-GTA Ontario jobs (e.g. Cambridge) through. Work-arrangement
# words (remote/hybrid/work from home) are NOT geography: they used to make
# any "Hybrid in <US city>" listing pass this filter. Remote eligibility is
# decided separately in remote_in_scope().
AREA_PAT = re.compile(
    r"toronto|greater toronto|\bgta\b|scarborough|etobicoke|north york|east york|"
    r"mississauga|brampton|caledon|vaughan|markham|richmond hill|oakville|"
    r"burlington|milton|halton hills|ajax|pickering|whitby|oshawa|clarington|"
    r"newmarket|\baurora\b|king city|whitchurch|stouffville|east gwillimbury|"
    r"georgina|\buxbridge\b|scugog|\bbrock\b|\bmaple\b|\bconcord\b|thornhill|"
    r"woodbridge|bolton|georgetown|acton|nobleton|"
    r"york region|peel region|durham region|halton region", re.I)


def in_target_area(location):
    return bool(AREA_PAT.search(location or ""))


def remote_in_scope(location):
    # Remote queries should only keep genuinely remote postings (or GTA ones,
    # which are in scope anyway). LinkedIn's remote filter also returns plain
    # on-site jobs elsewhere in Canada.
    ll = (location or "").lower()
    return "remote" in ll or "work from home" in ll or in_target_area(location)


# Titles that signal a non-junior role get filtered out of results.
SENIOR_PAT = re.compile(
    r"\b(senior|sr\.?|director|vice president|\bvp\b|principal|staff|"
    r"head of|chief|lead|architect|manager of|10\+|7\+|5\+)\b", re.I)
INTERN_PAT = re.compile(r"\bintern(ship)?\b", re.I)


def is_junior_friendly(title):
    t = " " + title.lower() + " "
    # "project manager" is fine; "manager of projects" is not
    if INTERN_PAT.search(title):
        return False
    # don't nuke "project manager" via the generic "manager" word
    tmp = re.sub(r"\bproject manager\b", "projectmanager", t, flags=re.I)
    tmp = re.sub(r"\bprogram manager\b", "programmanager", tmp, flags=re.I)
    tmp = re.sub(r"\bproduct manager\b", "productmanager", tmp, flags=re.I)
    return not SENIOR_PAT.search(tmp)


# Role-family categories derived from the search query / title, since the
# sources don't expose a true "industry" field.
CATEGORY_RULES = [
    ("Business Analysis", ["business analyst", "business systems", "technical business"]),
    ("Project Management", ["project coordinator", "project manager", "project analyst", "digital project"]),
    ("Product Management", ["product analyst", "product manager", "product owner"]),
    ("PMO", ["pmo"]),
    ("Implementation", ["implementation"]),
    ("Quality Assurance", ["qa analyst", "quality assurance", "analyst tester"]),
    ("Agile / Scrum", ["scrum"]),
    ("Operations", ["operations analyst", "operations coordinator", "delivery manager"]),
    ("Data & Analytics", ["data analyst"]),
    ("CRM / Salesforce", ["salesforce", "crm"]),
]


def category_for(query, title):
    text = f"{query or ''} [remote] {title or ''}".lower().replace("-", " ")
    # strip the remote marker from the query so it doesn't interfere
    text = text.replace("[remote]", " ")
    for name, keys in CATEGORY_RULES:
        if any(k in text for k in keys):
            return name
    return "Other"


def norm_key(title, company):
    """Normalized (title, company) key for cross-source duplicate detection."""
    def norm(s):
        s = re.sub(r"[^a-z0-9 ]", "", (s or "").lower())
        return re.sub(r"\s+", " ", s).strip()
    return norm(title), norm(company)


def parse_salary(s):
    """Parse a Job Bank salary string -> (min_annual_cad, max_annual_cad)."""
    if not s:
        return (None, None)
    nums = [float(n.replace(",", "")) for n in re.findall(r"\$?([\d,]+\.?\d*)", s)]
    if not nums:
        return (None, None)
    lo = nums[0]
    hi = nums[1] if len(nums) > 1 else nums[0]
    if re.search(r"hour", s, re.I):
        lo, hi = lo * 2080, hi * 2080
    return (int(round(lo)), int(round(hi)))


def http_get(url, timeout=25):
    req = Request(url, headers={"User-Agent": UA,
                                "Accept": "text/html,application/rss+xml,application/xml;q=0.9,*/*;q=0.8"})
    with urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


# ---------------- LinkedIn (public guest search, no login) ----------------
def fetch_linkedin(query, location, remote=False, days=7, pages=2, area_filter=False):
    out = []
    params = {"keywords": query, "location": location,
              "f_TPR": f"r{days * 86400}", "f_E": "2,3", "sortBy": "DD"}
    if remote:
        params["f_WT"] = "2"
    for page in range(pages):
        url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
               + urlencode(params) + f"&start={page * 25}")
        try:
            page_html = http_get(url)
        except Exception:
            break
        cards = re.split(r"<li[^>]*>", page_html)
        if len(cards) < 2:
            break
        for card in cards[1:]:
            m = re.search(r'href="(https://[^"]*?/jobs/view/[^"]*?(\d{6,})[^"]*)"', card)
            if not m:
                continue
            job_url, job_id = htmllib.unescape(m.group(1)).split("?")[0], m.group(2)
            title = re.search(r'base-search-card__title[^>]*>\s*([^<]+)', card)
            sub = re.search(r'base-search-card__subtitle.*?</h4>', card, re.S)
            company = ""
            if sub:
                cm = re.search(r'<a[^>]*>\s*([^<]+?)\s*</a>', sub.group(0), re.S)
                if cm:
                    company = cm.group(1)
                else:
                    company = re.sub(r"<[^>]+>", "", sub.group(0))
            loc = re.search(r'job-search-card__location[^>]*>\s*([^<]+)', card)
            dt = re.search(r'<time[^>]*datetime="([^"]+)"', card)
            location_txt = htmllib.unescape(loc.group(1)).strip() if loc else ""
            if area_filter and not in_target_area(location_txt):
                continue
            if remote and not remote_in_scope(location_txt):
                continue
            out.append({
                "source": "linkedin", "ext_id": job_id,
                "title": htmllib.unescape(title.group(1)).strip() if title else "",
                "company": htmllib.unescape(company).strip(),
                "location": location_txt,
                "url": job_url, "posted": dt.group(1) if dt else "",
                "query": query + (" [remote]" if remote else ""),
            })
        time.sleep(2)
    return out


# ---------------- Job Bank (federal Atom feed, no auth) ----------------
def fetch_jobbank(searchstring, locationstring, area_filter=False):
    url = ("https://www.jobbank.gc.ca/jobsearch/feed/jobSearchRSSfeed?"
           + urlencode({"searchstring": searchstring, "locationstring": locationstring}))
    try:
        xml = http_get(url)
    except Exception:
        return []
    time.sleep(5)  # honor Job Bank robots.txt crawl-delay
    out = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("a:entry", ns):
        eid = (entry.findtext("a:id", default="", namespaces=ns) or "")
        m = re.search(r"id=(\d+)", eid)
        ext_id = m.group(1) if m else eid
        title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
        link = ""
        for l in entry.findall("a:link", ns):
            if l.get("rel") == "alternate":
                link = l.get("href", "")
        updated = (entry.findtext("a:updated", default="", namespaces=ns) or "")[:10]
        summary = (entry.findtext("a:summary", default="", namespaces=ns) or "")
        loc = re.search(r"<strong>Location:</strong>\s*([^<]+)", summary)
        emp = re.search(r"<strong>Employer:</strong>\s*([^<]+)", summary)
        sal = re.search(r"<strong>Salary:</strong>\s*([^<]+)", summary)
        location_txt = htmllib.unescape(loc.group(1)).strip() if loc else ""
        if area_filter and not in_target_area(location_txt):
            continue
        out.append({
            "source": "jobbank", "ext_id": ext_id,
            "title": htmllib.unescape(title),
            "company": htmllib.unescape(emp.group(1)).strip() if emp else "",
            "location": location_txt,
            "url": link, "posted": updated,
            "query": searchstring,
            "salary": htmllib.unescape(sal.group(1)).strip() if sal else "",
        })
    return out


# ---------------- We Work Remotely (remote roles) ----------------
WWR_PHRASES = ["business analyst", "project manager", "project coordinator",
               "product manager", "program manager", "implementation",
               "scrum master", "business systems", "operations analyst",
               "delivery manager", "product owner", "operations coordinator",
               "pmo", "qa analyst", "quality assurance", "salesforce",
               "data analyst", "technical business analyst", "project analyst"]


def fetch_wwr():
    try:
        xml = http_get("https://weworkremotely.com/remote-jobs.rss")
    except Exception:
        return []
    out = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        tl = title.lower()
        if not any(p in tl for p in WWR_PHRASES):
            continue
        company, _, role = title.partition(":")
        role = role.strip() or title
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        ext_id = re.sub(r"\D", "", link)[-12:] or link
        posted = ""
        try:
            posted = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").strftime("%Y-%m-%d")
        except Exception:
            pass
        out.append({"source": "wwr", "ext_id": ext_id, "title": role,
                    "company": company.strip(), "location": "Remote",
                    "url": link, "posted": posted, "query": "remote"})
    return out


# ---------------- Dice (tech/analyst roles, Toronto) ----------------
def parse_dice_salary(s):
    if not s or "$" not in s:
        return (None, None)
    nums = [float(n.replace(",", "")) for n in
            re.findall(r"\$?\s*(\d[\d,]*(?:\.\d+)?)", s) if n.replace(",", "")]
    if not nums:
        return (None, None)
    isk = "k" in s.lower()
    vals = [n * 1000 if isk else n for n in nums]
    lo = vals[0]
    hi = vals[1] if len(vals) > 1 else vals[0]
    if max(vals) < 1000 and not re.search(r"hour|annual|year", s, re.I):
        lo, hi = lo * 2080, hi * 2080  # bare $65-$75 on Dice is an hourly contract rate
    return (int(round(lo)), int(round(hi)))


def dice_in_scope(loc):
    # Dice is US-centric: its "Remote" listings are US staffing roles, not
    # Canada-remote. Keep only genuinely GTA-located postings.
    ll = (loc or "").lower()
    if "remote" in ll:
        return False
    return in_target_area(loc)


def dice_posted(text):
    today = datetime.now().date()
    t = (text or "").lower()
    if "just posted" in t or t.strip() == "today":
        return today.isoformat()
    if "yesterday" in t:
        return (today - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\s*([dhm])", t)
    if m:
        n, u = int(m.group(1)), m.group(2)
        days = n if u == "d" else 1
        return (today - timedelta(days=days)).isoformat()
    return ""


DICE_ROLE_PAT = re.compile(
    r"analyst|analytics|manager|coordinator|engineer|developer|writer|"
    r"specialist|architect|consultant|owner|administrator|designer|scientist|"
    r"technician|programmer|tester", re.I)


def dice_title_company(first, second):
    """Dice cards do not keep a stable title/company text order.

    Some cards yield (title, company), others (company, title). If only the
    second fragment looks like a job title, treat the pair as swapped.
    """
    if DICE_ROLE_PAT.search(second or "") and not DICE_ROLE_PAT.search(first or ""):
        return second, first
    return first, second


def fetch_dice(queries, location="Toronto, ON, Canada", pages=2):
    out = []
    for q in queries:
        for page in range(1, pages + 1):
            url = ("https://www.dice.com/jobs?"
                   + urlencode({"q": q, "location": location, "page": page, "pageSize": 40}))
            try:
                page_html = http_get(url)
            except Exception:
                break
            items = re.split(r'role="listitem"', page_html)
            if len(items) < 2:
                break
            for it in items[1:]:
                m = re.search(r'href="(/job-detail/[0-9a-f-]{36})"', it)
                if not m:
                    continue
                job_url = "https://www.dice.com" + m.group(1)
                guid = re.search(r'data-job-guid="([0-9a-f-]{36})"', it)
                ext_id = guid.group(1) if guid else m.group(1).rsplit("/", 1)[-1]
                txt = htmllib.unescape(re.sub(r"<[^>]+>", "|", it))
                parts = [p.strip() for p in txt.split("|") if p.strip() and p.strip() != ">"]
                if len(parts) < 3:
                    continue
                title, company = dice_title_company(parts[0], parts[1])
                loc = parts[2]
                if not dice_in_scope(loc):
                    continue
                posted = ""
                pm = re.search(r"(just posted|today|yesterday|\d+\s*[dhm]\s*\+?\s*ago)",
                               " ".join(parts), re.I)
                if pm:
                    posted = dice_posted(pm.group(1))
                salary, smin, smax = "", None, None
                for p in reversed(parts):
                    if "$" in p:
                        salary, (smin, smax) = p, parse_dice_salary(p)
                        break
                out.append({
                    "source": "dice", "ext_id": ext_id, "title": title,
                    "company": company, "location": loc, "url": job_url,
                    "posted": posted, "query": q, "salary": salary,
                    "salary_min": smin, "salary_max": smax,
                })
            time.sleep(2)
    return out


# ---------------- storage ----------------
def init_db():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS listings (
        source TEXT, ext_id TEXT, title TEXT, company TEXT, location TEXT,
        url TEXT, posted TEXT, first_seen TEXT, last_seen TEXT,
        query TEXT, salary TEXT, category TEXT, salary_min INTEGER, salary_max INTEGER,
        ntitle TEXT, ncompany TEXT,
        PRIMARY KEY (source, ext_id))""")
    cols = [r[1] for r in con.execute("PRAGMA table_info(listings)").fetchall()]
    for name, typ in (("category", "TEXT"), ("salary_min", "INTEGER"), ("salary_max", "INTEGER"),
                      ("ntitle", "TEXT"), ("ncompany", "TEXT")):
        if name not in cols:
            con.execute(f"ALTER TABLE listings ADD COLUMN {name} {typ}")
    con.commit()
    return con


SEED = os.path.join(BASE, "seed.json")
SEED_COLS = ["source", "ext_id", "title", "company", "location", "url", "posted",
             "first_seen", "last_seen", "query", "salary", "category",
             "salary_min", "salary_max", "ntitle", "ncompany"]


def import_seed(con):
    """One-time bootstrap: load seed.json (list of row lists) if DB is empty."""
    if not os.path.exists(SEED):
        return 0
    n = con.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    if n:
        return 0
    rows = json.load(open(SEED))
    con.executemany(
        "INSERT OR IGNORE INTO listings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [tuple(r) for r in rows])
    con.commit()
    return len(rows)


def dedupe_existing(con):
    """Merge cross-source duplicates already stored: keep earliest first_seen."""
    rows = con.execute(
        "SELECT rowid, source, ext_id, first_seen FROM listings "
        "WHERE ntitle IS NOT NULL AND ncompany IS NOT NULL "
        "ORDER BY first_seen ASC").fetchall()
    seen, removed = {}, 0
    for rowid, source, ext_id, first_seen in rows:
        r = con.execute("SELECT ntitle, ncompany FROM listings WHERE rowid=?",
                        (rowid,)).fetchone()
        key = (r[0], r[1])
        if key in seen:
            con.execute("DELETE FROM listings WHERE rowid=?", (rowid,))
            removed += 1
        else:
            seen[key] = rowid
    con.commit()
    return removed


def backfill(con):
    """Derive category/salary bounds/normalized keys for rows stored before those columns existed."""
    rows = con.execute(
        "SELECT rowid, title, company, query, salary FROM listings WHERE category IS NULL").fetchall()
    for rowid, title, company, query, salary in rows:
        smin, smax = parse_salary(salary or "")
        nt, nc = norm_key(title, company)
        con.execute(
            "UPDATE listings SET category=?, salary_min=?, salary_max=?, ntitle=?, ncompany=? WHERE rowid=?",
            (category_for(query, title), smin, smax, nt, nc, rowid))
    con.commit()
    return len(rows)


def store(con, records):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat(timespec="seconds")
    new, dupes = [], 0
    for r in records:
        if not r["title"] or not is_junior_friendly(r["title"]):
            continue
        cur = con.execute("SELECT 1 FROM listings WHERE source=? AND ext_id=?",
                          (r["source"], r["ext_id"])).fetchone()
        if cur:
            con.execute("UPDATE listings SET last_seen=? WHERE source=? AND ext_id=?",
                        (now, r["source"], r["ext_id"]))
            continue
        nt, nc = norm_key(r["title"], r["company"])
        # cross-source duplicate: same normalized title+company seen recently
        other = con.execute(
            "SELECT source FROM listings WHERE ntitle=? AND ncompany=? AND last_seen>=?",
            (nt, nc, cutoff)).fetchone()
        if other:
            con.execute(
                "UPDATE listings SET last_seen=? WHERE ntitle=? AND ncompany=? AND last_seen>=?",
                (now, nt, nc, cutoff))
            dupes += 1
            continue
        smin = r.get("salary_min")
        smax = r.get("salary_max")
        if smin is None and smax is None:
            smin, smax = parse_salary(r.get("salary", ""))
        cat = r.get("category") or category_for(r.get("query", ""), r["title"])
        con.execute(
            "INSERT INTO listings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["source"], r["ext_id"], r["title"], r["company"], r["location"],
             r["url"], r.get("posted", ""), now, now,
             r.get("query", ""), r.get("salary", ""), cat, smin, smax, nt, nc))
        new.append(r)
    con.commit()
    return new, dupes


# ---------------- dashboard ----------------
MANUAL_LINKS = [
    ("Indeed", "https://ca.indeed.com/jobs?q=business+analyst&l=Toronto%2C+ON&sort=date",
     "Biggest aggregator - blocks automated checks, best visited directly"),
    ("GC Jobs (federal government)", "https://emploisfp-psjobs.cfp-psc.gc.ca/psrs-srfp/applicant/page2440",
     "Official Government of Canada hiring portal - set up a job alert account here"),
    ("Eluta", "https://www.eluta.ca/search?q=business+analyst&l=Toronto",
     "Canadian new-job listings, updated daily"),
    ("Glassdoor", "https://www.glassdoor.ca/Job/toronto-business-analyst-jobs-SRCH_IL.0,7_IC2281069_KO8,24.htm",
     "Jobs + salary/company reviews"),
    ("SimplyHired", "https://www.simplyhired.ca/search?q=business+analyst&l=Toronto%2C+ON",
     "Big Canadian aggregator - blocks automated checks like Indeed, best visited directly"),
]

SOURCE_LABEL = {"linkedin": "LinkedIn", "jobbank": "Job Bank", "wwr": "We Work Remotely",
                "dice": "Dice"}


def render():
    con = init_db()
    rows = con.execute(
        "SELECT source, ext_id, title, company, location, url, posted, first_seen, query, salary, "
        "category, salary_min, salary_max "
        "FROM listings ORDER BY posted DESC, first_seen DESC LIMIT 400").fetchall()
    con.close()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cards = []
    for (source, ext_id, title, company, location, url, posted, first_seen, query, salary,
         category, salary_min, salary_max) in rows:
        is_new = False
        try:
            fs = datetime.fromisoformat(first_seen)
            is_new = (datetime.now(timezone.utc) - fs).total_seconds() < 48 * 3600
        except Exception:
            pass
        cards.append({
            "source": source, "label": SOURCE_LABEL.get(source, source),
            "title": title, "company": company, "location": location,
            "url": url, "posted": posted or "n/a", "query": query,
            "salary": salary or "", "is_new": is_new,
            "category": category or "Other",
            "salary_min": salary_min, "salary_max": salary_max,
        })
    manual = [{"name": n, "url": u, "note": d} for n, u, d in MANUAL_LINKS]
    data_json = json.dumps({"updated": now, "listings": cards, "manual": manual})
    page = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Business analyst, project management &amp; related roles &mdash; Toronto / GTA + remote Canada. Updated every few hours.">
<meta property="og:title" content="Job Tracker - BA / PM roles">
<meta property="og:description" content="Business analyst, project management &amp; related roles &mdash; Toronto / GTA + remote Canada. Updated every few hours.">
<meta property="og:type" content="website">
<meta property="og:url" content="https://brntng.github.io/job-tracker/">
<title>Job Tracker - BA / PM roles</title>
<style>
*{box-sizing:border-box}body{font-family:-apple-system,system-ui,Segoe UI,Roboto,sans-serif;margin:0;background:#f4f6f8;color:#1c1e21}
header{background:#0d2d5e;color:#fff;padding:22px 20px}header h1{margin:0 0 4px;font-size:22px}header p{margin:0;opacity:.85;font-size:14px}
.wrap{max-width:980px;margin:0 auto;padding:16px}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0;align-items:center}
.chip{border:1px solid #ccd4dd;background:#fff;border-radius:999px;padding:7px 14px;font-size:13px;cursor:pointer}
.chip.active{background:#0d2d5e;color:#fff;border-color:#0d2d5e}
#q{flex:1;min-width:180px;padding:8px 12px;border:1px solid #ccd4dd;border-radius:8px;font-size:14px}
select.f{padding:8px 10px;border:1px solid #ccd4dd;border-radius:8px;font-size:13px;background:#fff;max-width:170px}
.star{background:none;border:none;font-size:22px;cursor:pointer;color:#c3cad4;padding:0;line-height:1}
.star.on{color:#f5a623}
.appliedbtn{background:none;border:1px solid #ccd4dd;border-radius:999px;font-size:12px;cursor:pointer;color:#5b6470;padding:5px 12px;white-space:nowrap}
.appliedbtn.on{background:#137333;border-color:#137333;color:#fff}
.tag.applied{background:#e6f4ea;color:#137333}
.notintbtn{background:none;border:1px solid #ccd4dd;border-radius:999px;font-size:12px;cursor:pointer;color:#5b6470;padding:5px 12px;white-space:nowrap}
.notintbtn.on{background:#c62828;border-color:#c62828;color:#fff}
.tag.notint{background:#fdecea;color:#c62828}
.cardbtns{display:flex;align-items:center;gap:6px;flex-shrink:0}
.cardhead{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
.cattag{display:inline-block;font-size:11px;background:#eef1f5;color:#5b6470;border-radius:4px;padding:2px 8px;margin-left:6px;vertical-align:middle;font-weight:600}
.card{background:#fff;border-radius:10px;padding:14px 16px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.card h3{margin:0 0 4px;font-size:16px}.card h3 a{color:#0d2d5e;text-decoration:none}.card h3 a:hover{text-decoration:underline}
.meta{font-size:13px;color:#5b6470;margin:2px 0}.tag{display:inline-block;font-size:11px;font-weight:700;border-radius:4px;padding:2px 8px;margin-right:6px;vertical-align:middle}
.tag.linkedin{background:#e8f0fe;color:#0a66c2}.tag.jobbank{background:#e6f4ea;color:#137333}.tag.wwr{background:#f3e8fd;color:#7b1fa2}.tag.dice{background:#e3f2fd;color:#0d47a1}
.new{background:#d32f2f;color:#fff}.count{font-size:13px;color:#5b6470;margin:10px 0}
.manual{background:#fffbe8;border:1px solid #f0d878;border-radius:10px;padding:14px 16px;margin-top:18px}
.manual h3{margin:0 0 8px;font-size:15px}.manual a{color:#0d2d5e;font-weight:600}.manual p{font-size:13px;margin:4px 0 10px;color:#5b6470}
footer{font-size:12px;color:#8a94a0;text-align:center;padding:20px}
</style></head><body>
<header><h1>Job Tracker</h1><p>Business analyst, project management &amp; related roles &mdash; Toronto / GTA + remote Canada. Auto-checked every few hours.</p></header>
<div class="wrap">
<div class="toolbar">
<button class="chip active" data-f="all">All</button>
<button class="chip" data-f="linkedin">LinkedIn</button>
<button class="chip" data-f="jobbank">Job Bank</button>
<button class="chip" data-f="dice">Dice</button>
<button class="chip" data-f="wwr">Remote</button>
<button class="chip" id="savedChip">&#9734; Saved</button>
<button class="chip" id="appliedChip">Hide applied</button>
<button class="chip" id="notintChip">Hide not interested</button>
<select class="f" id="cat"><option value="all">All categories</option></select>
<select class="f" id="sal"><option value="0">Any salary</option><option value="40000">$40k+</option><option value="50000">$50k+</option><option value="60000">$60k+</option><option value="75000">$75k+</option><option value="100000">$100k+</option></select>
<select class="f" id="age"><option value="0">Any time</option><option value="7">Last 7 days</option><option value="14">Last 14 days</option><option value="30">Last 30 days</option></select>
<input id="q" type="search" placeholder="Filter by keyword, company...">
</div>
<div class="count" id="count"></div>
<div id="list"></div>
<div class="manual"><h3>Sites to check manually</h3><div id="manual"></div>
<p style="margin-top:10px">Tip: on Indeed and GC Jobs, create a free job alert with the same keywords - they email new matches daily.</p></div>
</div>
<footer id="upd"></footer>
<script>
const DATA = __DATA__;
let f="all", kw="", fcat="all", fsal=0, fage=0, savedOnly=false, hideApplied=false, hideNotInt=false;
const bookmarks = new Set(JSON.parse(localStorage.getItem("jt_bookmarks")||"[]"));
const applied = new Set(JSON.parse(localStorage.getItem("jt_applied")||"[]"));
const notint = new Set(JSON.parse(localStorage.getItem("jt_notinterested")||"[]"));
function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}
function salLabel(c){
 if(!c.salary_max) return c.salary ? c.salary : "";
 const k=v=>"$"+(Math.round(v/100)/10)+"k";
 return k(c.salary_min)+(c.salary_min!==c.salary_max?"&ndash;"+k(c.salary_max):"")+"/yr";
}
function daysOld(c){
 const d=new Date(c.posted);
 if(isNaN(d)) return null;
 return (Date.now()-d.getTime())/86400000;
}
function draw(){
 const items = DATA.listings.filter(c =>
   (f==="all"||c.source===f) &&
   (fcat==="all"||c.category===fcat) &&
   (!fsal||!c.salary_max||c.salary_max>=fsal) &&
   (!fage||daysOld(c)===null||daysOld(c)<=fage) &&
   (!savedOnly||bookmarks.has(c.url)) &&
   (!hideApplied||!applied.has(c.url)) &&
   (!hideNotInt||!notint.has(c.url)) &&
   (!kw || (c.title+" "+c.company+" "+c.location+" "+(c.category||"")).toLowerCase().includes(kw)));
 document.getElementById("count").textContent = items.length + " postings shown" + (savedOnly?" (saved)":"");
 document.getElementById("list").innerHTML = items.map(c =>
  `<div class="card"><div class="cardhead"><h3><a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.title)}</a>
   ${c.is_new?'<span class="tag new">NEW</span>':""}${applied.has(c.url)?'<span class="tag applied">APPLIED</span>':""}${notint.has(c.url)?'<span class="tag notint">NOT INTERESTED</span>':""}<span class="cattag">${esc(c.category||"")}</span></h3>
   <div class="cardbtns"><button class="notintbtn${notint.has(c.url)?" on":""}" data-url="${esc(c.url)}" title="Mark as not interested">&#10005; Not interested</button><button class="appliedbtn${applied.has(c.url)?" on":""}" data-url="${esc(c.url)}" title="Mark as applied">${applied.has(c.url)?"&#10003; Applied":"Mark applied"}</button><button class="star${bookmarks.has(c.url)?" on":""}" data-url="${esc(c.url)}" title="Save this job">${bookmarks.has(c.url)?"&#9733;":"&#9734;"}</button></div></div>
   <div class="meta"><span class="tag ${esc(c.source)}">${esc(c.label)}</span><strong>${esc(c.company||"")}</strong></div>
   <div class="meta">${esc(c.location||"")}${salLabel(c)?" &middot; "+salLabel(c):""} &middot; posted ${esc(c.posted)}</div></div>`).join("")
 || '<p class="meta">No postings match.</p>';
 document.getElementById("manual").innerHTML = DATA.manual.map(m =>
  `<div><a href="${esc(m.url)}" target="_blank" rel="noopener">${esc(m.name)}</a><p>${esc(m.note)}</p></div>`).join("");
 const d = new Date(DATA.updated);
 document.getElementById("upd").textContent = "Last checked " + d.toLocaleString();
}
document.querySelectorAll(".chip[data-f]").forEach(b=>b.onclick=()=>{document.querySelectorAll(".chip[data-f]").forEach(x=>x.classList.remove("active"));b.classList.add("active");f=b.dataset.f;draw()});
document.getElementById("savedChip").onclick=e=>{savedOnly=!savedOnly;e.target.classList.toggle("active",savedOnly);draw()};
document.getElementById("appliedChip").onclick=e=>{hideApplied=!hideApplied;e.target.classList.toggle("active",hideApplied);draw()};
document.getElementById("notintChip").onclick=e=>{hideNotInt=!hideNotInt;e.target.classList.toggle("active",hideNotInt);draw()};
document.getElementById("cat").onchange=e=>{fcat=e.target.value;draw()};
document.getElementById("sal").onchange=e=>{fsal=parseInt(e.target.value,10);draw()};
document.getElementById("age").onchange=e=>{fage=parseInt(e.target.value,10);draw()};
document.getElementById("q").oninput=e=>{kw=e.target.value.toLowerCase();draw()};
document.getElementById("list").onclick=e=>{
 const s=e.target.closest(".star");
 if(s){const u=s.dataset.url;if(bookmarks.has(u))bookmarks.delete(u);else bookmarks.add(u);localStorage.setItem("jt_bookmarks",JSON.stringify([...bookmarks]));draw();return;}
 const a=e.target.closest(".appliedbtn");
 if(a){const u=a.dataset.url;if(applied.has(u))applied.delete(u);else applied.add(u);localStorage.setItem("jt_applied",JSON.stringify([...applied]));draw();return;}
 const n=e.target.closest(".notintbtn");
 if(n){const u=n.dataset.url;if(notint.has(u))notint.delete(u);else notint.add(u);localStorage.setItem("jt_notinterested",JSON.stringify([...notint]));draw();}
};;
[...new Set(DATA.listings.map(c=>c.category).filter(Boolean))].sort().forEach(c=>{
 const o=document.createElement("option");o.value=c;o.textContent=c;document.getElementById("cat").appendChild(o);
});
draw();
</script></body></html>"""
    page = page.replace("__DATA__", data_json)
    with open(DASHBOARD, "w") as f:
        f.write(page)


def prune_old(con, days=90):
    # Drop listings older than `days` (by posted date, else first-seen date).
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    cur = con.execute(
        "DELETE FROM listings WHERE (posted != '' AND posted < ?)"
        " OR (posted = '' AND substr(first_seen,1,10) < ?)", (cutoff, cutoff))
    return cur.rowcount


def main():
    if "--render" in sys.argv:
        render()
        print(json.dumps({"rendered": DASHBOARD}))
        return
    if os.path.exists(CONFIG):
        cfg = json.load(open(CONFIG))
    else:
        cfg = EMBEDDED_CONFIG
    con = init_db()
    if "--dedupe" in sys.argv:
        backfilled = backfill(con)
        removed = dedupe_existing(con)
        con.close()
        render()
        print(json.dumps({"backfilled": backfilled, "seeded": seeded, "deduped": removed}))
        return
    backfilled = backfill(con)
    seeded = import_seed(con)
    records = []
    for q in cfg.get("linkedin", []):
        remote = q.get("remote", False)
        records += fetch_linkedin(q["query"], q["location"], remote=remote,
                                  area_filter=not remote)
    for q in cfg.get("jobbank", []):
        records += fetch_jobbank(q["searchstring"], q["locationstring"], area_filter=True)
    records += fetch_wwr()
    records += fetch_dice([q["query"] for q in cfg.get("dice", [])])
    new, dupes = store(con, records)
    pruned = prune_old(con, days=90)
    con.close()
    render()
    if os.environ.get("JT_NO_PUBLISH"):
        publish_result = "skipped (JT_NO_PUBLISH set)"
    else:
        try:
            import subprocess
            pub = subprocess.run([sys.executable, os.path.join(BASE, "publish.py")],
                                 capture_output=True, timeout=120, text=True)
            publish_result = pub.stdout.strip() or pub.stderr.strip()
        except Exception as exc:
            publish_result = f"publish skipped: {exc}"
    if os.environ.get("JT_BUILD_PAGE"):
        page_html = build_page()
        with open(os.path.join(BASE, "index.html"), "w", encoding="utf-8") as _fh:
            _fh.write(page_html)
    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fetched": len(records),
        "new_count": len(new),
        "duplicates_skipped": dupes,
        "backfilled": backfilled,
        "pruned": pruned,
        "publish": publish_result,
        "new": [{"title": r["title"], "company": r["company"], "location": r["location"],
                 "url": r["url"], "source": SOURCE_LABEL.get(r["source"], r["source"]),
                 "posted": r.get("posted", "")} for r in new],
    }
    print(json.dumps(summary, indent=1))


DASHBOARD = os.path.join(BASE, "dashboard.html")
MAX_LISTINGS = 200
MAX_CONTENT_CHARS = 95000
TEMPLATE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Business analyst, project management & related roles - Toronto / GTA + remote Canada. Updated every few hours.">
<meta property="og:title" content="Job Tracker - BA / PM roles">
<meta property="og:type" content="website">
<meta property="og:url" content="https://brntng.github.io/job-tracker/">
<title>Job Tracker - BA / PM roles</title>
<style>
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;margin:0;background:#fbfbf9;color:#111;-webkit-font-smoothing:antialiased}
header{max-width:760px;margin:0 auto;padding:56px 24px 8px}
header .eyebrow{font-size:13px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:#6b6b66;margin:0 0 14px}
header h1{margin:0;font-size:42px;line-height:1.08;font-weight:650;letter-spacing:-.02em}
header p{margin:14px 0 0;font-size:17px;line-height:1.5;color:#5c5c57;max-width:560px}
.wrap{max-width:760px;margin:0 auto;padding:24px 24px 48px}
.search{position:sticky;top:0;background:#fbfbf9;padding:14px 0 10px;z-index:5}
#q{width:100%;padding:16px 20px;border:1px solid #e3e3dc;border-radius:999px;font-size:16px;background:#fff;color:#111;outline:none}
#q:focus{border-color:#111}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 4px;align-items:center}
.chip{border:1px solid #e3e3dc;background:#fff;border-radius:999px;padding:9px 16px;font-size:14px;cursor:pointer;color:#333}
.chip.active{background:#111;color:#fff;border-color:#111}
select.f{padding:9px 14px;border:1px solid #e3e3dc;border-radius:999px;font-size:14px;background:#fff;color:#333;max-width:190px}
.count{font-size:14px;color:#8a8a83;margin:22px 2px 14px}
.card{background:#fff;border:1px solid #e8e8e1;border-radius:20px;padding:22px;margin-bottom:12px}
.card:hover{border-color:#cfcfc6}
.card h3{margin:0;font-size:18px;line-height:1.35;font-weight:600;letter-spacing:-.01em}
.card h3 a{color:#111;text-decoration:none}
.cardbtns{display:flex;align-items:center;gap:6px;flex-shrink:0;margin-left:12px}
.cardhead{display:flex;justify-content:space-between;align-items:flex-start}
.star{background:none;border:none;font-size:20px;cursor:pointer;color:#c9c9c0;padding:2px;line-height:1}
.star.on{color:#111}
.appliedbtn,.notintbtn{background:#fff;border:1px solid #e3e3dc;border-radius:999px;font-size:12.5px;cursor:pointer;color:#5c5c57;padding:7px 12px;white-space:nowrap}
.appliedbtn.on{background:#111;border-color:#111;color:#fff}
.notintbtn.on{background:#fff;border-color:#111;color:#111;font-weight:600}
.meta{font-size:14px;color:#6b6b66;margin:8px 0 0;line-height:1.45}
.meta strong{color:#111;font-weight:600}
.tag{display:inline-block;font-size:11px;font-weight:650;letter-spacing:.05em;text-transform:uppercase;border-radius:999px;padding:3px 9px;margin-right:6px;vertical-align:middle;background:#f1f1ec;color:#6b6b66}
.tag.new{background:#111;color:#fff}
.tag.applied{background:#e7f4e4;color:#2e6b24}
.tag.notint{background:#f6e8e8;color:#8a3434}
.cattag{display:inline-block;font-size:12px;color:#8a8a83;margin-left:8px;vertical-align:middle}
.manual{background:#fff;border:1px solid #e8e8e1;border-radius:20px;padding:22px;margin-top:28px}
.manual h3{margin:0 0 10px;font-size:16px;font-weight:600}
.manual a{color:#111;font-weight:600}
.manual p{font-size:14px;line-height:1.5;margin:4px 0 12px;color:#6b6b66}
footer{font-size:13px;color:#a3a39b;text-align:center;padding:28px 20px 40px}
@media(max-width:560px){header{padding-top:40px}header h1{font-size:34px}.cardbtns{flex-direction:column;align-items:flex-end}}
</style></head><body>
<header><p class="eyebrow">Job Tracker</p><h1>Find your next role.</h1><p>Business analyst, project management &amp; related roles &mdash; Toronto / GTA + remote Canada. Checked twice a day.</p></header>
<div class="wrap">
<div class="search"><input id="q" type="search" placeholder="Search by keyword or company"></div>
<div class="toolbar">
<button class="chip active" data-f="all">All</button>
<button class="chip" data-f="linkedin">LinkedIn</button>
<button class="chip" data-f="jobbank">Job Bank</button>
<button class="chip" data-f="dice">Dice</button>
<button class="chip" data-f="wwr">Remote</button>
<button class="chip" id="savedChip">&#9734; Saved</button>
<button class="chip" id="appliedChip">Hide applied</button>
<button class="chip" id="notintChip">Hide not interested</button>
<select class="f" id="cat"><option value="all">All categories</option></select>
<select class="f" id="sal"><option value="0">Any salary</option><option value="40000">$40k+</option><option value="50000">$50k+</option><option value="60000">$60k+</option><option value="75000">$75k+</option><option value="100000">$100k+</option></select>
<select class="f" id="age"><option value="0">Any time</option><option value="7">Last 7 days</option><option value="14">Last 14 days</option><option value="30">Last 30 days</option></select>
</div>
<div class="count" id="count"></div>
<div id="list"></div>
<div class="manual"><h3>Sites to check manually</h3><div id="manual"></div>
<p style="margin-top:10px">Tip: on Indeed and GC Jobs, create a free job alert with the same keywords - they email new matches daily.</p></div>
</div>
<footer id="upd"></footer>
<script>
const D=__DATA__;
const DATA={updated:D.u,manual:D.m,listings:D.l.map(c=>({source:c.s,label:c.l,title:c.t,company:c.c,location:c.lo,url:c.u,posted:c.p,salary:c.sa||"",is_new:!!c.n,category:c.ca||"Other",salary_min:c.smn,salary_max:c.smx}))};
let f="all", kw="", fcat="all", fsal=0, fage=0, savedOnly=false, hideApplied=false, hideNotInt=false;
const bookmarks = new Set(JSON.parse(localStorage.getItem("jt_bookmarks")||"[]"));
const applied = new Set(JSON.parse(localStorage.getItem("jt_applied")||"[]"));
const notint = new Set(JSON.parse(localStorage.getItem("jt_notinterested")||"[]"));
function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}
function salLabel(c){
 if(!c.salary_max) return c.salary ? c.salary : "";
 const k=v=>"$"+(Math.round(v/100)/10)+"k";
 return k(c.salary_min)+(c.salary_min!==c.salary_max?"&ndash;"+k(c.salary_max):"")+"/yr";
}
function daysOld(c){
 const d=new Date(c.posted);
 if(isNaN(d)) return null;
 return (Date.now()-d.getTime())/86400000;
}
function draw(){
 const items = DATA.listings.filter(c =>
   (f==="all"||c.source===f) &&
   (fcat==="all"||c.category===fcat) &&
   (!fsal||!c.salary_max||c.salary_max>=fsal) &&
   (!fage||daysOld(c)===null||daysOld(c)<=fage) &&
   (!savedOnly||bookmarks.has(c.url)) &&
   (!hideApplied||!applied.has(c.url)) &&
   (!hideNotInt||!notint.has(c.url)) &&
   (!kw || (c.title+" "+c.company+" "+c.location+" "+(c.category||"")).toLowerCase().includes(kw)));
 document.getElementById("count").textContent = items.length + " postings shown" + (savedOnly?" (saved)":"");
 document.getElementById("list").innerHTML = items.map(c =>
  `<div class="card"><div class="cardhead"><h3><a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.title)}</a>
   ${c.is_new?'<span class="tag new">NEW</span>':""}${applied.has(c.url)?'<span class="tag applied">APPLIED</span>':""}${notint.has(c.url)?'<span class="tag notint">NOT INTERESTED</span>':""}<span class="cattag">${esc(c.category||"")}</span></h3>
   <div class="cardbtns"><button class="notintbtn${notint.has(c.url)?" on":""}" data-url="${esc(c.url)}" title="Mark as not interested">&#10005; Not interested</button><button class="appliedbtn${applied.has(c.url)?" on":""}" data-url="${esc(c.url)}" title="Mark as applied">${applied.has(c.url)?"&#10003; Applied":"Mark applied"}</button><button class="star${bookmarks.has(c.url)?" on":""}" data-url="${esc(c.url)}" title="Save this job">${bookmarks.has(c.url)?"&#9733;":"&#9734;"}</button></div></div>
   <div class="meta"><span class="tag ${esc(c.source)}">${esc(c.label)}</span><strong>${esc(c.company||"")}</strong></div>
   <div class="meta">${esc(c.location||"")}${salLabel(c)?" &middot; "+salLabel(c):""} &middot; posted ${esc(c.posted)}</div></div>`).join("")
 || '<p class="meta">No postings match.</p>';
 document.getElementById("manual").innerHTML = DATA.manual.map(m =>
  `<div><a href="${esc(m.url)}" target="_blank" rel="noopener">${esc(m.name)}</a><p>${esc(m.note)}</p></div>`).join("");
 const d = new Date(DATA.updated);
 document.getElementById("upd").textContent = "Last checked " + d.toLocaleString();
}
document.querySelectorAll(".chip[data-f]").forEach(b=>b.onclick=()=>{document.querySelectorAll(".chip[data-f]").forEach(x=>x.classList.remove("active"));b.classList.add("active");f=b.dataset.f;draw()});
document.getElementById("savedChip").onclick=e=>{savedOnly=!savedOnly;e.target.classList.toggle("active",savedOnly);draw()};
document.getElementById("appliedChip").onclick=e=>{hideApplied=!hideApplied;e.target.classList.toggle("active",hideApplied);draw()};
document.getElementById("notintChip").onclick=e=>{hideNotInt=!hideNotInt;e.target.classList.toggle("active",hideNotInt);draw()};
document.getElementById("cat").onchange=e=>{fcat=e.target.value;draw()};
document.getElementById("sal").onchange=e=>{fsal=parseInt(e.target.value,10);draw()};
document.getElementById("age").onchange=e=>{fage=parseInt(e.target.value,10);draw()};
document.getElementById("q").oninput=e=>{kw=e.target.value.toLowerCase();draw()};
document.getElementById("list").onclick=e=>{
 const s=e.target.closest(".star");
 if(s){const u=s.dataset.url;if(bookmarks.has(u))bookmarks.delete(u);else bookmarks.add(u);localStorage.setItem("jt_bookmarks",JSON.stringify([...bookmarks]));draw();return;}
 const a=e.target.closest(".appliedbtn");
 if(a){const u=a.dataset.url;if(applied.has(u))applied.delete(u);else applied.add(u);localStorage.setItem("jt_applied",JSON.stringify([...applied]));draw();return;}
 const n=e.target.closest(".notintbtn");
 if(n){const u=n.dataset.url;if(notint.has(u))notint.delete(u);else notint.add(u);localStorage.setItem("jt_notinterested",JSON.stringify([...notint]));draw();}
};
[...new Set(DATA.listings.map(c=>c.category).filter(Boolean))].sort().forEach(c=>{
 const o=document.createElement("option");o.value=c;o.textContent=c;document.getElementById("cat").appendChild(o);
});
draw();
</script></body></html>"""

def build_page():
    with open(DASHBOARD, encoding="utf-8") as fh:
        page = fh.read()
    m = re.search(r"const DATA = (\{.*?\});\nlet f=", page, re.S)
    if not m:
        raise RuntimeError("could not locate DATA JSON in dashboard.html")
    data = json.loads(m.group(1))
    listings = data["listings"][:MAX_LISTINGS]
    compact = {
        "u": data["updated"],
        "m": data["manual"],
        "l": [
            {"s": c["source"], "l": c["label"], "t": c["title"], "c": c["company"],
             "lo": c["location"], "u": c["url"], "p": c["posted"],
             "sa": c.get("salary") or "", "n": 1 if c.get("is_new") else 0,
             "ca": c.get("category") or "Other",
             "smn": c.get("salary_min"), "smx": c.get("salary_max")}
            for c in listings
        ],
    }
    out = TEMPLATE.replace("__DATA__", json.dumps(compact, separators=(",", ":")))
    # Trim listings further if still oversize
    while len(out) > MAX_CONTENT_CHARS and len(compact["l"]) > 50:
        compact["l"] = compact["l"][:-50]
        out = TEMPLATE.replace("__DATA__", json.dumps(compact, separators=(",", ":")))
    return out


EMBEDDED_CONFIG = json.loads('{"jobbank": [{"locationstring": "Toronto", "searchstring": "business analyst"}, {"locationstring": "Toronto", "searchstring": "project coordinator"}, {"locationstring": "Toronto", "searchstring": "project manager"}, {"locationstring": "Toronto", "searchstring": "business systems analyst"}, {"locationstring": "Toronto", "searchstring": "data analyst"}, {"locationstring": "Toronto", "searchstring": "operations analyst"}], "linkedin": [{"location": "Toronto, Ontario, Canada", "query": "business analyst"}, {"location": "Toronto, Ontario, Canada", "query": "project coordinator"}, {"location": "Toronto, Ontario, Canada", "query": "associate project manager"}, {"location": "Toronto, Ontario, Canada", "query": "project manager"}, {"location": "Toronto, Ontario, Canada", "query": "business systems analyst"}, {"location": "Toronto, Ontario, Canada", "query": "technical business analyst"}, {"location": "Toronto, Ontario, Canada", "query": "product analyst"}, {"location": "Toronto, Ontario, Canada", "query": "associate product manager"}, {"location": "Toronto, Ontario, Canada", "query": "pmo analyst"}, {"location": "Toronto, Ontario, Canada", "query": "project analyst"}, {"location": "Toronto, Ontario, Canada", "query": "implementation specialist"}, {"location": "Toronto, Ontario, Canada", "query": "qa analyst"}, {"location": "Toronto, Ontario, Canada", "query": "scrum master"}, {"location": "Toronto, Ontario, Canada", "query": "operations analyst"}, {"location": "Toronto, Ontario, Canada", "query": "data analyst"}, {"location": "Toronto, Ontario, Canada", "query": "salesforce analyst"}, {"location": "Toronto, Ontario, Canada", "query": "crm analyst"}, {"location": "Toronto, Ontario, Canada", "query": "digital project coordinator"}, {"location": "Canada", "query": "business analyst", "remote": true}, {"location": "Canada", "query": "project coordinator", "remote": true}, {"location": "Canada", "query": "business systems analyst", "remote": true}, {"location": "Canada", "query": "data analyst", "remote": true}], "profile": "Junior BA/PM + adjacent roles (systems analyst, QA, scrum, data, Salesforce, implementation) - Toronto/GTA + remote Canada", "wwr_keywords": [], "dice": [{"query": "business analyst"}, {"query": "data analyst"}, {"query": "project coordinator"}, {"query": "product analyst"}]}')

if __name__ == "__main__":
    main()
