; RootMind NSIS installer hooks (used only with tauri.installer.conf.json).
;
; One installer sets up the app AND its background service, with a single UAC
; prompt. installMode "perMachine" (see ../tauri.installer.conf.json) runs the
; whole installer elevated, so the PowerShell calls below inherit that
; elevation and need no second prompt.

!include "LogicLib.nsh"

; Absolute path: a bare "powershell.exe" would be looked up in the installer's
; own folder first (often Downloads), where a planted copy would run elevated.
!define POWERSHELL "$SYSDIR\WindowsPowerShell\v1.0\powershell.exe"

!macro NSIS_HOOK_PREINSTALL
  ; Upgrade path: a running service locks rootmind-backend.exe, and -Install
  ; refuses to run when the service already exists. Stop and remove the old
  ; service first. On a fresh install the script is not there yet, and on
  ; errors we continue anyway; POSTINSTALL reports a real failure.
  ${If} ${FileExists} "$INSTDIR\backend\deploy\windows\_service-backend.ps1"
    DetailPrint "Stopping the existing RootMind background service..."
    nsExec::ExecToLog '"${POWERSHELL}" -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\backend\deploy\windows\_service-backend.ps1" -Stop'
    Pop $0
    nsExec::ExecToLog '"${POWERSHELL}" -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\backend\deploy\windows\_service-backend.ps1" -Uninstall'
    Pop $0
  ${EndIf}
!macroend

!macro NSIS_HOOK_POSTINSTALL
  DetailPrint "Installing the RootMind background service..."
  nsExec::ExecToLog '"${POWERSHELL}" -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\backend\deploy\windows\_service-backend.ps1" -Install'
  Pop $0
  ${If} $0 != 0
    DetailPrint "RootMind service install failed (exit code $0)."
    DetailPrint "You can retry manually: $INSTDIR\backend\deploy\windows\install-backend-service.bat"
    MessageBox MB_OK|MB_ICONEXCLAMATION "RootMind was installed, but the background service could not be set up.$\r$\n$\r$\nRun '$INSTDIR\backend\deploy\windows\install-backend-service.bat' as Administrator."
  ${Else}
    DetailPrint "Starting the RootMind background service..."
    nsExec::ExecToLog '"${POWERSHELL}" -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\backend\deploy\windows\_service-backend.ps1" -Start'
    Pop $0
  ${EndIf}
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  ; Runs before files are removed, so the service script (and the NSSM binary
  ; it needs) is still on disk at this point.
  DetailPrint "Removing the RootMind background service..."
  nsExec::ExecToLog '"${POWERSHELL}" -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\backend\deploy\windows\_service-backend.ps1" -Uninstall'
  Pop $0
!macroend
