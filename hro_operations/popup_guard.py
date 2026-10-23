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
    """閉じてよいなら、その規則を返す。★純粋関数。

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


def close_window(w: Win, clicks: list[str] | None) -> str:
    """指定されたボタンを**この順に**押す。空なら WM_CLOSE。何をしたかを返す。

    ★ラジオを押したら、**本当に選ばれたかを読み返す**。ここを省くと
      「押したことにはなっているが既定の選択肢のまま OK」になる
      (2026-10-24 に JV-Link のセットアップで実際に起きた)。
      選べなかったら、そこで止める。OK は押さない。
    """
    ctypes, wintypes = _win32()
    u = _user32(ctypes, wintypes)

    if not clicks:
        u.PostMessageW(w.hwnd, WM_CLOSE, 0, 0)
        return "WM_CLOSE"

    # ★先に全部の在処を確かめてから押す(半端な状態で残さない)。
    targets = resolve_handles(_enum_children(ctypes, wintypes, u, w.hwnd), clicks)

    steps: list[str] = []
    for label, h in targets:
        style = u.GetWindowLongW(h, GWL_STYLE)
        u.SendMessageW(h, BM_CLICK, 0, 0)
        time.sleep(0.2)
        if not is_toggle(style):
            steps.append(label)
            continue
        if u.SendMessageW(h, BM_GETCHECK, 0, 0) != BST_CHECKED:
            # ★BM_CLICK を無視する作りの窓がある。状態を直接立て、親に
            #   「押された」と伝える(ダイアログ側はこの通知で選択を覚える)。
            u.SendMessageW(h, BM_SETCHECK, BST_CHECKED, 0)
            cid = u.GetDlgCtrlID(h)
            u.SendMessageW(w.hwnd, WM_COMMAND,
                           (BN_CLICKED << 16) | (cid & 0xFFFF), h)
            time.sleep(0.2)
        if u.SendMessageW(h, BM_GETCHECK, 0, 0) != BST_CHECKED:
            raise RuntimeError(
                f"{label!r} を選べませんでした(押したが選択状態になりません)。"
                f"残り {[c for c, _ in targets[len(steps) + 1:]]} は押していません")
        steps.append(f"{label}=選択済")
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
            r = match(w, rules)
            tag = f"[{r.name}]" if r else "[規則なし]"
            _log(f"窓 {tag} title={w.title!r} class={w.class_name!r} "
                 f"exe={os.path.basename(w.exe)!r} buttons={list(w.buttons)}", log_path)
            if r is None:
                continue
            if not close:
                _log(f"  → 閉じる対象ですが、記録のみの設定なので触りません({r.name})", log_path)
                continue
            btn = pick_clicks(w, r)
            if btn is None:
                # ★半端に押すくらいなら押さない。意図と違う選択肢で先へ進むほうが悪い。
                _log(f"  ✗ {r.name}: 押すボタンが揃いません(clicks={list(r.clicks)} / "
                     f"ある={list(w.buttons)})。何もしません", log_path)
                continue
            try:
                how = closer(w, btn)
                closed += 1
                _log(f"  → 閉じました({r.name}, {how})", log_path)
            except Exception as e:                   # noqa: BLE001
                _log(f"  ✗ 閉じられませんでした({r.name}): {e}", log_path)
        # ★消えた窓は忘れる。同じ hwnd が再利用されても、次に出たら記録したい。
        live = {(w.hwnd, w.title) for w in wins}
        seen &= live
        if once:
            return closed
        sleep(interval)
