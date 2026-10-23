"""ポップアップ監視。★「閉じてよいものだけ閉じる」を試験で縛る。"""
import json
import pytest

from hro_operations.popup_guard import (
    Rule, Win, match, pick_clicks, suggest_rule, load_rules, dump_rules, watch,
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


# --- 既定の規則は「実物を採って確かめたもの」だけ ---------------------------- #
# 2026-10-24 に Windows で実際に採れた JV-Link セットアップダイアログ。
_SETUP = Win(
    hwnd=1, title="セットアップ", class_name="#32770", pid=9,
    exe=r"C:\Users\kyosuke.tomoda\AppData\Local\Programs\Python\Python312-32\python.exe",
    buttons=("OK", "キャンセル",
             "スタートキット(CD/DVD-ROM)を持っている（推奨）",
             "スタートキット(CD/DVD-ROM)を持っていない"))


def test_the_jvlink_setup_dialog_is_recognised():
    r = match(_SETUP, DEFAULT_RULES)
    assert r is not None and r.name == "jvlink-setup"


def test_the_setup_dialog_picks_no_start_kit_then_ok_in_that_order():
    """★この機に DVD は無い。既定で選ばれている「持っている」のまま OK を押すと、
       空のドライブを読みに行って進まない。ラジオが**先**であること。
    """
    r = match(_SETUP, DEFAULT_RULES)
    assert pick_clicks(_SETUP, r) == [
        "スタートキット(CD/DVD-ROM)を持っていない", "OK"]


def test_the_setup_rule_does_not_grab_the_recommended_radio():
    """★「持っている（推奨）」に当たると、意味が真逆になる。"""
    r = match(_SETUP, DEFAULT_RULES)
    assert "持っている（推奨）" not in pick_clicks(_SETUP, r)[0]


def test_the_setup_rule_does_not_fire_on_someone_elses_dialog():
    """★title『セットアップ』は、ありふれている。exe と class で絞る。"""
    other = Win(hwnd=2, title="セットアップ", class_name="#32770", pid=9,
                exe=r"C:\Program Files\Something\installer.exe", buttons=("OK",))
    assert match(other, DEFAULT_RULES) is None


def test_every_default_rule_was_taken_from_a_real_window():
    """★推測で書いた規則を載せない。title/class/exe が全部埋まっていること。"""
    for r in DEFAULT_RULES:
        assert r.title and r.class_name and r.exe, r.name


# --- 押すボタン ------------------------------------------------------------- #
def test_picks_the_buttons_the_rule_names_in_order():
    r = Rule(name="jv", title="お知らせ", clicks=("^キャンセル$", "^OK$"))
    assert pick_clicks(_w(buttons=("キャンセル", "OK")), r) == ["キャンセル", "OK"]


def test_no_clicks_means_wm_close():
    assert pick_clicks(_w(), Rule(name="jv", title="お知らせ")) == []


def test_a_missing_button_cancels_the_whole_sequence():
    """★ここが肝。ラジオが見つからないまま OK だけ押すと、意図と違う
       選択肢で先へ進む。半端に押すくらいなら、押さない。
    """
    r = Rule(name="jv", title="お知らせ", clicks=("^持っていない$", "^OK$"))
    assert pick_clicks(_w(buttons=("OK",)), r) is None


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
    dump_rules([Rule(name="jv", title="お知らせ", clicks=("^A$", "^OK$"))], p)
    mine = next(r for r in load_rules(p) if r.name == "jv")
    assert mine.clicks == ("^A$", "^OK$")               # ★list で戻っても tuple


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
    n, acted = _run([_w()], [Rule(name="jv", title="お知らせ", clicks=("^OK$",))], close=True)
    assert (n, acted) == (1, [("お知らせ", ["OK"])])


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
    """★--close したのに何も閉じない、を黙って起こさない。

    ★ここは**既定ごと差し替えた規則ファイル**を渡し、--once も付ける。
      どちらかを忘れると既定の jvlink-setup が有効なまま見張りが始まり、
      試験が終わらない(2026-10-24 に実際に固まった)。
    """
    import json as _j
    p = tmp_path / "r.json"
    p.write_text(_j.dumps({"replace_defaults": True,
                           "rules": [{"name": "off", "title": "x", "enabled": False}]}),
                 encoding="utf-8")
    assert ops_main(["popups", "watch", "--close", "--once", "--rules", str(p)]) == 1
    assert "有効な規則が1件もありません" in capsys.readouterr().out


# --- 既定と自分の規則の関係 -------------------------------------------------- #
def test_a_user_file_adds_to_the_defaults_instead_of_replacing_them(tmp_path):
    """★置き換えにすると、1つ書き足した日に JV-Link の規則が黙って消える。"""
    p = str(tmp_path / "r.json")
    dump_rules([Rule(name="mine", title="^何か$")], p)
    names = [r.name for r in load_rules(p)]
    assert "jvlink-setup" in names and "mine" in names


def test_a_same_named_rule_overrides_just_that_default(tmp_path):
    p = str(tmp_path / "r.json")
    dump_rules([Rule(name="jvlink-setup", title="^別の窓$", enabled=False)], p)
    rules = load_rules(p)
    assert [r.name for r in rules].count("jvlink-setup") == 1
    assert match(_SETUP, rules) is None      # 差し替わっている


def test_replace_defaults_is_available_when_you_really_mean_it(tmp_path):
    import json as _j
    p = tmp_path / "r.json"
    p.write_text(_j.dumps({"replace_defaults": True,
                           "rules": [{"name": "mine", "title": "^何か$"}]}),
                 encoding="utf-8")
    assert [r.name for r in load_rules(str(p))] == ["mine"]


def test_cli_watch_supports_one_pass(monkeypatch, capsys):
    """★`--once` が無いと、試験からも運用からも「1回だけ片付ける」ができない。
       そして試験が止まらなくなる。
    """
    _fake_windows(monkeypatch, [_SETUP])
    assert ops_main(["popups", "watch", "--once"]) == 0
    assert "jvlink-setup" in capsys.readouterr().out
