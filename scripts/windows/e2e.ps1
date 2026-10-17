<#
.SYNOPSIS
    saywell 的 Windows 端到端测试：沙箱里安装、用真实 provider 跑 CLI、托盘手测。

.DESCRIPTION
    在仓库根目录运行：  .\scripts\windows\e2e.cmd [参数]
    （或 powershell / pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\windows\e2e.ps1 [参数]）

    所有状态都在仓库下的 .e2e\：config、data、cache、uv tools、logs。环境变量只在本脚本进程里
    设置，结束时还原；不碰真实的 ~/.config/saywell、uv tool 环境和 HKCU Run，运行前后会对比一次。

    第一次运行会把 e2e.config.example.toml 复制成 .e2e\config\saywell\config.toml，用记事本打开，
    然后退出（退出码 2）：在那里填好 API key 后重新运行。

    退出码：0 全部 PASS/SKIP；1 有 FAIL；2 需要先填配置。

.PARAMETER Provider
    临时使用的 provider（例如 claude），不检查 default provider 的 key。
.PARAMETER Clean
    清空沙箱 .e2e\（包括里面填了 key 的 config.toml），然后退出。
.PARAMETER Keep
    跑完后保留沙箱里的 uv tool 安装，方便反复手测。
.PARAMETER SkipTests
    跳过单元测试。
.PARAMETER SkipTray
    跳过托盘手测。
.PARAMETER NonInteractive
    不等待输入、不打开记事本，并隐含 -SkipTray。供 CI 或 agent 自测。
#>
[CmdletBinding()]
param(
    [string]$Provider = '',
    [switch]$Clean,
    [switch]$Keep,
    [switch]$SkipTests,
    [switch]$SkipTray,
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'
$Utf8 = New-Object System.Text.UTF8Encoding($false)
# Chinese output in conhost / Windows Terminal under both PowerShell 5.1 and 7.
try { [Console]::OutputEncoding = $Utf8 } catch { }
$OutputEncoding = $Utf8
if ($NonInteractive) { $SkipTray = [switch]$true }

$Repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$Sandbox = Join-Path $Repo '.e2e'
$Dirs = [ordered]@{
    XDG_CONFIG_HOME = Join-Path $Sandbox 'config'
    XDG_DATA_HOME   = Join-Path $Sandbox 'data'
    XDG_CACHE_HOME  = Join-Path $Sandbox 'cache'
    UV_TOOL_DIR     = Join-Path $Sandbox 'uv\tools'
    UV_TOOL_BIN_DIR = Join-Path $Sandbox 'uv\bin'
}
$LogDir = Join-Path $Sandbox 'logs'
$ConfigFile = Join-Path $Dirs.XDG_CONFIG_HOME 'saywell\config.toml'
$DataDir = Join-Path $Dirs.XDG_DATA_HOME 'saywell'
$BinDir = $Dirs.UV_TOOL_BIN_DIR
$Template = Join-Path $PSScriptRoot 'e2e.config.example.toml'
$Helper = Join-Path $PSScriptRoot 'e2e_helper.py'
$Backend = Join-Path $Repo 'saywell'
$TaskKill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$Sample = 'why the test is fail? check src/auth.ts and 修复 it'

# Every variable this script sets or removes; restored when it ends, so a run inside an
# interactive session leaves that session as it was.
$EnvNames = @($Dirs.Keys) + @('PYTHONUTF8', 'PYTHONIOENCODING', 'CLAUDECODE', 'SAYWELL_PROVIDER', 'SAYWELL_BACKEND', 'USERNAME')
$OrigEnv = @{}
foreach ($n in $EnvNames) { $OrigEnv[$n] = [Environment]::GetEnvironmentVariable($n, 'Process') }
$RealConfigDir = if ($OrigEnv.XDG_CONFIG_HOME) { Join-Path $OrigEnv.XDG_CONFIG_HOME 'saywell' } else { Join-Path $env:USERPROFILE '.config\saywell' }

$script:LogFile = $null
$script:Secrets = @()
$script:Results = New-Object System.Collections.Generic.List[object]
$script:Py = $null
$script:Uv = $null

# ---- output and log ---------------------------------------------------------------

function Hide([string]$Text) {
    foreach ($s in $script:Secrets) { if ($s) { $Text = $Text.Replace($s, '***') } }
    return $Text
}

function Log([string]$Text) {
    if ($script:LogFile) { [IO.File]::AppendAllText($script:LogFile, (Hide $Text) + "`r`n", $Utf8) }
}

function Say([string]$Text = '', [string]$Color = '') {
    $Text = Hide $Text
    if ($Color) { Write-Host $Text -ForegroundColor $Color } else { Write-Host $Text }
    Log $Text
}

function Rel([string]$Path) { if ($Path.StartsWith($Repo + '\')) { return $Path.Substring($Repo.Length + 1) } return $Path }

function Warn([string]$Text) { Say "  警告：$Text" Yellow }

function OneLine([string]$Text, [int]$Max = 90) {
    $t = (($Text -replace '\s+', ' ').Trim())
    if ($t.Length -gt $Max) { $t = $t.Substring(0, $Max) + '…' }
    return $t
}

# ---- processes ----------------------------------------------------------------------

function ConvertTo-Arg([string]$Arg) {
    if ($Arg -ne '' -and $Arg -notmatch '[\s"]') { return $Arg }
    $a = $Arg -replace '(\\*)"', '$1$1\"'
    $a = $a -replace '(\\+)$', '$1$1'
    return '"' + $a + '"'
}

# Run a program with UTF-8 pipes and a timeout; never through PowerShell's own native-command
# handling, whose encodings and stderr records differ between 5.1 and 7.
# $Env: variables to set for the child only ($null removes one).
function Invoke-Native {
    param([string]$File, [string[]]$Arguments = @(), [string]$InputText = $null, [int]$TimeoutSec = 300,
          [hashtable]$Env = @{}, [string]$WorkDir = $Repo, [switch]$NoLog)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $File
    $psi.Arguments = (@($Arguments | ForEach-Object { ConvertTo-Arg $_ }) -join ' ')
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.WorkingDirectory = $WorkDir
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = $Utf8
    $psi.StandardErrorEncoding = $Utf8
    foreach ($k in $Env.Keys) {
        if ($null -eq $Env[$k]) { $psi.EnvironmentVariables.Remove($k) } else { $psi.EnvironmentVariables[$k] = $Env[$k] }
    }
    Log "`$ $File $($psi.Arguments)"
    $p = [Diagnostics.Process]::Start($psi)
    $outTask = $p.StandardOutput.ReadToEndAsync()
    $errTask = $p.StandardError.ReadToEndAsync()
    if ($InputText) {
        $bytes = $Utf8.GetBytes($InputText)
        $p.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
    }
    $p.StandardInput.Close()
    $timedOut = -not $p.WaitForExit($TimeoutSec * 1000)
    if ($timedOut) { Stop-Tree $p.Id }
    $p.WaitForExit()
    $r = [pscustomobject]@{ Code = $p.ExitCode; Out = $outTask.Result; Err = $errTask.Result; TimedOut = $timedOut }
    if ($timedOut) { $r.Err += "`n(超时 $TimeoutSec 秒，已结束)" }
    if (-not $NoLog) {
        Log "  exit $($r.Code)"
        if ($r.Out) { Log ("  stdout:`r`n" + $r.Out.TrimEnd()) }
        if ($r.Err) { Log ("  stderr:`r`n" + $r.Err.TrimEnd()) }
    }
    return $r
}

function Invoke-Py([string[]]$Arguments, [string]$InputText = $null, [int]$TimeoutSec = 120, [hashtable]$Env = @{}, [switch]$NoLog) {
    return Invoke-Native $script:Py.File (@($script:Py.Prefix) + $Arguments) -InputText $InputText -TimeoutSec $TimeoutSec -Env $Env -NoLog:$NoLog
}

function Get-SandboxProcesses {
    $root = $Sandbox + '\'
    @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, [StringComparison]::OrdinalIgnoreCase) })
}

# Tray processes that are not ours: the user's real saywell-tray.
function Get-RealTray {
    $root = $Sandbox + '\'
    @(Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -eq 'saywell-tray.exe' -or ($_.CommandLine -and $_.CommandLine -match 'saywell_tray|saywell_app.*tray')) -and
        -not ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) -and
        $_.ProcessId -ne $PID
    })
}

function Stop-Tree([int]$Id) {
    Invoke-Native $TaskKill @('/T', '/F', '/PID', "$Id") -TimeoutSec 30 | Out-Null
}

function Test-FromExplorer {
    try {
        $me = Get-CimInstance Win32_Process -Filter "ProcessId=$PID"
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($me.ParentProcessId)"
        if ($parent -and $parent.Name -eq 'cmd.exe') {
            $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($parent.ParentProcessId)"
        }
        return [bool]($parent -and $parent.Name -eq 'explorer.exe')
    } catch { return $false }
}

# ---- steps -------------------------------------------------------------------------

function Skip([string]$Why) { throw "SKIP:$Why" }

# $Body returns a one-line detail on PASS, throws on FAIL, or calls Skip.
function Invoke-Step([string]$Name, [scriptblock]$Body) {
    Say ''
    Say "==> $Name" Cyan
    $watch = [Diagnostics.Stopwatch]::StartNew()
    try {
        $out = @(& $Body)
        $status = 'PASS'
        $detail = if ($out.Count) { [string]$out[-1] } else { '' }
    } catch {
        $msg = $_.Exception.Message
        if ($msg.StartsWith('SKIP:')) { $status = 'SKIP'; $detail = $msg.Substring(5) }
        else { $status = 'FAIL'; $detail = $msg; Log ($_ | Out-String) }
    }
    $secs = '{0:0.0}s' -f $watch.Elapsed.TotalSeconds
    $color = @{ PASS = 'Green'; FAIL = 'Red'; SKIP = 'DarkYellow' }[$status]
    Say "[$status] $Name ($secs) $(OneLine $detail 200)" $color
    $script:Results.Add([pscustomobject]@{ Status = $status; Step = $Name; Time = $secs; Detail = (OneLine (Hide $detail)) })
    return ($status -eq 'PASS')
}

function Assert-Run($r, [string]$What) {
    if ($r.Code -ne 0 -or $r.TimedOut) {
        $why = if ($r.Err.Trim()) { $r.Err.Trim() } else { $r.Out.Trim() }
        throw "$What 失败（exit $($r.Code)）：$(OneLine $why 300)"
    }
}

# ---- real user state, compared before and after --------------------------------------

function Get-RealState {
    $config = '(不存在)'
    if (Test-Path -LiteralPath $RealConfigDir) {
        $config = (@(Get-ChildItem -LiteralPath $RealConfigDir -Recurse -File -Force | Sort-Object FullName | ForEach-Object {
            "$($_.FullName)|$($_.Length)|$((Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash)"
        }) -join "`n")
    }
    $uv = '(没有 uv)'
    if ($script:Uv) {
        $r = Invoke-Native $script:Uv @('tool', 'list') -TimeoutSec 60 -NoLog -Env @{
            UV_TOOL_DIR = $OrigEnv.UV_TOOL_DIR; UV_TOOL_BIN_DIR = $OrigEnv.UV_TOOL_BIN_DIR }
        $uv = $r.Out.Trim()
    }
    $run = ''
    if (Test-Path $RunKey) {
        $item = Get-Item $RunKey
        $run = (@($item.GetValueNames() | Sort-Object | ForEach-Object { "$_=$($item.GetValue($_))" }) -join "`n")
    }
    return [pscustomobject]@{ Config = $config; Uv = $uv; Run = $run }
}

function Format-State($s) {
    $files = if ($s.Config -eq '(不存在)') { '不存在' } else { "$(@($s.Config -split "`n").Count) 个文件" }
    $tools = @($s.Uv -split "`n" | Where-Object { $_.Trim() -and $_ -notmatch '^\s*-|No tools installed' }).Count
    $run = if ($s.Run) { @($s.Run -split "`n").Count } else { 0 }
    return "$RealConfigDir：$files；uv tool list：$tools 个工具；HKCU Run：$run 个值"
}

# ---- config ------------------------------------------------------------------------

function Find-Python {
    $candidates = @()
    foreach ($name in 'python', 'python3') {
        $c = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($c) { $candidates += , @{ File = $c.Source; Prefix = @() } }
    }
    $c = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { $candidates += , @{ File = $c.Source; Prefix = @('-3') } }
    foreach ($cand in $candidates) {
        try {
            $r = Invoke-Native $cand.File (@($cand.Prefix) + @('-c', 'import sys; print("%d.%d.%d" % sys.version_info[:3])')) -TimeoutSec 30
        } catch { continue }
        if ($r.Code -eq 0 -and $r.Out.Trim() -match '^(\d+)\.(\d+)\.\d+$' -and ([int]$Matches[1] -gt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 11))) {
            return [pscustomobject]@{ File = $cand.File; Prefix = $cand.Prefix; Version = $r.Out.Trim() }
        }
    }
    return $null
}

function Open-Config {
    if (-not $NonInteractive) { Start-Process notepad.exe -ArgumentList ('"' + $ConfigFile + '"') }
}

function Stop-ForConfig([string]$Why) {
    Say ''
    Say $Why Yellow
    Say "  配置文件：$ConfigFile" Yellow
    Say '  填好 api_key（或改好 default）并保存后，重新运行：' Yellow
    Say '    .\scripts\windows\e2e.cmd' Yellow
    Say '  不用 API key、改用 Claude 订阅：.\scripts\windows\e2e.cmd -Provider claude' Yellow
    if (-not $NonInteractive) { Say '  （已用记事本打开该文件）' Yellow }
    Open-Config
    return 2
}

# The provider this run uses, or an exit code when the config needs the user first.
function Resolve-RunProvider {
    $created = $false
    if (-not (Test-Path -LiteralPath $ConfigFile)) {
        New-Item -ItemType Directory -Force (Split-Path $ConfigFile) | Out-Null
        Copy-Item -LiteralPath $Template -Destination $ConfigFile
        $created = $true
        if (-not $Provider) { return (Stop-ForConfig '首次运行：已从 scripts\windows\e2e.config.example.toml 生成沙箱配置，请先填写 API key。') }
        Say "首次运行：已生成 $ConfigFile；本次用 -Provider $Provider，先不检查 key。" Yellow
    }
    $args_ = @($Helper, 'provider', $ConfigFile)
    if ($Provider) { $args_ += $Provider }
    $r = Invoke-Py $args_
    $info = $null
    try { $info = $r.Out.Trim() | ConvertFrom-Json } catch { }
    if (-not $info -or $info.PSObject.Properties['error']) {
        $why = if ($info) { $info.error } else { OneLine ($r.Err + $r.Out) 300 }
        return (Stop-ForConfig "配置有问题：$why")
    }
    if ($info.key_state -notin 'ok', 'none-needed') {
        $what = switch ($info.key_state) {
            'placeholder' { 'api_key 还是占位符 PASTE_YOUR_API_KEY_HERE' }
            'empty' { 'api_key 是空的' }
            'env-missing' { "环境变量 $($info.api_key_env) 没有设置" }
            default { $info.key_state }
        }
        if (-not $Provider) { return (Stop-ForConfig "default provider「$($info.name)」的 $what。") }
        Warn "provider「$($info.name)」的 $what；按 -Provider 继续，API 调用预计会失败。"
    }
    if ($info.type -ne 'claude-cli') {
        $s = Invoke-Py (@($Helper, 'secret', $ConfigFile) + @($Provider | Where-Object { $_ })) -NoLog
        $key = $s.Out.Trim()
        if ($key.Length -ge 4) { $script:Secrets += $key }
    }
    return $info
}

# ---- the run -----------------------------------------------------------------------

function Invoke-Clean {
    if ((Split-Path $Sandbox -Leaf) -ne '.e2e' -or (Split-Path $Sandbox -Parent) -ne $Repo) { throw "unexpected sandbox path $Sandbox" }
    foreach ($p in Get-SandboxProcesses) {
        Say "结束沙箱里残留的进程 $($p.Name) (PID $($p.ProcessId))"
        Stop-Tree $p.ProcessId
    }
    if (Test-Path -LiteralPath $Sandbox) {
        Remove-Item -LiteralPath $Sandbox -Recurse -Force
        Say "已清空沙箱 $Sandbox（包括其中的 config.toml 和 API key）。" Green
    } else {
        Say "沙箱 $Sandbox 不存在，无需清理。"
    }
    return 0
}

function Invoke-Main {
    if ($Clean) { return (Invoke-Clean) }

    foreach ($d in @($Dirs.Values) + @($LogDir)) { New-Item -ItemType Directory -Force $d | Out-Null }
    $script:LogFile = Join-Path $LogDir ("e2e-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))
    Say "saywell 端到端测试  仓库：$Repo"
    Say "沙箱：$Sandbox    日志：$script:LogFile"
    Log "PowerShell $($PSVersionTable.PSVersion) $($PSVersionTable.PSEdition); args: Provider=$Provider Clean=$Clean Keep=$Keep SkipTests=$SkipTests SkipTray=$SkipTray NonInteractive=$NonInteractive"

    foreach ($k in $Dirs.Keys) { Set-Item "Env:$k" $Dirs[$k] }
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    Remove-Item Env:CLAUDECODE, Env:SAYWELL_BACKEND -ErrorAction SilentlyContinue
    if ($Provider) { $env:SAYWELL_PROVIDER = $Provider } else { Remove-Item Env:SAYWELL_PROVIDER -ErrorAction SilentlyContinue }

    $uvCmd = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($uvCmd) { $script:Uv = $uvCmd.Source }
    $script:Py = Find-Python
    if (-not $script:Py) {
        Say '[FAIL] 找不到 Python 3.11+（试过 python、python3、py -3）。请先安装 Python 3.11 或更新版本。' Red
        return 1
    }

    $info = Resolve-RunProvider
    if ($info -is [int]) { return $info }
    Say "provider：$($info.name)（$($info.type)，模型 $($info.model)）"

    $before = Get-RealState
    Say "运行前：$(Format-State $before)"
    $script:Installed = $false

    # 1. preflight
    $preOk = Invoke-Step '1 环境预检' {
        $v = $PSVersionTable.PSVersion
        if ($v -lt [version]'5.1') { throw "需要 PowerShell 5.1+，当前 $v" }
        Say "  PowerShell $v ($($PSVersionTable.PSEdition))"
        if (-not $script:Uv) { throw '找不到 uv：https://docs.astral.sh/uv/ ；装好后重新打开终端' }
        $r = Invoke-Native $script:Uv @('--version') -TimeoutSec 30
        Assert-Run $r 'uv --version'
        $uvVersion = $r.Out.Trim()
        Say "  $uvVersion  ($script:Uv)"
        Say "  Python $($script:Py.Version)  ($($script:Py.File) $($script:Py.Prefix))"
        $claude = ''
        if ($info.type -eq 'claude-cli') {
            $r = Invoke-Py @($Helper, 'claude', $Backend)
            $c = $null
            try { $c = $r.Out.Trim() | ConvertFrom-Json } catch { }
            if (-not $c -or $c.PSObject.Properties['error']) { throw "找不到 claude.exe：$(if ($c) { $c.error } else { OneLine $r.Err })" }
            $claude = $c.argv[0]
            Say "  claude：$($c.argv -join ' ')"
        } else {
            Say "  API：$($info.base_url)"
        }
        $real = Get-RealTray
        if ($real.Count) {
            Warn "你真实的 saywell-tray 正在运行（PID $(@($real | ForEach-Object { $_.ProcessId }) -join ', ')）。沙箱托盘用独立的单实例名，但 Win+Alt+E 热键只能归一个进程；手测热键前请先退出真实托盘。"
        }
        $detail = "PowerShell $v；$(($uvVersion -split ' ')[0..1] -join ' ')；Python $($script:Py.Version)"
        if ($claude) { $detail += '；claude.exe 已找到' }
        if ($real.Count) { $detail += '；真实托盘在运行（见警告）' }
        $detail
    }

    # 2. unit tests
    Invoke-Step '2 单元测试' {
        if (-not $preOk) { Skip '预检失败' }
        if ($SkipTests) { Skip '-SkipTests' }
        $r = Invoke-Py @('-m', 'unittest', 'discover', 'tests') -TimeoutSec 900 -Env @{ SAYWELL_PROVIDER = $null }
        $lines = @(($r.Err + "`n" + $r.Out) -split "`r?`n" | Where-Object { $_ -match '^(Ran \d+ tests|OK|FAILED)' })
        $summary = $lines -join '，'
        if ($r.Code -ne 0) { throw "python -m unittest discover tests 失败：$summary（详见日志）" }
        $summary
    } | Out-Null

    # 3. install into the sandbox
    $script:Installed = Invoke-Step '3 沙箱 uv tool install' {
        if (-not $preOk) { Skip '预检失败' }
        foreach ($p in Get-SandboxProcesses) {
            Warn "结束上次残留的沙箱进程 $($p.Name) (PID $($p.ProcessId))"
            Stop-Tree $p.ProcessId
        }
        $r = Invoke-Native $script:Uv @('tool', 'install', '--reinstall', $Repo) -TimeoutSec 900
        Assert-Run $r 'uv tool install'
        $missing = @('saywell.exe', 'saywell-tray.exe', 'saywell-autostart.exe' | Where-Object { -not (Test-Path (Join-Path $BinDir $_)) })
        if ($missing.Count) { throw "安装后缺少：$($missing -join ', ')（$BinDir）" }
        "saywell.exe、saywell-tray.exe、saywell-autostart.exe 在 $(Rel $BinDir)"
    }

    # 4. the CLI against the real provider
    $exe = Join-Path $BinDir 'saywell.exe'
    $cliSkip = if (-not $script:Installed) { '没有安装成功' } else { '' }
    # A fresh data dir each run, so the checks below only see what this run wrote.
    if (Test-Path -LiteralPath $DataDir) { Remove-Item -LiteralPath $DataDir -Recurse -Force }
    New-Item -ItemType Directory -Force $DataDir | Out-Null
    $usageFile = Join-Path $DataDir 'usage.jsonl'
    Invoke-Step "4.1 CLI 修正（$($info.name)）" {
        if ($cliSkip) { Skip $cliSkip }
        $r = Invoke-Native $exe @() -InputText ($Sample + "`n") -TimeoutSec 300
        Assert-Run $r 'saywell'
        $fixed = $r.Out.Trim()
        if (-not $fixed) { throw '输出为空' }
        # A fix is about as long as the draft; a much longer reply is the model answering
        # something else (e.g. echoing claude -p's environment section).
        if ($fixed.Length -gt 3 * $Sample.Length + 80) { throw "回复不像修正结果（$($fixed.Length) 个字符）：$(OneLine $fixed 120)" }
        "「$Sample」→「$fixed」"
    } | Out-Null
    Invoke-Step '4.2 CLI --json' {
        if ($cliSkip) { Skip $cliSkip }
        $r = Invoke-Native $exe @('--json') -InputText $Sample -TimeoutSec 300
        Assert-Run $r 'saywell --json'
        $last = @($r.Out -split "`r?`n" | Where-Object { $_.Trim() })
        if (-not $last.Count) { throw '输出为空' }
        try { $j = $last[-1] | ConvertFrom-Json } catch { throw "输出不是合法 JSON：$(OneLine $last[-1])" }
        if (-not $j.done -or -not $j.fixed) { throw "JSON 缺少 done/fixed：$(OneLine $last[-1])" }
        "合法 JSON；fixed：$($j.fixed)"
    } | Out-Null
    Invoke-Step '4.3 CLI --learn' {
        if ($cliSkip) { Skip $cliSkip }
        $r = Invoke-Native $exe @('--learn') -TimeoutSec 600
        Assert-Run $r 'saywell --learn'
        $last = @($r.Out.Trim() -split "`r?`n")[-1]
        $calls = 0
        if (Test-Path $usageFile) { $calls = @(Get-Content -LiteralPath $usageFile -Encoding UTF8 | Where-Object { $_ -match '"kind": "learn"' }).Count }
        if (-not $calls) { throw "--learn 没有调用模型（$last）；4.1/4.2 失败时没有可学习的记录" }
        $last
    } | Out-Null
    Invoke-Step '4.4 CLI --usage' {
        if ($cliSkip) { Skip $cliSkip }
        $r = Invoke-Native $exe @('--usage') -TimeoutSec 60
        Assert-Run $r 'saywell --usage'
        if ($r.Out -match 'no usage logged yet') { throw '没有任何 usage 记录' }
        foreach ($line in ($r.Out.TrimEnd() -split "`r?`n")) { Say "  $line" }
        '已汇总 usage'
    } | Out-Null
    Invoke-Step '4.5 沙箱 data 目录' {
        if ($cliSkip) { Skip $cliSkip }
        $found = @('log.jsonl', 'usage.jsonl', 'notes.jsonl' | Where-Object {
            $f = Join-Path $DataDir $_; (Test-Path $f) -and (Get-Item $f).Length -gt 0 })
        if ('log.jsonl' -notin $found -and 'usage.jsonl' -notin $found) { throw "$DataDir 里没有 log.jsonl 或 usage.jsonl" }
        "$(Rel $DataDir)：$($found -join '、')"
    } | Out-Null

    # 5. tray, by hand
    Invoke-Step '5 托盘手测' {
        if ($NonInteractive) { Skip '-NonInteractive' }
        if ($SkipTray) { Skip '-SkipTray' }
        if ($cliSkip) { Skip $cliSkip }
        $trayExe = Join-Path $BinDir 'saywell-tray.exe'
        $trayLog = Join-Path $DataDir 'tray.log'
        Remove-Item (Join-Path $DataDir 'tray.log*') -Force -ErrorAction SilentlyContinue
        # The single-instance mutex and pipe are named after USERNAME: a sandbox-only name keeps
        # this tray from handing over to (and opening the popup of) a real tray that is running.
        $env:USERNAME = "$($OrigEnv.USERNAME)-e2e"
        try { $proc = Start-Process -FilePath $trayExe -WorkingDirectory $Sandbox -PassThru }
        finally { $env:USERNAME = $OrigEnv.USERNAME }
        Say "  已启动 $trayExe（PID $($proc.Id)）"
        try {
            $deadline = (Get-Date).AddSeconds(45)
            $started = $false
            while ((Get-Date) -lt $deadline -and -not $proc.HasExited) {
                if ((Test-Path $trayLog) -and (Get-Content -LiteralPath $trayLog -Raw -Encoding UTF8) -match 'started \(pid') { $started = $true; break }
                Start-Sleep -Milliseconds 500
            }
            if ($proc.HasExited) { throw "托盘进程已退出（exit $($proc.ExitCode)）" }
            if (-not $started) { throw "45 秒内 $trayLog 没有出现 started 记录" }
            Say "  PASS 进程在运行，tray.log 已生成：$trayLog" Green
            if ((Get-Content -LiteralPath $trayLog -Raw -Encoding UTF8) -match 'not registered') {
                Warn '热键没注册上（多半被真实托盘或别的程序占用），见 tray.log。手测热键前先退出占用的程序。'
            }
            Say ''
            Say '  请手测（沙箱托盘，图标是 "S"）：' Cyan
            Say '   1. 打开记事本，点进编辑区，按 Win+Alt+E：弹窗出现在鼠标所在屏幕中央。'
            Say "   2. 输入一句中英混合的草稿（如：$Sample），按 Ctrl+Enter 修正。"
            Say '   3. 看三个版本和含义回译；按 Alt+1/2/3 选版本，按 Ctrl+Shift+Enter（或点 Insert）。'
            Say '   4. 确认文字粘贴回记事本原来的光标处。'
            Say '   5. 在 Windows Terminal 里重复 1–4：应粘贴到命令行（用 Ctrl+Shift+V），且不会自动回车。'
            Say '   6. 弹窗里在追问框输入问题回车，确认有中文回答；按 Esc 关闭弹窗不粘贴。'
            Say '   7. 右键托盘图标，逐项点：Open Saywell（打开弹窗）、Learn now（tray.log 记一条 learn）。'
            Say '   8. 不要点 Start at logon；如果点了，请再点一次关掉（脚本最后会检查 HKCU Run）。'
            Say '   9. 不要点 Quit，留给脚本结束。'
            Say ''
            Say "  手测完成后按回车，脚本会结束托盘进程（PID $($proc.Id)）……" Cyan
            [void][Console]::ReadLine()
        } finally {
            if (-not $proc.HasExited) { Stop-Tree $proc.Id }
            [void]$proc.WaitForExit(15000)
        }
        if (-not $proc.HasExited) { throw "托盘进程 PID $($proc.Id) 没能结束" }
        $left = @(Get-SandboxProcesses)
        if ($left.Count) { throw "还有沙箱进程没结束：$(@($left | ForEach-Object { "$($_.Name)($($_.ProcessId))" }) -join ', ')" }
        if (Test-Path $trayLog) { Log ("tray.log:`r`n" + (Get-Content -LiteralPath $trayLog -Raw -Encoding UTF8)) }
        "PID $($proc.Id) 启动、tray.log 生成、已结束"
    } | Out-Null

    # 6. uninstall
    Invoke-Step '6 清理沙箱安装' {
        if ($Keep) { Skip "-Keep：保留在 $BinDir（再跑一次不带 -Keep，或 -Clean，即可清掉）" }
        if (-not (Test-Path (Join-Path $Dirs.UV_TOOL_DIR 'saywell'))) { Skip '沙箱里没有安装' }
        $r = Invoke-Native $script:Uv @('tool', 'uninstall', 'saywell') -TimeoutSec 120
        Assert-Run $r 'uv tool uninstall saywell'
        if (Test-Path (Join-Path $BinDir 'saywell.exe')) { throw "卸载后 $BinDir\saywell.exe 还在" }
        '已从沙箱卸载 saywell'
    } | Out-Null

    # 7. the user's real state is untouched
    Invoke-Step '7 真实环境未改动' {
        $after = Get-RealState
        Say "  运行后：$(Format-State $after)"
        $changed = @()
        if ($after.Config -ne $before.Config) { $changed += $RealConfigDir }
        if ($after.Uv -ne $before.Uv) { $changed += 'uv tool list' }
        if ($after.Run -ne $before.Run) { $changed += 'HKCU Run' }
        if ($changed.Count) { throw "有变化：$($changed -join '、')" }
        "$RealConfigDir、uv tool list、HKCU Run 与运行前一致"
    } | Out-Null

    Say ''
    Say '==== 汇总 ====' Cyan
    foreach ($r in $script:Results) {
        $color = @{ PASS = 'Green'; FAIL = 'Red'; SKIP = 'DarkYellow' }[$r.Status]
        Say ('{0,-4}  {1,7}  {2}  {3}' -f $r.Status, $r.Time, $r.Step, $r.Detail) $color
    }
    $fails = @($script:Results | Where-Object { $_.Status -eq 'FAIL' }).Count
    Say ''
    if ($fails) {
        Say "$fails 项 FAIL。完整日志：$script:LogFile" Red
        return 1
    }
    Say "全部通过（SKIP 的除外）。完整日志：$script:LogFile" Green
    return 0
}

$code = 1
try {
    $code = @(Invoke-Main)[-1]
} catch {
    Say "出错：$($_.Exception.Message)" Red
    Log ($_ | Out-String)
    $code = 1
} finally {
    foreach ($n in $EnvNames) { [Environment]::SetEnvironmentVariable($n, $OrigEnv[$n], 'Process') }
}
if (-not $NonInteractive -and (Test-FromExplorer)) {
    Write-Host ''
    Write-Host '按回车关闭窗口……'
    [void][Console]::ReadLine()
}
exit $code
