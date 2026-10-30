"""Market switch for the trader-* tools: US (NYSE/Nasdaq, the default) or NSE (India).

Pick NSE with the environment variable TRADER_MARKET=NSE, or with
"market": "NSE" in trader/config.json. Every tool calls apply(globals()) once at
import; in US mode it changes nothing.

In NSE mode apply() swaps, wherever the tool defines them:

  clock      India Standard Time, UTC+5:30, no daylight saving
  session    9:15 to 15:30 IST (normal market), no half days
  holidays   NSE equity-segment trading holidays (nseindia.com holiday master,
             CM segment, read 2026-10-30; 2027 is not published yet)
  defaults   no entries before 9:30 (after the 15-minute range) or after 15:00,
             flat by 15:10, before brokers' intraday auto square-off (~15:20)
  symbols    Yahoo tickers get the .NS suffix (RELIANCE -> RELIANCE.NS); a
             symbol that already ends in .NS or .BO, or an index like ^NSEI,
             is left alone
  labels     printed text says IST, NSE, India and Rs instead of ET, NYSE,
             New York and $ (files keep their field names, e.g. max_loss_usd,
             but the amounts in them are rupees)

Standard library only, like the tools.
"""
import datetime
import http.cookiejar
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request

IST = datetime.timedelta(hours=5, minutes=30)
NSE_OPEN = datetime.time(9, 15)
NSE_CLOSE = datetime.time(15, 30)
YAHOO_NSE_LAST_FULL_BAR_END = datetime.time(15, 15)
NSE_DEFAULTS = {"no_entries_before": "09:30", "no_entries_after": "15:00", "flat_by": "15:10"}
NSE_CALENDAR_YEARS = (2026,)
# CM (cash market) segment, https://www.nseindia.com/api/holiday-master?type=trading, read 2026-10-30.
# Weekend dates are kept for the record; Muhurat trading (Diwali evening) is not modelled.
NSE_HOLIDAYS = {
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-02-15": "Mahashivratri",
    "2026-03-03": "Holi",
    "2026-03-21": "Id-Ul-Fitr (Ramadan Eid)",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-08-15": "Independence Day",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-08": "Diwali Laxmi Pujan",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
NSE_HOME = "https://www.nseindia.com/"
NSE_PRIMER = NSE_HOME + "resources/exchange-communication-holidays"
NSE_ARCHIVES = "https://nsearchives.nseindia.com"
NSE_EQUITY_LIST = NSE_ARCHIVES + "/content/equities/EQUITY_L.csv"
NSE_BHAVCOPY = NSE_ARCHIVES + "/content/cm/BhavCopy_NSE_CM_0_0_0_%s_F_0000.csv.zip"  # %s = YYYYMMDD
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0 Safari/537.36")


def market(config="trader/config.json"):
    """"NSE" or "US": TRADER_MARKET first, then "market" in trader/config.json."""
    m = os.environ.get("TRADER_MARKET")
    if not m:
        try:
            with open(config, encoding="utf-8") as fh:
                m = (json.load(fh) or {}).get("market")
        except (OSError, ValueError, AttributeError):
            m = None
    return "NSE" if str(m or "").strip().upper() in ("NSE", "IN", "INDIA") else "US"


# ---------------------------------------------------------------- symbols

def nse_yahoo(sym):
    s = str(sym).strip().upper()
    if s.startswith("^") or s.endswith((".NS", ".BO")):
        return s
    return s + ".NS"


def strip_suffix(sym):
    s = str(sym).upper()
    return s[:-3] if s.endswith((".NS", ".BO")) else s


def _q(sym):
    return urllib.parse.quote(nse_yahoo(sym), safe=".^-")


# ---------------------------------------------------------------- NSE website

class NSESession(object):
    """www.nseindia.com/api answers only with the cookies its home page sets."""

    def __init__(self, timeout=30):
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.primed = False

    def get(self, url, referer=NSE_HOME, accept="*/*"):
        if url.startswith(NSE_HOME) and not self.primed:
            # the bare home page answers 403 to scripts; an ordinary page sets the same cookies
            self.opener.open(urllib.request.Request(NSE_PRIMER, headers={
                "User-Agent": BROWSER_UA, "Accept": "text/html", "Accept-Language": "en-US,en"}),
                timeout=self.timeout).read()
            self.primed = True
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Referer": referer,
                                                   "Accept": accept, "Accept-Language": "en-US,en"})
        with self.opener.open(req, timeout=self.timeout) as resp:
            return resp.read()

    def api(self, path):
        return json.loads(self.get(NSE_HOME + "api/" + path, accept="application/json").decode("utf-8"))


def nse_universe(text):
    """EQUITY_L.csv -> [(SYMBOL, Name)] for the EQ and BE series (ordinary equity)."""
    import csv
    rows = list(csv.reader(io.StringIO(text)))
    head = [h.strip().upper() for h in rows[0]]
    i_sym, i_name, i_ser = head.index("SYMBOL"), head.index("NAME OF COMPANY"), head.index("SERIES")
    counts = {"rows": len(rows) - 1, "keep": 0, "other series": 0}
    kept = []
    for r in rows[1:]:
        if len(r) <= max(i_sym, i_name, i_ser):
            continue
        if r[i_ser].strip().upper() not in ("EQ", "BE"):
            counts["other series"] += 1
            continue
        kept.append((r[i_sym].strip().upper(), r[i_name].strip()))
    counts["keep"] = len(kept)
    return sorted(kept), counts


# ---------------------------------------------------------------- printed labels

_LABELS = [
    (re.compile(r"nyse\.com"), "nseindia.com"),
    (re.compile(r"\bNYSE\b"), "NSE"),
    (re.compile(r"\bNew York\b"), "India"),
    (re.compile(r"\bET\b"), "IST"),
    (re.compile(r"(?<![\w$])\$(?=[\d,.\-+(%])"), "Rs "),
]


def localize(text):
    for rx, new in _LABELS:
        text = rx.sub(new, text)
    return text


class _Localized(io.TextIOBase):
    def __init__(self, stream):
        self._s = stream

    def write(self, text):
        return self._s.write(localize(text))

    def flush(self):
        return self._s.flush()

    def isatty(self):
        return self._s.isatty()

    @property
    def encoding(self):
        return getattr(self._s, "encoding", "utf-8")


# ---------------------------------------------------------------- the switch

def apply(g):
    """Patch a tool's module globals for the market in use. Returns "US" or "NSE"."""
    m = market()
    g["MARKET"] = m
    if m != "NSE":
        return m

    g["eastern_offset"] = lambda utc: 5.5
    if "eastern_to_utc" in g:
        g["eastern_to_utc"] = lambda local: local - IST
    if "ny_iso" in g:
        to_local = g["to_eastern"]
        g["ny_iso"] = lambda now_utc: "%s+05:30" % to_local(now_utc).replace(microsecond=0).isoformat()

    for name in ("NYSE_HOLIDAYS",):
        if isinstance(g.get(name), dict):
            g[name].clear()
            g[name].update(NSE_HOLIDAYS)
    if isinstance(g.get("NYSE_EARLY_CLOSE"), dict):
        g["NYSE_EARLY_CLOSE"].clear()
    if "CALENDAR_YEARS" in g:
        g["CALENDAR_YEARS"] = NSE_CALENDAR_YEARS
    if "OPEN" in g:
        g["OPEN"] = NSE_OPEN
    if "CLOSE" in g:
        g["CLOSE"] = NSE_CLOSE
    if "OPEN_MIN" in g:
        # The Time Machine needs every 5-minute bar of a session. Yahoo leaves NSE's
        # 15:15-15:30 bars empty (the closing-price window), so its session ends at
        # 15:15: 72 bars, which still covers the 15:10 flat-by.
        g["OPEN_MIN"] = NSE_OPEN.hour * 60 + NSE_OPEN.minute
        g["CLOSE_MIN"] = YAHOO_NSE_LAST_FULL_BAR_END.hour * 60 + YAHOO_NSE_LAST_FULL_BAR_END.minute
        g["SESSION_BARS"] = (g["CLOSE_MIN"] - g["OPEN_MIN"]) // 5
    if isinstance(g.get("DEFAULTS"), dict):
        for k, v in NSE_DEFAULTS.items():
            if k in g["DEFAULTS"]:
                g["DEFAULTS"][k] = v
    if "FLAT_BY" in g:
        g["FLAT_BY"] = NSE_DEFAULTS["flat_by"]
    if "NO_ENTRIES_AFTER" in g:
        g["NO_ENTRIES_AFTER"] = NSE_DEFAULTS["no_entries_after"]
    if "DAILY_MIN_PRICE" in g:
        g["DAILY_MIN_PRICE"], g["DAILY_MIN_VOLUME"] = 20.0, 250000

    for name in ("to_yahoo", "yahoo_symbol"):
        if name in g:
            g[name] = nse_yahoo
    if "spark_path" in g:
        g["spark_path"] = lambda symbols, rng, interval: "/v7/finance/spark?symbols=%s&range=%s&interval=%s" % (
            ",".join(_q(s) for s in symbols), rng, interval)
    if "chart_path" in g:
        g["chart_path"] = lambda sym, rng, interval: "/v8/finance/chart/%s?range=%s&interval=%s" % (
            _q(sym), rng, interval)

    if not isinstance(sys.stdout, _Localized):
        sys.stdout = _Localized(sys.stdout)
        sys.stderr = _Localized(sys.stderr)
    return m
