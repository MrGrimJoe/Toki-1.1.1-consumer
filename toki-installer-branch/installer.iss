; ============================================================================
; installer.iss -- TOKI consumer installer (Inno Setup 6.x)
; ============================================================================
; Lives on the "installer" branch of github.com/MrGrimJoe/Toki-1.1.1-consumer,
; ONLY alongside this file, setup_runtime.ps1, Launch TOKI.bat.template and
; BUILD_INSTRUCTIONS.md -- no app source (.py files, toki_graph_db\, wcl_kg\)
; on this branch at all. At install time, this installer downloads TOKI's
; actual app source fresh from the "main" branch (see DownloadTokiSource
; below) and extracts it into {app} -- so the installer branch is never
; touched by, or a dependency of, the install itself, and every install
; always ships whatever is currently on main.
;
; Installs like a normal Windows tool into Program Files -- NOT a single
; bundled PyInstaller exe. TOKI's actual .py files, its graph DBs
; (toki_graph_db\, wcl_kg\), and an isolated Python 3.12 runtime all land in
; {app}, visible and inspectable, with a real uninstaller.
;
; Custom wizard pages added below:
;   1. Drive/folder access  -> writes config\sandbox_config.json, which
;      extractor.py's get_sandbox_roots() already reads as of TOKI v1.1.1 --
;      no separate patch needed, main branch ships that way already.
;   2. Ollama                -> "install it for me" / "I already have it" /
;      "skip for now".
;   3. Standard [Components] page covers the voice-pipeline opt-in
;      (openwakeword/faster-whisper/sounddevice -- the heaviest, slowest
;      download) without needing a custom page for it.
;   4. Standard [Tasks] page also carries a "Launch TOKI when Windows
;      starts" checkbox, UNCHECKED by default -- opt-in, not silent. Writes
;      a normal HKCU Run-key value (not HKLM, so no extra admin consent
;      beyond what this installer already needs) and Inno removes it
;      automatically on uninstall. This replaces having to separately run
;      the repo's install_autostart.py by hand.
;
; BEFORE COMPILING: just check out the installer branch and open this file --
; nothing from main needs to be present locally. Compiling produces a single
; standalone TokiInstaller.exe; the app source is fetched at install time,
; on the end user's machine, not at compile time.
; ============================================================================

#define MyAppName "TOKI"
#define MyAppVersion "1.1.1"
#define MyAppPublisher "MrMIB"
#define MyAppExeName "Launch TOKI.bat"
#define MyRepoZipUrl "https://codeload.github.com/MrGrimJoe/Toki-1.1.1-consumer/zip/refs/heads/main"
#define MyRepoZipRootFolder "Toki-1.1.1-consumer-main"

[Setup]
AppId={{8F1B1E6E-6C9B-4B6E-9A6E-5F6A2B8C9D10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Normal per-machine Program Files install, like any desktop tool.
; Switch to DefaultDirName={autopf}\{#MyAppName} + PrivilegesRequired=lowest
; if you'd rather it install per-user with no admin prompt at all.
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
PrivilegesRequired=admin
OutputDir=dist
OutputBaseFilename=TokiInstaller
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
; This installer downloads TOKI's source (from GitHub) + Python + pip
; packages + (optionally) Ollama at install time -- it is NOT offline-
; capable as written, and it DOES need internet access even just to fetch
; the app itself now, not only its dependencies.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Components]
Name: "core"; Description: "TOKI core (required)"; Types: full compact custom; Flags: fixed
Name: "voice"; Description: "Voice input (Ctrl+K wake word + offline transcription) -- larger download"; Types: full

[Types]
Name: "full"; Description: "Full install (recommended)"
Name: "compact"; Description: "Compact (no voice input)"
Name: "custom"; Description: "Custom"; Flags: iscustom

[Files]
; Only the installer's own two runtime helpers ship inside the exe --
; everything else (main.py, extractor.py, toki_graph_db\, etc.) is
; downloaded fresh from the "main" branch at install time, see
; DownloadTokiSource in [Code] below.
Source: "setup_runtime.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "Launch TOKI.bat.template"; DestDir: "{app}"; DestName: "Launch TOKI.bat"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"
Name: "startupicon"; Description: "Launch TOKI when Windows starts"; GroupDescription: "Additional icons:"; Flags: unchecked

[Registry]
; Opt-in only -- unchecked by default (see [Tasks] above). Uses the current
; user's Run key (HKCU), not HKLM, so this needs no extra admin consent
; beyond what the installer itself already requires, and it un-registers
; itself automatically on uninstall via uninsdeletevalue -- no separate
; cleanup script needed, unlike install_autostart.py's standalone approach.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "TOKI"; \
    ValueData: """{app}\{#MyAppExeName}"""; \
    Tasks: startupicon; Flags: uninsdeletevalue

[UninstallDelete]
; setup_runtime.ps1 and DownloadTokiSource create these AFTER Inno's own
; file copy step, so Inno doesn't know about them automatically -- clean
; them up explicitly. filesandordirs here also covers everything fetched
; from main (main.py, extractor.py, toki_graph_db\, wcl_kg\, etc.), since
; none of it was installed via [Files] and Inno can't otherwise track it.
Type: filesandordirs; Name: "{app}"
Type: files; Name: "{app}\install_log.txt"

[Run]
; Ollama, only if the user picked "install it for me" on the custom page --
; NOT run silently. Ollama's official Windows installer doesn't publish a
; documented unattended-install flag, so this opens its real installer UI
; for the user to click through once, rather than promising a silent
; install this script can't actually guarantee.
Filename: "{tmp}\OllamaSetup.exe"; Description: "Install Ollama"; \
    Flags: postinstall skipifsilent; Check: ShouldInstallOllama

[Code]
var
  DrivesPage: TInputOptionWizardPage;
  FolderPage: TInputDirWizardPage;
  OllamaPage: TInputOptionWizardPage;
  DriveLetterAt: array[0..25] of Char;
  DriveCount: Integer;

// All three custom pages below use Inno's own documented high-level page
// helpers (CreateInputOptionPage / CreateInputDirPage) rather than hand-
// assembled controls -- these ship with every stock Inno Setup install,
// no third-party .iss include required.

procedure InitializeWizard;
var
  Letter: Char;
  Drive: String;
  i: Integer;
begin
  // ---- Page 1: which drives TOKI may access -------------------------------
  DrivesPage := CreateInputOptionPage(wpSelectComponents,
    'Folder Access', 'Choose which drives TOKI is allowed to read and write.',
    'TOKI never touches anything outside what''s checked below -- no ' +
    'System32, no Program Files, nothing unchecked here. Your Desktop is ' +
    'always included automatically regardless of this page.',
    False, False);   // Exclusive=False -> checkboxes, not radio buttons

  DriveCount := 0;
  for Letter := 'C' to 'Z' do begin
    Drive := Letter + ':\';
    if DirExists(Drive) then begin
      DrivesPage.Add(Drive + '  drive');
      DriveLetterAt[DriveCount] := Letter;
      DriveCount := DriveCount + 1;
    end;
  end;
  // Pre-check D:\ if present, matching TOKI's original default sandbox.
  for i := 0 to DriveCount - 1 do
    if DriveLetterAt[i] = 'D' then
      DrivesPage.Values[i] := True;

  // ---- Page 2: one optional additional folder, anywhere ---------------------
  FolderPage := CreateInputDirPage(DrivesPage.ID,
    'Additional Folder Access', 'Optionally add one more folder outside the drives above.',
    'TOKI will also be allowed to read and write inside this folder. ' +
    'Leave this as-is to skip it.',
    False, '');
  FolderPage.Add('Folder (leave as-is to skip):');
  FolderPage.Values[0] := ExpandConstant('{userdocs}');

  // ---- Page 3: Ollama -------------------------------------------------------
  OllamaPage := CreateInputOptionPage(FolderPage.ID,
    'Ollama (local AI fallback)',
    'TOKI uses a local Ollama model for anything its built-in command router can''t resolve on its own.',
    'Pick one:', True, False);   // Exclusive=True -> radio buttons
  OllamaPage.Add('Download and install Ollama for me (opens its installer)');
  OllamaPage.Add('I already have Ollama installed');
  OllamaPage.Add('Skip for now (TOKI still works for anything its command router covers)');
  OllamaPage.SelectedValueIndex := 0;
end;

function ShouldInstallOllama: Boolean;
begin
  Result := OllamaPage.SelectedValueIndex = 0;
end;

// ---------------------------------------------------------------------------
// Build config\sandbox_config.json from the drive checkboxes + extra folder
// ---------------------------------------------------------------------------
function JsonEscape(S: String): String;
begin
  StringChangeEx(S, '\', '\\', True);
  StringChangeEx(S, '"', '\"', True);
  Result := S;
end;

procedure WriteSandboxConfig;
var
  i: Integer;
  Roots: TStringList;
  Json, Extra, Desktop: String;
begin
  Roots := TStringList.Create;
  try
    for i := 0 to DriveCount - 1 do
      if DrivesPage.Values[i] then
        Roots.Add(DriveLetterAt[i] + ':\');

    // The wizard page's own copy says Desktop is always included
    // regardless of the drive checkboxes -- make that actually true.
    // {userdesktop} is Inno's resolved per-user Desktop constant (follows
    // OneDrive/Group Policy redirection the same way extractor.py's own
    // _resolve_real_desktop_path() does), so this can't drift from what
    // the app itself considers "the real Desktop".
    Desktop := ExpandConstant('{userdesktop}');
    if Roots.IndexOf(Desktop) = -1 then
      Roots.Add(Desktop);

    Extra := Trim(FolderPage.Values[0]);
    if (Extra <> '') and (Extra <> ExpandConstant('{userdocs}')) and DirExists(Extra)
       and (Roots.IndexOf(Extra) = -1) then
      Roots.Add(Extra);

    Json := '{"roots": [';
    for i := 0 to Roots.Count - 1 do begin
      if i > 0 then Json := Json + ', ';
      Json := Json + '"' + JsonEscape(Roots[i]) + '"';
    end;
    Json := Json + ']}';

    ForceDirectories(ExpandConstant('{app}\config'));
    SaveStringToFile(ExpandConstant('{app}\config\sandbox_config.json'), Json, False);
  finally
    Roots.Free;
  end;
end;

// ---------------------------------------------------------------------------
// Download TOKI's actual app source fresh from the "main" branch and
// extract it into {app}. This is what replaces the old local [Files]
// Source: "..\*" -- the installer branch never has main's files checked
// out locally, not even at compile time.
// ---------------------------------------------------------------------------
function DownloadTokiSource: Boolean;
var
  ResultCode: Integer;
  ZipPath, ExtractDir, InnerFolder, PSCommand: String;
begin
  Result := False;
  ZipPath := ExpandConstant('{tmp}\toki_main.zip');
  ExtractDir := ExpandConstant('{tmp}\toki_main_extracted');
  InnerFolder := ExtractDir + '\{#MyRepoZipRootFolder}';

  WizardForm.StatusLabel.Caption := 'Downloading TOKI (main branch) from GitHub...';
  PSCommand :=
    '-NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri ' +
    '''{#MyRepoZipUrl}'' -OutFile ''' + ZipPath + ''' -UseBasicParsing"';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
              PSCommand, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
     or (ResultCode <> 0) or not FileExists(ZipPath) then begin
    MsgBox('Could not download TOKI from GitHub (main branch). Check your ' +
           'internet connection and try again.', mbError, MB_OK);
    Exit;
  end;

  WizardForm.StatusLabel.Caption := 'Extracting TOKI...';
  PSCommand :=
    '-NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path ''' +
    ZipPath + ''' -DestinationPath ''' + ExtractDir + ''' -Force"';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
              PSCommand, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
     or (ResultCode <> 0) or not DirExists(InnerFolder) then begin
    MsgBox('Could not extract TOKI''s downloaded files. If GitHub renamed ' +
           'the default branch away from "main", update MyRepoZipUrl in ' +
           'installer.iss.', mbError, MB_OK);
    Exit;
  end;

  // GitHub's zip nests everything one level down inside
  // Toki-1.1.1-consumer-main\ -- move that folder's contents up into {app}
  // itself (setup_runtime.ps1 and Launch TOKI.bat.template were already
  // placed directly by [Files] above, so this only adds to {app}, never
  // overwrites those two).
  PSCommand :=
    '-NoProfile -ExecutionPolicy Bypass -Command "Copy-Item -Path ''' +
    InnerFolder + '\*'' -Destination ''' + ExpandConstant('{app}') +
    ''' -Recurse -Force"';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
              PSCommand, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
     or (ResultCode <> 0) then begin
    MsgBox('Could not copy TOKI''s files into the install folder.', mbError, MB_OK);
    Exit;
  end;

  DeleteFile(ZipPath);
  DelTree(ExtractDir, True, True, True);
  Result := True;
end;

// ---------------------------------------------------------------------------
// Post-install: fetch main-branch source, write config, build the Python
// runtime, stage Ollama's installer if requested.
// ---------------------------------------------------------------------------
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  IncludeVoiceStr: String;
  PSCommand: String;
begin
  if CurStep = ssPostInstall then begin
    if not DownloadTokiSource then begin
      MsgBox('TOKI could not be downloaded, so setup cannot continue. ' +
             'Nothing further was installed.', mbError, MB_OK);
      Exit;
    end;

    WriteSandboxConfig;

    if WizardIsComponentSelected('voice') then
      IncludeVoiceStr := '$true'
    else
      IncludeVoiceStr := '$false';

    WizardForm.StatusLabel.Caption :=
      'Setting up TOKI''s Python runtime -- this needs an internet ' +
      'connection and can take several minutes...';

    PSCommand :=
      '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\installer\setup_runtime.ps1') +
      '" -InstallDir "' + ExpandConstant('{app}') + '" -IncludeVoice ' + IncludeVoiceStr;

    if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
                 PSCommand, '', SW_SHOW, ewWaitUntilTerminated, ResultCode) then
      MsgBox('Could not start the Python runtime setup script. ' +
             'You can re-run it manually later from:' + #13#10 +
             ExpandConstant('{app}\installer\setup_runtime.ps1'), mbError, MB_OK
      )
    else if ResultCode <> 0 then
      MsgBox('Python runtime setup reported an error (exit code ' + IntToStr(ResultCode) + '). ' +
             'Check ' + ExpandConstant('{app}\install_log.txt') + ' for details.', mbError, MB_OK);

    // Stage Ollama's installer into {tmp} so the [Run] entry above can
    // launch it right after this page, if the user asked for it. Uses a
    // plain PowerShell download rather than the Inno Download Plugin, so
    // this compiles with a stock Inno Setup install -- no extra addon
    // (idp.iss) required.
    if ShouldInstallOllama then begin
      WizardForm.StatusLabel.Caption := 'Downloading Ollama''s installer...';
      PSCommand :=
        '-NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri ' +
        '''https://ollama.com/download/OllamaSetup.exe'' -OutFile ''' +
        ExpandConstant('{tmp}') + '\OllamaSetup.exe'' -UseBasicParsing"';
      if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
                   PSCommand, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
         or (ResultCode <> 0) or not FileExists(ExpandConstant('{tmp}\OllamaSetup.exe')) then
        MsgBox('Could not download Ollama''s installer automatically. ' +
               'Install it yourself from https://ollama.com when convenient.', mbInformation, MB_OK);
    end;
  end;
end;
