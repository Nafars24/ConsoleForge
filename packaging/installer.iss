; Inno Setup 6: iscc installer.iss  (setelah PyInstaller selesai)
#define AppName "ConsoleForge"
#define AppVersion "0.3.0"
[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir=.
OutputBaseFilename=ConsoleForge-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
SetupIconFile=app.ico
[Files]
Source: "dist\ConsoleForge\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\ConsoleForge.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\ConsoleForge.exe"; Tasks: desktopicon
[Tasks]
Name: "desktopicon"; Description: "Shortcut di desktop"; Flags: unchecked
[Run]
Filename: "{app}\ConsoleForge.exe"; Description: "Jalankan {#AppName}"; Flags: nowait postinstall skipifsilent
