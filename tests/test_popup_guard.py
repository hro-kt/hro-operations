"""ポップアップ監視。★「閉じてよいものだけ閉じる」を試験で縛る。"""
import json
import pytest

from hro_operations.popup_guard import (
    Rule, Win, choose, match, pick_clicks, suggest_rule, load_rules, dump_rules,
    watch, DEFAULT_RULES,
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
    r, _ = choose(_SETUP, DEFAULT_RULES)
    assert r.name == "jvlink-setup"
    assert pick_clicks(_SETUP, r) == [
        "スタートキット(CD/DVD-ROM)を持っていない", "OK"]


def test_the_setup_rule_does_not_grab_the_recommended_radio():
    """★「持っている（推奨）」に当たると、意味が真逆になる。"""
    r, _ = choose(_SETUP, DEFAULT_RULES)
    assert "持っている（推奨）" not in pick_clicks(_SETUP, r)[0]


# 2枚目。「合計 N Mバイトのディスク空き容量が必要です。よろしいですか？」
# ★title も class も exe も1枚目と**まったく同じ**。違うのはボタンだけ。
_CONFIRM = Win(hwnd=2, title="セットアップ", class_name="#32770", pid=9,
               exe=_SETUP.exe, buttons=("OK", "キャンセル"))


def test_the_second_setup_dialog_is_told_apart_by_its_buttons():
    """★1枚目の規則は『当たるが押せない』。そこで打ち切ると2枚目が永久に残り、
       セットアップが進まない(2026-10-24 に実際に止まった)。
    """
    r, notes = choose(_CONFIRM, DEFAULT_RULES)
    assert r.name == "jvlink-setup-confirm"
    assert pick_clicks(_CONFIRM, r) == ["OK"]
    assert any("jvlink-setup" in n and "見送り" in n for n in notes)


def test_the_confirm_rule_never_steals_the_first_dialog():
    """★2枚目の規則(OK を押すだけ)が1枚目に当たると、ラジオを選ばずに
       OK を押すことになり、意味が真逆になる。順番が効いていること。
    """
    r, _ = choose(_SETUP, DEFAULT_RULES)
    assert r.name == "jvlink-setup"


def test_choose_reports_why_it_passed_a_rule_over():
    """★黙って見送ると、規則を直す手掛かりが残らない。"""
    _, notes = choose(_CONFIRM, DEFAULT_RULES)
    assert "持っていない$" in notes[0] and "OK" in notes[0]


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
    def closer(w, b, expect_gone=True):
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
    def boom(w, b, expect_gone=True):
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
    mine = next(r for r in rules if r.name == "jvlink-setup")
    assert mine.title == "^別の窓$" and not mine.enabled   # 差し替わっている
    assert [r.name for r in rules] != ["jvlink-setup"]     # 他の既定は残る


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


# --- 押す対象の決め方(★2026-10-24 の取りこぼしの再発防止) ------------------- #
from hro_operations.popup_guard import is_toggle, resolve_handles

# JV-Link のセットアップの子ウィンドウ(handle, text, class)。
# ★同じ文言の STATIC が隣にいる形を再現する。
_CHILDREN = [
    (101, "スタートキット(CD/DVD-ROM)を持っていない", "Static"),   # ラベル側が先
    (102, "スタートキット(CD/DVD-ROM)を持っている（推奨）", "Button"),
    (103, "スタートキット(CD/DVD-ROM)を持っていない", "Button"),   # 本物のラジオ
    (104, "OK", "Button"),
    (105, "キャンセル", "Button"),
]


def test_only_real_buttons_are_clicked():
    """★STATIC に BM_CLICK を送っても何も起きない。それに気づかないまま
       次の OK を押すと、既定の選択肢(＝持っている)で先へ進む。
       2026-10-24 にこれで DVD 側が選ばれた。
    """
    got = resolve_handles(_CHILDREN, ["スタートキット(CD/DVD-ROM)を持っていない", "OK"])
    assert [h for _, h in got] == [103, 104]      # 101(Static) ではない


def test_resolve_keeps_the_given_order():
    got = resolve_handles(_CHILDREN, ["OK", "キャンセル"])
    assert [label for label, _ in got] == ["OK", "キャンセル"]


def test_resolve_refuses_when_a_label_is_missing():
    """★見つからないものがあれば、1つも押さない。"""
    with pytest.raises(RuntimeError, match="何も押しませんでした"):
        resolve_handles(_CHILDREN, ["持っていない", "OK"])   # 完全一致しない


def test_resolve_lists_what_was_actually_there():
    """★次に規則を直す人に、何があったのかを見せる。"""
    with pytest.raises(RuntimeError, match="ある BUTTON"):
        resolve_handles(_CHILDREN, ["はい"])


def test_radio_and_checkbox_styles_need_a_read_back():
    """★押しボタンは押せば終わり。選択式は『選ばれたか』を読み返す必要がある。"""
    for style in (0x02, 0x03, 0x04, 0x05, 0x06, 0x09):   # チェック/ラジオ各種
        assert is_toggle(style), hex(style)
    for style in (0x00, 0x01, 0x07):                      # 押しボタン/グループ枠
        assert not is_toggle(style), hex(style)


def test_toggle_detection_ignores_the_other_style_bits():
    """★BS_* の上位ビット(左寄せ・枠線など)が立っていても種類は変わらない。"""
    assert is_toggle(0x50012009)      # WS_CHILD|WS_VISIBLE|...|BS_AUTORADIOBUTTON
    assert not is_toggle(0x50010001)  # ...|BS_DEFPUSHBUTTON


# --- 押し方を変えながら、効いたかを確かめる -------------------------------- #
# ★2026-10-24: BM_CLICK を送っただけで「閉じました」と記録し、実際には
#   ダイアログが残っていた。嘘のログが一番高くつく。
from hro_operations.popup_guard import press_until


def _methods(names, log):
    return [(n, (lambda n=n: log.append(n))) for n in names]


def _clock():
    t = [0.0]
    return (lambda: t[0]), (lambda s: t.__setitem__(0, t[0] + s))


def test_the_first_method_that_works_is_the_answer():
    log = []
    now, sleep = _clock()
    got = press_until(_methods(["BM_CLICK", "mouse"], log), lambda: True, "OK",
                      now=now, sleep=sleep)
    assert got == "BM_CLICK" and log == ["BM_CLICK"]


def test_it_moves_on_when_a_method_does_not_take():
    """★BM_CLICK が効かない窓がある。1手で諦めない。"""
    log = []
    now, sleep = _clock()
    got = press_until(_methods(["a", "b", "c"], log),
                      lambda: log[-1:] == ["c"], "OK", now=now, sleep=sleep)
    assert got == "c" and log == ["a", "b", "c"]


def test_it_waits_for_the_result_instead_of_checking_once():
    """★窓が消えるのもラジオが点くのも即座ではない。1回見て諦めない。"""
    log, ticks = [], [0]
    now, sleep = _clock()

    def verify():
        ticks[0] += 1
        return ticks[0] >= 4          # 4回目でようやく True

    got = press_until(_methods(["a", "b"], log), verify, "OK", now=now, sleep=sleep)
    assert got == "a" and log == ["a"]


def test_it_raises_instead_of_claiming_success():
    """★ここが肝。効かなかったものを「閉じました」と書かない。"""
    log = []
    now, sleep = _clock()
    with pytest.raises(RuntimeError, match="押せませんでした"):
        press_until(_methods(["a", "b"], log), lambda: False, "OK",
                    now=now, sleep=sleep)
    assert log == ["a", "b"]          # 全部試してから諦める


def test_the_failure_says_what_was_tried():
    """★次の窓のために、何が効かなかったかを残す。"""
    now, sleep = _clock()
    try:
        press_until(_methods(["BM_CLICK", "mouse"], []), lambda: False, "OK",
                    now=now, sleep=sleep)
    except RuntimeError as e:
        assert "BM_CLICK" in str(e) and "mouse" in str(e)
    else:
        raise AssertionError("例外にならない")
