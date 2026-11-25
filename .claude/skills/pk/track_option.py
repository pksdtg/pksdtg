#!/usr/bin/env python3
"""Track an open NIFTY option position: premium, P&L, greeks and NIFTY's trend.

    python3 track_option.py --expiry 29-Dec-2026 --strike 25900 --type CE --entry 470.75 --lots 1
    python3 track_option.py ... --events

Prints one status block. With --events it also prints an EVENT line for anything new
since the last run (state in trader/state/track_option.json): premium at the max-loss
level, NIFTY closing a 5-minute bar under its daily Supertrend (7, 1.6), NIFTY's
5-minute side changing, P&L crossing another Rs 1,000 step, and the last check of the
day (after 15:20). Exit code 0 when there is an EVENT. It reads prices only; it never
places or changes an order.
"""
import argparse
import datetime
import importlib.util
import json
import math
import os
import sys
import urllib.request

SKILLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("piyush", os.path.join(SKILLS, "trader-piyush", "piyush.py"))
piyush = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(piyush)
sys.path.insert(0, SKILLS)
import trader_market  # noqa: E402

STATE = "trader/state/track_option.json"
LOT = 65
RATE, DIV = 0.065, 0.012  # risk-free rate, NIFTY dividend yield (Black-Scholes)
STEP = 1000


def rs(x):
    return "{:+,.0f}".format(x)


def bs_greeks(spot, strike, years, iv, typ):
    s = iv / 100.0
    d1 = (math.log(spot / strike) + (RATE - DIV + s * s / 2) * years) / (s * math.sqrt(years))
    d2 = d1 - s * math.sqrt(years)
    cdf = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
    pdf = lambda x: math.exp(-x * x / 2) / math.sqrt(2 * math.pi)
    disc_q, disc_r = math.exp(-DIV * years), math.exp(-RATE * years)
    if typ == "CE":
        delta = disc_q * cdf(d1)
        theta = (-spot * disc_q * pdf(d1) * s / (2 * math.sqrt(years)) - RATE * strike * disc_r * cdf(d2)
                 + DIV * spot * disc_q * cdf(d1)) / 365
    else:
        delta = -disc_q * cdf(-d1)
        theta = (-spot * disc_q * pdf(d1) * s / (2 * math.sqrt(years)) + RATE * strike * disc_r * cdf(-d2)
                 - DIV * spot * disc_q * cdf(-d1)) / 365
    vega = spot * disc_q * pdf(d1) * math.sqrt(years) / 100
    return delta, theta, vega


def nifty_daily():
    req = urllib.request.Request("https://query2.finance.yahoo.com/v8/finance/chart/%5ENSEI?range=1y&interval=1d",
                                 headers={"User-Agent": piyush.UA})
    r = json.loads(urllib.request.urlopen(req, timeout=30).read())["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    bars = [(None, o, h, l, c) for o, h, l, c in zip(q["open"], q["high"], q["low"], q["close"]) if None not in (o, h, l, c)]
    px = r["meta"].get("regularMarketPrice")
    if px:  # today's bar from the live quote (Yahoo can leave its close empty)
        bars[-1] = (None, bars[-1][1], max(bars[-1][2], px), min(bars[-1][3], px), px)
    # yesterday's daily Supertrend: today's line moves with today's bar
    return piyush.supertrend(bars[:-1], 7, 1.6)[-1]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--expiry", required=True, help="as NSE writes it, e.g. 29-Dec-2026")
    ap.add_argument("--strike", type=float, required=True)
    ap.add_argument("--type", choices=["CE", "PE"], required=True)
    ap.add_argument("--entry", type=float, required=True)
    ap.add_argument("--lots", type=int, default=1)
    ap.add_argument("--max-loss", type=float, help="rupees (default: config max_loss_per_trade_usd)")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--state", default=STATE)
    ap.add_argument("--events", action="store_true")
    a = ap.parse_args(argv)
    now, cfg = piyush.parse_now(None), piyush.read_config(a.config)
    qty = a.lots * LOT
    max_loss = a.max_loss if a.max_loss is not None else float(cfg["max_loss_per_trade_usd"])
    loss_level = round(a.entry - max_loss / qty, 2)

    rec = trader_market.NSESession().api("option-chain-v3?type=Indices&symbol=NIFTY&expiry=%s" % a.expiry)["records"]
    row = next((r for r in rec["data"] if float(r["strikePrice"]) == a.strike), None)
    leg = (row or {}).get(a.type) or {}
    if not leg.get("lastPrice"):
        print("track_option.py: no %s %g %s in NSE's chain for %s" % (a.type, a.strike, a.type, a.expiry), file=sys.stderr)
        return 2
    ltp, iv, spot = leg["lastPrice"], leg.get("impliedVolatility") or 0, rec["underlyingValue"]
    expiry_at = datetime.datetime.strptime(a.expiry, "%d-%b-%Y").replace(hour=15, minute=30)
    years = max((expiry_at - now).total_seconds(), 3600) / (365 * 86400)
    delta, theta, vega = bs_greeks(spot, a.strike, years, iv, a.type) if iv else (0, 0, 0)
    pnl = (ltp - a.entry) * qty
    m = piyush.evaluate("NIFTY 50", piyush.fetch_bars(piyush.INDEX), now)
    side = "up" if (m["st_fast"][1] and m["st_slow"][1] and m["rsi"] > 50 and m["smi"] > m["signal"]) else \
        "down" if (not m["st_fast"][1] and not m["st_slow"][1] and m["rsi"] < 50 and m["smi"] < m["signal"]) else "mixed"
    dst = nifty_daily()
    days_left = (expiry_at - now).total_seconds() / 86400

    print("NIFTY %s %g %s x %d lot(s) @ %.2f | LTP %.2f (NSE %s) | P&L Rs %s (%+.1f%%)"
          % (a.expiry, a.strike, a.type, a.lots, a.entry, ltp, rec.get("timestamp"), rs(pnl), (ltp / a.entry - 1) * 100))
    print("greeks: delta %+.2f (Rs %.0f per NIFTY point), theta %.1f/day (Rs %.0f/day), vega %.1f, IV %.2f, %.1f days left"
          % (delta, delta * qty, theta, theta * qty, vega, iv, days_left))
    print("levels: Rs %s max loss at premium %.2f | NIFTY daily Supertrend (7, 1.6) %s %.2f"
          % ("{:,.0f}".format(max_loss), loss_level, "up" if dst[1] else "down", dst[0]))
    print("NIFTY %.2f (5-min %s bar): Supertrends %s/%s, RSI %.1f, SMI %.1f vs signal %.1f -> %s"
          % (spot, m["bar"].strftime("%H:%M"), "up" if m["st_fast"][1] else "down", "up" if m["st_slow"][1] else "down",
             m["rsi"], m["smi"], m["signal"], side))
    if not a.events:
        return 0
    try:
        with open(a.state, encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        st = {}
    key = "%s|%g|%s|%.2f|%d" % (a.expiry, a.strike, a.type, a.entry, a.lots)
    seen = st.get(key, {"flags": [], "band": 0, "side": None, "eod": None})
    flags, events = set(seen["flags"]), []
    against = (a.type == "CE" and spot < dst[0]) or (a.type == "PE" and spot > dst[0])
    for name, cond, text in [
        ("maxloss", ltp <= loss_level, "premium %.2f is at the Rs %s max-loss level %.2f: per the plan it is time to exit"
         % (ltp, "{:,.0f}".format(max_loss), loss_level)),
        ("daily", against, "NIFTY %.2f is on the wrong side of its daily Supertrend %.2f: the trend the trade rides has broken"
         % (spot, dst[0])),
    ]:
        if cond and name not in flags:
            events.append(text)
            flags.add(name)
        elif not cond:
            flags.discard(name)
    if seen["side"] is not None and side != seen["side"]:
        events.append("NIFTY 5-minute side changed: %s -> %s" % (seen["side"], side))
    band = int(pnl // STEP) if pnl >= 0 else -int(-pnl // STEP)
    if band != seen["band"]:
        events.append("P&L now Rs %s" % rs(pnl))
    today = now.date().isoformat()
    if now.time() >= datetime.time(15, 20) and seen.get("eod") != today:
        events.append("end of day: premium %.2f, P&L Rs %s, theta about Rs %.0f overnight" % (ltp, rs(pnl), theta * qty))
        seen["eod"] = today
    st[key] = {"flags": sorted(flags), "band": band, "side": side, "eod": seen.get("eod")}
    os.makedirs(os.path.dirname(a.state), exist_ok=True)
    with open(a.state, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=1)
    for e in events:
        print("EVENT " + e)
    print("PUSH NIFTY %g%s %.2f P&L Rs %s%s" % (a.strike, a.type, ltp, rs(pnl), (" | " + events[0])[:140] if events else ""))
    return 0 if events else 1


if __name__ == "__main__":
    sys.exit(main())
