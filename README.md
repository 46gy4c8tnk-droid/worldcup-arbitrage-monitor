# World Cup Cross-Book Arbitrage Monitor

*English | [中文](README.zh.md)*

A dashboard that monitors **1X2 (match-result) odds for World Cup matches across Australian
bookmakers**, detects cross-book arbitrage (surebets), and runs a **10,000 AUD paper-trading
account** that auto-bets opportunities, settles at kickoff, and charts the equity curve.

> ⚠️ **Paper money only — this tool places no real bets.** It is for study and research.

![mode: live](https://img.shields.io/badge/data-The%20Odds%20API-blue) ![python](https://img.shields.io/badge/python-3.10%2B-green) ![license](https://img.shields.io/badge/license-MIT-lightgrey)

## Features

- **Live odds** via [The Odds API](https://the-odds-api.com) — a legal aggregator of Australian
  bookmaker prices (Sportsbet, TAB, Ladbrokes, Neds, PointsBet, Unibet, Betr, PlayUp, …).
- **Arbitrage detection**: for each match, if `1/best₁ + 1/best₂ + 1/best₃ < 1`, it's a surebet.
- **Paper account**: 10,000 AUD, **auto-resets to 10,000 if it ever hits zero** (reset count tracked).
- **Auto-betting**: opportunities above the profit threshold are staked by `1/odds` so every
  outcome pays the same; profit is locked at bet time and realized at kickoff.
- **Bet Helper (semi-auto / manual)**: enter a total stake → get the exact amount per leg,
  guaranteed return/P&L, and direct links to each bookmaker; place them yourself, then log it.
- **Market overview**: how close *every* match is to an arb (even negative), refreshed each poll.
- **Equity curve, bet history, event log, desktop alerts.**
- **Bilingual UI** — toggle 中文 / English with the top-right button.

## Quick start

```bash
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:8788**.

1. Get a free API key at [the-odds-api.com](https://the-odds-api.com).
2. Paste it into the **Settings** panel and click **Save**. (It is stored in `config.json`,
   which is git-ignored — your key never leaves your machine.)
3. The World Cup sport key is auto-detected.

## Fetch frequency & quota (important)

**Do not scrape Sportsbet/TAB pages directly** — it violates their ToS and risks IP bans.
This project reads odds through a legal aggregator API instead.

The Odds API free tier is **500 credits/month**; one poll of `h2h` + `au` costs **1 credit**:

| Interval | ~Monthly | Verdict |
|---|---|---|
| 2 hours | ~360 | ✅ safe |
| 90 min | ~480 | ⚠️ near the cap (default) |
| 30 min | ~1440 | ❌ over |

Built-in protection: 60s hard floor on the interval; exponential backoff on HTTP 429;
fast retry on transient network errors; remaining quota shown live in the UI.

## Paper-account rules

- Stake per arb = `min(balance × stake_fraction, max_stake)`, split across legs by `1/odds`.
- One open position per match (no double-betting the same event).
- Settles at **kickoff**, not full-time — an arb's payout is independent of the result, so the
  profit is locked once all legs are placed.
- Equity = available balance + locked payout of open positions; the curve plots equity.

## Project structure

```
app.py             Flask server (state / config / poll / reset / manual_bet APIs + static page)
engine.py          Core engine: polling, arb detection, paper betting, settlement, reset
datasources.py     The Odds API client
static/index.html  Dashboard (Chart.js equity curve, tables, settings, bet helper, i18n)
config.example.json  Config template (copy to config.json, or set the key in the UI)
config.json        Runtime config incl. API key — GIT-IGNORED, never committed
arb.db             SQLite data (bets, equity, logs) — git-ignored; delete to wipe clean
```

## Disclaimer

For study and research only. Real-world arbitrage betting carries risks not modeled here —
odds movement, partial fills (leg risk), bookmaker stake limits and account closures — and
gambling is heavily regulated across Australian states. **This tool executes no real bets.**

## License

[MIT](LICENSE)
