import importlib.util, datetime as d, sys, time, statistics as st
from collections import defaultdict
spec = importlib.util.spec_from_file_location("p", sys.argv[1]); p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
syms = sys.argv[2].split(",")
def series(sym):
    bars = p.fetch_bars(sym, rng="60d")
    f, s = p.supertrend(bars, 7, 0.8), p.supertrend(bars, 7, 1.6)
    r = p.rsi([b[4] for b in bars]); m, sg = p.smi(bars)
    return bars, f, s, r, m, sg
nb, nf, ns, nr, _, _ = series("^NSEI")
nifty = {b[0]: (nf[i][1] and ns[i][1] and nr[i] is not None and nr[i] > 50) for i, b in enumerate(nb)}
lo, hi, flat = d.time(9, 30), d.time(15, 0), d.time(15, 10)
trades = []
for sym in syms:
    time.sleep(0.3)
    bars, f, s, r, m, sg = series(sym)
    taken = set()
    for i in range(60, len(bars)):
        t = bars[i][0]; end = t + p.BAR; day = t.date()
        if day in taken or not (lo <= end.time() <= hi):
            continue
        if not (f[i][1] and s[i][1] and r[i] > 50 and m[i] is not None and sg[i] is not None and m[i] > sg[i] and nifty.get(t)):
            continue
        entry, stop = bars[i][4], int(s[i][0] * 100) / 100.0
        risk = entry - stop
        if risk <= 0: continue
        target = entry + 2 * risk; taken.add(day); out = None
        for j in range(i + 1, len(bars)):
            b = bars[j]
            if b[0].date() != day: break
            if b[3] <= stop: out = -1.0; break
            if b[2] >= target: out = 2.0; break
            if (b[0] + p.BAR).time() >= flat: out = (b[4] - entry) / risk; break
        if out is None: continue
        trades.append((sym, day, end.strftime("%H:%M"), risk / entry * 100, out))
R = [t[4] for t in trades]
print("trades", len(trades), "symbols", len(syms))
print("target hit %d, stop hit %d, closed at 15:10 %d" % (sum(x == 2 for x in R), sum(x == -1 for x in R), sum(-1 < x < 2 for x in R)))
print("win rate (R>0) %.1f%%  average R %+.3f  median R %+.2f  median stop distance %.2f%%" % (
    100 * sum(x > 0 for x in R) / len(R), st.mean(R), st.median(R), st.median(t[3] for t in trades)))
per = defaultdict(list)
for t in trades: per[t[0]].append(t[4])
for k, v in per.items(): print("  %-11s %2d trades  avg R %+.2f  wins %d" % (k, len(v), st.mean(v), sum(x > 0 for x in v)))
