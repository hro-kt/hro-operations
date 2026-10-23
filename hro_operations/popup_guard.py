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
BM_CLICK = 0x00F5
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


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


def list_windows(*, with_buttons: bool = True) -> list[Win]:
    """見えているトップレベルウィンドウを全部返す。"""
    ctypes, wintypes = _win32()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    out: list[Win] = []

    def _buttons(hwnd) -> tuple[str, ...]:
        names: list[str] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _child(child, _lp):
            cls = _text(ctypes, user32, child, lambda h: 255, user32.GetClassNameW)
            if cls.lower() == "button":
                t = _text(ctypes, user32, child, user32.GetWindowTextLengthW,
                          user32.GetWindowTextW)
                if t:
                    names.append(t)
            return True

        user32.EnumChildWindows(hwnd, _child, 0)
        return tuple(names)

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


def close_window(w: Win, clicks: list[str] | None) -> str:
    """指定されたボタンを**この順に**押す。空なら WM_CLOSE。何をしたかを返す。

    ★ラジオも BUTTON クラスなので BM_CLICK で選べる。JV-Link のセットアップは
      「ラジオを選ぶ → OK」の2手で、順番を逆にすると意味が変わる。
    """
    ctypes, wintypes = _win32()
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    def _find(label: str) -> int | None:
        found: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _child(child, _lp):
            t = _text(ctypes, user32, child, user32.GetWindowTextLengthW, user32.GetWindowTextW)
            if t == label:
                found.append(int(child))
                return False
            return True

        user32.EnumChildWindows(w.hwnd, _child, 0)
        return found[0] if found else None

    if clicks:
        # ★先に全部の在処を確かめてから押す。途中で見つからずに止まると、
        #   «ラジオは選んだが OK は押していない» のような半端な状態で残る。
        handles = [(label, _find(label)) for label in clicks]
        missing = [label for label, h in handles if h is None]
        if missing:
            raise RuntimeError(f"ボタンが見つかりません: {missing}(何も押しませんでした)")
        for label, h in handles:
            user32.SendMessageW(h, BM_CLICK, 0, 0)
            time.sleep(0.2)      # ラジオの選択が効いてから次へ
        return "click[" + " → ".join(clicks) + "]"
    user32.PostMessageW(w.hwnd, WM_CLOSE, 0, 0)
    return "WM_CLOSE"


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
