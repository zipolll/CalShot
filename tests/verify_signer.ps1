param()

# Windows-only, local fixtures. Never sends a signing request or real API key.
$ErrorActionPreference = 'Stop'
$taskProjectRoot = Split-Path -Parent $PSScriptRoot
$taskDistRoot = [IO.Path]::GetFullPath((Join-Path $taskProjectRoot 'dist'))
$taskTestRoot = Join-Path $taskDistRoot ('signer-test-' + [Guid]::NewGuid().ToString('N'))
$taskUnicode = [string][char]0x6D4B + [string][char]0x8BD5
$taskFixtureRoot = Join-Path $taskTestRoot ('copy with spaces ' + $taskUnicode)
$taskFixtureDist = Join-Path $taskFixtureRoot 'dist'
$taskFixtureScript = Join-Path $taskFixtureRoot 'scripts/sign-shortcut.ps1'
$taskLink = Join-Path $taskFixtureDist 'performance-check'
$taskChecks = New-Object 'System.Collections.Generic.List[string]'
$taskSkipped = New-Object 'System.Collections.Generic.List[string]'
$taskUTF8 = New-Object Text.UTF8Encoding($false)

function Assert-Task {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function New-TaskManifest {
    param([string]$Directory, [string]$Stem)
    $null = New-Item -ItemType Directory -Path $Directory -Force
    $taskInput = Join-Path $Directory ($Stem + '.plist')
    [IO.File]::WriteAllText($taskInput, '<plist><dict><key>fixture</key><true/></dict></plist>', $taskUTF8)
    $taskManifest = [ordered]@{
        name = 'CalShot fixture'
        version = '0.1.13-fixture'
        fileStem = $Stem
        signInput = $Stem + '.plist'
        signInputSHA256 = (Get-FileHash -LiteralPath $taskInput -Algorithm SHA256).Hash.ToLowerInvariant()
        apiKey = 'not used'
    }
    [IO.File]::WriteAllText((Join-Path $Directory 'manifest.json'), ($taskManifest | ConvertTo-Json), $taskUTF8)
}

function Invoke-TaskValidation {
    param([string[]]$Extra = @(), [string]$ExpectedError = '')
    $taskLines = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $taskFixtureScript -ValidateOnly @Extra 2>&1
    $taskExitCode = $LASTEXITCODE
    $taskText = ($taskLines | ForEach-Object { $_.ToString() }) -join "`n"
    if ($ExpectedError) {
        Assert-Task ($taskExitCode -ne 0 -and $taskText.Contains($ExpectedError)) ('Expected rejection: ' + $taskText)
        return
    }
    Assert-Task ($taskExitCode -eq 0) ('Validation failed: ' + $taskText)
    $taskReport = $taskText | ConvertFrom-Json
    Assert-Task ($taskReport.status -eq 'validated' -and -not $taskReport.networkRequestSent -and -not $taskReport.filesChanged) 'Validation must not sign or change files.'
    return $taskReport
}

function Get-TaskSnapshot {
    $taskFiles = Get-ChildItem -LiteralPath $taskFixtureRoot -File -Recurse | Sort-Object FullName
    return (($taskFiles | ForEach-Object {
        $_.FullName + ':' + (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
    }) -join "`n")
}

try {
    foreach ($taskFolder in @('scripts', 'src/calshot')) {
        $null = New-Item -ItemType Directory -Path (Join-Path $taskFixtureRoot $taskFolder) -Force
    }
    Copy-Item -LiteralPath (Join-Path $taskProjectRoot 'scripts/sign-shortcut.ps1') -Destination $taskFixtureScript
    Copy-Item -LiteralPath (Join-Path $taskProjectRoot 'sign-on-windows.cmd') -Destination (Join-Path $taskFixtureRoot 'sign-on-windows.cmd')
    [IO.File]::WriteAllText((Join-Path $taskFixtureRoot 'manage.py'), '# fixture project marker', $taskUTF8)
    [IO.File]::WriteAllText((Join-Path $taskFixtureRoot 'src/calshot/shortcut.py'), '# fixture source marker', $taskUTF8)
    New-TaskManifest $taskFixtureDist 'calshot'
    New-TaskManifest (Join-Path $taskFixtureDist 'reminder-runtime-check') 'calshot-reminder-runtime-check'
    $taskManifestPath = Join-Path $taskFixtureDist 'manifest.json'
    $taskOriginalManifest = [IO.File]::ReadAllText($taskManifestPath)
    $taskSigned = Join-Path $taskFixtureDist 'calshot.shortcut'
    $taskTempDownload = Join-Path $taskFixtureDist 'signed-shortcut.download.tmp'
    [IO.File]::WriteAllText($taskSigned, 'AEA1 unsigned test fixture', $taskUTF8)
    [IO.File]::WriteAllText($taskTempDownload, 'existing temporary file must stay untouched', $taskUTF8)
    $taskLegacy = Join-Path $taskFixtureDist 'editor-check'
    $null = New-Item -ItemType Directory -Path $taskLegacy
    [IO.File]::WriteAllText((Join-Path $taskLegacy 'keep.txt'), 'cleanup is only a plan', $taskUTF8)
    $taskBefore = Get-TaskSnapshot

    $taskNormal = Invoke-TaskValidation
    Assert-Task ($taskNormal.projectRoot -eq $taskFixtureRoot -and
                 $taskNormal.signOutput -eq $taskSigned) 'Paths must follow the relocated project.'
    $taskChecks.Add('Relocated project, spaces and Unicode paths')

    $taskClean = Invoke-TaskValidation -Extra @('-Clean')
    Assert-Task ($taskClean.cleanupCandidates -contains $taskSigned -and
                 $taskClean.cleanupCandidates -contains $taskLegacy) 'Stale outputs must be planned locally.'
    Assert-Task ((Get-TaskSnapshot) -eq $taskBefore) 'ValidateOnly changed a fixture, including an old temporary download.'
    $taskChecks.Add('Read-only cleanup planning preserves source, old outputs and temporary downloads')

    $taskReminder = Invoke-TaskValidation -Extra @('-ReminderCheck')
    Assert-Task ($taskReminder.signOutput -eq (Join-Path $taskFixtureDist 'reminder-runtime-check/calshot-reminder-runtime-check.shortcut')) 'Reminder target must use its own directory.'
    $taskChecks.Add('Independent reminder target')

    $taskBatchLines = & (Join-Path $taskFixtureRoot 'sign-on-windows.cmd') -ValidateOnly 2>&1
    Assert-Task ($LASTEXITCODE -eq 0) 'Batch entry failed from a different working directory.'
    $taskBatch = (($taskBatchLines | ForEach-Object { $_.ToString() }) -join "`n") | ConvertFrom-Json
    Assert-Task ($taskBatch.projectRoot -eq $taskFixtureRoot -and -not $taskBatch.networkRequestSent) 'Batch entry must locate its own project.'
    Assert-Task ((Get-TaskSnapshot) -eq $taskBefore) 'Batch validation changed files.'
    $taskChecks.Add('Batch entry works outside the fixture working directory')

    $taskMatching = $taskOriginalManifest | ConvertFrom-Json
    $taskMatching | Add-Member NoteProperty signing ([pscustomobject]@{
        sourceSHA256 = $taskMatching.signInputSHA256
        outputSHA256 = (Get-FileHash -LiteralPath $taskSigned -Algorithm SHA256).Hash.ToLowerInvariant()
    })
    [IO.File]::WriteAllText($taskManifestPath, ($taskMatching | ConvertTo-Json -Depth 4), $taskUTF8)
    $taskMatchingPlan = Invoke-TaskValidation -Extra @('-Clean')
    Assert-Task ($taskMatchingPlan.cleanupCandidates -notcontains $taskSigned) 'Matching source/output records must retain the signed file.'
    $taskChecks.Add('Matching signature records are retained')

    $taskBad = $taskOriginalManifest | ConvertFrom-Json
    $taskBad.signInput = '../outside.plist'
    [IO.File]::WriteAllText($taskManifestPath, ($taskBad | ConvertTo-Json), $taskUTF8)
    Invoke-TaskValidation -Extra @('-Clean') -ExpectedError 'Unexpected manifest filenames'
    [IO.File]::WriteAllText($taskManifestPath, $taskOriginalManifest, $taskUTF8)
    $taskBad = $taskOriginalManifest | ConvertFrom-Json
    $taskBad.signInputSHA256 = 'not-the-source-hash'
    [IO.File]::WriteAllText($taskManifestPath, ($taskBad | ConvertTo-Json), $taskUTF8)
    Invoke-TaskValidation -Extra @('-Clean') -ExpectedError 'The source has changed'
    [IO.File]::WriteAllText($taskManifestPath, $taskOriginalManifest, $taskUTF8)
    Invoke-TaskValidation -Extra @('-ReminderCheck', '-PerformanceCheck') -ExpectedError 'Choose one diagnostic signing target'
    Assert-Task ((Get-TaskSnapshot) -eq $taskBefore) 'Failed validation or manifest restoration changed other files.'
    $taskChecks.Add('Escaping manifest, changed source hash and conflicting targets are rejected')

    $taskOutside = Join-Path $taskTestRoot 'outside-dist'
    $null = New-Item -ItemType Directory -Path $taskOutside
    $taskProtected = Join-Path $taskOutside 'keep.txt'
    [IO.File]::WriteAllText($taskProtected, 'outside dist', $taskUTF8)
    try { $null = New-Item -ItemType Junction -Path $taskLink -Value $taskOutside }
    catch { $taskSkipped.Add('Directory junction fixture unavailable: ' + $_.Exception.Message) }
    if (Test-Path -LiteralPath $taskLink) {
        Invoke-TaskValidation -Extra @('-Clean') -ExpectedError 'symbolic link or junction'
        Assert-Task ([IO.File]::ReadAllText($taskProtected) -eq 'outside dist') 'A rejected junction touched its target.'
        $taskChecks.Add('Junction pointing outside dist is rejected before cleanup')
    }

    [pscustomobject]@{
        status = 'passed'
        checks = $taskChecks.ToArray()
        skipped = $taskSkipped.ToArray()
        networkRequestSent = $false
        formalOutputsChanged = $false
    } | ConvertTo-Json -Depth 4
}
finally {
    if (Test-Path -LiteralPath $taskLink) {
        Assert-Task (((Get-Item -LiteralPath $taskLink -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) 'Unexpected test link; cleanup stopped.'
        # Non-recursive deletion removes only the test junction itself.
        [IO.Directory]::Delete($taskLink)
    }
    if (Test-Path -LiteralPath $taskTestRoot) {
        $taskResolvedTestRoot = (Resolve-Path -LiteralPath $taskTestRoot).Path
        Assert-Task ($taskResolvedTestRoot -eq $taskTestRoot -and
                     $taskResolvedTestRoot.StartsWith($taskDistRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -and
                     [IO.Path]::GetFileName($taskResolvedTestRoot) -match '^signer-test-[0-9a-f]{32}$') 'Test cleanup target is outside the intended workspace.'
        Remove-Item -LiteralPath $taskResolvedTestRoot -Recurse -Force
    }
}
