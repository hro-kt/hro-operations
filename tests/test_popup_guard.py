"""ポップアップ監視。★「閉じてよいものだけ閉じる」を試験で縛る。"""
import json
import pytest

from hro_operations.popup_guard import (
    Rule, Win, match, pick_button, suggest_rule, load_rules, dump_rules, watch,
    DEFAULT_RULES,
)


def _w(title="お知らせ", cls="#32770", exe=r"C:\JV\jvlink.exe", buttons=("OK",)):
    return Win(hwnd=1, title=title, class_name=cls, pid=9, exe=exe, buttons=buttons)


# --- 当たり判定 ------------------------------------------------------------- #
def test_matches_on_title():
    r = Rule(name="jv", title="お知らせ")
    assert match(_w(), [r]) is r


def test_all_given_conditions_must_hold():
    """★title だけ合って exe が違うものを閉じない。同じ文言の窓は他にもある。"""
    r = Rule(name="jv", title="お知らせ", exe="jvlink")
    assert match(_w(exe=r"C:\x\edge.exe"), [r]) is None


def test_exe_match_ignores_case():
    r = Rule(name="jv", title="お知らせ", exe="JVLink")
    assert match(_w(), [r]) is not None


def test_disabled_rule_never_matches():
    """★採ったばかりの規則は enabled=false。有効にするのは人の判断。"""
    r = Rule(name="jv", title="お知らせ", enabled=False)
    assert match(_w(), [r]) is None


def test_untitled_windows_are_never_closed():
    """★タイトルの無い窓はトレイ/IME の常駐窓。閉じると巻き添えが出る。"""
    r = Rule(name="any", class_name=".*")
    assert match(_w(title="   "), [r]) is None


def test_a_rule_without_title_or_class_is_refused():
    """★両方空の規則は「全部に当たる」。作らせない。"""
    with pytest.raises(ValueError, match="title か class_name"):
        Rule(name="bad", exe="jvlink")


def test_a_broken_regex_is_refused_at_construction():
    with pytest.raises(ValueError, match="正規表現"):
        Rule(name="bad", title="(お知らせ")


def test_default_rules_are_empty():
    """★既定で何も閉じない。実物を見る前に閉じにいかない。"""
    assert DEFAULT_RULES == []


# --- 押すボタン ------------------------------------------------------------- #
def test_picks_the_button_the_rule_names():
    r = Rule(name="jv", title="お知らせ", button="^(OK|はい)$")
    assert pick_button(_w(buttons=("キャンセル", "OK")), r) == "OK"


def test_no_button_means_wm_close():
    assert pick_button(_w(), Rule(name="jv", title="お知らせ")) is None


def test_button_not_present_falls_back_to_wm_close():
    r = Rule(name="jv", title="お知らせ", button="^はい$")
    assert pick_button(_w(buttons=("OK",)), r) is None


# --- 雛形 ------------------------------------------------------------------- #
def test_suggested_rule_is_disabled_and_digit_tolerant():
    """★採った title をそのまま使うと、件数や日付が変わった日に当たらない。"""
    d = suggest_rule(_w(title="未取得データが 12 件あります"))
    assert d["enabled"] is False
    assert "12" not in d["title"]
    assert r"\d+" in d["title"]


def test_suggested_rule_round_trips_into_a_rule():
    d = suggest_rule(_w())
    d["enabled"] = True
    assert match(_w(), [Rule(**d)]) is not None


# --- 規則ファイル ----------------------------------------------------------- #
def test_unknown_key_in_the_rules_file_is_an_error(tmp_path):
    """★綴り間違いを黙って無視すると「書いたのに閉じない」になる。"""
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"rules": [{"name": "a", "titel": "x"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="知らない項目"):
        load_rules(str(p))


def test_rules_round_trip(tmp_path):
    p = str(tmp_path / "sub" / "r.json")
    dump_rules([Rule(name="jv", title="お知らせ", button="^OK$")], p)
    assert load_rules(p)[0].button == "^OK$"


def test_missing_file_gives_the_defaults(tmp_path):
    assert load_rules(str(tmp_path / "nope.json")) == DEFAULT_RULES


# --- 監視ループ ------------------------------------------------------------- #
def _run(wins, rules, **kw):
    acted = []
    def closer(w, b):
        acted.append((w.title, b))
        return "click"
    n = watch(rules=rules, once=True, lister=lambda: list(wins),
              sleep=lambda s: None, closer=closer, **kw)
    return n, acted


def test_watch_records_but_does_not_close_by_default(capsys):
    """★既定は記録のみ。一日ぶん見てから規則を書く。"""
    n, acted = _run([_w()], [Rule(name="jv", title="お知らせ")])
    assert (n, acted) == (0, [])
    assert "記録のみ" in capsys.readouterr().out


def test_watch_closes_when_asked():
    n, acted = _run([_w()], [Rule(name="jv", title="お知らせ", button="^OK$")], close=True)
    assert (n, acted) == (1, [("お知らせ", "OK")])


def test_watch_leaves_unmatched_windows_alone():
    n, acted = _run([_w(title="IPAT - Microsoft Edge", cls="Chrome_WidgetWin_1")],
                    [Rule(name="jv", title="お知らせ")], close=True)
    assert (n, acted) == (0, [])


def test_watch_logs_every_window_it_sees_so_rules_can_be_written(capsys):
    """★規則が無い窓こそ記録したい。これが唯一の手掛かりになる。"""
    _run([_w(title="JRA-VAN からのお知らせ")], [])
    out = capsys.readouterr().out
    assert "[規則なし]" in out and "JRA-VAN" in out


def test_a_failure_to_close_does_not_stop_the_watch(capsys):
    def boom(w, b):
        raise OSError("アクセスが拒否されました")
    n = watch(rules=[Rule(name="jv", title="お知らせ", enabled=True)], once=True,
              close=True, lister=lambda: [_w()], sleep=lambda s: None, closer=boom)
    assert n == 0
    assert "閉じられませんでした" in capsys.readouterr().out


def test_a_failure_to_list_windows_does_not_stop_the_watch(capsys):
    def boom():
        raise OSError("EnumWindows failed")
    assert watch(rules=[], once=True, lister=boom, sleep=lambda s: None) == 0
    assert "ウィンドウ一覧に失敗" in capsys.readouterr().out


# --- CLI(★実際に呼ぶ。呼ばないまま出して `b.page` を壊した前科がある) ------ #
import json as _json

from hro_operations.__main__ import main as ops_main


def _fake_windows(monkeypatch, wins):
    monkeypatch.setattr("hro_operations.popup_guard.list_windows", lambda **k: list(wins))


def test_cli_list_prints_windows_without_a_rule(monkeypatch, capsys, tmp_path):
    _fake_windows(monkeypatch, [_w(title="JRA-VAN からのお知らせ")])
    assert ops_main(["popups", "list", "--rules", str(tmp_path / "none.json")]) == 0
    assert "JRA-VAN" in capsys.readouterr().out


def test_cli_list_suggest_emits_pasteable_json(monkeypatch, capsys, tmp_path):
    _fake_windows(monkeypatch, [_w(title="未取得データが 12 件あります")])
    ops_main(["popups", "list", "--suggest", "--rules", str(tmp_path / "none.json")])
    out = capsys.readouterr().out
    body = _json.loads(out[out.index("{"):])
    assert body["rules"][0]["enabled"] is False     # ★採った直後は無効


def test_cli_list_hides_windows_a_rule_already_covers(monkeypatch, capsys, tmp_path):
    """★採取したいのは『まだ規則の無い窓』。既知を並べると埋もれる。"""
    p = tmp_path / "r.json"
    dump_rules([Rule(name="jv", title="お知らせ")], str(p))
    _fake_windows(monkeypatch, [_w(title="お知らせ")])
    ops_main(["popups", "list", "--rules", str(p)])
    assert "規則に当たらない窓はありません" in capsys.readouterr().out
    ops_main(["popups", "list", "--all", "--rules", str(p)])
    assert "(jv)" in capsys.readouterr().out


def test_cli_watch_close_refuses_when_no_rule_is_enabled(capsys, tmp_path):
    """★--close したのに何も閉じない、を黙って起こさない。"""
    assert ops_main(["popups", "watch", "--close", "--rules", str(tmp_path / "none.json")]) == 1
    assert "有効な規則が1件もありません" in capsys.readouterr().out
