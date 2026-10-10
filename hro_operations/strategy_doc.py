"""戦略ドキュメントを**コードと DB から**生成する。手で書かない。

★手で書いた戦略書は「後から書いたのではないか」という疑いを払えない。ここは
  strategy_versions(稼働した設定そのもの)・race_coverage(全レースに規則を当てた
  記録)・ipat_receipts(IPAT 側の金額)・tax_ledger_seals(封印の鎖)から組み立てる。
  書いてある数字は全部、その日に記録されたものから出ている。
★読み手は税理士と税務当局。彼らが知りたい順に並べる:
  何を・どういう規則で・どれだけ継続的に網羅的に買ったのか、収支はどうか、
  その記録は信用できるのか。
★一時所得か雑所得かの結論は書かない。事実と両方の計算を並べるところまで。
"""

from __future__ import annotations

from .strategy import RULE_TEXT


def _pct(x) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def _yen(n) -> str:
    return f"{int(n or 0):,} 円"


def render(rep: dict, *, versions: list[dict], now: str) -> str:
    """tax_report.build の結果 + 戦略の版から Markdown を組む。"""
    t, c, st = rep["totals"], rep["coverage"], rep["settled"]
    y = rep["year"]
    out: list[str] = []
    a = out.append

    a(f"# 馬券購入に関する戦略書({y}年)")
    a("")
    a(f"作成日時: {now}  /  対象: {y}年1月1日〜12月31日の実購入(live)のみ")
    a("")
    a("> この文書は手で書いたものではなく、**実際に稼働した設定と、その日に記録された"
      "データから自動生成**しています。記載の数値はすべて下記テーブルに由来します。")
    a("")

    a("## 1. 要旨")
    a("")
    a("| 項目 | 値 |")
    a("|---|---|")
    a(f"| 購入総額 | {_yen(t['bought'])} |")
    a(f"| 払戻総額 | {_yen(t['payout'])} |")
    a(f"| 収支 | {t['pnl']:+,} 円 |")
    a(f"| 回収率 | {('%.4f' % t['roi']) if t['roi'] else '—'} |")
    a(f"| 購入日数 | {t['days']} 日 |")
    a(f"| 購入のあった月 | {rep['months_active']} か月 |")
    a(f"| 購入点数(受付) | {t['receipts']} 件 |")
    a(f"| 対象レース | {c['races']} |")
    a(f"| 規則を当てたレース | {c['evaluated']}({_pct(c['evaluated_ratio'])}) |")
    a(f"| 購入したレース | {c['bought_races']}({_pct(c['bought_ratio'])}) |")
    a("")

    a("## 2. 購入の規則")
    a("")
    a("```")
    a(RULE_TEXT)
    a("```")
    a("")
    a("**レースごとの人の判断は介在しません。** 設定を変えない限り、同じ入力からは"
      "同じ買い目が出ます。購入は専用のソフトウェアが自動で行い、"
      "発走時刻の約70秒前に判断と投票を機械的に実行します。")
    a("")

    a("## 3. 条件設定の履歴")
    a("")
    a("設定は「版」として確定させ、**購入指示1件ごとにどの版に基づくかを記録**して"
      "います。版は稼働した設定そのものを正規化して SHA-256 を取ったもので、"
      "文書のために別途書き起こしたものではありません。")
    a("")
    a("| 版 | 有効期間 | 設定の要約 | ハッシュ |")
    a("|---|---|---|---|")
    for v in versions:
        p = v.get("params") or {}
        f = p.get("flow") or {}
        bets = " + ".join(f"{b['bet_type']} {b['amount']:,}円"
                          for b in (p.get("bets") or []))
        thr = " ".join(f"T-{k}s:{x}" for k, x in (f.get("thresholds") or {}).items())
        band = " ".join(f"{k}={f[k]}" for k in
                        ("min_ninki", "max_ninki", "min_horses", "max_odds", "partners")
                        if f.get(k))
        a(f"| v{v['version']} | {v['effective_from']}〜{v.get('effective_to') or '現行'} "
          f"| 信号源={f.get('source')} 決定=発走-{f.get('lead_seconds')}秒 "
          f"起点=発走-{f.get('flow_minutes')}分 閾値 {thr} / {bets} / {band} "
          f"| `{str(v.get('params_hash') or '')[:16]}…` |")
    a("")

    a("## 4. 継続性")
    a("")
    a("| 年月 | 購入日数 | 受付 | 購入額 | 払戻額 | 対象R | 規則を当てたR | 購入R |")
    a("|---|---|---|---|---|---|---|---|")
    for m in rep["monthly"]:
        # ★m['bought'] は購入**額**、m['bought_races'] はレース**数**。
        #   同じ行に両方あるので取り違えると黙って別の数字が出る。
        a(f"| {m['ym']} | {m['days']} | {m['receipts']} | {m['bought']:,} "
          f"| {m['payout']:,} | {m.get('races', 0)} | {m.get('evaluated', 0)} "
          f"| {m.get('bought_races', 0)} |")
    a("")

    a("## 5. 網羅性")
    a("")
    a("購入は特定のレースを選んで行うものではなく、**対象期間の全 JRA レースに"
      "同一の規則を当て、その結果として購入の有無が決まります**。"
      "個々の馬券の的中可能性に着目した選択は行っていません。")
    a("")
    a("下表は対象レースの内訳です。**規則を当てられなかったレース(収集の失敗・"
      "システム停止)も分母に含めています。**")
    a("")
    a("| 分類 | レース数 | 割合 |")
    a("|---|---|---|")
    from .coverage import STATUS_JP
    for k, n in sorted((c.get("by_status") or {}).items(), key=lambda kv: -kv[1]):
        a(f"| {STATUS_JP.get(k, k)} | {n} | {_pct(n / c['races']) if c['races'] else '—'} |")
    a("")

    a("## 6. 購入の経路")
    a("")
    m = rep["manual"]
    if m["n"]:
        a(f"自動購入のほかに、**手動による購入が {m['n']} 件 {_yen(m['amount'])} "
          f"あります**(システムの動作検証のための少額購入を含む)。内訳:")
        a("")
        a("| 開催日 | レース | 券種 | 組 | 金額 | 受付 |")
        a("|---|---|---|---|---|---|")
        for r in m["rows"]:
            a(f"| {r['budget_key']} | {r['race_id']} | {r['bet_type']} "
              f"| {r['selection_id']} | {r['amount']:,} | {r['receipt']} |")
    else:
        a("対象期間の購入はすべて自動購入です(手動購入はありません)。")
    a("")

    a("## 7. 所得の計算")
    a("")
    a("事実から機械的に計算できる両案を併記します。**どちらを採るかの判断は"
      "本文書の範囲外です。**")
    a("")
    a("| | 金額 | 計算式 |")
    a("|---|---|---|")
    a(f"| 雑所得として | {rep['tax']['zatsu']:+,} 円 "
      f"| 払戻総額 − 購入総額(ハズレ馬券を含む全額を経費とする) |")
    if rep["tax"]["ichiji"] is None:
        a("| 一時所得として | 算出不能 | 払戻との突合が未了"
          "(払戻データは開催の3〜5日後に配信されるため) |")
    else:
        a(f"| 一時所得として | {rep['tax']['ichiji']:+,} 円 "
          f"| (払戻総額 − 的中馬券の購入額 {st['hit_cost']:,} 円 − 特別控除 "
          f"{rep['tax']['ichiji_deduction']:,} 円) ÷ 2 |")
    a("")
    a(f"払戻との突合済み: {st['n_settled']} 件(うち的中 {st['n_hit']} 件)")
    a("")

    a("## 8. 記録の裏づけ")
    a("")
    sl = rep["seals"]
    a(f"- 各開催日の購入額・払戻額・網羅性・依拠した設定の版を、その日のうちに"
      f"ハッシュ化して封印しています(**{sl['links']} 環 / {sl['days']} 日**、"
      f"最終封印 {sl['last']})。")
    a("- 各封印は**直前の封印のハッシュを含む**ため、過去の記録を変更すると"
      "それ以降の封印がすべて不整合になります。")
    a("- 購入指示の訂正・再実行は削除ではなく退避(`*_archive`)として残しています。")
    a("- 購入指示1件ごとに、判断に用いた入力値と適用した規則の判定結果を"
      "保存しています(`bet_decision_logs`)。")
    a("")
    a("### 典拠テーブル")
    a("")
    a("| 内容 | テーブル |")
    a("|---|---|")
    a("| 購入額・払戻額(真実源) | `ipat_receipts` / `ipat_vote_lines`(IPAT の投票履歴) |")
    a("| 購入指示 | `bet_orders`(`strategy_version_id` で版に接続) |")
    a("| 判断の根拠 | `bet_decision_logs`(規則ごとの閾値・実測値・判定) |")
    a("| 条件設定の版 | `strategy_versions` |")
    a("| 網羅性 | `race_coverage` |")
    a("| 封印 | `tax_ledger_seals` |")
    a("")
    return "\n".join(out)


def save(conn, year: str, markdown: str, summary: dict | None = None) -> dict:
    """生成した戦略書を保存する。**追記のみ**(作り直すと revision が増える)。

    ★古い版は消さない。提出済みの資料が消えるのは最悪。
    ★同じ本文なら新しい版を作らない(生成しただけで版が増えると、何版が
      提出したものか分からなくなる)。
    """
    import hashlib
    import json

    h = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    row = conn.execute(
        "SELECT revision, content_hash FROM strategy_documents"
        " WHERE year=%s ORDER BY revision DESC LIMIT 1", (year,)).fetchone()
    prev_rev = int(row[0]) if row else 0
    if row and row[1] == h:
        return {"status": "unchanged", "year": year, "revision": prev_rev, "hash": h}
    conn.execute(
        "INSERT INTO strategy_documents(year, revision, markdown, content_hash, summary)"
        " VALUES(%s,%s,%s,%s,%s::jsonb)",
        (year, prev_rev + 1, markdown, h,
         json.dumps(summary or {}, ensure_ascii=False, sort_keys=True)))
    return {"status": "saved", "year": year, "revision": prev_rev + 1, "hash": h}


def summary_of(rep: dict) -> dict:
    """保存に添える要約(一覧で中身を開かずに見分けるため)。"""
    t, c = rep["totals"], rep["coverage"]
    return {"bought": t["bought"], "payout": t["payout"], "pnl": t["pnl"],
            "roi": t["roi"], "days": t["days"],
            "races": c["races"], "evaluated": c["evaluated"],
            "evaluated_ratio": c["evaluated_ratio"],
            "bought_races": c["bought_races"]}
