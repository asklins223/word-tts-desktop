!include "nsDialogs.nsh"
!include "WinMessages.nsh"
!include "LogicLib.nsh"

; INSTALLER DESIGN CONTRACT
; THESIS: this is a five-step installation console, not a themed Windows wizard.
; OWN-WORLD: a four-tone pixel field, hard grid, inverted active state, and no stock page chrome.
; STORY: choose scope, choose a folder, watch the files land, then launch the desktop workbench.
; FIRST VIEWPORT: an ink-green left rail anchors a full-width work surface with one clear action row.
; FORM: the assigned Game Boy four-shade field translated into a practical desktop installer; seed aa0de03f.
; FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance.

; MUI still supplies the reliable installation engine and the real file progress bar.
; Its page chrome is hidden and replaced by the shell below.
!ifndef MUI_BGCOLOR
  !define MUI_BGCOLOR "9BBC0F"
!endif
!ifndef MUI_TEXTCOLOR
  !define MUI_TEXTCOLOR "0F380F"
!endif
!ifndef MUI_INSTFILESPAGE_COLORS
  !define MUI_INSTFILESPAGE_COLORS "0F380F 9BBC0F"
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

; The four colors are the whole visual language. Active controls invert them.
!define INSTALLER_LIGHT "9BBC0F"
!define INSTALLER_MID "8BAC0F"
!define INSTALLER_SHADE "306230"
!define INSTALLER_INK "0F380F"
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
    CreateFont $InstallerBrandFont "Consolas" 10 700
    CreateFont $InstallerTitleFont "Microsoft YaHei UI" 15 700
    CreateFont $InstallerBodyFont "Microsoft YaHei UI" 9 400
    CreateFont $InstallerSmallFont "Microsoft YaHei UI" 8 400
    CreateFont $InstallerPixelFont "Consolas" 8 700
    CreateFont $InstallerButtonFont "Microsoft YaHei UI" 9 700
    CreateFont $InstallerProgressFont "Consolas" 9 700
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
  System::Call "*(i 0x000F380F) p.s"
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
  ${NSD_CreateButton} ${X} ${Y} ${WIDTH} ${HEIGHT} "${TEXT}"
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
  SetCtlColors $9 "" "${INSTALLER_LIGHT}"
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
    ; Dark rail and a one-unit grid seam replace the stock sidebar image.
    !insertmacro InstallerCreatePanel 0u 0u 88u 193u ${INSTALLER_INK}
    !insertmacro InstallerCreatePanel 88u 0u 1u 193u ${INSTALLER_SHADE}

    ; Brand lockup in the rail.
    !insertmacro InstallerCreateLabel $0 11u 12u 66u 14u "WORDTTS" $InstallerBrandFont ${INSTALLER_LIGHT} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 11u 28u 66u 12u "安装控制台" $InstallerSmallFont ${INSTALLER_MID} ${INSTALLER_INK}

    ; Five concrete stages. The active stage is a reversed tile, not a dot.
    !insertmacro InstallerCreatePanel 0u 49u 3u 18u ${INSTALLER_SHADE}
    !insertmacro InstallerCreatePanel 0u 75u 3u 18u ${INSTALLER_SHADE}
    !insertmacro InstallerCreatePanel 0u 101u 3u 18u ${INSTALLER_SHADE}
    !insertmacro InstallerCreatePanel 0u 127u 3u 18u ${INSTALLER_SHADE}
    !insertmacro InstallerCreatePanel 0u 153u 3u 18u ${INSTALLER_SHADE}

    !insertmacro InstallerCreateLabel $0 11u 51u 20u 12u "01" $InstallerPixelFont ${INSTALLER_MID} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 35u 51u 47u 12u "开始" $InstallerSmallFont ${INSTALLER_MID} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 11u 77u 20u 12u "02" $InstallerPixelFont ${INSTALLER_MID} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 35u 77u 47u 12u "安装范围" $InstallerSmallFont ${INSTALLER_MID} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 11u 103u 20u 12u "03" $InstallerPixelFont ${INSTALLER_MID} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 35u 103u 47u 12u "安装位置" $InstallerSmallFont ${INSTALLER_MID} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 11u 129u 20u 12u "04" $InstallerPixelFont ${INSTALLER_MID} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 35u 129u 47u 12u "写入文件" $InstallerSmallFont ${INSTALLER_MID} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 11u 155u 20u 12u "05" $InstallerPixelFont ${INSTALLER_MID} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 35u 155u 47u 12u "完成" $InstallerSmallFont ${INSTALLER_MID} ${INSTALLER_INK}

    ; Invert only the active tile and rail marker.
    StrCmp $InstallerActiveStep "1" installer_frame_step_1 0
    StrCmp $InstallerActiveStep "2" installer_frame_step_2 0
    StrCmp $InstallerActiveStep "3" installer_frame_step_3 0
    StrCmp $InstallerActiveStep "4" installer_frame_step_4 0
    Goto installer_frame_step_5
installer_frame_step_1:
    !insertmacro InstallerCreatePanel 0u 49u 3u 18u ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 11u 51u 20u 12u "01" $InstallerPixelFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 35u 51u 47u 12u "开始" $InstallerSmallFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    Goto installer_frame_step_done
installer_frame_step_2:
    !insertmacro InstallerCreatePanel 0u 75u 3u 18u ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 11u 77u 20u 12u "02" $InstallerPixelFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 35u 77u 47u 12u "安装范围" $InstallerSmallFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    Goto installer_frame_step_done
installer_frame_step_3:
    !insertmacro InstallerCreatePanel 0u 101u 3u 18u ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 11u 103u 20u 12u "03" $InstallerPixelFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 35u 103u 47u 12u "安装位置" $InstallerSmallFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    Goto installer_frame_step_done
installer_frame_step_4:
    !insertmacro InstallerCreatePanel 0u 127u 3u 18u ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 11u 129u 20u 12u "04" $InstallerPixelFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 35u 129u 47u 12u "写入文件" $InstallerSmallFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    Goto installer_frame_step_done
installer_frame_step_5:
    !insertmacro InstallerCreatePanel 0u 153u 3u 18u ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 11u 155u 20u 12u "05" $InstallerPixelFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 35u 155u 47u 12u "完成" $InstallerSmallFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
installer_frame_step_done:

    ; A real, labeled title strip replaces the native caption bar.
    !insertmacro InstallerCreateLabel $0 89u 0u 226u 16u "小猪wordTTS / 安装程序" $InstallerSmallFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
    ${NSD_OnClick} $0 InstallerCaptionDrag
    !insertmacro InstallerCreateButton $1 246u 2u 32u 12u "最小化" InstallerMinimize ${INSTALLER_LIGHT} ${INSTALLER_SHADE}
    !insertmacro InstallerCreateButton $1 280u 2u 31u 12u "关闭" InstallerClose ${INSTALLER_LIGHT} ${INSTALLER_SHADE}
  FunctionEnd

  Function InstallerBuildCompactFrame
    Call InstallerEnsureFonts
    SetCtlColors $InstallerFrame "${INSTALLER_INK}" "${INSTALLER_LIGHT}"
    !insertmacro InstallerCreatePanel 0u 0u 84u 140u ${INSTALLER_INK}
    !insertmacro InstallerCreatePanel 84u 0u 1u 140u ${INSTALLER_SHADE}
    !insertmacro InstallerCreateLabel $0 10u 8u 64u 13u "WORDTTS" $InstallerBrandFont ${INSTALLER_LIGHT} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 10u 23u 64u 11u "安装控制台" $InstallerSmallFont ${INSTALLER_MID} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 10u 37u 18u 11u "01" $InstallerPixelFont ${INSTALLER_MID} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 33u 37u 45u 11u "开始" $InstallerSmallFont ${INSTALLER_MID} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 10u 56u 18u 11u "02" $InstallerPixelFont ${INSTALLER_MID} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 33u 56u 45u 11u "安装范围" $InstallerSmallFont ${INSTALLER_MID} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 10u 75u 18u 11u "03" $InstallerPixelFont ${INSTALLER_MID} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 33u 75u 45u 11u "安装位置" $InstallerSmallFont ${INSTALLER_MID} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 10u 94u 18u 11u "04" $InstallerPixelFont ${INSTALLER_MID} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 33u 94u 45u 11u "写入文件" $InstallerSmallFont ${INSTALLER_MID} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 10u 113u 18u 11u "05" $InstallerPixelFont ${INSTALLER_MID} ${INSTALLER_INK}
    !insertmacro InstallerCreateLabel $0 33u 113u 45u 11u "完成" $InstallerSmallFont ${INSTALLER_MID} ${INSTALLER_INK}
    StrCmp $InstallerActiveStep "2" installer_compact_active 0
    Goto installer_compact_active_done
installer_compact_active:
    !insertmacro InstallerCreatePanel 0u 56u 3u 14u ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 10u 56u 18u 11u "02" $InstallerPixelFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 33u 56u 45u 11u "安装范围" $InstallerSmallFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
installer_compact_active_done:
    !insertmacro InstallerCreateLabel $0 86u 0u 214u 14u "小猪wordTTS / 安装程序" $InstallerSmallFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
    ${NSD_OnClick} $0 InstallerCaptionDrag
    !insertmacro InstallerCreateButton $1 230u 1u 32u 11u "最小化" InstallerMinimize ${INSTALLER_LIGHT} ${INSTALLER_SHADE}
    !insertmacro InstallerCreateButton $1 266u 1u 30u 11u "关闭" InstallerClose ${INSTALLER_LIGHT} ${INSTALLER_SHADE}
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

    !insertmacro InstallerCreateLabel $0 104u 25u 202u 26u "安装小猪wordTTS" $InstallerTitleFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 104u 55u 202u 28u "把文档配音工作台安装到这台 Windows 电脑。" $InstallerBodyFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateRule $0 104u 87u 202u ${INSTALLER_SHADE}
    !insertmacro InstallerCreateLabel $0 104u 92u 202u 12u "安装流程" $InstallerPixelFont ${INSTALLER_INK} ${INSTALLER_LIGHT}

    !insertmacro InstallerCreatePanel 104u 108u 202u 16u ${INSTALLER_MID}
    !insertmacro InstallerCreateLabel $0 112u 110u 22u 11u "01" $InstallerPixelFont ${INSTALLER_INK} ${INSTALLER_MID}
    !insertmacro InstallerCreateLabel $0 138u 110u 162u 11u "确认安装范围" $InstallerSmallFont ${INSTALLER_INK} ${INSTALLER_MID}
    !insertmacro InstallerCreatePanel 104u 126u 202u 16u ${INSTALLER_MID}
    !insertmacro InstallerCreateLabel $0 112u 128u 22u 11u "02" $InstallerPixelFont ${INSTALLER_INK} ${INSTALLER_MID}
    !insertmacro InstallerCreateLabel $0 138u 128u 162u 11u "选择应用文件夹" $InstallerSmallFont ${INSTALLER_INK} ${INSTALLER_MID}
    !insertmacro InstallerCreatePanel 104u 144u 202u 16u ${INSTALLER_MID}
    !insertmacro InstallerCreateLabel $0 112u 146u 22u 11u "03" $InstallerPixelFont ${INSTALLER_INK} ${INSTALLER_MID}
    !insertmacro InstallerCreateLabel $0 138u 146u 162u 11u "写入文件并创建快捷方式" $InstallerSmallFont ${INSTALLER_INK} ${INSTALLER_MID}

    !insertmacro InstallerCreateButton $InstallerWelcomeAction 235u 171u 69u 16u "开始安装" InstallerWelcomeNext ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateButton $InstallerWelcomeCancel 164u 171u 65u 16u "取消" InstallerCancel ${INSTALLER_LIGHT} ${INSTALLER_INK}
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

    !insertmacro InstallerCreateLabel $0 96u 18u 194u 20u "选择安装范围" $InstallerTitleFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 96u 40u 194u 15u "决定哪些 Windows 账户可以使用它。" $InstallerSmallFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
    ${NSD_CreateRadioButton} 104u 59u 186u 14u "为这台电脑上的所有用户安装"
    Pop $InstallerModeAllUsersControl
    SetCtlColors $InstallerModeAllUsersControl "${INSTALLER_INK}" "${INSTALLER_LIGHT}"
    SendMessage $InstallerModeAllUsersControl ${WM_SETFONT} $InstallerBodyFont 0
    ${NSD_OnClick} $InstallerModeAllUsersControl InstallerInstallModeToggle
    ${NSD_CreateRadioButton} 104u 86u 186u 14u "仅为当前用户安装"
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
    !insertmacro InstallerCreateLabel $0 124u 61u 162u 14u "所有本机账户都可启动此工作台。" $InstallerSmallFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 124u 88u 162u 14u "设置只保留在当前账户中。" $InstallerSmallFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}

    !insertmacro InstallerCreateButton $InstallerModeAction 227u 117u 68u 16u "继续" InstallerWelcomeNext ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateButton $InstallerModeBack 158u 117u 63u 16u "上一步" InstallerGoBack ${INSTALLER_LIGHT} ${INSTALLER_INK}
    !insertmacro InstallerCreateButton $InstallerModeCancel 96u 117u 56u 16u "取消" InstallerCancel ${INSTALLER_LIGHT} ${INSTALLER_INK}
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

    !insertmacro InstallerCreateLabel $0 104u 25u 202u 26u "选择安装位置" $InstallerTitleFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 104u 55u 202u 24u "安装器会把程序文件放到所选文件夹。" $InstallerBodyFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateRule $0 104u 84u 202u ${INSTALLER_SHADE}
    !insertmacro InstallerCreateLabel $0 104u 91u 202u 12u "应用文件夹" $InstallerPixelFont ${INSTALLER_INK} ${INSTALLER_LIGHT}

    ${NSD_CreateDirRequest} 104u 106u 163u 17u "$INSTDIR"
    Pop $InstallerDirectoryInput
    SetCtlColors $InstallerDirectoryInput "${INSTALLER_INK}" "${INSTALLER_LIGHT}"
    SendMessage $InstallerDirectoryInput ${WM_SETFONT} $InstallerBodyFont 0
    ${NSD_OnChange} $InstallerDirectoryInput InstallerDirectoryChanged
    ${NSD_CreateBrowseButton} 270u 106u 35u 17u "选择"
    Pop $InstallerDirectoryBrowse
    SetCtlColors $InstallerDirectoryBrowse "${INSTALLER_INK}" "${INSTALLER_MID}"
    SendMessage $InstallerDirectoryBrowse ${WM_SETFONT} $InstallerSmallFont 0
    ${NSD_OnClick} $InstallerDirectoryBrowse InstallerDirectoryBrowse

    !insertmacro InstallerCreatePanel 104u 130u 202u 27u ${INSTALLER_MID}
    !insertmacro InstallerCreateLabel $0 112u 133u 22u 11u "PATH" $InstallerPixelFont ${INSTALLER_INK} ${INSTALLER_MID}
    !insertmacro InstallerCreateLabel $0 146u 133u 154u 20u "将在此文件夹下创建小猪wordTTS。" $InstallerSmallFont ${INSTALLER_INK} ${INSTALLER_MID}

    !insertmacro InstallerCreateButton $InstallerDirectoryAction 235u 171u 69u 16u "开始写入" InstallerWelcomeNext ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateButton $InstallerDirectoryBack 164u 171u 65u 16u "上一步" InstallerGoBack ${INSTALLER_LIGHT} ${INSTALLER_INK}
    !insertmacro InstallerCreateButton $InstallerDirectoryCancel 104u 171u 54u 16u "取消" InstallerCancel ${INSTALLER_LIGHT} ${INSTALLER_INK}
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

    ; Discover the actual client rectangle so the progress shell follows DPI.
    System::Call "*(i, i, i, i) p.s"
    Pop $1
    System::Call 'user32::GetClientRect(p $0, p $1)'
    System::Call '*$1(i.r2, i.r3, i.r4, i.r5)'
    System::Free $1
    IntOp $6 $4 * 24
    IntOp $6 $6 / 100
    IntOp $7 $4 - $6
    IntOp $7 $7 - 1
    IntOp $R0 $4 * 46
    IntOp $R0 $R0 / ${INSTALLER_PROGRESS_BASE_WIDTH}
    IntOp $8 $7 - $R0

    ; The progress page is not an nsDialogs page, so draw its title strip
    ; directly as Win32 children too. Every dimension is derived from the
    ; authored baseline so the frameless window remains usable at high DPI.
    IntOp $R0 $4 - $6
    IntOp $R9 $5 * 30
    IntOp $R9 $R9 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    System::Call 'user32::CreateWindowEx(i 0, t "STATIC", t "", i ${WS_CHILD}|${WS_VISIBLE}|${SS_WHITERECT}, i $6, i 0, i $R0, i $R9, p $0, i 0, p 0, p 0) p.r1'
    SetCtlColors $1 "" "${INSTALLER_LIGHT}"
    IntOp $R1 $4 * 28
    IntOp $R1 $R1 / ${INSTALLER_PROGRESS_BASE_WIDTH}
    IntOp $R2 $4 * 78
    IntOp $R2 $R2 / ${INSTALLER_PROGRESS_BASE_WIDTH}
    IntOp $R3 $4 - $R1
    IntOp $R3 $R3 - $R2
    IntOp $R4 $5 * 6
    IntOp $R4 $R4 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    IntOp $R5 $5 * 18
    IntOp $R5 $R5 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    System::Call 'user32::CreateWindowEx(i 0, t "STATIC", t "小猪wordTTS / 安装程序", i ${WS_CHILD}|${WS_VISIBLE}|${SS_LEFT}|${SS_NOTIFY}, i $R1, i $R4, i $R3, i $R5, p $0, i 0, p 0, p 0) p.r2'
    SetCtlColors $2 "${INSTALLER_SHADE}" "${INSTALLER_LIGHT}"
    SendMessage $2 ${WM_SETFONT} $InstallerSmallFont 0
    nsDialogs::OnClick $2 InstallerCaptionDrag
    IntOp $R6 $4 * 30
    IntOp $R6 $R6 / ${INSTALLER_PROGRESS_BASE_WIDTH}
    IntOp $R7 $4 * 6
    IntOp $R7 $R7 / ${INSTALLER_PROGRESS_BASE_WIDTH}
    IntOp $R8 $5 * 4
    IntOp $R8 $R8 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    IntOp $R9 $5 * 22
    IntOp $R9 $R9 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    IntOp $R0 $4 - $R6
    IntOp $R0 $R0 - $R7
    IntOp $R1 $R0 - $R6
    IntOp $R1 $R1 - $R7
    System::Call 'user32::CreateWindowEx(i 0, t "BUTTON", t "最小化", i ${WS_CHILD}|${WS_VISIBLE}|${WS_TABSTOP}|${BS_PUSHBUTTON}, i $R1, i $R8, i $R6, i $R9, p $0, i 0, p 0, p 0) p.r3'
    SetCtlColors $3 "${INSTALLER_SHADE}" "${INSTALLER_LIGHT}"
    SendMessage $3 ${WM_SETFONT} $InstallerSmallFont 0
    nsDialogs::OnClick $3 InstallerMinimize
    System::Call 'user32::CreateWindowEx(i 0, t "BUTTON", t "关闭", i ${WS_CHILD}|${WS_VISIBLE}|${WS_TABSTOP}|${BS_PUSHBUTTON}, i $R0, i $R8, i $R6, i $R9, p $0, i 0, p 0, p 0) p.r3'
    SetCtlColors $3 "${INSTALLER_SHADE}" "${INSTALLER_LIGHT}"
    SendMessage $3 ${WM_SETFONT} $InstallerSmallFont 0
    nsDialogs::OnClick $3 InstallerClose

    ; Dark rail.
    System::Call 'user32::CreateWindowEx(i 0, t "STATIC", t "", i ${WS_CHILD}|${WS_VISIBLE}|${SS_WHITERECT}, i 0, i 0, i $6, i $5, p $0, i 0, p 0, p 0) p.r1'
    SetCtlColors $1 "" "${INSTALLER_INK}"
    ; Rail label / stage numbers.
    IntOp $R1 $4 * 14
    IntOp $R1 $R1 / ${INSTALLER_PROGRESS_BASE_WIDTH}
    IntOp $R2 $6 - $R1
    IntOp $R2 $R2 - $R1
    IntOp $R3 $5 * 16
    IntOp $R3 $R3 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    IntOp $R4 $5 * 22
    IntOp $R4 $R4 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    System::Call 'user32::CreateWindowEx(i 0, t "STATIC", t "WORDTTS", i ${WS_CHILD}|${WS_VISIBLE}|${SS_LEFT}, i $R1, i $R3, i $R2, i $R4, p $0, i 0, p 0, p 0) p.r2'
    SetCtlColors $2 "${INSTALLER_LIGHT}" "${INSTALLER_INK}"
    SendMessage $2 ${WM_SETFONT} $InstallerBrandFont 0
    IntOp $R3 $5 * 42
    IntOp $R3 $R3 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    IntOp $R4 $5 * 18
    IntOp $R4 $R4 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    System::Call 'user32::CreateWindowEx(i 0, t "STATIC", t "安装控制台", i ${WS_CHILD}|${WS_VISIBLE}|${SS_LEFT}, i $R1, i $R3, i $R2, i $R4, p $0, i 0, p 0, p 0) p.r3'
    SetCtlColors $3 "${INSTALLER_MID}" "${INSTALLER_INK}"
    SendMessage $3 ${WM_SETFONT} $InstallerSmallFont 0
    !insertmacro InstallerCreateProgressLabel "01  开始" 78 ${INSTALLER_MID} ${INSTALLER_INK} $InstallerPixelFont
    !insertmacro InstallerCreateProgressLabel "02  范围" 105 ${INSTALLER_MID} ${INSTALLER_INK} $InstallerPixelFont
    !insertmacro InstallerCreateProgressLabel "03  位置" 132 ${INSTALLER_MID} ${INSTALLER_INK} $InstallerPixelFont
    !insertmacro InstallerCreateProgressLabel "04  写入" 159 ${INSTALLER_INK} ${INSTALLER_LIGHT} $InstallerPixelFont
    !insertmacro InstallerCreateProgressLabel "05  完成" 186 ${INSTALLER_MID} ${INSTALLER_INK} $InstallerPixelFont
    !insertmacro InstallerCreateProgressMarker 159

    ; Work surface labels.
    IntOp $R0 $4 * 28
    IntOp $R0 $R0 / ${INSTALLER_PROGRESS_BASE_WIDTH}
    IntOp $R1 $6 + $R0
    IntOp $R3 $5 * 28
    IntOp $R3 $R3 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    IntOp $R4 $5 * 30
    IntOp $R4 $R4 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    System::Call 'user32::CreateWindowEx(i 0, t "STATIC", t "写入安装文件", i ${WS_CHILD}|${WS_VISIBLE}|${SS_LEFT}, i $R1, i $R3, i $8, i $R4, p $0, i 0, p 0, p 0) p.r2'
    SetCtlColors $2 "${INSTALLER_INK}" "${INSTALLER_LIGHT}"
    SendMessage $2 ${WM_SETFONT} $InstallerTitleFont 0
    IntOp $R3 $5 * 68
    IntOp $R3 $R3 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    IntOp $R4 $5 * 22
    IntOp $R4 $R4 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    System::Call 'user32::CreateWindowEx(i 0, t "STATIC", t "正在把工作台放进电脑，请保持窗口打开。", i ${WS_CHILD}|${WS_VISIBLE}|${SS_LEFT}, i $R1, i $R3, i $8, i $R4, p $0, i 0, p 0, p 0) p.r2'
    SetCtlColors $2 "${INSTALLER_SHADE}" "${INSTALLER_LIGHT}"
    SendMessage $2 ${WM_SETFONT} $InstallerBodyFont 0

    ; Keep the actual NSIS status and progress controls, but move them into
    ; the same work surface and hide the log/show-log affordance.
    GetDlgItem $R2 $0 1006
    GetDlgItem $R3 $0 1004
    GetDlgItem $R4 $0 1027
    GetDlgItem $R5 $0 1016
    ShowWindow $R4 ${SW_HIDE}
    ShowWindow $R5 ${SW_HIDE}
    SetCtlColors $R2 "${INSTALLER_SHADE}" "${INSTALLER_LIGHT}"
    SendMessage $R2 ${WM_SETFONT} $InstallerSmallFont 0
    IntOp $R6 $5 * 92
    IntOp $R6 $R6 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    IntOp $R6 $5 - $R6
    IntOp $R7 $5 * 20
    IntOp $R7 $R7 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    System::Call 'user32::SetWindowPos(p $R2, p 0, i $R1, i $R6, i $8, i $R7, i ${SWP_NOZORDER}|${SWP_NOACTIVATE})'
    IntOp $R6 $5 * 55
    IntOp $R6 $R6 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    IntOp $R6 $5 - $R6
    IntOp $R7 $4 * 2
    IntOp $R7 $R7 / ${INSTALLER_PROGRESS_BASE_WIDTH}
    IntOp $R8 $8 - $R7
    IntOp $R9 $5 * 18
    IntOp $R9 $R9 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    System::Call 'user32::SetWindowPos(p $R3, p 0, i $R1, i $R6, i $R8, i $R9, i ${SWP_NOZORDER}|${SWP_NOACTIVATE})'

    ; Explicit cancel action inside the redesigned surface.
    IntOp $R6 $5 * 30
    IntOp $R6 $R6 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    IntOp $R6 $5 - $R6
    IntOp $R7 $4 * 102
    IntOp $R7 $R7 / ${INSTALLER_PROGRESS_BASE_WIDTH}
    IntOp $R7 $4 - $R7
    IntOp $R8 $4 * 90
    IntOp $R8 $R8 / ${INSTALLER_PROGRESS_BASE_WIDTH}
    IntOp $R9 $5 * 22
    IntOp $R9 $R9 / ${INSTALLER_PROGRESS_BASE_HEIGHT}
    System::Call 'user32::CreateWindowEx(i 0, t "BUTTON", t "取消安装", i ${WS_CHILD}|${WS_VISIBLE}|${WS_TABSTOP}|${BS_PUSHBUTTON}, i $R7, i $R6, i $R8, i $R9, p $0, i 0, p 0, p 0) p.r8'
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
    StrCpy $InstallerActiveStep "5"
    StrCpy $InstallerFrameCompact "0"
    Call InstallerBuildFrame

    !insertmacro InstallerCreateLabel $0 104u 25u 202u 26u "安装完成" $InstallerTitleFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateLabel $0 104u 55u 202u 26u "小猪wordTTS 已经写入电脑。" $InstallerBodyFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreateRule $0 104u 87u 202u ${INSTALLER_SHADE}
    !insertmacro InstallerCreateLabel $0 104u 94u 202u 12u "结果" $InstallerPixelFont ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !insertmacro InstallerCreatePanel 104u 110u 202u 20u ${INSTALLER_MID}
    !insertmacro InstallerCreateLabel $0 112u 114u 22u 11u "OK" $InstallerPixelFont ${INSTALLER_INK} ${INSTALLER_MID}
    !insertmacro InstallerCreateLabel $0 144u 114u 156u 11u "程序文件与快捷方式已准备好" $InstallerSmallFont ${INSTALLER_INK} ${INSTALLER_MID}

    !ifdef HIDE_RUN_AFTER_FINISH
      !insertmacro InstallerCreateLabel $0 104u 139u 202u 20u "现在可以从桌面或开始菜单启动。" $InstallerSmallFont ${INSTALLER_SHADE} ${INSTALLER_LIGHT}
      !insertmacro InstallerCreateButton $InstallerFinishAction 235u 171u 69u 16u "完成" InstallerFinishDone ${INSTALLER_INK} ${INSTALLER_LIGHT}
    !else
      ${NSD_CreateCheckBox} 104u 139u 202u 15u "完成后打开小猪wordTTS"
      Pop $InstallerFinishCheckbox
      ${NSD_Check} $InstallerFinishCheckbox
      SetCtlColors $InstallerFinishCheckbox "${INSTALLER_INK}" "${INSTALLER_LIGHT}"
      SendMessage $InstallerFinishCheckbox ${WM_SETFONT} $InstallerBodyFont 0
      ${NSD_OnClick} $InstallerFinishCheckbox InstallerFinishToggleOpen
      ${NSD_CreateButton} 235u 171u 69u 16u "完成并打开"
      Pop $InstallerFinishAction
      SendMessage $InstallerFinishAction ${WM_SETFONT} $InstallerButtonFont 0
      SetCtlColors $InstallerFinishAction "${INSTALLER_INK}" "${INSTALLER_LIGHT}"
      ${NSD_OnClick} $InstallerFinishAction InstallerFinishDone
      ${NSD_SetFocus} $InstallerFinishAction
    !endif
    !insertmacro InstallerCreateButton $InstallerFinishCancel 164u 171u 65u 16u "关闭" InstallerCancel ${INSTALLER_LIGHT} ${INSTALLER_INK}
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
