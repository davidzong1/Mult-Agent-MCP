"""
Multi-Agent MCP — Team Manager TUI compatibility entrypoint.

The implementation was split into tui_screens.py and helper modules. This file
keeps the historical `python team_manger.py` entrypoint and public imports.
"""
from __future__ import annotations

from tui.tui_screens import *  # noqa: F401,F403
from tui.tui_screens import run_team_manager_app


if __name__ == "__main__":
    run_team_manager_app()
