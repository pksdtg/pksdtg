"""Daily-chart patterns for buy-today-sell-tomorrow: cup and handle, 52-week-high breakout."""
import importlib.util, sys, json, datetime as dt, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor
spec = importlib.util.spec_from_file_location("pk", sys.argv[1]); pk = importlib.util.module_from_spec(spec); spec.loader.exec_module(pk)
p, tm = pk.piyush, pk.trader_market
now = p.parse_now(None)
syms, total = pk.liquid_universe("trader/universe.txt", today=now.date())
IST = dt.timedelta(hours=5, minutes=30)
def daily(sym):
    ys = urllib.parse.quote(tm.nse_yahoo(sym), safe=".^-")
    req = urllib.request.Request("https://query2.finance.yahoo.com/v8/finance/chart/%s?range=1y&interval=1d" % ys, headers={"User-Agent": p.UA})
    r = json.loads(urllib.request.urlopen(req, timeout=30).read())["chart"]["result"][0]
    q = r["indicators"]["quote"][0]; meta = r["meta"]; rows = []
    for i, t in enumerate(r["timestamp"]):
        o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
        d = (dt.datetime(1970, 1, 1) + dt.timedelta(seconds=t) + IST).date()
        if c is None and d == now.date() and meta.get("regularMarketPrice"):
            c = meta["regularMarketPrice"]; h = max(h or c, meta.get("regularMarketDayHigh") or c); l = min(l or c, meta.get("regularMarketDayLow") or c); o = o or c
        if None in (o, h, l, c): continue
        rows.append((d, o, h, l, c, v or 0))
    return rows
def cup_handle(rows):
    """Last close near/above the pivot of a cup (20-130 bars, 12-35% deep, rims within 5%)
    followed by a 3-20 bar handle in the cup's upper half that pulled back <= 12%."""
    n = len(rows); c = [r[4] for r in rows]; h = [r[2] for r in rows]; l = [r[3] for r in rows]
    best = None
    for hl in range(3, 21):                       # handle length
        hs = n - hl                               # handle start = right rim index
        if hs < 25: break
        R = max(h[hs - 3:hs + 1]); ri = hs - 3 + h[hs - 3:hs + 1].index(R)
        for clen in range(20, 131, 5):
            li = ri - clen
            if li < 0: break
            L = h[li]
            if abs(R / L - 1) > 0.05: continue
            seg = l[li:ri + 1]; B = min(seg); bi = li + seg.index(B)
            depth = (min(L, R) - B) / min(L, R)
            if not (0.12 <= depth <= 0.35): continue
            if not (li + clen * 0.25 <= bi <= ri - clen * 0.25): continue  # bottom in the middle half: a U, not a V
            if max(h[li + 1:ri]) > max(L, R) * 1.02: continue             # nothing poked above the rims
            hlow = min(l[ri:n - 1]) if ri < n - 1 else l[ri]   # handle: rim to yesterday
            hhigh = max(h[ri + 1:n - 1]) if ri + 1 < n - 1 else R
            if hhigh > R * 1.02: continue                       # the handle stayed under the rim
            pull = (R - hlow) / R
            if pull > 0.12 or hlow < B + (min(L, R) - B) / 2: continue    # handle in the upper half
            pivot = max(R, hhigh)
            last = c[-1]
            if not (pivot * 0.97 <= last <= pivot * 1.05): continue      # fresh: at the pivot, not long past it
            vols = [r[5] for r in rows]; avg50 = sum(vols[-51:-1]) / 50 if n > 51 else 0
            cand = dict(pivot=pivot, left=L, right=R, bottom=B, depth=depth * 100, cup_bars=clen, handle_bars=hl,
                        handle_low=hlow, pull=pull * 100, last=last, broke=last > pivot,
                        vol_x=(vols[-1] / avg50) if avg50 else None, left_date=str(rows[li][0]), bottom_date=str(rows[bi][0]))
            if best is None or cand["depth"] < best["depth"]: best = cand
    return best
def high52(rows):
    c = [r[4] for r in rows]; h = [r[2] for r in rows]; vols = [r[5] for r in rows]
    if len(rows) < 200: return None
    prior = max(h[:-1]); avg50 = sum(vols[-51:-1]) / 50
    if c[-1] > prior and avg50 and vols[-1] >= 1.5 * avg50:
        return dict(prior_high=prior, last=c[-1], vol_x=vols[-1] / avg50, chg=(c[-1] / c[-2] - 1) * 100)
def one(sym):
    try: rows = daily(sym)
    except Exception: return sym, None, None, None
    if len(rows) < 60: return sym, None, None, None
    return sym, rows, cup_handle(rows), high52(rows)
with ThreadPoolExecutor(8) as ex: res = list(ex.map(one, syms))
ok = [r for r in res if r[1]]
gaps = {}
for sym, rows, ch, hi in ok:
    g = sorted((rows[i][1] / rows[i - 1][4] - 1) * 100 for i in range(max(1, len(rows) - 120), len(rows)))
    gaps[sym] = g[len(g) // 20]  # 5th percentile overnight gap, last ~6 months
print("as of", now.strftime("%Y-%m-%d %H:%M IST"), "checked", len(ok), "of", len(syms), "liquid")
cups = [(s, rows, ch) for s, rows, ch, _ in ok if ch]
print("CUP AND HANDLE:", len(cups))
for s, rows, ch in sorted(cups, key=lambda x: (not x[2]["broke"], -(x[2]["vol_x"] or 0)))[:15]:
    stop = ch["handle_low"]; risk = ch["last"] - stop
    qty_loss = int(2000 // risk) if risk > 0 else 0
    qty_gap = int(2000 // (ch["last"] * abs(min(gaps[s], -0.5)) / 100))
    qty = max(0, min(qty_loss, qty_gap, int(75000 // ch["last"])))
    tgt = ch["pivot"] + (ch["pivot"] - ch["bottom"])
    print("  %-11s last %9.2f pivot %9.2f %s vol %.1fx | cup %s->%s depth %.0f%% %d bars, handle %d bars pull %.1f%% low %.2f | stop %.2f (%.1f%%) qty %d = Rs %.0f loss@stop Rs %.0f, 5%%-worst gap %.1f%% | measured target %.2f (+%.0f%%)" % (
        s, ch["last"], ch["pivot"], "BROKE OUT" if ch["broke"] else "near pivot", ch["vol_x"] or 0, ch["left_date"], ch["bottom_date"], ch["depth"], ch["cup_bars"], ch["handle_bars"], ch["pull"], ch["handle_low"],
        stop, risk / ch["last"] * 100, qty, qty * ch["last"], qty * risk, gaps[s], tgt, (tgt / ch["last"] - 1) * 100))
his = [(s, hi) for s, rows, _, hi in ok if hi]
print("52-WEEK HIGH BREAKOUT on 1.5x+ volume:", len(his))
for s, hi in sorted(his, key=lambda x: -x[1]["vol_x"])[:12]:
    print("  %-11s last %9.2f prior 52w high %9.2f day %+.1f%% vol %.1fx, 5%%-worst gap %.1f%%" % (s, hi["last"], hi["prior_high"], hi["chg"], hi["vol_x"], gaps[s]))
