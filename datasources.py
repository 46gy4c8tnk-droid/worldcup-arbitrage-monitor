"""Data sources: The Odds API (live) / local simulated odds (demo).

Both sources return a unified list of events:
    {
        "event_id": str,
        "match_name": str,
        "commence_ts": float,          # kickoff time (epoch seconds)
        "prices": { outcome: [(bookmaker, odds), ...] }
    }
"""
import random
import time
import uuid

import requests

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Australian bookmakers used by the demo source
AU_BOOKIES = [
    "Sportsbet", "TAB", "Ladbrokes", "Neds",
    "PointsBet", "Unibet", "Betr", "PlayUp",
]

DEMO_TEAMS = [
    "Argentina", "France", "England", "Brazil", "Spain", "Germany", "Portugal", "Netherlands",
    "USA", "Mexico", "Canada", "Japan", "South Korea", "Australia", "Morocco", "Croatia",
    "Uruguay", "Colombia", "Switzerland", "Senegal", "Belgium", "Italy", "Ecuador", "Ghana",
]

DRAW = "Draw"


class DemoSource:
    """Generate realistic 1X2 odds locally, without any external request.

    Each round has ~15% chance of injecting a 0.5%-2.5% arbitrage gap into a
    match, making it easy to watch the full "detect -> auto-bet -> settle" flow.
    """

    def __init__(self):
        self.events = {}

    def get_events(self, _config):
        now = time.time()
        # Drop matches that have kicked off (the engine settles their positions)
        self.events = {k: v for k, v in self.events.items() if v["commence_ts"] > now}
        while len(self.events) < 10:
            ev = self._new_event(now)
            self.events[ev["event_id"]] = ev

        out = []
        for ev in self.events.values():
            prices = self._gen_prices(ev)
            out.append({
                "event_id": ev["event_id"],
                "match_name": ev["match_name"],
                "commence_ts": ev["commence_ts"],
                "prices": prices,
            })
        return out, None

    def _new_event(self, now):
        home, away = random.sample(DEMO_TEAMS, 2)
        sh, sa = random.uniform(0.8, 2.2), random.uniform(0.8, 2.2)
        pd = random.uniform(0.18, 0.30)
        ph = (1 - pd) * sh / (sh + sa)
        pa = 1 - pd - ph
        return {
            "event_id": uuid.uuid4().hex[:12],
            "match_name": f"{home} vs {away}",
            "commence_ts": now + random.uniform(180, 1800),
            "probs": {home: ph, DRAW: pd, away: pa},
            "outcomes": [home, DRAW, away],
        }

    def _gen_prices(self, ev):
        prices = {o: [] for o in ev["outcomes"]}
        for bookie in AU_BOOKIES:
            margin = random.uniform(1.04, 1.08)
            for o in ev["outcomes"]:
                fair = 1 / ev["probs"][o]
                odds = fair / margin * random.uniform(0.985, 1.015)
                prices[o].append((bookie, round(max(1.05, min(odds, 21.0)), 2)))

        # Occasionally inject an arb: lift one outcome's best odds so sum(1/best) < 1
        if random.random() < 0.15:
            best_inv = sum(1 / max(v for _, v in offers) for offers in prices.values())
            target_inv = random.uniform(0.975, 0.995)
            o = random.choice(ev["outcomes"])
            others_inv = best_inv - 1 / max(v for _, v in prices[o])
            need = target_inv - others_inv
            if need > 0.02:
                new_odds = round(1 / need, 2)
                if new_odds < 30:
                    idx = random.randrange(len(prices[o]))
                    prices[o][idx] = (prices[o][idx][0], new_odds)
        return prices


class OddsApiSource:
    """The Odds API client. One poll = one request = one quota credit."""

    def __init__(self):
        self.sport_key = None
        self.excluded = set()   # sport keys without an h2h market (e.g. outright winner)

    def get_events(self, config):
        api_key = config.get("odds_api_key", "").strip()
        if not api_key:
            raise RuntimeError("The Odds API key not set (enter it in Settings)")

        sport = self._resolve_sport(config, api_key)
        # NOTE: error messages must never include the URL/params, or the apiKey leaks into logs
        try:
            resp = requests.get(
                f"{ODDS_API_BASE}/sports/{sport}/odds",
                params={
                    "apiKey": api_key,
                    "regions": config.get("region", "au"),
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                },
                timeout=25,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"Network request failed ({type(e).__name__})") from None
        quota = {
            "remaining": resp.headers.get("x-requests-remaining"),
            "used": resp.headers.get("x-requests-used"),
        }
        if resp.status_code == 401:
            raise RuntimeError("Invalid API key (401)")
        if resp.status_code == 429:
            raise RuntimeError("Rate limited (429); polling slowed automatically")
        if resp.status_code == 422:
            self.excluded.add(sport)
            self.sport_key = None
            raise RuntimeError(f"Event {sport} has no h2h market; excluded and will re-pick")
        if resp.status_code >= 400:
            raise RuntimeError(f"The Odds API returned HTTP {resp.status_code}")

        excluded = [name.lower()
                    for name in config.get("excluded_bookmakers", [])]
        events = []
        for ev in resp.json():
            prices = {}
            for bm in ev.get("bookmakers", []):
                title = bm.get("title", bm.get("key", "?"))
                if any(x in title.lower() for x in excluded):
                    continue
                for market in bm.get("markets", []):
                    if market.get("key") != "h2h":
                        continue
                    for oc in market.get("outcomes", []):
                        prices.setdefault(oc["name"], []).append(
                            (title, float(oc["price"]))
                        )
            # Soccer 1X2 needs all three outcomes priced, else arb detection is wrong
            if len(prices) < 3:
                continue
            commence = ev.get("commence_time")
            ts = _iso_to_ts(commence)
            events.append({
                "event_id": ev["id"],
                "match_name": f"{ev.get('home_team', '?')} vs {ev.get('away_team', '?')}",
                "commence_ts": ts,
                "prices": prices,
            })
        return events, quota

    def _resolve_sport(self, config, api_key):
        configured = config.get("sport_key", "auto")
        if configured and configured != "auto":
            return configured
        if self.sport_key:
            return self.sport_key
        # The /sports list request does not consume quota
        try:
            resp = requests.get(
                f"{ODDS_API_BASE}/sports",
                params={"apiKey": api_key, "all": "false"},
                timeout=25,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"Network request failed ({type(e).__name__})") from None
        if resp.status_code >= 400:
            raise RuntimeError(f"Sports list request failed (HTTP {resp.status_code})")
        candidates = [
            s["key"] for s in resp.json()
            if s["key"].startswith("soccer") and "world_cup" in s["key"]
            and s["key"] not in self.excluded
            and not any(x in s["key"] for x in ("winner", "qualifier"))
        ]
        if not candidates:
            raise RuntimeError(
                "No live World Cup match markets found on The Odds API; "
                "set sport_key manually in config.json"
            )
        # The match (per-game) market has the shortest key; derived markets carry suffixes
        self.sport_key = sorted(candidates, key=len)[0]
        return self.sport_key


def _iso_to_ts(iso_str):
    if not iso_str:
        return time.time() + 86400
    import datetime
    try:
        dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except ValueError:
        return time.time() + 86400
