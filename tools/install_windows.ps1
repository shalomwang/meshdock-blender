[CmdletBinding()]
param(
    [string]$BlenderPath = "",
    [string]$PythonCommand = "python",
    [switch]$SkipSidecar,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$workspaceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path

function Find-BlenderExecutable {
    if ($BlenderPath) {
        $resolved = (Resolve-Path -LiteralPath $BlenderPath).Path
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            throw "Blender executable not found: $resolved"
        }
        return $resolved
    }
    $roots = @(
        "E:\Program Files\Blender Foundation",
        "C:\Program Files\Blender Foundation"
    )
    $matches = foreach ($root in $roots) {
        if (Test-Path -LiteralPath $root -PathType Container) {
            Get-ChildItem -LiteralPath $root -Filter blender.exe -File -Recurse -ErrorAction SilentlyContinue
        }
    }
    $selected = $matches | Sort-Object FullName -Descending | Select-Object -First 1
    if (-not $selected) {
        throw "Blender was not found. Pass -BlenderPath with the full blender.exe path."
    }
    return $selected.FullName
}

$blenderExe = Find-BlenderExecutable
if (Get-Process -Name blender -ErrorAction SilentlyContinue) {
    throw "Close Blender before installing or removing the extension."
}

if ($Uninstall) {
    foreach ($extensionId in @("meshdock", "ai3d_asset_pipeline")) {
        try {
            & $blenderExe --command extension remove $extensionId 2>$null
        } catch {
            # Missing extensions need no action.
        }
    }
    Write-Host "Removed MeshDock and its pre-release extension id if installed."
    Write-Host "Staging files and OS credentials were intentionally preserved."
    exit 0
}

& $PythonCommand (Join-Path $workspaceRoot "tools\build_extension.py")
if ($LASTEXITCODE -ne 0) {
    throw "Extension build failed."
}
$package = Get-ChildItem -LiteralPath (Join-Path $workspaceRoot "dist") -Filter "meshdock-*.zip" -File |
    Sort-Object LastWriteTimeUtc -Descending |
    Select-Object -First 1
if (-not $package) {
    throw "Built extension package was not found."
}

# Removing the installed copy first makes this command work for both a clean install and an update.
try {
    & $blenderExe --command extension remove meshdock 2>$null
} catch {
    # A clean first install reports "not installed" on stderr. Installation should continue.
}
# Remove the pre-release id so users do not end up with two panels after the rename.
try {
    & $blenderExe --command extension remove ai3d_asset_pipeline 2>$null
} catch {
    # The old id is optional.
}
& $blenderExe --command extension install-file -r user_default -e $package.FullName
if ($LASTEXITCODE -ne 0) {
    throw "Blender extension installation failed."
}

if (-not $SkipSidecar) {
    & $PythonCommand -m pip uninstall --yes blender-ai-asset-pipeline
    & $PythonCommand -m pip install --user --upgrade $workspaceRoot
    if ($LASTEXITCODE -ne 0) {
        throw "MCP sidecar installation failed. The Blender extension is already installed."
    }
}

Write-Host "Installed MeshDock from $($package.FullName)"
Write-Host "Start Blender, open View3D > MeshDock, and enter provider keys there."
