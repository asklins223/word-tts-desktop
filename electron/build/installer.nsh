!include "nsDialogs.nsh"
!include "WinMessages.nsh"
!include "LogicLib.nsh"

!ifndef BUILD_UNINSTALLER

; 小猪wordTTS Windows 安装器的品牌化欢迎页与完成页。
; 使用 MUI 的全窗口页面保留 164x314 品牌侧栏，同时把自定义内容限制在
; NSIS 标准右侧内容区（120u..315u，y <= 193u）内。
; 文件复制、权限提升和升级语义仍由 electron-builder 的受信任模板处理。

Var InstallerWelcomeBodyFont
Var InstallerWelcomeFont
Var InstallerFinishBodyFont
Var InstallerFinishFont
!ifndef HIDE_RUN_AFTER_FINISH
  Var InstallerFinishCheckbox
  Var InstallerFinishStartArgs
!endif

!macro customWelcomePage
  !define MUI_PAGE_CUSTOMFUNCTION_SHOW InstallerWelcomeCreate
  !define MUI_PAGE_CUSTOMFUNCTION_DESTROYED InstallerWelcomeDestroy
  !insertmacro MUI_PAGE_WELCOME

  Function InstallerWelcomeCreate
    ; MUI_PAGE_WELCOME 已创建 1044 全窗口对话框并加载 installerSidebar。
    ShowWindow $mui.WelcomePage.Title ${SW_HIDE}
    ShowWindow $mui.WelcomePage.Text ${SW_HIDE}
    SetCtlColors $mui.WelcomePage "F4F9FF" "F4F9FF"
    CreateFont $InstallerWelcomeFont "Microsoft YaHei UI" 15 700
    CreateFont $InstallerWelcomeBodyFont "Microsoft YaHei UI" 9 400

    ${NSD_CreateLabel} 120u 10u 195u 24u "小猪wordTTS"
    Pop $0
    SendMessage $0 ${WM_SETFONT} $InstallerWelcomeFont 0
    SetCtlColors $0 "12345A" "F4F9FF"

    ${NSD_CreateLabel} 120u 40u 195u 30u "把文档配音工作台安装到这台 Windows 电脑。"
    Pop $0
    SendMessage $0 ${WM_SETFONT} $InstallerWelcomeBodyFont 0
    SetCtlColors $0 "55748F" "F4F9FF"

    ${NSD_CreateGroupBox} 120u 76u 195u 76u "安装内容"
    Pop $0
    SetCtlColors $0 "B7D2EB" "F4F9FF"

    ${NSD_CreateLabel} 132u 98u 171u 14u "●  默认当前用户；全局安装可能需管理员"
    Pop $0
    SendMessage $0 ${WM_SETFONT} $InstallerWelcomeBodyFont 0
    SetCtlColors $0 "24445F" "F4F9FF"
    ${NSD_CreateLabel} 132u 117u 171u 14u "●  创建桌面与开始菜单快捷方式"
    Pop $0
    SendMessage $0 ${WM_SETFONT} $InstallerWelcomeBodyFont 0
    SetCtlColors $0 "24445F" "F4F9FF"
    ${NSD_CreateLabel} 132u 136u 171u 14u "●  不会删除已有任务与本地配置"
    Pop $0
    SendMessage $0 ${WM_SETFONT} $InstallerWelcomeBodyFont 0
    SetCtlColors $0 "24445F" "F4F9FF"

    ${NSD_CreateLabel} 120u 163u 195u 28u "建议先关闭正在运行的小猪wordTTS。"
    Pop $0
    SendMessage $0 ${WM_SETFONT} $InstallerWelcomeBodyFont 0
    SetCtlColors $0 "55748F" "F4F9FF"

    GetDlgItem $0 $HWNDPARENT 1
    SendMessage $0 ${WM_SETTEXT} 0 "STR:开始安装"
  FunctionEnd

  Function InstallerWelcomeDestroy
    ${If} $InstallerWelcomeFont != 0
      System::Call "GDI32::DeleteObject(p $InstallerWelcomeFont)"
      StrCpy $InstallerWelcomeFont 0
    ${EndIf}
    ${If} $InstallerWelcomeBodyFont != 0
      System::Call "GDI32::DeleteObject(p $InstallerWelcomeBodyFont)"
      StrCpy $InstallerWelcomeBodyFont 0
    ${EndIf}
  FunctionEnd
!macroend

!macro customFinishPage
  !define MUI_PAGE_CUSTOMFUNCTION_SHOW InstallerFinishCreate
  !define MUI_PAGE_CUSTOMFUNCTION_DESTROYED InstallerFinishDestroy
  !define MUI_PAGE_CUSTOMFUNCTION_LEAVE InstallerFinishLeave
  !insertmacro MUI_PAGE_FINISH

  !ifndef HIDE_RUN_AFTER_FINISH
    Function InstallerFinishUpdateButton
      GetDlgItem $0 $HWNDPARENT 1
      ${NSD_GetState} $InstallerFinishCheckbox $1
      ${If} $1 == ${BST_CHECKED}
        SendMessage $0 ${WM_SETTEXT} 0 "STR:完成并打开"
      ${Else}
        SendMessage $0 ${WM_SETTEXT} 0 "STR:完成"
      ${EndIf}
    FunctionEnd

    Function InstallerFinishToggleOpen
      Pop $0 ; NSD_OnClick passes the clicked control HWND on the stack.
      Call InstallerFinishUpdateButton
    FunctionEnd
  !endif

  Function InstallerFinishCreate
    ; MUI_PAGE_FINISH 已创建 1044 全窗口对话框并加载 installerSidebar。
    ShowWindow $mui.FinishPage.Title ${SW_HIDE}
    ShowWindow $mui.FinishPage.Text ${SW_HIDE}
    SetCtlColors $mui.FinishPage "F4F9FF" "F4F9FF"
    CreateFont $InstallerFinishFont "Microsoft YaHei UI" 15 700
    CreateFont $InstallerFinishBodyFont "Microsoft YaHei UI" 9 400

    ${NSD_CreateLabel} 120u 10u 195u 24u "安装完成"
    Pop $0
    SendMessage $0 ${WM_SETFONT} $InstallerFinishFont 0
    SetCtlColors $0 "12345A" "F4F9FF"

    ${NSD_CreateLabel} 120u 41u 195u 34u "小猪wordTTS 已经准备好。打开它，继续把下一份文档变成声音。"
    Pop $0
    SendMessage $0 ${WM_SETFONT} $InstallerFinishBodyFont 0
    SetCtlColors $0 "55748F" "F4F9FF"

    !ifdef HIDE_RUN_AFTER_FINISH
      ${NSD_CreateLabel} 120u 105u 195u 32u "安装已完成，之后可以从开始菜单或桌面快捷方式启动。"
      Pop $0
      SendMessage $0 ${WM_SETFONT} $InstallerFinishBodyFont 0
      SetCtlColors $0 "55748F" "F4F9FF"
      GetDlgItem $0 $HWNDPARENT 1
      SendMessage $0 ${WM_SETTEXT} 0 "STR:完成"
    !else
      ${NSD_CreateCheckBox} 120u 105u 195u 16u "完成后打开小猪wordTTS"
      Pop $InstallerFinishCheckbox
      ${NSD_Check} $InstallerFinishCheckbox
      ${NSD_OnClick} $InstallerFinishCheckbox InstallerFinishToggleOpen
      SetCtlColors $InstallerFinishCheckbox "24445F" "F4F9FF"
      ${NSD_SetFocus} $InstallerFinishCheckbox

      ${NSD_CreateLabel} 120u 133u 195u 36u "任务与设置仍保存在本机，之后可以从开始菜单或桌面快捷方式启动。"
      Pop $0
      SendMessage $0 ${WM_SETFONT} $InstallerFinishBodyFont 0
      SetCtlColors $0 "55748F" "F4F9FF"
      Call InstallerFinishUpdateButton
    !endif
  FunctionEnd

  Function InstallerFinishDestroy
    ${If} $InstallerFinishFont != 0
      System::Call "GDI32::DeleteObject(p $InstallerFinishFont)"
      StrCpy $InstallerFinishFont 0
    ${EndIf}
    ${If} $InstallerFinishBodyFont != 0
      System::Call "GDI32::DeleteObject(p $InstallerFinishBodyFont)"
      StrCpy $InstallerFinishBodyFont 0
    ${EndIf}
  FunctionEnd

  Function InstallerFinishLeave
    !ifndef HIDE_RUN_AFTER_FINISH
      ${NSD_GetState} $InstallerFinishCheckbox $0
      ${If} $0 == ${BST_CHECKED}
        ; Use electron-builder's non-elevated launch path and preserve update args.
        ${if} ${isUpdated}
          StrCpy $InstallerFinishStartArgs "--updated"
        ${else}
          StrCpy $InstallerFinishStartArgs ""
        ${endif}
        ${StdUtils.ExecShellAsUser} $0 "$launchLink" "open" "$InstallerFinishStartArgs"
      ${EndIf}
    !endif
  FunctionEnd
!macroend

!endif
