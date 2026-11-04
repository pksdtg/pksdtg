import importlib.util, datetime as d, sys, time, statistics as st
from collections import defaultdict
spec = importlib.util.spec_from_file_location("p", sys.argv[1]); p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
syms = sys.argv[2].split(",")
def series(sym):
    bars = p.fetch_bars(sym, rng="60d")
    c = [b[4] for b in bars]
    f, s = p.supertrend(bars, 7, 0.8), p.supertrend(bars, 7, 1.6)
    r = p.rsi(c); m, sg = p.smi(bars)
    e9, e21 = p.ema(c, 9), p.ema(c, 21); _, _, hist = p.macd(c); ax, pdi, mdi = p.adx(bars)
    vw, cum_pv, cum_v, day = [], 0.0, 0.0, None
    for b in bars:
        if b[0].date() != day: day, cum_pv, cum_v = b[0].date(), 0.0, 0.0
        cum_pv += (b[2] + b[3] + b[4]) / 3 * b[5]; cum_v += b[5]
        vw.append(cum_pv / cum_v if cum_v else None)
    vol = [b[5] for b in bars]
    def score(i):
        oks = [c[i] > vw[i] if vw[i] else None, e9[i] > e21[i], hist[i] > 0,
               (ax[i] > 20 and pdi[i] > mdi[i]) if ax[i] is not None else None,
               (vol[i] >= 1.5 * sum(vol[i-20:i]) / 20) if i >= 20 and sum(vol[i-20:i]) > 0 else None]
        return sum(1 for o in oks if o)
    return bars, f, s, r, m, sg, score
nb, nf, ns, nr, _, _, _ = series("^NSEI")
nifty = {b[0]: (nf[i][1] and ns[i][1] and nr[i] is not None and nr[i] > 50) for i, b in enumerate(nb)}
lo, hi, flat = d.time(9, 30), d.time(15, 0), d.time(15, 10)
trades = []
for sym in syms:
    time.sleep(0.3)
    bars, f, s, r, m, sg, score = series(sym)
    taken = set()
    for i in range(60, len(bars)):
        t = bars[i][0]; end = t + p.BAR; day = t.date()
        if day in taken or not (lo <= end.time() <= hi): continue
        if not (f[i][1] and s[i][1] and r[i] > 50 and m[i] is not None and sg[i] is not None and m[i] > sg[i] and nifty.get(t)): continue
        entry, stop = bars[i][4], int(s[i][0] * 100) / 100.0; risk = entry - stop
        if risk <= 0: continue
        target = entry + 2 * risk; taken.add(day); out = None
        for j in range(i + 1, len(bars)):
            b = bars[j]
            if b[0].date() != day: break
            if b[3] <= stop: out = -1.0; break
            if b[2] >= target: out = 2.0; break
            if (b[0] + p.BAR).time() >= flat: out = (b[4] - entry) / risk; break
        if out is None: continue
        trades.append((sym, score(i), risk / entry * 100, out))
by = defaultdict(list)
for t in trades: by[t[1]].append(t)
print("trades", len(trades))
for k in sorted(by):
    R = [t[3] for t in by[k]]; cost = st.mean(0.05 / t[2] for t in by[k])
    print("  confirm %d/5: %4d trades  win %.1f%%  avg R %+.3f  cost about %.2fR  after costs %+.3fR" % (k, len(R), 100 * sum(x > 0 for x in R) / len(R), st.mean(R), cost, st.mean(R) - cost))
for th in (3, 4):
    sel = [t for t in trades if t[1] >= th]; R = [t[3] for t in sel]; cost = st.mean(0.05 / t[2] for t in sel)
    print("  confirm >= %d: %d trades  win %.1f%%  avg R %+.3f  after costs %+.3fR" % (th, len(R), 100 * sum(x > 0 for x in R) / len(R), st.mean(R), st.mean(R) - cost))
