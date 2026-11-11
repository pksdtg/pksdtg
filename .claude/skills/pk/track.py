#!/usr/bin/env python3
"""Track an open intraday long: live price, P&L, Piyush's indicators and exit levels.

    python3 track.py GOODLUCK --entry 560.70 --qty 400 --stop 556.02 --target 567.66
    python3 track.py GOODLUCK --entry 560.70 --qty 400 --stop 556.02 --target 567.66 --events

Prints one status block. With --events it also prints an EVENT line for anything new
since the last run (kept in trader/state/track.json): price at or under the stop or the
max-loss level, the target reached, the fast or slow Supertrend turning red, NIFTY
turning down, P&L crossing another Rs 500 step, and the flat-by time. Exit code 0 when
there is an EVENT, 1 otherwise. It reads prices only; it never places or changes an order.
"""
import argparse
import datetime
import importlib.util
import json
import os
import sys
import urllib.parse
import urllib.request

SKILLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("piyush", os.path.join(SKILLS, "trader-piyush", "piyush.py"))
piyush = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(piyush)
sys.path.insert(0, SKILLS)
import trader_market  # noqa: E402

STATE = "trader/state/track.json"
STEP = 500


def rs(x):
    return "{:+,.0f}".format(x)


def live_quote(sym):
    ysym = urllib.parse.quote(trader_market.nse_yahoo(sym), safe=".^-")
    req = urllib.request.Request("https://query2.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=1m" % ysym,
                                 headers={"User-Agent": piyush.UA})
    meta = json.loads(urllib.request.urlopen(req, timeout=20).read())["chart"]["result"][0]["meta"]
    t = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=meta["regularMarketTime"]) + piyush.IST
    return meta["regularMarketPrice"], meta.get("regularMarketDayHigh"), meta.get("regularMarketDayLow"), t


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("symbol")
    ap.add_argument("--entry", type=float, required=True)
    ap.add_argument("--qty", type=int, required=True)
    ap.add_argument("--stop", type=float, help="planned stop (default: the Supertrend (7, 1.6) line now)")
    ap.add_argument("--target", type=float)
    ap.add_argument("--max-loss", type=float, help="rupees (default: config max_loss_per_trade_usd)")
    ap.add_argument("--flat-by", default=None, help="HH:MM IST (default: config flat_by, 15:10)")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--state", default=STATE)
    ap.add_argument("--events", action="store_true")
    a = ap.parse_args(argv)
    sym = a.symbol.upper()
    now, cfg = piyush.parse_now(None), piyush.read_config(a.config)
    max_loss = a.max_loss if a.max_loss is not None else float(cfg["max_loss_per_trade_usd"])
    flat_by = piyush.hhmm(a.flat_by or cfg.get("flat_by") or "15:10")
    ltp, hi, lo, qt = live_quote(sym)
    ev = piyush.evaluate(sym, piyush.fetch_bars(sym), now)
    m = piyush.evaluate("NIFTY 50", piyush.fetch_bars(piyush.INDEX), now)
    stop = a.stop if a.stop is not None else int(ev["st_slow"][0] * 100) / 100.0
    loss_level = round(a.entry - max_loss / a.qty, 2)
    pnl = (ltp - a.entry) * a.qty
    nifty_up = m["st_fast"][1] and m["st_slow"][1] and m["rsi"] > 50
    print("%s %d @ %.2f | LTP %.2f at %s IST (day %.2f-%.2f) | P&L Rs %s (%+.2f%%)"
          % (sym, a.qty, a.entry, ltp, qt.strftime("%H:%M:%S"), lo or 0, hi or 0, rs(pnl), (ltp / a.entry - 1) * 100))
    print("levels: stop %.2f (Rs %s) | max-loss Rs %s at %.2f | target %s | flat by %s"
          % (stop, rs((stop - a.entry) * a.qty), "{:,.0f}".format(max_loss), loss_level,
             "%.2f (Rs %s)" % (a.target, rs((a.target - a.entry) * a.qty)) if a.target else "-", flat_by.strftime("%H:%M")))
    print("chart %s bar: Supertrend (7, 0.8) %s %.2f, (7, 1.6) %s %.2f, RSI %.1f, SMI %.1f vs signal %.1f, confirm %d/%d"
          % (ev["bar"].strftime("%H:%M"), "green" if ev["st_fast"][1] else "red", ev["st_fast"][0],
             "green" if ev["st_slow"][1] else "red", ev["st_slow"][0], ev["rsi"], ev["smi"], ev["signal"],
             *piyush.confirm_score(ev)))
    print("NIFTY %.2f: Supertrends %s/%s, RSI %.1f -> %s"
          % (m["close"], "up" if m["st_fast"][1] else "down", "up" if m["st_slow"][1] else "down", m["rsi"],
             "agrees (up)" if nifty_up else "does not agree"))
    if not a.events:
        return 0
    try:
        with open(a.state, encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        st = {}
    key = "%s|%s|%.2f|%d" % (now.date(), sym, a.entry, a.qty)
    seen = st.get(key, {"flags": [], "band": 0})
    events, flags = [], set(seen["flags"])
    checks = [
        ("stop", ltp <= stop, "price %.2f is at or under the stop %.2f: exit per plan" % (ltp, stop)),
        ("maxloss", ltp <= loss_level, "price %.2f is at the Rs %s max-loss level %.2f" % (ltp, "{:,.0f}".format(max_loss), loss_level)),
        ("target", bool(a.target) and ltp >= a.target, "price %.2f reached the target %.2f" % (ltp, a.target or 0)),
        ("fastred", ev["st_fast"][1] is False, "Supertrend (7, 0.8) is red on the %s bar" % ev["bar"].strftime("%H:%M")),
        ("slowred", ev["st_slow"][1] is False, "Supertrend (7, 1.6) turned red: the trailing stop is broken"),
        ("nifty", not nifty_up, "NIFTY no longer agrees (Supertrends %s/%s, RSI %.1f)"
         % ("up" if m["st_fast"][1] else "down", "up" if m["st_slow"][1] else "down", m["rsi"])),
        ("flat", now.time() >= flat_by, "it is past the flat-by time %s: close the intraday position" % flat_by.strftime("%H:%M")),
    ]
    for name, cond, text in checks:
        if cond and name not in flags:
            events.append(text)
            flags.add(name)
        elif not cond and name in ("fastred", "nifty"):
            flags.discard(name)  # can flip back and alert again
    band = int(pnl // STEP) if pnl >= 0 else -int(-pnl // STEP)
    if band != seen["band"]:
        events.append("P&L now Rs %s" % rs(pnl))
    st[key] = {"flags": sorted(flags), "band": band}
    os.makedirs(os.path.dirname(a.state), exist_ok=True)
    with open(a.state, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=1)
    for e in events:
        print("EVENT " + e)
    print("PUSH %s %.2f P&L Rs %s%s" % (sym, ltp, rs(pnl), (" | " + events[0])[:150] if events else ""))
    return 0 if events else 1


if __name__ == "__main__":
    sys.exit(main())
