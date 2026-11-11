; Установщик «Сосны» 1.3.8 — Inno Setup 6
; Сборка: ISCC.exe sosna_setup.iss
#define MyAppName "Сосна"
#define MyAppVersion "1.3.10"
#define SrcDir "C:\Users\Love\AppData\Local\Programs\Sosna"

[Setup]
AppId={{6F5A3D2E-1B4C-4E8A-9F0D-2C7B8A1E5D3F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
DefaultDirName={localappdata}\Programs\Sosna
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=C:\Users\Love\Desktop\Сосна 1.3.8
OutputBaseFilename=Sosna-Setup-1.3.10
SetupIconFile={#SrcDir}\sosna.ico
Compression=lzma2
SolidCompression=yes
DiskSliceSize=2147483647
WizardStyle=modern
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\sosna.ico
CloseApplications=no
RestartApplications=no

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion; Excludes: "__pycache__\*,*.pyc,unins000.*,config.json,history.json,glossary.json,models\installed.json,models\nllb-ct2\.cache\*,_*.py,*.log"

[Icons]
Name: "{userdesktop}\Сосна"; Filename: "{app}\runtime\Sosna.exe"; Parameters: """{app}\main.py"""; WorkingDir: "{app}"; IconFilename: "{app}\sosna.ico"; Comment: "Сосна — офлайн-переводчик фрагмента экрана"
Name: "{userstartup}\Сосна"; Filename: "{app}\runtime\Sosna.exe"; Parameters: """{app}\main.py"""; WorkingDir: "{app}"; Comment: "Сосна — автозапуск"
Name: "{group}\Сосна"; Filename: "{app}\runtime\Sosna.exe"; Parameters: """{app}\main.py"""; WorkingDir: "{app}"; IconFilename: "{app}\sosna.ico"
Name: "{group}\Настройки Сосны"; Filename: "{app}\runtime\Sosna.exe"; Parameters: """{app}\main.py"""
Name: "{group}\Удалить Сосну"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\runtime\Sosna.exe"; Parameters: """{app}\main.py"""; WorkingDir: "{app}"; Description: "Запустить Сосну"; Flags: nowait postinstall skipifsilent

[Messages]
WelcomeLabel2=Это установит Сосну {#MyAppVersion} — офлайн-переводчик фрагмента экрана (Windows OCR + нейросети NLLB/Opus-MT, всё работает локально).%n%nВсе три базы переводов включены в установщик (~2.3 ГБ). После установки приложение добавится в автозапуск и появится в трее (значок 🌲).
FinishedLabelNoIcons=Сосна установлена!%n%nПриложение уже запущено — ищите значок 🌲 в трее. Горячие клавиши: Ctrl+Alt+T (фрагмент), Ctrl+Alt+F (весь экран), Ctrl+Alt+L (живые субтитры), Ctrl+Alt+R (повтор).
