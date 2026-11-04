---
name: trader-piyush
description: >-
  Piyush checkpoints. Checks NSE stocks against Piyush's own 5-minute chart setup:
  Supertrend (7, 0.8), Supertrend (7, 1.6), RSI (14) against 50 and the Stochastic
  Momentum Index (10, 3, 3, 10) against its signal line, with NIFTY 50 as the market
  filter, the entry window and the max loss as further checkpoints. Prints PASS or
  FAIL for each checkpoint, warnings (SMI above +40, RSI above 75, a fresh Supertrend
  flip, stale data), and for a stock that passes them all a plan: entry, stop at the
  Supertrend (7, 1.6) line, 2R target and the shares that fit the max loss. Use when
  the user says "check my indicators", "Piyush checkpoints", "Supertrend check", "does
  it pass my setup", "check SBIN on my indicators", or before /trader-exit on NSE.
argument-hint: "TICKER [TICKER ...] | --watchlist trader/watchlist.json [--now HH:MM]"
---

# trader-piyush

Piyush trades from a 5-minute chart with four studies: Supertrend (7, 0.8),
Supertrend (7, 1.6), RSI (14) and the Stochastic Momentum Index (10, 3, 3, 10,
EMA), and looks at NIFTY 50 the same way. This tool turns that chart into
checkpoints a stock must pass before a long trade is planned.

`${CLAUDE_SKILL_DIR}` below is this skill's folder.

```bash
python3 "${CLAUDE_SKILL_DIR}/piyush.py" check SBIN MARICO KOTAKBANK
python3 "${CLAUDE_SKILL_DIR}/piyush.py" check --watchlist trader/watchlist.json --only-pass
python3 "${CLAUDE_SKILL_DIR}/piyush.py" check SBIN --now "2026-11-03 15:20"   # replay, India time
python3 "${CLAUDE_SKILL_DIR}/piyush.py" check SBIN --json
```

## The checkpoints (long side)

| # | checkpoint | passes when |
| --- | --- | --- |
| 1 | Supertrend (7, 0.8) up | the fast line is green, below the price |
| 2 | Supertrend (7, 1.6) up | the slow line is green, below the price |
| 3 | RSI (14) above 50 | momentum is on the buyers' side |
| 4 | SMI above its signal | the black line is above the red one |
| 5 | NIFTY 50 agrees | NIFTY's two Supertrends are up and its RSI is above 50 |
| 6 | inside the entry window | an NSE trading day, between `no_entries_before` and `no_entries_after` (09:30-15:00) |
| 7 | stop fits the max loss | the Supertrend (7, 1.6) line is close enough for at least one share within `max_loss_per_trade_usd` (rupees) |

Confirmations, a score next to the checkpoints (not checkpoints): price above the
session VWAP, EMA 9 above EMA 21, MACD (12, 26, 9) histogram above 0, ADX (14)
above 20 with +DI above -DI, and the last bar's volume at least 1.5x its 20-bar
average. Studies that need volume are n/a for NIFTY 50 and for option charts. In
the 60-session backtest (`tools/piyush_backtest_confirm.py`, 20 large stocks, 964
trades) passes with 1-2 confirmations averaged -0.19R and -0.07R before costs,
3 of 5 +0.12R, 4 of 5 +0.06R, 5 of 5 +0.20R (87 trades); after about 0.05% round-trip
costs only 5 of 5 stayed positive (+0.08R).

Warnings print as notes and do not fail a stock (price above the upper Bollinger
band (20, 2) is one of them): SMI above +40 (stretched),
RSI above 75, the fast Supertrend turned up only 1-2 bars ago (it can flip
back), and data more than 15 minutes old.

A stock that passes all seven gets a plan: entry at the last close, stop at the
Supertrend (7, 1.6) line rounded down to the paisa, target 2R, and the whole
shares that fit the max loss, the position cap and the cash buffer in
`trader/config.json`. The tool prints the `exit.py plan` command that hands it
to /trader-exit.

## Data

Yahoo's free 5-minute bars for the last 5 sessions, `.NS` tickers and `^NSEI`
for NIFTY 50. Only completed bars count. Yahoo's feed differs a little from a
broker's chart: on SBIN at 15:20 IST on 2026-11-03 this tool put the two
Supertrend lines at 970.95 and 976.90 where Piyush's app showed 974.87 and
981.41, and RSI 33.4 against 28.9. Every PASS or FAIL matched the app. Before
acting, look at the same chart in the broker app.

## Steps

1. Run the check on the names in play (the scanner's hits, or the ones the user
   names), after 9:30 IST so the session has bars.
2. Report each stock's checkpoints: how many passed, which failed and why, and
   the warnings. Lead with the stocks that pass all seven.
3. For a stock that passes, hand the plan to /trader-exit, then
   /trader-timemachine, /trader-budget and /trader-risk. The user decides.
4. Re-run before acting: a 5-minute setup changes every 5 minutes.

## Rules

- A checkpoint is a rule check, not a buy signal. Never say a stock "will" go up,
  and never call a trade safe or sure.
- Say which bar the numbers come from and that the data is Yahoo's.
- Long side only. A stock with every line red is a FAIL, not a short.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
