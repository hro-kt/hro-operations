"""Windows 機(JV-Link / IPAT)の設定が揃っているかを機械で確認する。

★手順書は腐る。確認できることはコマンドにして、開催日の朝に1回流せるようにする。
★ここで見るのは「**実際に踏んだ**」ものだけ。踏んでいない心配事を並べると、
  本当に効く項目が埋もれる。根拠を各項目のコメントに残す。
"""

from __future__ import annotations

import os
import subprocess
import sys

OK, WARN, NG = "ok", "warn", "ng"


def _r(name: str, level: str, detail: str, fix: str = "") -> dict:
    return {"name": name, "level": level, "detail": detail, "fix": fix}


def check_bitness() -> dict:
    """この Python が 64bit か。

    ★JV-Link は 32bit COM なので hro-synchronizer は 32bit venv。一方 run-day と
      hro-buyer は psycopg や Playwright を使うので **64bit venv** が要る。
      同じ venv に同居させようとして動かなくなるのが定番。
    """
    bits = 64 if sys.maxsize > 2 ** 32 else 32
    if bits == 64:
        return _r("Python(hro-operations)", OK, "64bit")
    return _r("Python(hro-operations)", NG, "32bit で動いています",
              "run-day / hro-buyer は 64bit venv で動かすこと"
              "(32bit は hro-synchronizer 専用)")


def actual_home() -> str:
    """**このコードが実在する場所**から、あるべき HRO_HOME を割り出す。

    ★環境変数を見るだけでは「間違っている」としか言えず、正解を言えない。
      このファイルは <HOME>/hro-operations/hro_operations/wincheck.py に在るので、
      3つ上がリポジトリ親=HRO_HOME のあるべき値。設定ミスを指摘するときは
      **直すべき値まで出す**。
    """
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def check_home() -> list[dict]:
    r"""HRO_HOME とリポジトリの実体。

    ★agent は HRO_HOME からジョブの cwd を組む。実体が Documents\hro なのに
      ~\hro を指していると「作業ディレクトリが存在しません」で全ジョブが落ちる
      (2026-10-11 の新 VM で実際に踏んだ)。**空の ~\hro が存在すると、
      ディレクトリの有無だけでは気づけない**ので、中身で判定する。
    """
    env_set = bool(os.environ.get("HRO_HOME"))
    home = os.environ.get("HRO_HOME") or os.path.join(os.path.expanduser("~"), "hro")
    want = actual_home()
    repos = ("hro-operations", "hro-synchronizer", "hro-buyer")
    found = [r for r in repos if os.path.isdir(os.path.join(home, r))]

    if not found:
        detail = (f"{home}" + ("" if env_set else "(未設定なので既定)")
                  + " にリポジトリがありません")
        fix = (f'setx /M HRO_HOME "{want}" を実行し、**エージェントを再起動**'
               "(環境変数はプロセス起動時に読まれます)")
        return [_r("HRO_HOME", NG, detail, fix),
                _r("  実体の場所", OK, f"{want}(このコードが動いている場所)")]

    out = [_r("HRO_HOME", OK if len(found) == len(repos) else NG,
              home + ("" if env_set else "(未設定なので既定)"))]
    for r in repos:
        p = os.path.join(home, r)
        out.append(_r(f"  {r}", OK if os.path.isdir(p) else NG, p,
                      "" if os.path.isdir(p) else f"git clone するか HRO_HOME を {want} に"))
    if os.path.normcase(os.path.abspath(home)) != os.path.normcase(want):
        out.append(_r("  ★実体とのずれ", NG,
                      f"HRO_HOME={home} / このコードは {want} から動いています",
                      f'setx /M HRO_HOME "{want}" → エージェント再起動'))
    return out


def check_ipat_env() -> dict:
    """IPAT の認証情報が環境変数に在るか(値は見ない)。"""
    need = ("IPAT_SUBSCRIBER_NO", "IPAT_PIN", "IPAT_PARS_NO", "IPAT_INET_ID")
    miss = [k for k in need if not os.environ.get(k)]
    if miss:
        return _r("IPAT の認証情報", NG, f"未設定: {', '.join(miss)}",
                  "システム環境変数に設定し、**エージェントを再起動**すること"
                  "(環境変数はプロセス起動時に読まれる)")
    return _r("IPAT の認証情報", OK, "4つとも設定済み(値は確認しません)")


def check_recipe(path: str | None = None) -> dict:
    """IPAT レシピが在り、実画面で検証済みで、版が合っているか。

    ★verified でないと run-day が live の起動を拒否する。版がずれていると
      画面の作りが変わっているのに古い手順で操作しに行く。
    """
    import json
    p = path or os.environ.get("HRO_IPAT_RECIPE") or os.path.join(
        os.path.expanduser("~"), "ipat_recipe.json")
    if not os.path.isfile(p):
        return _r("IPAT レシピ", NG, f"ありません: {p}",
                  "hro-buyer ipat probe で作り、dry-vote で検証すること")
    try:
        d = json.loads(open(p, encoding="utf-8").read())
    except Exception as e:  # noqa: BLE001
        return _r("IPAT レシピ", NG, f"読めません: {e}", "作り直すこと")
    try:
        from hro_buyer.ipat import RECIPE_VERSION
    except Exception:       # noqa: BLE001 - hro-buyer 未導入
        return _r("IPAT レシピ", WARN, f"{p}(hro-buyer を import できず版を確認できません)")
    ver, verified = d.get("version"), bool(d.get("verified"))
    if not verified:
        return _r("IPAT レシピ", NG, f"verified でない({p})",
                  "hro-buyer ipat dry-vote で実画面を通し --set-verified すること")
    if ver != RECIPE_VERSION:
        return _r("IPAT レシピ", NG, f"版が違う: レシピ {ver} / コード {RECIPE_VERSION}",
                  "画面の手順が変わっています。dry-vote で取り直すこと")
    return _r("IPAT レシピ", OK, f"verified / 版 {ver}")


def check_db() -> list[dict]:
    """DB に届くか、記録に要る列が在るか、**時計がずれていないか**。

    ★新しい VM は私設 IP が変わる。VNet/NSG で弾かれていないかをここで見る。
    ★締切判定はこの機の時計で行う。ずれていると、締切前なのに捨てたり、
      締切後に送ったりする。DB の now() と突き合わせる。
    """
    from datetime import datetime, timezone
    out: list[dict] = []
    try:
        import psycopg

        from .agent import _conninfo
        t0 = datetime.now(timezone.utc)
        with psycopg.connect(_conninfo(), connect_timeout=10) as conn:
            db_now = conn.execute("SELECT now()").fetchone()[0]
            have = {r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name='bet_orders'").fetchall()}
    except Exception as e:  # noqa: BLE001
        return [_r("DB 接続", NG, f"{type(e).__name__}: {str(e)[:150]}",
                   "POSTGRES_* と VNet/NSG を確認(新しい VM は私設 IP が変わります)")]
    out.append(_r("DB 接続", OK, "接続できました"))
    skew = abs((db_now - t0).total_seconds())
    out.append(_r("時計のずれ", OK if skew < 5 else NG, f"{skew:.1f} 秒",
                  "" if skew < 5 else
                  "締切判定がこの機の時計で行われます。w32tm /resync で合わせること"))
    if "strategy_version_id" not in have:
        out.append(_r("スキーマ", NG, "bet_orders.strategy_version_id がありません",
                      "hro-db/schema/*.sql を番号順に適用(無いと1件も買えません)"))
    else:
        out.append(_r("スキーマ", OK, "記録に要る列は揃っています"))
    return out


def check_agent() -> dict:
    """この機の agent が登録され、心拍が新しいか。"""
    try:
        import psycopg

        from .agent import _conninfo
        with psycopg.connect(_conninfo(), connect_timeout=10) as conn:
            row = conn.execute(
                "SELECT EXTRACT(EPOCH FROM (now()-last_seen))::int FROM ops_agent"
                " WHERE server='windows'").fetchone()
    except Exception as e:  # noqa: BLE001
        return _r("エージェント", WARN, f"確認できません: {str(e)[:120]}")
    if not row:
        return _r("エージェント", NG, "windows の登録がありません",
                  "hro-ops agent --server windows を起動すること")
    age = int(row[0])
    if age > 60:
        return _r("エージェント", NG, f"心拍が {age} 秒前",
                  "起動していません。VM の起動時に自動で上がるようにすること")
    return _r("エージェント", OK, f"心拍 {age} 秒前")


def check_jvlink(home: str | None = None) -> dict:
    """JV-Link が使えるか。32bit venv の hro-synchronizer に聞く。

    ★ここだけは別 venv に出て確認する。この(64bit)プロセスからは COM を開けない。
    """
    # ★HRO_HOME が壊れていても JV-Link は確認したい。実体の場所も見る。
    cands = [home, os.environ.get("HRO_HOME"),
             os.path.join(os.path.expanduser("~"), "hro"), actual_home()]
    cwd = next((os.path.join(b, "hro-synchronizer") for b in cands
                if b and os.path.isdir(os.path.join(b, "hro-synchronizer"))), None)
    if cwd is None:
        return _r("JV-Link", NG, "hro-synchronizer が見つかりません",
                  f"HRO_HOME を {actual_home()} に")
    env = {k: v for k, v in os.environ.items()
           if k not in ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "POETRY_ACTIVE",
                        "PYTHONHOME", "PYTHONPATH")}
    try:
        p = subprocess.run(["poetry", "run", "hro-synchronizer", "env-check"],
                           cwd=cwd, env=env, capture_output=True, text=True,
                           timeout=180)
    except Exception as e:  # noqa: BLE001
        return _r("JV-Link", NG, f"env-check を実行できません: {str(e)[:150]}")
    tail = (p.stdout or p.stderr or "").strip().splitlines()[-3:]
    if p.returncode != 0:
        return _r("JV-Link", NG, " / ".join(tail) or f"exit {p.returncode}",
                  "32bit venv か、JV-Link の登録とサービスキーを確認すること")
    return _r("JV-Link", OK, " / ".join(tail) or "env-check 正常")


def run_all(*, skip_jvlink: bool = False) -> list[dict]:
    out = [check_bitness()]
    out += check_home()
    out.append(check_ipat_env())
    out.append(check_recipe())
    out += check_db()
    out.append(check_agent())
    if not skip_jvlink:
        out.append(check_jvlink())
    return out
