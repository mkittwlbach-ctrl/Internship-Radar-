"""Praktikums-Radar: prueft taeglich alle Karriereseiten aus firmen.json und
schreibt die Lissabon-/Portugal-Stellen nach daten/.

- ATS mit offener Schnittstelle (Lever, Greenhouse, Ashby, SmartRecruiters,
  Workable, Recruitee, Personio, Amazon) werden direkt als JSON/XML gelesen.
- Workday wird ueber die oeffentliche Such-Schnittstelle (/wday/cxs/...) gelesen.
- Alles andere oeffnet ein echter Browser (Playwright/Chromium) und sammelt
  Links, in deren Umfeld ein Ort im Grossraum Lissabon/Portugal steht.

Ergebnis:
  daten/status.json      je Firma: ok/fehler, Methode, Anzahl Treffer
  daten/kandidaten.json  Lissabon-Stellen mit passendem Titel (Praktikum, Junior, Sales, Einkauf ...)
  daten/alle.json        alle gefundenen Lissabon-/Portugal-Stellen
"""
import asyncio, json, re, sys, time, datetime, xml.etree.ElementTree as ET
from urllib.parse import urlparse, urljoin
import requests

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9,pt;q=0.8"}
LOC = re.compile(r"lisbo|lisbon|lissabon|portugal|oeiras|amadora|carnaxide|sintra|porto salvo|almada|loures|odivelas|cascais|"
                 r"linda-a-velha|alfragide|barcarena|sacav[eé]m|parque das na[cç][oõ]es|taguspark|lagoas park|\bPT\b|PRT", re.I)
ROLE = re.compile(r"intern|internship|est[aá]gi|trainee|graduate|junior|jr\.?\b|entry|associate|academy|working student|werkstudent|"
                  r"\bbdr\b|\bsdr\b|business development|sales|account|partner|commercial|procure|purchas|buyer|sourcing|supply|"
                  r"logistic|operations|\bops\b|strategy|consult|analyst|product|launch|growth|customer success|order.to.cash|category", re.I)
SENIOR = re.compile(r"senior|\bsr\.?\b|\blead\b|head of|principal|director|vice president|\bvp\b|chief|staff |manager", re.I)
MANAGER_OK = re.compile(r"account manager|partner manager|junior .*manager|associate .*manager|graduate", re.I)

def is_candidate(title):
    if not title or not ROLE.search(title):
        return False
    if SENIOR.search(title) and not MANAGER_OK.search(title):
        return False
    return True

def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=40, **kw)
    r.raise_for_status()
    return r

def hit(title, ort, url):
    return {"titel": (title or "").strip()[:200], "ort": (ort or "").strip()[:200], "url": url}

# ---------- ATS-Schnittstellen ----------
def lever(api):
    out = []
    for p in get(api).json():
        cat = p.get("categories") or {}
        locs = " / ".join([cat.get("location") or ""] + (cat.get("allLocations") or []))
        out.append(hit(p.get("text"), locs, p.get("hostedUrl")))
    return out

def greenhouse(api):
    try:
        data = get(api).json()
    except Exception:
        alt = api.replace("boards-api.eu.", "boards-api.") if ".eu." in api else api.replace("boards-api.", "boards-api.eu.")
        data = get(alt).json()
    out = []
    for j in data.get("jobs", []):
        locs = (j.get("location") or {}).get("name", "")
        offices = " / ".join(o.get("name", "") for o in j.get("offices", []) or [])
        out.append(hit(j.get("title"), f"{locs} {offices}".strip(), j.get("absolute_url")))
    return out

def ashby(api):
    out = []
    for j in get(api).json().get("jobs", []):
        locs = " / ".join([j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations", []) or []])
        out.append(hit(j.get("title"), locs, j.get("jobUrl")))
    return out

def smartrecruiters(api):
    out, offset = [], 0
    company = re.search(r"companies/([^/]+)/", api).group(1)
    while True:
        sep = "&" if "?" in api else "?"
        d = get(f"{api}{sep}limit=100&offset={offset}").json()
        for p in d.get("content", []):
            l = p.get("location") or {}
            ort = " ".join(str(l.get(k) or "") for k in ("city", "region", "country"))
            out.append(hit(p.get("name"), ort, f"https://jobs.smartrecruiters.com/{company}/{p.get('id')}"))
        offset += 100
        if offset >= d.get("totalFound", 0) or offset > 1000:
            break
    return out

def workable(api):
    d = get(api).json()
    out = []
    for j in d.get("jobs", []):
        ort = " ".join(str(j.get(k) or "") for k in ("city", "state", "country"))
        locs = " / ".join(" ".join(str(l.get(k) or "") for k in ("city", "country")) for l in j.get("locations", []) or [])
        out.append(hit(j.get("title"), f"{ort} {locs}".strip(), j.get("url") or j.get("shortlink")))
    return out

def recruitee(api):
    out = []
    for o in get(api).json().get("offers", []):
        ort = " ".join(str(o.get(k) or "") for k in ("location", "city", "country"))
        out.append(hit(o.get("title"), ort, o.get("careers_url")))
    return out

def personio(api):
    root = ET.fromstring(get(api).content)
    base = api.rsplit("/xml", 1)[0]
    out = []
    for p in root.findall("position"):
        out.append(hit(p.findtext("name"), p.findtext("office"), f"{base}/job/{p.findtext('id')}"))
    return out

def amazon(api):
    out = []
    for j in get(api).json().get("jobs", []):
        out.append(hit(j.get("title"), j.get("normalized_location") or j.get("location"), "https://www.amazon.jobs" + (j.get("job_path") or "")))
    return out

def workday(url):
    u = urlparse(url)
    host = u.netloc
    tenant = host.split(".")[0]
    parts = [p for p in u.path.split("/") if p and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
    site = parts[0] if parts else "External"
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out, offset = [], 0
    for search in ("Lisbon", "Portugal"):
        offset = 0
        while offset < 200:
            r = requests.post(api, json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": search},
                              headers={**UA, "Content-Type": "application/json", "Accept": "application/json"}, timeout=40)
            r.raise_for_status()
            d = r.json()
            posts = d.get("jobPostings", [])
            for p in posts:
                out.append(hit(p.get("title"), p.get("locationsText"), f"https://{host}/{site}{p.get('externalPath', '')}"))
            offset += 20
            if offset >= d.get("total", 0) or not posts:
                break
    return out

def pick_api(f):
    api, url = f.get("api_url") or "", f["karriere_url"]
    if "myworkdayjobs.com" in url:
        return "workday", lambda: workday(url)
    table = [("lever.co", lever), ("greenhouse.io", greenhouse), ("ashbyhq.com", ashby), ("smartrecruiters.com", smartrecruiters),
             ("workable.com", workable), ("recruitee.com", recruitee), ("personio.com", personio), ("amazon.jobs", amazon)]
    for key, fn in table:
        if key in api:
            return key.split(".")[0], lambda fn=fn: fn(api)
    return None, None

# ---------- Browser fuer alles andere ----------
EXTRACT_JS = r"""
() => {
  const LOC = /lisbo|lisbon|lissabon|portugal|oeiras|amadora|carnaxide|sintra|porto salvo|almada|loures|odivelas|cascais|linda-a-velha|alfragide|barcarena|sacav[eé]m|parque das na[cç][oõ]es|taguspark|lagoas park/i;
  const out = []; const seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const t = (a.innerText || a.getAttribute('aria-label') || '').replace(/\s+/g,' ').trim();
    if (t.length < 6 || t.length > 160) continue;
    let ctx = t, el = a;
    for (let i = 0; i < 4 && el; i++) { el = el.parentElement; if (el && el.innerText && el.innerText.length < 600) ctx = el.innerText.replace(/\s+/g,' '); }
    const m = (ctx + ' ' + a.href).match(LOC);
    if (!m) continue;
    if (seen.has(a.href)) continue; seen.add(a.href);
    out.push({titel: t, ort: ctx.slice(0, 200), url: a.href});
  }
  return {links: out.slice(0, 300), text: (document.body.innerText || '').length};
}
"""

async def browse(pw_browser, f, sem):
    async with sem:
        ctx = await pw_browser.new_context(user_agent=UA["User-Agent"], locale="en-US")
        page = await ctx.new_page()
        try:
            await page.goto(f["karriere_url"], wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            for _ in range(3):
                await page.mouse.wheel(0, 4000)
                await page.wait_for_timeout(800)
            res = await page.evaluate(EXTRACT_JS)
            links = [hit(l["titel"], l["ort"], l["url"]) for l in res["links"]]
            status = "ok" if res["text"] > 200 else "fehler"
            return f, status, links, "" if status == "ok" else "Seite fast leer (Blockade oder Login?)"
        except Exception as e:
            return f, "fehler", [], f"Browser: {type(e).__name__}: {str(e)[:150]}"
        finally:
            await ctx.close()

async def main():
    firmen = json.load(open("firmen.json", encoding="utf-8"))
    stand = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    status, alle = [], []
    browser_list = []
    for f in firmen:
        kind, fn = pick_api(f)
        if not fn:
            browser_list.append(f); continue
        t0 = time.time()
        try:
            jobs = [j for j in fn() if j["url"] and LOC.search(f"{j['ort']} {j['titel']}")]
            status.append({"name": f["name"], "methode": kind, "status": "ok", "treffer": len(jobs), "fehler": ""})
            alle += [{**j, "firma": f["name"]} for j in jobs]
        except Exception as e:
            # Schnittstelle kaputt -> Browser als Ersatz
            browser_list.append({**f, "_api_fehler": f"{kind}: {type(e).__name__}: {str(e)[:120]}"})
        print(f"{f['name']}: {kind} {time.time()-t0:.1f}s", file=sys.stderr)

    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        sem = asyncio.Semaphore(4)
        for f, st, links, err in await asyncio.gather(*[browse(b, f, sem) for f in browser_list]):
            if f.get("_api_fehler") and st == "ok":
                err = "Schnittstelle fehlgeschlagen, Browser ok"
            status.append({"name": f["name"], "methode": "browser", "status": st, "treffer": len(links),
                           "fehler": err or f.get("_api_fehler", "")})
            alle += [{**l, "firma": f["name"]} for l in links]
        await b.close()

    # Dubletten raus
    seen, uniq = set(), []
    for j in alle:
        if j["url"] in seen: continue
        seen.add(j["url"]); uniq.append(j)
    kandidaten = [j for j in uniq if is_candidate(j["titel"])]
    ok = sum(1 for s in status if s["status"] == "ok")
    meta = {"stand": stand, "firmen": len(firmen), "ok": ok, "fehler": len(firmen) - ok,
            "stellen_lissabon": len(uniq), "kandidaten": len(kandidaten)}
    status.sort(key=lambda s: (s["status"] != "fehler", s["name"]))
    json.dump({"meta": meta, "firmen": status}, open("daten/status.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump({"meta": meta, "stellen": kandidaten}, open("daten/kandidaten.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump({"meta": meta, "stellen": uniq}, open("daten/alle.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(meta), file=sys.stderr)

if __name__ == "__main__":
    asyncio.run(main())
