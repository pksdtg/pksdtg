"""Rank today's NSE scan hits by risk, for a given capital and max loss.
1) Base rate: for every liquid NSE stock, every day of the last year where a setup fired
   (trader-scanner's own find_setups on closes), what the next session did
   (open vs close = overnight gap, next close vs close).
2) Per hit: ATR14 (from v8 daily OHLC), worst overnight gap down in a year, its own history
   of the same setup, and the size that fits the max loss with a 1-ATR stop."""
import importlib.util, json, sys, os, statistics as st, datetime as dt
from collections import defaultdict
scan_py, wl_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
CAPITAL, MAX_LOSS, POS_CAP = 100000.0, 2000.0, 75000.0
os.environ["TRADER_MARKET"] = "NSE"
spec = importlib.util.spec_from_file_location("scan", scan_py); scan = importlib.util.module_from_spec(spec); spec.loader.exec_module(scan)
IST = dt.timedelta(hours=5, minutes=30)
import csv, io, zipfile
_z = zipfile.ZipFile(sys.argv[4]); BHAV_DAY = dt.date(2026, 11, 3)
BHAV = {r["TckrSymb"]: r for r in csv.DictReader(io.StringIO(_z.read(_z.namelist()[0]).decode())) if r["SctySrs"] in ("EQ", "BE")}
wl = json.load(open(wl_path)); hits = wl["hits"]; syms = [h["symbol"] for h in hits]

def ohlc(sym):
    d = json.loads(scan.yahoo_get(scan.chart_path(sym, "1y", "1d")))["chart"]["result"][0]
    q = d["indicators"]["quote"][0]; rows = []
    for i, t in enumerate(d["timestamp"]):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None not in (o, h, l, c):
            rows.append(((dt.datetime(1970, 1, 1) + dt.timedelta(seconds=t) + IST).date(), o, h, l, c))
    b = BHAV.get(sym)
    if b and (not rows or rows[-1][0] < BHAV_DAY):  # Yahoo left the last daily bar empty: NSE's own bhavcopy
        rows.append((BHAV_DAY, float(b["OpnPric"]), float(b["HghPric"]), float(b["LwPric"]), float(b["ClsPric"])))
    return rows

base = defaultdict(lambda: {"n": 0, "up": 0, "ret": [], "gap": []})
rows_out = []
for k, sym in enumerate(syms):
    try:
        r = ohlc(sym)
    except Exception as e:
        continue
    if k: scan.SLEEP(0.25)
    if len(r) < 60: continue
    closes = [x[4] for x in r]
    own = defaultdict(lambda: {"n": 0, "up": 0, "ret": []})
    for i in range(30, len(r) - 1):
        tags, _ = scan.find_setups(closes[:i + 1])
        nxt_ret = (r[i + 1][4] / closes[i] - 1) * 100
        gap = (r[i + 1][1] / closes[i] - 1) * 100
        for t in tags:
            b = base[t]; b["n"] += 1; b["up"] += nxt_ret > 0; b["ret"].append(nxt_ret); b["gap"].append(gap)
            o = own[t]; o["n"] += 1; o["up"] += nxt_ret > 0; o["ret"].append(nxt_ret)
    trs = [max(r[i][2] - r[i][3], abs(r[i][2] - r[i - 1][4]), abs(r[i][3] - r[i - 1][4])) for i in range(len(r) - 14, len(r))]
    atr = sum(trs) / 14; price = closes[-1]
    gaps = [(r[i][1] / r[i - 1][4] - 1) * 100 for i in range(1, len(r))]
    h = hits[k]
    qty_loss = int(MAX_LOSS // atr); qty_cap = int(POS_CAP // price)
    gap5 = sorted(gaps)[max(0, len(gaps) // 20 - 1)]  # 5th percentile overnight gap
    qty_gap = int(MAX_LOSS // (price * abs(min(gap5, -0.01)) / 100))
    rows_out.append({"symbol": sym, "price": price, "setups": h["setups"], "rsi": h["rsi"],
        "atr": atr, "atr_pct": atr / price * 100, "worst_gap": min(gaps), "gap_p5": gap5,
        "own": {t: {"n": own[t]["n"], "up": own[t]["up"], "avg": st.mean(own[t]["ret"]) if own[t]["ret"] else None} for t in h["setups"]},
        "qty_day": min(qty_loss, qty_cap), "qty_overnight": min(qty_loss, qty_cap, qty_gap),
        "turnover": price * (h.get("volume") or 0)})
    if (k + 1) % 50 == 0: print("  %d of %d" % (k + 1, len(syms)), file=sys.stderr)
b_out = {t: {"n": b["n"], "up_pct": b["up"] / b["n"] * 100, "median_ret": st.median(b["ret"]), "mean_ret": st.mean(b["ret"]),
             "gap_p5": sorted(b["gap"])[len(b["gap"]) // 20]} for t, b in base.items()}
json.dump({"base": b_out, "rows": rows_out}, open(out_path, "w"), indent=1)
print("done", len(rows_out))
