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
