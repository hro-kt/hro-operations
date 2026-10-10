"""稼働した設定そのものを「戦略の版」として確定させる。

★税務では「この馬券はこの戦略に基づく」と言えることが要る。ところが依拠先が
  人の書いた宣言だと、後から書いたのではと疑われる余地が残る。そこで**実際に
  動いた設定から**版を起こす: run-day が起動時に DayConfig を正規化してハッシュし、
  同じハッシュの版が在ればそれを使い、無ければ新しい版を作る。
  結果として、戦略ドキュメントと実際の挙動が構造的に食い違わない。
★model_version(文字列 "flow@netkeiba:T-90s:0.1368")では足りない。閾値と信号源しか
  入らず、人気帯・頭数下限・券種・金額が落ちる。宣言としては不完全だった。
"""

from __future__ import annotations

import hashlib
import json

STRATEGY_ID = "flow_tan"

RULE_TEXT = (
    "flow_tan(馬 i) = logit(share_i @ 発走-lead秒) − logit(share_i @ 発走-flow_min分)\n"
    "  share_i = (1/単勝オッズ_i) / Σ_j (1/単勝オッズ_j)  … 単勝票数シェアの推定値\n"
    "判定: 同一レース内で、選別条件(券種・人気帯・単勝オッズ帯・出走頭数・複勝オッズ上限)を\n"
    "      満たす馬のうち flow_tan が閾値以上の馬を買う。閾値は決定時点(lead)ごとに\n"
    "      事前に定めた表から引き、表に無いリードのレースは見送る。\n"
    "      馬単は軸を1着固定、相手は決定時点の単勝人気の上位N頭。\n"
    "★レースごとの人の裁量は入らない。設定を変えない限り、同じ入力からは同じ買い目が出る。"
)


# 買い目に影響しない FlowConfig のフィールド。版の同一性からは外す。
# ★入れると、無関係な調整のたびに版が増え「戦略を頻繁に変えていた」という
#   誤った像になる。逆に、買い目に影響するものを落としてはいけない(別の戦略が
#   同じ版に見える)。だから**除外リスト方式**にして、新しく足された設定は
#   自動的に版へ入るようにする。
_NOT_STRATEGY = frozenset({"window_tolerance_sec"})


def canonical_params(cfg) -> dict:
    """DayConfig から、戦略の同一性を決めるパラメータを正規化して取り出す。

    ★信号と選別は **race_day.flow_config が実際に組んだ FlowConfig** から取る。
      ここで項目を書き並べると、FlowConfig に足した設定が版へ入らず、
      別の戦略が同じ版に見える。判断に使う物と記録する物は同じ所から出す。
    """
    import dataclasses

    from .race_day import flow_config

    fc = flow_config(cfg)
    signal = {}
    for f in dataclasses.fields(fc):
        if f.name in _NOT_STRATEGY:
            continue
        v = getattr(fc, f.name)
        if f.name == "thresholds":
            v = (dict(sorted(((str(int(k)), float(x)) for k, x in (v or {}).items()),
                             key=lambda kv: int(kv[0]))) or None)
        signal[f.name] = v

    return {
        "strategy": STRATEGY_ID,
        # ★券種ごとの金額("tan:1000,umatan:100")。FlowConfig は1券種ぶんしか
        #   持たないので、買う組み合わせ全体はこちらで押さえる。
        "bets": [{"bet_type": bt, "amount": amt} for bt, amt in _bet_plan(cfg)],
        "flow": signal,
        "money": {
            "flat_amount": int(cfg.flat_amount),
            # 買い目そのものではないが、**買える上限**を決めるので含める
            "max_amount_per_order": int(getattr(cfg, "max_amount_per_order", 0) or 0),
            "max_amount_per_day": int(getattr(cfg, "max_amount_per_day", 0) or 0),
        },
        "execution": {
            # 締切に対してどこで判断し、どこで投票するか。買える/買えないを分ける
            "deadline_lead_seconds": int(cfg.deadline_lead_seconds),
            "act_lead_seconds": int(cfg.lead_seconds),
        },
        "mode": str(cfg.mode),
    }


def _bet_plan(cfg):
    from .race_day import bet_plan
    return bet_plan(cfg)


def params_hash(params: dict) -> str:
    """正規化 JSON の SHA-256。キー順を固定し、空白も固定する。

    ★ここがぶれると同じ設定が別の版に見える。json.dumps の既定(キー順そのまま)に
      頼らず sort_keys を明示する。
    """
    blob = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def describe(params: dict) -> str:
    """版の中身を人が読める1行に(ログと文書の見出し用)。"""
    f = params["flow"]
    thr = (",".join(f"T-{k}s:{v:+.4f}" for k, v in (f.get("thresholds") or {}).items())
           or f"{f.get('threshold', 0.0):+.4f}")
    bets = ",".join(f"{b['bet_type']}:{b['amount']}" for b in params["bets"])
    band = " ".join(f"{k}={f[k]}" for k in
                    ("min_ninki", "max_ninki", "min_horses", "max_odds",
                     "min_tan_odds", "max_tan_odds", "partners", "max_per_race")
                    if f.get(k))
    return (f"{params['strategy']}@{f['source']}:T-{f['lead_seconds']}s"
            f"/{f['flow_minutes']}min thr={thr} {bets} {band} mode={params['mode']}")


def resolve_version(conn, params: dict, *, date: str, evidence: dict | None = None) -> int:
    """同じ設定の版が在ればその id、無ければ新しい版を起こして id を返す。

    ★effective_from は**初めてその版で発注した日**。既存の版を使い回すときは
      触らない(遡って書き換えない)。
    ★前の版の effective_to は、別の版に移った日で閉じる。期間が重ならないので
      「いつからいつまで、どの戦略だったか」が一意に読める。
    """
    h = params_hash(params)
    row = conn.execute(
        "SELECT id FROM strategy_versions WHERE strategy_id=%s AND params_hash=%s",
        (STRATEGY_ID, h)).fetchone()
    if row:
        return int(row[0])

    blob = json.dumps(params, ensure_ascii=False, sort_keys=True)
    ev = json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True)
    row = conn.execute(
        "INSERT INTO strategy_versions"
        " (strategy_id, version, params, params_hash, rule_text, evidence, mode,"
        "  effective_from)"
        " SELECT %s, coalesce(max(version),0)+1, %s::jsonb, %s, %s, %s::jsonb, %s, %s"
        " FROM strategy_versions WHERE strategy_id=%s"
        " RETURNING id",
        (STRATEGY_ID, blob, h, RULE_TEXT, ev, params["mode"], date, STRATEGY_ID)
    ).fetchone()
    new_id = int(row[0])
    # 直前まで現行だった版を閉じる(同じ mode の中で)
    conn.execute(
        "UPDATE strategy_versions SET effective_to=%s"
        " WHERE strategy_id=%s AND mode=%s AND id<>%s AND effective_to IS NULL",
        (date, STRATEGY_ID, params["mode"], new_id))
    return new_id
