# SPDX-License-Identifier: GPL-3.0-only OR MIT
param(
    [Parameter(Mandatory)][string]$BetterGIExe,
    [string]$GameExe,
    [ValidateSet('RunScene','Release')][string]$Action = 'RunScene'
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$PSStyle.OutputRendering = 'PlainText'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class GenshinCodexDesktop {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr window, int command);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr window);
}
'@

function Find-WindowProcess([string]$Path, [bool]$IncludeHidden = $false) {
    $base = [IO.Path]::GetFileNameWithoutExtension($Path)
    Get-Process -Name $base -ErrorAction SilentlyContinue |
        Where-Object {
            $image = $_.Path
            if (!$image) {
                $image = (Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $_.Id)).ExecutablePath
            }
            $image -eq $Path -and ($IncludeHidden -or $_.MainWindowHandle -ne [IntPtr]::Zero)
        } |
        Select-Object -First 1
}

function Wait-For([scriptblock]$Read, [string]$Label, [int]$Attempts = 18) {
    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        $value = & $Read
        if ($value) { return $value }
        Start-Sleep -Milliseconds 180
    }
    throw "等待 $Label 超时"
}

function Find-Control([string]$Name, $Type) {
    foreach ($caption in @($Name, $translations[$Name]) | Where-Object { $_ } | Select-Object -Unique) {
        $condition = [Windows.Automation.PropertyCondition]::new(
            [Windows.Automation.AutomationElement]::NameProperty, $caption)
        if ($Type) {
            $typeCondition = [Windows.Automation.PropertyCondition]::new(
                [Windows.Automation.AutomationElement]::ControlTypeProperty, $Type)
            $condition = [Windows.Automation.AndCondition]::new($condition, $typeCondition)
        }
        $found = $automationRoot.FindFirst([Windows.Automation.TreeScope]::Descendants, $condition)
        if ($found) { return $found }
    }
}

function Invoke-Control($Control) {
    $pattern = $Control.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern)
    $pattern.Invoke()
}

function Select-Navigation($Control) {
    $expectedName = $Control.Current.Name
    if ([GenshinCodexDesktop]::GetForegroundWindow() -ne $betterWindow) {
        $shell = New-Object -ComObject WScript.Shell
        [void]$shell.AppActivate($better.Id)
    }
    $null = Wait-For {
        $Control.SetFocus()
        Start-Sleep -Milliseconds 180
        $focused = [Windows.Automation.AutomationElement]::FocusedElement
        ($focused.Current.ProcessId -eq $better.Id -and $focused.Current.Name -eq $expectedName -and
         [GenshinCodexDesktop]::GetForegroundWindow() -eq $betterWindow)
    } "BetterGI 导航 $expectedName 的前台焦点" 6
    [Windows.Forms.SendKeys]::SendWait('{ENTER}')
}

function Select-Group($TextControl) {
    $node = $TextControl
    for ($depth = 0; $node -and $depth -lt 5; $depth++) {
        $pattern = $null
        if ($node.TryGetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pattern)) {
            $pattern.Select()
            return
        }
        $node = [Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($node)
    }
    throw '配置组缺少可用的选择控件'
}

$better = Find-WindowProcess $BetterGIExe ($Action -eq 'Release')
if ($Action -eq 'Release') {
    if ($better) {
        if (!$better.CloseMainWindow() -or !$better.WaitForExit(5000)) {
            $better.Refresh()
            if (!$better.HasExited) {
                Stop-Process -InputObject $better -ErrorAction Stop
                if (!$better.WaitForExit(3000)) { throw '便携版 BetterGI 进程未退出' }
            }
        }
    }
    'BetterGI 已释放游戏控制'
    exit 0
}
if (!$better) {
    Start-Process -FilePath $BetterGIExe -WorkingDirectory (Split-Path -Parent $BetterGIExe) -WindowStyle Hidden
    $better = Wait-For { Find-WindowProcess $BetterGIExe } 'BetterGI 窗口' 60
}
$betterWindow = $better.MainWindowHandle
$automationRoot = [Windows.Automation.AutomationElement]::FromHandle($betterWindow)
$translations = @{}
$languageFile = Join-Path (Split-Path -Parent $BetterGIExe) 'User\I18n\en.json'
if (Test-Path -LiteralPath $languageFile) {
    $translations = Get-Content -LiteralPath $languageFile -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
}
$buttonType = [Windows.Automation.ControlType]::Button
$navType = [Windows.Automation.ControlType]::DataItem
if (!$GameExe) {
    $candidates = @(Get-CimInstance Win32_Process -Filter "Name = 'YuanShen.exe' OR Name = 'GenshinImpact.exe'" |
        Where-Object { $_.ExecutablePath -and (Find-WindowProcess $_.ExecutablePath) })
    if ($candidates.Count -ne 1) { throw '需要唯一的原神窗口，或在本地设置中指定游戏可执行文件' }
    $GameExe = $candidates[0].ExecutablePath
}
$game = Find-WindowProcess $GameExe
if (!$game) { throw '找不到需要绑定的原神窗口' }
$gameWindow = $game.MainWindowHandle

try {
    [void][GenshinCodexDesktop]::ShowWindowAsync($betterWindow, 9)
    $readCaptureButton = {
        $stopping = Find-Control '停止' $buttonType
        if ($stopping) { $stopping }
        else { Find-Control '启动' $buttonType }
    }
    $capture = & $readCaptureButton
    if (!$capture) {
        $launchNavigation = Wait-For { Find-Control '启动' $navType } '启动导航'
        Select-Navigation $launchNavigation
        $capture = Wait-For $readCaptureButton '截图器按钮'
    }
    if ($capture.Current.Name -in @('启动', $translations['启动'])) {
        Invoke-Control $capture
        $null = Wait-For { Find-Control '停止' $buttonType } 'BetterGI 截图器' 32
        Start-Sleep -Milliseconds 700
    }
    $automation = Wait-For { Find-Control '全自动' $navType } '全自动导航'
    $scheduler = Find-Control '调度器' $navType
    if (!$scheduler) {
        Select-Navigation $automation
        $scheduler = Wait-For { Find-Control '调度器' $navType } '调度器导航'
    }
    Select-Navigation $scheduler
    $group = Wait-For { Find-Control 'CodexScene' ([Windows.Automation.ControlType]::Text) } 'CodexScene 配置组'
    Select-Group $group
    $run = Wait-For { Find-Control '连续执行' $buttonType } '连续执行按钮'
    if (!$run.Current.IsEnabled) { throw 'BetterGI 正在处理任务，连续执行按钮当前不可用' }
    Invoke-Control $run
    $confirm = Wait-For { Find-Control '确认执行' $buttonType } '配置组确认弹窗'
    $modal = [Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($confirm)
    for ($depth = 0; $modal -and $depth -lt 8; $depth++) {
        if ($modal.Current.ControlType -eq [Windows.Automation.ControlType]::Window) { break }
        $modal = [Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($modal)
    }
    if (!$modal -or $modal.Current.Name -notin @('选择需要执行的配置组', $translations['选择需要执行的配置组'])) { throw '无法确认 BetterGI 配置组选择弹窗' }
    $boxes = $modal.FindAll(
        [Windows.Automation.TreeScope]::Descendants,
        [Windows.Automation.PropertyCondition]::new(
            [Windows.Automation.AutomationElement]::ControlTypeProperty,
            [Windows.Automation.ControlType]::CheckBox
        )
    )
    $selected = $false
    foreach ($box in $boxes) {
        $target = $box.Current.Name -eq 'CodexScene'
        $toggle = $box.GetCurrentPattern([Windows.Automation.TogglePattern]::Pattern)
        if ($target -ne ($toggle.Current.ToggleState -eq [Windows.Automation.ToggleState]::On)) {
            $toggle.Toggle()
        }
        $selected = $selected -or $target
    }
    if (!$selected) { throw '选择窗口里找不到 CodexScene' }
    Invoke-Control $confirm
} finally {
    if (![GenshinCodexDesktop]::SetForegroundWindow($gameWindow)) {
        try { [Windows.Automation.AutomationElement]::FromHandle($gameWindow).SetFocus() }
        catch { Write-Verbose '原神焦点将由 BetterGI 任务接管' }
    }
}
'CodexScene 已通过 BetterGI 调度器提交'
