; RootMind NSIS installer hooks (bundle.windows.nsis.installerHooks in
; tauri.conf.json), inserted into Tauri's NSIS template.
;
; The installer is per user and never elevates. The app starts its bundled
; backend from $INSTDIR\backend, so before files are replaced or removed both
; the app and that backend must be closed. Restart Manager matches processes
; by path: the background service, which runs from
; %ProgramFiles%\RootMind Service, is never touched here.
;
; The bundled backend is a folder of about 900 files. Before an install or
; an uninstall the whole $INSTDIR\backend folder is removed: copying the new
; version over the old one would leave files that are no longer shipped (an
; old version's metadata among them, which would then report the old version).
;
; Uninstalling also offers to remove the background service, which outlives
; the app. That needs administrator approval and runs the service's own copy
; from Program Files. It is not offered when an installer of a newer version
; runs this uninstaller to replace the app, nor in silent uninstalls (/S, as
; package managers run them); `rootmind-backend.exe service status` then warns
; about a service that is left.

!include "LogicLib.nsh"

; Close the app first: its exit handler stops the backend, and while it runs,
; its crash restart would start the backend again. Then close a backend that
; is still shutting down or was left behind.
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

!macro NSIS_HOOK_PREINSTALL
  !insertmacro ROOTMIND_CLOSE_APP_AND_BACKEND
  !insertmacro ROOTMIND_REMOVE_BACKEND
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro ROOTMIND_CLOSE_APP_AND_BACKEND
  ; Keep the service when this uninstall is part of an update or reinstall:
  ; - the updater runs it with /UPDATE;
  ; - the installer of another version runs it in place (_?=$INSTDIR), so it
  ;   runs from $INSTDIR. A normal uninstall (Settings > Apps) runs from a
  ;   temporary copy instead.
  ${If} $UpdateMode <> 1
  ${AndIf} $EXEDIR != $INSTDIR
    !insertmacro ROOTMIND_OFFER_SERVICE_REMOVAL
  ${EndIf}
  !insertmacro ROOTMIND_REMOVE_BACKEND
!macroend

; Ask whether to remove the background service, and run its Program Files
; exe elevated with "service uninstall". Only interactive uninstalls ask.
!macro ROOTMIND_OFFER_SERVICE_REMOVAL
  Push $R0
  ClearErrors
  ReadRegStr $R0 HKLM "SYSTEM\CurrentControlSet\Services\RootMind-Backend" "ImagePath"
  ${If} ${Errors}
    StrCpy $R0 ""
  ${EndIf}
  !insertmacro ROOTMIND_OUR_SERVICE_EXE $R0
  Pop $R0
  ${If} $R0 != ""
  ${AndIf} ${FileExists} $R0
  ${AndIfNot} ${Silent}
  ${AndIf} $PassiveMode <> 1
    ${If} ${Cmd} `MessageBox MB_YESNO|MB_ICONQUESTION "Also remove the RootMind background service? (administrator approval required)" /SD IDNO IDYES`
      ClearErrors
      ExecShellWait "runas" $R0 "service uninstall" SW_HIDE
      ${If} ${Errors}
        DetailPrint "The RootMind background service was not removed."
      ${EndIf}
    ${EndIf}
  ${EndIf}
  Pop $R0
!macroend

; Push the executable named by a RootMind-Backend ImagePath when it is
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
