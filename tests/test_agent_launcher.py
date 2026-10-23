"""Windows のランチャ。

★自動ログオンを入れると「ログオン時」のタスクが**2回**発火する:
  起動時の自動ログオン と、運用者の RDP ログオン(同じユーザ、別セッション)。
  agent が2本になると、同じ待ち行列からジョブを奪い合い、
  **同じレースを2回買う**(2026-10-17 に親2本で実際に起きた)。
"""

from __future__ import annotations

from pathlib import Path

_PS1 = (Path(__file__).resolve().parents[1]
        / "scripts" / "run-agent-windows.ps1").read_text(encoding="utf-8")


def test_launcher_refuses_a_second_agent():
    assert "agent already running" in _PS1
    assert "--server\\s+windows" in _PS1 or "--server\\\\s+windows" in _PS1
    assert "exit 0" in _PS1


def test_the_check_excludes_itself():
    """★自分自身を数えると、永久に起動できない。"""
    assert "$_.ProcessId -ne $me" in _PS1


def test_it_explains_why():
    """『なぜ駄目か』が書いていないと、次の人が握りつぶす。"""
    assert "buy the same race twice" in _PS1


def test_the_file_stays_ascii():
    """★PowerShell 5.1 は BOM 無し .ps1 を CP932 として読む。
       非 ASCII を入れると文字化けして構文が壊れる。
    """
    raw = (Path(__file__).resolve().parents[1]
           / "scripts" / "run-agent-windows.ps1").read_bytes()
    assert all(b < 128 for b in raw), "非 ASCII が混ざっている"


def test_it_still_documents_the_session_zero_constraint():
    """★サービス化できない理由。消すと、また Session 0 を試す人が出る。"""
    assert "INTERACTIVE desktop" in _PS1
    assert "session-0" in _PS1


# --------------------------------------------------------------------------- #
# ★git pull で機械の設定を壊さない
# --------------------------------------------------------------------------- #
def test_hro_home_is_only_a_fallback():
    """★2026-10-24: git pull でこのファイルが戻り、HRO_HOME が既定値(C:\\hro)に
       化けて agent が起動しなくなった。実体は Documents\\hro。
       既に設定されている値を上書きしない。
    """
    assert "if (-not $env:HRO_HOME) { $env:HRO_HOME = 'C:\\hro' }" in _PS1
    assert "\n$env:HRO_HOME = " not in _PS1, "無条件に代入している"


def test_credentials_never_overwrite_existing_values():
    """★同じ pull で POSTGRES_PASSWORD も CHANGE_ME に戻っていた。

    このファイルは git 管理下なので、機械固有の値を置く場所ではない。
    """
    assert "function Set-IfEmpty" in _PS1
    assert "$value -eq 'CHANGE_ME'" in _PS1, "CHANGE_ME を書き込んでいる"
    for name in ("POSTGRES_HOST", "POSTGRES_USER", "POSTGRES_SSLMODE"):
        assert f"$env:{name}      =" not in _PS1 and f"$env:{name}  =" not in _PS1, name
    assert "POSTGRES_PASSWORD" not in _PS1, "パスワードを雛形に残さない"


def test_it_points_at_env_file_for_real_values():
    assert ".env" in _PS1 and "NOT here" in _PS1


def test_the_venv_error_names_hro_home():
    """★『venv が無い』だけでは、パスが間違っていることに気づけない。"""
    assert "HRO_HOME = $env:HRO_HOME" in _PS1
    assert "setx /M HRO_HOME" in _PS1
