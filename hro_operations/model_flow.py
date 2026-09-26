"""flow を特徴量の1つにして、買うべき馬をモデルに選ばせる。

★目的変数は**純収益**にする(確率×オッズで並べてはいけない)
  確率を当てて `p × 複勝オッズ` で並べると、高配当馬では確率のわずかな誤差が
  オッズ倍されて増幅される。実測(2026-09-25): 的中12.7%・回収 0.8316 で、
  モデルは「穴馬で確率をわずかに上振れさせたもの」=雑音を拾っていた。
  対して flow は EV を見ず**レース内のシェア変化**という相対量で並べており、
  オッズ水準の誤差に影響されない(的中42.4%・1.1281)。
  → 目的変数を **純収益 = 払戻/賭け金 − 1** にして直接回帰する。これが買う基準そのもの。
  ★払戻は裾が非常に重い(複勝で20倍超が出る)。L2 だと数件の大穴に引きずられるので
    **Huber 損失 + 上側の刈り込み**を併用する。

★市場をオフセットに置く(分類のときだけ意味がある)
  複勝圏内の確率をそのまま学習させると、モデルは**市場を再現すること**に容量を使い切る。
  オッズを特徴量に入れている以上そうなるし、市場より上手く当てられないので、
  市場とのズレ=モデルの誤差を買いに行くことになる(2026-09-25 実測: 0.84 / 0.89 対
  flow 単体 1.13 で完敗)。
  正しくは市場の推定値 logit(q) を **init_score** に置き、モデルには
  **市場からの補正だけ**を学ばせる。q は複勝オッズから q≈(1-控除率)/複勝オッズ。

★目的関数の設計が肝
  複勝の的中確率をそのまま最大化すると**人気馬を選ぶだけ**になる。パリミュチュエルでは
  払戻が最終オッズで決まるので、狙うべきは
      期待回収率_i = P(複勝圏内)_i × 複勝オッズ_i
  これで並べて上位を買う。市場の見積もり(オッズ)を明示的に割り算に入れている形。

★評価は AUC ではなく**回収率**。確率がよく当たっても、市場が同じだけ当てていれば
  1円も儲からない。比較基準は flow 単体(同一テスト期間の同一本数)。

★リークを作らない
  特徴量は**決定時点(T-60s/T-75s)で live に取れるもの**だけ。着順・確定オッズ・
  払戻は当然だめだが、**nl_se 由来(馬体重など)も RACE蓄積でレース後配信**なので
  live では使えない。研究で使うなら別経路の取り込みが前提になる。

★学習は時系列で切る。ランダム分割は同一開催日の馬が train/test に跨り、
  同じレースの情報が漏れる。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .flow_signal import _SQL_BT, _logit, _num, summarize_bets

# 決定時点で live に取れる特徴量。★ここに増やすときは「T-75s に本当に手元にあるか」を
# 必ず確認する。nl_se 由来(zogen 等)は RACE蓄積なので **入れない**。
FEATURES = [
    "flow",             # 本命の信号
    "share_late", "share_early", "d_share",
    "log_tan", "log_fuku",
    "ninki", "ninki_norm",
    "n_horses",
    "waku_norm",        # ★対照。因果を考えにくい軸。重要度が高く出たら過学習の証拠
    "kyori", "is_dirt", "grade",
    "jyo",
]


@dataclass
class ModelConfig:
    lead_seconds: int = 60
    flow_minutes: int = 6
    source: str = "ts"
    quantile: float = 0.95          # 上位何%を買うか(既存ルールと揃える)
    num_leaves: int = 15            # ★小さく保つ。3,500レースに自由度を与えない
    n_estimators: int = 300
    learning_rate: float = 0.05
    min_child_samples: int = 200
    seed: int = 20260925
    # ★市場をオフセットに置くか。False にすると「市場の再現」を学んで必ず負ける。
    market_offset: bool = True
    place_takeout: float = 0.20      # 複勝の控除率(q の目安に使うだけ)
    # return(純収益の回帰) | hit(複勝圏内の分類) | rank(レース内の順位学習)
    # ★rank を足した理由: return/hit は**絶対量**の推定で、そこは市場がほぼ最適。
    #   flow が効くのは絶対量ではなく**レース内の相対的な動き**を見ているから。
    #   モデルにも同じ土俵で学ばせる。選択は**レース内で上位k頭**(スコアはレース間で
    #   比較できないので、大域の分位では切らない)。
    target: str = "return"
    winsor: float = 10.0             # ★純収益の上限。裾の数件に引きずられないように
    top_per_race: int = 1            # target=rank のとき、レース内で何頭買うか
    features: list[str] = field(default_factory=lambda: list(FEATURES))


def _f(v, default=0.0) -> float:
    s = (str(v) or "").strip()
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def load_rows(db, d_from: str, d_to: str, cfg: ModelConfig) -> list[dict]:
    """期間の全馬について特徴量と結果を作る(閾値で絞らない)。"""
    table = "ts_o1" if cfg.source == "ts" else "ts_sokuho_o1"
    rows = db.query(_SQL_BT.replace("{TABLE}", table),
                    {"d0": d_from, "d1": d_to,
                     "lead": cfg.lead_seconds, "flow": cfg.flow_minutes})
    by_race: dict[str, list[dict]] = {}
    for r in rows:
        by_race.setdefault(r["rid"], []).append(r)

    out: list[dict] = []
    for rid, rs in by_race.items():
        if rs[0]["ht1"] is not None and rs[0]["ht1"] == rs[0]["ht0"]:
            continue                                  # 測れていない
        if not rs[0].get("has_payout", True):
            continue                                  # 未確定は学習にも評価にも使わない
        ok = [x for x in rs if _num(x["t1"]) and _num(x["t0"]) and _num(x["f1"])]
        if len(ok) < 5:
            continue
        s1 = sum(1.0 / _num(x["t1"]) for x in ok)
        s0 = sum(1.0 / _num(x["t0"]) for x in ok)
        if s1 <= 0 or s0 <= 0:
            continue
        rank = {x["umaban"]: i for i, x in enumerate(
            sorted(ok, key=lambda r: (_num(r["t1"]), r["umaban"])), 1)}
        n = len(ok)
        for x in ok:
            t1, t0, f1 = _num(x["t1"]), _num(x["t0"]), _num(x["f1"])
            sl, se = (1.0 / t1) / s1, (1.0 / t0) / s0
            # ★返還(出走取消/除外)は勝ちでも負けでもない。学習から外す。
            if str(x.get("i_jyo_cd") or "").strip() in ("1", "2", "3"):
                continue
            pay = x["pay"]
            payout = int(pay) if str(pay or "").strip().isdigit() else 0
            track = (str(x.get("track_cd") or "")).strip()
            out.append({
                "rid": rid, "ymd": x["ymd"], "umaban": x["umaban"],
                "flow": _logit(sl) - _logit(se),
                "share_late": sl, "share_early": se, "d_share": sl - se,
                "log_tan": __import__("math").log(t1),
                "log_fuku": __import__("math").log(f1),
                "ninki": rank[x["umaban"]],
                "ninki_norm": rank[x["umaban"]] / n,
                "n_horses": n,
                "waku_norm": _f(x.get("wakuban")) / 8.0,
                "kyori": _f(x.get("kyori")),
                "is_dirt": 1.0 if track.isdigit() and 20 <= int(track) <= 29 else 0.0,
                "grade": _f(x.get("grade_cd")),
                "jyo": _f(rid[8:10]),
                "fuku_odds": f1,
                "hit": 1 if payout > 0 else 0,
                "payout": payout,
            })
    return out


def train_and_eval(train: list[dict], test: list[dict], cfg: ModelConfig, *,
                   amount: int = 100) -> dict:
    """学習→テスト期間で買って回収率を出す。比較基準は flow 単体(同一本数)。"""
    try:
        import lightgbm as lgb
    except ModuleNotFoundError as e:       # noqa: F841
        raise SystemExit(
            "lightgbm がありません。hro-operations で `poetry install -E model` してください"
        ) from None

    import math

    feats = cfg.features
    xtr = [[r[f] for f in feats] for r in train]
    ytr = [r["hit"] for r in train]
    xte = [[r[f] for f in feats] for r in test]

    def _market_logit(rows):
        """複勝オッズから市場の複勝確率を出して logit に。q≈(1-控除率)/複勝オッズ。"""
        out = []
        for r in rows:
            q = (1.0 - cfg.place_takeout) / max(r["fuku_odds"], 1.01)
            q = min(max(q, 1e-4), 1 - 1e-4)
            out.append(math.log(q / (1 - q)))
        return out

    if cfg.target == "rank":
        import collections

        def _rel(payout: int) -> int:
            """★配当の大きさを段階で表す。0/1 の二値にすると「当たりやすい人気馬」を
            学ぶだけになり、flow の利益が生まれている高配当の的中が評価されない。"""
            if payout <= 0:
                return 0
            if payout < 200:
                return 1
            return 2 if payout < 500 else 3

        def _grouped(rows):
            order = collections.OrderedDict()
            for r in rows:
                order.setdefault(r["rid"], []).append(r)
            flat = [r for rs in order.values() for r in rs]
            return flat, [len(rs) for rs in order.values()], order

        ftr, gtr, _ = _grouped(train)
        fte, gte, order_te = _grouped(test)
        rk = lgb.LGBMRanker(
            objective="lambdarank", num_leaves=cfg.num_leaves,
            n_estimators=cfg.n_estimators, learning_rate=cfg.learning_rate,
            min_child_samples=cfg.min_child_samples, label_gain=[0, 1, 3, 7],
            random_state=cfg.seed, verbose=-1)
        rk.fit([[r[f] for f in feats] for r in ftr], [_rel(r["payout"]) for r in ftr],
               group=gtr)
        for r, v in zip(fte, rk.predict([[r[f] for f in feats] for r in fte])):
            r["rank_score"] = float(v)

        def _top_per_race(key: str, k: int, label: str) -> dict:
            bets = []
            for rs in order_te.values():
                for r in sorted(rs, key=lambda x: -x[key])[:k]:
                    bets.append((r["rid"], amount,
                                 int(round(r["payout"] * amount / 100)), False))
            rep = summarize_bets(bets, races=len(order_te), races_scored=len(order_te))
            rep["label"] = label
            return rep

        k = max(1, cfg.top_per_race)
        imp = sorted(zip(feats, rk.feature_importances_), key=lambda kv: -kv[1])
        return {"n_train": len(train), "n_test": len(test),
                "n_take": k * len(order_te),
                "model": _top_per_race("rank_score", k, f"モデル(レース内上位{k}頭)"),
                "edge": _top_per_race("rank_score", k, f"モデル(レース内上位{k}頭)"),
                "proba": _top_per_race("rank_score", k, f"モデル(レース内上位{k}頭)"),
                "flow": _top_per_race("flow", k, f"flow 単体(レース内上位{k}頭)"),
                "importance": imp}

    if cfg.target == "return":
        # ★買う基準そのものを直接学ぶ。純収益 = 払戻/賭け金 − 1(外れ = -1)。
        #   裾が重いので Huber + 刈り込み。
        ytr_r = [min(r["payout"] / 100.0 - 1.0, cfg.winsor) for r in train]
        reg = lgb.LGBMRegressor(
            objective="huber", num_leaves=cfg.num_leaves,
            n_estimators=cfg.n_estimators, learning_rate=cfg.learning_rate,
            min_child_samples=cfg.min_child_samples,
            random_state=cfg.seed, verbose=-1)
        reg.fit(xtr, ytr_r)
        pred = reg.predict(xte)
        for r, v in zip(test, pred):
            r["p"] = 0.0
            r["ev"] = float(v)          # 予測純収益そのもので並べる
            r["edge"] = float(v)
        imp = sorted(zip(feats, reg.feature_importances_), key=lambda kv: -kv[1])
        n_take = max(1, int(len(test) * (1 - cfg.quantile)))

        def _bet_r(key: str, label: str) -> dict:
            picked = sorted(test, key=lambda r: -r[key])[:n_take]
            bets = [(r["rid"], amount, int(round(r["payout"] * amount / 100)), False)
                    for r in picked]
            rep = summarize_bets(bets, races=len({r["rid"] for r in test}),
                                 races_scored=len({r["rid"] for r in test}))
            rep["label"] = label
            return rep

        return {"n_train": len(train), "n_test": len(test), "n_take": n_take,
                "model": _bet_r("ev", "モデル(純収益の予測で上位)"),
                "edge": _bet_r("ev", "同上"),
                "proba": _bet_r("ev", "同上"),
                "flow": _bet_r("flow", "flow 単体(比較基準)"),
                "importance": imp}

    model = lgb.LGBMClassifier(
        num_leaves=cfg.num_leaves, n_estimators=cfg.n_estimators,
        learning_rate=cfg.learning_rate, min_child_samples=cfg.min_child_samples,
        random_state=cfg.seed, verbose=-1)
    if cfg.market_offset:
        init_tr = _market_logit(train)
        model.fit(xtr, ytr, init_score=init_tr)
        raw = model.predict(xte, raw_score=True)
        init_te = _market_logit(test)
        proba = [1.0 / (1.0 + math.exp(-max(min(a + b, 30.0), -30.0)))
                 for a, b in zip(raw, init_te)]
    else:
        model.fit(xtr, ytr)
        proba = model.predict_proba(xte)[:, 1]

    for r, p in zip(test, proba):
        # ★期待回収率で並べる。確率だけで並べると人気馬を選ぶだけになる。
        r["p"] = float(p)
        r["ev"] = float(p) * r["fuku_odds"]

    def _bet(key: str, n_take: int, label: str) -> dict:
        picked = sorted(test, key=lambda r: -r[key])[:n_take]
        bets = [(r["rid"], amount, int(round(r["payout"] * amount / 100)), False)
                for r in picked]
        rep = summarize_bets(bets, races=len({r["rid"] for r in test}),
                             races_scored=len({r["rid"] for r in test}))
        rep["label"] = label
        return rep

    for r in test:
        # 市場だけで並べた場合。オフセットが効いていればモデルはこれを基準に補正する
        r["mkt_ev"] = (1.0 - cfg.place_takeout)
        r["edge"] = r["p"] * r["fuku_odds"] - (1.0 - cfg.place_takeout)
    n_take = max(1, int(len(test) * (1 - cfg.quantile)))
    imp = sorted(zip(feats, model.feature_importances_), key=lambda kv: -kv[1])
    return {
        "n_train": len(train), "n_test": len(test), "n_take": n_take,
        "model": _bet("ev", n_take, "モデル(期待回収率で上位)"),
        "proba": _bet("p", n_take, "モデル(確率だけで上位)"),
        "flow": _bet("flow", n_take, "flow 単体(比較基準)"),
        "edge": _bet("edge", n_take, "モデル(市場からの上乗せ分)"),
        "importance": imp,
    }
