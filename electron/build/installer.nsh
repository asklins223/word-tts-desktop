!include "nsDialogs.nsh"
!include "WinMessages.nsh"
!include "LogicLib.nsh"

; INSTALLER DESIGN CONTRACT
; THESIS: this is a small product launch card, not a themed Windows wizard.
; OWN-WORLD: warm paper, deep ink, burnt orange, and a quiet amber signal; no purple, no stock chrome, no decorative dashboard rail.
; STORY: see what is being installed, make one choice at a time, then open the document-to-voice workbench.
; FIRST VIEWPORT: an editorial masthead, one oversized document-to-voice mark, and one clear action share the same calm surface.
; FORM: a typography-led document-to-voice direction edited into an asymmetric launch card; seed 05c65526.
; FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance.

; MUI still supplies the reliable installation engine and the real file progress bar.
; Its page chrome is hidden and replaced by the shell below.
!ifndef MUI_BGCOLOR
  !define MUI_BGCOLOR "F6F1E8"
!endif
!ifndef MUI_TEXTCOLOR
  !define MUI_TEXTCOLOR "23201D"
!endif
!ifndef MUI_INSTFILESPAGE_COLORS
  !define MUI_INSTFILESPAGE_COLORS "23201D F6F1E8"
!endif
!ifndef MUI_INSTFILESPAGE_PROGRESSBAR
  !define MUI_INSTFILESPAGE_PROGRESSBAR "colored"
!endif
!ifndef MUI_INSTFILESPAGE_FINISHHEADER_TEXT
  !define MUI_INSTFILESPAGE_FINISHHEADER_TEXT ""
!endif
!ifndef MUI_INSTFILESPAGE_FINISHHEADER_SUBTEXT
  !define MUI_INSTFILESPAGE_FINISHHEADER_SUBTEXT ""
!endif
!ifndef MUI_INSTFILESPAGE_ABORTHEADER_TEXT
  !define MUI_INSTFILESPAGE_ABORTHEADER_TEXT ""
!endif
!ifndef MUI_INSTFILESPAGE_ABORTHEADER_SUBTEXT
  !define MUI_INSTFILESPAGE_ABORTHEADER_SUBTEXT ""
!endif

; The visual language is paper, ink, burnt orange, and a small amber signal.
!define INSTALLER_LIGHT "F6F1E8"
!define INSTALLER_MID "E8E0D5"
!define INSTALLER_SHADE "746D63"
!define INSTALLER_INK "23201D"
!define INSTALLER_ACCENT "F06445"
!define INSTALLER_SIGNAL "FFC857"
!define INSTALLER_PROGRESS_BASE_WIDTH 416
!define INSTALLER_PROGRESS_BASE_HEIGHT 242

!ifndef GWL_STYLE
  !define GWL_STYLE -16
!endif
!ifndef WS_CAPTION
  !define WS_CAPTION 0x00C00000
!endif
!ifndef HT_CAPTION
  !define HT_CAPTION 2
!endif
!ifndef SWP_FRAMECHANGED
  !define SWP_FRAMECHANGED 0x0020
!endif
!ifndef SWP_NOACTIVATE
  !define SWP_NOACTIVATE 0x0010
!endif
!ifndef SWP_NOZORDER
  !define SWP_NOZORDER 0x0004
!endif
!ifndef BS_FLAT
  !define BS_FLAT 0x8000
!endif

!ifndef BUILD_UNINSTALLER

!include "StrContains.nsh"

Var InstallerFrame
Var InstallerFrameCompact
Var InstallerActiveStep
Var InstallerBrandFont
Var InstallerTitleFont
Var InstallerBodyFont
Var InstallerSmallFont
Var InstallerPixelFont
Var InstallerButtonFont
Var InstallerDirectoryInput
Var InstallerDirectoryBrowse
Var InstallerDirectoryPath
Var InstallerWelcomeAction
Var InstallerWelcomeCancel
Var InstallerModeAction
Var InstallerModeBack
Var InstallerModeCancel
Var InstallerDirectoryAction
Var InstallerDirectoryBack
Var InstallerDirectoryCancel
Var InstallerModeAllUsersControl
Var InstallerModeCurrentUserControl
Var InstallerProgressRoot
Var InstallerProgressCancel
Var InstallerProgressFont
!ifndef HIDE_RUN_AFTER_FINISH
  Var InstallerFinishCheckbox
  Var InstallerFinishAction
!endif
Var InstallerFinishCancel

!macro customInit
  Call InstallerSetWindowChrome
!macroend

Function InstallerEnsureFonts
  ${If} $InstallerBrandFont == 0
    CreateFont $InstallerBrandFont "Microsoft YaHei UI" 11 700
    CreateFont $InstallerTitleFont "Microsoft YaHei UI" 18 700
    CreateFont $InstallerBodyFont "Microsoft YaHei UI" 9 400
    CreateFont $InstallerSmallFont "Microsoft YaHei UI" 8 400
    CreateFont $InstallerPixelFont "Microsoft YaHei UI" 7 700
    CreateFont $InstallerButtonFont "Microsoft YaHei UI" 9 700
    CreateFont $InstallerProgressFont "Microsoft YaHei UI" 9 700
  ${EndIf}
FunctionEnd

Function InstallerSetWindowChrome
  ; Remove the native caption so the installer has a real product frame.
  ; The shell adds explicit text buttons for the two actions an installer needs.
  SendMessage $HWNDPARENT ${WM_SETTEXT} 0 "STR:小猪wordTTS 安装控制台"
  System::Call 'user32::GetWindowLong(p $HWNDPARENT, i ${GWL_STYLE}) i.r0'
  IntOp $1 $0 & 0xFF3FFFFF
  System::Call 'user32::SetWindowLong(p $HWNDPARENT, i ${GWL_STYLE}, i $1) i.r2'
  System::Call 'user32::SetWindowPos(p $HWNDPARENT, p 0, i 0, i 0, i 0, i ${SWP_FRAMECHANGED}|${SWP_NOACTIVATE}|${SWP_NOZORDER})'

  IfFileExists "$SYSDIR\dwmapi.dll" 0 installer_chrome_done
  ; Keep a clean ink border and rounded outer shadow where DWM supports it.
  System::Call "*(i 0x0023201D) p.s"
  Pop $0
  System::Call 'dwmapi::DwmSetWindowAttribute(p $HWNDPARENT, i 34, p $0, i 4) i.r1'
  System::Free $0
  System::Call "*(i 2) p.s"
  Pop $0
  System::Call 'dwmapi::DwmSetWindowAttribute(p $HWNDPARENT, i 33, p $0, i 4) i.r1'
  System::Free $0
installer_chrome_done:
FunctionEnd

Function InstallerCaptionDrag
  Pop $0
  System::Call 'user32::ReleaseCapture()'
  SendMessage $HWNDPARENT ${WM_NCLBUTTONDOWN} ${HT_CAPTION} 0
FunctionEnd

Function InstallerMinimize
  Pop $0
  System::Call 'user32::ShowWindow(p $HWNDPARENT, i 6)'
FunctionEnd

Function InstallerClose
  Pop $0
  SendMessage $HWNDPARENT ${WM_CLOSE} 0 0
FunctionEnd

!macro InstallerCreateLabel HANDLE X Y WIDTH HEIGHT TEXT FONT COLOR BACKGROUND
  ${NSD_CreateLabel} ${X} ${Y} ${WIDTH} ${HEIGHT} "${TEXT}"
  Pop ${HANDLE}
  SendMessage ${HANDLE} ${WM_SETFONT} ${FONT} 0
  SetCtlColors ${HANDLE} "${COLOR}" "${BACKGROUND}"
!macroend

!macro InstallerCreatePanel X Y WIDTH HEIGHT BACKGROUND
  nsDialogs::CreateControl STATIC ${WS_CHILD}|${WS_VISIBLE}|${SS_WHITERECT} 0 ${X} ${Y} ${WIDTH} ${HEIGHT} ""
  Pop $0
  SetCtlColors $0 "" "${BACKGROUND}"
!macroend

!macro InstallerCreateRule HANDLE X Y WIDTH COLOR
  ${NSD_CreateLabel} ${X} ${Y} ${WIDTH} 1u ""
  Pop ${HANDLE}
  SetCtlColors ${HANDLE} "" "${COLOR}"
!macroend

!macro InstallerCreateButton HANDLE X Y WIDTH HEIGHT TEXT FUNCTION BACKGROUND COLOR
  nsDialogs::CreateControl BUTTON ${WS_CHILD}|${WS_VISIBLE}|${WS_TABSTOP}|${BS_PUSHBUTTON}|${BS_FLAT} 0 ${X} ${Y} ${WIDTH} ${HEIGHT} "${TEXT}"
  Pop ${HANDLE}
  SendMessage ${HANDLE} ${WM_SETFONT} $InstallerButtonFont 0
  SetCtlColors ${HANDLE} "${COLOR}" "${BACKGROUND}"
  ${NSD_OnClick} ${HANDLE} ${FUNCTION}
!macroend

; The instfiles page is a real Win32 dialog, so these helpers convert the
; authored baseline into client pixels instead of assuming one DPI forever.
!macro InstallerCreateProgressLabel TEXT Y COLOR BACKGROUND FONT
  IntOp $R3 $5 * ${Y}
  IntOp $R3 $R3 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
  IntOp $R4 $5 * 18
  IntOp $R4 $R4 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
  System::Call 'user32::CreateWindowEx(i 0, t "STATIC", t "${TEXT}", i ${WS_CHILD}|${WS_VISIBLE}|${SS_LEFT}, i $R1, i $R3, i $R2, i $R4, p $0, i 0, p 0, p 0) p.r7'
  SetCtlColors $7 "${COLOR}" "${BACKGROUND}"
  SendMessage $7 ${WM_SETFONT} ${FONT} 0
!macroend

!macro InstallerCreateProgressMarker Y
  IntOp $R3 $5 * ${Y}
  IntOp $R3 $R3 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
  IntOp $R4 $5 * 18
  IntOp $R4 $R4 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
  IntOp $R5 $4 * 3
  IntOp $R5 $R5 / ${INSTALLER_PROGRESS_BASE_WIDTH}
  System::Call 'user32::CreateWindowEx(i 0, t "STATIC", t "", i ${WS_CHILD}|${WS_VISIBLE}|${SS_WHITERECT}, i 0, i $R3, i $R5, i $R4, p $0, i 0, p 0, p 0) p.r9'
  SetCtlColors $9 "" "${INSTALLER_SIGNAL}"
!macroend

!macro InstallerCreateProgressPanel X Y WIDTH HEIGHT BACKGROUND
  IntOp $R6 $4 * ${X}
  IntOp $R6 $R6 / ${INSTALLER_PROGRESS_BASE_WIDTH}
  IntOp $R7 $5 * ${Y}
  IntOp $R7 $R7 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
  IntOp $R8 $4 * ${WIDTH}
  IntOp $R8 $R8 / ${INSTALLER_PROGRESS_BASE_WIDTH}
  IntOp $R9 $5 * ${HEIGHT}
  IntOp $R9 $R9 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
  System::Call 'user32::CreateWindowEx(i 0, t "STATIC", t "", i ${WS_CHILD}|${WS_VISIBLE}|${SS_WHITERECT}, i $R6, i $R7, i $R8, i $R9, p $0, i 0, p 0, p 0) p.r7'
  SetCtlColors $7 "" "${BACKGROUND}"
!macroend

!macro customInstallMode
  ; MultiUser still owns the elevation decision. Its controls are replaced in
  ; the SHOW hook, then kept in sync with our custom radio controls.
  !define MUI_PAGE_CUSTOMFUNCTION_SHOW InstallerInstallModeCreate
!macroend

!macro customWelcomePage
  ; All raw custom pages use this helper, so it is defined after MUI2 has
  ; declared the $mui handles and can safely hide the stock page chrome.
  Function InstallerHideStockChrome
    ShowWindow $mui.Header.Text ${SW_HIDE}
    ShowWindow $mui.Header.SubText ${SW_HIDE}
    ShowWindow $mui.Header.Background ${SW_HIDE}
    ShowWindow $mui.Header.Image ${SW_HIDE}
    ShowWindow $mui.Branding.Background ${SW_HIDE}
    ShowWindow $mui.Branding.Text ${SW_HIDE}
    ShowWindow $mui.Line.Standard ${SW_HIDE}
    ShowWindow $mui.Line.FullWindow ${SW_HIDE}
    ShowWindow $mui.Button.Back ${SW_HIDE}
    ShowWindow $mui.Button.Next ${SW_HIDE}
    ShowWindow $mui.Button.Cancel ${SW_HIDE}
  FunctionEnd

  Function InstallerWelcomeNext
    Pop $0
    SendMessage $mui.Button.Next ${BM_CLICK} 0 0
  FunctionEnd

  Function InstallerGoBack
    Pop $0
    SendMessage $mui.Button.Back ${BM_CLICK} 0 0
  FunctionEnd

  Function InstallerCancel
    Pop $0
    SendMessage $mui.Button.Cancel ${BM_CLICK} 0 0
  FunctionEnd

  Function InstallerBuildFrame
    Call InstallerEnsureFonts
    SetCtlColors $InstallerFrame "${INSTALLER_INK}" "${INSTALLER_LIGHT}"
    ; One paper surface, one editorial rule, and one document-to-voice mark.
    ; The shell intentionally has no vertical step rail or faux dashboard.
    !insertmacro InstallerCreatePanel 0u 0u 315u 193u ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 16u 10u 120u 14u "小猪wordTTS" $InstallerBrandFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 16u 24u 150u 9u "文档配音工作台" $InstallerSmallFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 244u 10u 40u 11u "3.0.1" $InstallerSmallFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateRule $0 16u 31u 283u ${INSTALLER_MID}
    !insertmacro InstallerCreatePanel 16u 30u 58u 3u ${INSTALLER_ACCENT}
    !insertmacro InstallerCreateLabel $0 224u 23u 72u 9u "DOCUMENT / VOICE" $InstallerSmallFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 228u 50u 25u 30u "文" $InstallerTitleFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreatePanel 258u 66u 16u 3u ${INSTALLER_ACCENT}
    !insertmacro InstallerCreateLabel $0 278u 50u 25u 30u "声" $InstallerTitleFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 224u 87u 79u 9u "TEXT IN / AUDIO OUT" $InstallerSmallFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateRule $0 224u 104u 79u ${INSTALLER_MID}

    ; A minimal custom caption replaces the native Windows title bar.
    !insertmacro InstallerCreateButton $1 264u 6u 18u 16u "—" InstallerMinimize ${INSTALLER_LIGHT} ${INSTALLER_INK}
    !insertmacro InstallerCreateButton $1 288u 6u 18u 16u "×" InstallerClose ${INSTALLER_LIGHT} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 148u 10u 88u 11u "DESKTOP INSTALLER" $InstallerSmallFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
    ${NSD_OnClick} $0 InstallerCaptionDrag
  FunctionEnd

  Function InstallerBuildCompactFrame
    Call InstallerEnsureFonts
    SetCtlColors $InstallerFrame "${INSTALLER_INK}" "${INSTALLER_LIGHT}"
    !insertmacro InstallerCreatePanel 0u 0u 300u 140u ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 16u 9u 120u 14u "小猪wordTTS" $InstallerBrandFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 16u 23u 150u 9u "安装程序 · 3.0.1" $InstallerSmallFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateRule $0 16u 31u 268u ${INSTALLER_MID}
    !insertmacro InstallerCreatePanel 16u 30u 52u 3u ${INSTALLER_ACCENT}
    !insertmacro InstallerCreateLabel $0 219u 23u 74u 9u "DOCUMENT / VOICE" $InstallerSmallFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateButton $1 252u 6u 18u 16u "—" InstallerMinimize ${INSTALLER_LIGHT} ${INSTALLER_INK}
    !insertmacro InstallerCreateButton $1 276u 6u 18u 16u "×" InstallerClose ${INSTALLER_LIGHT} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 148u 9u 68u 11u "SETUP" $InstallerSmallFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
    ${NSD_OnClick} $0 InstallerCaptionDrag
  FunctionEnd

  Function InstallerWelcomeCreate
    nsDialogs::Create 1044
    Pop $InstallerFrame
    ${If} $InstallerFrame == error
      Abort
    ${EndIf}
    Call InstallerHideStockChrome
    StrCpy $InstallerActiveStep "1"
    StrCpy $InstallerFrameCompact "0"
    Call InstallerBuildFrame

    !insertmacro InstallerCreateLabel $0 16u 50u 204u 28u "文档，马上开口说话" $InstallerTitleFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 16u 83u 204u 24u "安装小猪wordTTS，把文字转换成配音文件。" $InstallerBodyFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateRule $0 16u 119u 287u ${INSTALLER_MID}
    !insertmacro InstallerCreateLabel $0 16u 130u 136u 11u "准备安装" $InstallerSmallFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 16u 145u 176u 12u "文档配音工作台 / 文档 + 配音" $InstallerBrandFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 247u 130u 48u 11u "01 / 04" $InstallerSmallFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}

    !insertmacro InstallerCreateButton $InstallerWelcomeAction 230u 165u 66u 18u "开始安装" InstallerWelcomeNext ${INSTALLER_ACCENT} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateButton $InstallerWelcomeCancel 174u 165u 48u 18u "取消" InstallerCancel ${INSTALLER_LIGHT} ${INSTALLER_INK}
    ${NSD_SetFocus} $InstallerWelcomeAction
    nsDialogs::Show
  FunctionEnd

  Function InstallerWelcomeLeave
  FunctionEnd

  Page custom InstallerWelcomeCreate InstallerWelcomeLeave
!macroend

!macro customPageAfterChangeDir
  ; The mode page keeps MultiUser's elevation behavior but receives a fully
  ; custom compact composition because MultiUser creates a 1018 dialog.
  Function InstallerInstallModeCreate
    Call InstallerHideStockChrome
    Call InstallerEnsureFonts
    ShowWindow $MultiUser.InstallModePage.Text ${SW_HIDE}
    ShowWindow $MultiUser.InstallModePage.AllUsers ${SW_HIDE}
    ShowWindow $MultiUser.InstallModePage.CurrentUser ${SW_HIDE}
    ShowWindow $RadioButtonLabel1 ${SW_HIDE}
    StrCpy $InstallerFrame $MultiUser.InstallModePage
    StrCpy $InstallerActiveStep "2"
    StrCpy $InstallerFrameCompact "1"
    Call InstallerBuildCompactFrame

    !insertmacro InstallerCreateLabel $0 16u 43u 190u 20u "安装给谁？" $InstallerTitleFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 16u 65u 270u 13u "选择可以启动小猪wordTTS 的 Windows 账户。" $InstallerSmallFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
    ${NSD_CreateRadioButton} 16u 80u 96u 14u "所有用户"
    Pop $InstallerModeAllUsersControl
    SetCtlColors $InstallerModeAllUsersControl "${INSTALLER_INK}" "${INSTALLER_LIGHT}"
    SendMessage $InstallerModeAllUsersControl ${WM_SETFONT} $InstallerBodyFont 0
    ${NSD_OnClick} $InstallerModeAllUsersControl InstallerInstallModeToggle
    ${NSD_CreateRadioButton} 16u 102u 96u 14u "仅当前用户"
    Pop $InstallerModeCurrentUserControl
    SetCtlColors $InstallerModeCurrentUserControl "${INSTALLER_INK}" "${INSTALLER_LIGHT}"
    SendMessage $InstallerModeCurrentUserControl ${WM_SETFONT} $InstallerBodyFont 0
    ${NSD_OnClick} $InstallerModeCurrentUserControl InstallerInstallModeToggle
    SendMessage $MultiUser.InstallModePage.AllUsers ${BM_GETCHECK} 0 0 $0
    ${If} $0 == ${BST_CHECKED}
      ${NSD_Check} $InstallerModeAllUsersControl
    ${Else}
      ${NSD_Check} $InstallerModeCurrentUserControl
    ${EndIf}
    !insertmacro InstallerCreateLabel $0 118u 81u 162u 11u "这台电脑上的每个账户都能启动。" $InstallerSmallFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 118u 103u 162u 11u "设置只保留在当前账户中。" $InstallerSmallFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}

    !insertmacro InstallerCreateButton $InstallerModeAction 230u 119u 56u 16u "继续" InstallerWelcomeNext ${INSTALLER_ACCENT} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateButton $InstallerModeBack 77u 119u 54u 16u "上一步" InstallerGoBack ${INSTALLER_LIGHT} ${INSTALLER_INK}
    !insertmacro InstallerCreateButton $InstallerModeCancel 16u 119u 48u 16u "取消" InstallerCancel ${INSTALLER_LIGHT} ${INSTALLER_INK}
    ${NSD_SetFocus} $InstallerModeAction
  FunctionEnd

  Function InstallerInstallModeToggle
    Pop $0
    ${NSD_GetState} $InstallerModeAllUsersControl $1
    ${If} $1 == ${BST_CHECKED}
      SendMessage $MultiUser.InstallModePage.AllUsers ${BM_SETCHECK} ${BST_CHECKED} 0
      SendMessage $MultiUser.InstallModePage.CurrentUser ${BM_SETCHECK} ${BST_UNCHECKED} 0
    ${Else}
      SendMessage $MultiUser.InstallModePage.AllUsers ${BM_SETCHECK} ${BST_UNCHECKED} 0
      SendMessage $MultiUser.InstallModePage.CurrentUser ${BM_SETCHECK} ${BST_CHECKED} 0
    ${EndIf}
  FunctionEnd

  Function InstallerDirectoryCreate
    ${If} ${isUpdated}
      Abort
    ${EndIf}

    nsDialogs::Create 1044
    Pop $InstallerFrame
    ${If} $InstallerFrame == error
      Abort
    ${EndIf}
    Call InstallerHideStockChrome
    StrCpy $InstallerActiveStep "3"
    StrCpy $InstallerFrameCompact "0"
    Call InstallerBuildFrame

    !insertmacro InstallerCreateLabel $0 16u 50u 204u 25u "选择安装位置" $InstallerTitleFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 16u 81u 204u 21u "程序文件会放到你选择的文件夹。" $InstallerBodyFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateRule $0 16u 109u 287u ${INSTALLER_MID}
    !insertmacro InstallerCreateLabel $0 16u 116u 204u 11u "应用文件夹" $InstallerPixelFont ${INSTALLER_INK} ${INSTALLER_LIGHT}

    ${NSD_CreateDirRequest} 16u 129u 208u 18u "$INSTDIR"
    Pop $InstallerDirectoryInput
    SetCtlColors $InstallerDirectoryInput "${INSTALLER_INK}" "${INSTALLER_LIGHT}"
    SendMessage $InstallerDirectoryInput ${WM_SETFONT} $InstallerBodyFont 0
    ${NSD_OnChange} $InstallerDirectoryInput InstallerDirectoryChanged
    ${NSD_CreateBrowseButton} 232u 129u 64u 18u "选择文件夹"
    Pop $InstallerDirectoryBrowse
    SetCtlColors $InstallerDirectoryBrowse "${INSTALLER_INK}" "${INSTALLER_MID}"
    SendMessage $InstallerDirectoryBrowse ${WM_SETFONT} $InstallerSmallFont 0
    ${NSD_OnClick} $InstallerDirectoryBrowse InstallerDirectoryBrowse

    !insertmacro InstallerCreateLabel $0 16u 151u 260u 11u "将在此文件夹下创建小猪wordTTS。" $InstallerSmallFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}

    !insertmacro InstallerCreateButton $InstallerDirectoryAction 230u 165u 66u 18u "开始写入" InstallerWelcomeNext ${INSTALLER_ACCENT} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateButton $InstallerDirectoryBack 174u 165u 48u 18u "上一步" InstallerGoBack ${INSTALLER_LIGHT} ${INSTALLER_INK}
    !insertmacro InstallerCreateButton $InstallerDirectoryCancel 118u 165u 48u 18u "取消" InstallerCancel ${INSTALLER_LIGHT} ${INSTALLER_INK}
    ${NSD_SetFocus} $InstallerDirectoryInput
    nsDialogs::Show
  FunctionEnd

  Function InstallerDirectoryChanged
    Pop $0
    ${NSD_GetText} $InstallerDirectoryInput $InstallerDirectoryPath
  FunctionEnd

  Function InstallerDirectoryBrowse
    Pop $0
    ${NSD_GetText} $InstallerDirectoryInput $1
    nsDialogs::SelectFolderDialog "选择安装位置" "$1"
    Pop $2
    ${If} $2 != error
      ${NSD_SetText} $InstallerDirectoryInput "$2"
      StrCpy $InstallerDirectoryPath "$2"
    ${EndIf}
  FunctionEnd

  Function InstallerDirectoryLeave
    ${NSD_GetText} $InstallerDirectoryInput $0
    ${If} $0 == ""
      MessageBox MB_OK|MB_ICONEXCLAMATION "请选择安装位置。"
      Abort
    ${EndIf}
    StrCpy $INSTDIR "$0"
    ${StrContains} $1 "${APP_FILENAME}" $INSTDIR
    ${If} $1 == ""
      StrCpy $INSTDIR "$INSTDIR\${APP_FILENAME}"
    ${EndIf}
  FunctionEnd

  Function InstallerInstallFilesCreate
    Call InstallerHideStockChrome
    Call InstallerEnsureFonts
    FindWindow $0 "#32770" "" $HWNDPARENT
    StrCpy $InstallerProgressRoot $0
    SetCtlColors $0 "${INSTALLER_INK}" "${INSTALLER_LIGHT}"

    ; Discover the actual client rectangle so the editorial shell follows DPI.
    System::Call "*(i, i, i, i) p.s"
    Pop $1
    System::Call 'user32::GetClientRect(p $0, p $1)'
    System::Call '*$1(i.r2, i.r3, i.r4, i.r5)'
    System::Free $1
    ; The progress page is the same paper surface as the setup pages. The
    ; actual NSIS status and progress controls remain the source of truth.
    IntOp $R1 $4 * 24
    IntOp $R1 $R1 / ${INSTALLER_PROGRESS_BASE_WIDTH}
    IntOp $8 $4 * 368
    IntOp $8 $8 / ${INSTALLER_PROGRESS_BASE_WIDTH}
    StrCpy $R2 $8
    !insertmacro InstallerCreateProgressLabel "小猪wordTTS" 10 ${INSTALLER_INK} ${INSTALLER_LIGHT} $InstallerBrandFont
    !insertmacro InstallerCreateProgressLabel "安装程序 · 3.0.1" 25 ${INSTALLER_SHADE} ${INSTALLER_LIGHT} $InstallerSmallFont
    !insertmacro InstallerCreateProgressPanel 24 44 84 3 ${INSTALLER_ACCENT}

    ; A single strong mark carries the voice idea without rebuilding a fake
    ; waveform out of a row of colored bars.
    !insertmacro InstallerCreateProgressPanel 370 54 18 58 ${INSTALLER_ACCENT}
    !insertmacro InstallerCreateProgressPanel 394 76 8 36 ${INSTALLER_SIGNAL}

    ; Caption controls use glyphs, not a second stock-looking title bar.
    IntOp $R6 $4 * 22
    IntOp $R6 $R6 / ${INSTALLER_PROGRESS_BASE_WIDTH}
    IntOp $R7 $5 * 18
    IntOp $R7 $R7 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    IntOp $R8 $4 * 354
    IntOp $R8 $R8 / ${INSTALLER_PROGRESS_BASE_WIDTH}
    IntOp $R9 $5 * 6
    IntOp $R9 $R9 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    System::Call 'user32::CreateWindowEx(i 0, t "BUTTON", t "—", i ${WS_CHILD}|${WS_VISIBLE}|${WS_TABSTOP}|${BS_PUSHBUTTON}|${BS_FLAT}, i $R8, i $R9, i $R6, i $R7, p $0, i 0, p 0, p 0) p.r3'
    SetCtlColors $3 "${INSTALLER_INK}" "${INSTALLER_LIGHT}"
    SendMessage $3 ${WM_SETFONT} $InstallerSmallFont 0
    nsDialogs::OnClick $3 InstallerMinimize
    IntOp $R8 $4 * 382
    IntOp $R8 $R8 / ${INSTALLER_PROGRESS_BASE_WIDTH}
    System::Call 'user32::CreateWindowEx(i 0, t "BUTTON", t "×", i ${WS_CHILD}|${WS_VISIBLE}|${WS_TABSTOP}|${BS_PUSHBUTTON}|${BS_FLAT}, i $R8, i $R9, i $R6, i $R7, p $0, i 0, p 0, p 0) p.r3'
    SetCtlColors $3 "${INSTALLER_INK}" "${INSTALLER_LIGHT}"
    SendMessage $3 ${WM_SETFONT} $InstallerSmallFont 0
    nsDialogs::OnClick $3 InstallerClose

    ; Work surface: one title, one sentence, then one live progress signal.
    IntOp $R3 $5 * 54
    IntOp $R3 $R3 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    IntOp $R4 $5 * 28
    IntOp $R4 $R4 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    System::Call 'user32::CreateWindowEx(i 0, t "STATIC", t "正在准备工作台", i ${WS_CHILD}|${WS_VISIBLE}|${SS_LEFT}, i $R1, i $R3, i $R2, i $R4, p $0, i 0, p 0, p 0) p.r2'
    SetCtlColors $2 "${INSTALLER_INK}" "${INSTALLER_LIGHT}"
    SendMessage $2 ${WM_SETFONT} $InstallerTitleFont 0
    IntOp $R3 $5 * 88
    IntOp $R3 $R3 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    IntOp $R4 $5 * 22
    IntOp $R4 $R4 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    System::Call 'user32::CreateWindowEx(i 0, t "STATIC", t "正在把小猪wordTTS 放进电脑，请保持窗口打开。", i ${WS_CHILD}|${WS_VISIBLE}|${SS_LEFT}, i $R1, i $R3, i $R2, i $R4, p $0, i 0, p 0, p 0) p.r2'
    SetCtlColors $2 "${INSTALLER_SHADE}" "${INSTALLER_LIGHT}"
    SendMessage $2 ${WM_SETFONT} $InstallerBodyFont 0
    IntOp $R3 $5 * 126
    IntOp $R3 $R3 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    IntOp $R4 $5 * 18
    IntOp $R4 $R4 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    System::Call 'user32::CreateWindowEx(i 0, t "STATIC", t "安装进度", i ${WS_CHILD}|${WS_VISIBLE}|${SS_LEFT}, i $R1, i $R3, i $R2, i $R4, p $0, i 0, p 0, p 0) p.r2'
    SetCtlColors $2 "${INSTALLER_INK}" "${INSTALLER_LIGHT}"
    SendMessage $2 ${WM_SETFONT} $InstallerSmallFont 0

    ; Keep the actual NSIS status and progress controls, but move them into
    ; the authored work surface and hide the log/show-log affordance.
    GetDlgItem $R2 $0 1006
    GetDlgItem $R3 $0 1004
    GetDlgItem $R4 $0 1027
    GetDlgItem $R5 $0 1016
    ShowWindow $R4 ${SW_HIDE}
    ShowWindow $R5 ${SW_HIDE}
    SetCtlColors $R2 "${INSTALLER_SHADE}" "${INSTALLER_LIGHT}"
    SendMessage $R2 ${WM_SETFONT} $InstallerSmallFont 0
    IntOp $R6 $5 * 150
    IntOp $R6 $R6 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    IntOp $R7 $5 * 20
    IntOp $R7 $R7 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    System::Call 'user32::SetWindowPos(p $R2, p 0, i $R1, i $R6, i $8, i $R7, i ${SWP_NOZORDER}|${SWP_NOACTIVATE})'
    IntOp $R6 $5 * 176
    IntOp $R6 $R6 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    StrCpy $R8 $8
    IntOp $R9 $5 * 18
    IntOp $R9 $R9 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    System::Call 'user32::SetWindowPos(p $R3, p 0, i $R1, i $R6, i $8, i $R9, i ${SWP_NOZORDER}|${SWP_NOACTIVATE})'

    ; Explicit cancel action inside the redesigned surface.
    IntOp $R6 $5 * 205
    IntOp $R6 $R6 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    IntOp $R7 $4 * 316
    IntOp $R7 $R7 / ${INSTALLER_PROGRESS_BASE_WIDTH}
    IntOp $R8 $4 * 76
    IntOp $R8 $R8 / ${INSTALLER_PROGRESS_BASE_WIDTH}
    IntOp $R9 $5 * 22
    IntOp $R9 $R9 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    System::Call 'user32::CreateWindowEx(i 0, t "BUTTON", t "取消安装", i ${WS_CHILD}|${WS_VISIBLE}|${WS_TABSTOP}|${BS_PUSHBUTTON}|${BS_FLAT}, i $R7, i $R6, i $R8, i $R9, p $0, i 0, p 0, p 0) p.r8'
    SetCtlColors $8 "${INSTALLER_INK}" "${INSTALLER_MID}"
    SendMessage $8 ${WM_SETFONT} $InstallerButtonFont 0
    StrCpy $InstallerProgressCancel $8
    nsDialogs::OnClick $8 InstallerCancel
    ShowWindow $mui.Button.Back ${SW_HIDE}
    ShowWindow $mui.Button.Next ${SW_HIDE}
    ShowWindow $mui.Button.Cancel ${SW_HIDE}
  FunctionEnd

  Page custom InstallerDirectoryCreate InstallerDirectoryLeave
  !define MUI_PAGE_CUSTOMFUNCTION_SHOW InstallerInstallFilesCreate
!macroend

!macro customFinishPage
  Function InstallerFinishDone
    Pop $0
    SendMessage $mui.Button.Next ${BM_CLICK} 0 0
  FunctionEnd

  !ifndef HIDE_RUN_AFTER_FINISH
    Function InstallerFinishUpdateAction
      ${NSD_GetState} $InstallerFinishCheckbox $1
      ${If} $1 == ${BST_CHECKED}
        ${NSD_SetText} $InstallerFinishAction "完成并打开"
      ${Else}
        ${NSD_SetText} $InstallerFinishAction "完成"
      ${EndIf}
    FunctionEnd

    Function InstallerFinishToggleOpen
      Pop $0
      Call InstallerFinishUpdateAction
    FunctionEnd
  !endif

  Function InstallerFinishCreate
    nsDialogs::Create 1044
    Pop $InstallerFrame
    ${If} $InstallerFrame == error
      Abort
    ${EndIf}
    Call InstallerHideStockChrome
    StrCpy $InstallerActiveStep "4"
    StrCpy $InstallerFrameCompact "0"
    Call InstallerBuildFrame

    !insertmacro InstallerCreateLabel $0 16u 50u 204u 25u "安装完成" $InstallerTitleFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 16u 82u 204u 20u "小猪wordTTS 已经准备就绪。" $InstallerBodyFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateRule $0 16u 111u 287u ${INSTALLER_MID}
    !insertmacro InstallerCreateLabel $0 16u 119u 120u 11u "安装结果" $InstallerPixelFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreatePanel 16u 132u 287u 24u ${INSTALLER_MID}
    !insertmacro InstallerCreatePanel 26u 141u 5u 5u ${INSTALLER_SIGNAL}
    !insertmacro InstallerCreateLabel $0 40u 138u 38u 12u "完成" $InstallerPixelFont ${INSTALLER_ACCENT} ${INSTALLER_MID}
    !insertmacro InstallerCreateLabel $0 91u 138u 192u 12u "程序文件与快捷方式已准备好" $InstallerSmallFont ${INSTALLER_INK} ${INSTALLER_MID}

    !ifdef HIDE_RUN_AFTER_FINISH
      !insertmacro InstallerCreateLabel $0 16u 159u 196u 12u "现在可以从桌面或开始菜单启动。" $InstallerSmallFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
      !insertmacro InstallerCreateButton $InstallerFinishAction 230u 165u 66u 18u "完成" InstallerFinishDone ${INSTALLER_ACCENT} ${INSTALLER_LIGHT}
    !else
      ${NSD_CreateCheckBox} 16u 159u 196u 13u "完成后打开小猪wordTTS"
      Pop $InstallerFinishCheckbox
      ${NSD_Check} $InstallerFinishCheckbox
      SetCtlColors $InstallerFinishCheckbox "${INSTALLER_INK}" "${INSTALLER_LIGHT}"
      SendMessage $InstallerFinishCheckbox ${WM_SETFONT} $InstallerBodyFont 0
      ${NSD_OnClick} $InstallerFinishCheckbox InstallerFinishToggleOpen
      nsDialogs::CreateControl BUTTON ${WS_CHILD}|${WS_VISIBLE}|${WS_TABSTOP}|${BS_PUSHBUTTON}|${BS_FLAT} 0 230u 165u 66u 18u "完成并打开"
      Pop $InstallerFinishAction
      SendMessage $InstallerFinishAction ${WM_SETFONT} $InstallerButtonFont 0
      SetCtlColors $InstallerFinishAction "${INSTALLER_LIGHT}" "${INSTALLER_ACCENT}"
      ${NSD_OnClick} $InstallerFinishAction InstallerFinishDone
      ${NSD_SetFocus} $InstallerFinishAction
    !endif
    !insertmacro InstallerCreateButton $InstallerFinishCancel 174u 165u 48u 18u "关闭" InstallerCancel ${INSTALLER_LIGHT} ${INSTALLER_INK}
    nsDialogs::Show
  FunctionEnd

  Function InstallerFinishLeave
    !ifndef HIDE_RUN_AFTER_FINISH
      ${NSD_GetState} $InstallerFinishCheckbox $0
      ${If} $0 == ${BST_CHECKED}
        ${if} ${isUpdated}
          StrCpy $1 "--updated"
        ${else}
          StrCpy $1 ""
        ${endif}
        ${StdUtils.ExecShellAsUser} $0 "$launchLink" "open" "$1"
      ${EndIf}
    !endif
  FunctionEnd

  Page custom InstallerFinishCreate InstallerFinishLeave
!macroend

!endif
