# PyInstaller spec: python -m PyInstaller --noconfirm consoleforge.spec
# Hasil (onedir): dist/ConsoleForge/ConsoleForge.exe (GUI, tanpa konsol) + consoleforge-cli.exe (CLI)
import os
icon = "app.ico" if os.path.exists("app.ico") else None
hidden = ["gui_web", "wii", "aes", "flac", "wbfs", "cso", "gcz", "lz4block", "chdforge", "common", "version",
          "lz4.block", "Crypto.Cipher.AES"]            # dua terakhir opsional (dilewati bila tidak terpasang)
a = Analysis(["consoleforge.py"], pathex=["."], datas=[("ui.html", ".")], hiddenimports=hidden,
             excludes=["tkinter", "unittest", "pydoc", "test"], noarchive=False)
pyz = PYZ(a.pure)
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ConsoleForge", console=False, icon=icon, upx=False)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="consoleforge-cli", console=True, icon=icon, upx=False)
coll = COLLECT(gui, cli, a.binaries, a.datas, name="ConsoleForge", upx=False)
