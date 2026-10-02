#ifndef PayloadDir
  #error PayloadDir must name the staged desktop client directory
#endif
#ifndef AppVersion
  #error AppVersion must be passed (/DAppVersion=x.y.z) and match installer/version.txt
#endif
#ifndef InstallerOutputDir
  #define InstallerOutputDir "..\dist"
#endif

[Setup]
AppId={{76543D83-1B1F-483D-A5C5-AFD742D7B41F}
AppName=CapsWriter
AppVersion={#AppVersion}
AppVerName=CapsWriter {#AppVersion}
AppPublisher=CapsWriter
DefaultDirName={localappdata}\Programs\CapsWriter
DefaultGroupName=CapsWriter
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#InstallerOutputDir}
OutputBaseFilename=CapsWriter-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\CapsWriterDesktop.exe
UninstallDisplayName=CapsWriter 语音输入
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
VersionInfoDescription=CapsWriter 客户端安装程序
VersionInfoProductVersion={#AppVersion}

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："

; 用户可编辑文件不随升级覆盖（onlyifdoesntexist），卸载时保留
; （uninsneveruninstall）。payload 由 installer/stage_payload.py 组装并
; 校验过：config_client.py / hot*.txt 只存在于根目录，因此这里的全局
; basename 排除不会误删子目录同名文件。
[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Excludes: "config_client.py,hot.txt,hot-server.txt,hot-rule.txt"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadDir}\config_client.py"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "{#PayloadDir}\hot.txt"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "{#PayloadDir}\hot-server.txt"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "{#PayloadDir}\hot-rule.txt"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{group}\CapsWriter"; Filename: "{app}\CapsWriterDesktop.exe"; WorkingDir: "{app}"
Name: "{group}\卸载 CapsWriter"; Filename: "{uninstallexe}"
Name: "{autodesktop}\CapsWriter"; Filename: "{app}\CapsWriterDesktop.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\CapsWriterDesktop.exe"; Description: "启动 CapsWriter"; Flags: nowait postinstall skipifsilent
