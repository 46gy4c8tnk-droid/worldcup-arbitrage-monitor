"""核心引擎：轮询行情 -> 检测套利 -> 模拟下单 -> 开赛结算 -> 净值记录。"""
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
    "mode": "demo",                # demo: 本地模拟行情 / live: The Odds API 真实赔率
    "odds_api_key": "",
    "sport_key": "auto",           # auto = 自动查找世界杯赛事 key
    "region": "au",                # 澳洲博彩商
    "poll_interval_live": 7200,    # 真实模式轮询间隔（秒），免费配额下建议 >= 5400
    "poll_interval_demo": 15,
    "min_profit_pct": 0.3,         # 低于该利润率的机会不下单
    "stake_fraction": 0.10,        # 单组套利投入 = 余额 * 该比例（受 max_stake 限制）
    "max_stake": 2000.0,
    "initial_balance": 10000.0,
    "auto_bet": True,
    # Betfair 是交易所赔率（未扣 ~5% 佣金），与博彩商直接比价会产生假套利
    "excluded_bookmakers": ["Betfair"],
}

# 真实模式轮询间隔下限，防止误配置打爆 API 配额
MIN_LIVE_INTERVAL = 60

# 各博彩商官网（API 不提供具体盘口深链，给首页方便手动下单时快速跳转）
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
        self.next_poll_at = 0       # 立即开始第一轮
        self.force_flag = False
        self.backoff = 1
        self.net_retries = 0

    # ---------- 配置 ----------

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

    # ---------- 数据库 ----------

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
            pass  # 列已存在
        if self._meta("balance") is None:
            self._set_meta("balance", self.config["initial_balance"])
            self._set_meta("resets", 0)
            self._record_equity("初始资金")
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

    # ---------- 主循环 ----------

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
            except Exception as e:  # 任何异常都不能杀死主循环
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
                elif "429" in msg or "频繁" in msg:
                    # 限流：指数退避，避免继续打爆配额
                    self.backoff = min(self.backoff * 2, 8)
                    self.next_poll_at = now + interval * self.backoff
                elif "网络" in msg or "ConnectionError" in msg or "Timeout" in msg:
                    # 瞬时网络抖动：60s 起快速重试，最多到正常间隔，不拉长到几小时
                    self.net_retries = min(self.net_retries + 1, 5)
                    self.next_poll_at = now + min(60 * self.net_retries, interval)
                else:
                    # 配置类错误（401/422 等）：按正常间隔，重试也需先修正配置
                    self.next_poll_at = now + interval
                self._log("error", f"行情获取失败：{msg}")
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
                          f"抓取成功：{len(events)} 场比赛，"
                          f"{len(self.opportunities)} 个套利机会")

    def _scan(self, events):
        """每场比赛取各结果最优赔率，构建市场总览，对真实套利自动下单。

        返回 (opportunities, market_view)：
        - opportunities：真实套利（profit_pct > 0），会触发自动下单；
        - market_view：所有比赛的最优组合与套利率（含负值，按接近套利排序），
          即使没有机会，页面每轮也有鲜活数据，并能看到市场离套利有多近。
        """
        opportunities = []
        market_view = []
        self._market_index = {}   # event_id -> 比赛快照，供手动下单复算注金
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
        if profit_pct < self.config["min_profit_pct"]:
            return "低于利润率阈值"
        if not self.config["auto_bet"]:
            return "自动下单已关闭"
        exists = self.db.execute(
            "SELECT 1 FROM arb_groups WHERE event_id=? AND status='open'",
            (ev["event_id"],),
        ).fetchone()
        if exists:
            return "已持仓"
        if ev["commence_ts"] <= time.time():
            return "已开赛"

        balance = self._meta("balance")
        total = min(balance * self.config["stake_fraction"],
                    self.config["max_stake"], balance)
        if total < 10:
            return "余额不足"

        legs = self._alloc_legs(best, inv, total)
        payout = round(min(o * s for _, _, o, s in legs), 2)
        total_stake = round(sum(s for _, _, _, s in legs), 2)
        if payout <= total_stake:
            return "取整后无利润"

        self._place_group(ev["event_id"], ev["match_name"], ev["commence_ts"],
                          legs, profit_pct, "auto")
        return "已下单"

    @staticmethod
    def _alloc_legs(best, inv, total):
        """按 1/odds 比例分配注金，使各结果赔付一致。返回 (outcome, bookie, odds, stake) 列表。"""
        return [
            (outcome, bookie, odds, round(total * (1 / odds) / inv, 2))
            for outcome, (bookie, odds) in best.items()
        ]

    def _place_group(self, event_id, match_name, commence_ts, legs,
                     profit_pct, source):
        """记一组套利下单（auto 自动 / manual 手动），扣减余额并记账。"""
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
        tag = "自动下单" if source == "auto" else "手动下单"
        self._log("bet",
                  f"{tag} {match_name}：投入 {total_stake:.2f}，"
                  f"锁定利润 {payout - total_stake:.2f}（{profit_pct:.2f}%）")
        self._record_equity(f"下单 {match_name}")
        self._check_reset()
        return group_id

    def record_manual_bet(self, event_id, total_stake):
        """用户手动在各博彩商下注后，按当前最优赔率记入模拟账户。"""
        with self.lock:
            snap = getattr(self, "_market_index", {}).get(event_id)
            if not snap:
                return {"ok": False, "error": "该比赛已不在当前行情中，请先抓取最新赔率"}
            total = float(total_stake)
            balance = self._meta("balance")
            if total < 10:
                return {"ok": False, "error": "投入金额过小"}
            if total > balance:
                return {"ok": False, "error": f"超出可用余额（{balance:.2f}）"}

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
                # 套利组合赔付与赛果无关，开赛即按锁定赔付结算
                balance = round(self._meta("balance") + g["payout"], 2)
                self._set_meta("balance", balance)
                self.db.execute(
                    "UPDATE arb_groups SET status='settled', settled_at=? WHERE id=?",
                    (now, g["id"]),
                )
                self.db.commit()
                profit = g["payout"] - g["total_stake"]
                self._log("settle",
                          f"结算 {g['match_name']}：+{g['payout']:.2f}"
                          f"（利润 {profit:+.2f}）")
                self._record_equity(f"结算 {g['match_name']}")

    def _check_reset(self):
        if self._meta("balance") <= 0.005:
            self._do_reset("余额归零，自动重置")

    def reset_account(self, reason="manual"):
        with self.lock:
            self.db.execute(
                "UPDATE arb_groups SET status='voided' WHERE status='open'")
            self.db.commit()
            self._do_reset("手动重置" if reason == "manual" else reason)

    def _do_reset(self, note):
        initial = self.config["initial_balance"]
        self._set_meta("balance", initial)
        self._set_meta("resets", int(self._meta("resets") or 0) + 1)
        self.db.commit()
        self._log("reset", f"{note}，资金恢复至 {initial:.2f} AUD")
        self._record_equity(note)

    # ---------- 状态快照 ----------

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
