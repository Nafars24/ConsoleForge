@echo off
REM Build lokal di Windows. Butuh Python 3.9+ di PATH.
python -m pip install --upgrade pyinstaller pywebview lz4 || goto :err
python -m PyInstaller --noconfirm consoleforge.spec || goto :err
echo.
echo Selesai: dist\ConsoleForge\ConsoleForge.exe
echo (Opsional) installer: iscc installer.iss
exit /b 0
:err
echo GAGAL & exit /b 1
