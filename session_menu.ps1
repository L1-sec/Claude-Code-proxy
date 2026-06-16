# -*- coding: utf-8 -*-
param(
    [ValidateSet("Codex", "Claude")]
    [string]$Tool = "Codex"
)

$ErrorActionPreference = "Stop"
try { $Host.UI.RawUI.CursorSize = 1 } catch {}

$CodexDir = "C:\Users\user\Desktop\Codex"
$ClaudeDir = "C:\Users\user\Desktop\Claude Code"
$CodexSessionsDir = "$env:USERPROFILE\.codex\sessions"
$ClaudeProjectsDir = "$env:USERPROFILE\.claude\projects"
$ClaudeHistoryFile = "$env:USERPROFILE\.claude\history.jsonl"
$ClaudeBin = "$env:USERPROFILE\AppData\Roaming\npm\claude.ps1"

$ToolDir = if ($Tool -eq "Codex") { $CodexDir } else { $ClaudeDir }

function Get-CodexSessions {
    if (!(Test-Path $CodexSessionsDir)) { return @() }
    $files = Get-ChildItem -Path $CodexSessionsDir -Recurse -Filter "*.jsonl" -File | Sort-Object LastWriteTime -Descending | Select-Object -First 10
    $sessions = @()
    foreach ($f in $files) {
        if ($f.BaseName -match 'rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-([a-f0-9-]{36})$') {
            $sid = $matches[1]
            $dt = $f.LastWriteTime.ToString("yyyy-MM-dd HH:mm")
            $q = Get-FirstQuestionCodex $f.FullName
            $sk = $f.LastWriteTime.ToString("yyyyMMddHHmmss")
            $sessions += [PSCustomObject]@{ SessionId = $sid; Timestamp = $dt; Question = $q; File = $f.FullName; SortKey = $sk }
        }
    }
    return $sessions | Sort-Object SortKey -Descending
}

function Get-FirstQuestionCodex($path) {
    try {
        $r = [System.IO.StreamReader]::new($path, [Text.Encoding]::UTF8)
        $c = 0
        while (!$r.EndOfStream -and $c -lt 100) {
            $l = $r.ReadLine(); $c++
            if ($l -match '"type"\s*:\s*"user_message"') {
                $m = [regex]::Match($l, '"message"\s*:\s*"((?:[^"\\]|\\.)*)"')
                if ($m.Success) {
                    $q = $m.Groups[1].Value
                    if ($q.Length -gt 50) { $q = $q.Substring(0, 47) + "..." }
                    $r.Close()
                    if (![string]::IsNullOrWhiteSpace($q)) { return $q }
                }
            }
        }
        $r.Close()
    } catch {}
    return ""
}

function Get-ClaudeSessions {
    $sessions = @(); $seen = @{}
    if (Test-Path $ClaudeHistoryFile) {
        try {
            foreach ($l in (Get-Content $ClaudeHistoryFile -Encoding UTF8)) {
                try {
                    $o = $l | ConvertFrom-Json
                    if ($o.sessionId -and $o.timestamp -and $o.display -and !$seen.ContainsKey($o.sessionId)) {
                        $seen[$o.sessionId] = $true
                        $ts = [DateTimeOffset]::FromUnixTimeMilliseconds([long]$o.timestamp)
                        $d = $o.display; if ($d.Length -gt 50) { $d = $d.Substring(0, 47) + "..." }
                        $sk = $ts.ToString("yyyyMMddHHmmssfff")
                        $sessions += [PSCustomObject]@{ SessionId = $o.sessionId; Timestamp = $ts.ToString("yyyy-MM-dd HH:mm"); Question = $d; File = ""; SortKey = $sk }
                    }
                } catch {}
            }
        } catch {}
    }
    $projDir = "$ClaudeProjectsDir\C--Users-user-Desktop-Claude-Code"
    if (Test-Path $projDir) {
        $seen2 = @{}
        foreach ($f in (Get-ChildItem -Path $projDir -Filter "*.jsonl" -File | Sort-Object LastWriteTime -Descending | Select-Object -First 10)) {
            if ($f.BaseName -match '^[a-f0-9-]{36}$' -and !$seen.ContainsKey($f.BaseName) -and !$seen2.ContainsKey($f.BaseName)) {
                $seen2[$f.BaseName] = $true
                $q = Get-FirstQuestionClaude $f.FullName
                $dt = $f.LastWriteTime.ToString("yyyy-MM-dd HH:mm")
                $sk = $f.LastWriteTime.ToString("yyyyMMddHHmmss")
                $sessions += [PSCustomObject]@{ SessionId = $f.BaseName; Timestamp = $dt; Question = $q; File = $f.FullName; SortKey = $sk }
            }
        }
    }
    $all = $sessions | Sort-Object SortKey -Descending | Select-Object -First 10
    $unique = @{}; $result = @()
    foreach ($s in $all) {
        if (!$unique.ContainsKey($s.SessionId)) { $unique[$s.SessionId] = $true; $result += $s }
    }
    return $result
}

function Get-FirstQuestionClaude($path) {
    try {
        $r = [System.IO.StreamReader]::new($path, [Text.Encoding]::UTF8)
        $c = 0
        while (!$r.EndOfStream -and $c -lt 50) {
            $l = $r.ReadLine(); $c++
            if ($l -match '"type"\s*:\s*"user"') {
                $m = [regex]::Match($l, '"content"\s*:\s*"((?:[^"\\]|\\.)*)"')
                if ($m.Success) {
                    $q = $m.Groups[1].Value
                    if ($q.Length -gt 50) { $q = $q.Substring(0, 47) + "..." }
                    $r.Close()
                    if (![string]::IsNullOrWhiteSpace($q)) { return $q }
                }
            }
        }
        $r.Close()
    } catch {}
    return ""
}

function Show-Menu {
    param($items, $title)
    $current = 0; $selected = $null
    $maxLines = $Host.UI.RawUI.WindowSize.Height - 5
    $scrollOffset = 0
    $lines = @()
    $lines += "[1] New Session"
    for ($i = 0; $i -lt $items.Count; $i++) {
        $it = $items[$i]
        $q = $it.Question
        if ([string]::IsNullOrWhiteSpace($q)) { $q = "(empty)" }
        $s = "[" + ($i + 2) + "] " + $it.Timestamp + " | " + $q
        $lines += $s
    }
    while ($true) {
        if ($current -lt $scrollOffset) { $scrollOffset = $current }
        if ($current -ge $scrollOffset + $maxLines) { $scrollOffset = $current - $maxLines + 1 }
        $host.UI.RawUI.FlushInputBuffer()
        try { [Console]::CursorVisible = $false; [Console]::SetCursorPosition(0, 0) } catch {}
        $helpText = if ($selected -ne $null) { "[Enter] confirm | [Arrow/ESC] change" } else { "[Arrow] move | [Enter] select | [Enter] again launch" }
        Write-Host ("  " + $helpText) -ForegroundColor DarkGray
        Write-Host ("  " + $title) -ForegroundColor Cyan
        Write-Host ""
        $endIdx = [Math]::Min($scrollOffset + $maxLines, $lines.Count)
        $gi = $scrollOffset
        for ($ri = $scrollOffset; $ri -lt $endIdx; $ri++) {
            $dl = $lines[$ri]
            $prefix = "   "
            $fg = "White"
            $bg = [ConsoleColor]::Black
            if ($gi -eq $current) {
                if ($selected -ne $null -and $selected -eq $gi) {
                    $prefix = ">> "
                    $fg = "Black"; $bg = [ConsoleColor]::Green
                } else {
                    $prefix = " > "
                    $fg = "Yellow"
                }
            } elseif ($selected -ne $null -and $selected -eq $gi) {
                $prefix = "   "
                $fg = "DarkGray"
            }
            Write-Host $prefix -NoNewline
            if ($bg -ne [ConsoleColor]::Black) {
                Write-Host $dl -ForegroundColor $fg -BackgroundColor $bg
            } else {
                Write-Host $dl -ForegroundColor $fg
            }
            $gi++
        }
        $remaining = $maxLines - ($endIdx - $scrollOffset)
        for ($i = 0; $i -lt $remaining; $i++) { Write-Host (" " * 80) }
        $key = $Host.UI.RawUI.ReadKey("IncludeKeyDown,NoEcho")
        $vk = $key.VirtualKeyCode
        if ($vk -eq 38) {
            if ($selected -ne $null) { $selected = $null }
            if ($current -gt 0) { $current-- } else { $current = $lines.Count - 1 }
        } elseif ($vk -eq 40) {
            if ($selected -ne $null) { $selected = $null }
            if ($current -lt $lines.Count - 1) { $current++ } else { $current = 0 }
        } elseif ($vk -eq 13) {
            if ($selected -eq $null) {
                $selected = $current
            } elseif ($selected -eq $current) {
                return $selected
            } else {
                $selected = $current
            }
        } elseif ($vk -eq 27) {
            if ($selected -ne $null) { $selected = $null } else { return -1 }
        } elseif ($vk -eq 33) {
            if ($selected -ne $null) { $selected = $null }
            $current = [Math]::Max(0, $current - $maxLines)
        } elseif ($vk -eq 34) {
            if ($selected -ne $null) { $selected = $null }
            $current = [Math]::Min($lines.Count - 1, $current + $maxLines)
        }
    }
}

function Launch-Tool($choice) {
    Clear-Host
    $proxyPy = if ($Tool -eq "Codex") { "codex_proxy.py" } else { "Claude_Code_proxy.py" }
    $proxyTitle = $proxyPy
    Start-Process -WindowStyle Minimized -FilePath "cmd.exe" -ArgumentList "/c start `"$proxyTitle`" cmd /k py -3.10 `"$ToolDir\$proxyPy`""
    Start-Sleep -Milliseconds 500
    Push-Location $ToolDir
    try {
        if ($choice -eq 0) {
            if ($Tool -eq "Codex") {
                & "$ToolDir\codex.exe"
            } else {
                $env:ANTHROPIC_BASE_URL = "http://127.0.0.1:5000"
                $env:ANTHROPIC_AUTH_TOKEN = "dummy"
                $env:ANTHROPIC_MODEL = "deepseek-v4-flash"
                & $ClaudeBin
            }
        } else {
            $idx = $choice - 1
            if ($Tool -eq "Codex") {
                $sessions = Get-CodexSessions
            } else {
                $sessions = Get-ClaudeSessions
            }
            $session = $sessions[$idx]
            if ($Tool -eq "Codex") {
                & "$ToolDir\codex.exe" resume $session.SessionId
            } else {
                $env:ANTHROPIC_BASE_URL = "http://127.0.0.1:5000"
                $env:ANTHROPIC_AUTH_TOKEN = "dummy"
                $env:ANTHROPIC_MODEL = "deepseek-v4-flash"
                & $ClaudeBin --resume $session.SessionId
            }
        }
    } finally {
        Pop-Location
    }
}

try {
    if ($Tool -eq "Codex") {
        $title = "=== Codex Session Selector ==="
    } else {
        $title = "=== Claude Code Session Selector ==="
    }
    $toolExe = if ($Tool -eq "Codex") { "$ToolDir\codex.exe" } else { $ClaudeBin }
    if (!(Test-Path $toolExe)) {
        Write-Host ("Error: not found -> " + $toolExe) -ForegroundColor Red
        pause; exit 1
    }
    Write-Host ("scanning sessions...") -ForegroundColor Cyan
    $sessions = if ($Tool -eq "Codex") { Get-CodexSessions } else { Get-ClaudeSessions }
    Write-Host ("found " + $sessions.Count + " sessions") -ForegroundColor Green
    Start-Sleep -Milliseconds 800
    $choice = Show-Menu -items $sessions -title $title
    if ($choice -eq -1) {
        Write-Host ("cancelled") -ForegroundColor Yellow
        pause; exit 0
    }
    Write-Host ("launching...") -ForegroundColor Cyan
    Launch-Tool -choice $choice
    Write-Host ("session ended. press any key...")
    pause
} catch {
    Write-Host ("Error: " + $_.Exception.Message) -ForegroundColor Red
    pause
}
