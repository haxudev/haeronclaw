<#
.SYNOPSIS
    Deploy to the DEV environment — completely isolated from production.

.DESCRIPTION
    This script deploys HaeronClaw to a pre-selected set of
    non-production Azure resources for development and testing, including a
    separate Dev Bot Service for end-to-end Teams testing.

    ISOLATION GUARANTEES:
    - Uses a different Container App than production
    - Uses a different ACR than production
    - Uses a different Storage Account than production
    - Uses a separate Dev Bot Service for Teams validation
    - Optionally uses a separate dev UAMI for bot auth
    - May share AI services with RBAC isolation when your environment is configured that way

    DUAL IDENTITY ARCHITECTURE:
    - AZURE_MANAGED_IDENTITY_CLIENT_ID -> shared UAMI for storage/AI/Foundry access
    - BOT_APP_ID -> optional dev UAMI for Teams bot authentication
    - When both are used, both UAMIs must be assigned to the dev container app

    PRODUCTION IS NEVER TOUCHED.

.PARAMETER ImageTag
    Tag for the container image. Defaults to "dev-latest".

.PARAMETER Model
    The model to use. Defaults to "github:gpt-5.4".

.PARAMETER GitHubToken
    Optional GitHub token for the agent. If empty, per-user OAuth tokens are used at request time.

.PARAMETER SkipSmokeTest
    Skip the post-deployment smoke test.

.PARAMETER SkipBotSetup
    Skip dev bot endpoint update and UAMI assignment (HTTP-only testing mode).

.EXAMPLE
    # Standard dev deployment (full Teams E2E)
    .\scripts\deploy-dev.ps1

    # HTTP-only testing (no bot)
    .\scripts\deploy-dev.ps1 -SkipBotSetup

    # Deploy with a specific image tag
    .\scripts\deploy-dev.ps1 -ImageTag "dev-v2"

    # Deploy with a different model
    .\scripts\deploy-dev.ps1 -Model "github:gpt-5.4"
#>

param(
    [Parameter(Mandatory = $false)]
    [string]$ImageTag = "dev-latest",

    [Parameter(Mandatory = $false)]
    [string]$Model = "github:gpt-5.4",

    [Parameter(Mandatory = $false)]
    [string]$GitHubToken = "",

    [Parameter(Mandatory = $false)]
    [switch]$SkipSmokeTest,

    [Parameter(Mandatory = $false)]
    [switch]$SkipBotSetup
)

$ErrorActionPreference = "Stop"

# Navigate to repo root
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

# Load dev parameters
$paramsFile = Join-Path $PSScriptRoot "deploy.dev.parameters.local.json"
$sampleParamsFile = Join-Path $PSScriptRoot "deploy.dev.parameters.sample.json"
if (-not (Test-Path $paramsFile)) {
    if (Test-Path $sampleParamsFile) {
        throw "Dev parameters file not found: $paramsFile. Copy $sampleParamsFile to deploy.dev.parameters.local.json and fill in your environment values."
    }
    throw "Dev parameters file not found: $paramsFile"
}
$params = Get-Content $paramsFile -Raw | ConvertFrom-Json

# ── Safety check: verify we're targeting the dev resources, NOT production ──
$protectedContainerApp = [string]$params.protectedContainerAppName
$protectedAcr = [string]$params.protectedAcrName
$protectedStorage = [string]$params.protectedStorageAccountName
$protectedBotName = [string]$params.protectedBotName

if (-not [string]::IsNullOrWhiteSpace($protectedContainerApp) -and $params.targetContainerAppName -eq $protectedContainerApp) {
    throw "SAFETY: targetContainerAppName points to a protected production app ($protectedContainerApp). Aborting."
}
if (-not [string]::IsNullOrWhiteSpace($protectedAcr) -and $params.targetAcrName -eq $protectedAcr) {
    throw "SAFETY: targetAcrName points to a protected production registry ($protectedAcr). Aborting."
}
if (-not [string]::IsNullOrWhiteSpace($protectedStorage) -and $params.existingStorageAccountName -eq $protectedStorage) {
    throw "SAFETY: existingStorageAccountName points to a protected production storage account ($protectedStorage). Aborting."
}
if (-not $SkipBotSetup) {
    if ([string]::IsNullOrWhiteSpace($params.devBotName)) {
        throw "SAFETY: devBotName is not set in parameters. Set it or use -SkipBotSetup."
    }
    if (-not [string]::IsNullOrWhiteSpace($protectedBotName) -and $params.devBotName -eq $protectedBotName) {
        throw "SAFETY: devBotName points to a protected production bot ($protectedBotName). Aborting."
    }
    if ($params.devBotAppId -eq $params.existingIdentityClientId) {
        throw "SAFETY: devBotAppId is the same as the prod UAMI. The dev bot must use a separate identity."
    }
}

# Determine effective bot settings
# NOTE: Always pass devBotAppId so BOT_APP_ID env var is set correctly even
# when -SkipBotSetup is used.  SkipBotSetup only skips the Bot Service
# endpoint update and UAMI assignment, NOT the runtime auth config.
$effectiveSkipBot = [bool]$SkipBotSetup
$effectiveBotAppId = $params.devBotAppId
$effectiveBotName = if ($SkipBotSetup) { "" } else { $params.devBotName }

Write-Host ""
Write-Host "╔══════════════════════════════════════════════════════════════╗"
Write-Host "║              DEV ENVIRONMENT DEPLOYMENT                     ║"
Write-Host "╠══════════════════════════════════════════════════════════════╣"
Write-Host "║  Container App : $($params.targetContainerAppName)"
Write-Host "║  ACR           : $($params.targetAcrName)"
Write-Host "║  Storage       : $($params.existingStorageAccountName)"
Write-Host "║  Model         : $Model"
Write-Host "║  Image Tag     : $ImageTag"
if ($SkipBotSetup) {
    Write-Host "║  Bot Update    : SKIPPED (HTTP-only mode)"
} else {
    Write-Host "║  Dev Bot       : $effectiveBotName"
    Write-Host "║  Dev Bot AppId : $effectiveBotAppId"
}
Write-Host "╚══════════════════════════════════════════════════════════════╝"
Write-Host ""

# ── Ensure dev storage has required blob containers ──
Write-Host "==> Ensuring 'agent-files' blob container exists in dev storage..."
$existingContainers = az storage container list `
    --account-name $params.existingStorageAccountName `
    --auth-mode login `
    --query "[].name" -o json 2>$null | ConvertFrom-Json

if ($existingContainers -notcontains "agent-files") {
    az storage container create `
        --name "agent-files" `
        --account-name $params.existingStorageAccountName `
        --auth-mode login `
        --only-show-errors 2>&1 | Out-Null
    Write-Host "    Created 'agent-files' container."
} else {
    Write-Host "    'agent-files' container already exists."
}

# ── Invoke the existing ACA deployment script with dev parameters ──
$deployArgs = @{
    ResourceGroup            = $params.resourceGroup
    Location                 = $params.location
    Prefix                   = $params.prefix
    Model                    = $Model
    ImageTag                 = $ImageTag
    ExistingStorageAccountName = $params.existingStorageAccountName
    ExistingIdentityId       = $params.existingIdentityId
    ExistingIdentityClientId = $params.existingIdentityClientId
    BotAppId                 = $effectiveBotAppId
    BotName                  = $effectiveBotName
    BotResourceGroup         = $params.resourceGroup
    SkipBotEndpointUpdate    = $effectiveSkipBot
    SkipSmokeTest            = [bool]$SkipSmokeTest
    InPlaceUpdate            = $true
    TargetContainerAppName   = $params.targetContainerAppName
    TargetAcrName            = $params.targetAcrName
}

if (-not [string]::IsNullOrWhiteSpace($GitHubToken)) {
    $deployArgs.GitHubToken = $GitHubToken
} elseif (-not [string]::IsNullOrWhiteSpace($params.githubToken)) {
    $deployArgs.GitHubToken = $params.githubToken
}

& "$PSScriptRoot/deploy-aca-functions.ps1" @deployArgs

if ($LASTEXITCODE -ne 0) {
    throw "Dev deployment failed."
}

# ── Assign dev bot UAMI to container app AFTER deployment ──
# (must happen after deploy-aca-functions.ps1 which may reset identity config)
if (-not $SkipBotSetup -and -not [string]::IsNullOrWhiteSpace($params.devBotIdentityId)) {
    Write-Host "==> Ensuring dev bot UAMI is assigned to container app..."
    $existingIdentities = az containerapp identity show `
        --name $params.targetContainerAppName `
        --resource-group $params.resourceGroup `
        --query "userAssignedIdentities" `
        -o json 2>$null

    $devUamiAlreadyAssigned = $false
    if ($existingIdentities -and $existingIdentities -ne "null") {
        $identityObj = $existingIdentities | ConvertFrom-Json
        if ($identityObj.PSObject.Properties.Name -contains $params.devBotIdentityId) {
            $devUamiAlreadyAssigned = $true
        }
    }

    if ($devUamiAlreadyAssigned) {
        Write-Host "    Dev bot UAMI already assigned."
    } else {
        Write-Host "    Assigning dev bot UAMI: $($params.devBotIdentityId)"
        az containerapp identity assign `
            --name $params.targetContainerAppName `
            --resource-group $params.resourceGroup `
            --user-assigned $params.devBotIdentityId `
            --only-show-errors 2>&1 | Out-Null
        Write-Host "    Dev bot UAMI assigned."
    }
}

# ── Print dev endpoint ──
$devFqdn = az containerapp show `
    --name $params.targetContainerAppName `
    --resource-group $params.resourceGroup `
    --query "properties.configuration.ingress.fqdn" `
    -o tsv 2>$null

Write-Host ""
Write-Host "╔══════════════════════════════════════════════════════════════╗"
Write-Host "║              DEV DEPLOYMENT COMPLETE                        ║"
Write-Host "╠══════════════════════════════════════════════════════════════╣"
Write-Host "║  Dev URL: https://$devFqdn"
Write-Host "║  Chat:    https://$devFqdn/agent/chat"
Write-Host "║  Health:  https://$devFqdn/"
Write-Host "╠══════════════════════════════════════════════════════════════╣"
if ($SkipBotSetup) {
    Write-Host "║  Bot: SKIPPED (HTTP-only mode)"
} else {
    Write-Host "║  Dev Bot: $effectiveBotName → https://$devFqdn/messages"
}
Write-Host "║  Production: UNTOUCHED (safe)"
Write-Host "╚══════════════════════════════════════════════════════════════╝"
Write-Host ""
