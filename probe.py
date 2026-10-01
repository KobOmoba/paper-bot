"""Probe: can we download + read NGX daily price-list PDFs for past dates?"""
import io, json, time, urllib.request, urllib.parse, traceback, datetime
UA = {"User-Agent": "Mozilla/5.0 (compatible; ngx-paper-bot; educational)"}
B = "https://doclib.ngxgroup.com/DownloadsContent/"
NAMES = ["DAILY SUMMARY FOR {d}.pdf", "Daily Summary for {d}.pdf",
         "Daily Official List - Equities for {d}.pdf"]
DATES = [datetime.date(2026, 9, 30), datetime.date(2026, 6, 15), datetime.date(2026, 1, 30),
         datetime.date(2025, 9, 30), datetime.date(2025, 1, 31), datetime.date(2024, 12, 31)]
rep = {"attempts": [], "samples": {}}

def fetch(name, d):
    url = B + urllib.parse.quote(name.format(d=d.strftime("%d-%m-%Y")))
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40)
        data = r.read()
        return r.status, data, url
    except Exception as e:
        return str(e)[:60], b"", url

for d in DATES:
    for nm in NAMES:
        st, data, url = fetch(nm, d)
        rep["attempts"].append([str(d), nm.split(" ")[0] + (" OL" if "Official" in nm else ""), st, len(data)])
        key = nm.split("{")[0]
        if st == 200 and data[:4] == b"%PDF" and key not in rep["samples"]:
            try:
                import pdfplumber
                with pdfplumber.open(io.BytesIO(data)) as pdf:
                    txt = pdf.pages[0].extract_text() or ""
                    rep["samples"][key] = {"date": str(d), "pages": len(pdf.pages),
                                           "text_head": txt[:2200]}
            except Exception:
                rep["samples"][key] = {"date": str(d), "err": traceback.format_exc()[-300:]}
        time.sleep(1)
json.dump(rep, open("probe_report.json", "w"), indent=1)
print("done")
