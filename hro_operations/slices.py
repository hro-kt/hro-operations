"""flow のエッジがどこに偏っているかを見る。

★改善の王道は「主要な軸で ROI をスライスして得手不得手を見る」こと。
  3,555レースで +8% という薄いエッジに LightGBM の自由度を与えると簡単に過学習する。
  先に構造が見えれば、条件を1つ足すだけで改善できるし、モデルを作るにしても
  どの軸が効くかの当たりが付く。

★バケットごとの本数が少ないと何も言えない。本数と CI を必ず一緒に見ること。
  片方だけ見て「この帯は強い」と決めるのが、最も典型的な自滅の仕方。
"""

from __future__ import annotations

from .flow_signal import summarize_bets

_JYO = {"01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
        "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉"}


def _bucket_odds(v: float) -> str:
    for hi, label in ((2.0, "1 〜2.0倍"), (4.0, "2 2.0-4.0"), (7.0, "3 4.0-7.0"),
                      (12.0, "4 7.0-12"), (20.0, "5 12-20"), (40.0, "6 20-40")):
        if v < hi:
            return label
    return "7 40倍〜"


def _bucket_n(v) -> str:
    n = int(v or 0)
    if n <= 9:
        return "1 〜9頭"
    if n <= 12:
        return "2 10-12頭"
    if n <= 15:
        return "3 13-15頭"
    return "4 16頭〜"


def _bucket_zogen(d) -> str:
    """馬体重増減。★符号は zogen_fugo(+/-)に入っており、zogen_sa は絶対値。
    符号を無視すると増と減が同じ帯に落ちて、効果が打ち消し合って見えなくなる。
    """
    sa = (str(d.get("zogen_sa") or "")).strip()
    if not sa.isdigit():
        return "0 不明"
    v = int(sa)
    if v == 0:
        return "3 増減なし"
    minus = (str(d.get("zogen_fugo") or "")).strip() == "-"
    v = -v if minus else v
    if v <= -10:
        return "1 -10kg以下"
    if v < 0:
        return "2 -1〜-9kg"
    if v < 10:
        return "4 +1〜+9kg"
    return "5 +10kg以上"


def _bucket_ninki(d) -> str:
    n = (str(d.get("ninki") or "")).strip()
    if not n.isdigit() or int(n) <= 0:
        return "0 不明"
    v = int(n)
    if v <= 3:
        return "1 1-3番人気"
    if v <= 6:
        return "2 4-6番人気"
    if v <= 10:
        return "3 7-10番人気"
    return "4 11番人気〜"


def _bucket_waku(d) -> str:
    w = (str(d.get("waku") or "")).strip()
    if not w.isdigit() or int(w) <= 0:
        return "0 不明"
    v = int(w)
    return f"{v} {v}枠" if v <= 8 else "0 不明"


def _bucket_kyori(d) -> str:
    k = (str(d.get("kyori") or "")).strip()
    if not k.isdigit():
        return "0 不明"
    v = int(k)
    if v <= 1400:
        return "1 〜1400m"
    if v <= 1800:
        return "2 1600-1800m"
    if v <= 2200:
        return "3 2000-2200m"
    return "4 2400m〜"


def _bucket_track(d) -> str:
    """track_cd: 10番台=芝, 20番台=ダート, 50番台以上=障害(JV-Data)。"""
    t = (str(d.get("track_cd") or "")).strip()
    if not t.isdigit():
        return "0 不明"
    v = int(t)
    if 10 <= v <= 19:
        return "1 芝"
    if 20 <= v <= 29:
        return "2 ダート"
    return "3 障害・その他"


AXES = {
    "tan": ("決定時点の単勝オッズ", lambda d: _bucket_odds(float(d["tan"]))),
    "fuku": ("決定時点の複勝オッズ", lambda d: _bucket_odds(float(d["fuku"]))),
    "n": ("出走頭数", lambda d: _bucket_n(d.get("n_horses"))),
    "jyo": ("競馬場", lambda d: _JYO.get(d["rid"][8:10], d["rid"][8:10])),
    "month": ("月", lambda d: d["rid"][4:6]),
    "lead": ("実測リード", lambda d: f"T-{d.get('lead')}s"),
    "zogen": ("馬体重増減", _bucket_zogen),
    "ninki": ("決定時点の単勝人気", _bucket_ninki),
    "waku": ("枠番", _bucket_waku),
    "kyori": ("距離", _bucket_kyori),
    "track": ("馬場(芝/ダ)", _bucket_track),
    "score": ("スコアの大きさ(閾値からの超過)", None),   # 閾値相対なので別扱い
}


def slice_details(details: list[dict], axis: str, *, threshold: float | None = None,
                  min_bets: int = 30) -> list[dict]:
    """明細を軸でグループ化して回収率を出す。

    ★返還(元金が戻る)は summarize_bets 側で扱う。ここでは賭け金と払戻をそのまま渡す。
    """
    if axis == "score":
        if threshold is None:
            raise ValueError("score 軸には閾値が要ります")
        qs = sorted(d["score"] - threshold for d in details)

        def _b(d):
            x = d["score"] - threshold
            # 超過量を4分位で割る(絶対値はリード・信号源で尺度が変わるため)
            for i, q in enumerate((0.25, 0.5, 0.75), 1):
                if x < qs[int(len(qs) * q)]:
                    return f"{i} 下位{int(q * 100)}%まで"
            return "4 上位25%"
        key = _b
    else:
        if axis not in AXES:
            raise ValueError(f"不明な軸: {axis} ({'|'.join(AXES)})")
        key = AXES[axis][1]

    groups: dict[str, list[dict]] = {}
    for d in details:
        try:
            groups.setdefault(key(d), []).append(d)
        except (TypeError, ValueError, KeyError):
            groups.setdefault("(不明)", []).append(d)

    out = []
    for name, ds in sorted(groups.items()):
        bets = [(d["rid"], d.get("amount", 100), d["payout"], d["note"] == "返還")
                for d in ds]
        rep = summarize_bets(bets, races=len({d["rid"] for d in ds}),
                             races_scored=len({d["rid"] for d in ds}),
                             with_ci=len(bets) >= min_bets)
        rep["name"] = name
        out.append(rep)
    return out
