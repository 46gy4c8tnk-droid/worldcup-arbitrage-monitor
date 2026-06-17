"""Core engine: poll odds -> detect arbitrage -> paper-bet -> settle at kickoff -> record equity."""
import json
import os
import sqlite3
import threading
import time
import uuid

import datasources

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "arb.db")
CONFIG_PATH = os.path.join(BASE, "config.json")

DEFAULT_CONFIG = {
    "mode": "live",                # always The Odds API live odds (demo toggle removed from UI)
    "odds_api_key": "",
    "sport_key": "auto",           # auto = discover the World Cup sport key
    "region": "au",                # Australian bookmakers
    "poll_interval_live": 7200,    # live poll interval (s); >= 5400 recommended on the free tier
    "poll_interval_demo": 15,
    "min_profit_pct": 0.3,         # skip opportunities below this profit margin
    "stake_fraction": 0.10,        # stake per arb = balance * this fraction (capped by max_stake)
    "max_stake": 2000.0,
    "initial_balance": 10000.0,
    "auto_bet": True,
    # Betfair is exchange odds (before ~5% commission); comparing it to bookmakers fakes arbs
    "excluded_bookmakers": ["Betfair"],
}

# Hard floor on the live poll interval, to stop a misconfig from burning the API quota
MIN_LIVE_INTERVAL = 60

# Bookmaker home pages (the API has no deep links to specific markets; these speed up manual betting)
BOOKMAKER_LINKS = {
    "sportsbet": "https://www.sportsbet.com.au/betting/soccer",
    "tab": "https://www.tab.com.au/sports/betting/Soccer",
    "ladbrokes": "https://www.ladbrokes.com.au/sports/soccer/",
    "neds": "https://www.neds.com.au/sports/soccer",
    "pointsbet": "https://pointsbet.com.au/sports/soccer",
    "unibet": "https://www.unibet.com.au/betting/sports/filter/football",
    "betr": "https://www.betr.com.au/sports/soccer",
    "playup": "https://www.playup.com.au/sports/soccer",
    "bet365": "https://www.bet365.com.au/",
    "betfair": "https://www.betfair.com.au/exchange/plus/football",
}


class Engine:
    def __init__(self):
        self.lock = threading.RLock()
        self.config = self._load_config()
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._init_db()

        self.sources = {
            "demo": datasources.DemoSource(),
            "live": datasources.OddsApiSource(),
        }
        self.opportunities = []
        self.market_view = []
        self.quota = None
        self.last_poll_ts = None
        self.last_error = None
        self.next_poll_at = 0       # start the first poll immediately
        self.force_flag = False
        self.backoff = 1
        self.net_retries = 0

    # ---------- config ----------

    def _load_config(self):
        cfg = dict(DEFAULT_CONFIG)
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg.update(json.load(f))
            except (json.JSONDecodeError, OSError):
                pass
        return cfg

    def _save_config(self):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)

    def update_config(self, data):
        with self.lock:
            for key in DEFAULT_CONFIG:
                if key not in data:
                    continue
                value = data[key]
                if key in ("poll_interval_live", "poll_interval_demo",
                           "min_profit_pct", "stake_fraction",
                           "max_stake", "initial_balance"):
                    value = float(value)
                if key == "auto_bet":
                    value = bool(value)
                if key == "poll_interval_live":
                    value = max(MIN_LIVE_INTERVAL, value)
                if key == "poll_interval_demo":
                    value = max(5, value)
                self.config[key] = value
            self._save_config()
            self.next_poll_at = min(self.next_poll_at,
                                    time.time() + self._interval())
            return {"ok": True, "config": self._public_config()}

    def _interval(self):
        if self.config["mode"] == "live":
            return max(MIN_LIVE_INTERVAL, self.config["poll_interval_live"])
        return max(5, self.config["poll_interval_demo"])

    def _public_config(self):
        cfg = dict(self.config)
        key = cfg.get("odds_api_key", "")
        cfg["odds_api_key_set"] = bool(key)
        cfg["odds_api_key"] = ("*" * 6 + key[-4:]) if len(key) > 4 else ""
        return cfg

    # ---------- database ----------

    def _init_db(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS arb_groups (
            id TEXT PRIMARY KEY,
            event_id TEXT,
            match_name TEXT,
            commence_ts REAL,
            placed_at REAL,
            settled_at REAL,
            profit_pct REAL,
            total_stake REAL,
            payout REAL,
            status TEXT
        );
        CREATE TABLE IF NOT EXISTS bets (
            id TEXT PRIMARY KEY,
            group_id TEXT,
            bookmaker TEXT,
            outcome TEXT,
            odds REAL,
            stake REAL
        );
        CREATE TABLE IF NOT EXISTS equity (
            ts REAL, balance REAL, equity REAL, note TEXT
        );
        CREATE TABLE IF NOT EXISTS logs (ts REAL, type TEXT, message TEXT);
        """)
        try:
            self.db.execute(
                "ALTER TABLE arb_groups ADD COLUMN source TEXT DEFAULT 'auto'")
        except sqlite3.OperationalError:
            pass  # column already exists
        if self._meta("balance") is None:
            self._set_meta("balance", self.config["initial_balance"])
            self._set_meta("resets", 0)
            self._record_equity("Initial funds")
        self.db.commit()

    def _meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else float(row["value"])

    def _set_meta(self, key, value):
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def _log(self, type_, message):
        self.db.execute("INSERT INTO logs VALUES(?,?,?)",
                        (time.time(), type_, message))
        self.db.execute(
            "DELETE FROM logs WHERE ts NOT IN "
            "(SELECT ts FROM logs ORDER BY ts DESC LIMIT 300)")
        self.db.commit()

    def _open_payout_sum(self):
        row = self.db.execute(
            "SELECT COALESCE(SUM(payout),0) s FROM arb_groups WHERE status='open'"
        ).fetchone()
        return row["s"]

    def _record_equity(self, note):
        balance = self._meta("balance")
        equity = balance + self._open_payout_sum()
        self.db.execute("INSERT INTO equity VALUES(?,?,?,?)",
                        (time.time(), balance, equity, note))
        self.db.commit()

    # ---------- main loop ----------

    def start(self):
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        while True:
            try:
                now = time.time()
                with self.lock:
                    due = self.force_flag or now >= self.next_poll_at
                if due:
                    self._poll()
                self._settle_due()
            except Exception as e:  # no exception may kill the main loop
                with self.lock:
                    self.last_error = str(e)
            time.sleep(1)

    def force_poll(self):
        with self.lock:
            self.force_flag = True

    def _poll(self):
        with self.lock:
            self.force_flag = False
            mode = self.config["mode"]
            source = self.sources[mode]
            cfg = dict(self.config)
        try:
            events, quota = source.get_events(cfg)
        except Exception as e:
            with self.lock:
                msg = str(e)
                self.last_error = msg
                now = time.time()
                interval = self._interval()
                if mode != "live":
                    self.next_poll_at = now + interval
                elif "429" in msg:
                    # Rate limited: exponential backoff so we stop hammering the quota
                    self.backoff = min(self.backoff * 2, 8)
                    self.next_poll_at = now + interval * self.backoff
                elif "Network request failed" in msg or "ConnectionError" in msg or "Timeout" in msg:
                    # Transient network blip: retry fast (60s up), capped at the normal interval
                    self.net_retries = min(self.net_retries + 1, 5)
                    self.next_poll_at = now + min(60 * self.net_retries, interval)
                else:
                    # Config errors (401/422, ...): normal interval; a retry needs a config fix first
                    self.next_poll_at = now + interval
                self._log("error", f"Fetch failed: {msg}")
            return

        with self.lock:
            self.backoff = 1
            self.net_retries = 0
            self.last_error = None
            self.last_poll_ts = time.time()
            self.next_poll_at = self.last_poll_ts + self._interval()
            if quota:
                self.quota = quota
            self.opportunities, self.market_view = self._scan(events)
            if mode == "live":
                self._log("poll",
                          f"Fetched: {len(events)} matches, "
                          f"{len(self.opportunities)} arb(s)")

    def _scan(self, events):
        """Take each outcome's best odds, build the market overview, auto-bet real arbs.

        Returns (opportunities, market_view):
        - opportunities: real arbs (profit_pct > 0) that trigger auto-betting;
        - market_view: best combo + arb % for every match (incl. negative, sorted by
          closeness to an arb), so the page always has fresh data and shows how close
          the market is to an arb even when there is none.
        """
        opportunities = []
        market_view = []
        self._market_index = {}   # event_id -> snapshot, used to re-size manual bets
        with self.lock:
            for ev in events:
                best = {}
                for outcome, offers in ev["prices"].items():
                    bookie, odds = max(offers, key=lambda x: x[1])
                    best[outcome] = (bookie, odds)
                if len(best) < 3:
                    continue
                inv = sum(1 / odds for _, odds in best.values())
                profit_pct = (1 / inv - 1) * 100

                legs = [{"outcome": o, "bookmaker": b, "odds": odds}
                        for o, (b, odds) in best.items()]
                entry = {
                    "event_id": ev["event_id"],
                    "match_name": ev["match_name"],
                    "commence_ts": ev["commence_ts"],
                    "profit_pct": round(profit_pct, 3),
                    "is_arb": inv < 1,
                    "legs": legs,
                }
                market_view.append(entry)
                self._market_index[ev["event_id"]] = {
                    "match_name": ev["match_name"],
                    "commence_ts": ev["commence_ts"],
                    "best": best,
                    "inv": inv,
                }

                if inv < 1:
                    opp = dict(entry)
                    opp["status"] = self._maybe_bet(ev, best, inv, profit_pct)
                    opportunities.append(opp)
        opportunities.sort(key=lambda x: -x["profit_pct"])
        market_view.sort(key=lambda x: -x["profit_pct"])
        return opportunities, market_view

    def _maybe_bet(self, ev, best, inv, profit_pct):
        """Returns a language-neutral status code; the frontend translates it for display."""
        if profit_pct < self.config["min_profit_pct"]:
            return "below_threshold"
        if not self.config["auto_bet"]:
            return "autobet_off"
        exists = self.db.execute(
            "SELECT 1 FROM arb_groups WHERE event_id=? AND status='open'",
            (ev["event_id"],),
        ).fetchone()
        if exists:
            return "held"
        if ev["commence_ts"] <= time.time():
            return "started"

        balance = self._meta("balance")
        total = min(balance * self.config["stake_fraction"],
                    self.config["max_stake"], balance)
        if total < 10:
            return "insufficient"

        legs = self._alloc_legs(best, inv, total)
        payout = round(min(o * s for _, _, o, s in legs), 2)
        total_stake = round(sum(s for _, _, _, s in legs), 2)
        if payout <= total_stake:
            return "no_profit_rounded"

        self._place_group(ev["event_id"], ev["match_name"], ev["commence_ts"],
                          legs, profit_pct, "auto")
        return "placed"

    @staticmethod
    def _alloc_legs(best, inv, total):
        """Split stakes by 1/odds so every outcome pays the same. Returns (outcome, bookie, odds, stake)."""
        return [
            (outcome, bookie, odds, round(total * (1 / odds) / inv, 2))
            for outcome, (bookie, odds) in best.items()
        ]

    def _place_group(self, event_id, match_name, commence_ts, legs,
                     profit_pct, source):
        """Record one arb bet group (auto / manual): deduct balance and book it."""
        total_stake = round(sum(s for _, _, _, s in legs), 2)
        payout = round(min(o * s for _, _, o, s in legs), 2)
        group_id = uuid.uuid4().hex[:10]
        self.db.execute(
            "INSERT INTO arb_groups "
            "(id,event_id,match_name,commence_ts,placed_at,settled_at,"
            "profit_pct,total_stake,payout,status,source) "
            "VALUES(?,?,?,?,?,NULL,?,?,?,'open',?)",
            (group_id, event_id, match_name, commence_ts, time.time(),
             round(profit_pct, 3), total_stake, payout, source),
        )
        for outcome, bookie, odds, stake in legs:
            self.db.execute(
                "INSERT INTO bets VALUES(?,?,?,?,?,?)",
                (uuid.uuid4().hex[:10], group_id, bookie, outcome, odds, stake),
            )
        self._set_meta("balance", round(self._meta("balance") - total_stake, 2))
        self.db.commit()
        tag = "Auto bet" if source == "auto" else "Manual bet"
        self._log("bet",
                  f"{tag} {match_name}: stake {total_stake:.2f}, "
                  f"locked profit {payout - total_stake:.2f} ({profit_pct:.2f}%)")
        self._record_equity(f"Bet {match_name}")
        self._check_reset()
        return group_id

    def record_manual_bet(self, event_id, total_stake):
        """After the user places bets at each bookmaker, book it into the paper account."""
        with self.lock:
            snap = getattr(self, "_market_index", {}).get(event_id)
            if not snap:
                return {"ok": False, "error": "match no longer in current odds; fetch the latest first"}
            total = float(total_stake)
            balance = self._meta("balance")
            if total < 10:
                return {"ok": False, "error": "stake too small"}
            if total > balance:
                return {"ok": False, "error": f"exceeds available balance ({balance:.2f})"}

            inv = snap["inv"]
            legs = self._alloc_legs(snap["best"], inv, total)
            profit_pct = (1 / inv - 1) * 100
            gid = self._place_group(event_id, snap["match_name"],
                                    snap["commence_ts"], legs, profit_pct, "manual")
            return {"ok": True, "group_id": gid}

    def _settle_due(self):
        with self.lock:
            now = time.time()
            rows = self.db.execute(
                "SELECT * FROM arb_groups WHERE status='open' AND commence_ts<=?",
                (now,),
            ).fetchall()
            for g in rows:
                # An arb's payout is independent of the result; settle at kickoff by locked payout
                balance = round(self._meta("balance") + g["payout"], 2)
                self._set_meta("balance", balance)
                self.db.execute(
                    "UPDATE arb_groups SET status='settled', settled_at=? WHERE id=?",
                    (now, g["id"]),
                )
                self.db.commit()
                profit = g["payout"] - g["total_stake"]
                self._log("settle",
                          f"Settled {g['match_name']}: +{g['payout']:.2f} "
                          f"(profit {profit:+.2f})")
                self._record_equity(f"Settled {g['match_name']}")

    def _check_reset(self):
        if self._meta("balance") <= 0.005:
            self._do_reset("Balance hit zero, auto-reset")

    def reset_account(self, reason="manual"):
        with self.lock:
            self.db.execute(
                "UPDATE arb_groups SET status='voided' WHERE status='open'")
            self.db.commit()
            self._do_reset("Manual reset" if reason == "manual" else reason)

    def _do_reset(self, note):
        initial = self.config["initial_balance"]
        self._set_meta("balance", initial)
        self._set_meta("resets", int(self._meta("resets") or 0) + 1)
        self.db.commit()
        self._log("reset", f"{note}; funds restored to {initial:.2f} AUD")
        self._record_equity(note)

    # ---------- state snapshot ----------

    def snapshot(self):
        with self.lock:
            balance = self._meta("balance")
            resets = int(self._meta("resets") or 0)
            equity_rows = self.db.execute(
                "SELECT * FROM equity ORDER BY ts DESC LIMIT 500").fetchall()
            open_groups = self._groups_with_legs("status='open'")
            history = self._groups_with_legs(
                "status IN ('settled','voided') ORDER BY settled_at DESC, placed_at DESC LIMIT 100")
            logs = self.db.execute(
                "SELECT * FROM logs ORDER BY ts DESC LIMIT 80").fetchall()
            stats = self.db.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(payout-total_stake),0) p, "
                "COALESCE(AVG(profit_pct),0) a "
                "FROM arb_groups WHERE status='settled'").fetchone()

            open_payout = self._open_payout_sum()
            equity_now = balance + open_payout
            initial = self.config["initial_balance"]
            return {
                "balance": balance,
                "equity": round(equity_now, 2),
                "return_pct": round((equity_now / initial - 1) * 100, 3),
                "resets": resets,
                "stats": {
                    "settled_count": stats["n"],
                    "total_profit": round(stats["p"], 2),
                    "avg_arb_pct": round(stats["a"], 3),
                    "open_count": len(open_groups),
                },
                "equity_curve": [
                    {"ts": r["ts"], "balance": r["balance"],
                     "equity": r["equity"], "note": r["note"]}
                    for r in reversed(equity_rows)
                ],
                "opportunities": self.opportunities,
                "market_view": self.market_view,
                "open_groups": open_groups,
                "history": history,
                "logs": [dict(r) for r in logs],
                "config": self._public_config(),
                "bookmaker_links": BOOKMAKER_LINKS,
                "quota": self.quota,
                "last_poll_ts": self.last_poll_ts,
                "next_poll_at": self.next_poll_at,
                "last_error": self.last_error,
                "now": time.time(),
            }

    def _groups_with_legs(self, where):
        groups = [dict(r) for r in self.db.execute(
            f"SELECT * FROM arb_groups WHERE {where}").fetchall()]
        for g in groups:
            g["legs"] = [dict(r) for r in self.db.execute(
                "SELECT bookmaker, outcome, odds, stake FROM bets WHERE group_id=?",
                (g["id"],)).fetchall()]
            g["profit"] = round(g["payout"] - g["total_stake"], 2)
        return groups
