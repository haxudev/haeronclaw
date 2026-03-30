<#
.SYNOPSIS
    Upload m365-cli credentials to Container App as encrypted secret (zero key exposure).

.DESCRIPTION
    Stores m365-cli credentials as a Container App encrypted secret with secretRef mapping.
    - Secret is encrypted at rest by the CA platform
    - Secret values are NOT returned by ARM API (only names)
    - Env var shows only "secretref:m365-creds", never the actual value
    - No Key Vault, no RBAC assignment, no extra permissions needed

.PARAMETER ContainerAppName
    Name of the target Container App.

.PARAMETER ResourceGroupName
    Resource group containing the Container App.

.PARAMETER CredentialsPath
    Path to local m365-cli credentials.json. Default: ~/.m365-cli/credentials.json

.EXAMPLE
    .\setup-kv-credentials.ps1
    .\setup-kv-credentials.ps1 -ContainerAppName "my-app" -CredentialsPath "C:\creds.json"
#>

param(
    [string]$ContainerAppName = "",
    [string]$ResourceGroupName = "",
    [System.IO.FileInfo]$CredentialsPath = (Join-Path $env:USERPROFILE ".m365-cli\credentials.json")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ([string]::IsNullOrWhiteSpace($ContainerAppName) -or [string]::IsNullOrWhiteSpace($ResourceGroupName)) {
    throw "ContainerAppName and ResourceGroupName are required. Example: .\\setup-kv-credentials.ps1 -ContainerAppName <app-name> -ResourceGroupName <resource-group>"
}

function Write-Step { param([string]$msg) Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok   { param([string]$msg) Write-Host "    [OK] $msg" -ForegroundColor Green }
function Write-Fail { param([string]$msg) Write-Host "    [FAIL] $msg" -ForegroundColor Red }

# 1. Validate local credentials
Write-Step "Validating local credentials"

if (-not (Test-Path $CredentialsPath)) {
    Write-Fail "Credentials file not found: $CredentialsPath"
    Write-Host "  Run 'm365 login' locally first."
    exit 1
}

$credsRaw = Get-Content $CredentialsPath -Raw -Encoding UTF8
try { $null = $credsRaw | ConvertFrom-Json } catch {
    Write-Fail "Invalid JSON in $CredentialsPath"
    exit 1
}

$b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($credsRaw.Trim()))
Write-Ok "Credentials valid ($($b64.Length) chars base64)"

# 2. Store as CA encrypted secret
Write-Step "Storing credentials as encrypted Container App secret"

az containerapp secret set `
    --name $ContainerAppName `
    --resource-group $ResourceGroupName `
    --secrets "m365-creds=$b64" `
    --only-show-errors -o none 2>&1

if ($LASTEXITCODE -ne 0) {
    Write-Fail "Failed to set CA secret"
    exit 1
}
Write-Ok "Secret 'm365-creds' stored (encrypted at rest, invisible via API)"

# 3. Remove old plain-text env var (if any)
Write-Step "Removing any old plain-text env var"

az containerapp update `
    --name $ContainerAppName `
    --resource-group $ResourceGroupName `
    --remove-env-vars "M365_CLI_CREDENTIALS" `
    --only-show-errors -o none 2>$null

# 4. Map env var to secretref
Write-Step "Mapping M365_CLI_CREDENTIALS to secretref"

az containerapp update `
    --name $ContainerAppName `
    --resource-group $ResourceGroupName `
    --set-env-vars "M365_CLI_CREDENTIALS=secretref:m365-creds" `
    --only-show-errors -o none 2>&1

if ($LASTEXITCODE -ne 0) {
    Write-Fail "Failed to set env var"
    exit 1
}
Write-Ok "M365_CLI_CREDENTIALS -> secretref:m365-creds"

# 5. Verify
Write-Step "Verifying"

$envVar = az containerapp show `
    --name $ContainerAppName `
    --resource-group $ResourceGroupName `
    --query "properties.template.containers[0].env[?name=='M365_CLI_CREDENTIALS'] | [0]" `
    -o json 2>$null | ConvertFrom-Json

if ($envVar.secretRef -eq "m365-creds" -and [string]::IsNullOrEmpty($envVar.value)) {
    Write-Ok "Env var correctly references secret (no plain value exposed)"
} else {
    Write-Fail "Verification failed: $($envVar | ConvertTo-Json -Compress)"
    exit 1
}

Write-Host "`n" -NoNewline
Write-Host "============================================" -ForegroundColor Green
Write-Host " Credentials setup complete!" -ForegroundColor Green
Write-Host " Container App: $ContainerAppName" -ForegroundColor Green
Write-Host " Secret: encrypted CA secret (m365-creds)" -ForegroundColor Green
Write-Host " Env var: M365_CLI_CREDENTIALS=secretref" -ForegroundColor Green
Write-Host " Exposure: ZERO (not visible via ARM API)" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green