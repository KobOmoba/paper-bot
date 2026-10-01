"""Build ngx_history.json from NGX's official public 'Daily Summary' price-list PDFs.
Educational / personal use only; NGX owns its market data. Polite: ~2 requests/second.
Usage: python ngx_backfill.py [--days 520]"""
import datetime, difflib, io, json, re, sys, time, urllib.error, urllib.parse, urllib.request
from multiprocessing import Pool
import ngx_bot

B = "https://doclib.ngxgroup.com/DownloadsContent/"
SUMMARY = "DAILY SUMMARY FOR {d}.pdf"
OFFICIAL = "Daily Official List - Equities for {d}.pdf"
UA = ngx_bot.UA
ROW = re.compile(r"^\d+\s+(.+?)\s+([\d,]+\.\d+)\s+([\d,]+\.\d+)\s+(-?[\d,.]+|-)\s+([\d,]+)\s+([\d,]+)$")
HEAD = re.compile(r"^\d+\s+[A-Z(]")
OLROW = re.compile(r"^([A-Z][A-Z0-9]{2,})\s+(.+?)\s+\d+\.\d{2}\s")
STOP = {"PLC", "LTD", "LIMITED", "THE", "OF", "AND", "CO", "COMPANY"}


def download(tpl, d):
    url = B + urllib.parse.quote(tpl.format(d=d.strftime("%d-%m-%Y")))
    for attempt in range(2):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return None
        except Exception:
            time.sleep(2)
    return None


def pdf_lines(data):
    import pdfplumber
    out = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            out += (page.extract_text() or "").splitlines()
    return out


def parse_summary(args):
    d, data = args
    rows, pend = {}, None
    try:
        lines = pdf_lines(data)
    except Exception:
        return d, rows
    for ln in (x.strip() for x in lines):
        m = ROW.match(ln)
        if not m and pend:
            m = ROW.match(pend + " " + ln)
        pend = ln if (not m and HEAD.match(ln)) else None
        if m:
            name, _mc, price, pc, _tr, vol = m.groups()
            pcv = 0.0 if pc == "-" else float(pc.replace(",", ""))
            rows.setdefault(name.strip(), (float(price.replace(",", "")), pcv, int(vol.replace(",", ""))))
    return d, rows


def toks(name):
    return {t for t in re.sub(r"[^A-Z0-9 ]", " ", name.upper()).split() if t not in STOP}


def build_matcher(live_syms, live_names, official):
    cands = [(sym, toks(nm)) for sym, nm in official + live_names if toks(nm)]
    cache = {}

    def match(name):
        if name in cache:
            return cache[name]
        t, best, bs = toks(name), None, 0.0
        for sym, ct in cands:
            inter = len(ct & t)
            if not inter:
                continue
            sc = inter / len(ct | t) + (0.5 if (ct <= t or t <= ct) else 0)
            if sc > bs:
                best, bs = sym, sc
        if bs < 0.6:
            best, br = None, 0.0
            for sym, ct in cands:
                r = difflib.SequenceMatcher(None, " ".join(sorted(t)), " ".join(sorted(ct))).ratio()
                if r > br:
                    best, br = sym, r
            if br < 0.86:
                best = None
        cache[name] = best
        return best
    return match


def main():
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 520
    q, _ = ngx_bot.fetch()
    live_syms = set(q)
    live_names = [(s, r["name"]) for s, r in q.items() if r["name"].upper() != s]
    # dates: newest weekday backwards
    d = datetime.date.today()
    dates = []
    while len(dates) < days:
        if d.weekday() < 5:
            dates.append(d)
        d -= datetime.timedelta(days=1)
    pdfs, missing = [], 0
    for dt in dates:
        data = download(SUMMARY, dt)
        if data and data[:4] == b"%PDF":
            pdfs.append((dt.isoformat(), data))
        else:
            missing += 1
        time.sleep(0.4)
    print(f"downloaded {len(pdfs)} PDFs, {missing} dates without a file (weekends excluded; holidays expected)")
    # official list (latest available) for symbol <-> name hints
    official = []
    for dt in dates[:10]:
        data = download(OFFICIAL, dt)
        if data and data[:4] == b"%PDF":
            for ln in pdf_lines(data):
                m = OLROW.match(ln.strip())
                if m and m.group(1) in live_syms:
                    official.append((m.group(1), m.group(2)))
            break
    match = build_matcher(live_syms, live_names, official)
    with Pool() as pool:
        parsed = pool.map(parse_summary, pdfs, chunksize=4)
    hist, unmatched, total_rows = {}, {}, 0
    for dt, rows in sorted(parsed):
        for name, (price, pc, vol) in rows.items():
            total_rows += 1
            sym = match(name)
            if sym:
                hist.setdefault(sym, {})[dt] = [dt, price, vol, pc]
            else:
                u = unmatched.setdefault(name, [0, 0.0])
                u[0] += 1
                u[1] += price * vol
    out = {s: [v for _, v in sorted(rows.items())] for s, rows in hist.items()}
    json.dump(out, open("ngx_history.json", "w"), separators=(",", ":"))
    top_un = sorted(unmatched.items(), key=lambda kv: -kv[1][1])[:20]
    rep = {"pdfs": len(pdfs), "dates_without_file": missing, "rows_parsed": total_rows,
           "stocks_matched": len(out), "live_symbols": len(live_syms),
           "official_pairs": len(official), "live_name_pairs": len(live_names),
           "first_date": min((r[0][0] for r in out.values()), default=None),
           "last_date": max((r[-1][0] for r in out.values()), default=None),
           "unmatched_names": len(unmatched),
           "top_unmatched_by_value": [[k, v[0], round(v[1] / max(v[0], 1))] for k, v in top_un],
           "watch_found": {w: len(out.get(w, [])) for w in ngx_bot.WATCH}}
    json.dump(rep, open("backfill_report.json", "w"), indent=1)
    print(json.dumps(rep, indent=1))


if __name__ == "__main__":
    main()
