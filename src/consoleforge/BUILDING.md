# Membuat ConsoleForge.exe

**Cara termudah (tanpa Windows lokal):** push repo ke GitHub, lalu `git tag v0.3.0 && git push --tags`.
Workflow `.github/workflows/build.yml` menjalankan tes di Windows+Linux, membangun exe, installer, dan
mengunggah `ConsoleForge-portable.zip` + `ConsoleForge-Setup-x.y.z.exe` ke Releases.

**Lokal di Windows:** `build_windows.bat` -> `dist\ConsoleForge\ConsoleForge.exe`
(GUI tanpa konsol) dan `consoleforge-cli.exe` (CLI). Installer: `iscc installer.iss`.

Catatan:
- Mode onedir (folder) dipilih, bukan onefile: start lebih cepat & lebih jarang dianggap antivirus.
- `pywebview` terpasang -> jendela native (WebView2). Tidak terpasang -> buka di browser default.
- Pengaturan: `%APPDATA%\ConsoleForge\settings.json` (key Wii TIDAK pernah disimpan). Crash: `crash.log` di folder itu.
- wii_keys.txt diletakkan di sebelah ConsoleForge.exe.
- Exe belum ditandatangani: Windows SmartScreen akan memperingatkan sampai ada code-signing certificate.
- Rilis baru: ubah `version.py` dan `installer.iss`, commit, tag.
