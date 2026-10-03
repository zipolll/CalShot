param([switch]$NoPause, [switch]$ReminderCheck, [switch]$CalendarSelectionCheck, [switch]$PerformanceCheck, [switch]$Clean, [switch]$ValidateOnly)

# Only the pristine, placeholder-only file is submitted to the signing service.
# Enter the real API key on the iPhone after importing the signed file.
$ErrorActionPreference = 'Stop'
$taskProjectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$taskDistRoot = [IO.Path]::GetFullPath((Join-Path $taskProjectRoot 'dist'))
$taskRoot = $taskDistRoot
$taskExpectedStem = 'calshot'
if ($ReminderCheck) {
    $taskRoot = Join-Path $taskRoot 'reminder-runtime-check'
    $taskExpectedStem = 'calshot-reminder-runtime-check'
}
if ($CalendarSelectionCheck) {
    $taskRoot = Join-Path $taskRoot 'calendar-selection-check'
    $taskExpectedStem = 'calshot-calendar-selection-check'
}
if ($PerformanceCheck) {
    $taskRoot = Join-Path $taskRoot 'performance-check'
    $taskExpectedStem = 'calshot-performance-check'
}
$taskTemp = Join-Path $taskRoot 'signed-shortcut.download.tmp'
$taskExit = 0
$taskResponse = $null
$taskTempCreated = $false

function Get-TaskDistPath {
    param([string]$Path, [switch]$AllowRoot)
    $taskCheckedPath = [IO.Path]::GetFullPath($Path)
    $taskIsRoot = $taskCheckedPath.Equals($taskDistRoot, [StringComparison]::OrdinalIgnoreCase)
    $taskPrefix = $taskDistRoot + [IO.Path]::DirectorySeparatorChar
    if (($taskIsRoot -and -not $AllowRoot) -or
        (-not $taskIsRoot -and -not $taskCheckedPath.StartsWith($taskPrefix, [StringComparison]::OrdinalIgnoreCase))) {
        throw 'Output or cleanup target is outside the project dist directory.'
    }
    # Resolve-Path alone does not reliably reject Windows junctions. Refuse
    # reparse points in dist and its descendants before any read/write/delete.
    $taskAncestor = $taskCheckedPath
    while ($true) {
        if (Test-Path -LiteralPath $taskAncestor) {
            $taskNode = Get-Item -LiteralPath $taskAncestor -Force
            if (($taskNode.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Output or cleanup target contains a symbolic link or junction.'
            }
        }
        if ($taskAncestor.Equals($taskDistRoot, [StringComparison]::OrdinalIgnoreCase)) { break }
        $taskAncestor = [IO.Path]::GetDirectoryName($taskAncestor)
    }
    return $taskCheckedPath
}

try {
    if (([int][bool]$ReminderCheck + [int][bool]$CalendarSelectionCheck + [int][bool]$PerformanceCheck) -gt 1) {
        throw 'Choose one diagnostic signing target.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $taskProjectRoot 'manage.py') -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $taskProjectRoot 'src/calshot/shortcut.py') -PathType Leaf)) {
        throw 'Keep the signing tool in the project scripts directory.'
    }
    $taskRoot = Get-TaskDistPath $taskRoot -AllowRoot
    $taskManifestPath = Get-TaskDistPath (Join-Path $taskRoot 'manifest.json')
    if (-not (Test-Path -LiteralPath $taskManifestPath -PathType Leaf)) {
        throw 'Generated manifest not found. Run python manage.py verify first, or build the requested diagnostic.'
    }
    $taskManifest = Get-Content -LiteralPath $taskManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($taskManifest.fileStem -ne $taskExpectedStem -or $taskManifest.signInput -ne ($taskExpectedStem + '.plist')) {
        throw 'Unexpected manifest filenames; signing stopped.'
    }
    $taskInput = Get-TaskDistPath (Join-Path $taskRoot $taskManifest.signInput)
    $taskOutput = Get-TaskDistPath (Join-Path $taskRoot ($taskManifest.fileStem + '.shortcut'))
    $taskTemp = Get-TaskDistPath $taskTemp
    $taskHash = (Get-FileHash -LiteralPath $taskInput -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($taskHash -ne $taskManifest.signInputSHA256) {
        throw 'The source has changed. Rebuild the placeholder-only source before signing.'
    }
    $taskCleanupPaths = @()
    if ($Clean) {
        # Plan and validate every target before deleting any generated file.
        $taskCleanRoot = Get-TaskDistPath $taskDistRoot -AllowRoot
        $taskOldNames = @('_inspection', 'editor-check', 'reminder-check', 'reminder-custom-check',
            'phone-delivery.json', 'signing-diagnostics.json',
            'reminder-runtime-check/sign-on-windows.cmd', 'reminder-runtime-check/sign-shortcut.ps1')
        # The calendar bridge was confirmed by the user and is now integrated.
        # Keep its output only when that diagnostic is explicitly requested.
        if (-not $CalendarSelectionCheck) { $taskOldNames += 'calendar-selection-check' }
        if (-not $PerformanceCheck) { $taskOldNames += 'performance-check' }
        $taskOldNames += Get-ChildItem -LiteralPath $taskCleanRoot -File |
            Where-Object { $_.Name -match '^calshot-\d+\.\d+\.\d+\.(shortcut|zip)$' } |
            ForEach-Object { $_.Name }
        foreach ($taskOldName in $taskOldNames) {
            $taskOldPath = Get-TaskDistPath (Join-Path $taskCleanRoot $taskOldName)
            if (Test-Path -LiteralPath $taskOldPath) {
                $taskCleanupPaths += $taskOldPath
            }
        }
        # Do not present a previous signature as the rebuilt release.
        foreach ($taskCurrentDir in @($taskCleanRoot, (Join-Path $taskCleanRoot 'reminder-runtime-check'),
                                     (Join-Path $taskCleanRoot 'calendar-selection-check'),
                                     (Join-Path $taskCleanRoot 'performance-check'))) {
            $taskCurrentManifestPath = Get-TaskDistPath (Join-Path $taskCurrentDir 'manifest.json')
            if (-not (Test-Path -LiteralPath $taskCurrentManifestPath -PathType Leaf)) { continue }
            $taskCurrentManifest = Get-Content -LiteralPath $taskCurrentManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($taskCurrentManifest.fileStem -notin @('calshot', 'calshot-reminder-runtime-check', 'calshot-calendar-selection-check', 'calshot-performance-check')) {
                throw 'Unexpected current output name; cleanup stopped.'
            }
            $taskCurrentSigned = Get-TaskDistPath (Join-Path $taskCurrentDir ($taskCurrentManifest.fileStem + '.shortcut'))
            if (Test-Path -LiteralPath $taskCurrentSigned) {
                $taskCurrentHash = (Get-FileHash -LiteralPath $taskCurrentSigned -Algorithm SHA256).Hash.ToLowerInvariant()
                if ($taskCurrentManifest.signing.sourceSHA256 -ne $taskCurrentManifest.signInputSHA256 -or
                    $taskCurrentManifest.signing.outputSHA256 -ne $taskCurrentHash) {
                    $taskCleanupPaths += $taskCurrentSigned
                }
            }
        }
    }
    $taskCleanupPaths = @($taskCleanupPaths | Select-Object -Unique)
    if ($ValidateOnly) {
        [pscustomobject]@{
            status = 'validated'
            projectRoot = $taskProjectRoot
            version = $taskManifest.version
            signInput = $taskInput
            signOutput = $taskOutput
            signInputSHA256 = $taskHash
            cleanupCandidates = $taskCleanupPaths
            networkRequestSent = $false
            filesChanged = $false
        } | ConvertTo-Json -Depth 4
        exit 0
    }
    foreach ($taskCleanupPath in $taskCleanupPaths) {
        $taskCleanupSafe = Get-TaskDistPath $taskCleanupPath
        Remove-Item -LiteralPath $taskCleanupSafe -Recurse -Force
    }
    if ($Clean) {
        Write-Host 'Old generated releases and archives cleaned. Current source and active diagnostic retained.'
    }

    Write-Host 'Signing the Shortcut with RoutineHub HubSign...' -ForegroundColor Cyan
    if ($taskManifest.apiKey -eq 'not used') {
        Write-Host 'This diagnostic does not use an API key.'
    } else {
        Write-Host 'Your API key is NOT requested here. Enter it on your iPhone after import.'
    }
    # Read an undecorated .NET string; Windows PowerShell may otherwise serialize
    # Get-Content's extra properties as an object instead of the XML string.
    $taskXML = [IO.File]::ReadAllText($taskInput, [Text.Encoding]::UTF8)
    $taskPayload = @{shortcutName = $taskManifest.name; shortcut = $taskXML} | ConvertTo-Json -Compress -Depth 4
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    # A redirect is not a signing response. Keep the original endpoint visible.
    $taskRequest = [Net.HttpWebRequest]::Create('https://hubsign.routinehub.services/sign')
    $taskRequest.Method = 'POST'
    $taskRequest.ContentType = 'application/json; charset=utf-8'
    # HubSign redirects the former User-Agent to its homepage. Use the public
    # Cherri protocol identifier and also identify CalShot (verified 2026-09-30).
    $taskRequest.UserAgent = 'cherri/2.3.0 CalShot/' + $taskManifest.version
    $taskRequest.AllowAutoRedirect = $false
    $taskRequest.Timeout = 45000
    $taskRequest.ReadWriteTimeout = 45000
    $taskPayloadBytes = [Text.Encoding]::UTF8.GetBytes($taskPayload)
    $taskRequest.ContentLength = $taskPayloadBytes.Length
    $taskUpload = $taskRequest.GetRequestStream()
    try { $taskUpload.Write($taskPayloadBytes, 0, $taskPayloadBytes.Length) }
    finally { $taskUpload.Dispose() }
    try { $taskResponse = $taskRequest.GetResponse() }
    catch [Net.WebException] {
        if ($_.Exception.Response) { $taskResponse = $_.Exception.Response }
        else { throw }
    }

    $taskStatus = [int]$taskResponse.StatusCode
    if ($taskStatus -ge 300 -and $taskStatus -lt 400) {
        $taskLocation = $taskResponse.Headers['Location']
        throw ('HubSign redirected the signing endpoint (HTTP ' + $taskStatus + ') to: ' + $taskLocation + '. The endpoint is not returning a signed Shortcut. Use the Mac signing alternative or wait for the service to recover.')
    }
    if ($taskStatus -eq 403) {
        throw 'HubSign rejected the signing request (HTTP 403). This is a signing-service access error, unrelated to your model API key.'
    }
    if ($taskStatus -ne 200) { throw ('The signing service returned HTTP ' + $taskStatus + '.') }
    $taskMime = ($taskResponse.ContentType -split ';')[0].Trim()
    if ($taskMime -notin @('application/octet-stream', 'application/x-plist', 'application/x-apple-shortcut')) {
        throw ('Unexpected response type: ' + $taskMime)
    }
    $taskDownload = $taskResponse.GetResponseStream()
    $taskFileStream = [IO.File]::Create($taskTemp)
    $taskTempCreated = $true
    try { $taskDownload.CopyTo($taskFileStream) }
    finally {
        $taskFileStream.Dispose()
        $taskDownload.Dispose()
    }
    $taskBytes = [IO.File]::ReadAllBytes($taskTemp)
    if ($taskBytes.Length -lt 4 -or [Text.Encoding]::ASCII.GetString($taskBytes, 0, 4) -ne 'AEA1') {
        throw 'The response is not an Apple signed Shortcut. No importable output was created.'
    }
    $taskTemp = Get-TaskDistPath $taskTemp
    $taskOutput = Get-TaskDistPath $taskOutput
    Move-Item -LiteralPath $taskTemp -Destination $taskOutput -Force
    $taskSignInfo = [pscustomobject]@{
        sourceSHA256 = $taskHash
        outputSHA256 = (Get-FileHash -LiteralPath $taskOutput -Algorithm SHA256).Hash.ToLowerInvariant()
        signedAtUTC = [DateTime]::UtcNow.ToString('o')
        httpStatus = $taskStatus
        bytes = $taskBytes.Length
        containerMarker = 'AEA1'
        iPhoneAccepted = $false
    }
    $taskManifest | Add-Member -MemberType NoteProperty -Name signing -Value $taskSignInfo -Force
    $taskManifest.status = 'signed; iPhone acceptance not tested'
    [IO.File]::WriteAllText($taskManifestPath, ($taskManifest | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))
    Write-Host ''
    Write-Host 'Success. Send this file to your iPhone and open it in Shortcuts:' -ForegroundColor Green
    Write-Host $taskOutput
    Write-Host 'See README.md for import and Back Tap setup.'
}
catch {
    $taskExit = 1
    Write-Host ''
    if ($ValidateOnly) { Write-Host 'Local signing validation failed.' -ForegroundColor Yellow }
    else { Write-Host 'Signing did not finish. The unsigned source is still available.' -ForegroundColor Yellow }
    Write-Host $_.Exception.Message
    Write-Host 'See README_Developer.md for build and signing instructions. No API key is needed for signing.'
}
finally {
    if ($taskResponse) { $taskResponse.Close() }
    if ($taskTempCreated -and (Test-Path -LiteralPath $taskTemp)) {
        $taskTempSafe = Get-TaskDistPath $taskTemp
        Remove-Item -LiteralPath $taskTempSafe -Force
    }
    if (-not $NoPause -and -not $ValidateOnly) { [void](Read-Host 'Press Enter to close') }
}
exit $taskExit
