"""Windows のポップアップを見張って、**許可したものだけ**閉じる。

★なぜ要るか: JV-Link / JRA-VAN まわりは、たまにモーダルを出して処理を止める。
  人が消すまで sync-all が進まないので、無人運転がそこで途切れる。
  IPAT 側(Playwright)のモーダルは hro_buyer.ipat が閉じているが、
  ネイティブのウィンドウには手が届かない。ここが担当する。

★設計の芯は「許可リストにあるものしか閉じない」。
  「ダイアログっぽいものを全部閉じる」は絶対にやらない。JV-Link の
  進捗ウィンドウや、投票中の Edge を巻き込むと**静かに一日を壊す**。
  だから既定の許可リストは**空**で、まず `popups list` / `popups watch` で
  実物の title/class/exe を採ってから足す。IPAT の describe_element と同じ順序。

Win32 は ctypes だけで触る(32bit Python でも動く / 追加依存なし)。
判断は純粋関数に出してあるので、Mac でも試験できる。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class Win:
    """見えているトップレベルウィンドウ1枚。"""
    hwnd: int
    title: str
    class_name: str
    pid: int
    exe: str = ""
    buttons: tuple[str, ...] = ()      # 子ボタンの文字列(押す先を選ぶのに使う)


@dataclass(frozen=True)
class Rule:
    """閉じてよいウィンドウの条件。**すべて指定したものだけ**が AND で効く。"""
    name: str
    title: str = ""                    # 正規表現(部分一致)
    class_name: str = ""               # 正規表現(部分一致)
    exe: str = ""                      # 正規表現(部分一致・大小無視)
    # 押すボタンの正規表現を**この順に**。空なら WM_CLOSE。
    # ★ラジオを選んでから OK、のような順番つきの操作のためにリストにしてある。
    clicks: tuple[str, ...] = ()
    # ★押したあと窓が消えるはずか。消えなければ別の押し方に移り、
    #   どれも効かなければ例外にする(「押したつもり」を成功と書かない)。
    expect_gone: bool = True
    enabled: bool = True

    def __post_init__(self) -> None:
        # ★title も class も無い規則は「全部に当たる」。事故そのものなので作らせない。
        if not (self.title or self.class_name):
            raise ValueError(f"規則 {self.name!r}: title か class_name のどちらかは必須です"
                             "(両方空だと、あらゆるウィンドウに当たります)")
        object.__setattr__(self, "clicks", tuple(self.clicks))
        for fld in ("title", "class_name", "exe"):
            pat = getattr(self, fld)
            if pat:
                try:
                    re.compile(pat)
                except re.error as e:
                    raise ValueError(f"規則 {self.name!r} の {fld} が正規表現として壊れています: {e}") from e
        for i, pat in enumerate(self.clicks):
            try:
                re.compile(pat)
            except re.error as e:
                raise ValueError(f"規則 {self.name!r} の clicks[{i}] が正規表現として壊れています: {e}") from e


def _hit(pat: str, value: str, *, ignore_case: bool = False) -> bool:
    if not pat:
        return True
    flags = re.IGNORECASE if ignore_case else 0
    return re.search(pat, value or "", flags) is not None


def match(w: Win, rules: list[Rule]) -> Rule | None:
    """窓の**見分け**だけ。その規則が押せるかは見ない。★純粋関数。

    ★title が空のウィンドウは**絶対に閉じない**。タスクトレイの隠しウィンドウや
      IME の常駐窓は title が空で、閉じると巻き添えで変なことが起きる。
    """
    if not w.title.strip():
        return None
    for r in rules:
        if not r.enabled:
            continue
        if (_hit(r.title, w.title)
                and _hit(r.class_name, w.class_name)
                and _hit(r.exe, w.exe, ignore_case=True)):
            return r
    return None


def choose(w: Win, rules: list[Rule]) -> tuple[Rule | None, list[str]]:
    """実際に押せる規則を1つ選ぶ。選ばなかった理由も返す。★純粋関数。

    ★同じ title/class/exe の窓が、場面によって違うボタンを出すことがある。
      JV-Link のセットアップがそれで、1枚目は「ラジオ + OK」、2枚目は
      「OK / キャンセル」。title はどちらも『セットアップ』。
      見分けはボタンの顔ぶれでしかつかないので、**押せるかどうかで選ぶ**。
    ★「当たったが押せない」規則で打ち切らない。打ち切ると、2枚目が
      1枚目の規則に当たったまま永久に進まない(2026-10-24 に実際に止まった)。
    """
    if not w.title.strip():
        return None, []
    notes: list[str] = []
    for r in rules:
        if not r.enabled:
            continue
        if not (_hit(r.title, w.title)
                and _hit(r.class_name, w.class_name)
                and _hit(r.exe, w.exe, ignore_case=True)):
            continue
        if pick_clicks(w, r) is None:
            notes.append(f"{r.name}: ボタンが揃わないので見送り"
                         f"(要 {list(r.clicks)} / ある {list(w.buttons)})")
            continue
        return r, notes
    return None, notes


def pick_clicks(w: Win, rule: Rule) -> list[str] | None:
    """規則が指すボタンを、順に解決する。

    ★**全部そろわなければ何もしない**(None を返す)。これが一番大事。
      「ラジオを選んでから OK」の規則で、ラジオだけ見つからずに OK を押すと、
      意図と違う選択肢で先へ進む。JV-Link のセットアップなら、持っていない
      DVD を読みに行って固まる。途中まで押すくらいなら、押さないほうがよい。
    空のリストは「WM_CLOSE で閉じる」。
    """
    out: list[str] = []
    for pat in rule.clicks:
        hit = next((b for b in w.buttons if _hit(pat, b)), None)
        if hit is None:
            return None
        out.append(hit)
    return out


def suggest_rule(w: Win) -> dict:
    """見えているウィンドウから、貼り付けられる規則の雛形を作る。

    ★title をそのまま正規表現にすると、日付や件数が入った瞬間に当たらなくなる。
      数字列は \\d+ に置き換えておく。
    """
    title = re.escape(w.title.strip())
    title = re.sub(r"(?:\\ )?\d+(?:\\\.\d+)?", r"\\d+", title)
    return {
        "name": (w.exe or w.class_name or "popup").split("\\")[-1].replace(".exe", "") + "-dialog",
        "title": f"^{title}$",
        "class_name": re.escape(w.class_name) if w.class_name else "",
        "exe": re.escape(os.path.basename(w.exe)) if w.exe else "",
        # ★押す順に並べる。ラジオや chekcbox を先に入れること。
        "clicks": ["^(OK|はい|閉じる)$"] if w.buttons else [],
        "enabled": False,     # ★採った直後は無効。中身を見てから人が有効にする
    }


# --------------------------------------------------------------------------- #
# 規則ファイル
# --------------------------------------------------------------------------- #
# ★ここに載せてよいのは「実物を見て、押す先まで確かめたもの」だけ。
#   推測で書いた規則は載せない。実物の採り方は `popups list --suggest`。
#
# ★どれも --close(= HRO_POPUP_CLOSE=1)を付けない限り押さない。
#   既定の運転は記録のみ。
DEFAULT_RULES: list[Rule] = [
    # JV-Link のセットアップ。JVOpen の option=3/4 で出て、人が答えるまで
    # sync-all がそこで止まる。無人運転が途切れる主犯(2026-10-24 に実物を採取)。
    #
    # ★「スタートキット(CD/DVD-ROM)を持っていない」を選んで OK を押す。
    #   この機に DVD は無いので、持っている側(既定で選択済み)のまま OK を押すと
    #   空のドライブを読みに行って進まない。**ラジオを先に押すことが必須**。
    # ★答えた先は「全データのダウンロード」で、非常に時間がかかる。
    #   朝の支度として流すこと(司令塔が第1発走の6時間前に投入する)。
    #   発走が近づけば hro-orchestrator が打ち切る。
    Rule(
        name="jvlink-setup",
        title="^セットアップ$",
        class_name="^#32770$",            # Win32 の標準ダイアログ
        exe=r"python(w|3)?\.exe$",        # JV-Link の COM を抱えている自分自身
        clicks=("持っていない$", "^OK$"),
    ),
    # 2枚目。「合計 N Mバイトのディスク空き容量が必要です。よろしいですか？」
    # ★title も class も exe も1枚目と同じ。見分けはボタンの顔ぶれだけ
    #   (1枚目にはラジオが在り、こちらは OK/キャンセルのみ)。
    #   だから choose() が「押せる規則」を選ぶ。順番は 1枚目が先。
    Rule(
        name="jvlink-setup-confirm",
        title="^セットアップ$",
        class_name="^#32770$",
        exe=r"python(w|3)?\.exe$",
        clicks=("^OK$",),
    ),
]


def rules_path() -> str:
    home = os.environ.get("HRO_HOME") or os.path.expanduser("~")
    return os.environ.get("HRO_POPUP_RULES") or os.path.join(home, "hro-operations", "popup_rules.json")


def load_rules(path: str | None = None) -> list[Rule]:
    """規則ファイル + 既定の規則。

    ★ファイルは既定を**置き換えない**。追加する。置き換えにすると、自分で
      1つ書き足した日に、確かめ済みの JV-Link セットアップ規則が黙って消える。
      同名の規則を書けば、その1つだけを差し替えられる。
      丸ごと置き換えたいときだけ "replace_defaults": true。
    """
    p = path or rules_path()
    if not os.path.exists(p):
        return list(DEFAULT_RULES)
    with open(p, encoding="utf-8") as f:
        raw = json.load(f)
    items = raw.get("rules", raw) if isinstance(raw, dict) else raw
    replace = bool(raw.get("replace_defaults")) if isinstance(raw, dict) else False
    out: list[Rule] = []
    for i, d in enumerate(items):
        if not isinstance(d, dict):
            raise ValueError(f"{p}: rules[{i}] がオブジェクトではありません")
        known = {f for f in Rule.__dataclass_fields__}
        extra = set(d) - known
        if extra:
            # ★綴り間違いを黙って無視すると「規則を書いたのに閉じない」になる。
            raise ValueError(f"{p}: rules[{i}] に知らない項目があります: {sorted(extra)}")
        out.append(Rule(**d))
    if replace:
        return out
    mine = {r.name for r in out}
    return [d for d in DEFAULT_RULES if d.name not in mine] + out


def dump_rules(rules: list[Rule], path: str | None = None) -> str:
    p = path or rules_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"rules": [asdict(r) for r in rules]}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return p


# --------------------------------------------------------------------------- #
# Win32(ここだけが OS 依存。判断は上の純粋関数が持つ)
# --------------------------------------------------------------------------- #
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
BM_GETCHECK = 0x00F0
BM_SETCHECK = 0x00F1
BM_CLICK = 0x00F5
BST_CHECKED = 1
BN_CLICKED = 0
GWL_STYLE = -16
BS_TYPEMASK = 0x0F
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001
# ★状態を持つボタン(押すと「選ばれた」になるもの)。押しただけで満足せず、
#   本当に選ばれたかを読み返す必要がある種類。
_TOGGLE_STYLES = frozenset((
    0x02,   # BS_CHECKBOX
    0x03,   # BS_AUTOCHECKBOX
    0x04,   # BS_RADIOBUTTON
    0x05,   # BS_3STATE
    0x06,   # BS_AUTO3STATE
    0x09,   # BS_AUTORADIOBUTTON
))
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def is_toggle(style: int) -> bool:
    """ラジオ / チェックボックスか。★純粋関数。"""
    return (style & BS_TYPEMASK) in _TOGGLE_STYLES


def resolve_handles(children, clicks: list[str]) -> list[tuple[str, int]]:
    """押す対象の子ウィンドウを決める。★純粋関数。

    children は (handle, text, class_name) の並び。

    ★**BUTTON クラスだけ**を見る。ここで絞らないと、同じ文言の
      STATIC(説明文やラベル)に当たる。STATIC に BM_CLICK を送っても
      何も起きないので、「押したことになっているのに選ばれていない」まま
      次の OK を押してしまう(2026-10-24 にこれで既定の選択肢が通った)。
    ★全部そろわなければ例外。半端に押さない。
    """
    buttons = [(h, t) for h, t, c in children if (c or "").lower() == "button"]
    out: list[tuple[str, int]] = []
    missing: list[str] = []
    for label in clicks:
        hit = next((h for h, t in buttons if t == label), None)
        if hit is None:
            missing.append(label)
        else:
            out.append((label, hit))
    if missing:
        raise RuntimeError(
            f"ボタンが見つかりません: {missing} / "
            f"ある BUTTON: {[t for _, t in buttons]}(何も押しませんでした)")
    return out


def _win32():
    if not sys.platform.startswith("win"):
        raise RuntimeError("Windows 専用です(ポップアップ監視)")
    import ctypes                       # noqa: WPS433
    from ctypes import wintypes         # noqa: WPS433
    return ctypes, wintypes


def _text(ctypes, user32, hwnd, fn_len, fn_get) -> str:
    n = fn_len(hwnd)
    buf = ctypes.create_unicode_buffer(n + 2)
    fn_get(hwnd, buf, n + 2)
    return buf.value


def _exe_of(ctypes, wintypes, pid: int) -> str:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        k32.CloseHandle(h)


def _enum_children(ctypes, wintypes, user32, hwnd) -> list[tuple[int, str, str]]:
    """子ウィンドウを (handle, text, class) で全部返す。"""
    out: list[tuple[int, str, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _child(child, _lp):
        cls = _text(ctypes, user32, child, lambda h: 255, user32.GetClassNameW)
        txt = _text(ctypes, user32, child, user32.GetWindowTextLengthW, user32.GetWindowTextW)
        out.append((int(child), txt, cls))
        return True

    user32.EnumChildWindows(hwnd, _child, 0)
    return out


def _user32(ctypes, wintypes):
    """argtypes を明示した user32。

    ★既定のまま呼ぶと引数が C の int に落ちる。ハンドルや wParam が
      黙って切り詰められても例外は出ず、「送ったのに効かない」になる。
    """
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                               wintypes.WPARAM, wintypes.LPARAM]
    u.SendMessageW.restype = wintypes.LPARAM
    u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    u.GetWindowLongW.restype = ctypes.c_long
    u.GetDlgCtrlID.argtypes = [wintypes.HWND]
    u.GetDlgCtrlID.restype = ctypes.c_int
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                               wintypes.WPARAM, wintypes.LPARAM]
    u.PostMessageW.restype = wintypes.BOOL
    u.IsWindow.argtypes = [wintypes.HWND]
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    return u


def list_windows(*, with_buttons: bool = True) -> list[Win]:
    """見えているトップレベルウィンドウを全部返す。"""
    ctypes, wintypes = _win32()
    user32 = _user32(ctypes, wintypes)
    out: list[Win] = []

    def _buttons(hwnd) -> tuple[str, ...]:
        # ★BUTTON だけ。押す側(resolve_handles)と同じ絞り方にしておかないと、
        #   「一覧に出た文言が押せない」というずれが起きる。
        return tuple(t for _h, t, c in _enum_children(ctypes, wintypes, user32, hwnd)
                     if (c or "").lower() == "button" and t)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _text(ctypes, user32, hwnd, user32.GetWindowTextLengthW, user32.GetWindowTextW)
        cls = _text(ctypes, user32, hwnd, lambda h: 255, user32.GetClassNameW)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append(Win(hwnd=int(hwnd), title=title, class_name=cls, pid=int(pid.value),
                       exe=_exe_of(ctypes, wintypes, int(pid.value)),
                       buttons=_buttons(hwnd) if with_buttons else ()))
        return True

    user32.EnumWindows(_cb, 0)
    return out


def inspect_buttons(hwnd: int, *, all_children: bool = False) -> list[dict]:
    """その窓の子を、種類と選択状態つきで返す(採取と診断に使う)。

    ★all_children=True は「同じ文言の STATIC が居ないか」を見るため。
      居ると、BUTTON で絞らない実装はそちらを押してしまう(2026-10-24)。
    """
    ctypes, wintypes = _win32()
    u = _user32(ctypes, wintypes)
    out = []
    for h, t, c in _enum_children(ctypes, wintypes, u, hwnd):
        is_btn = (c or "").lower() == "button"
        if not is_btn:
            if all_children:
                out.append({"text": t, "class": c, "kind": "その他", "checked": None})
            continue
        style = u.GetWindowLongW(h, GWL_STYLE)
        tog = is_toggle(style)
        out.append({"text": t, "class": c,
                    "kind": "選択式" if tog else "押しボタン",
                    "checked": bool(u.SendMessageW(h, BM_GETCHECK, 0, 0) == BST_CHECKED)
                    if tog else None})
    return out


def _press_methods(ctypes, wintypes, u):
    """ボタンを押す手。上から順に試す。

    ★BM_CLICK が効かない窓がある(2026-10-24 の JV-Link セットアップ2枚目)。
      «送ったら押せたはず» は成り立たない。効いたかを確かめ、駄目なら次の手に
      移る。どの手で通ったかはログに残す — 次の窓の手掛かりになる。
    """
    def _bm_click(dlg, btn, cid):
        u.SendMessageW(btn, BM_CLICK, 0, 0)

    def _wm_command_post(dlg, btn, cid):
        u.PostMessageW(dlg, WM_COMMAND, (BN_CLICKED << 16) | (cid & 0xFFFF), btn)

    def _wm_command_send(dlg, btn, cid):
        u.SendMessageW(dlg, WM_COMMAND, (BN_CLICKED << 16) | (cid & 0xFFFF), btn)

    def _mouse(dlg, btn, cid):
        # ★本物のクリックに一番近い。ボタンの中心に押下→解放を投げる。
        r = wintypes.RECT()
        u.GetClientRect(btn, ctypes.byref(r))
        x, y = (r.right - r.left) // 2, (r.bottom - r.top) // 2
        pos = (y << 16) | (x & 0xFFFF)
        u.PostMessageW(btn, WM_LBUTTONDOWN, MK_LBUTTON, pos)
        time.sleep(0.05)
        u.PostMessageW(btn, WM_LBUTTONUP, 0, pos)

    return (("BM_CLICK", _bm_click),
            ("WM_COMMAND(post)", _wm_command_post),
            ("WM_COMMAND(send)", _wm_command_send),
            ("mouse", _mouse))


def press_until(methods, verify, label: str, *, wait: float = 1.5,
                now=time.monotonic, sleep=time.sleep) -> str:
    """効くまで手を変えて押す。効いた手の名前を返す。駄目なら例外。★純粋な段取り。

    methods は (名前, 押す関数) の並び。verify() が True になったら成功。
    ★結果が出るまで少し待つ。窓が消えるのも、ラジオが点くのも即座ではない。
    ★どれも効かなければ例外。「押したつもり」を成功と書かない。
    """
    tried: list[str] = []
    for name, fn in methods:
        fn()
        tried.append(name)
        deadline = now() + wait
        while now() < deadline:
            if verify():
                return name
            sleep(0.1)
    raise RuntimeError(f"{label!r} を押せませんでした(試した手: {tried})")


def _press(ctypes, wintypes, u, dlg: int, btn: int, label: str, verify,
           *, wait: float = 1.5) -> str:
    cid = u.GetDlgCtrlID(btn)
    methods = [(name, (lambda f=fn: f(dlg, btn, cid)))
               for name, fn in _press_methods(ctypes, wintypes, u)]
    return press_until(methods, verify, label, wait=wait)


def close_window(w: Win, clicks: list[str] | None, *, expect_gone: bool = True) -> str:
    """指定されたボタンを**この順に**押す。空なら WM_CLOSE。何をしたかを返す。

    ★押しっぱなしにしない。毎回**結果を確かめる**:
        ラジオ/チェック → 本当に選択状態になったか(BM_GETCHECK)
        最後のボタン    → 窓が本当に消えたか(expect_gone)
      ここを省いたせいで、2026-10-24 に «click[OK] と記録したのに
      ダイアログは残ったまま» を2度繰り返した。嘘のログが一番高くつく。
    ★途中で確かめられなくなったら、そこで例外。以降のボタンは押さない。
    """
    ctypes, wintypes = _win32()
    u = _user32(ctypes, wintypes)

    def _gone() -> bool:
        return not (u.IsWindow(w.hwnd) and u.IsWindowVisible(w.hwnd))

    if not clicks:
        u.PostMessageW(w.hwnd, WM_CLOSE, 0, 0)
        if expect_gone:
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline and not _gone():
                time.sleep(0.1)
            if not _gone():
                raise RuntimeError("WM_CLOSE を送りましたが窓が残っています")
        return "WM_CLOSE"

    # ★先に全部の在処を確かめてから押す(半端な状態で残さない)。
    targets = resolve_handles(_enum_children(ctypes, wintypes, u, w.hwnd), clicks)

    steps: list[str] = []
    for i, (label, h) in enumerate(targets):
        last = i == len(targets) - 1
        style = u.GetWindowLongW(h, GWL_STYLE)
        if is_toggle(style):
            how = _press(ctypes, wintypes, u, w.hwnd, h, label,
                         lambda: u.SendMessageW(h, BM_GETCHECK, 0, 0) == BST_CHECKED)
            steps.append(f"{label}=選択済({how})")
            continue
        if last and expect_gone:
            how = _press(ctypes, wintypes, u, w.hwnd, h, label, _gone)
            steps.append(f"{label}=窓が閉じた({how})")
            continue
        # ★確かめようがない押しボタン。押したとだけ記録する(成功とは書かない)。
        u.SendMessageW(h, BM_CLICK, 0, 0)
        time.sleep(0.2)
        steps.append(f"{label}=押下(未確認)")
    return "click[" + " → ".join(steps) + "]"


# --------------------------------------------------------------------------- #
# 監視ループ
# --------------------------------------------------------------------------- #
def _log(line: str, path: str | None) -> None:
    stamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    msg = f"{stamp} {line}"
    print(msg, flush=True)
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except OSError:
            pass          # ★ログが書けなくても見張りは続ける


def watch(*, rules: list[Rule], interval: float = 3.0, close: bool = False,
          log_path: str | None = None, once: bool = False,
          lister=None, sleep=time.sleep, closer=None) -> int:
    """新しく現れたウィンドウを記録し、許可したものだけ閉じる。

    ★`close=False` が既定。まず**何が出るか**を一日ぶん記録してから、
      規則を書いて有効にする。見ないで閉じにいくのが一番危ない。
    戻り値は閉じた件数(`once` 用)。
    """
    # ★既定値で束縛しない。def 時に固まると、呼び出し側が差し替えられない
    #   (試験からも、別の採り方からも)。
    lister = lister or list_windows
    closer = closer or close_window
    seen: set[tuple[int, str]] = set()
    closed = 0
    enabled = [r for r in rules if r.enabled]
    _log(f"popup-guard 開始: 規則 {len(enabled)}/{len(rules)} 件有効 / "
         f"{'閉じます' if close else '記録のみ(閉じません)'} / {interval:.0f}秒ごと", log_path)
    while True:
        try:
            wins = lister()
        except Exception as e:                       # noqa: BLE001
            _log(f"✗ ウィンドウ一覧に失敗: {e}", log_path)
            wins = []
        for w in wins:
            key = (w.hwnd, w.title)
            if key in seen:
                continue
            seen.add(key)
            if not w.title.strip():
                continue
            r, notes = choose(w, rules)
            tag = f"[{r.name}]" if r else ("[当たるが押せない]" if notes else "[規則なし]")
            _log(f"窓 {tag} title={w.title!r} class={w.class_name!r} "
                 f"exe={os.path.basename(w.exe)!r} buttons={list(w.buttons)}", log_path)
            for n in notes:
                _log(f"  ・{n}", log_path)
            if r is None:
                continue
            if not close:
                _log(f"  → 閉じる対象ですが、記録のみの設定なので触りません({r.name})", log_path)
                continue
            btn = pick_clicks(w, r)
            try:
                how = closer(w, btn, expect_gone=r.expect_gone)
                closed += 1
                _log(f"  → 片付けました({r.name}, {how})", log_path)
            except Exception as e:                   # noqa: BLE001
                _log(f"  ✗ 閉じられませんでした({r.name}): {e}", log_path)
        # ★消えた窓は忘れる。同じ hwnd が再利用されても、次に出たら記録したい。
        live = {(w.hwnd, w.title) for w in wins}
        seen &= live
        if once:
            return closed
        sleep(interval)
