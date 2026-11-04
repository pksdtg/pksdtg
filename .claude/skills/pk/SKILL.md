---
name: pk
description: >-
  pk: Piyush's saved buy condition, run as a scan of every NIFTY 200 stock and of
  NIFTY options priced Rs 100-150. The condition is trader-piyush's seven
  checkpoints on 5-minute bars: Supertrend (7, 0.8) and (7, 1.6) up, RSI (14) above
  50, SMI (10, 3, 3, 10) above its signal, NIFTY 50 agreeing, inside the entry
  window, and a stop that fits the max loss. Prints every stock that passes with
  entry, stop, 2R target and size, the NIFTY options on NIFTY's side (calls when
  NIFTY is up, puts when down) checked on their own 5-minute charts, and a one-line
  summary for a phone alert. Use when the user says "check with pk", "pk", "run pk",
  "pk scan", "scan with my condition", or when the pk market-hours alert fires.
argument-hint: "[--stocks SBIN,MARICO] [--now HH:MM] [--premium 100-150] [--new-only]"
---

# pk

Piyush named his condition **pk**. "Check with pk" means: run this scan now and
report the data.

`${CLAUDE_SKILL_DIR}` below is this skill's folder.

```bash
python3 "${CLAUDE_SKILL_DIR}/pk.py" scan                        # NIFTY 200 + NIFTY options, now
python3 "${CLAUDE_SKILL_DIR}/pk.py" scan --all                  # every liquid NSE stock, not just NIFTY 200
python3 "${CLAUDE_SKILL_DIR}/pk.py" scan --stocks SBIN,MARICO   # your own list
python3 "${CLAUDE_SKILL_DIR}/pk.py" scan --premium 80-120       # another premium band
python3 "${CLAUDE_SKILL_DIR}/pk.py" scan --new-only             # only signals not reported yet today
python3 "${CLAUDE_SKILL_DIR}/pk.py" scan --now "2026-11-03 13:00" --no-options   # replay stocks
```

A full scan takes about 2 minutes (200 stocks, one request each, a polite pause).
`--all` ("scan with pk in all") reads every stock in `trader/universe.txt`
(`scan.py universe`, about 2,600), keeps the ones with a price of Rs 20 or more and
250,000+ shares traded in the latest session (Yahoo's batch quotes; during market
hours that is today's volume so far), and checks those with four requests at a time:
about 650 stocks in 1.5 minutes.

## What it reports

1. NIFTY 50: both Supertrends, RSI, SMI against its signal, and the side it gives:
   up (calls), down (puts) or mixed (no side).
2. Every NIFTY 200 stock that passes all seven checkpoints, cleanest first (no
   warnings, then the tightest stop): entry, stop at the Supertrend (7, 1.6) line,
   2R target, shares for `trader/config.json` (max loss, position cap, cash buffer)
   and the warnings (SMI above +40, RSI above 75, a 1-2 bar old Supertrend flip).
3. NIFTY options: the nearest weekly expiry, calls (CE) and puts (PE) with premium
   inside the band (`--sides auto` lists only NIFTY's side). An option can pass only
   on NIFTY's side: "NIFTY agrees" is one of its checkpoints. Each one's own 5-minute chart is built from NSE's intraday
   prices for the contract and gets the four chart checkpoints, NIFTY agreeing and the entry window. The chart starts
   at 9:15 each day, so the indicators are ready from about 11:25 IST; before that
   the option is listed with "indicators not ready". A passing option gets a stop
   at its Supertrend (7, 1.6) line, a 2R target and the lots (NIFTY lot: 65, from
   NSE) that fit the max loss, capped at `max_option_lots` in `trader/config.json`
   (1: Piyush trades one lot).
   The option chain line gives the put-call ratio of open interest and the strikes
   with the most call OI (resistance) and put OI (support); each option shows its
   implied volatility, OI and change in OI, and the confirmations its chart has.
4. Confirmations (VWAP, EMA 9/21, MACD, ADX, volume; see trader-piyush) for every
   stock and option, as "confirm 4/5 [VWAP+ EMA+ MACD+ ADX- VOL+]". Stocks are
   sorted by them. `--min-confirm N` (or `"pk_min_confirm"` in trader/config.json)
   drops stocks with fewer; the default 0 keeps Piyush's seven checkpoints as the
   only rule.
5. `SUMMARY ...`: one line under 200 characters, options first, then the top three
   stocks and "+N more".

`trader/state/pk.json` keeps what was already reported today, for `--new-only`.

## How to brief the user

Lead with NIFTY's side, then the stocks that passed with no warnings, then the
options. Give the time of the last bar and say the data is Yahoo's (stocks) and
NSE's (options). If dozens pass, say so: on a strong day most stocks agree with
NIFTY, and the list is a starting point, not a pick. Offer /trader-news for the
one the user looks at, and /trader-exit to set the plan.

## Market-hours alerts

The alert is a session scheduled job: every 5 minutes from 9:30 to 15:25 IST on
weekdays it runs `pk.py scan --new-only`. When something new passes, it sends a
phone notification with the SUMMARY line and posts the details in the chat. Jobs
like this last 7 days and only run while this Claude session is open; set it up
again after that.

## Rules

- A pass is a rule check, not a buy signal or a promise. Never call a trade sure or safe.
- Options can lose the whole premium quickly, and more so on expiry day. Say so when
  you report an option.
- Replays (`--now` in the past) are exact for stocks only: the option chain is
  always today's.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
