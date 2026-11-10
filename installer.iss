; dogani 설치 파일 (Inno Setup 6). build.bat 이 dist\dogani 를 만든 뒤 iscc 로 컴파일 → dist\dogani-setup.exe
; 사용자 단위 설치(관리자 권한 없음). 설정·데이터(%APPDATA%\Dogani)는 제거해도 남김
#ifndef AppVer
  #define AppVer "dev"
#endif

[Setup]
AppId={{6F4D2B1E-7C3A-4E8F-9B21-D0A9C5E3F781}
AppName=dogani
AppVersion={#AppVer}
AppVerName=dogani {#AppVer}
AppPublisher=dogani
DefaultDirName={localappdata}\Programs\dogani
DefaultGroupName=dogani
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=dogani-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\dogani.exe
UninstallDisplayName=dogani
; 실행 중이면(같은 mutex) 닫으라고 안내
AppMutex=DoganiMutex
CloseApplications=yes

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "autostart"; Description: "Windows 시작 시 자동 실행"
Name: "desktopicon"; Description: "바탕화면 바로가기"; Flags: unchecked

[Files]
Source: "dist\dogani\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 예전 빌드의 남은 파일이 섞이지 않게 _internal 은 매번 새로
[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{userprograms}\dogani"; Filename: "{app}\dogani.exe"
Name: "{userdesktop}\dogani"; Filename: "{app}\dogani.exe"; Tasks: desktopicon

[Registry]
; 앱의 트레이 '자동 실행' 과 같은 값(HKCU Run\Dogani)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Dogani"; ValueData: """{app}\dogani.exe"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\dogani.exe"; Description: "dogani 실행"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/im dogani.exe /f"; Flags: runhidden; RunOnceId: "killdogani"
