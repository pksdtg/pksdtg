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
three the other way -> puts (PE); mixed -> no side. Calls and puts are both listed
(--sides auto lists only NIFTY's side); an option passes only on NIFTY's side, so
"NIFTY agrees" is one of its checkpoints.
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
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
WORKERS = 4  # parallel chart requests in --all mode: polite to Yahoo, ~4x faster


def prev_session_volumes(today):
    """{SYMBOL: shares traded} from NSE's bhavcopy of the last session before `today`."""
    import zipfile
    session = trader_market.NSESession()
    d = today - datetime.timedelta(days=1)
    for _ in range(7):
        if d.weekday() < 5 and d.isoformat() not in trader_market.NSE_HOLIDAYS:
            try:
                raw = session.get(trader_market.NSE_BHAVCOPY % d.strftime("%Y%m%d"))
                z = zipfile.ZipFile(io.BytesIO(raw))
                rows = csv.DictReader(io.StringIO(z.read(z.namelist()[0]).decode("utf-8", "replace")))
                return {r["TckrSymb"]: float(r["TtlTradgVol"] or 0) for r in rows if r.get("SctySrs") in ("EQ", "BE")}
            except Exception:
                pass
        d -= datetime.timedelta(days=1)
    return {}


def liquid_universe(path, min_price=20.0, min_volume=250000, today=None):
    """--all: every stock in trader/universe.txt (scan.py universe) priced min_price or more that
    traded at least min_volume shares today or in the last session, from Yahoo's batch quotes."""
    today = today or (piyush.utc_now() + piyush.IST).date()
    spec = importlib.util.spec_from_file_location("scan", os.path.join(SKILLS, "trader-scanner", "scan.py"))
    scan = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scan)
    with open(path, encoding="utf-8") as fh:
        syms = [ln.split("|")[0].strip() for ln in fh if ln.strip()]
    data, _ = scan.fetch_spark(syms, "5d", "1d", 0.2)
    prev = prev_session_volumes(today)
    keep = []
    for sym in syms:
        meta = (data.get(sym) or {}).get("meta") or {}
        price = meta.get("regularMarketPrice")
        # early in the session today's volume is tiny: take the bigger of today's and the last session's
        vol = max(meta.get("regularMarketVolume") or 0, prev.get(sym, 0))
        if price and vol and price >= min_price and vol >= min_volume:
            keep.append((price * vol, sym))
    return [sym for _, sym in sorted(keep, reverse=True)], len(syms)

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


def option_scan(session, market, now, lo, hi, cfg, lot, sides="both", min_bars=OPT_MIN_BARS):
    info = session.api("option-chain-contract-info?symbol=NIFTY")
    expiries = [datetime.datetime.strptime(e, "%d-%b-%Y").date() for e in info.get("expiryDates", [])]
    live = [e for e in expiries if e > now.date() or (e == now.date() and now.time() < datetime.time(15, 30))]
    if not live:
        raise piyush.PiyushError("no live NIFTY expiry in NSE's list")
    expiry = min(live)
    chain = session.api("option-chain-v3?type=Indices&symbol=NIFTY&expiry=%s" % expiry.strftime("%d-%b-%Y"))
    rec = chain.get("records") or {}
    side = nifty_direction(market) if market else None
    rows = rec.get("data") or []
    ce_oi = sum((r.get("CE") or {}).get("openInterest") or 0 for r in rows)
    pe_oi = sum((r.get("PE") or {}).get("openInterest") or 0 for r in rows)
    wall = lambda t: max(rows, key=lambda r: (r.get(t) or {}).get("openInterest") or 0)["strikePrice"] if rows else None
    chain_stats = {"pcr": pe_oi / ce_oi if ce_oi else None, "ce_wall": wall("CE"), "pe_wall": wall("PE")}
    out = []
    for row in rows:
        for typ in ("CE", "PE"):
            leg = row.get(typ) or {}
            ltp = leg.get("lastPrice")
            if not ltp or not lo <= ltp <= hi or (sides == "auto" and side and typ != side):
                continue
            item = {"strike": row["strikePrice"], "type": typ, "ltp": ltp, "id": leg.get("identifier"),
                    "oi": leg.get("openInterest"), "oi_change": leg.get("changeinOpenInterest"),
                    "iv": leg.get("impliedVolatility"), "volume": leg.get("totalTradedVolume"), "checks": None}
            try:
                time.sleep(0.3)
                g = session.api("chart-databyindex?index=%s" % leg["identifier"])
                bars = ticks_to_bars(g.get("grapthData") or [], now)
                if len(bars) >= min_bars:
                    fast, slow = piyush.supertrend(bars, *piyush.ST_FAST), piyush.supertrend(bars, *piyush.ST_SLOW)
                    r = piyush.rsi([b[4] for b in bars])
                    s, sg = piyush.smi(bars, *piyush.SMI_ARGS)
                    checks = [("Supertrend (7, 0.8) up", fast[-1][1] is True), ("Supertrend (7, 1.6) up", slow[-1][1] is True),
                              ("RSI (14) above 50", r[-1] > 50), ("SMI above signal", s[-1] > sg[-1]),
                              ("NIFTY agrees", side == typ), ("inside the entry window", in_window(now, cfg))]
                    item.update(checks=checks, bars=len(bars), close=bars[-1][4], stop=int(slow[-1][0] * 20) / 20.0,
                                rsi=r[-1], smi=s[-1], signal=sg[-1], bar=bars[-1][0].strftime("%H:%M"),
                                studies=piyush.studies(bars)[0])
                    item["confirm"] = piyush.confirm_score(item)
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
    out.sort(key=lambda x: (x["type"] != side, x["type"], abs(x["ltp"] - (lo + hi) / 2)))
    return dict(chain_stats, expiry=expiry.isoformat(), spot=rec.get("underlyingValue"), side=side, options=out)


# ---------------------------------------------------------------- the scan

SHORT = {"above VWAP": "VWAP", "EMA 9 above EMA 21": "EMA", "MACD histogram above 0": "MACD",
         "ADX above 20, +DI above -DI": "ADX", "volume 1.5x the 20-bar average": "VOL"}


def short_studies(studies):
    return " ".join("%s%s" % (SHORT.get(n, n), "+" if ok else "-" if ok is not None else "?") for n, ok, _ in studies)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("scan")
    p.add_argument("--stocks", help="comma-separated NSE symbols instead of NIFTY 200")
    p.add_argument("--all", action="store_true",
                   help="every liquid NSE stock (Rs 20+, 250,000+ shares) from trader/universe.txt instead of NIFTY 200")
    p.add_argument("--universe", default="trader/universe.txt")
    p.add_argument("--now", help='India time "YYYY-MM-DD HH:MM" (default: now)')
    p.add_argument("--premium", default="100-150", help="option premium band in rupees (100-150)")
    p.add_argument("--no-options", action="store_true")
    p.add_argument("--min-option-bars", type=int, default=OPT_MIN_BARS,
                   help="5-minute bars an option needs before its indicators are read (%d; 15 = earlier, rougher)" % OPT_MIN_BARS)
    p.add_argument("--sides", default="both", choices=["both", "auto"],
                   help="options to list: calls and puts (both, default) or only NIFTY's side (auto)")
    p.add_argument("--new-only", action="store_true", help="only signals not reported earlier today")
    p.add_argument("--min-confirm", type=int, default=None,
                   help="stocks also need this many of the 5 confirmations (default: config pk_min_confirm, 0)")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default=STATE)
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd != "scan":
        ap.print_help()
        return 1
    t0 = time.time()
    now, cfg = piyush.parse_now(a.now), piyush.read_config(a.config)
    if a.min_confirm is None:
        a.min_confirm = int(cfg.get("pk_min_confirm", 0))
    today = now.date().isoformat()
    st = load_state(a.state)
    seen = st.get("alerted", {}).get(today, [])
    session = trader_market.NSESession()
    try:
        universe_note = "NIFTY 200"
        if a.stocks:
            syms, universe_note = [s.strip().upper() for s in a.stocks.split(",")], "your list"
        elif a.all:
            syms, total = liquid_universe(a.universe, today=now.date())
            universe_note = "all NSE: %d liquid of %d listed (Rs 20+, 250,000+ shares)" % (len(syms), total)
        else:
            syms = nifty200(session, st, today)
        market = piyush.evaluate("NIFTY 50", piyush.fetch_bars(piyush.INDEX), now)
    except Exception as e:
        print("pk.py: %s" % e, file=sys.stderr)
        return 1
    passes, failed, scanned = [], 0, 0

    def one(sym):
        try:
            return sym, piyush.evaluate(sym, piyush.fetch_bars(sym), now)
        except Exception:
            return sym, None
    if a.all:
        with ThreadPoolExecutor(WORKERS) as pool:
            evs = list(pool.map(one, syms))
    else:
        evs = []
        for k, sym in enumerate(syms):
            if k:
                time.sleep(piyush.PAUSE)
            evs.append(one(sym))
    for sym, ev in evs:
        if ev is None:
            failed += 1
            continue
        scanned += 1
        cps, warn = piyush.checkpoints(ev, market, cfg, now)
        if all(c[1] for c in cps):
            sc = piyush.confirm_score(ev)
            if sc[0] >= a.min_confirm:
                passes.append({"symbol": sym, "ev": ev, "warnings": warn, "plan": piyush.plan(ev, cfg), "confirm": sc})
    # most confirmations first, then fewest warnings, then the tightest stop as a share of the price
    passes.sort(key=lambda p: (-p["confirm"][0], len(p["warnings"]), p["plan"]["risk"] / p["plan"]["entry"]))
    opts = None
    if not a.no_options:
        lo, hi = (float(x) for x in a.premium.split("-"))
        try:
            opts = option_scan(session, market, now, lo, hi, cfg, nifty_lot(session, st, today), a.sides, a.min_option_bars)
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
    print("pk scan, %s IST (%d s). Stocks (%s): %d of %d checked (Yahoo 5-minute bars, free, can lag)."
          % (now.strftime("%a %Y-%m-%d %H:%M"), time.time() - t0, universe_note, scanned, len(syms)))
    print("NIFTY 50 %.2f (bar %s): Supertrends %s/%s, RSI %.2f, SMI %.2f vs signal %.2f -> %s"
          % (m["close"], m["bar"].strftime("%H:%M"), "up" if m["st_fast"][1] else "down", "up" if m["st_slow"][1] else "down",
             m["rsi"], m["smi"], m["signal"],
             {"CE": "up: calls", "PE": "down: puts", None: "mixed: no side"}[nifty_direction(m)]))
    label = "New stocks passing all 7 checkpoints" if a.new_only else "Stocks passing all 7 checkpoints"
    print("\n%s: %d" % (label, len(passes)))
    for p in passes:
        pl, ev = p["plan"], p["ev"]
        print("  %-11s Rs %9.2f  stop %.2f  target %.2f  risk %.2f  %d sh = Rs %s, max loss Rs %s  (RSI %.1f, SMI %.1f)"
              "  confirm %d/%d [%s]%s"
              % (p["symbol"], pl["entry"], pl["stop"], pl["target"], pl["risk"], pl["qty"], "{:,.0f}".format(pl["cost"]),
                 "{:,.0f}".format(pl["max_loss"]), ev["rsi"], ev["smi"], p["confirm"][0], p["confirm"][1], short_studies(ev["studies"]),
                 ("  note: " + "; ".join(p["warnings"])) if p["warnings"] else ""))
    if opts is not None:
        if "error" in opts:
            print("\nNIFTY options: %s" % opts["error"])
        else:
            print("\nNIFTY options, expiry %s, spot %s, premium Rs %s, side %s:"
                  % (opts["expiry"], opts["spot"], a.premium, opts["side"] or "none (NIFTY mixed): none can pass"))
            print("  chain: put-call ratio (OI) %s, most call OI (resistance) at %s, most put OI (support) at %s"
                  % ("%.2f" % opts["pcr"] if opts.get("pcr") else "n/a", opts.get("ce_wall"), opts.get("pe_wall")))
            for o in opts["options"]:
                if o.get("checks"):
                    n = sum(ok for _, ok in o["checks"])
                    line = "  %s %s  LTP %.2f  chart %s Rs %.2f: %d of 6 checkpoints%s\n      confirm %d/%d [%s]  IV %s  OI %s (change %s)" % (
                        o["strike"], o["type"], o["ltp"], o["bar"], o["close"], n,
                        " " + ", ".join(nm for nm, ok in o["checks"] if not ok) + " fail" if n < 6 else "",
                        o["confirm"][0], o["confirm"][1], short_studies(o["studies"]), o.get("iv"),
                        "{:,}".format(o["oi"]) if o.get("oi") is not None else "n/a",
                        "{:+,}".format(o["oi_change"]) if o.get("oi_change") is not None else "n/a")
                    if o.get("pass"):
                        line += "\n      PASS: stop %.2f (Supertrend 7, 1.6), target %.2f, %d lot(s) x %d = Rs %s, max loss Rs %s" % (
                            o["stop"], o["target"], o["lots"], st.get("lot") or 0, "{:,.0f}".format(o["cost"]),
                            "{:,.0f}".format(o["max_loss"]))
                    print(line)
                else:
                    print("  %s %s  LTP %.2f  %s" % (o["strike"], o["type"], o["ltp"], o.get("error") or
                          "only %d five-minute bars today: indicators not ready" % o.get("bars", 0)))
    bits = ["NIFTY %s %s %.1f SL %.1f" % (o["strike"], o["type"], o["close"], o["stop"]) for o in opt_pass_new]
    bits += ["%s %.2f SL %.2f T %.2f" % (p["symbol"], p["plan"]["entry"], p["plan"]["stop"], p["plan"]["target"]) for p in passes]
    more = len(bits) - 3
    summary = ("PK buy: " + " | ".join(bits[:3]) + (" | +%d more" % more if more > 0 else "")) if bits else "PK: no new signal"
    print("\n%s\nA checkpoint pass is a rule check, not a promise; nothing was placed." % (
        "SUMMARY " + (summary if len(summary) <= 190 else summary[:186] + "...")))
    return 0 if bits else 1


if __name__ == "__main__":
    sys.exit(main())
