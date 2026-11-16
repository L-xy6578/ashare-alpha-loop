# 三角闭环守护 —— Windows 计划任务安装脚本（管理员运行）
#
# 为什么用计划任务而不是"启动文件夹 VBS"：
#   - VBS 只在"用户登录后"启动，关机/未登录期间不会补跑，11-04 就是关机导致整段缺失；
#   - 计划任务可在开机时（AtStartup，S4U 免密码后台运行）拉起守护，不依赖登录态；
#   - 任务自带崩溃重启（RestartCount）与无限运行时长，比 VBS 更可控；
#   - 也可直接运行：python run_daily.py --watch
#
# 用法（管理员 PowerShell）：
#   .\scripts\install_daily_task.ps1                # 安装（默认开机触发）
#   .\scripts\install_daily_task.ps1 -Trigger AtLogOn
#   .\scripts\install_daily_task.ps1 -Action query  # 查询状态
#   .\scripts\install_daily_task.ps1 -Action run    # 立即启动
#   .\scripts\install_daily_task.ps1 -Action uninstall
#
# 说明：AtStartup + S4U 以当前用户身份后台运行（无需输入密码）。
#       若本机策略禁止 S4U，退回 -Trigger AtLogOn（登录后启动）。

param(
    [ValidateSet("install", "uninstall", "query", "run")]
    [string]$Action = "install",
    [ValidateSet("AtStartup", "AtLogOn")]
    [string]$Trigger = "AtStartup",
    [string]$TaskName = "TriangleLoopDaily"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Get-Python {
    # 优先项目 .venv（pythonw 无控制台窗口），其次 PATH 中的 python
    foreach ($p in @(
        (Join-Path $Root ".venv\Scripts\pythonw.exe"),
        (Join-Path $Root ".venv\Scripts\python.exe")
    )) {
        if (Test-Path $p) { return $p }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "未找到 Python：请先创建项目 .venv（python -m venv .venv）"
}

function Show-Task {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t) {
        Write-Host "未安装计划任务 $TaskName"
        return
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "任务名: $TaskName"
    Write-Host "状态: $($t.State)"
    Write-Host "上次运行: $($info.LastRunTime)  结果: $($info.LastTaskResult)"
    Write-Host "下次运行: $($info.NextRunTime)"
}

switch ($Action) {
    "query" {
        Show-Task
    }
    "run" {
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "已触发 $TaskName"
        Show-Task
    }
    "uninstall" {
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Host "已删除计划任务 $TaskName"
        } else {
            Write-Host "计划任务 $TaskName 不存在"
        }
        # 同时移除旧的启动文件夹 VBS（避免两个守护抢跑；单实例锁会拒绝，但清理更干净）
        $vbs = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\TriangleLoopDaily.vbs"
        if (Test-Path $vbs) {
            Remove-Item $vbs -Force
            Write-Host "已移除旧启动项 VBS: $vbs"
        }
    }
    "install" {
        $python = Get-Python
        $script = Join-Path $Root "run_daily.py"
        if (-not (Test-Path $script)) { throw "找不到 run_daily.py: $script" }

        $argument = ('"{0}" --watch --source task-scheduler' -f $script)
        $act = New-ScheduledTaskAction -Execute $python -Argument $argument -WorkingDirectory $Root

        if ($Trigger -eq "AtStartup") {
            $trg = New-ScheduledTaskTrigger -AtStartup
        } else {
            $trg = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        }

        $set = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 5) `
            -MultipleInstances IgnoreNew

        # S4U：以当前用户身份后台运行，不需要登录，也不需要保存密码
        $prn = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited

        Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger $trg `
            -Settings $set -Principal $prn -Description "三角闭环每日守护" -Force | Out-Null
        Write-Host "已安装计划任务 $TaskName（触发=$Trigger，Python=$python）"

        # 已登录环境下立即启动一次，避免等到下次关机/登录
        Start-ScheduledTask -TaskName $TaskName
        Start-Sleep -Seconds 2
        Show-Task
    }
}
