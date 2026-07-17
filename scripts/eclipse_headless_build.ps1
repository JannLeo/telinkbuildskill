param(
    [Parameter(Mandatory=$true)]
    [string]$IdePath,

    [Parameter(Mandatory=$true)]
    [string]$ProjectPath,

    [Parameter(Mandatory=$true)]
    [string]$BuildTarget,

    [string]$WorkspaceDir = '',

    [string]$OutputDir = '',

    [string]$OutputName = '',

    [string]$SdkRoot = ''
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($SdkRoot)) {
    $SdkRoot = (Get-Location).Path
}
$SdkRoot = (Resolve-Path $SdkRoot).Path

$IdeExe = Join-Path $IdePath 'TelinkIoTStudio.exe'
if (-not (Test-Path $IdeExe)) {
    $IdeExe = Join-Path $IdePath 'eclipse.exe'
}
if (-not (Test-Path $IdeExe)) {
    $IdeExe = $IdePath
}

if ([string]::IsNullOrWhiteSpace($WorkspaceDir)) {
    $SdkName = Split-Path -Leaf $SdkRoot
    $SdkParent = Split-Path -Parent $SdkRoot
    $WorkspaceDir = Join-Path (Join-Path $SdkParent 'woekspace') $SdkName
}
if (-not (Test-Path $WorkspaceDir)) {
    New-Item -ItemType Directory -Path $WorkspaceDir | Out-Null
}
$WorkspaceDir = (Resolve-Path $WorkspaceDir).Path

$ResolvedProject = if ([System.IO.Path]::IsPathRooted($ProjectPath)) { $ProjectPath } else { Join-Path $SdkRoot $ProjectPath }
if (Test-Path $ResolvedProject) {
    $ResolvedProject = (Resolve-Path $ResolvedProject).Path
}

if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path $SdkRoot 'build_variants'
}
if (-not (Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir | Out-Null
}
$OutputDir = (Resolve-Path $OutputDir).Path

$BuildLog = Join-Path $SdkRoot 'eclipse_headless_build.log'
$BuildStart = (Get-Date).AddSeconds(-3)

$status = 0
$BuildWasRun = $false
if (Test-Path $IdeExe) {
    $BuildWasRun = $true
    Write-Host "Running Eclipse headless build:"
    Write-Host "  IDE: $IdeExe"
    Write-Host "  -data: $WorkspaceDir"
    Write-Host "  -import: $ResolvedProject"
    Write-Host "  -cleanBuild: $BuildTarget"
    Write-Host "  log: $BuildLog"

    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'

    & $IdeExe --launcher.suppressErrors -nosplash `
        -application org.eclipse.cdt.managedbuilder.core.headlessbuild `
        -data $WorkspaceDir `
        -import $ResolvedProject `
        -cleanBuild $BuildTarget `
        2>&1 | ForEach-Object { $_.ToString() } | Tee-Object -FilePath $BuildLog

    $status = $LASTEXITCODE
    $ErrorActionPreference = $oldErrorActionPreference
} else {
    Write-Warning "Eclipse IDE not found at $IdeExe"
    Write-Host "Set the correct IdePath parameter (directory containing TelinkIoTStudio.exe or eclipse.exe)."
    $status = 1
}

$CopiedBin = $null
if ($status -eq 0) {
    $BuildConfigName = ($BuildTarget -split '/', 2)[-1]
    $binCandidates = Get-ChildItem -Path $ResolvedProject -Recurse -Include *.bin -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -ge $BuildStart } |
        Sort-Object LastWriteTime -Descending
    if (-not $binCandidates -or $binCandidates.Count -eq 0) {
        $binCandidates = Get-ChildItem -Path $ResolvedProject -Recurse -Include *.bin -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending
    }
    $SourceBin = $binCandidates | Select-Object -First 1
    if ($SourceBin) {
        $Timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
        $ProjName = Split-Path -Leaf $ResolvedProject
        if ([string]::IsNullOrWhiteSpace($OutputName)) {
            $OutputBaseName = "${ProjName}_${BuildConfigName}_${Timestamp}"
        } else {
            $OutputBaseName = $OutputName -replace '[\\/:*?"<>| ]', '_'
        }
        $DestBin = Join-Path $OutputDir ($OutputBaseName + '.bin')
        Copy-Item -Path $SourceBin.FullName -Destination $DestBin -Force
        $CopiedBin = Get-Item $DestBin
    } else {
        Write-Warning "Build succeeded, but no .bin artifact found under $ResolvedProject."
    }
}

Write-Host ''
Write-Host 'Eclipse headless build summary'
Write-Host "  project: $ResolvedProject"
Write-Host "  target:  $BuildTarget"
Write-Host "  IDE exit code: $status"
Write-Host ("  build: " + ($(if ($status -eq 0) { 'success' } elseif ($BuildWasRun) { 'failed' } else { 'ide-not-found' })))
if ($status -ne 0 -and $BuildWasRun) {
    Write-Host "  See log: $BuildLog"
}
if ($CopiedBin) {
    $rel = $CopiedBin.FullName.Substring($SdkRoot.Length + 1)
    Write-Host "  copied bin: $rel ($($CopiedBin.Length) bytes)"
}
exit $status
