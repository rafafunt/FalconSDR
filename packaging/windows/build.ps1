# Builds FalconSDR for Windows: a self-contained PyInstaller --onedir bundle
# (its own Python + numpy/scipy/pyrtlsdrlib + the web UI), shipped two ways:
#   out\FalconSDR-<version>-windows-x64-portable.zip  - unzip and run falconsdr.exe
#   out\FalconSDR-<version>-windows-x64-setup.exe     - Inno Setup installer
# The Windows counterpart of packaging/linux-deb/build.sh - same PyInstaller
# flags and the same reasons for them (AGENTS.md gotcha 35); see README.md here.
#
# Built in a throwaway venv (build-venv\, never the project's .venv), and the
# built app is then checked with a stripped PATH and an empty user profile,
# so nothing from this machine's own setup can make a broken build look fine.
#
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
#   options: -Python 3.12   -SkipInstaller (ZIP only, no Inno Setup needed)
param(
    [string]$Python = "3.12",
    [switch]$SkipInstaller
)
$ErrorActionPreference = "Stop"

$ScriptDir = $PSScriptRoot
$RepoRoot = (Resolve-Path "$ScriptDir\..\..").Path
$Build = "$ScriptDir\build"
$Venv = "$ScriptDir\build-venv"
$Out = "$ScriptDir\out"
$Version = (Select-String -Path "$RepoRoot\falconsdr\__init__.py" -Pattern '^__version__ = "(.+)"').Matches[0].Groups[1].Value
$Dist = "$Build\dist\falconsdr"

function Step($text) { Write-Host "-- $text --" -ForegroundColor Cyan }
function Check($ok, $text) { if (-not $ok) { throw $text } }

Write-Host "== FalconSDR Windows build =="
Write-Host "repo root: $RepoRoot"
Write-Host "version:   $Version"

Step "1/7: fresh build venv (Python $Python)"
if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
& py "-$Python" -m venv $Venv
Check ($LASTEXITCODE -eq 0) "Python $Python not found - install it or pass -Python <version> (see: py -0)"
$VPy = "$Venv\Scripts\python.exe"
& $VPy -m pip install --upgrade pip --quiet --disable-pip-version-check
& $VPy -m pip install --quiet --disable-pip-version-check -r "$RepoRoot\requirements.txt" pyinstaller
Check ($LASTEXITCODE -eq 0) "pip install failed"

Step "2/7: PyInstaller (--onedir, no console window)"
if (Test-Path $Build) { Remove-Item -Recurse -Force $Build }
New-Item -ItemType Directory -Force $Build | Out-Null
$v = $Version.Split(".") | ForEach-Object { [int]$_ }
@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($($v[0]), $($v[1]), $($v[2]), 0), prodvers=($($v[0]), $($v[1]), $($v[2]), 0),
                    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'rafafunt'),
      StringStruct('FileDescription', 'FalconSDR'),
      StringStruct('FileVersion', '$Version'),
      StringStruct('InternalName', 'falconsdr'),
      StringStruct('LegalCopyright', 'GPL-3.0-or-later'),
      StringStruct('OriginalFilename', 'falconsdr.exe'),
      StringStruct('ProductName', 'FalconSDR'),
      StringStruct('ProductVersion', '$Version')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@ | Set-Content -Encoding UTF8 "$Build\version_info.txt"

# --collect-data tzdata / pyrtlsdrlib: both are loaded at runtime in ways
# PyInstaller's import analysis can't see (zoneinfo's fallback; librtlsdr.dll
# found via importlib.resources) - silently dropped without these. Windows
# has no system time zone database at all, so tzdata is not optional here.
# --python-option "X utf8": Python's UTF-8 mode, only a safety net - every text
# read/write in the code names its encoding (Windows' default is cp1250).
# --noconsole: the app runs in the background with its UI in the browser;
# main.py sends its log to ~\.config\FalconSDR\falconsdr.log instead.
Push-Location $RepoRoot
try {
    & "$Venv\Scripts\pyinstaller.exe" `
        --name falconsdr `
        --onedir `
        --noconsole `
        --noconfirm `
        --icon "$ScriptDir\..\icons\falconsdr.ico" `
        --version-file "$Build\version_info.txt" `
        --add-data "$RepoRoot\falconsdr\locale\pl.po;falconsdr\locale" `
        --add-data "$RepoRoot\falconsdr\presets\band_presets.json;falconsdr\presets" `
        --add-data "$RepoRoot\falconsdr\web\static;falconsdr\web\static" `
        --collect-data tzdata `
        --collect-data pyrtlsdrlib `
        --collect-data imageio_ffmpeg `
        --collect-all pip `
        --python-option "X utf8" `
        --distpath "$Build\dist" `
        --workpath "$Build\work" `
        --specpath "$Build" `
        main.py
    Check ($LASTEXITCODE -eq 0) "PyInstaller failed"
} finally { Pop-Location }
Check (Test-Path "$Dist\falconsdr.exe") "falconsdr.exe was not produced"
# python3.dll (stable ABI): nothing in the app itself needs it, so PyInstaller
# leaves it out - but add-on wheels built for abi3 (PyAV, used by in-app
# transcription) fail to load without it ("DLL load failed while importing _core").
$BasePython = & $VPy -c "import sys; print(sys.base_prefix)"
Copy-Item "$BasePython\python3.dll" "$Dist\_internal\"
# libhackrf + libairspy (+ libusb, libwinpthread): HackRF/Airspy without SoapySDR (sdr_native.py, AGENTS.md gotcha 55).
# Pinned downloads, checked by SHA-256 - see fetch_native_libs.py for why conda-forge and how to update.
Step "2b: native receiver libraries (libhackrf, libairspy)"
& $VPy -m pip install --quiet --disable-pip-version-check zstandard
Check ($LASTEXITCODE -eq 0) "pip install zstandard failed"
$Native = "$Build\native"
& $VPy "$ScriptDir\fetch_native_libs.py" $Native
Check ($LASTEXITCODE -eq 0) "fetching the native receiver libraries failed"
Copy-Item "$Native\*.dll" "$Dist\_internal\"
# License texts of everything bundled (BSD/MIT/Apache need them next to a
# binary distribution; ffmpeg's GPL source pointer) - ships in the ZIP and installer.
& $VPy "$ScriptDir\..\collect_licenses.py" "$Dist\THIRD-PARTY-LICENSES.txt"
Check ($LASTEXITCODE -eq 0) "collecting third-party licenses failed"
Get-Content -Raw -Encoding UTF8 "$Native\LICENSES.txt" | Add-Content -Encoding UTF8 "$Dist\THIRD-PARTY-LICENSES.txt"

# The checks run the built exe the way a machine with nothing installed would:
# empty profile, PATH without Python, SDR programs or this repo's DLLs.
$TestHome = Join-Path ([IO.Path]::GetTempPath()) "falconsdr-build-test-$PID"
function Invoke-Clean([string[]]$ArgList, [switch]$NoWait) {
    New-Item -ItemType Directory -Force $TestHome | Out-Null
    $saved = @{ PATH = $env:PATH; USERPROFILE = $env:USERPROFILE; HOME = $env:HOME }
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    $env:USERPROFILE = $TestHome
    $env:HOME = $TestHome
    try {
        return Start-Process -FilePath "$Dist\falconsdr.exe" -ArgumentList $ArgList -PassThru -Wait:(-not $NoWait) -WindowStyle Hidden
    } finally {
        $env:PATH = $saved.PATH; $env:USERPROFILE = $saved.USERPROFILE; $env:HOME = $saved.HOME
    }
}
$TestLog = "$TestHome\.config\FalconSDR\falconsdr.log"
# A real user's profile holds non-ASCII data (Polish band names, transcripts with
# typographic quotes) - an empty one once hid a cp1250 decode crash at startup.
$TestLabel = "Lotnictwo $([char]0x0141)$([char]0x0105)czno$([char]0x015B)$([char]0x0107) $([char]0x2018)test$([char]0x2019)"
function Initialize-TestProfile {
    $utf8 = New-Object System.Text.UTF8Encoding $false
    New-Item -ItemType Directory -Force "$TestHome\.config\FalconSDR", "$TestHome\.local\share\FalconSDR" | Out-Null
    [IO.File]::WriteAllText("$TestHome\.config\FalconSDR\config.json",
        "{`"bands`": [{`"start_hz`": 118000000, `"stop_hz`": 136975000, `"step_hz`": 8333, `"label`": `"$TestLabel`"}]}", $utf8)
    [IO.File]::WriteAllText("$TestHome\.local\share\FalconSDR\transcription_cache.json",
        "{`"x|1|1`": {`"text`": `"$TestLabel`"}}", $utf8)
}

Step "3/7: self-test of the built app (clean PATH, empty profile)"
$p = Invoke-Clean @("--self-test")
if (Test-Path $TestLog) { Get-Content $TestLog | Write-Host }
Check ($p.ExitCode -eq 0) "self-test FAILED (exit $($p.ExitCode)) - the bundle is missing something, see above"

# The in-app transcription installer runs the bundled pip (`--pip`, main.py);
# installing a wheel with console scripts exercises the launcher-template
# lookup that once failed only at the very end of a 400 s real install.
$Wheels = "$Build\wheels"
& $VPy -m pip download pip --no-deps --only-binary=:all: --disable-pip-version-check -q -d $Wheels
Check ($LASTEXITCODE -eq 0) "downloading the test wheel failed"
$pipLog = "$Build\pip-test.log"
New-Item -ItemType Directory -Force $TestHome | Out-Null
$p = Start-Process -FilePath "$Dist\falconsdr.exe" -Wait -PassThru -WindowStyle Hidden -RedirectStandardOutput $pipLog `
    -ArgumentList @("--pip", "install", "--no-index", "--no-deps", "--disable-pip-version-check", "--target", "`"$TestHome\addons-test`"", "`"$((Get-ChildItem "$Wheels\*.whl")[0].FullName)`"")
Get-Content $pipLog | Write-Host
Check ($p.ExitCode -eq 0 -and (Test-Path "$TestHome\addons-test\bin\pip.exe")) "the bundled pip could not install a wheel (exit $($p.ExitCode))"
Write-Host "OK    bundled pip installs wheels (in-app transcription installer)"

Step "4/7: smoke test - the built app serves its UI and quits cleanly"
$port = 18765
Initialize-TestProfile
$p = Invoke-Clean @("--no-browser", "--port", "$port") -NoWait
try {
    $ok = $false
    for ($i = 0; $i -lt 60 -and -not $ok; $i++) {
        Start-Sleep -Milliseconds 500
        try { $ok = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$port/api/session" -TimeoutSec 2).StatusCode -eq 200 } catch {}
    }
    if (-not $ok -and (Test-Path $TestLog)) { Get-Content $TestLog | Write-Host }
    Check $ok "the built app did not start its web server - log above"
    $s = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$port/api/settings").Content | ConvertFrom-Json
    Check ($s.config.bands[0].label -eq $TestLabel) "non-ASCII band name did not survive loading"
    Write-Host "OK    non-ASCII profile data loaded"
    foreach ($url in @("/", "/js/main.js", "/api/settings", "/api/i18n?lang=pl")) {
        $r = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$port$url" -TimeoutSec 5
        Check ($r.StatusCode -eq 200) "GET $url -> $($r.StatusCode)"
        Write-Host "OK    GET $url ($($r.RawContentLength) bytes)"
    }
    Invoke-WebRequest -UseBasicParsing -Method Post "http://127.0.0.1:$port/api/quit" -Headers @{ "X-FalconSDR" = "1" } -ContentType "application/json" -Body "{}" | Out-Null
    Check ($p.WaitForExit(15000)) "the app did not quit within 15 s after /api/quit"
    Write-Host "OK    quit cleanly"
} finally {
    if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force }
    Remove-Item -Recurse -Force $TestHome -ErrorAction SilentlyContinue
}

Step "5/7: portable ZIP"
New-Item -ItemType Directory -Force $Out | Out-Null
$Stage = "$Build\zip\FalconSDR"
New-Item -ItemType Directory -Force $Stage | Out-Null
Copy-Item -Recurse "$Dist\*" $Stage
Copy-Item "$ScriptDir\DRIVER-pl.txt" "$Stage\Sterownik RTL-SDR (Zadig) - PRZECZYTAJ.txt"
Copy-Item "$ScriptDir\DRIVER-en.txt" "$Stage\RTL-SDR driver (Zadig) - README.txt"
Copy-Item "$RepoRoot\LICENSE" "$Stage\LICENSE.txt"
$Zip = "$Out\FalconSDR-$Version-windows-x64-portable.zip"
if (Test-Path $Zip) { Remove-Item -Force $Zip }
# tar.exe (bsdtar, built into Windows 10+) - far faster than Compress-Archive.
& "$env:SystemRoot\System32\tar.exe" -a -c -f $Zip -C "$Build\zip" FalconSDR
Check ($LASTEXITCODE -eq 0) "creating the ZIP failed"

Step "6/7: installer (Inno Setup)"
$Setup = $null
if ($SkipInstaller) {
    Write-Host "skipped (-SkipInstaller)"
} else {
    $Iscc = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    Check $Iscc "Inno Setup 6 not found - install it (winget install JRSoftware.InnoSetup) or pass -SkipInstaller"
    & $Iscc /Q "/DAppVersion=$Version" "/DSourceDir=$Dist" "/DOutputDir=$Out" "$ScriptDir\falconsdr.iss"
    Check ($LASTEXITCODE -eq 0) "Inno Setup failed"
    $Setup = "$Out\FalconSDR-$Version-windows-x64-setup.exe"
}

Step "7/7: checksums + Windows Sandbox test config"
Get-ChildItem $Out -Include *.zip, *.exe -Recurse | Get-FileHash -Algorithm SHA256 |
    ForEach-Object { "$($_.Hash.ToLower())  $(Split-Path -Leaf $_.Path)" } |
    Set-Content -Encoding ASCII "$Out\SHA256SUMS.txt"
# Windows Sandbox = a clean, throwaway Windows with nothing installed - the
# honest "first launch on someone else's PC" test. The out\ folder is mapped
# read-only onto the sandbox desktop.
@"
<Configuration>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>$Out</HostFolder>
      <SandboxFolder>C:\Users\WDAGUtilityAccount\Desktop\FalconSDR-build</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>explorer.exe C:\Users\WDAGUtilityAccount\Desktop\FalconSDR-build</Command>
  </LogonCommand>
</Configuration>
"@ | Set-Content -Encoding UTF8 "$Out\test-in-sandbox.wsb"

Write-Host ""
Write-Host "== done ==" -ForegroundColor Green
Get-ChildItem $Out | Format-Table Name, @{ n = "MB"; e = { [math]::Round($_.Length / 1MB, 1) } } -AutoSize
Write-Host "Clean-machine test: double-click $Out\test-in-sandbox.wsb (needs Windows Sandbox, see README.md)"
