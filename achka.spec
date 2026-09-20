# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one self-contained `achka` binary.

    uv run pyinstaller achka.spec --clean --noconfirm   ->  dist/achka

The two agent SDKs each ship their own ~225 MB CLI inside the wheel. Those are
left out by default - the binary finds `claude` / `codex` on PATH instead, and
the codex daemon also honours `codex_bin` in config.toml. Set
AGENTCTL_BUNDLE_CLIS=1 to bundle them anyway (adds roughly half a gigabyte).
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

BUNDLE_CLIS = os.environ.get("AGENTCTL_BUNDLE_CLIS") == "1"
VENDORED_CLI_DIRS = ("claude_agent_sdk/_bundled", "codex_cli_bin")

datas, binaries, hiddenimports = [], [], []
for package in ("flask", "jinja2", "werkzeug", "pydantic", "claude_agent_sdk", "openai_codex"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# mcp.cli needs typer, which is an optional extra we do not install - importing
# it during collection kills the build, so skip that subpackage.
datas += collect_data_files("mcp")
hiddenimports += collect_submodules("mcp", filter=lambda name: not name.startswith("mcp.cli"))

excludes = ["tkinter", "test", "pytest", "IPython", "matplotlib", "numpy"]

if not BUNDLE_CLIS:
    # leave the vendored CLI out whole: half of it (module without its data
    # files) fails at runtime, where a clean ImportError sends people to PATH
    excludes.append("codex_cli_bin")

    def keep(entry):
        source = entry[0].replace(os.sep, "/")
        return not any(vendored in source for vendored in VENDORED_CLI_DIRS)

    datas = [d for d in datas if keep(d)]
    binaries = [b for b in binaries if keep(b)]

# the daemons are reached through importlib, so PyInstaller cannot see them
hiddenimports += ["achka.daemons.claude", "achka.daemons.codex"]

# Jinja templates and the stylesheet are read from disk at runtime, so they
# have to travel with the binary, at the same path inside the package
datas += [
    ("src/achka/templates", "achka/templates"),
    ("src/achka/static", "achka/static"),
]

a = Analysis(
    ["packaging/entry.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="achka",
    console=True,
    upx=False,
    strip=False,
    debug=False,
    bootloader_ignore_signals=False,
    disable_windowed_traceback=False,
)
