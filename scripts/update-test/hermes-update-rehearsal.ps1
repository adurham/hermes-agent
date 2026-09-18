<#
.SYNOPSIS
  hermes-update-rehearsal.ps1 -- for an EXISTING Hermes install (Windows).

.DESCRIPTION
  Two steps:

    pre   back up everything, then point the install's update source at a
          custom repo + ref so `hermes update` pulls it. Prints what to do next.
    post  undo all of it: remove the redirect, wipe, and restore the backup.

  Plus `status`, which only prints. This script never judges your install: it
  reports what it did and stops. Whether the update worked is yours to see.

  Read PLAN.md (next to this script) first.

.PARAMETER Command
  pre | post | status

.PARAMETER Source
  Repo to pull the update from (default: the rehearsal fork).

.PARAMETER Ref
  Branch or tag in that repo (default: main).

.PARAMETER BackupRoot
  Where the backup lives (default: $HOME\hermes-update-rehearsal).

.PARAMETER Yes
  post: skip the confirmation.

.EXAMPLE
  ./hermes-update-rehearsal.ps1 pre --source <git-url> --ref <branch>
  # ... run `hermes update`, use Hermes, test ...
  ./hermes-update-rehearsal.ps1 post

.NOTES
  The backup is the ENTIRE HERMES_HOME plus the desktop app's Electron userData,
  the `hermes` shims on PATH, your USER PATH value, and your global git config.
  `pre` needs network access to -Source and a usable git on PATH.
#>
[CmdletBinding()]
param(
  [Parameter(Position = 0)]
  [ValidateSet('pre', 'post', 'status', 'help')]
  [string]$Command = 'help',

  [string]$Source = 'https://github.com/ethernet8023/hermes-agent.git',
  [string]$Ref = 'main',
  [string]$BackupRoot,
  [switch]$Yes
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$OfficialHttps = 'https://github.com/NousResearch/hermes-agent.git'
$OfficialSsh = 'git@github.com:NousResearch/hermes-agent.git'

# The durable state `post` reports on after restoring. Regenerable trees
# (caches, logs, dependency dirs) are deliberately absent, and `skills/` is
# reported but never judged: the product syncs the bundled library into it on
# startup and after every update, so it changes legitimately.
$DurableTop = @('config.yaml', '.env', 'auth.json', 'state.db', 'gateway_state.json',
  'memories', 'skills', 'cron', 'plugins', 'photon', 'desktop-plugins',
  'tui-widgets', 'skins', 'pets', 'sessions', 'profiles')

$SkipDirs = @('node_modules', '.venv', 'venv', 'site-packages', '__pycache__', '.git',
  '.cache', '.tox', '.nox', '.pytest_cache', '.mypy_cache', '.ruff_cache',
  'backups', 'state-snapshots', 'checkpoints', 'hermes-agent',
  'browser-profiles', 'browser-profile', 'models', 'runtimes', 'node')
$SkipSuffixes = @('.pyc', '.pyo', '.db-wal', '.db-shm', '.db-journal')

$script:Snap = ''
$script:Armed = ''
$script:Tar = $null
$script:DbCountPy = $null
$script:LastGitExit = $null

function Say  { param([string]$m) Write-Host $m }
function Ok   { param([string]$m) Write-Host "  OK $m" }
function Warn { param([string]$m) Write-Warning "  $m" }
function Step { param([string]$m) Write-Host "`n=== $m ===" }
function Fail { param([string]$m) throw "ERROR: $m" }

function Resolve-Tar {
  # A PATH pointing into a WindowsApps payload (the bundled app's toolchain)
  # yields a tar.exe PowerShell cannot launch, so prefer the real one.
  $cands = @()
  if ($env:SystemRoot) {
    $cands += (Join-Path $env:SystemRoot 'System32\tar.exe')
    $cands += (Join-Path $env:SystemRoot 'Sysnative\tar.exe')
  }
  $onPath = Get-Command tar.exe -ErrorAction SilentlyContinue
  if ($onPath) { $cands += $onPath.Source }
  foreach ($c in $cands) {
    if (-not $c) { continue }
    if ($c -like '*\Microsoft\WindowsApps\*') { continue }
    if (-not (Test-Path -LiteralPath $c)) { continue }
    try { & $c --version *> $null; if ($LASTEXITCODE -eq 0) { return $c } }
    catch { }
  }
  return $null
}

# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

function Test-TreeAccess {
  param([string]$Root, [string]$Label)
  Step "checking permissions on $Label"
  $user = "$env:USERDOMAIN\$env:USERNAME"
  $denied = icacls $Root /T 2>&1 |
    Where-Object { $_ -match ': Access is denied\.$' } |
    ForEach-Object { ($_ -replace ': Access is denied\.$', '').Trim() }

  if ($denied) {
    Warn "$($denied.Count) path(s) under $Root are not readable by $user`:"
    $denied | ForEach-Object { Write-Host "    $_" }
    Say ''
    Say 'run this in an elevated (Run as Administrator) PowerShell, then re-run pre:'
    Say "  icacls `"$Root`" /grant `"$user`:(OI)(CI)F`" /t /c"
    Fail "$($denied.Count) unreadable path(s) under $Root -- fix with the command above"
  }
  Ok "all paths under $Root are readable"
}

function Get-ResolvedPaths {
  $suffix = if ($env:HERMES_DATA_DIR_SUFFIX) { $env:HERMES_DATA_DIR_SUFFIX } else { '' }
  $userProfile = $env:USERPROFILE
  if ($env:HERMES_HOME) {
    # Must tolerate a MISSING home: post resolves paths in order to recreate them.
    $home_ = $env:HERMES_HOME
    try { $home_ = (Resolve-Path -LiteralPath $home_ -ErrorAction Stop).Path }
    catch { $home_ = [IO.Path]::GetFullPath($home_) }
  }
  else {
    $base = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $userProfile 'AppData\Local' }
    $home_ = "$(Join-Path $base 'hermes')$suffix"
  }
  $parent = Split-Path -Parent $home_
  if ((Split-Path -Leaf $parent) -ieq 'profiles') { $root = Split-Path -Parent $parent } else { $root = $home_ }
  $install = Join-Path $home_ 'hermes-agent'
  if ($env:HERMES_DESKTOP_USER_DATA_DIR) {
    $ud = $env:HERMES_DESKTOP_USER_DATA_DIR
    try { $ud = (Resolve-Path -LiteralPath $ud -ErrorAction Stop).Path }
    catch { $ud = [IO.Path]::GetFullPath($ud) }
    $userData = $ud
    $userDataSource = 'env'
  }
  else {
    $appData = if ($env:APPDATA) { $env:APPDATA } else { Join-Path $userProfile 'AppData\Roaming' }
    $userData = Join-Path $appData "Hermes$suffix"
    $userDataSource = 'default'
  }
  # install.ps1 publishes into <home>\bin and wires the USER PATH to it; the
  # store/venv launchers live in the checkout's .hermes\bin.
  $shims = @(
    (Join-Path $install '.hermes\bin\hermes.exe'), (Join-Path $install '.hermes\bin\hermes.cmd'),
    (Join-Path $install '.hermes\bin\hermes-acp.exe'), (Join-Path $install '.hermes\bin\hermes-acp.cmd'),
    (Join-Path $home_ 'bin\hermes.exe'), (Join-Path $home_ 'bin\hermes.cmd'),
    (Join-Path $home_ 'bin\hermes-acp.exe'), (Join-Path $home_ 'bin\hermes-acp.cmd'),
    (Join-Path $userProfile '.local\bin\hermes'), (Join-Path $root 'bin\hermes.exe'),
    (Join-Path $root 'bin\hermes.cmd')
  )
  return [pscustomobject]@{
    Home = $home_; Root = $root; Install = $install
    UserData = $userData; UserDataOrigin = $userDataSource; Shims = $shims
  }
}

function Get-UserPathValue {
  try { return [Environment]::GetEnvironmentVariable('Path', 'User') } catch { return $null }
}

function Get-LatestSnapshot {
  if (-not (Test-Path -LiteralPath $BackupRoot)) { return $null }
  $dirs = Get-ChildItem -LiteralPath $BackupRoot -Directory -ErrorAction SilentlyContinue | Sort-Object Name
  if (-not $dirs) { return $null }
  return $dirs[-1].FullName
}

function Load-Snapshot {
  $snap = Get-LatestSnapshot
  if (-not $snap) { Fail "no backup found under $BackupRoot -- run 'pre' first" }
  $script:Snap = $snap
  $script:Armed = Join-Path $snap 'armed'
  $manifestPath = Join-Path $snap 'manifest.json'
  if (-not (Test-Path -LiteralPath $manifestPath)) { Fail "$snap is not a rehearsal backup (no manifest.json)" }
  $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
  $P = Get-ResolvedPaths
  if ($manifest.hermes_home -ne $P.Home) {
    Fail "that backup belongs to HERMES_HOME=$($manifest.hermes_home), not $($P.Home); pass -BackupRoot to pick the right one"
  }
  return $manifest
}

# ---------------------------------------------------------------------------
# git helpers (stdout only: merging stderr folds git warnings into the value)
# ---------------------------------------------------------------------------

function Invoke-Git {
  param($P, [string[]]$GitArgs)
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $out = & git -C $P.Install @GitArgs 2>$null
    $script:LastGitExit = $LASTEXITCODE
  }
  finally { $ErrorActionPreference = $prev }
  return (($out | Out-String) -replace "`r", '').Trim()
}

function Get-GitConfigValue {
  param($P, [string]$Key)
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $out = & git -C $P.Install config --get $Key 2>$null
    if ($LASTEXITCODE -ne 0) { return '' }
  }
  finally { $ErrorActionPreference = $prev }
  return (($out | Out-String) -replace "`r", '').Trim()
}

function Invoke-GitCmd {
  param([string[]]$GitArgs)
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try { & git @GitArgs 2>$null } finally { $ErrorActionPreference = $prev }
}

# ---------------------------------------------------------------------------
# fingerprints (used only to report how exact a restore was)
# ---------------------------------------------------------------------------

function Find-SqlitePython {
  param($P)
  $cands = @(
    (Join-Path $P.Install 'venv\Scripts\python.exe'),
    (Join-Path $P.Install '.venv\Scripts\python.exe')
  )
  $store = Get-ChildItem -LiteralPath (Join-Path $P.Install '.hermes-runtime\tools') -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like 'python-*' }
  foreach ($s in $store) { $cands += (Join-Path $s.FullName 'python.exe') }
  $cands += (Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
  foreach ($c in $cands) {
    if (-not $c) { continue }
    # The Store alias resolves to a stub PowerShell cannot launch.
    if ($c -like '*\Microsoft\WindowsApps\*') { continue }
    if (Test-Path -LiteralPath $c) {
      try { & $c -c 'import sqlite3' 2>&1 | Out-Null; if ($LASTEXITCODE -eq 0) { return $c } }
      catch { }
    }
  }
  return $null
}

function Get-DbCountsPythonPath {
  # A temp FILE, not `-c`: a multi-line -c argument gets reshaped by
  # PowerShell's native-argument handling and fails to parse.
  if (-not $script:DbCountPy) {
    $script:DbCountPy = Join-Path ([IO.Path]::GetTempPath()) 'hermes-rehearsal-dbcount.py'
    @'
import sqlite3, sys
try:
    con = sqlite3.connect("file:" + sys.argv[1].replace("\\", "/") + "?mode=ro", uri=True)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    parts = [f"{t}={con.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0]}"
             for t in sorted(tables & {"sessions", "messages", "usage", "cron_jobs"})]
    con.close()
    print(";".join(parts) if parts else "")
except Exception:
    print("")
'@ | Set-Content -LiteralPath $script:DbCountPy -Encoding ASCII
  }
  return $script:DbCountPy
}

function Get-DbCounts {
  param([string]$Python, [string]$DbPath)
  if (-not $Python) { return $null }
  $prog = Get-DbCountsPythonPath
  try {
    $out = & $Python $prog $DbPath 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
  }
  catch { return $null }
  $text = ($out | Out-String).Trim()
  if ($text) { return $text }
  return $null
}

function Get-RelForward {
  param([string]$Root, [string]$Path)
  return $Path.Substring($Root.Length).TrimStart('\', '/').Replace('\', '/')
}

function Add-TreeEntries {
  param([string]$Root, [string]$Dir, $Lines, [string]$Python)
  $stack = New-Object System.Collections.Stack
  $stack.Push($Dir)
  while ($stack.Count -gt 0) {
    $cur = $stack.Pop()
    $kept = @()
    foreach ($it in @(Get-ChildItem -LiteralPath $cur -Force -ErrorAction SilentlyContinue)) {
      $rel = Get-RelForward -Root $Root -Path $it.FullName
      if ($it.PSIsContainer) {
        if ($SkipDirs -contains $it.Name) { continue }
        if ($it.Attributes -band [IO.FileAttributes]::ReparsePoint) {
          $Lines.Add("link`t$rel`t$($it.Target)")   # record, never descend
          continue
        }
        $kept += $it
      }
      else {
        $skip = $false
        foreach ($sfx in $SkipSuffixes) { if ($it.Name.EndsWith($sfx)) { $skip = $true; break } }
        if ($skip) { continue }
        if ($it.Name -eq 'state.db') {
          $counts = Get-DbCounts -Python $Python -DbPath $it.FullName
          if ($counts) { $Lines.Add("db`t$counts`t$rel") }
          else { $Lines.Add("file`t$rel`t$((Get-FileHash -LiteralPath $it.FullName -Algorithm SHA256).Hash.ToLower())") }
        }
        else {
          $Lines.Add("file`t$rel`t$((Get-FileHash -LiteralPath $it.FullName -Algorithm SHA256).Hash.ToLower())")
        }
        $kept += $it
      }
    }
    if ($kept.Count -eq 0) { $Lines.Add("dir`t" + (Get-RelForward -Root $Root -Path $cur)) }
    foreach ($k in $kept) { if ($k.PSIsContainer) { $stack.Push($k.FullName) } }
  }
}

function Get-TreeFingerprint {
  param($P, [string]$Out)
  $python = Find-SqlitePython $P
  $lines = New-Object System.Collections.Generic.List[string]
  foreach ($entry in $DurableTop) {
    $topPath = Join-Path $P.Home $entry
    if (-not (Test-Path -LiteralPath $topPath)) { continue }
    $item = Get-Item -LiteralPath $topPath -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
      $lines.Add("link`t$entry`t$($item.Target)"); continue
    }
    if ($item.PSIsContainer) {
      $lines.Add("dir`t$entry")
      Add-TreeEntries -Root $P.Home -Dir $topPath -Lines $lines -Python $python
    }
    elseif ($item.Name -eq 'state.db') {
      $counts = Get-DbCounts -Python $python -DbPath $item.FullName
      if ($counts) { $lines.Add("db`t$counts`t$entry") }
      else { $lines.Add("file`t$entry`t$((Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLower())") }
    }
    else {
      $lines.Add("file`t$entry`t$((Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLower())")
    }
  }
  Set-Content -LiteralPath $Out -Value ($lines | Sort-Object) -Encoding utf8
  Write-Host "  $($lines.Count) state entries fingerprinted"
}

function Get-UserDataFingerprint {
  param($P, [string]$Out)
  $lines = New-Object System.Collections.Generic.List[string]
  if (Test-Path -LiteralPath $P.UserData) {
    $udSkip = @('Cache', 'Code Cache', 'GPUCache', 'DawnGraphiteCache', 'DawnWebGPUCache',
      'ShaderCache', 'Crashpad', 'CachedData', 'blob_storage')
    $stack = New-Object System.Collections.Stack
    $stack.Push($P.UserData)
    while ($stack.Count -gt 0) {
      $cur = $stack.Pop()
      foreach ($it in @(Get-ChildItem -LiteralPath $cur -Force -ErrorAction SilentlyContinue)) {
        $rel = Get-RelForward -Root $P.UserData -Path $it.FullName
        if ($it.PSIsContainer) {
          if ($udSkip -contains $it.Name) { continue }
          if ($it.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            $lines.Add("link`t$rel`t$($it.Target)"); continue
          }
          $stack.Push($it.FullName)
        }
        else {
          $lines.Add("file`t$rel`t$((Get-FileHash -LiteralPath $it.FullName -Algorithm SHA256).Hash.ToLower())")
        }
      }
    }
  }
  Set-Content -LiteralPath $Out -Value ($lines | Sort-Object) -Encoding utf8
  Write-Host "  $($lines.Count) desktop-data entries fingerprinted"
}

# ---------------------------------------------------------------------------
# pre
# ---------------------------------------------------------------------------

function Invoke-Pre {
  $P = Get-ResolvedPaths
  Step 'your install'
  Say "HERMES_HOME   $($P.Home)"
  Say "install       $($P.Install)"
  Say "desktop data  $($P.UserData) ($($P.UserDataOrigin))"
  Say "backup to     $BackupRoot"
  if (-not (Test-Path -LiteralPath $P.Home)) { Fail "no HERMES_HOME at $($P.Home)" }
  if (-not (Test-Path -LiteralPath (Join-Path $P.Install '.git'))) { Fail "no git checkout at $($P.Install) -- this tool covers source installs" }

  Step 'before we start (nothing here is pass/fail, just read it)'
  $procs = @(Get-Process -Name 'Hermes', 'hermes' -ErrorAction SilentlyContinue)
  if ($procs.Count) {
    Warn 'Hermes looks like it is running -- close the desktop app and the gateway'
    Warn "before you run 'hermes update', or the dependency sync may fail:"
    $procs | ForEach-Object { Write-Host "    $($_.ProcessName) (pid $($_.Id))" }
  }
  else { Ok 'no Hermes processes running' }
  $n = @(Invoke-GitCmd @('config', '--global', '--get-regexp', '^url\.')).Count
  if ($n -eq 0) { Ok 'global git config has no URL rewrites' }
  else { Warn "$n existing url.* insteadOf entr(y/ies) in your git config; we add more and remove only ours" }

  $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
  $script:Snap = Join-Path $BackupRoot $stamp
  if (Test-Path -LiteralPath $script:Snap) { Fail "backup dir already exists: $($script:Snap)" }
  $script:Armed = Join-Path $script:Snap 'armed'
  New-Item -ItemType Directory -Force -Path (Join-Path $script:Snap 'shims'), $script:Armed | Out-Null

  Step 'copying your entire HERMES_HOME'
  Test-TreeAccess -Root $P.Home -Label 'HERMES_HOME'
  $started = Get-Date
  $homeTar = Join-Path $script:Snap 'hermes-home.tar'
  # No excludes: checkout, venv, PM store and node_modules come too, so post is
  # a true rollback rather than a re-download. No -z: the backup root is
  # typically the same internal disk, so gzip costs ~5x the wall time for
  # nothing (measured on an M1).
  & $script:Tar -cf $homeTar -C $P.Home .
  if ($LASTEXITCODE -ne 0) { Fail 'backup failed (tar)' }
  $elapsed = [int]((Get-Date) - $started).TotalSeconds
  Ok "hermes-home.tar ($([math]::Round((Get-Item $homeTar).Length / 1MB, 1)) MB, ${elapsed}s)"

  Step 'recording git facts'
  $head = Invoke-Git $P @('rev-parse', 'HEAD')
  $branch = Invoke-Git $P @('branch', '--show-current')
  Set-Content -LiteralPath (Join-Path $script:Snap 'checkout.txt') -Encoding utf8 -Value @("head`t$head", "branch`t$branch")
  $remotes = @()
  foreach ($r in @(& git -C $P.Install remote)) {
    if (-not $r) { continue }
    $remotes += "$r`t$(Get-GitConfigValue $P "remote.$r.url")"
  }
  Set-Content -LiteralPath (Join-Path $script:Snap 'remotes.txt') -Encoding utf8 -Value $remotes
  Ok "checkout at $head"

  Step "copying the desktop app's data"
  if (Test-Path -LiteralPath $P.UserData) {
    Test-TreeAccess -Root $P.UserData -Label 'Electron userData'
    $udTar = Join-Path $script:Snap 'electron-userdata.tar'
    & $script:Tar -cf $udTar -C $P.UserData .
    if ($LASTEXITCODE -ne 0) { Fail 'userData backup failed' }
    Ok "electron-userdata.tar ($([math]::Round((Get-Item $udTar).Length / 1KB, 1)) KB)"
  }
  else { Warn "no Electron userData at $($P.UserData) (desktop app not installed?)" }

  Step 'copying the hermes shims on your PATH'
  $shimList = New-Object System.Collections.Generic.List[string]
  $shimLinks = New-Object System.Collections.Generic.List[string]
  foreach ($shim in $P.Shims) {
    if (-not (Test-Path -LiteralPath $shim)) { continue }
    $shimList.Add($shim)
    $item = Get-Item -LiteralPath $shim -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
      $shimLinks.Add("$shim`t$($item.Target)")
    }
    else {
      Copy-Item -LiteralPath $shim -Destination (Join-Path (Join-Path $script:Snap 'shims') ($shim -replace '[:\\/]', '_')) -Force
    }
  }
  Set-Content -LiteralPath (Join-Path $script:Snap 'shims.txt') -Encoding utf8 -Value $shimList
  Set-Content -LiteralPath (Join-Path $script:Snap 'shims-links.txt') -Encoding utf8 -Value $shimLinks
  if ($shimList.Count) { Ok "$($shimList.Count) shim path(s) recorded" } else { Warn 'no shims found' }
  Set-Content -LiteralPath (Join-Path $script:Snap 'user-path-before.txt') -Encoding utf8 -Value @(Get-UserPathValue)

  Step 'copying your global git config'
  $globalCfg = Join-Path $env:USERPROFILE '.gitconfig'
  if (Test-Path -LiteralPath $globalCfg) {
    Copy-Item -LiteralPath $globalCfg -Destination (Join-Path $script:Snap 'gitconfig.bak') -Force
    Ok "saved $globalCfg"
  }
  else { Warn 'no global git config yet; we will create one and remove it again in post' }

  Step 'fingerprinting (so post can tell you how exact the restore was)'
  Get-TreeFingerprint -P $P -Out (Join-Path $script:Snap 'fingerprint-before.txt')
  Get-UserDataFingerprint -P $P -Out (Join-Path $script:Snap 'userdata-before.txt')

  $manifest = [ordered]@{
    schema              = 2
    created             = (Get-Date).ToUniversalTime().ToString('o')
    hermes_home         = $P.Home
    hermes_root         = $P.Root
    install_dir         = $P.Install
    userdata_dir        = $P.UserData
    userdata_dir_source = $P.UserDataOrigin
    rehearsal_source    = $Source
    rehearsal_ref       = $Ref
    env                 = [ordered]@{
      HERMES_HOME                  = $env:HERMES_HOME
      HERMES_DESKTOP_USER_DATA_DIR = $env:HERMES_DESKTOP_USER_DATA_DIR
      HERMES_DATA_DIR_SUFFIX       = $env:HERMES_DATA_DIR_SUFFIX
      PHOTON_SIDECAR_DIR           = $env:PHOTON_SIDECAR_DIR
    }
    checkout            = [ordered]@{
      head     = (Invoke-Git $P @('rev-parse', 'HEAD'))
      branch   = (Invoke-Git $P @('branch', '--show-current'))
      origin   = (Get-GitConfigValue $P 'remote.origin.url')
      upstream = (Get-GitConfigValue $P 'remote.upstream.url')
    }
  }
  $manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $script:Snap 'manifest.json') -Encoding utf8
  Ok 'manifest.json'

  # --- point the install at the rehearsal source ---------------------------
  Step 'fetching the rehearsal source'
  Say "source        $Source"
  Say "ref           $Ref"
  $serve = Join-Path $script:Snap 'serve.git'
  if (Test-Path -LiteralPath $serve) { Remove-Item -LiteralPath $serve -Recurse -Force }
  $null = Invoke-GitCmd @('clone', '--quiet', '--bare', '--branch', $Ref, '--single-branch', $Source, $serve)
  if ($LASTEXITCODE -ne 0) {
    if (Test-Path -LiteralPath $serve) { Remove-Item -LiteralPath $serve -Recurse -Force }
    $null = Invoke-GitCmd @('clone', '--quiet', '--bare', $Source, $serve)
    if ($LASTEXITCODE -ne 0) { Fail "could not clone $Source (network? permissions? bad -Source?)" }
    Ok "cloned the whole repo (-Ref '$Ref' is not a branch/tag name)"
  }
  else { Ok "cloned $Ref" }
  $targetSha = (@(& git -C $serve rev-parse --verify "$Ref^{commit}" 2>$null | Out-String) -replace "`r", '').Trim()
  # Shape-check: a warning or error line folded into the capture would otherwise
  # be handed to update-ref as a bogus revision.
  if ($targetSha -notmatch '^[0-9a-f]{40}$') { Fail "-Ref '$Ref' was not found in $Source" }
  $null = Invoke-GitCmd @('-C', $serve, 'update-ref', 'refs/heads/main', $targetSha)
  $null = Invoke-GitCmd @('-C', $serve, 'symbolic-ref', 'HEAD', 'refs/heads/main')
  $null = Invoke-GitCmd @('-C', $serve, 'config', 'uploadpack.allowAnySHA1InWant', 'true')
  Ok "the update will land on $targetSha"

  Step 'pointing your install at it'
  # insteadOf is a TRANSPORT rewrite. Your checkout's origin keeps the official
  # URL, which matters: `hermes update` resolves its channel from the archive and
  # validates it against `git config --get remote.origin.url`. Repointing origin
  # at a fork would make the update fail before any git work.
  $fileUrl = 'file:///' + ($serve -replace '\\', '/')
  $armedLines = New-Object System.Collections.Generic.List[string]
  # REPO-LOCAL, like the POSIX script: the checkout's own config lives inside
  # the home this kit backs up and post restores, and it cannot be read-only or
  # ACL-denied (the install could not have written its own repo otherwise).
  foreach ($url in @($OfficialHttps, $OfficialSsh)) {
    # --add: the key is multi-valued; a plain set would drop the first URL.
    $null = Invoke-GitCmd @('-C', $P.Install, 'config', '--local', '--add', "url.$fileUrl.insteadOf", $url)
    if ($LASTEXITCODE -ne 0) { Fail "could not write the URL redirect into $($P.Install)\.git\config" }
    $armedLines.Add("$fileUrl`t$url`tlocal")
  }
  Set-Content -LiteralPath (Join-Path $script:Armed 'armed.txt') -Encoding utf8 -Value $armedLines
  New-Item -ItemType Directory -Force -Path $P.Home | Out-Null
  Set-Content -LiteralPath (Join-Path $P.Home '.skip_upstream_prompt') -Encoding utf8 -Value @()
  Set-Content -LiteralPath (Join-Path $script:Armed 'target-sha') -Encoding utf8 -Value @($targetSha)
  Ok 'official repo URL now resolves to the rehearsal copy'
  Ok "created $($P.Home)\.skip_upstream_prompt (stops the 'add upstream remote?' prompt)"

  Step 'ready'
  Say 'your install is unchanged so far -- nothing has been updated yet.'
  Say ''
  Say 'continue with the instructions provided'
  Say "your backup is at $($script:Snap) -- keep it until post has run."
}

# ---------------------------------------------------------------------------
# status (read-only)
# ---------------------------------------------------------------------------

function Invoke-Status {
  $P = Get-ResolvedPaths
  Step 'your install'
  Say "HERMES_HOME   $($P.Home)"
  Say "install       $($P.Install)"
  Say "desktop data  $($P.UserData) ($($P.UserDataOrigin))"
  Say "backup root   $BackupRoot"
  Step 'backup'
  $snap = Get-LatestSnapshot
  if (-not $snap) { Say "none -- nothing has been set up yet (run 'pre')"; return }
  Say "latest        $snap"
  $armed = Join-Path $snap 'armed'
  $ts = Join-Path $armed 'target-sha'
  if (Test-Path -LiteralPath $ts) {
    Say "prepared for  $((Get-Content -LiteralPath $ts -Raw).Trim())"
    $manifest = Get-Content -LiteralPath (Join-Path $snap 'manifest.json') -Raw | ConvertFrom-Json
    Say "source        $($manifest.rehearsal_source) @ $($manifest.rehearsal_ref)"
  }
  else { Say 'prepared      no' }
  Say "marker        $(if (Test-Path -LiteralPath (Join-Path $P.Home '.skip_upstream_prompt')) { 'present' } else { 'absent' })"
  $n = @(Invoke-GitCmd @('config', '--global', '--get-regexp', '^url\.')).Count
  Say "git rewrites  $n insteadOf entr(y/ies)"
  if (Test-Path -LiteralPath (Join-Path $P.Install '.git')) {
    Say "checkout now  $(Invoke-Git $P @('rev-parse', '--short', 'HEAD')) ($(Invoke-Git $P @('branch', '--show-current')))"
  }
}

# ---------------------------------------------------------------------------
# post
# ---------------------------------------------------------------------------

function Confirm-Action {
  param([string]$Prompt)
  if ($Yes) { return }
  $reply = Read-Host "$Prompt [y/N]"
  if ($reply -notmatch '^(y|yes)$') { Fail 'aborted -- nothing was changed' }
}

function Invoke-Unarm {
  Step 'removing the URL redirect'
  $armedFile = Join-Path $script:Armed 'armed.txt'
  if (Test-Path -LiteralPath $armedFile) {
    foreach ($line in @(Get-Content -LiteralPath $armedFile | Where-Object { $_ })) {
      $parts = $line -split "`t"
      $t = $parts[0]
      if (-not $t) { continue }
      $scope = if ($parts.Count -ge 3) { $parts[2] } else { 'global' }
      if ($scope -eq 'local') {
        $null = Invoke-GitCmd @('-C', $P.Install, 'config', '--local', '--unset-all', "url.$t.insteadOf")
      }
      else {
        $null = Invoke-GitCmd @('config', '--global', '--unset-all', "url.$t.insteadOf")
      }
    }
    # An earlier version of this kit wrote the redirect GLOBALLY; clear that too
    # so a machine that ran it is not left with a stale redirect.
    foreach ($t in @(Get-Content -LiteralPath $armedFile | Where-Object { $_ } |
        ForEach-Object { ($_ -split "`t")[0] } | Sort-Object -Unique)) {
      $null = Invoke-GitCmd @('config', '--global', '--unset-all', "url.$t.insteadOf")
    }
    Ok 'removed our insteadOf entries'
  }
  $cfgBackup = Join-Path $script:Snap 'gitconfig.bak'
  if (Test-Path -LiteralPath $cfgBackup) {
    Copy-Item -LiteralPath $cfgBackup -Destination (Join-Path $env:USERPROFILE '.gitconfig') -Force
    Ok 'restored your global git config'
  }
  $P = Get-ResolvedPaths
  $marker = Join-Path $P.Home '.skip_upstream_prompt'
  if (Test-Path -LiteralPath $marker) { Remove-Item -LiteralPath $marker -Force; Ok 'removed the upstream-prompt marker' }
}

function Invoke-Post {
  $manifest = Load-Snapshot
  $P = Get-ResolvedPaths
  Step 'this will delete and restore:'
  Say "  $($P.Home)  (all of it, including the checkout)"
  Say "  $($P.UserData)"
  Say "  the shim files recorded in $($script:Snap)\shims.txt"
  Confirm-Action "Put everything back from $($script:Snap)?"

  Invoke-Unarm
  Step 'stopping Hermes'
  foreach ($name in @('Hermes', 'hermes')) {
    Get-Process -Name $name -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
  }
  Ok 'asked Hermes to stop (if anything was running)'

  Step 'clearing what the rehearsal touched'
  if (Test-Path -LiteralPath $P.Home) { Remove-Item -LiteralPath $P.Home -Recurse -Force; Ok "removed $($P.Home)" }
  if (Test-Path -LiteralPath $P.UserData) { Remove-Item -LiteralPath $P.UserData -Recurse -Force; Ok "removed $($P.UserData)" }
  $shimFile = Join-Path $script:Snap 'shims.txt'
  if (Test-Path -LiteralPath $shimFile) {
    foreach ($s in @(Get-Content -LiteralPath $shimFile | Where-Object { $_ })) {
      if (Test-Path -LiteralPath $s) { Remove-Item -LiteralPath $s -Force -ErrorAction SilentlyContinue }
    }
    Ok 'removed the recorded shim files'
  }

  Step 'restoring your HERMES_HOME'
  New-Item -ItemType Directory -Force -Path $P.Home | Out-Null
  & $script:Tar -xf (Join-Path $script:Snap 'hermes-home.tar') -C $P.Home
  if ($LASTEXITCODE -ne 0) { Fail "restore failed -- your backup is intact at $($script:Snap)" }
  Ok 'restored'

  Step "restoring the desktop app's data"
  $udTar = Join-Path $script:Snap 'electron-userdata.tar'
  if (Test-Path -LiteralPath $udTar) {
    New-Item -ItemType Directory -Force -Path $P.UserData | Out-Null
    & $script:Tar -xf $udTar -C $P.UserData
    if ($LASTEXITCODE -ne 0) { Fail "userData restore failed (backup intact at $($script:Snap))" }
    Ok 'restored'
  }
  else { Warn 'there was no desktop app data to restore' }

  Step 'restoring the shims'
  $linkFile = Join-Path $script:Snap 'shims-links.txt'
  if (Test-Path -LiteralPath $shimFile) {
    foreach ($s in @(Get-Content -LiteralPath $shimFile | Where-Object { $_ })) {
      $saved = Join-Path (Join-Path $script:Snap 'shims') ($s -replace '[:\\/]', '_')
      if (Test-Path -LiteralPath $saved) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $s) | Out-Null
        Copy-Item -LiteralPath $saved -Destination $s -Force
        Ok "restored $s"
      }
      elseif ((Test-Path -LiteralPath $linkFile) -and ((Get-Content -LiteralPath $linkFile -Raw) -match [regex]::Escape($s))) {
        $target = (Get-Content -LiteralPath $linkFile | Where-Object { $_ -like "$s`t*" } | ForEach-Object { ($_ -split "`t", 2)[1] })
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $s) | Out-Null
        if (Test-Path -LiteralPath $s) { Remove-Item -LiteralPath $s -Force }
        # Symlinks need a privilege (or Developer Mode); a copy is close enough
        # and must never abort the restore.
        try {
          New-Item -ItemType SymbolicLink -Path $s -Target $target -Force -ErrorAction Stop | Out-Null
          Ok "restored symlink $s"
        }
        catch {
          Copy-Item -LiteralPath $target -Destination $s -Recurse -Force -ErrorAction SilentlyContinue
          Warn "could not create a symlink at $s (no privilege); restored a copy instead"
        }
      }
    }
  }

  Step 'putting your USER PATH back'
  $beforePath = Join-Path $script:Snap 'user-path-before.txt'
  if (Test-Path -LiteralPath $beforePath) {
    $old = (Get-Content -LiteralPath $beforePath -Raw)
    if ($old) { $old = $old.TrimEnd("`r", "`n") } else { $old = '' }
    if ($old) {
      [Environment]::SetEnvironmentVariable('Path', $old, 'User')
      Ok 'restored your USER PATH (open a new shell to pick it up)'
    }
    else { Warn 'your USER PATH was recorded empty; leaving it untouched' }
  }

  Step 'how exact was the restore'
  $diffCount = 0
  Get-TreeFingerprint -P $P -Out (Join-Path $script:Snap 'fingerprint-restored.txt') *> $null
  Get-UserDataFingerprint -P $P -Out (Join-Path $script:Snap 'userdata-restored.txt') *> $null

  function Compare-Files([string]$Before, [string]$After, [string]$Label) {
    $b = @{}; $a = @{}
    if (Test-Path -LiteralPath $Before) {
      foreach ($line in (Get-Content -LiteralPath $Before -ErrorAction SilentlyContinue)) {
        $parts = $line -split "`t"; if ($parts.Count -ge 2) { $b[$parts[1]] = $line }
      }
    }
    if (Test-Path -LiteralPath $After) {
      foreach ($line in (Get-Content -LiteralPath $After -ErrorAction SilentlyContinue)) {
        $parts = $line -split "`t"; if ($parts.Count -ge 2) { $a[$parts[1]] = $line }
      }
    }
    $diff = @($b.Keys | Where-Object { -not $a.ContainsKey($_) -or $b[$_] -ne $a[$_] })
    if ($diff.Count -eq 0) { Ok "all $($b.Count) $Label entries match your backup"; return 0 }
    Warn "$($diff.Count) $Label entr(y/ies) differ from the backup:"
    $diff | ForEach-Object { Write-Host "    $_" }
    return 1
  }
  $diffCount += (Compare-Files (Join-Path $script:Snap 'fingerprint-before.txt') (Join-Path $script:Snap 'fingerprint-restored.txt') 'state')
  $diffCount += (Compare-Files (Join-Path $script:Snap 'userdata-before.txt') (Join-Path $script:Snap 'userdata-restored.txt') 'desktop data')

  Step 'done'
  Say 'Your install, your data and your git config are back as they were.'
  Say "Open the desktop app once and run 'hermes doctor' to confirm."
  Say "Nothing was judged or changed by this script; the backup at $($script:Snap)"
  Say 'is yours to keep or delete.'
  if ($diffCount -gt 0) { Say '(The differences above are informational, not a failure.)' }
}

# ---------------------------------------------------------------------------

if (-not $BackupRoot) { $BackupRoot = Join-Path $env:USERPROFILE 'hermes-update-rehearsal' }

if ($Command -ne 'help') {
  $script:Tar = Resolve-Tar
  if (-not $script:Tar) { Fail 'no usable tar.exe found; install the Windows tar or put it on PATH' }
}

switch ($Command) {
  'pre' { Invoke-Pre }
  'post' { Invoke-Post }
  'status' { Invoke-Status }
  default {
    # $PSCommandPath is empty when the script was piped in rather than run
    # from a file, so fall back to a self-contained summary.
    if ($PSCommandPath) {
      Get-Help $PSCommandPath -Detailed | Out-String | Write-Host
    }
    else {
      Write-Host 'hermes-update-rehearsal.ps1 -- run against an EXISTING Hermes install.'
      Write-Host ''
      Write-Host '  pre     back up everything, then point the update source at a custom repo+ref'
      Write-Host '  post    undo all of it: remove the redirect, wipe, restore the backup'
      Write-Host '  status  print what is prepared (read-only; nothing is touched)'
      Write-Host ''
      Write-Host 'Options:'
      Write-Host '  -Source URL        repo to pull the update from'
      Write-Host '  -Ref REV           branch or tag in that repo'
      Write-Host '  -BackupRoot DIR    where the backup lives'
      Write-Host '  -Yes               post: skip the confirmation'
      Write-Host ''
      Write-Host "Run 'pre' first: it reports what it did and prints the next commands."
    }
  }
}

# The db-count program is a temp file; do not leave it behind.
if ($script:DbCountPy -and (Test-Path -LiteralPath $script:DbCountPy)) {
  Remove-Item -LiteralPath $script:DbCountPy -Force -ErrorAction SilentlyContinue
}
