; 视频下载助手 Windows 安装包（Inno Setup 6）
; CI 用法: ISCC.exe /DMyAppVersion=1.0.0.0 installer\installer.iss
; 本地用法: 先跑 build.py 产出 dist\视频下载助手\，再执行上一行

#define MyAppName "视频下载助手"
#define MyAppPublisher "家庭自用"
#ifndef MyAppVersion
#define MyAppVersion "1.0.0.0"
#endif
#define MyAppExeName "视频下载助手.exe"

[Setup]
AppId={{8E4F2A91-6C3D-4B8E-9A25-1D7C0B3F5E42}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=installer_out
OutputBaseFilename=视频下载助手-{#MyAppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
; 产物 exe 已带版本信息与图标

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}";

[Files]
Source: "..\dist\视频下载助手\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 用户数据（配置/缓存/下载的视频）默认留在安装目录旁，卸载不删；如需彻底清理取消下行注释
; Type: filesandordirs; Name: "{app}"
