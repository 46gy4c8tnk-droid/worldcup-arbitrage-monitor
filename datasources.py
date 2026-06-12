"""数据源：The Odds API（真实模式） / 本地模拟行情（演示模式）。

两种数据源都返回统一结构的事件列表：
    {
        "event_id": str,
        "match_name": str,
        "commence_ts": float,          # 开赛时间（epoch 秒）
        "prices": { outcome: [(bookmaker, odds), ...] }
    }
"""
import random
import time
import uuid

import requests

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# 演示模式使用的澳洲主流博彩商
AU_BOOKIES = [
    "Sportsbet", "TAB", "Ladbrokes", "Neds",
    "PointsBet", "Unibet", "Betr", "PlayUp",
]

DEMO_TEAMS = [
    "阿根廷", "法国", "英格兰", "巴西", "西班牙", "德国", "葡萄牙", "荷兰",
    "美国", "墨西哥", "加拿大", "日本", "韩国", "澳大利亚", "摩洛哥", "克罗地亚",
    "乌拉圭", "哥伦比亚", "瑞士", "塞内加尔", "比利时", "意大利", "厄瓜多尔", "加纳",
]

DRAW = "平局"


class DemoSource:
    """本地生成接近真实的 1X2 赔率，不发出任何外部请求。

    每轮约有 15% 的概率在某场比赛中注入一个 0.5%~2.5% 的套利空间，
    方便观察"发现机会 -> 自动下单 -> 结算"的完整流程。
    """

    def __init__(self):
        self.events = {}

    def get_events(self, _config):
        now = time.time()
        # 已开赛的比赛从行情中移除（引擎会在开赛时结算对应持仓）
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

        # 偶尔注入套利空间：抬高某一结果的最优赔率，使 sum(1/best) < 1
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
    """The Odds API 客户端。一次轮询 = 1 次请求 = 1 个配额积分。"""

    def __init__(self):
        self.sport_key = None
        self.excluded = set()   # 不支持 h2h 的赛事键（如冠军竞猜盘）

    def get_events(self, config):
        api_key = config.get("odds_api_key", "").strip()
        if not api_key:
            raise RuntimeError("未配置 The Odds API key（在设置面板填入）")

        sport = self._resolve_sport(config, api_key)
        # 注意：错误信息绝不能带 URL/参数，否则 apiKey 会泄露到日志
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
            raise RuntimeError(f"网络请求失败（{type(e).__name__}）") from None
        quota = {
            "remaining": resp.headers.get("x-requests-remaining"),
            "used": resp.headers.get("x-requests-used"),
        }
        if resp.status_code == 401:
            raise RuntimeError("API key 无效（401）")
        if resp.status_code == 429:
            raise RuntimeError("请求过于频繁（429），已自动延长轮询间隔")
        if resp.status_code == 422:
            self.excluded.add(sport)
            self.sport_key = None
            raise RuntimeError(f"赛事 {sport} 不支持 h2h 市场，已排除并将自动重选")
        if resp.status_code >= 400:
            raise RuntimeError(f"The Odds API 返回 HTTP {resp.status_code}")

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
            # 足球 1X2 需要三个结果都有报价，否则会误判套利
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
        # /sports 列表请求不消耗配额
        try:
            resp = requests.get(
                f"{ODDS_API_BASE}/sports",
                params={"apiKey": api_key, "all": "false"},
                timeout=25,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"网络请求失败（{type(e).__name__}）") from None
        if resp.status_code >= 400:
            raise RuntimeError(f"赛事列表请求失败（HTTP {resp.status_code}）")
        candidates = [
            s["key"] for s in resp.json()
            if s["key"].startswith("soccer") and "world_cup" in s["key"]
            and s["key"] not in self.excluded
            and not any(x in s["key"] for x in ("winner", "qualifier"))
        ]
        if not candidates:
            raise RuntimeError(
                "在 The Odds API 中未找到进行中的世界杯单场赛事，"
                "可在 config.json 中手动指定 sport_key"
            )
        # 单场比赛盘的 key 最短；冠军盘等衍生市场 key 带后缀
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
