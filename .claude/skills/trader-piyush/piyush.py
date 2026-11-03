#!/usr/bin/env python3
"""Piyush checkpoints (trader-piyush): the chart setup from Piyush's 5-minute charts,
checked bar by bar, with NIFTY 50 as the market filter.

    python3 piyush.py check SBIN MARICO KOTAKBANK            # now, NSE time
    python3 piyush.py check SBIN --now "2026-11-03 15:20"    # replay a past moment (IST)
    python3 piyush.py check --watchlist trader/watchlist.json --only-pass
    python3 piyush.py check SBIN --json

Indicators, on completed 5-minute bars (Yahoo, free, can lag):
  Supertrend (7, 0.8) and Supertrend (7, 1.6): ATR 7 (Wilder), basic bands
      (high + low) / 2 +- multiplier x ATR, the usual trailing rule. Up = green.
  RSI (14), Wilder smoothing, against the 50 line.
  Stochastic Momentum Index (10, 3, 3, 10, EMA): over 10 bars, the close minus the
      midpoint of the highest high and lowest low, smoothed twice by EMA 3, over half
      the high-low range smoothed the same way, times 100. Signal = EMA 10 of it.
      +40 and -40 are the extreme lines.

Checkpoints for a long (buy) setup, each PASS or FAIL:
  1  Supertrend (7, 0.8) is up
  2  Supertrend (7, 1.6) is up
  3  RSI (14) is above 50
  4  SMI is above its signal line
  5  NIFTY 50: both Supertrends up and RSI (14) above 50 (the market agrees)
  6  inside the entry window (config no_entries_before / no_entries_after; NSE 09:30-15:00)
  7  the stop (the Supertrend (7, 1.6) line) is close enough that at least one share
     fits the max loss per trade
Warnings, not failures: SMI above +40 (stretched), RSI above 75, a fresh flip of the
fast Supertrend (1-2 bars old, can flip back), the last bar older than 15 minutes.

When every checkpoint passes it prints a plan to hand on: entry = last close,
stop = the Supertrend (7, 1.6) line, target 2R, the shares that fit the max loss and
the position cap, and the exact /trader-exit command. It never places an order.
Exit code 0 when at least one ticker passes every checkpoint, 1 otherwise.
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import trader_market  # noqa: E402

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0)"
HOSTS = ("query2", "query1")
PAUSE = 0.3
IST = datetime.timedelta(hours=5, minutes=30)
BAR = datetime.timedelta(minutes=5)
INDEX = "^NSEI"
ST_FAST, ST_SLOW = (7, 0.8), (7, 1.6)
RSI_N = 14
SMI_ARGS = (10, 3, 3, 10)
SMI_EXTREME = 40.0
DEFAULTS = {"no_entries_before": "09:30", "no_entries_after": "15:00", "max_loss_per_trade_usd": 2000,
            "budget": 100000, "max_position_pct": 75, "cash_buffer_pct": 10}


class PiyushError(Exception):
    pass


# ---------------------------------------------------------------- data

def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)


def parse_now(text):
    """IST wall clock "2026-11-03 15:20" (or HH:MM for today) -> naive IST."""
    if not text:
        return utc_now() + IST
    t = text.strip()
    if len(t) <= 5:
        t = "%s %s" % ((utc_now() + IST).date().isoformat(), t.zfill(5))
    try:
        return datetime.datetime.fromisoformat(t)
    except ValueError:
        raise PiyushError('--now wants an India time like "2026-11-03 10:15"')


def fetch_bars(sym, rng="5d", offline=None):
    """Yahoo 5-minute chart -> [(start IST, o, h, l, c)], null bars dropped."""
    ysym = trader_market.nse_yahoo(sym)
    if offline:
        with open(os.path.join(offline, ysym + ".json"), encoding="utf-8") as fh:
            doc = json.load(fh)
    else:
        path = "/v8/finance/chart/%s?range=%s&interval=5m" % (urllib.parse.quote(ysym, safe=".^-"), rng)
        last = None
        for host in HOSTS:
            try:
                req = urllib.request.Request("https://%s.finance.yahoo.com%s" % (host, path), headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    doc = json.loads(resp.read().decode("utf-8"))
                break
            except (urllib.error.URLError, OSError, ValueError) as e:
                last = e
        else:
            raise PiyushError("Yahoo did not answer for %s: %s" % (ysym, getattr(last, "reason", last)))
    res = ((doc.get("chart") or {}).get("result") or [None])[0]
    if not res:
        raise PiyushError("Yahoo has no 5-minute data for %s" % ysym)
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        row = [q.get(k, [None] * (i + 1))[i] for k in ("open", "high", "low", "close")]
        if ts is None or ts % 300 or None in row:
            continue
        out.append((datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=ts) + IST,) + tuple(float(x) for x in row))
    return out


# ---------------------------------------------------------------- indicators

def rma(values, n):
    """Wilder's moving average; None until n values."""
    out, avg = [], None
    for i, v in enumerate(values):
        if i + 1 < n:
            out.append(None)
        elif i + 1 == n:
            avg = sum(values[:n]) / n
            out.append(avg)
        else:
            avg = (avg * (n - 1) + v) / n
            out.append(avg)
    return out


def ema(values, n):
    out, e, k = [], None, 2.0 / (n + 1)
    for v in values:
        if v is None:
            out.append(None)
            continue
        e = v if e is None else e + k * (v - e)
        out.append(e)
    return out


def supertrend(bars, n, mult):
    """[(line, up)] per bar; (None, None) until the ATR exists."""
    tr = [bars[0][2] - bars[0][3]] + [max(b[2] - b[3], abs(b[2] - p[4]), abs(b[3] - p[4]))
                                      for p, b in zip(bars, bars[1:])]
    atr = rma(tr, n)
    out, fu, fl, up = [], None, None, True
    for i, b in enumerate(bars):
        if atr[i] is None:
            out.append((None, None))
            continue
        mid = (b[2] + b[3]) / 2
        bu, bl = mid + mult * atr[i], mid - mult * atr[i]
        prev_c = bars[i - 1][4] if i else b[4]
        fu = bu if fu is None or bu < fu or prev_c > fu else fu
        fl = bl if fl is None or bl > fl or prev_c < fl else fl
        if up and b[4] < fl:
            up = False
        elif not up and b[4] > fu:
            up = True
        out.append((fl if up else fu, up))
    return out


def rsi(closes, n=RSI_N):
    gains = [0.0] + [max(c - p, 0) for p, c in zip(closes, closes[1:])]
    losses = [0.0] + [max(p - c, 0) for p, c in zip(closes, closes[1:])]
    ag, al = rma(gains[1:], n), rma(losses[1:], n)
    out = [None]
    for g, l in zip(ag, al):
        out.append(None if g is None else (100.0 if l == 0 else 100 - 100 / (1 + g / l)))
    return out


def smi(bars, k=10, d1=3, d2=3, sig=10):
    """(smi, signal) per bar."""
    rel, rng = [], []
    for i in range(len(bars)):
        if i + 1 < k:
            rel.append(None)
            rng.append(None)
            continue
        w = bars[i + 1 - k:i + 1]
        hh, ll = max(b[2] for b in w), min(b[3] for b in w)
        rel.append(bars[i][4] - (hh + ll) / 2)
        rng.append(hh - ll)
    r2, h2 = ema(ema(rel, d1), d2), ema(ema(rng, d1), d2)
    s = [None if a is None or not b else 100 * a / (0.5 * b) for a, b in zip(r2, h2)]
    return s, ema(s, sig)


# ---------------------------------------------------------------- the check

def read_config(path):
    cfg = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as fh:
            cfg.update({k: v for k, v in (json.load(fh) or {}).items() if v is not None})
    except (OSError, ValueError):
        pass
    return cfg


def hhmm(text):
    h, m = str(text).split(":")
    return datetime.time(int(h), int(m))


def evaluate(sym, bars, now):
    """Indicators on the bars complete at `now` (IST)."""
    bars = [b for b in bars if b[0] + BAR <= now]
    if len(bars) < 40:
        raise PiyushError("only %d complete 5-minute bars for %s by %s" % (len(bars), sym, now.strftime("%Y-%m-%d %H:%M")))
    fast, slow = supertrend(bars, *ST_FAST), supertrend(bars, *ST_SLOW)
    r = rsi([b[4] for b in bars])
    s, sg = smi(bars, *SMI_ARGS)
    flip = 0
    for i in range(len(fast) - 1, 0, -1):
        if fast[i][1] != fast[i - 1][1]:
            break
        flip += 1
    last = bars[-1]
    return {"symbol": sym, "bar": last[0], "close": last[4], "st_fast": fast[-1], "st_slow": slow[-1],
            "rsi": r[-1], "smi": s[-1], "signal": sg[-1], "bars_since_fast_flip": flip + 1}


def checkpoints(ev, market, cfg, now):
    c = []
    c.append(("Supertrend (7, 0.8) up", ev["st_fast"][1] is True,
              "line %.2f, %s" % (ev["st_fast"][0], "green, below price" if ev["st_fast"][1] else "red, above price")))
    c.append(("Supertrend (7, 1.6) up", ev["st_slow"][1] is True,
              "line %.2f, %s" % (ev["st_slow"][0], "green, below price" if ev["st_slow"][1] else "red, above price")))
    c.append(("RSI (14) above 50", ev["rsi"] > 50, "RSI %.2f" % ev["rsi"]))
    c.append(("SMI above its signal", ev["smi"] > ev["signal"], "SMI %.2f, signal %.2f" % (ev["smi"], ev["signal"])))
    if market:
        ok = market["st_fast"][1] and market["st_slow"][1] and market["rsi"] > 50
        c.append(("NIFTY 50 agrees", bool(ok), "Supertrends %s/%s, RSI %.2f" % (
            "up" if market["st_fast"][1] else "down", "up" if market["st_slow"][1] else "down", market["rsi"])))
    else:
        c.append(("NIFTY 50 agrees", False, "no NIFTY 50 data"))
    lo, hi = hhmm(cfg["no_entries_before"]), hhmm(cfg["no_entries_after"])
    ok_t = trader_market.NSE_HOLIDAYS.get(now.date().isoformat()) is None and now.weekday() < 5 and lo <= now.time() <= hi
    c.append(("inside the entry window", ok_t, "%s, window %s-%s" % (now.strftime("%a %H:%M"), cfg["no_entries_before"],
                                                                     cfg["no_entries_after"])))
    risk = ev["close"] - ev["st_slow"][0] if ev["st_slow"][1] else None
    fits = bool(risk and risk > 0 and cfg["max_loss_per_trade_usd"] // risk >= 1)
    c.append(("stop fits the max loss", fits, ("risk %.2f a share vs max loss Rs %s" % (risk, "{:,}".format(cfg["max_loss_per_trade_usd"])))
              if risk and risk > 0 else "no stop below the price (Supertrend (7, 1.6) is down)"))
    warn = []
    if ev["smi"] > SMI_EXTREME:
        warn.append("SMI %.1f is above +40: stretched" % ev["smi"])
    if ev["rsi"] > 75:
        warn.append("RSI %.1f is above 75" % ev["rsi"])
    if ev["st_fast"][1] and ev["bars_since_fast_flip"] <= 2:
        warn.append("Supertrend (7, 0.8) turned up %d bar(s) ago: it can flip back" % ev["bars_since_fast_flip"])
    if now - (ev["bar"] + BAR) > datetime.timedelta(minutes=15):
        warn.append("the last bar ended at %s: the data is not live" % (ev["bar"] + BAR).strftime("%Y-%m-%d %H:%M"))
    return c, warn


def plan(ev, cfg):
    entry, stop = ev["close"], int(ev["st_slow"][0] * 100) / 100.0
    risk = entry - stop
    cap = cfg["budget"] * cfg["max_position_pct"] / 100.0
    cash = cfg["budget"] * (1 - cfg["cash_buffer_pct"] / 100.0)
    qty = int(min(cfg["max_loss_per_trade_usd"] // risk, cap // entry, cash // entry))
    return {"entry": entry, "stop": stop, "risk": risk, "target": round(entry + 2 * risk, 2), "qty": qty,
            "cost": qty * entry, "max_loss": qty * risk}


def symbols_from(args):
    syms = [s.upper() for s in args.tickers or []]
    if args.watchlist:
        with open(args.watchlist, encoding="utf-8") as fh:
            syms += [h["symbol"].upper() for h in (json.load(fh) or {}).get("hits", [])]
    out = []
    for s in syms:
        if s not in out:
            out.append(s)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("check", help="run the checkpoints")
    p.add_argument("tickers", nargs="*")
    p.add_argument("--watchlist", help="a trader/watchlist.json: check its hits")
    p.add_argument("--now", help='India time "YYYY-MM-DD HH:MM" (default: now)')
    p.add_argument("--only-pass", action="store_true", help="print only tickers that pass every checkpoint")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--offline", metavar="DIR", help="read DIR/<SYMBOL>.NS.json instead of the network")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd != "check":
        ap.print_help()
        return 1
    try:
        syms = symbols_from(a)
        if not syms:
            raise PiyushError("no tickers: name some, or pass --watchlist trader/watchlist.json")
        now, cfg = parse_now(a.now), read_config(a.config)
        try:
            market = evaluate("NIFTY 50", fetch_bars(INDEX, offline=a.offline), now)
        except PiyushError:
            market = None
        results, passed = [], 0
        for k, sym in enumerate(syms):
            if k and not a.offline:
                time.sleep(PAUSE)
            try:
                ev = evaluate(sym, fetch_bars(sym, offline=a.offline), now)
            except PiyushError as e:
                results.append({"symbol": sym, "error": str(e)})
                continue
            cps, warn = checkpoints(ev, market, cfg, now)
            ok = all(c[1] for c in cps)
            passed += ok
            results.append({"symbol": sym, "ev": ev, "checkpoints": cps, "warnings": warn, "pass": ok,
                            "plan": plan(ev, cfg) if ok else None})
    except PiyushError as e:
        print("piyush.py: %s" % e, file=sys.stderr)
        return 1
    if a.json:
        def clean(r):
            r = dict(r)
            if "ev" in r:
                r["ev"] = dict(r["ev"], bar=r["ev"]["bar"].isoformat())
                r["checkpoints"] = [{"name": n, "pass": bool(ok), "detail": d} for n, ok, d in r["checkpoints"]]
            return r
        print(json.dumps({"now": now.isoformat(), "market": market and dict(market, bar=market["bar"].isoformat()),
                          "results": [clean(r) for r in results]}, indent=2))
        return 0 if passed else 1
    print("Piyush checkpoints, %s IST, completed 5-minute bars (Yahoo, free, can lag)." % now.strftime("%a %Y-%m-%d %H:%M"))
    if market:
        print("NIFTY 50 %.2f (bar %s): Supertrend (7, 0.8) %s %.2f, (7, 1.6) %s %.2f, RSI %.2f, SMI %.2f / signal %.2f"
              % (market["close"], market["bar"].strftime("%H:%M"), "up" if market["st_fast"][1] else "down",
                 market["st_fast"][0], "up" if market["st_slow"][1] else "down", market["st_slow"][0],
                 market["rsi"], market["smi"], market["signal"]))
    for r in results:
        if "error" in r:
            if not a.only_pass:
                print("\n%s: %s" % (r["symbol"], r["error"]))
            continue
        if a.only_pass and not r["pass"]:
            continue
        ev = r["ev"]
        n_ok = sum(1 for c in r["checkpoints"] if c[1])
        print("\n%s  Rs %.2f (bar %s)  %s: %d of %d checkpoints" % (
            r["symbol"], ev["close"], ev["bar"].strftime("%H:%M"), "ALL PASS" if r["pass"] else "NOT YET", n_ok,
            len(r["checkpoints"])))
        for k, (name, ok, detail) in enumerate(r["checkpoints"], 1):
            print("  %d %-4s %-26s %s" % (k, "PASS" if ok else "FAIL", name, detail))
        for w in r["warnings"]:
            print("  note: " + w)
        if r["plan"]:
            p = r["plan"]
            print("  plan: entry Rs %.2f, stop Rs %.2f (Supertrend 7, 1.6), risk Rs %.2f a share, target Rs %.2f (2R)"
                  % (p["entry"], p["stop"], p["risk"], p["target"]))
            print("        %d shares = Rs %s, max loss at the stop Rs %s"
                  % (p["qty"], "{:,.0f}".format(p["cost"]), "{:,.0f}".format(p["max_loss"])))
            print("        next: exit.py plan --symbol %s --setup level --entry %.2f --stop price:%.2f --date %s --time %s --write"
                  % (r["symbol"], p["entry"], p["stop"], now.date().isoformat(), (ev["bar"] + BAR).strftime("%H:%M")))
    print("\n%d of %d passed every checkpoint. A checkpoint is a rule check, not a buy signal or a promise; "
          "nothing was placed." % (passed, len(syms)))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
