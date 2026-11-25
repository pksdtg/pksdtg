"""NIFTY option greeks (Black-Scholes from NSE's IV) and a 'worth holding' screen for buyers."""
import sys, json, math, datetime as dt, importlib.util
sys.path.insert(0, sys.argv[1]); import trader_market as tm
spec = importlib.util.spec_from_file_location("p", sys.argv[1] + "/trader-piyush/piyush.py"); p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
R, Q, LOT = 0.065, 0.012, 65  # risk-free ~6.5%, NIFTY dividend yield ~1.2%, lot 65
N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
n = lambda x: math.exp(-x * x / 2) / math.sqrt(2 * math.pi)
def greeks(S, K, T, iv, typ):
    s = iv / 100; d1 = (math.log(S / K) + (R - Q + s * s / 2) * T) / (s * math.sqrt(T)); d2 = d1 - s * math.sqrt(T)
    if typ == "CE":
        delta = math.exp(-Q * T) * N(d1); theta = (-S * math.exp(-Q * T) * n(d1) * s / (2 * math.sqrt(T)) - R * K * math.exp(-R * T) * N(d2) + Q * S * math.exp(-Q * T) * N(d1)) / 365
    else:
        delta = -math.exp(-Q * T) * N(-d1); theta = (-S * math.exp(-Q * T) * n(d1) * s / (2 * math.sqrt(T)) + R * K * math.exp(-R * T) * N(-d2) - Q * S * math.exp(-Q * T) * N(-d1)) / 365
    gamma = math.exp(-Q * T) * n(d1) / (S * s * math.sqrt(T)); vega = S * math.exp(-Q * T) * n(d1) * math.sqrt(T) / 100
    return delta, gamma, theta, vega
now = p.parse_now(None)
# NIFTY trend: daily (EMA 20/50, Supertrend 7,1.6 on daily bars) and today's 5-minute pk side
import urllib.request
r = json.loads(urllib.request.urlopen(urllib.request.Request("https://query2.finance.yahoo.com/v8/finance/chart/%5ENSEI?range=1y&interval=1d", headers={"User-Agent": p.UA}), timeout=30).read())["chart"]["result"][0]
q = r["indicators"]["quote"][0]
db = [(None, o, h, l, c) for o, h, l, c in zip(q["open"], q["high"], q["low"], q["close"]) if None not in (o, h, l, c)]
if r["meta"].get("regularMarketPrice"): db[-1] = (None, db[-1][1], max(db[-1][2], r["meta"]["regularMarketPrice"]), min(db[-1][3], r["meta"]["regularMarketPrice"]), r["meta"]["regularMarketPrice"])
dc = [b[4] for b in db]; e20, e50 = p.ema(dc, 20)[-1], p.ema(dc, 50)[-1]; dst = p.supertrend(db, 7, 1.6)[-1]; drsi = p.rsi(dc)[-1]
m = p.evaluate("NIFTY 50", p.fetch_bars(p.INDEX), now)
daily_up = dc[-1] > e20 > e50 and dst[1]
daily_down = dc[-1] < e20 < e50 and not dst[1]
print("NIFTY daily: close %.2f EMA20 %.2f EMA50 %.2f Supertrend(7,1.6) %s %.2f RSI %.1f -> %s" % (dc[-1], e20, e50, "up" if dst[1] else "down", dst[0], drsi, "UP" if daily_up else "DOWN" if daily_down else "MIXED"))
print("NIFTY 5-min: fast %s slow %s RSI %.1f SMI %.1f sig %.1f" % (m["st_fast"][1], m["st_slow"][1], m["rsi"], m["smi"], m["signal"]))
s = tm.NSESession(); exps = s.api("option-chain-contract-info?symbol=NIFTY")["expiryDates"][:5]
rows = []
for e in exps:
    d = s.api("option-chain-v3?type=Indices&symbol=NIFTY&expiry=%s" % e); rec = d["records"]; S = rec["underlyingValue"]
    ed = dt.datetime.strptime(e, "%d-%b-%Y").replace(hour=15, minute=30); T = max((ed - now).total_seconds(), 3600) / (365 * 86400); days = (ed - now).total_seconds() / 86400
    for x in rec["data"]:
        K = x["strikePrice"]
        if abs(K / S - 1) > 0.04: continue
        for typ in ("CE", "PE"):
            o = x.get(typ) or {}; ltp, iv = o.get("lastPrice"), o.get("impliedVolatility")
            bid, ask = o.get("buyPrice1"), o.get("sellPrice1")
            if not ltp or not iv or not bid or not ask: continue
            de, ga, th, ve = greeks(S, K, T, iv, typ); mid = (bid + ask) / 2
            rows.append(dict(exp=e, days=days, K=K, typ=typ, ltp=ltp, iv=iv, delta=de, gamma=ga, theta=th, vega=ve, spread=(ask - bid) / mid * 100,
                             oi=o.get("openInterest") or 0, vol=o.get("totalTradedVolume") or 0, theta_pct=abs(th) / ltp * 100,
                             be=K + ltp if typ == "CE" else K - ltp, S=S))
print("spot %.2f, %d contracts read across %s" % (rows[0]["S"], len(rows), ", ".join(exps)))
atm = {}
for e in exps:
    near = sorted([x for x in rows if x["exp"] == e], key=lambda x: abs(x["K"] - x["S"]))[:4]
    atm[e] = sum(x["iv"] for x in near) / len(near) if near else None
    print("  %s: ATM IV %.2f, days left %.1f" % (e, atm[e] or 0, near[0]["days"] if near else 0))
def ok(x):
    return 0.40 <= abs(x["delta"]) <= 0.65 and x["days"] >= 6 and x["theta_pct"] <= 3.0 and x["spread"] <= 1.0 and x["oi"] >= 5000
good = [x for x in rows if ok(x)]
for typ in ("CE", "PE"):
    lst = sorted([x for x in good if x["typ"] == typ], key=lambda x: (x["theta_pct"] + abs(abs(x["delta"]) - 0.5) * 4 + x["spread"]))
    print("HOLD-WORTHY %s (delta 0.40-0.65, >=6 days, theta <=3%%/day, spread <=1%%, OI >=5000): %d" % (typ, len(lst)))
    for x in lst[:8]:
        print("  %s %d %s ltp %.2f (1 lot Rs %.0f) | delta %+.2f gamma %.5f theta %.2f/day (%.1f%%, Rs %.0f/lot) vega %.2f | IV %.2f (ATM %.2f) | spread %.2f%% OI %d | days %.1f | breakeven at expiry %.0f (%+.1f%%)" % (
            x["exp"], x["K"], typ, x["ltp"], x["ltp"] * LOT, x["delta"], x["gamma"], x["theta"], x["theta_pct"], x["theta"] * LOT, x["vega"], x["iv"], atm[x["exp"]], x["spread"], x["oi"], x["days"], x["be"], (x["be"] / x["S"] - 1) * 100))
print("PER EXPIRY, delta ~0.5 CE and PE, and the Rs 100-150 strikes:")
for e in exps:
    for typ in ("CE", "PE"):
        xs = [x for x in rows if x["exp"] == e and x["typ"] == typ]
        if not xs: continue
        a = min(xs, key=lambda x: abs(abs(x["delta"]) - 0.5))
        c = [x for x in xs if 100 <= x["ltp"] <= 150]
        c = min(c, key=lambda x: abs(x["ltp"] - 125)) if c else None
        print("  %s %s | ATM-ish %d ltp %.1f delta %+.2f theta %.1f/day (%.1f%%) spread %.2f%% | Rs100-150: %s" % (e, typ, a["K"], a["ltp"], a["delta"], a["theta"], a["theta_pct"], a["spread"],
              "%d ltp %.1f delta %+.2f theta %.1f/day (%.1f%%) BE %+.1f%%" % (c["K"], c["ltp"], c["delta"], c["theta"], c["theta_pct"], (c["be"] / c["S"] - 1) * 100) if c else "-"))
