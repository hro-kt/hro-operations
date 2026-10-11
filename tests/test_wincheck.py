"""Windows 機の設定確認。

★手順書は腐る。確認できることはコマンドにする。
★見るのは「**実際に踏んだ**」ものだけ。踏んでいない心配事を並べると、本当に効く
  項目が埋もれる。
"""

from __future__ import annotations

import json

from hro_operations import wincheck as wc


def test_bitness_requires_64_for_this_package():
    """★JV-Link は 32bit COM だが、run-day と hro-buyer は 64bit venv が要る。"""
    r = wc.check_bitness()
    assert r["level"] in (wc.OK, wc.NG)
    assert "64bit" in r["detail"] or "32bit" in r["detail"]


def test_home_flags_a_missing_repo(tmp_path, monkeypatch):
    """★agent は HRO_HOME から cwd を組む。実体と違うと全ジョブが落ちる。"""
    monkeypatch.setenv("HRO_HOME", str(tmp_path))
    (tmp_path / "hro-operations").mkdir()
    rows = wc.check_home()
    by = {r["name"].strip(): r for r in rows}
    assert by["hro-operations"]["level"] == wc.OK
    assert by["hro-synchronizer"]["level"] == wc.NG


def test_ipat_env_lists_what_is_missing(monkeypatch):
    for k in ("IPAT_SUBSCRIBER_NO", "IPAT_PIN", "IPAT_PARS_NO", "IPAT_INET_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("IPAT_PIN", "x")
    r = wc.check_ipat_env()
    assert r["level"] == wc.NG
    assert "IPAT_PIN" not in r["detail"] and "IPAT_SUBSCRIBER_NO" in r["detail"]
    # ★環境変数はプロセス起動時に読まれる。再起動が要ることまで書く
    assert "再起動" in r["fix"]


def test_ipat_env_does_not_print_values(monkeypatch):
    """★確認結果はログや画面に出る。認証情報の値は絶対に出さない。"""
    for k in ("IPAT_SUBSCRIBER_NO", "IPAT_PIN", "IPAT_PARS_NO", "IPAT_INET_ID"):
        monkeypatch.setenv(k, "SECRET123")
    r = wc.check_ipat_env()
    assert r["level"] == wc.OK and "SECRET123" not in json.dumps(r, ensure_ascii=False)


def _recipe(tmp_path, **kw):
    from hro_buyer.ipat import RECIPE_VERSION
    d = {"version": RECIPE_VERSION, "verified": True}
    d.update(kw)
    p = tmp_path / "ipat_recipe.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    return str(p)


def test_recipe_ok(tmp_path):
    assert wc.check_recipe(_recipe(tmp_path))["level"] == wc.OK


def test_recipe_not_verified_is_fatal(tmp_path):
    """★verified でないと run-day が live の起動を拒否する。"""
    r = wc.check_recipe(_recipe(tmp_path, verified=False))
    assert r["level"] == wc.NG and "verified" in r["detail"]


def test_recipe_version_mismatch_is_fatal(tmp_path):
    """★版がずれていると、画面の作りが変わっているのに古い手順で操作しに行く。"""
    r = wc.check_recipe(_recipe(tmp_path, version=1))
    assert r["level"] == wc.NG and "版" in r["detail"]


def test_recipe_missing_file(tmp_path):
    r = wc.check_recipe(str(tmp_path / "none.json"))
    assert r["level"] == wc.NG and "ありません" in r["detail"]


def test_db_failure_points_at_the_new_private_ip(monkeypatch):
    """★新しい VM は私設 IP が変わる。VNet/NSG で弾かれる経路を名指しする。"""
    import hro_operations.agent as ag
    monkeypatch.setattr(ag, "_conninfo", lambda: "host=203.0.113.1 connect_timeout=1")
    rows = wc.check_db()
    assert rows[0]["level"] == wc.NG
    assert "VNet" in rows[0]["fix"] and "私設 IP" in rows[0]["fix"]


def test_run_all_can_skip_jvlink():
    """★開催中は run_odds が JV-Link を握っている。確認のために奪わない。"""
    rows = wc.run_all(skip_jvlink=True)
    assert not any(r["name"] == "JV-Link" for r in rows)


def test_home_points_at_the_real_location_when_misconfigured(tmp_path, monkeypatch):
    """★「間違っている」だけでなく**直すべき値**を出す。

    空の ~\\hro が存在すると、ディレクトリの有無だけでは気づけない
    (2026-10-11 の新 VM で実際に踏んだ)。中身で判定し、このコードが動いている
    場所を正解として提示する。
    """
    empty = tmp_path / "hro"
    empty.mkdir()
    monkeypatch.setenv("HRO_HOME", str(empty))
    rows = wc.check_home()
    assert rows[0]["level"] == wc.NG
    assert "リポジトリがありません" in rows[0]["detail"]
    # 直すべき値(= このコードの実在場所)が fix に入っている
    assert wc.actual_home() in rows[0]["fix"]
    assert "再起動" in rows[0]["fix"]


def test_home_flags_the_gap_even_when_repos_exist(tmp_path, monkeypatch):
    """別の場所にも一式あるが、コードは違う場所から動いている場合。"""
    other = tmp_path / "hro"
    for r in ("hro-operations", "hro-synchronizer", "hro-buyer"):
        (other / r).mkdir(parents=True)
    monkeypatch.setenv("HRO_HOME", str(other))
    rows = wc.check_home()
    assert any("実体とのずれ" in r["name"] for r in rows)


def test_actual_home_is_the_repo_parent():
    import os
    assert os.path.isdir(os.path.join(wc.actual_home(), "hro-operations"))


def test_jvlink_finds_the_synchronizer_even_with_a_broken_home(tmp_path, monkeypatch):
    """★HRO_HOME が壊れていても JV-Link は確認したい。"""
    monkeypatch.setenv("HRO_HOME", str(tmp_path))
    r = wc.check_jvlink(home=str(tmp_path))
    # 実体側に hro-synchronizer があるので「見つかりません」にはならない
    assert "見つかりません" not in r["detail"]
