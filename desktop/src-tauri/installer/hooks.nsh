; RootMind NSIS installer hooks (bundle.windows.nsis.installerHooks in
; tauri.conf.json), inserted into the installer template installer.nsi
; (bundle.windows.nsis.template).
;
; Since 2.0 RootMind always runs its backend as a Windows service, and the
; installer is per machine (installMode perMachine: Program Files, one UAC
; prompt). The installer sets the service up when it can, its uninstaller
; removes it:
;
; - The install folder is always $PROGRAMFILES64\RootMind (the template has
;   no folder page). The service is set up from the backend in that folder,
;   so it must be a folder only administrators can change; any other $INSTDIR
;   (/D=, or a changed registry value) is refused before anything is copied
;   (exit code 10, as "service install" refuses an unsafe program folder).
;   This is the only refusal: everything below installs the app.
; - The service reads only what one Windows account may read, and its token
;   is readable only by that account, so it is set up for the account that
;   runs the installer, and only when that is clearly the account that uses
;   RootMind: a user account (not SYSTEM or another service account) that is
;   signed in to this Windows session, or that runs the installer in a
;   session without a signed-in user (session 0: SSH, a CI runner). The
;   service step is skipped, and the app installed all the same, when the
;   installer runs as SYSTEM or another service account (Intune,
;   Configuration Manager, winget's validation machines, which install
;   silently) or as another account than the one signed in (an
;   administrator approving the prompt for a standard account, "over the
;   shoulder"). The app then says that the service is not set up yet and
;   sets it up for the signed-in account (Set up, one administrator
;   approval). Exception: an update run as SYSTEM updates an existing
;   service, for the account it is registered for ("service install" allows
;   LocalSystem nothing else).
; - A RootMind 1.x installation for this account only (per user, in
;   %LOCALAPPDATA%) is uninstalled silently, as the signed-in account and not
;   elevated, and only when the signed-in account runs the installer: it never
;   runs as SYSTEM or in another account's profile. Its silent uninstall keeps
;   the background service, which this installer then updates.
; - After the files are in place, $INSTDIR\backend\rootmind-backend.exe
;   "service install" installs or updates the services, with MCP over HTTP on
;   for a new install (127.0.0.1 only, bearer token). Free ports are chosen
;   for a new install; an update keeps the ports, the MCP setting, the tokens
;   and the data in %ProgramData%\RootMind. The services run from their own
;   admin-only copy under %ProgramFiles%\RootMind Service\ (the reasons are
;   in backend/src/rootmind/service/install.py), so nothing here has to stop
;   them before files are replaced.
; - The installer exits with 0 once the app is installed, also when the
;   service step was skipped or failed: deployment tools and winget see a
;   successful install, and the app shows what is wrong and offers Set up or
;   Repair. The outcome is recorded in the app's uninstall entry (HKLM, so
;   admin-only, and removed with the app): RootMindServiceSetup = installed,
;   skipped or failed, RootMindServiceSetupReason = why (no secrets), and
;   RootMindServiceSetupCode = the exit code of "service install" when it
;   failed. The installer's details list has the same text and the command's
;   output. When an account is signed in to the installer's session and the
;   installer is not silent, a message says what happened.
; - Uninstalling runs "service uninstall" with the service's own copy, and
;   with --purge when "Delete the application data" is checked, which then
;   also deletes %ProgramData%\RootMind (index, settings, MCP token). An
;   uninstall that is part of an update (/UPDATE) keeps the service.
;
; Installing over an installed version (older, newer or the same) replaces it
; in place without asking: the template has no reinstall page and never runs
; the installed version's uninstaller, so app data, shortcuts and the service
; stay. The bundled backend is a folder of about 900 files. Before an install
; or an uninstall the whole $INSTDIR\backend folder is removed: copying the new
; version over the old one would leave files that are no longer shipped (an
; old version's metadata among them, which would then report the old version).
; Programs that run from it (an MCP client's "rootmind-backend.exe mcp") are
; closed first. Restart Manager matches processes by path, so the service's
; own copy is never touched here.
;
; Messages are in English only: this file must stay ASCII
; (scripts/tests/test_nsis_template.py).

!include "LogicLib.nsh"

!define ROOTMIND_SERVICES_KEY "SYSTEM\CurrentControlSet\Services"
!define ROOTMIND_V1_UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\RootMind"

; --- registers -----------------------------------------------------------------------------

; The macros below use $0-$9 and $R0-$R9 freely; these keep the template's values.
!macro ROOTMIND_SAVE_REGISTERS
  Push $0
  Push $1
  Push $2
  Push $3
  Push $4
  Push $5
  Push $6
  Push $7
  Push $8
  Push $9
  Push $R0
  Push $R1
  Push $R2
  Push $R3
  Push $R4
  Push $R5
  Push $R6
  Push $R7
  Push $R8
  Push $R9
!macroend

!macro ROOTMIND_RESTORE_REGISTERS
  Pop $R9
  Pop $R8
  Pop $R7
  Pop $R6
  Pop $R5
  Pop $R4
  Pop $R3
  Pop $R2
  Pop $R1
  Pop $R0
  Pop $9
  Pop $8
  Pop $7
  Pop $6
  Pop $5
  Pop $4
  Pop $3
  Pop $2
  Pop $1
  Pop $0
!macroend

; --- our service's program ----------------------------------------------------------------

; Push the executable named by a RootMind service's ImagePath when it is
; "<Program Files>\RootMind Service\<folder>\rootmind-backend.exe" (the copy
; the service installer made), else push "". The path is the text between the
; leading quote and the next one. String comparisons are case-insensitive.
!macro ROOTMIND_OUR_SERVICE_EXE imagePath
  Push $R0
  Push $R1
  Push $R2
  Push $R3
  Push $R4
  Push $R5
  StrCpy $R0 "${imagePath}"

  ; $R1 = the quoted path.
  StrCpy $R1 ""
  StrCpy $R2 $R0 1
  ${If} $R2 == '"'
    StrLen $R3 $R0
    StrCpy $R4 1
    ${DoWhile} $R4 < $R3
      StrCpy $R2 $R0 1 $R4
      ${If} $R2 == '"'
        IntOp $R5 $R4 - 1
        ${If} $R5 > 0
          StrCpy $R1 $R0 $R5 1
        ${EndIf}
        ${Break}
      ${EndIf}
      IntOp $R4 $R4 + 1
    ${Loop}
  ${EndIf}

  ; $R5 = 1 when the path has the expected prefix and file name, and the
  ; folder between them is one plain name.
  StrCpy $R5 0
  ${If} $R1 != ""
    StrCpy $R2 "$PROGRAMFILES64\RootMind Service\"
    StrLen $R3 $R2
    StrCpy $R4 $R1 $R3
    ${If} $R4 == $R2
      StrCpy $R4 $R1 "" -21
      ${If} $R4 == "\rootmind-backend.exe"
        ; $R4 = the folder: what lies between prefix and file name.
        StrLen $R4 $R1
        IntOp $R4 $R4 - $R3
        IntOp $R4 $R4 - 21
        ${If} $R4 > 0
          StrCpy $R4 $R1 $R4 $R3
          ${If} $R4 != "."
          ${AndIf} $R4 != ".."
            StrCpy $R5 1
            ; No separator inside the folder name.
            StrLen $R3 $R4
            StrCpy $R2 0
            ${DoWhile} $R2 < $R3
              StrCpy $R0 $R4 1 $R2
              ${If} $R0 == "\"
              ${OrIf} $R0 == "/"
                StrCpy $R5 0
                ${Break}
              ${EndIf}
              IntOp $R2 $R2 + 1
            ${Loop}
          ${EndIf}
        ${EndIf}
      ${EndIf}
    ${EndIf}
  ${EndIf}

  ${If} $R5 <> 1
    StrCpy $R1 ""
  ${EndIf}
  ; Restore the registers and leave the result on the stack.
  StrCpy $R0 $R1
  Pop $R5
  Pop $R4
  Pop $R3
  Pop $R2
  Pop $R1
  Exch $R0
!macroend

; --- accounts ------------------------------------------------------------------------------

; $R9 = the string SID of the account named $R8 ("DOMAIN\user"), or "".
!macro ROOTMIND_SID_OF_NAME
  StrCpy $R9 ""
  ${If} $R8 != ""
    ; SECURITY_MAX_SID_SIZE
    System::Alloc 68
    Pop $0
    System::Call 'advapi32::LookupAccountNameW(p 0, w R8, p r0, *i 68, w .r1, *i ${NSIS_MAX_STRLEN}, *i .r2) i .r3'
    ${If} $3 <> 0
      System::Call 'advapi32::ConvertSidToStringSidW(p r0, *p .r4) i .r3'
      ${If} $3 <> 0
        System::Call 'kernel32::lstrcpynW(w .R9, p r4, i ${NSIS_MAX_STRLEN})'
        System::Call 'kernel32::LocalFree(p r4)'
      ${EndIf}
    ${EndIf}
    System::Free $0
  ${EndIf}
!macroend

; The account this installer runs as and the account signed in to its Windows
; session:
;   $R4 = "DOMAIN\user" of this process (GetUserNameEx, SAM-compatible)
;   $R5 = the SID of this process's account, from its token, or ""
;   $R2 = that account's name for messages ("NT AUTHORITY\SYSTEM" for
;         LocalSystem, which GetUserNameEx names after the computer account)
;   $R6 = "DOMAIN\user" of the session's user, or "" when the session has no
;         user (session 0: a service, a deployment tool, SSH)
;   $R7 = its SID, or ""
;   $R3 = 1 when a session user is known and is not this account, else 0
!macro ROOTMIND_ACCOUNTS
  StrCpy $R4 ""
  System::Call 'secur32::GetUserNameExW(i 2, w .r0, *i ${NSIS_MAX_STRLEN}) i .r1'
  ${If} $1 <> 0
    StrCpy $R4 $0
  ${EndIf}

  ; TOKEN_USER (TokenUser = 1): a pointer to the SID, then the SID itself.
  StrCpy $R5 ""
  StrCpy $R2 $R4
  System::Call 'kernel32::GetCurrentProcess() p .r0'
  ; TOKEN_QUERY
  System::Call 'advapi32::OpenProcessToken(p r0, i 8, *p .r1) i .r2'
  ${If} $2 <> 0
    System::Alloc 256
    Pop $3
    System::Call 'advapi32::GetTokenInformation(p r1, i 1, p r3, i 256, *i .r4) i .r2'
    ${If} $2 <> 0
      System::Call '*$3(p .r4)'
      System::Call 'advapi32::ConvertSidToStringSidW(p r4, *p .r5) i .r2'
      ${If} $2 <> 0
        System::Call 'kernel32::lstrcpynW(w .R5, p r5, i ${NSIS_MAX_STRLEN})'
        System::Call 'kernel32::LocalFree(p r5)'
      ${EndIf}
      System::Call 'advapi32::LookupAccountSidW(p 0, p r4, w .r6, *i ${NSIS_MAX_STRLEN}, w .r7, *i ${NSIS_MAX_STRLEN}, *i .r8) i .r2'
      ${If} $2 <> 0
      ${AndIf} $6 != ""
        StrCpy $R2 $6
        ${If} $7 != ""
          StrCpy $R2 "$7\$6"
        ${EndIf}
      ${EndIf}
    ${EndIf}
    System::Free $3
    System::Call 'kernel32::CloseHandle(p r1)'
  ${EndIf}

  StrCpy $R6 ""
  System::Call 'kernel32::GetCurrentProcessId() i .r0'
  System::Call 'kernel32::ProcessIdToSessionId(i r0, *i .r1) i .r2'
  ${If} $2 <> 0
    ; WTSUserName (5), WTSDomainName (7)
    System::Call 'wtsapi32::WTSQuerySessionInformationW(p 0, i r1, i 5, *p .r3, *i .r4) i .r2'
    ${If} $2 <> 0
      System::Call 'kernel32::lstrcpynW(w .r5, p r3, i ${NSIS_MAX_STRLEN})'
      System::Call 'wtsapi32::WTSFreeMemory(p r3)'
      ${If} $5 != ""
        StrCpy $R6 $5
        System::Call 'wtsapi32::WTSQuerySessionInformationW(p 0, i r1, i 7, *p .r3, *i .r4) i .r2'
        ${If} $2 <> 0
          System::Call 'kernel32::lstrcpynW(w .r6, p r3, i ${NSIS_MAX_STRLEN})'
          System::Call 'wtsapi32::WTSFreeMemory(p r3)'
          ${If} $6 != ""
            StrCpy $R6 "$6\$5"
          ${EndIf}
        ${EndIf}
      ${EndIf}
    ${EndIf}
  ${EndIf}
  StrCpy $R8 $R6
  !insertmacro ROOTMIND_SID_OF_NAME
  StrCpy $R7 $R9

  ; String comparisons are case-insensitive. SIDs decide when both are
  ; known; otherwise the names do (an unknown name differs).
  StrCpy $R3 0
  ${If} $R6 != ""
    ${If} $R5 != ""
    ${AndIf} $R7 != ""
      ${If} $R5 != $R7
        StrCpy $R3 1
      ${EndIf}
    ${ElseIf} $R4 != $R6
      StrCpy $R3 1
    ${EndIf}
  ${EndIf}
!macroend

; Whether "service install" can run, and for which account (see the top of
; this file). Sets the registers of ROOTMIND_ACCOUNTS, and:
;   $R0 = "" when it can run, else why it is skipped (one sentence, no secrets)
;   $R1 = its account option: "--user-sid <SID>" for a user account, or ""
;         when LocalSystem updates the existing service (the backend keeps the
;         service's registered account)
;   $9  = 1 when the account signed in to this session runs the installer, so
;         a step may run as that account (RootMind 1.x), else 0
!macro ROOTMIND_SERVICE_CONTEXT
  !insertmacro ROOTMIND_ACCOUNTS
  StrCpy $R0 ""
  StrCpy $R1 ""
  StrCpy $9 0
  ; A user account: local or domain (S-1-5-21-...) or Microsoft Entra ID
  ; (S-1-12-1-...). SYSTEM, LOCAL SERVICE, NETWORK SERVICE and virtual
  ; service accounts are not.
  StrCpy $8 0
  StrCpy $0 $R5 9
  ${If} $0 == "S-1-5-21-"
  ${OrIf} $0 == "S-1-12-1-"
    StrCpy $8 1
  ${EndIf}
  ${If} $R5 == ""
    StrCpy $R0 "The Windows account this installer runs as could not be determined."
  ${ElseIf} $8 <> 1
    ; LocalSystem may update our existing service (for its own account).
    StrCpy $0 ""
    ${If} $R5 == "S-1-5-18"
      ReadRegStr $1 HKLM "${ROOTMIND_SERVICES_KEY}\RootMind-Backend" "ImagePath"
      !insertmacro ROOTMIND_OUR_SERVICE_EXE $1
      Pop $0
    ${EndIf}
    ${If} $0 == ""
      StrCpy $R0 "The installer runs as $R2, a system or service account (as deployment tools and package managers run installers), not as the Windows account that uses RootMind."
    ${EndIf}
  ${ElseIf} $R3 = 1
    StrCpy $R0 "The installer runs as $R4, but $R6 is signed in to Windows; the service is set up for the account that uses RootMind."
  ${Else}
    StrCpy $R1 "--user-sid $R5"
    ${If} $R6 != ""
      StrCpy $9 1
    ${EndIf}
  ${EndIf}
!macroend

; --- the install folder ------------------------------------------------------------------

; Stop before anything changes unless $INSTDIR is $PROGRAMFILES64\RootMind
; (case-insensitive; a trailing backslash is ignored). The template has no
; folder page, so only /D= or a changed InstallLocation in HKLM gets here.
!macro ROOTMIND_REQUIRE_PROGRAM_FILES
  Push $0
  Push $1
  StrCpy $0 "$INSTDIR"
  StrCpy $1 $0 1 -1
  ${If} $1 == "\"
    StrCpy $0 $0 -1
  ${EndIf}
  ${If} $0 != "$PROGRAMFILES64\${PRODUCTNAME}"
    StrCpy $0 "RootMind installs only into $PROGRAMFILES64\${PRODUCTNAME}, a folder that only administrators can change: its background service runs from there with system rights. Nothing was changed.$\r$\n$\r$\nRun the installer again without choosing another folder (no /D=)."
    DetailPrint "$0"
    MessageBox MB_OK|MB_ICONSTOP "$0" /SD IDOK
    SetErrorLevel 10
    Pop $1
    Pop $0
    Abort "RootMind was not installed: another install folder."
  ${EndIf}
  Pop $1
  Pop $0
!macroend

; --- RootMind 1.x (per user) ---------------------------------------------------------------

; Uninstall a RootMind 1.x installation of this account (HKCU), silently and
; as the signed-in account: its uninstaller lies in a folder that account can
; change, so it never runs elevated. Only when the account signed in to this
; session runs the installer: as SYSTEM, HKCU and the "user" RunAsUser picks
; would not be the same account. Waits up to 2 minutes for its uninstall
; entry to disappear.
!macro ROOTMIND_REMOVE_V1
  !insertmacro ROOTMIND_SAVE_REGISTERS
  !insertmacro ROOTMIND_SERVICE_CONTEXT
  ${If} $9 <> 1
    DetailPrint "RootMind 1.x was not looked for: the installer does not run as the account signed in to this session."
  ${Else}
    ReadRegStr $R0 HKCU "${ROOTMIND_V1_UNINSTKEY}" "UninstallString"
    ${If} $R0 != ""
      StrCpy $R1 $R0 1
      ${If} $R1 == '"'
        StrCpy $R1 $R0 "" 1
        StrCpy $R1 $R1 -1
      ${Else}
        StrCpy $R1 $R0
      ${EndIf}
      StrCpy $R2 $R1 "" -14
      ${If} $R2 == "\uninstall.exe"
      ${AndIf} ${FileExists} $R1
        DetailPrint "Removing RootMind 1.x, installed for this account only..."
        nsis_tauri_utils::RunAsUser "$R1" "/S"
        StrCpy $R2 0
        ${Do}
          Sleep 500
          IntOp $R2 $R2 + 1
          ReadRegStr $R0 HKCU "${ROOTMIND_V1_UNINSTKEY}" "UninstallString"
          ${If} $R0 == ""
          ${OrIf} $R2 >= 240
            ${Break}
          ${EndIf}
        ${Loop}
      ${EndIf}
      ${If} $R0 != ""
        DetailPrint "RootMind 1.x is still installed for this account. Uninstall it in Settings > Apps, and keep the background service when asked."
      ${EndIf}
    ${EndIf}
  ${EndIf}
  !insertmacro ROOTMIND_RESTORE_REGISTERS
!macroend

; --- the bundled backend -------------------------------------------------------------------

; Close the app and programs that run from $INSTDIR\backend (an MCP client's
; "rootmind-backend.exe mcp").
!macro ROOTMIND_CLOSE_APP_AND_BACKEND
  !insertmacro CheckIfAppIsRunning "$INSTDIR\${MAINBINARYNAME}.exe" "${PRODUCTNAME}"
  ${If} ${FileExists} "$INSTDIR\backend\rootmind-backend.exe"
    !insertmacro CheckIfAppIsRunning "$INSTDIR\backend\rootmind-backend.exe" "${PRODUCTNAME}"
  ${EndIf}
!macroend

; Remove the bundled backend folder, once it is closed. Only a folder that
; holds the backend executable is removed.
!macro ROOTMIND_REMOVE_BACKEND
  ${If} ${FileExists} "$INSTDIR\backend\rootmind-backend.exe"
    RMDir /r "$INSTDIR\backend"
  ${EndIf}
!macroend

; --- the service ---------------------------------------------------------------------------

; Record the outcome of the service step in the app's uninstall entry, which
; the template has written by now (HKLM for a per-machine install; removed
; with the app). "reason" may name accounts and exit codes, never a secret.
!macro ROOTMIND_RECORD_SERVICE state reason code
  WriteRegStr SHCTX "${UNINSTKEY}" "RootMindServiceSetup" "${state}"
  !if "${reason}" == ""
    DeleteRegValue SHCTX "${UNINSTKEY}" "RootMindServiceSetupReason"
  !else
    WriteRegStr SHCTX "${UNINSTKEY}" "RootMindServiceSetupReason" "${reason}"
  !endif
  !if "${code}" == ""
    DeleteRegValue SHCTX "${UNINSTKEY}" "RootMindServiceSetupCode"
  !else
    WriteRegDWORD SHCTX "${UNINSTKEY}" "RootMindServiceSetupCode" "${code}"
  !endif
!macroend

; Install or update the services, or skip that step (ROOTMIND_SERVICE_CONTEXT).
; The installer exits with 0 either way: the app is installed, and it sets
; the service up or repairs it.
!macro ROOTMIND_INSTALL_SERVICE
  !insertmacro ROOTMIND_SAVE_REGISTERS
  !insertmacro ROOTMIND_SERVICE_CONTEXT
  ${If} $R0 != ""
    StrCpy $R8 "RootMind was installed. Its background service was not set up: $R0$\r$\n$\r$\nThe service is set up from the app: sign in to Windows with the account that uses RootMind (it must be an administrator), open RootMind and choose Set up."
    DetailPrint "$R8"
    !insertmacro ROOTMIND_RECORD_SERVICE "skipped" "$R0" ""
    ; Only someone signed in to this session could answer a message.
    ${IfNot} ${Silent}
    ${AndIf} $R6 != ""
      MessageBox MB_OK|MB_ICONINFORMATION "$R8"
    ${EndIf}
  ${Else}
    StrCpy $R8 '"$INSTDIR\backend\rootmind-backend.exe" service install --port auto --mcp-port auto'
    ${If} $R1 != ""
      StrCpy $R8 "$R8 $R1"
    ${EndIf}
    DetailPrint "Setting up the RootMind background service..."
    nsExec::ExecToLog $R8
    Pop $R1
    ${If} $R1 == "0"
      !insertmacro ROOTMIND_RECORD_SERVICE "installed" "" ""
    ${Else}
      ${If} $R1 == "3"
        StrCpy $R2 "A Windows service named RootMind-Backend or RootMind-MCP-HTTP exists that RootMind did not install. It was not changed."
      ${ElseIf} $R1 == "4"
        StrCpy $R2 "No free port was found for the service."
      ${ElseIf} $R1 == "6"
        StrCpy $R2 "The folder %ProgramData%\RootMind exists but is not protected. Remove or rename it, then choose Set up in RootMind or run the installer again."
      ${ElseIf} $R1 == "7"
        StrCpy $R2 "The service did not start or did not answer."
      ${ElseIf} $R1 == "8"
        StrCpy $R2 "The background service belongs to another Windows account and was not changed. To use RootMind from this account, uninstall RootMind first (Settings > Apps), then install it again from this account."
      ${ElseIf} $R1 == "9"
        StrCpy $R2 "The folder access check for this account could not be set up (for example, its domain cannot be reached)."
      ${ElseIf} $R1 == "10"
        StrCpy $R2 "The install folder $INSTDIR can be changed by accounts other than administrators, so the service was not set up from it."
      ${Else}
        StrCpy $R2 "Unexpected error."
      ${EndIf}
      IntOp $R3 $R1 + 0
      ${If} $R3 = 0
        StrCpy $R3 1
      ${EndIf}
      !insertmacro ROOTMIND_RECORD_SERVICE "failed" "$R2" "$R3"
      StrCpy $R2 "RootMind was installed, but its background service could not be set up (error $R3).$\r$\n$\r$\n$R2$\r$\n$\r$\nThe details are in the list of this installer and in %ProgramData%\RootMind\logs\install.log. After fixing the cause, open RootMind and choose Repair or Set up, or run the installer again."
      DetailPrint "$R2"
      ${IfNot} ${Silent}
      ${AndIf} $R6 != ""
        MessageBox MB_OK|MB_ICONEXCLAMATION "$R2"
      ${EndIf}
    ${EndIf}
  ${EndIf}
  !insertmacro ROOTMIND_RESTORE_REGISTERS
!macroend

; Remove the services with "service uninstall", run from the service's own
; copy under Program Files (checked by ROOTMIND_OUR_SERVICE_EXE), else from
; $INSTDIR\backend. With "Delete the application data" checked, --purge also
; deletes %ProgramData%\RootMind. The uninstaller is elevated (per machine).
!macro ROOTMIND_REMOVE_SERVICE
  !insertmacro ROOTMIND_SAVE_REGISTERS
  ReadRegStr $R2 HKLM "${ROOTMIND_SERVICES_KEY}\RootMind-Backend" "ImagePath"
  ${If} $R2 == ""
    ReadRegStr $R2 HKLM "${ROOTMIND_SERVICES_KEY}\RootMind-MCP-HTTP" "ImagePath"
  ${EndIf}
  !insertmacro ROOTMIND_OUR_SERVICE_EXE $R2
  Pop $R0
  ${If} $R0 != ""
  ${AndIfNot} ${FileExists} $R0
    StrCpy $R0 ""
  ${EndIf}
  ${If} $R0 == ""
  ${AndIf} ${FileExists} "$INSTDIR\backend\rootmind-backend.exe"
    StrCpy $R0 "$INSTDIR\backend\rootmind-backend.exe"
  ${EndIf}
  StrCpy $R1 "service uninstall"
  ${If} $DeleteAppDataCheckboxState = 1
    StrCpy $R1 "service uninstall --purge"
  ${EndIf}
  ; Nothing to do without a service, unless the data is to be deleted.
  ${If} $R2 == ""
  ${AndIf} $DeleteAppDataCheckboxState <> 1
    StrCpy $R0 ""
  ${EndIf}
  ${If} $R0 != ""
    DetailPrint "Removing the RootMind background service..."
    nsExec::ExecToLog '"$R0" $R1'
    Pop $R3
    ${If} $R3 != "0"
      StrCpy $R4 "The RootMind background service could not be removed (error $R3). The details are in the list of this uninstaller. To remove it later, run from an elevated prompt:$\r$\n$\r$\n$\"$R0$\" $R1"
      DetailPrint "$R4"
      MessageBox MB_OK|MB_ICONEXCLAMATION "$R4" /SD IDOK
      IntOp $R5 $R3 + 0
      ${If} $R5 = 0
        StrCpy $R5 1
      ${EndIf}
      SetErrorLevel $R5
    ${EndIf}
  ${EndIf}
  !insertmacro ROOTMIND_RESTORE_REGISTERS
!macroend

; --- hooks ---------------------------------------------------------------------------------

!macro NSIS_HOOK_PREINSTALL
  !insertmacro ROOTMIND_REQUIRE_PROGRAM_FILES
  !insertmacro ROOTMIND_CLOSE_APP_AND_BACKEND
  !insertmacro ROOTMIND_REMOVE_V1
  !insertmacro ROOTMIND_REMOVE_BACKEND
!macroend

!macro NSIS_HOOK_POSTINSTALL
  !insertmacro ROOTMIND_INSTALL_SERVICE
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro ROOTMIND_CLOSE_APP_AND_BACKEND
  ; Keep the service when this uninstall is part of an update: Tauri marks
  ; that with /UPDATE.
  ${If} $UpdateMode <> 1
    !insertmacro ROOTMIND_REMOVE_SERVICE
  ${EndIf}
  !insertmacro ROOTMIND_REMOVE_BACKEND
!macroend
