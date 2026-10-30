"""NSE daily scan: universe + Oct 29 liquidity from NSE's bhavcopy, history from Yahoo (.NS),
setups from trader-scanner's own find_setups. Dates are India time (IST)."""
import csv, io, json, sys, zipfile, importlib.util, datetime as dt
from collections import Counter
scan_py, bhav_zip, out_json, cutoff = sys.argv[1], sys.argv[2], sys.argv[3], dt.date.fromisoformat(sys.argv[4])
spec = importlib.util.spec_from_file_location("scan", scan_py); scan = importlib.util.module_from_spec(spec); spec.loader.exec_module(scan)
scan.to_yahoo = lambda s: s  # keep RELIANCE.NS as is
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
MIN_PRICE, MIN_TURNOVER = 20.0, 10e7  # Rs 20 and Rs 10 crore traded on the cutoff day

z = zipfile.ZipFile(bhav_zip); rows = list(csv.DictReader(io.StringIO(z.read(z.namelist()[0]).decode())))
eq = [r for r in rows if r["SctySrs"] in ("EQ", "BE")]
liquid = {r["TckrSymb"]: r for r in eq if float(r["ClsPric"]) >= MIN_PRICE and float(r["TtlTrfVal"]) >= MIN_TURNOVER}
syms = sorted(s + ".NS" for s in liquid)
print("NSE bhavcopy %s: %d EQ/BE stocks, %d with close >= Rs %d and turnover >= Rs 10 crore." % (cutoff, len(eq), len(liquid), MIN_PRICE))
data, failed = scan.fetch_spark(syms, "6mo", "1d", 0.3)
print("Yahoo: fetched %d of %d charts (%d failed or had no data)." % (len(data), len(syms), len(failed)))
hits, short, nxt = [], 0, {}
for s, ser in data.items():
    bars = {}
    for t, c in zip(ser["t"], ser["c"]):
        if c is not None:
            bars[dt.datetime.fromtimestamp(t, IST).date()] = float(c)
    days = sorted(bars)
    upto = [bars[d] for d in days if d <= cutoff]
    after = [d for d in days if d > cutoff]
    if not days or max(d for d in days if d <= cutoff) != cutoff if any(d <= cutoff for d in days) else True:
        short += 1; continue
    if len(upto) < scan.MIN_DAILY_BARS:
        short += 1; continue
    tags, n = scan.find_setups(upto)
    if tags:
        b = liquid[s[:-3]]
        h = {"symbol": s[:-3], "name": b["FinInstrmNm"], "close": upto[-1], "turnover_cr": float(b["TtlTrfVal"]) / 1e7,
             "rsi": n["rsi"], "setups": tags}
        if after:
            d1 = after[0]; h["next_day"] = d1.isoformat(); h["next_close"] = bars[d1]; h["next_chg_pct"] = (bars[d1] / upto[-1] - 1) * 100
        hits.append(h)
hits.sort(key=lambda h: -h["turnover_cr"])
print("%d scanned (%d skipped: short history or no %s bar). %d matched a setup:" % (len(data) - short, short, cutoff, len(hits)))
c = Counter(t for h in hits for t in h["setups"])
for k, _ in scan.SETUPS:
    names = [h["symbol"] for h in hits if k in h["setups"]]
    print("  %-16s %3d  %s" % (k, c[k], ", ".join(names[:8])))
json.dump({"created_by": "trader-scanner (NSE wrapper)", "mode": "daily", "market": "NSE", "as_of": "%s close" % cutoff,
           "universe": len(syms), "scanned": len(data) - short, "failed": len(failed), "source": "NSE bhavcopy + Yahoo .NS",
           "note": scan.NOTE, "hits": hits}, open(out_json, "w"), indent=2)
print("Wrote", out_json)
