 
# Copyright (c) 2026, QuantumLoopAI
# Modified for EMMA Receptionist by QuantumLoopAI.

"""The import-time version banner is suppressed for the emma/pc CLI."""

import sys

import emma


def test_banner_suppressed_for_cli_programs(monkeypatch):
    for prog in ("emma", "pc", "/usr/local/bin/emma", "emma.exe"):
        monkeypatch.setattr(sys, "argv", [prog])
        assert emma._should_log_version_banner() is False


def test_banner_shown_for_bots_and_library_use(monkeypatch):
    for prog in ("bot.py", "python", "-c", "pytest", "/path/to/myapp"):
        monkeypatch.setattr(sys, "argv", [prog])
        assert emma._should_log_version_banner() is True