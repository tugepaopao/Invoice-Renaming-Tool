; =============================================================
;  全电发票批量重命名工具 —— Windows 安装包脚本 (NSIS 3.x)
;  编译：makensis installer.nsi
;  产物：安装包\发票批量重命名工具_安装程序_v2.8.9.exe
; =============================================================

Unicode true
SetCompressor /SOLID lzma
SetCompressorDictSize 32

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"

; ---------------- 基本信息 ----------------
!define APP_NAME       "全电发票批量重命名工具"
!define APP_ID         "InvoiceRenamer"
!define APP_VERSION    "2.8.9"
!define APP_PUBLISHER  "石文豪"
; 作者署名（2026-10-17 业主指定）。跟 APP_PUBLISHER 是两回事：
;   发布者 = 装到「添加或删除程序」里显示的公司/个人名
;   作者   = 作品署名，写进安装包的版本信息「备注」栏 + 界面顶栏
!define APP_AUTHOR     "豪崽子"
!define APP_EXE        "发票批量重命名工具.exe"
!define SOURCE_DIR     "dist\发票批量重命名工具"
!define OUT_FILE       "安装包\发票批量重命名工具_安装程序_v${APP_VERSION}.exe"

Name "${APP_NAME}"
OutFile "${OUT_FILE}"

; 装到当前用户目录，全程不需要管理员权限，不弹 UAC
InstallDir "$LOCALAPPDATA\Programs\${APP_ID}"
InstallDirRegKey HKCU "Software\${APP_ID}" "InstallDir"
RequestExecutionLevel user
ShowInstDetails show
ShowUninstDetails show

VIProductVersion "2.8.9.0"
VIAddVersionKey /LANG=2052 "ProductName"      "${APP_NAME}"
VIAddVersionKey /LANG=2052 "FileDescription"  "${APP_NAME} 安装程序"
VIAddVersionKey /LANG=2052 "FileVersion"      "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "ProductVersion"   "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "CompanyName"      "${APP_PUBLISHER}"
VIAddVersionKey /LANG=2052 "LegalCopyright"   "© 2026 ${APP_PUBLISHER}"
VIAddVersionKey /LANG=2052 "Comments"         "作者：${APP_AUTHOR}　版本：${APP_VERSION}"

; ---------------- 界面 ----------------
!define MUI_ICON   "app.ico"
!define MUI_UNICON "app.ico"
!define MUI_ABORTWARNING
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_RIGHT
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "立即启动 ${APP_NAME}"
!define MUI_FINISHPAGE_SHOWREADME ""
!define MUI_FINISHPAGE_SHOWREADME_TEXT "在桌面创建快捷方式"
!define MUI_FINISHPAGE_SHOWREADME_FUNCTION CreateDesktopShortcut
!define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED

!insertmacro MUI_PAGE_WELCOME
; 许可页正文用专门的短文，不要塞整份使用说明（太长，而且 NSIS 的
; LicenseData 只认 ANSI 或 UTF-16LE，UTF-8 会显示成乱码）
!insertmacro MUI_PAGE_LICENSE "许可协议.txt"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

; ---------------- 安装前检查：程序是否在运行 ----------------
; 静默模式（/S）下跳过这个检查，否则弹窗会卡住自动化安装
;
; ⚠️ 这里**不能**用 FindWindow 按窗口标题判断（老版本踩过的坑）：
;     Windows 11 的资源管理器有一个类名为
;       "Windows.Internal.Shell.TabProxyWindow"
;     的代理窗口，它的窗口标题 = 当前打开的文件夹名。
;     只要用户在资源管理器里开着同名文件夹（比如解压出来的程序目录），
;     FindWindow 就会命中 → 安装包误报「程序正在运行」，
;     而用户其实根本没开过程序，也没有任何办法关掉"它"。
;     所以改成直接查**进程名** —— 这才是我们真正关心的，
;     而且它同时覆盖了"程序被最小化到任务栏"这类用户看不见的情况。
!macro CheckRunning
  IfSilent done
  loop:
    nsExec::ExecToStack 'tasklist /FI "IMAGENAME eq ${APP_EXE}" /FO CSV /NH'
    Pop $0                              ; 命令退出码
    Pop $1                              ; 命令输出
    ; 进程存在时 tasklist 输出 CSV 行，以半角引号开头：
    ;   "发票批量重命名工具.exe","1234","Console","1","12,345 K"
    ; 进程不存在时输出的是本地化的提示语（中文以「信息」开头，英文以 INFO 开头），
    ; 都不会以引号开头。用首字符判断，顺便避开中文编码问题。
    StrCpy $2 "$1" 1
    StrCmp $2 $\" 0 done
      MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION \
        "检测到「${APP_NAME}」正在运行，程序文件被占用，装不进去。$\r$\n$\r$\n请先把它关掉：$\r$\n  1. 看一眼任务栏，它可能只是被最小化了$\r$\n  2. 找不到窗口的话，按 Ctrl+Shift+Esc 打开任务管理器，$\r$\n     在「进程」列表里找到「${APP_NAME}」，右键结束任务$\r$\n$\r$\n关掉之后点「重试」即可继续安装。" \
        IDRETRY loop
      Abort
  done:
!macroend

Function .onInit
  !insertmacro CheckRunning
FunctionEnd

Function un.onInit
  !insertmacro CheckRunning
FunctionEnd

; ---------------- 组件 ----------------
Section "主程序（必需）" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"
  SetOverwrite on

  ; 整个程序目录（含 _internal、web 等）
  File /r "${SOURCE_DIR}\*.*"

  ; 说明文档（PyInstaller 产物里没有，单独带上，供开始菜单快捷方式指向）
  File "使用说明.md"
  File "许可协议.txt"

  ; 主程序文件属性
  WriteRegStr HKCU "Software\${APP_ID}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\${APP_ID}" "Version"    "${APP_VERSION}"

  ; 开始菜单快捷方式
  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" \
                 "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\使用说明.lnk" \
                 "$INSTDIR\使用说明.md" "" "$INSTDIR\${APP_EXE}" 0

  ; 写卸载程序 + 注册到「程序和功能」
  WriteUninstaller "$INSTDIR\卸载 ${APP_NAME}.exe"
  WriteRegStr   HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" \
                "DisplayName"     "${APP_NAME}"
  WriteRegStr   HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" \
                "DisplayIcon"     "$INSTDIR\${APP_EXE}"
  WriteRegStr   HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" \
                "DisplayVersion"  "${APP_VERSION}"
  WriteRegStr   HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" \
                "Publisher"       "${APP_PUBLISHER}"
  WriteRegStr   HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" \
                "InstallLocation" "$INSTDIR"
  WriteRegStr   HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" \
                "UninstallString" '"$INSTDIR\卸载 ${APP_NAME}.exe"'
  WriteRegStr   HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" \
                "QuietUninstallString" '"$INSTDIR\卸载 ${APP_NAME}.exe" /S'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" \
                "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" \
                "NoRepair" 1

  ; 估算占用体积（KB）
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" \
                "EstimatedSize" "$0"
SectionEnd

Section "桌面快捷方式" SecDesktop
  call CreateDesktopShortcut
SectionEnd

Function CreateDesktopShortcut
  CreateShortCut "$DESKTOP\${APP_NAME}.lnk" \
                 "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
FunctionEnd

; 组件说明
!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain}    "程序主体文件，必需。"
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} "在桌面放一个快捷方式，方便随时打开。"
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; ---------------- 卸载 ----------------
Section "Uninstall"
  ; 删程序目录
  RMDir /r "$INSTDIR"

  ; 删快捷方式
  Delete "$DESKTOP\${APP_NAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APP_NAME}"

  ; 删注册表
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}"
  DeleteRegKey HKCU "Software\${APP_ID}"

  ; 询问是否一并删除设置与缓存
  ; 静默卸载（/S）时按「全部清理」处理，不弹窗
  IfSilent un_clean_all
  MessageBox MB_YESNO|MB_ICONQUESTION \
    "是否同时删除程序设置和界面缓存？$\r$\n（选「否」则保留，重装后设置还在）" \
    IDNO keep_data
  un_clean_all:
    RMDir /r "$APPDATA\发票批量重命名工具"
    RMDir /r "$LOCALAPPDATA\InvoiceRenamer"
  keep_data:
SectionEnd
