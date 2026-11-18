' Hidden wrapper for task MihomoFeishuBitableSync.
' Runs run-feishu-bitable-sync.bat with a hidden console (SW_HIDE) so no
' black cmd window flashes every 30 minutes. Exit code is propagated so
' the task's LastTaskResult keeps working as before.
Option Explicit

Dim sh, fso, baseDir, batPath, cmdLine, exitCode
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
batPath = fso.BuildPath(baseDir, "run-feishu-bitable-sync.bat")

If WScript.Arguments.Count <> 0 Then WScript.Quit 2
If Not fso.FileExists(batPath) Then WScript.Quit 2

sh.CurrentDirectory = baseDir
cmdLine = Chr(34) & "C:\Windows\System32\cmd.exe" & Chr(34) & " /d /c " & Chr(34) & batPath & Chr(34)

On Error Resume Next
exitCode = sh.Run(cmdLine, 0, True)
If Err.Number <> 0 Then WScript.Quit 1
On Error GoTo 0
WScript.Quit exitCode
