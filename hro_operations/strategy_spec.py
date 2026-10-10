"""戦略書(仕様)を生成する。**運用実績は入れない**。

★これまでの生成物(strategy_doc)は運用報告書だった。購入額・回収率・網羅性は
  「どう運用したか」であって「何をなぜどう買うのか」ではない。別物として分ける。
★構成は ① 戦略ID・名称・版 / ③ 目的・基本仮説 / ④ 対象範囲 / ⑤ 購入判定ロジック
  / ⑥ 資金配分ロジック / ⑦ モデル仕様 / ⑧ 収益性の検証 / ⑨ 実行・例外処理。
  ② 適用期間・⑩ 変更管理・⑪ 運用実績は**仕様書の外**(strategy_versions の列と
  運用報告書)で記録する。
★機械が出せる事実(設定・規則・上限)は設定から、人が書く部分(仮説・検証の解釈・
  障害時の方針)は strategy_notes から取る。混ぜて1本にするが、**どちらに由来するかを
  文書上で区別する**(事実と見解を混ぜた資料は信用されない)。
"""

from __future__ import annotations

from .strategy import NOTE_SECTIONS, RULE_TEXT, SECTION_TITLE, STRATEGY_ID, STRATEGY_NAME

_SOURCE_JP = {"netkeiba": "netkeiba(実時刻・秒単位)",
              "sokuho": "JRA-VAN 速報オッズ 0B30(発表時刻は分単位)",
              "ts": "JRA-VAN 公式時系列オッズ 0B41(発走近傍は 0/60/360 秒のみ)"}
_BET_JP = {"tan": "単勝", "fuku": "複勝", "umatan": "馬単(軸1着固定)"}

_MISSING = ("> **未記入。** この節は人が書く部分です。"
            "`hro-ops strategy-note --version {vid} --section {sec}` で登録してください。")


def _notes(rows: list[dict]) -> dict[str, dict]:
    """strategy_notes の行を section -> 行 に畳む(版指定が全版共通より優先)。"""
    out: dict[str, dict] = {}
    for r in sorted(rows, key=lambda x: (x.get("version_id") is not None)):
        out[r["section"]] = r
    return out


def _yen(n) -> str:
    return f"{int(n or 0):,} 円"


def render(version: dict, notes: list[dict], *, now: str) -> str:
    """1つの版の仕様書を Markdown で。

    version は strategy_versions の行(params / params_hash / version / mode …)。
    """
    p = version.get("params") or {}
    f = p.get("flow") or {}
    bets = p.get("bets") or []
    money = p.get("money") or {}
    ex = p.get("execution") or {}
    n = _notes(notes)
    vid = version.get("id")

    out: list[str] = []
    a = out.append

    def section(no: str, title: str, key: str | None = None) -> None:
        a(f"## {no}. {title}")
        a("")
        if key:
            row = n.get(key)
            if row:
                a(row["body"].rstrip())
                a("")
                a(f"<small>記載者: {row.get('authored_by') or '—'} / "
                  f"{str(row.get('authored_at') or '')[:19]} / "
                  f"rev{row.get('revision')}</small>")
            else:
                a(_MISSING.format(vid=vid, sec=key))
            a("")

    # ---- ① 戦略ID・名称・バージョン ----
    a(f"# 馬券購入戦略書 {STRATEGY_NAME}")
    a("")
    a("| | |")
    a("|---|---|")
    a(f"| 戦略ID | `{STRATEGY_ID}` |")
    a(f"| 名称 | {version.get('name') or STRATEGY_NAME} |")
    a(f"| バージョン | v{version.get('version')}(内部ID {vid}) |")
    a(f"| 設定ハッシュ | `{version.get('params_hash')}` |")
    a(f"| 実行モード | {version.get('mode')} |")
    a(f"| 文書生成日時 | {now} |")
    a("")
    a("> 本書は**実際に稼働した設定から生成**しています。設定ハッシュは稼働中の"
      "パラメータを正規化して SHA-256 を取った値で、この値が一致する限り本書の記載と"
      "実際の購入条件は一致します。")
    a("")
    a("> 適用期間(②)・変更管理(⑩)・運用実績(⑪)は本書の対象外です。"
      "いずれも別途記録しています(`strategy_versions` / 運用報告書)。")
    a("")

    # ---- ③ 目的・基本仮説 ----
    section("③", "目的・基本仮説", "purpose")

    # ---- ④ 対象範囲 ----
    a("## ④. 対象範囲")
    a("")
    a("| 項目 | 内容 |")
    a("|---|---|")
    a("| 対象競走 | 日本中央競馬会(JRA)が施行する平地・障害の全競走 |")
    a("| 対象外 | 地方競馬・海外競馬は一切対象としない |")
    a(f"| 券種 | {'、'.join(_BET_JP.get(b['bet_type'], b['bet_type']) for b in bets) or '—'} |")
    a("| 対象レースの選び方 | **開催日の全競走を対象に同一の規則を当てる。"
      "個々の競走を選んで購入することはしない** |")
    cond = []
    if f.get("min_ninki"):
        cond.append(f"決定時点の単勝人気が {f['min_ninki']} 番人気以降")
    if f.get("max_ninki"):
        cond.append(f"決定時点の単勝人気が {f['max_ninki']} 番人気まで")
    if f.get("min_horses"):
        cond.append(f"出走頭数 {f['min_horses']} 頭以上")
    if f.get("max_odds"):
        cond.append(f"複勝オッズ {f['max_odds']} 倍以下")
    if f.get("min_tan_odds"):
        cond.append(f"単勝オッズ {f['min_tan_odds']} 倍以上")
    if f.get("max_tan_odds"):
        cond.append(f"単勝オッズ {f['max_tan_odds']} 倍以下")
    a(f"| 除外条件(馬単位) | {'、'.join(cond) if cond else '設定なし(全馬が対象)'} |")
    a("| 除外条件(競走単位) | 判断に必要なオッズのスナップショットが"
      "規定の時間窓で取得できなかった競走、決定時点に対応する閾値が"
      "定められていない競走は購入を見送る |")
    a("")
    if n.get("scope"):
        section("", "", "scope")

    # ---- ⑤ 購入判定ロジック ----
    a("## ⑤. 購入判定ロジック")
    a("")
    a("### 指標の定義")
    a("")
    a("```")
    a(RULE_TEXT)
    a("```")
    a("")
    a("### 判定に用いる値")
    a("")
    a("| 項目 | 設定値 |")
    a("|---|---|")
    a(f"| オッズの取得元 | {_SOURCE_JP.get(f.get('source'), f.get('source'))} |")
    a(f"| 決定時点 | 発走 {f.get('lead_seconds')} 秒前 |")
    a(f"| 変化の起点 | 発走 {f.get('flow_minutes')} 分前 |")
    thr = f.get("thresholds") or {}
    if thr:
        a(f"| 判定閾値 | 決定時点ごとに定める: "
          f"{'、'.join(f'発走 {k} 秒前 → {v}' for k, v in thr.items())} |")
        a("| 閾値が未定義の決定時点 | **購入しない**(その競走は見送る) |")
    else:
        a(f"| 判定閾値 | {f.get('threshold')} |")
    a(f"| 時間窓の許容ずれ | ±{f.get('window_tolerance_sec', '—')} 秒"
      "(超えた競走は見送る) |")
    if f.get("max_per_race"):
        a(f"| 1競走あたりの点数上限 | {f['max_per_race']} 点"
          "(指標の高い順に残す) |")
    if any(b["bet_type"] == "umatan" for b in bets):
        a(f"| 馬単の相手選定 | 決定時点の単勝人気 上位 {f.get('partners')} 頭"
          "(指標の順位ではない) |")
    a("")
    a("### 購入条件")
    a("")
    a("1. 開催日の全競走について、決定時点のオッズと起点のオッズを取得する。")
    a("2. 除外条件(④)を満たす馬に限定する。")
    a("3. 限定した馬のうち、指標が閾値以上の馬を購入する。")
    a("4. 閾値以上の馬がいなければ、その競走では購入しない。")
    a("")
    a("**期待回収率**: 購入判定は個々の競走の的中見込みではなく、"
      "上記条件を満たす母集団全体の期待回収率が1を上回ることに基づく。"
      "根拠は ⑧ に記載する。")
    a("")

    # ---- ⑥ 資金配分ロジック ----
    a("## ⑥. 資金配分ロジック")
    a("")
    a("| 項目 | 設定値 |")
    a("|---|---|")
    for b in bets:
        a(f"| {_BET_JP.get(b['bet_type'], b['bet_type'])} 1点あたり | {_yen(b['amount'])} |")
    a(f"| 既定額(券種指定が無い場合) | {_yen(money.get('flat_amount'))} |")
    a(f"| 1件あたりの上限 | {_yen(money.get('max_amount_per_order')) if money.get('max_amount_per_order') else '設定なし'} |")
    a(f"| 1日あたりの上限 | {_yen(money.get('max_amount_per_day')) if money.get('max_amount_per_day') else '設定なし'} |")
    a("")
    a("**投票額の決定**: 定額。指標の大小や推定確率に応じて金額を変えることはしない"
      "(可変にすると、母集団の期待回収率と実際の収支が対応しなくなるため)。")
    a("")
    a("**損失制御**: 1件および1日の上限を超える投票は執行時に棄却する。"
      "上限は購入処理側で機械的に判定し、超過分は投票しない。")
    a("")

    # ---- ⑦ モデル仕様 ----
    a("## ⑦. モデル仕様")
    a("")
    a("| 項目 | 内容 |")
    a("|---|---|")
    a("| 推定方法 | 統計モデルによる確率推定は行わない(model-free)。"
      "単勝票数シェアの推定値の対数オッズ変化量を指標とし、閾値で選別する |")
    a("| 特徴量 | 決定時点と起点における単勝オッズから算出した票数シェアの推定値、"
      "ただ1つ |")
    a("| 学習 | 学習は行わない。閾値のみ過去データの分位から決定する |")
    # ★Python の辞書表現をそのまま文書へ出さない(提出資料に {'90': 0.1368} と出る)
    thr_txt = ("、".join(f"発走 {k} 秒前 → {v}" for k, v in thr.items()) if thr
               else str(f.get("threshold")))
    a(f"| 閾値の決定方法 | 対象母集団(④の除外条件を適用した集合)における"
      f"指標の上側分位。設定値は {thr_txt} |")
    a(f"| モデルID | `{STRATEGY_ID}` v{version.get('version')} "
      f"(`{str(version.get('params_hash') or '')[:16]}…`) |")
    a("")
    if n.get("model"):
        section("", "", "model")

    # ---- ⑧ 収益性の検証 ----
    section("⑧", "収益性の検証", "evidence")

    # ---- ⑨ 実行・例外処理 ----
    a("## ⑨. 実行・例外処理")
    a("")
    a("| 項目 | 内容 |")
    a("|---|---|")
    a("| 購入方法 | JRA 即PAT をソフトウェアが自動操作して投票する。"
      "競走ごとの人の判断は介在しない |")
    a(f"| 投票開始 | 発走 {ex.get('act_lead_seconds')} 秒前 |")
    a(f"| 発売締切 | 発走 {ex.get('deadline_lead_seconds')} 秒前 |")
    a("| 締切超過時 | 締切を過ぎた投票は送信せず棄却する(後追いの投票はしない) |")
    a("| 同一競走の複数点 | 1回の送信にまとめる |")
    a("| オッズ変動時 | 決定時点のオッズで判断し、**その後の変動で判断を変えない**。"
      "投票は定額なので、変動による金額の再計算も行わない |")
    a("| 投票の確認 | 即PAT の受付番号を記録し、後日**即PAT の投票履歴と突合**して"
      "成立を確認する。履歴に無い投票は不成立として扱う |")
    a("| 障害時 | 収集または投票の処理が停止した競走は購入しない。"
      "復旧後に遡って購入することはしない |")
    a("| 記録 | 競走ごとに、適用した規則・判定に用いた値・判定結果を保存する |")
    a("")
    if n.get("execution"):
        section("", "", "execution")

    return "\n".join(out)


def missing_sections(notes: list[dict]) -> list[str]:
    """人が書く節のうち、まだ書かれていないもの。

    ★未記入のまま提出すると、仮説も検証も無いまま買っていたように見える。
      どこが空かを機械が言えるようにしておく。
    """
    have = set(_notes(notes))
    return [s for s in NOTE_SECTIONS if s not in have and s in ("purpose", "evidence")]


def save_draft(conn, version_id: int, markdown: str, *, regenerate: bool = False) -> dict:
    """下書きを作る/更新する。版ごとに下書きは1本だけ。

    ★自動生成はそのまま確定版にしない。**下書き → 編集 → 確定**。機械が出せるのは
      設定と規則までで、目的・仮説・検証の解釈は人が詰める。
    ★既に下書きが在るとき、生成(regenerate=True)は**上書きしない**。人が編集した
      内容を機械が黙って消すのが最悪なので、既存の下書きをそのまま返す。
      作り直したいなら先に破棄する。
    """
    import hashlib

    h = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    row = conn.execute(
        "SELECT id, revision, content_hash FROM strategy_specs"
        " WHERE version_id=%s AND status='draft'", (version_id,)).fetchone()
    if row:
        if regenerate:
            return {"status": "draft_exists", "id": int(row[0]),
                    "revision": int(row[1]),
                    "note": "下書きが既にあります(編集内容を上書きしません)"}
        if row[2] == h:
            return {"status": "unchanged", "id": int(row[0]), "revision": int(row[1])}
        conn.execute(
            "UPDATE strategy_specs SET markdown=%s, content_hash=%s, edited_at=now()"
            " WHERE id=%s", (markdown, h, int(row[0])))
        return {"status": "edited", "id": int(row[0]), "revision": int(row[1]),
                "hash": h}
    last = conn.execute(
        "SELECT max(revision) FROM strategy_specs WHERE version_id=%s",
        (version_id,)).fetchone()
    rev = int((last and last[0]) or 0) + 1
    row = conn.execute(
        "INSERT INTO strategy_specs(strategy_id, version_id, revision, status,"
        " markdown, content_hash) VALUES(%s,%s,%s,'draft',%s,%s) RETURNING id",
        (STRATEGY_ID, version_id, rev, markdown, h)).fetchone()
    return {"status": "created", "id": int(row[0]), "revision": rev, "hash": h}


def discard_draft(conn, version_id: int) -> int:
    """下書きを破棄する。確定版はトリガが守るので消えない。"""
    rows = conn.execute(
        "DELETE FROM strategy_specs WHERE version_id=%s AND status='draft'"
        " RETURNING id", (version_id,)).fetchall()
    return len(rows)


def publish(conn, version_id: int, *, by: str | None = None) -> dict:
    """下書きを確定する。**確定後は変更できない**(DB のトリガが拒否する)。"""
    row = conn.execute(
        "SELECT id, revision, content_hash FROM strategy_specs"
        " WHERE version_id=%s AND status='draft'", (version_id,)).fetchone()
    if not row:
        return {"status": "no_draft",
                "note": "確定できる下書きがありません(先に生成してください)"}
    conn.execute(
        "UPDATE strategy_specs SET status='published', published_at=now(),"
        " published_by=%s WHERE id=%s", (by, int(row[0])))
    return {"status": "published", "id": int(row[0]), "revision": int(row[1]),
            "hash": row[2]}


def current(conn, version_id: int) -> dict | None:
    """表示するべき1本。確定版があればそれ、無ければ下書き。"""
    row = conn.execute(
        "SELECT id, revision, status, markdown, content_hash, generated_at,"
        "       edited_at, published_at, published_by FROM strategy_specs"
        " WHERE version_id=%s ORDER BY (status='published') DESC, revision DESC"
        " LIMIT 1", (version_id,)).fetchone()
    if not row:
        return None
    keys = ("id", "revision", "status", "markdown", "content_hash", "generated_at",
            "edited_at", "published_at", "published_by")
    return dict(zip(keys, row))


def save_note(conn, *, section: str, body: str, version_id: int | None,
              authored_by: str | None) -> dict:
    """人が書く節を保存する。追記のみ。

    ★古い本文は残す。説明を書き換えてなかったことにできる記録は信用されない。
    """
    import hashlib

    if section not in NOTE_SECTIONS:
        raise ValueError(f"section は {NOTE_SECTIONS} のいずれか: {section!r}")
    h = hashlib.sha256(body.encode("utf-8")).hexdigest()
    row = conn.execute(
        "SELECT revision, content_hash FROM strategy_notes WHERE strategy_id=%s"
        " AND coalesce(version_id,0)=coalesce(%s,0) AND section=%s"
        " ORDER BY revision DESC LIMIT 1",
        (STRATEGY_ID, version_id, section)).fetchone()
    prev = int(row[0]) if row else 0
    if row and row[1] == h:
        return {"status": "unchanged", "section": section, "revision": prev}
    conn.execute(
        "INSERT INTO strategy_notes(strategy_id, version_id, section, revision, body,"
        " content_hash, authored_by) VALUES(%s,%s,%s,%s,%s,%s,%s)",
        (STRATEGY_ID, version_id, section, prev + 1, body, h, authored_by))
    return {"status": "saved", "section": section, "revision": prev + 1}
