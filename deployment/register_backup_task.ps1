<#
  ★★ 這個檔**必須存成 UTF-8 with BOM**。Windows PowerShell 5.1 讀 .ps1 時，
     沒有 BOM 就當成系統 ANSI 代碼頁（這台是 cp932 日文），檔案裡的中文會
     被解成亂碼，然後在某個位元組上把字串引號吃掉——報出來的是
     "The string is missing the terminator"，看起來像語法錯，實際上是編碼。
     踩過一次（2026-09-12）。改這個檔的編輯器若會去掉 BOM，要記得加回來。
#>
<#
  把備援爬蟲註冊成 Windows 工作排程器的工作（對應 Mac 那邊的 launchd plist）。

  用法（在專案根，一般權限即可）：
      powershell -ExecutionPolicy Bypass -File deployment\register_backup_task.ps1
      powershell -ExecutionPolicy Bypass -File deployment\register_backup_task.ps1 -Unregister

  ★★ 排程時刻：每小時 :56，外加 23:59。
     主端（mac-relay-01）是每小時 :55 加 23:59，所以備援**晚一分鐘**。
     一分鐘夠讓主端把 fetch_run 那列 commit 完嗎？實測 344 次執行平均
     6.7 秒、p95 7.9 秒、最久 24.7 秒——還有三十幾秒餘裕。

     這個「晚一分鐘」跟 scripts/run_once.py 的 BACKUP_WINDOW（30 分鐘）
     是綁在一起的：

         排程錯開（1 分鐘） < BACKUP_WINDOW（30 分鐘） < 執行間隔（60 分鐘）

     改任何一邊都要回去看另一邊，否則會靜靜地變成「兩台搶著抓」或
     「主端死了備援永遠不接手」，兩種都**看起來完全正常**。

  ★ 23:59 那一次照主端的時刻跑，不往後挪：台電的檔 00:00 換日重置，
    當日最後那幾個點過了午夜就永久取不回來。備援在 23:59 才動，若主端
    23:55 已經成功（4 分鐘前、落在窗內）就會自動待命，不會重複抓。

  ★ -WakeToRun / -StartWhenAvailable：對應 DEPLOY.md 裡 Mac 睡眠那一節。
    機器睡著時排程不會跑，醒來後只補跑一次，不會把睡掉的每一次都補齊。
    夜間睡著最貴（23:56／23:59 補不回來），白天漏掉則由當日累積檔補齊。
#>
[CmdletBinding()]
param(
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'

$proj = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$cmd = Join-Path $proj 'deployment\run_backup.cmd'
$hourlyName = 'TaipowerCurveBackup'
$midnightName = 'TaipowerCurveBackupMidnight'

if ($Unregister) {
    foreach ($n in @($hourlyName, $midnightName)) {
        if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $n -Confirm:$false
            Write-Output "已移除 $n"
        } else {
            Write-Output "$n 本來就不存在"
        }
    }
    return
}

if (-not (Test-Path $cmd)) { throw "找不到 $cmd" }
if (-not (Test-Path (Join-Path $proj 'venv\Scripts\python.exe'))) {
    throw '找不到 venv——先跑 py -3.13 -m venv venv 並安裝 requirements.txt'
}

# ★ 時區必須是 Asia/Taipei：工作排程器照系統時區跑，而台電的檔是台北時間，
#   00:00 換日。系統時區錯掉的話 23:59 那一次會跑在錯的時刻，當天最後
#   幾個點就永久遺失——而且圖上看起來只像「那時候沒用電」。
$tz = (Get-TimeZone).Id
if ($tz -ne 'Taipei Standard Time') {
    throw "系統時區是 '$tz'，不是 Taipei Standard Time。排程時刻會錯開，先改時區。"
}

$action = New-ScheduledTaskAction -Execute $cmd -WorkingDirectory $proj

# 每小時 :56（從 00:56 起、每小時重複一次、持續一天）
$hourly = New-ScheduledTaskTrigger -Daily -At '00:56'
$hourly.Repetition = (New-ScheduledTaskTrigger -Once -At '00:56' `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 1)).Repetition

# 收當日尾巴的那一次
$midnight = New-ScheduledTaskTrigger -Daily -At '23:59'

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -WakeToRun `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

foreach ($spec in @(@{ Name = $hourlyName; Trigger = $hourly;
                       Desc = '台電用電曲線備援爬蟲（每小時 :56，主端 :55 晚一分鐘）' },
                    @{ Name = $midnightName; Trigger = $midnight;
                       Desc = '台電用電曲線備援爬蟲（23:59 收當日尾巴，檔案 00:00 換日重置）' })) {
    if (Get-ScheduledTask -TaskName $spec.Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $spec.Name -Confirm:$false
    }
    Register-ScheduledTask -TaskName $spec.Name -Action $action `
        -Trigger $spec.Trigger -Settings $settings -Principal $principal `
        -Description $spec.Desc | Out-Null
    Write-Output "已註冊 $($spec.Name)"
}

Get-ScheduledTask -TaskName $hourlyName, $midnightName |
    Select-Object TaskName, State |
    Format-Table -AutoSize
