"""Praktikums-Radar v2: prueft taeglich alle Karriereseiten aus firmen.json.

Wege je Firma:
  - offene ATS-Schnittstellen (Lever, Greenhouse, Ashby, SmartRecruiters, Workable,
    Recruitee, Personio, Teamtailor-RSS, Amazon) -> direkt als JSON/XML
  - Workday -> oeffentliche Suchschnittstelle /wday/cxs/... (mit Paginierung)
  - alles andere -> echter Browser (Playwright/Chromium), inkl. eingebetteter
    iframes und "Mehr laden"-Knoepfe

Status je Firma:
  ok      Schnittstelle hat geantwortet bzw. die Seite zeigt Stellen-Links
  leer    Seite geladen, aber keine Stellen-Links gefunden (Startseite? Sperre?) -> NICHT als geprueft werten
  fehler  nicht erreichbar

Dateien in daten/:
  status.json      je Firma Status, Methode, Treffer, Fehler + Zusammenfassung
  kandidaten.json  Lissabon-Stellen mit passendem Titel
  alle.json        alle Lissabon-Stellen
  neu.json         Kandidaten, die heute zum ersten Mal auftauchen
  weg.json         Stellen, die gestern noch da waren und heute fehlen (nur Firmen mit Status ok)
  verlauf.json     Gedaechtnis: url -> erstes/letztes Datum
"""
import asyncio, json, os, re, sys, time, datetime, xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
import requests

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9,pt;q=0.8"}
HEUTE = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

LISSABON = (r"lisbo|lisbon|lissabon|oeiras|amadora|carnaxide|sintra|porto salvo|almada|loures|odivelas|cascais|estoril|carcavelos|"
            r"pa[cç]o de arcos|alg[eé]s|linda-a-velha|alfragide|barcarena|sacav[eé]m|moscavide|parque das na[cç][oõ]es|taguspark|"
            r"lagoas park|queluz|alverca|seixal|barreiro|montijo|palmela|set[uú]bal|vila franca de xira|mafra|odivelas")
LOC_LIS = re.compile(LISSABON, re.I)
LOC = re.compile(LISSABON + r"|portugal|\bPRT\b", re.I)
LOC_WEIT = re.compile(r"\b(porto|braga|coimbra|aveiro|faro|leiria|viseu|guimar[aã]es|madeira|a[cç]ores|covilh[aã]|evora|[eé]vora)\b", re.I)

ROLE = re.compile(r"intern|internship|est[aá]gi|stagiair|praktik|trainee|graduate|junior|j[uú]nior|jr\.?\b|entry|associate|academy|"
                  r"working student|werkstudent|\bbdr\b|\bsdr\b|business development|desenvolvimento de neg|sales|vendas?|vendedor|"
                  r"account|conta|partner|commercial|comercial|procure|purchas|buyer|compra|comprador|aprovision|sourcing|supply|"
                  r"logistic|log[ií]stic|operations|opera[cç][oõ]es|\bops\b|strategy|estrat[eé]g|consult|analyst|analista|neg[oó]cio|"
                  r"product manager|product owner|launch|growth|customer success|order.to.cash|category|einkauf|vertrieb", re.I)
SENIOR = re.compile(r"senior|s[eé]nior|\bsr\.?\b|\blead\b|head of|principal|director|diretor|vice president|\bvp\b|chief|\bstaff\b", re.I)
MANAGER = re.compile(r"manager|gestor", re.I)
MANAGER_OK = re.compile(r"account manager|partner manager|product manager|gestor de conta|junior|j[uú]nior|associate|graduate|trainee|intern|est[aá]gi", re.I)
NOISE = re.compile(r"accountant|payable|receivable|payroll|engineer|developer|devops|sre\b|talent|recruit|\bhr\b|human resources|"
                   r"nurse|physio|doctor|m[eé]dico|legal counsel|lawyer|designer\b", re.I)

def is_candidate(title):
    t = title or ""
    if not ROLE.search(t) or NOISE.search(t):
        return False
    if SENIOR.search(t):
        return False
    if MANAGER.search(t) and not MANAGER_OK.search(t):
        return False
    return True

def im_raum_lissabon(ort, titel=""):
    s = f"{ort} {titel}"
    if LOC_LIS.search(s):
        return True
    if LOC_WEIT.search(s):          # Porto, Braga ... ohne Lissabon-Bezug -> zu weit
        return False
    return bool(LOC.search(s))      # nur "Portugal"/"PRT" (z. B. remote PT) -> behalten

def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=40, **kw)
    r.raise_for_status()
    return r

def hit(title, ort, url):
    return {"titel": (title or "").strip()[:220], "ort": (ort or "").strip()[:220], "url": url}

# ---------- ATS-Schnittstellen ----------
def lever(api):
    data = get(api).json()
    if not data:  # EU-Konto? anderen Host probieren
        alt = api.replace("api.lever.co", "api.eu.lever.co") if "api.eu." not in api else api.replace("api.eu.lever.co", "api.lever.co")
        try:
            data = get(alt).json()
        except Exception:
            pass
    out = []
    for p in data:
        cat = p.get("categories") or {}
        locs = " / ".join([cat.get("location") or ""] + (cat.get("allLocations") or []))
        out.append(hit(p.get("text"), locs, p.get("hostedUrl")))
    return out

def greenhouse(api):
    data = None
    for u in (api, api.replace("boards-api.eu.", "boards-api.") if ".eu." in api else api.replace("boards-api.", "boards-api.eu.")):
        try:
            data = get(u).json()
            if data.get("jobs"):
                break
        except Exception:
            continue
    if data is None:
        raise RuntimeError("Greenhouse-Board nicht gefunden (Name falsch?)")
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
    while offset <= 1000:
        sep = "&" if "?" in api else "?"
        d = get(f"{api}{sep}limit=100&offset={offset}").json()
        content = d.get("content", [])
        for p in content:
            l = p.get("location") or {}
            ort = " ".join(str(l.get(k) or "") for k in ("city", "region", "country"))
            out.append(hit(p.get("name"), ort, f"https://jobs.smartrecruiters.com/{company}/{p.get('id')}"))
        offset += 100
        if not content or offset >= d.get("totalFound", 0):
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

def teamtailor_rss(url):
    u = urlparse(url)
    rss = f"{u.scheme}://{u.netloc}/jobs.rss"
    root = ET.fromstring(get(rss).content)
    out = []
    for it in root.iter("item"):
        ort = " ".join((e.text or "") for e in it.iter() if e.tag.split('}')[-1] in ("location", "city", "country", "name"))
        out.append(hit(it.findtext("title"), ort or it.findtext("description", "")[:200], it.findtext("link")))
    return out

def amazon(api):
    out, offset = [], 0
    base = re.sub(r"&?result_limit=\d+", "", api)
    while offset < 600:
        d = get(f"{base}&result_limit=100&offset={offset}").json()
        jobs = d.get("jobs", [])
        for j in jobs:
            out.append(hit(j.get("title"), j.get("normalized_location") or j.get("location"), "https://www.amazon.jobs" + (j.get("job_path") or "")))
        offset += 100
        if not jobs or offset >= d.get("hits", 0):
            break
    return out

def workday(url):
    u = urlparse(url)
    host = u.netloc
    tenant = host.split(".")[0]
    parts = [p for p in u.path.split("/") if p and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
    site = parts[0] if parts else "External"
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out = []
    for search in ("Lisbon", "Lisboa", "Portugal"):
        offset, total = 0, None
        while offset < 400:
            r = requests.post(api, json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": search},
                              headers={**UA, "Content-Type": "application/json", "Accept": "application/json"}, timeout=40)
            r.raise_for_status()
            d = r.json()
            if total is None:
                total = d.get("total", 0)   # Workday nennt total nur auf der ersten Seite
            posts = d.get("jobPostings", [])
            for p in posts:
                ort = p.get("locationsText") or ""
                if search != "Portugal" and re.search(r"\d+ locations?", ort, re.I):
                    ort += f" (Suche: {search})"  # Mehrort-Stellen aus der Lissabon-Suche behalten
                out.append(hit(p.get("title"), ort, f"https://{host}/{site}{p.get('externalPath', '')}"))
            offset += 20
            if not posts or offset >= (total or 0):
                break
    return out

def pick_api(f):
    api, url = f.get("api_url") or "", f["karriere_url"]
    if "myworkdayjobs.com" in url:
        return "workday", lambda: workday(url)
    table = [("lever.co", lever), ("greenhouse.io", greenhouse), ("ashbyhq.com", ashby), ("smartrecruiters.com", smartrecruiters),
             ("workable.com", workable), ("recruitee.com", recruitee), ("personio.", personio), ("amazon.jobs", amazon)]
    for key, fn in table:
        if key in api:
            return key.split(".")[0], lambda fn=fn: fn(api)
    if "teamtailor.com" in url:
        return "teamtailor", lambda: teamtailor_rss(url)
    return None, None

# ---------- Browser ----------
EXTRACT_JS = r"""
(reLoc) => {
  const LOC = new RegExp(reLoc, 'i');
  const JOB = /job|vaga|career|carreira|position|opening|stelle|requisition|apply|posting|vacanc/i;
  const out = []; const seen = new Set(); let jobLinks = 0;
  for (const a of document.querySelectorAll('a[href]')) {
    const t = (a.innerText || a.getAttribute('aria-label') || a.title || '').replace(/\s+/g,' ').trim();
    if (t.length < 6 || t.length > 250) continue;
    if (JOB.test(a.href)) jobLinks++;
    let ctx = t, el = a;
    for (let i = 0; i < 4 && el; i++) { el = el.parentElement; if (el && el.innerText && el.innerText.length < 700) ctx = el.innerText.replace(/\s+/g,' '); }
    if (!LOC.test(ctx + ' ' + a.href)) continue;
    if (seen.has(a.href)) continue; seen.add(a.href);
    out.push({titel: t, ort: ctx.slice(0, 220), url: a.href});
  }
  return {links: out.slice(0, 400), jobLinks, text: (document.body ? document.body.innerText.length : 0)};
}
"""
JOBURL = re.compile(r"job|vaga|career|carreira|position|opening|stelle|requisition|apply|posting|vacanc|oferta|emprego", re.I)
MORE = re.compile(r"load more|show more|more jobs|see more|view more|mais vagas|ver mais|carregar mais|mehr laden|weitere", re.I)

async def browse_once(pw_browser, f):
    ctx = await pw_browser.new_context(user_agent=UA["User-Agent"], locale="en-US")
    page = await ctx.new_page()
    try:
        await page.goto(f["karriere_url"], wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        for sel in ("button:has-text('Accept')", "button:has-text('Aceitar')", "button:has-text('Agree')", "#onetrust-accept-btn-handler"):
            try:
                await page.click(sel, timeout=1500)
                break
            except Exception:
                pass
        for _ in range(5):   # "Mehr laden" bis zu 5x
            await page.mouse.wheel(0, 5000)
            await page.wait_for_timeout(700)
            clicked = False
            for b in await page.query_selector_all("button, a[role=button]"):
                try:
                    txt = (await b.inner_text()).strip()
                    if MORE.search(txt) and await b.is_visible():
                        await b.click(timeout=2000)
                        await page.wait_for_timeout(1500)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                break
        links, job_links, text = [], 0, 0
        for fr in page.frames:   # auch eingebettete Greenhouse/Lever-iframes
            try:
                res = await fr.evaluate(EXTRACT_JS, LISSABON + r"|portugal")
                links += res["links"]; job_links += res["jobLinks"]; text += res["text"]
            except Exception:
                continue
        found = [hit(l["titel"], l["ort"], l["url"]) for l in links
                 if im_raum_lissabon(l["ort"], l["titel"]) and (JOBURL.search(l["url"]) or ROLE.search(l["titel"]))]
        if text < 200:
            return "fehler", found, "Seite leer oder gesperrt"
        if not found and job_links < 3:
            return "leer", found, "keine Stellenliste erkannt (Startseite, Sperre oder falsche URL?)"
        return "ok", found, ""
    finally:
        await ctx.close()

async def browse(pw_browser, f, sem):
    async with sem:
        try:
            st, found, err = await asyncio.wait_for(browse_once(pw_browser, f), timeout=100)
        except asyncio.TimeoutError:
            st, found, err = "fehler", [], "Zeitlimit 100 s"
        except Exception as e:
            st, found, err = "fehler", [], f"Browser: {type(e).__name__}: {str(e)[:150]}"
        if f.get("_api_fehler"):
            err = f"Schnittstelle: {f['_api_fehler']} | Browser: {err or st}"
            if st == "ok" and not found:
                st = "leer"
        return f, st, found, err

def run_api(f):
    kind, fn = pick_api(f)
    t0 = time.time()
    try:
        jobs = [j for j in fn() if j["url"] and im_raum_lissabon(j["ort"], j["titel"])]
        return f, kind, "ok", jobs, ""
    except Exception as e:
        return f, kind, "fehler", [], f"{kind}: {type(e).__name__}: {str(e)[:120]}"
    finally:
        print(f"{f['name']}: {kind} {time.time()-t0:.1f}s", file=sys.stderr)

def schreiben(status, alle, firmen, fertig):
    seen, uniq = set(), []
    for j in alle:
        if j["url"] in seen:
            continue
        seen.add(j["url"]); uniq.append(j)
    kandidaten = [j for j in uniq if is_candidate(j["titel"])]

    verlauf = {}
    if os.path.exists("daten/verlauf.json"):
        try:
            verlauf = json.load(open("daten/verlauf.json", encoding="utf-8"))
        except Exception:
            verlauf = {}
    ok_firmen = {s["name"] for s in status if s["status"] == "ok"}
    for j in uniq:
        v = verlauf.get(j["url"]) or {"erst": HEUTE, "firma": j["firma"], "titel": j["titel"]}
        v["zuletzt"] = HEUTE
        verlauf[j["url"]] = v
    weg = [{"url": u, **v} for u, v in verlauf.items() if v.get("zuletzt") != HEUTE and v.get("firma") in ok_firmen]
    if fertig:
        for w in weg:
            verlauf.pop(w["url"], None)
    neu = [j for j in kandidaten if verlauf.get(j["url"], {}).get("erst") == HEUTE]

    zaehl = {k: sum(1 for s in status if s["status"] == k) for k in ("ok", "leer", "fehler")}
    meta = {"stand": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "datum": HEUTE, "fertig": fertig,
            "firmen": len(firmen), **zaehl, "stellen_lissabon": len(uniq), "kandidaten": len(kandidaten), "neu": len(neu), "weg": len(weg)}
    status_sorted = sorted(status, key=lambda s: ({"fehler": 0, "leer": 1, "ok": 2}[s["status"]], s["name"]))
    dump = lambda name, obj: json.dump(obj, open(f"daten/{name}", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    dump("status.json", {"meta": meta, "firmen": status_sorted, "namen": sorted(f["name"] for f in firmen)})
    dump("kandidaten.json", {"meta": meta, "stellen": kandidaten})
    dump("alle.json", {"meta": meta, "stellen": uniq})
    dump("neu.json", {"meta": meta, "stellen": neu})
    dump("weg.json", {"meta": meta, "stellen": weg if fertig else []})
    if fertig:
        dump("verlauf.json", verlauf)
    print(json.dumps(meta), file=sys.stderr)

async def main():
    os.makedirs("daten", exist_ok=True)
    firmen = [f for f in json.load(open("firmen.json", encoding="utf-8")) if str(f.get("karriere_url", "")).startswith("https://")]
    status, alle, browser_list = [], [], []
    api_firmen = [f for f in firmen if pick_api(f)[1]]
    browser_list = [f for f in firmen if not pick_api(f)[1]]

    with ThreadPoolExecutor(max_workers=8) as ex:
        for f, kind, st, jobs, err in ex.map(run_api, api_firmen):
            if st == "ok":
                status.append({"name": f["name"], "methode": kind, "status": "ok", "treffer": len(jobs), "fehler": ""})
                alle += [{**j, "firma": f["name"]} for j in jobs]
            else:
                browser_list.append({**f, "_api_fehler": err})
    schreiben(status, alle, firmen, fertig=False)   # Zwischenstand

    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        sem = asyncio.Semaphore(6)
        for f, st, links, err in await asyncio.gather(*[browse(b, f, sem) for f in browser_list]):
            status.append({"name": f["name"], "methode": "browser", "status": st, "treffer": len(links), "fehler": err})
            alle += [{**l, "firma": f["name"]} for l in links]
        await b.close()
    schreiben(status, alle, firmen, fertig=True)

if __name__ == "__main__":
    asyncio.run(main())
