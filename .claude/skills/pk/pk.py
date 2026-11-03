#!/usr/bin/env python3
"""pk: Piyush's saved condition, run as a scan.

    python3 pk.py scan                       # NIFTY 200 stocks + NIFTY options, now (IST)
    python3 pk.py scan --now "2026-11-03 11:00"
    python3 pk.py scan --new-only            # only buy signals not reported yet today (for alerts)
    python3 pk.py scan --stocks SBIN,MARICO  # a list of your own instead of NIFTY 200

The condition (trader-piyush's seven checkpoints, 5-minute bars):
  Supertrend (7, 0.8) up, Supertrend (7, 1.6) up, RSI (14) above 50, SMI (10, 3, 3, 10)
  above its signal, NIFTY 50 agrees, inside the entry window, the stop fits the max loss.

Stocks: every NIFTY 200 stock (NSE's index list), Yahoo 5-minute bars.

NIFTY options: the nearest weekly expiry from NSE's option chain, the strikes whose
premium is between Rs 100 and Rs 150 (--premium 100-150). NIFTY's direction picks the
side: both Supertrends up, RSI above 50 and SMI above its signal -> calls (CE); all
three the other way -> puts (PE); mixed -> no side, both are listed for information.
Each option's own 5-minute chart is built from NSE's intraday prices for that
contract (today only, so the indicators need about 26 bars: after ~11:25 IST) and
checked with the same four chart checkpoints plus the entry window. Lot size from
NSE (NIFTY: 65); lots are capped at "max_option_lots" in trader/config.json (1).

The last line is a one-line summary under 200 characters, for a phone alert.
Nothing is ever placed. Exit code 0 when anything passes, 1 otherwise.
"""
import argparse
import csv
import datetime
import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = os.path.dirname(HERE)
sys.path.insert(0, SKILLS)
import trader_market  # noqa: E402
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("piyush", os.path.join(SKILLS, "trader-piyush", "piyush.py"))
piyush = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(piyush)

NIFTY200 = trader_market.NSE_ARCHIVES + "/content/indices/ind_nifty200list.csv"
LOTS = trader_market.NSE_ARCHIVES + "/content/fo/fo_mktlots.csv"
STATE = "trader/state/pk.json"
OPT_MIN_BARS = 26


# ---------------------------------------------------------------- inputs

def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(path, st):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=1)


def nifty200(session, st, today):
    """Cached once a day in the state file."""
    if st.get("nifty200_day") == today and st.get("nifty200"):
        return st["nifty200"]
    rows = csv.DictReader(io.StringIO(session.get(NIFTY200).decode("utf-8", "replace")))
    syms = [r["Symbol"].strip().upper() for r in rows if r.get("Symbol")]
    st["nifty200"], st["nifty200_day"] = syms, today
    return syms


def nifty_lot(session, st, today):
    if st.get("lot_day") == today and st.get("lot"):
        return st["lot"]
    lot = None
    for line in session.get(LOTS).decode("utf-8", "replace").splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) > 2 and cells[1] == "NIFTY":
            lot = int(next(c for c in cells[2:] if c.isdigit()))
    st["lot"], st["lot_day"] = lot, today
    return lot


# ---------------------------------------------------------------- options

def ticks_to_bars(points, now):
    """NSE grapthData [[ms, price], ...] (IST wall clock as epoch) -> completed 5-minute OHLC."""
    bars = {}
    for ms, price in points:
        t = datetime.datetime(1970, 1, 1) + datetime.timedelta(milliseconds=ms)
        start = t.replace(second=0, microsecond=0, minute=t.minute - t.minute % 5)
        if start + piyush.BAR > now or price is None:
            continue
        b = bars.get(start)
        bars[start] = [start, price, price, price, price] if b is None else \
            [start, b[1], max(b[2], price), min(b[3], price), price]
    return [tuple(b) for _, b in sorted(bars.items())]


def in_window(now, cfg):
    """An NSE trading day, between no_entries_before and no_entries_after (09:30-15:00)."""
    lo, hi = piyush.hhmm(cfg["no_entries_before"]), piyush.hhmm(cfg["no_entries_after"])
    return now.weekday() < 5 and now.date().isoformat() not in trader_market.NSE_HOLIDAYS and lo <= now.time() <= hi


def nifty_direction(m):
    up = m["st_fast"][1] and m["st_slow"][1] and m["rsi"] > 50 and m["smi"] > m["signal"]
    down = (not m["st_fast"][1]) and (not m["st_slow"][1]) and m["rsi"] < 50 and m["smi"] < m["signal"]
    return "CE" if up else "PE" if down else None


def option_scan(session, market, now, lo, hi, cfg, lot):
    info = session.api("option-chain-contract-info?symbol=NIFTY")
    expiries = [datetime.datetime.strptime(e, "%d-%b-%Y").date() for e in info.get("expiryDates", [])]
    live = [e for e in expiries if e > now.date() or (e == now.date() and now.time() < datetime.time(15, 30))]
    if not live:
        raise piyush.PiyushError("no live NIFTY expiry in NSE's list")
    expiry = min(live)
    chain = session.api("option-chain-v3?type=Indices&symbol=NIFTY&expiry=%s" % expiry.strftime("%d-%b-%Y"))
    rec = chain.get("records") or {}
    side = nifty_direction(market) if market else None
    out = []
    for row in rec.get("data") or []:
        for typ in ("CE", "PE"):
            leg = row.get(typ) or {}
            ltp = leg.get("lastPrice")
            if not ltp or not lo <= ltp <= hi or (side and typ != side):
                continue
            item = {"strike": row["strikePrice"], "type": typ, "ltp": ltp, "id": leg.get("identifier"),
                    "oi": leg.get("openInterest"), "volume": leg.get("totalTradedVolume"), "checks": None}
            try:
                time.sleep(0.3)
                g = session.api("chart-databyindex?index=%s" % leg["identifier"])
                bars = ticks_to_bars(g.get("grapthData") or [], now)
                if len(bars) >= OPT_MIN_BARS:
                    fast, slow = piyush.supertrend(bars, *piyush.ST_FAST), piyush.supertrend(bars, *piyush.ST_SLOW)
                    r = piyush.rsi([b[4] for b in bars])
                    s, sg = piyush.smi(bars, *piyush.SMI_ARGS)
                    checks = [("Supertrend (7, 0.8) up", fast[-1][1] is True), ("Supertrend (7, 1.6) up", slow[-1][1] is True),
                              ("RSI (14) above 50", r[-1] > 50), ("SMI above signal", s[-1] > sg[-1]),
                              ("inside the entry window", in_window(now, cfg))]
                    item.update(checks=checks, bars=len(bars), close=bars[-1][4], stop=int(slow[-1][0] * 20) / 20.0,
                                rsi=r[-1], smi=s[-1], signal=sg[-1], bar=bars[-1][0].strftime("%H:%M"))
                    item["pass"] = all(ok for _, ok in checks)
                    if item["pass"] and lot:
                        risk = item["close"] - item["stop"]
                        lots = int(cfg["max_loss_per_trade_usd"] // (risk * lot)) if risk > 0 else 0
                        lots = min(lots, int(cfg["budget"] * (1 - cfg["cash_buffer_pct"] / 100.0) // (item["close"] * lot)),
                                   int(cfg.get("max_option_lots", 1)))
                        item.update(risk=risk, target=round(item["close"] + 2 * risk, 2), lots=lots,
                                    cost=lots * lot * item["close"], max_loss=lots * lot * risk)
                else:
                    item["bars"] = len(bars)
            except Exception as e:  # one contract's chart failing must not stop the scan
                item["error"] = str(e)
            out.append(item)
    out.sort(key=lambda x: abs(x["ltp"] - (lo + hi) / 2))
    return {"expiry": expiry.isoformat(), "spot": rec.get("underlyingValue"), "side": side, "options": out}


# ---------------------------------------------------------------- the scan

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("scan")
    p.add_argument("--stocks", help="comma-separated NSE symbols instead of NIFTY 200")
    p.add_argument("--now", help='India time "YYYY-MM-DD HH:MM" (default: now)')
    p.add_argument("--premium", default="100-150", help="option premium band in rupees (100-150)")
    p.add_argument("--no-options", action="store_true")
    p.add_argument("--new-only", action="store_true", help="only signals not reported earlier today")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default=STATE)
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd != "scan":
        ap.print_help()
        return 1
    t0 = time.time()
    now, cfg = piyush.parse_now(a.now), piyush.read_config(a.config)
    today = now.date().isoformat()
    st = load_state(a.state)
    seen = st.get("alerted", {}).get(today, [])
    session = trader_market.NSESession()
    try:
        syms = [s.strip().upper() for s in a.stocks.split(",")] if a.stocks else nifty200(session, st, today)
        market = piyush.evaluate("NIFTY 50", piyush.fetch_bars(piyush.INDEX), now)
    except Exception as e:
        print("pk.py: %s" % e, file=sys.stderr)
        return 1
    passes, failed, scanned = [], 0, 0
    for k, sym in enumerate(syms):
        if k:
            time.sleep(piyush.PAUSE)
        try:
            ev = piyush.evaluate(sym, piyush.fetch_bars(sym), now)
        except Exception:
            failed += 1
            continue
        scanned += 1
        cps, warn = piyush.checkpoints(ev, market, cfg, now)
        if all(c[1] for c in cps):
            passes.append({"symbol": sym, "ev": ev, "warnings": warn, "plan": piyush.plan(ev, cfg)})
    # clean signals first: fewest warnings, then the tightest stop as a share of the price
    passes.sort(key=lambda p: (len(p["warnings"]), p["plan"]["risk"] / p["plan"]["entry"]))
    opts = None
    if not a.no_options:
        lo, hi = (float(x) for x in a.premium.split("-"))
        try:
            opts = option_scan(session, market, now, lo, hi, cfg, nifty_lot(session, st, today))
        except Exception as e:
            opts = {"error": str(e)}
    opt_pass = [o for o in (opts or {}).get("options", []) if o.get("pass")]
    keys = ["S:" + p["symbol"] for p in passes] + ["O:%s%s" % (o["strike"], o["type"]) for o in opt_pass]
    new = [k for k in keys if k not in seen]
    st.setdefault("alerted", {})[today] = sorted(set(seen) | set(keys))
    st["alerted"] = {d: v for d, v in st["alerted"].items() if d >= (now.date() - datetime.timedelta(days=7)).isoformat()}
    save_state(a.state, st)
    if a.new_only:
        passes = [p for p in passes if "S:" + p["symbol"] in new]
        opt_pass_new = [o for o in opt_pass if "O:%s%s" % (o["strike"], o["type"]) in new]
    else:
        opt_pass_new = opt_pass

    if a.json:
        print(json.dumps({"now": now.isoformat(), "scanned": scanned, "failed": failed,
                          "passes": [dict(p, ev=dict(p["ev"], bar=p["ev"]["bar"].isoformat())) for p in passes],
                          "options": opts, "new": new}, indent=2, default=str))
        return 0 if (passes or opt_pass_new) else 1

    m = market
    print("pk scan, %s IST (%d s). Stocks: %d of %d checked (Yahoo 5-minute bars, free, can lag)."
          % (now.strftime("%a %Y-%m-%d %H:%M"), time.time() - t0, scanned, len(syms)))
    print("NIFTY 50 %.2f (bar %s): Supertrends %s/%s, RSI %.2f, SMI %.2f vs signal %.2f -> %s"
          % (m["close"], m["bar"].strftime("%H:%M"), "up" if m["st_fast"][1] else "down", "up" if m["st_slow"][1] else "down",
             m["rsi"], m["smi"], m["signal"],
             {"CE": "up: calls", "PE": "down: puts", None: "mixed: no side"}[nifty_direction(m)]))
    label = "New stocks passing all 7 checkpoints" if a.new_only else "Stocks passing all 7 checkpoints"
    print("\n%s: %d" % (label, len(passes)))
    for p in passes:
        pl, ev = p["plan"], p["ev"]
        print("  %-11s Rs %9.2f  stop %.2f  target %.2f  risk %.2f  %d sh = Rs %s, max loss Rs %s  (RSI %.1f, SMI %.1f)%s"
              % (p["symbol"], pl["entry"], pl["stop"], pl["target"], pl["risk"], pl["qty"], "{:,.0f}".format(pl["cost"]),
                 "{:,.0f}".format(pl["max_loss"]), ev["rsi"], ev["smi"], ("  note: " + "; ".join(p["warnings"])) if p["warnings"] else ""))
    if opts is not None:
        if "error" in opts:
            print("\nNIFTY options: %s" % opts["error"])
        else:
            print("\nNIFTY options, expiry %s, spot %s, premium Rs %s, side %s:"
                  % (opts["expiry"], opts["spot"], a.premium, opts["side"] or "none (NIFTY mixed): both listed, none can pass"))
            for o in opts["options"]:
                if o.get("checks"):
                    n = sum(ok for _, ok in o["checks"])
                    line = "  %s %s  LTP %.2f  chart %s Rs %.2f: %d of 5 checkpoints%s" % (
                        o["strike"], o["type"], o["ltp"], o["bar"], o["close"], n,
                        " " + ", ".join(nm for nm, ok in o["checks"] if not ok) + " fail" if n < 5 else "")
                    if o.get("pass") and opts["side"]:
                        line += "\n      PASS: stop %.2f (Supertrend 7, 1.6), target %.2f, %d lot(s) x %d = Rs %s, max loss Rs %s" % (
                            o["stop"], o["target"], o["lots"], st.get("lot") or 0, "{:,.0f}".format(o["cost"]),
                            "{:,.0f}".format(o["max_loss"]))
                    print(line)
                else:
                    print("  %s %s  LTP %.2f  %s" % (o["strike"], o["type"], o["ltp"], o.get("error") or
                          "only %d five-minute bars today: indicators not ready" % o.get("bars", 0)))
    bits = ["NIFTY %s %s %.1f SL %.1f" % (o["strike"], o["type"], o["close"], o["stop"]) for o in opt_pass_new if opts.get("side")]
    bits += ["%s %.2f SL %.2f T %.2f" % (p["symbol"], p["plan"]["entry"], p["plan"]["stop"], p["plan"]["target"]) for p in passes]
    more = len(bits) - 3
    summary = ("PK buy: " + " | ".join(bits[:3]) + (" | +%d more" % more if more > 0 else "")) if bits else "PK: no new signal"
    print("\n%s\nA checkpoint pass is a rule check, not a promise; nothing was placed." % (
        "SUMMARY " + (summary if len(summary) <= 190 else summary[:186] + "...")))
    return 0 if bits else 1


if __name__ == "__main__":
    sys.exit(main())
