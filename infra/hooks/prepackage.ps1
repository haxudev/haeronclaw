# This script prepares the infra/tmp folder for deployment.
# It assumes the current working directory is the repository root.

$ErrorActionPreference = "Stop"

$TMP_DIR = "infra/tmp"

# Create infra/tmp or clean it if it already exists
if (Test-Path $TMP_DIR) {
    Write-Host "Cleaning existing $TMP_DIR..."
    $emptyDir = Join-Path $env:TEMP ("fma-empty-" + [guid]::NewGuid().ToString())
    New-Item -ItemType Directory -Path $emptyDir -Force | Out-Null
    try {
        # Mirror an empty directory into infra/tmp instead of deleting the
        # directory itself. This is more reliable on Windows for large trees.
        & robocopy $emptyDir $TMP_DIR /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) {
            throw "robocopy failed while cleaning $TMP_DIR (exit code: $LASTEXITCODE)."
        }
    }
    finally {
        Remove-Item -LiteralPath $emptyDir -Recurse -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "Creating $TMP_DIR..."
    New-Item -ItemType Directory -Path $TMP_DIR -Force | Out-Null
}

# Copy contents of src into infra/tmp (including hidden files)
Write-Host "Copying src contents to $TMP_DIR..."
Copy-Item -Path "src/*" -Destination $TMP_DIR -Recurse -Force
# Also copy hidden directories (.github, .vscode)
Get-ChildItem -Path "src" -Hidden -Directory | ForEach-Object {
    Copy-Item -Path $_.FullName -Destination "$TMP_DIR/$($_.Name)" -Recurse -Force
}

# Copy contents of infra/assets into infra/tmp (overwriting if necessary, including hidden files)
Write-Host "Copying infra/assets contents to $TMP_DIR..."
Copy-Item -Path "infra/assets/*" -Destination $TMP_DIR -Recurse -Force
# Also copy hidden items from infra/assets
Get-ChildItem -Path "infra/assets" -Hidden | ForEach-Object {
    Copy-Item -Path $_.FullName -Destination "$TMP_DIR/$($_.Name)" -Recurse -Force
}

# Merge extra-requirements.txt into requirements.txt
$extraReqs = "$TMP_DIR/extra-requirements.txt"
if (Test-Path $extraReqs) {
    Write-Host "Merging extra-requirements.txt into requirements.txt..."
    $reqsFile = "$TMP_DIR/requirements.txt"
    Add-Content -Path $reqsFile -Value ""
    Get-Content $extraReqs | Add-Content -Path $reqsFile
    Remove-Item $extraReqs
}

Write-Host "prepackage.ps1 completed successfully."
exit 0
