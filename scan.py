#!/usr/bin/env python3
"""Listing scanner - runs free on GitHub Actions (public repository).

Reads public job listings, keeps the ones that match the private settings, remembers
what it has already seen, and wakes a Claude task only when there is something new,
a listing has closed, or a source stops working.

Privacy: this public code holds no personal data. The settings (config.json) and the
memory (state/) live in a private store that the workflow checks out into STORE_DIR.
With PUBLIC_LOGS=1 the log shows counts only; the full log stays in the private store.
It never applies, signs in, or contacts anyone. It only reads public pages.
"""
import datetime as dt
import html
import json
import os
import random
import re
import sys
import time
from urllib.parse import quote, urlencode

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.abspath(os.environ.get("STORE_DIR") or ROOT)   # private store: config.json + state/
STATE = os.path.join(STORE, "state")
PUBLIC = os.environ.get("PUBLIC_LOGS", "") == "1"              # public logs: counts only
with open(os.path.join(STORE, "config.json"), encoding="utf-8") as _f:
    CFG = json.load(_f)
F = CFG["filters"]
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime.now(IST)
TODAY = NOW.date().isoformat()
STAMP = NOW.strftime("%d %b %Y %H:%M IST")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
LOG = []


def log(msg):
    if not PUBLIC:
        print(msg, flush=True)
    LOG.append(msg)


# ---------------------------------------------------------------- state files
def load(name, default):
    try:
        with open(os.path.join(STATE, name), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(name, obj):
    os.makedirs(STATE, exist_ok=True)
    tmp = os.path.join(STATE, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, os.path.join(STATE, name))


# ---------------------------------------------------------------- text helpers
TAG = re.compile(r"<[^>]+>")


def clean(s):
    s = TAG.sub(" ", s or "")
    s = html.unescape(html.unescape(s))
    s = TAG.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm(s):
    return " " + re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip() + " "


def has_any(text, words):
    """True if any whole word/phrase from words is in text (text must be norm()-ed)."""
    return [w for w in words if norm(w) in text]


# ---------------------------------------------------------------- scoring
DESC_BFSI = F["domain_strong"] + ["bank", "banking", "banks", "fintech", "financial services",
                                  "nbfc", "financial institutions"]


def stage1(title, company, location, remote=False, known_bfsi=False):
    """Cheap check on a search card. Returns (score, reason)."""
    t, c, l = norm(title), norm(company), norm(location)
    if has_any(t, F["exclude_title"]):
        return 0, "excluded title"
    strong, weak = has_any(t, F["seniority_strong"]), has_any(t, F["seniority_weak"])
    if not strong and not weak:
        return 0, "not senior"
    loc_ok = remote or bool(has_any(l, F["locations_ok"])) or re.search(r"\d+ locations", l.strip())
    if not loc_ok:
        return 0, "location"
    bfsi = known_bfsi or bool(has_any(c, F["bfsi_companies"])) or bool(has_any(c, F["bfsi_words"]))
    dom_s, dom_m = has_any(t, F["domain_strong"]), has_any(t, F["domain_medium"])
    if not (dom_s or dom_m or bfsi):
        return 0, "no domain"
    sen = 12 if weak else 30
    dom = 35 if dom_s else (20 if dom_m else 0)
    return sen + dom + 20 + (15 if bfsi else 0), "ok"


def stage2(item):
    """Final check once the description is known. Sets item['pre'] and item['why']."""
    t, d, c = norm(item["title"]), norm(item.get("desc", "")), norm(item["company"])
    s1, why = stage1(item["title"], item["company"], item["location"], item.get("remote"),
                     item.get("bfsi_known", False))
    if not s1:
        return 0, why
    if has_any(d, F["uae_exclude"]) or has_any(t, F["uae_exclude"]):
        return 0, "UAE-national only"
    if item.get("closed"):
        return 0, "no longer accepting applications"
    bfsi = (item.get("bfsi_known") or has_any(c, F["bfsi_companies"]) or has_any(c, F["bfsi_words"])
            or len(set(has_any(d, DESC_BFSI))) >= 2)
    if not bfsi:
        return 0, "not BFSI/fintech"
    weak = has_any(t, F["seniority_weak"])
    sen = 12 if weak else 30
    if has_any(t, F["domain_strong"]):
        dom = 35
    elif has_any(t, F["domain_medium"]):
        dom = 28 if len(set(has_any(d, F["domain_strong"]))) >= 2 else 20
    elif len(set(has_any(d, F["domain_strong"]))) >= 2:
        dom = 20
    else:
        dom = 8 if has_any(d, F["domain_medium"]) else 0
    score = sen + dom + 20 + 15
    return score, "ok"


# ---------------------------------------------------------------- http
class Http:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

    def get(self, url, **kw):
        kw.setdefault("timeout", 30)
        return self.s.get(url, **kw)

    def post(self, url, **kw):
        kw.setdefault("timeout", 30)
        return self.s.post(url, **kw)


def pause(sec):
    time.sleep(sec + random.uniform(0, sec * 0.3))


class SourceDown(Exception):
    pass


# ---------------------------------------------------------------- LinkedIn
LI_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
LI_DETAIL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/"


def li_get(http, url, delay):
    for attempt in range(3):
        r = http.get(url)
        if r.status_code == 200:
            return r
        if r.status_code in (429, 999):
            pause(15 * (attempt + 1))
            continue
        if r.status_code in (400, 404, 410):
            return r
        pause(delay)
    raise SourceDown(f"HTTP {r.status_code}")


def parse_li_cards(text, remote):
    out = []
    for chunk in re.split(r"<li[\s>]", text)[1:]:
        m = re.search(r"jobPosting:(\d+)", chunk)
        if not m:
            continue
        g = lambda rx: (lambda mm: clean(mm.group(1)) if mm else "")(re.search(rx, chunk))
        out.append({
            "id": "LI:" + m.group(1), "source": "LinkedIn",
            "title": g(r"base-search-card__title[^>]*>([\s\S]*?)</h3>"),
            "company": g(r"base-search-card__subtitle[^>]*>([\s\S]*?)</h4>"),
            "location": g(r"job-search-card__location[^>]*>([\s\S]*?)</span>"),
            "posted": (re.search(r'datetime="([^"]+)"', chunk) or [None, ""])[1],
            "url": f"https://www.linkedin.com/jobs/view/{m.group(1)}/",
            "remote": remote,
        })
        if remote and "remote" not in out[-1]["location"].lower():
            out[-1]["location"] += " (found in the India-remote search)"
    return out


def li_detail(http, item, delay):
    r = li_get(http, LI_DETAIL + item["id"][3:], delay)
    if r.status_code in (404, 410):
        item["closed"] = True
        return item
    t = r.text
    item["closed"] = "No longer accepting applications" in t or "closed-job" in t
    m = re.search(r"show-more-less-html__markup[^>]*>([\s\S]*?)</div>", t)
    item["desc"] = clean(m.group(1)) if m else clean(t)[:3000]
    crit = re.findall(r"description__job-criteria-subheader[^>]*>([\s\S]*?)</h3>\s*<span[^>]*>([\s\S]*?)</span>", t)
    item["facts"] = "; ".join(f"{clean(a)}: {clean(b)}" for a, b in crit)
    m = re.search(r"num-applicants__caption[^>]*>([\s\S]*?)<", t) or re.search(r"(\d[\d,]*\s+applicants)", t)
    if m:
        item["applicants"] = clean(m.group(1))
    return item


def scan_linkedin(http, slot, out):
    cfg = CFG["linkedin"]
    combos = [(k, l) for k in cfg["keywords"] for l in cfg["locations"]]
    if slot in (0, 1, 2):
        combos = [c for i, c in enumerate(combos) if i % 3 == slot]
    fails, cards = 0, 0
    for kw, loc in combos:
        q = {"keywords": kw, "location": loc["name"], "f_TPR": "r864000", "f_E": "4,5,6", "start": 0}
        if loc.get("remote"):
            q["f_WT"] = "2"
        try:
            r = li_get(http, LI_SEARCH + "?" + urlencode(q), cfg["delay_seconds"])
            found = parse_li_cards(r.text, bool(loc.get("remote")))
            cards += len(found)
            out.extend(found)
            fails = 0
        except (SourceDown, requests.RequestException) as e:
            fails += 1
            log(f"  LinkedIn '{kw}' / {loc['name']}: {e}")
            if fails >= 3:
                raise SourceDown(f"blocked after {cards} cards ({e})")
        pause(cfg["delay_seconds"])
    if cards == 0:
        raise SourceDown("0 cards from all searches")
    return f"{len(combos)} searches, {cards} cards"


# ---------------------------------------------------------------- Workday
def wd_base(t):
    return f"https://{t['host']}/wday/cxs/{t['tenant']}/{t['site']}"


def scan_workday_tenant(http, t, out):
    base = wd_base(t)
    try:
        http.get(f"https://{t['host']}/en-US/{t['site']}", timeout=20)
    except requests.RequestException:
        pass
    hdr = {"Accept": "application/json", "Content-Type": "application/json",
           "Origin": f"https://{t['host']}", "Referer": f"https://{t['host']}/en-US/{t['site']}"}
    seen, n = set(), 0
    for city in CFG["workday"]["cities"]:
        for offset in (0, 20, 40):
            body = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": city}
            r = http.post(base + "/jobs", json=body, headers=hdr)
            if r.status_code != 200:
                raise SourceDown(f"HTTP {r.status_code}")
            posts = r.json().get("jobPostings") or []
            for p in posts:
                path = p.get("externalPath") or ""
                if not path or path in seen:
                    continue
                seen.add(path)
                n += 1
                out.append({
                    "id": f"WD:{t['tenant']}:{path.rsplit('_', 1)[-1] if '_' in path else path}",
                    "source": f"{t['company']} Workday", "company": t["company"], "bfsi_known": True,
                    "title": p.get("title", ""), "location": p.get("locationsText") or city,
                    "posted": p.get("postedOn", ""), "url": f"https://{t['host']}/en-US/{t['site']}{path}",
                    "api": base + path, "search_city": city,
                })
            pause(1.5)
            if len(posts) < 20:
                break
    return f"{n} jobs"


def wd_detail(http, item):
    r = http.get(item["api"], headers={"Accept": "application/json"})
    if r.status_code in (404, 410):
        item["closed"] = True
        return item
    r.raise_for_status()
    info = r.json().get("jobPostingInfo") or {}
    item["desc"] = clean(info.get("jobDescription", ""))
    locs = [info.get("location", "")] + list(info.get("additionalLocations") or [])
    item["location"] = ", ".join(x for x in locs if x) or item["location"]
    item["posted"] = info.get("postedOn", item.get("posted", ""))
    if info.get("startDate"):
        item["posted"] += f" (start date {info['startDate']})"
    if info.get("canApply") is False:
        item["closed"] = True
    return item


# ---------------------------------------------------------------- Oracle Recruiting
def scan_oracle(http, o, out):
    n, seen = 0, set()
    for kw in CFG["bank_keywords"]:
        url = (f"https://{o['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
               f"?onlyData=true&expand=requisitionList&finder=findReqs;siteNumber={o['site']},"
               f"limit=200,keyword=%22{quote(kw)}%22,sortBy=POSTING_DATES_DESC")
        r = http.get(url, headers={"Accept": "application/json"})
        if r.status_code != 200:
            raise SourceDown(f"HTTP {r.status_code}")
        items = r.json().get("items") or []
        reqs = (items[0].get("requisitionList") if items else None) or []
        for q in reqs:
            rid = str(q.get("Id", ""))
            if not rid or rid in seen:
                continue
            seen.add(rid)
            n += 1
            out.append({
                "id": f"OR:{o['site']}:{rid}", "source": f"{o['company']} careers (Oracle)",
                "company": o["company"], "bfsi_known": True, "title": q.get("Title", ""),
                "location": q.get("PrimaryLocation", ""), "posted": q.get("PostedDate", ""),
                "url": f"https://{o['host']}/hcmUI/CandidateExperience/en/sites/{o['site']}/job/{rid}",
                "desc": clean(q.get("ShortDescriptionStr", "")),
            })
        pause(1)
    return f"{n} jobs"


# ---------------------------------------------------------------- RippleHire
def find_job_lists(obj):
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and "jobTitle" in obj[0]:
            return obj
        for x in obj:
            res = find_job_lists(x)
            if res:
                return res
    elif isinstance(obj, dict):
        for v in obj.values():
            res = find_job_lists(v)
            if res:
                return res
    return []


def scan_ripplehire(http, rh, out):
    page = http.get(rh["base"] + "/candidate/careers", allow_redirects=True)
    m = re.search(r"[?&]token=([A-Za-z0-9._\-]+)", page.url) or \
        re.search(r"token[\"']?\s*[:=]\s*[\"']([A-Za-z0-9._\-]{8,})", page.text)
    if not m:
        raise SourceDown("no session token on careers page")
    token, n, seen = m.group(1), 0, set()
    hdr = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
           "X-Requested-With": "XMLHttpRequest", "Referer": page.url}
    for kw in CFG["bank_keywords"]:
        for pg in range(1, 6):
            params = {"page": pg, "search": kw, "token": token, "source": "CAREERSITE",
                      "pagesize": 50, "location": "", "function": ""}
            body = "careerSiteUrlParams=" + quote(json.dumps(params, separators=(",", ":"))) + "&lang=en"
            r = http.post(rh["base"] + "/candidate/candidatejobsearch", data=body, headers=hdr)
            if r.status_code != 200:
                raise SourceDown(f"HTTP {r.status_code}")
            try:
                jobs = find_job_lists(r.json())
            except ValueError:
                raise SourceDown("reply was not JSON")
            for j in jobs:
                jid = str(j.get("jobId", ""))
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                n += 1
                locs = j.get("locations")
                locs = ", ".join(map(str, locs)) if isinstance(locs, list) else str(locs or "")
                yrs = j.get("jobMinExp") or j.get("jobReqExp") or ""
                out.append({
                    "id": f"RH:{rh['company']}:{jid}", "source": f"{rh['company']} careers (RippleHire)",
                    "company": rh["company"], "bfsi_known": True, "title": j.get("jobTitle", ""),
                    "location": locs, "posted": "", "url": rh["base"] + "/candidate/careers",
                    "desc": f"Job id {jid}. Experience: {yrs}" if yrs else f"Job id {jid}.",
                })
            pause(1)
            if len(jobs) < 50:
                break
    if n == 0:
        raise SourceDown("0 jobs returned")
    return f"{n} jobs"


# ---------------------------------------------------------------- Lever / Greenhouse / SuccessFactors
def scan_lever(http, lv, out):
    r = http.get(f"https://api.lever.co/v0/postings/{lv['slug']}?mode=json")
    if r.status_code != 200:
        raise SourceDown(f"HTTP {r.status_code}")
    jobs = r.json()
    for j in jobs:
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location", "")]
        created = j.get("createdAt")
        out.append({
            "id": f"LV:{lv['slug']}:{j.get('id')}", "source": f"{lv['company']} careers (Lever)",
            "company": lv["company"], "bfsi_known": True, "title": j.get("text", ""),
            "location": ", ".join(x for x in locs if x),
            "posted": dt.datetime.fromtimestamp(created / 1000, IST).date().isoformat() if created else "",
            "url": j.get("hostedUrl", ""), "desc": clean(j.get("descriptionPlain") or j.get("description", "")),
        })
    return f"{len(jobs)} jobs", {f"LV:{lv['slug']}:{j.get('id')}" for j in jobs}


def scan_greenhouse(http, gh, out):
    r = http.get(f"https://boards-api.greenhouse.io/v1/boards/{gh['board']}/jobs?content=true")
    if r.status_code != 200:
        raise SourceDown(f"HTTP {r.status_code}")
    jobs = r.json().get("jobs") or []
    for j in jobs:
        out.append({
            "id": f"GH:{gh['board']}:{j.get('id')}", "source": f"{gh['company']} careers (Greenhouse)",
            "company": gh["company"], "bfsi_known": True, "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""), "posted": (j.get("updated_at") or "")[:10],
            "url": j.get("absolute_url", ""), "desc": clean(j.get("content", "")),
        })
    return f"{len(jobs)} jobs", {f"GH:{gh['board']}:{j.get('id')}" for j in jobs}


def scan_successfactors(http, sf, out):
    n = 0
    for start in range(0, 200, 25):
        r = http.get(sf["url"] + f"&startrow={start}")
        if r.status_code != 200:
            raise SourceDown(f"HTTP {r.status_code}")
        rows = re.split(r'<tr class="data-row', r.text)[1:]
        for row in rows:
            m = re.search(r'<a[^>]+href="([^"]+)"[^>]*class="jobTitle-link[^"]*"[^>]*>([\s\S]*?)</a>', row) or \
                re.search(r'<a[^>]+class="jobTitle-link[^"]*"[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>', row)
            if not m:
                continue
            loc = re.search(r'class="jobLocation"[^>]*>([\s\S]*?)</span>', row)
            date = re.search(r'class="jobDate"[^>]*>([\s\S]*?)</span>', row)
            host = re.match(r"(https://[^/]+)", sf["url"]).group(1)
            n += 1
            out.append({
                "id": f"SF:{sf['company']}:{m.group(1)}", "source": f"{sf['company']} careers",
                "company": sf["company"], "bfsi_known": True, "title": clean(m.group(2)),
                "location": clean(loc.group(1)) if loc else "", "posted": clean(date.group(1)) if date else "",
                "url": host + m.group(1),
            })
        if len(rows) < 25:
            break
        pause(1)
    if n == 0:
        raise SourceDown("0 jobs parsed")
    return f"{n} jobs"


# ---------------------------------------------------------------- iimjobs (experimental)
def scan_iimjobs(http, out):
    n, seen = 0, set()
    for kw in CFG["iimjobs"]["searches"]:
        slug = re.sub(r"[^a-z0-9]+", "-", kw.lower()).strip("-")
        r = http.get(f"https://www.iimjobs.com/search/{slug}-jobs?query={quote(kw)}")
        if r.status_code != 200:
            raise SourceDown(f"HTTP {r.status_code}")
        for path in re.findall(r'href="(?:https://www\.iimjobs\.com)?(/j/[a-z0-9\-]+)', r.text):
            if path in seen:
                continue
            seen.add(path)
            n += 1
            words = path[3:].rsplit("-", 1)[0].replace("-", " ")
            out.append({
                "id": "IJ:" + path.rsplit("-", 1)[-1], "source": "iimjobs", "company": "",
                "title": words.title(), "location": words, "posted": "",
                "url": "https://www.iimjobs.com" + path, "iim": True,
            })
        pause(2)
    if n == 0:
        raise SourceDown("0 listings parsed (page needs a browser)")
    return f"{n} listings"


def iim_detail(http, item):
    r = http.get(item["url"])
    if r.status_code in (404, 410):
        item["closed"] = True
        return item
    t = r.text
    m = re.search(r"<title>([\s\S]*?)</title>", t)
    if m:
        item["title"] = clean(m.group(1)).split("|")[0].strip() or item["title"]
    m = re.search(r'name="description" content="([^"]*)"', t)
    body = clean(t)
    i = body.lower().find("job description")
    item["desc"] = ((clean(m.group(1)) + " ") if m else "") + (body[i:i + 2000] if i >= 0 else "")
    places = has_any(norm(item["title"] + " " + item["desc"][:1500]), F["locations_ok"])
    item["location"] = (", ".join(p.strip().title() for p in places) + " (from listing text)") if places else "not stated"
    item["company"] = item["company"] or "see listing"
    return item


# ---------------------------------------------------------------- slots
SLOT_TIMES = {0: (8, 0), 1: (13, 0), 2: (18, 0)}   # IST start of each search group


def ran_today(runs, slot):
    day = NOW.strftime("%d %b %Y")
    return any(str(r.get("slot")) == str(slot) and str(r.get("at", "")).startswith(day) for r in runs)


def current_slot():
    """Which search group to run. The outside timer passes the group (0/1/2) at its exact time.
    GitHub's own back-up timer passes 'auto' and can start hours late: it runs the earliest group
    that is due today and has not run yet, so a late start never skips a group. None = nothing due."""
    s = os.environ.get("SLOT", "auto").strip().lower()
    if s == "all":
        return "all"
    runs = load("runs.json", [])
    if s in ("0", "1", "2"):
        k = int(s)
        if os.environ.get("TRIGGER", "") == "timer" and ran_today(runs, k):
            return None
        return k
    for k in (0, 1, 2):
        if (NOW.hour, NOW.minute) >= SLOT_TIMES[k] and not ran_today(runs, k):
            return k
    return None


def runs_in(slot, wanted):
    return slot == "all" or slot in wanted


# ---------------------------------------------------------------- main
def main():
    slot = current_slot()
    if slot is None:
        print(f"{STAMP}: no search group due now (already done today) - stopped.", flush=True)
        return
    log(f"Scanner - {STAMP} - slot {slot}")
    seen = load("seen.json", {})          # id -> {"d": date, "s": status}
    pending = load("pending.json", {})    # stage-1 passes waiting for a detail check
    queue = load("queue.json", {})        # final passes waiting to be sent to Claude
    sent = load("sent.json", {})          # sent items, for closed-listing checks
    health = load("health.json", {})      # source -> {fails, last_ok, err, said}
    closures = load("closures.json", {})  # id -> item, waiting to be reported
    http = Http()
    found, board_ids, stats = [], {}, {}

    def run(name, fn, *args):
        try:
            res = fn(*args)
            if isinstance(res, tuple):
                res, ids = res
                board_ids[name] = ids
            h = health.setdefault(name, {})
            h.update({"fails": 0, "last_ok": TODAY, "err": "", "last": res})
            stats[name] = res
            log(f"  {name}: ok - {res}")
        except Exception as e:  # one broken source never stops the others
            h = health.setdefault(name, {})
            h["fails"] = h.get("fails", 0) + 1
            h["err"] = f"{type(e).__name__}: {e}"[:200]
            stats[name] = "FAILED " + h["err"]
            log(f"  {name}: FAILED ({h['fails']} in a row) - {h['err']}")

    run("linkedin", scan_linkedin, http, slot, found)
    if runs_in(slot, (1,)):
        for t in CFG["workday"]["tenants"]:
            run(f"workday:{t['company']}", scan_workday_tenant, http, t, found)
        for o in CFG["oracle"]:
            run(f"oracle:{o['company']}", scan_oracle, http, o, found)
        for rh in CFG["ripplehire"]:
            run(f"ripplehire:{rh['company']}", scan_ripplehire, http, rh, found)
        for lv in CFG["lever"]:
            run(f"lever:{lv['company']}", scan_lever, http, lv, found)
        for gh in CFG["greenhouse"]:
            run(f"greenhouse:{gh['company']}", scan_greenhouse, http, gh, found)
        for sf in CFG["successfactors"]:
            run(f"successfactors:{sf['company']}", scan_successfactors, http, sf, found)
        run("iimjobs", scan_iimjobs, http, found)

    # ---- stage 1: cheap filter on what the searches returned
    new1 = 0
    for it in found:
        if it["id"] in seen or it["id"] in queue or it["id"] in pending:
            continue
        loc_text = it["location"] if not it.get("iim") else "remote"   # iimjobs: location checked later
        s1, _ = stage1(it["title"], it["company"], loc_text, it.get("remote"), it.get("bfsi_known", False))
        if s1:
            it["s1"] = s1
            it["first"] = TODAY
            pending[it["id"]] = it
            new1 += 1
    log(f"Stage 1: {len(found)} listings read, {new1} new possible matches, {len(pending)} waiting for a detail check")

    # ---- stage 2: open the most promising listings and score them properly
    todo = sorted(pending.values(), key=lambda x: (-x["s1"], x.get("first", "")))[:CFG["detail_fetch_per_scan"]]
    checked = passed = 0
    for it in todo:
        try:
            if it["id"].startswith("LI:"):
                if str(stats.get("linkedin", "")).startswith("FAILED"):
                    continue
                li_detail(http, it, CFG["linkedin"]["delay_seconds"])
                pause(CFG["linkedin"]["delay_seconds"])
            elif it["id"].startswith("WD:"):
                wd_detail(http, it)
                pause(1.5)
            elif it.get("iim"):
                iim_detail(http, it)
                pause(2)
        except Exception as e:
            log(f"  detail check failed for {it['id']}: {e}")
            it["tries"] = it.get("tries", 0) + 1
            if it["tries"] >= 3:
                seen[it["id"]] = {"d": TODAY, "s": "unreadable"}
                pending.pop(it["id"], None)
            continue
        checked += 1
        score, why = stage2(it)
        pending.pop(it["id"], None)
        if score >= CFG["threshold"]:
            it["pre"] = score
            it["checked"] = STAMP
            queue[it["id"]] = it
            passed += 1
        else:
            seen[it["id"]] = {"d": TODAY, "s": "low: " + why}
    # sources that already give a description (Oracle, Lever, Greenhouse, RippleHire, SuccessFactors) need no detail call
    log(f"Stage 2: {checked} listings opened, {passed} good matches queued, {len(queue)} in the queue")

    # ---- closed-listing checks
    for name, ids in board_ids.items():
        if not ids:          # an empty board is more likely a glitch than every job closing
            continue
        prefix = next(iter(ids)).rsplit(":", 1)[0] + ":"
        for sid, s in list(sent.items()):
            if sid.startswith(prefix) and sid not in ids:
                closures[sid] = s
    if runs_in(slot, (2,)):
        n = 0
        for sid, s in sorted(sent.items(), key=lambda kv: kv[1].get("checked", "")):
            if n >= 30 or not (sid.startswith("LI:") or sid.startswith("WD:")):
                continue
            n += 1
            try:
                probe = dict(s)
                if sid.startswith("LI:"):
                    li_detail(http, probe, CFG["linkedin"]["delay_seconds"])
                    pause(CFG["linkedin"]["delay_seconds"])
                else:
                    wd_detail(http, probe)
                    pause(1.5)
                s["checked"] = TODAY
                if probe.get("closed"):
                    closures[sid] = s
            except Exception as e:
                log(f"  closed-check failed for {sid}: {e}")
        log(f"Closed check: {n} sent listings re-opened, {len(closures)} closed to report")
    for sid in closures:
        sent.pop(sid, None)

    # ---- source health changes worth telling Claude about
    alerts = []
    for name, h in health.items():
        if h.get("fails", 0) >= 2 and h.get("said") != "down":
            alerts.append((name, "DOWN", h))
        elif h.get("fails", 0) == 0 and h.get("said") == "down":
            alerts.append((name, "WORKING AGAIN", h))

    # ---- wake Claude
    fire_ok, fire_err = None, ""
    want_fire = os.environ.get("FIRE", "yes").lower() != "no"
    in_fire_slot = runs_in(slot, CFG["fire"]["slots"]) or os.environ.get("TRIGGER", "") == "manual"
    if want_fire and in_fire_slot and (queue or closures or alerts):
        items = sorted(queue.values(), key=lambda x: -x["pre"])[:CFG["fire"]["max_items_per_fire"]]
        text, items = build_payload(items, list(closures.values()), alerts, health, stats, len(queue))
        fire_ok, fire_err = fire(text)
        if fire_ok:
            for it in items:
                queue.pop(it["id"], None)
                seen[it["id"]] = {"d": TODAY, "s": "sent"}
                sent[it["id"]] = {k: it.get(k, "") for k in ("id", "title", "company", "url", "source", "api")}
                sent[it["id"]]["sent"] = TODAY
            for sid in list(closures):
                seen[sid] = {"d": TODAY, "s": "closed"}
            closures = {}
            for name, state, h in alerts:
                h["said"] = "down" if state == "DOWN" else ""
    elif not (queue or closures or alerts):
        log("Nothing new - Claude not woken (zero tokens used).")
    else:
        log("Holding results for the next wake-up slot.")

    # ---- tidy and save
    cutoff = (NOW.date() - dt.timedelta(days=180)).isoformat()
    seen = {k: v for k, v in seen.items() if v.get("d", TODAY) >= cutoff}
    sent_cut = (NOW.date() - dt.timedelta(days=40)).isoformat()
    sent = {k: v for k, v in sent.items() if v.get("sent", TODAY) >= sent_cut}
    pend_cut = (NOW.date() - dt.timedelta(days=5)).isoformat()
    pending = {k: v for k, v in pending.items() if v.get("first", TODAY) >= pend_cut}
    save("seen.json", seen)
    save("pending.json", pending)
    save("queue.json", queue)
    save("sent.json", sent)
    save("health.json", health)
    save("closures.json", closures)
    line = {"at": STAMP, "slot": str(slot), "sources": stats, "stage1_new": new1, "opened": checked,
            "queued": passed, "queue": len(queue), "woke_claude": fire_ok, "error": fire_err}
    runs = load("runs.json", [])
    runs = (runs + [line])[-200:]
    save("runs.json", runs)
    ok = sum(1 for v in stats.values() if not str(v).startswith("FAILED"))
    woke = "yes" if fire_ok else ("no - wake-up failed" if fire_ok is False else "no")
    public = (f"{STAMP} - group {slot} - sources ok {ok} of {len(stats)} - listings read {len(found)} - "
              f"new possible {new1} - opened {checked} - queued {passed} (queue {len(queue)}) - Claude woken: {woke}")
    print(public, flush=True)
    try:
        with open(os.path.join(STATE, "last_log.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(LOG) + "\n")
    except OSError:
        pass
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## Scan\n\n" + (public if PUBLIC else "\n".join("    " + x for x in LOG)) + "\n")
    if fire_ok is False and fire_err.startswith("config"):
        sys.exit(f"Could not wake Claude: {fire_err}")


def build_payload(items, closed, alerts, health, stats, queue_len):
    cfg = CFG["fire"]
    head = [f"GITHUB JOB SCANNER - {STAMP}",
            "SOURCES TODAY: " + "; ".join(f"{k} {v}" for k, v in stats.items())]
    down = [k for k, h in health.items() if h.get("fails", 0) >= 2]
    head.append("SOURCES DOWN (laptop should cover them): " + (", ".join(down) if down else "none"))
    for name, state, h in alerts:
        head.append(f"HEALTH CHANGE: {name} {state} ({h.get('err') or 'ok'})")
    body = [f"NEW MATCHES ({len(items)} of {queue_len} waiting; best first):"]
    for i, it in enumerate(items, 1):
        jd = (it.get("desc") or "")[:cfg["jd_chars"]]
        body.append(
            f"{i}. [{it['id']}] {it['title']} | {it['company']} | {it['location'][:120]} | posted {it.get('posted') or 'unknown'}"
            f" | pre-score {it['pre']} | {it['source']} | {it['url']}\n"
            f"   Checked live {it.get('checked', STAMP)}: accepting applications"
            + (f"; {it['applicants']}" if it.get("applicants") else "")
            + (f"; {it['facts']}" if it.get("facts") else "") + "\n"
            f"   JD: {jd}")
    tail = [f"CLOSED ({len(closed)}) - mark these Closed if they are in the Command Centre:"]
    tail += [f"- [{c.get('id')}] {c.get('title')} | {c.get('company')} | {c.get('url')}" for c in closed]
    text = "\n".join(head + body + tail)
    while len(text) > cfg["max_payload_chars"] and items:
        items = items[:-1]
        return build_payload(items, closed, alerts, health, stats, queue_len)
    return text, items


def fire(text):
    url, token = os.environ.get("ROUTINE_FIRE_URL", ""), os.environ.get("ROUTINE_FIRE_TOKEN", "")
    if not url or not token:
        log("No wake-up key saved yet (ROUTINE_FIRE_URL / ROUTINE_FIRE_TOKEN) - results kept for later.")
        return False, "config: secrets missing"
    for attempt in range(3):
        try:
            r = requests.post(url, timeout=30, json={"text": text}, headers={
                "Authorization": f"Bearer {token}", "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"})
        except requests.RequestException as e:
            log(f"Wake-up call failed: {e}")
            time.sleep(20)
            continue
        if r.status_code == 200:
            log(f"Claude woken: {r.json().get('claude_code_session_url', '')}")
            return True, ""
        if r.status_code in (400, 401, 403, 404):
            log(f"Wake-up refused ({r.status_code}): {r.text[:300]}")
            return False, f"config: HTTP {r.status_code}"
        log(f"Wake-up busy ({r.status_code}), retrying")
        time.sleep(int(r.headers.get("Retry-After", "30")) if r.status_code == 429 else 30)
    return False, "busy: will retry next slot"


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        if not PUBLIC:
            raise
        import traceback
        try:
            os.makedirs(STATE, exist_ok=True)
            with open(os.path.join(STATE, "last_error.txt"), "w", encoding="utf-8") as f:
                f.write(STAMP + "\n" + traceback.format_exc())
        except OSError:
            pass
        sys.exit(f"Scanner stopped with an error ({type(e).__name__}) - details saved in the private store.")
