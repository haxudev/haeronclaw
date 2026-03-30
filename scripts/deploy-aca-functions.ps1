param(
    [Parameter(Mandatory = $true)]
    [string]$ResourceGroup,

    [Parameter(Mandatory = $false)]
    [string]$Location = "eastus2",

    [Parameter(Mandatory = $false)]
    [string]$Prefix = "fmaaca",

    [Parameter(Mandatory = $false)]
    [string]$Model = "github:gpt-5.4",

    [Parameter(Mandatory = $false)]
    [string]$ImageTag = "v1",

    [Parameter(Mandatory = $false)]
    [string]$GitHubToken = "",

    [Parameter(Mandatory = $false)]
    [string]$ExistingStorageAccountName = "",

    [Parameter(Mandatory = $false)]
    [string]$ExistingIdentityId = "",

    [Parameter(Mandatory = $false)]
    [string]$ExistingIdentityClientId = "",

    [Parameter(Mandatory = $false)]
    [string]$BotAppId = "",

    [Parameter(Mandatory = $false)]
    [string]$BotName = "",

    [Parameter(Mandatory = $false)]
    [string]$BotResourceGroup = "",

    [Parameter(Mandatory = $false)]
    [bool]$SkipBotEndpointUpdate = $false,

    [Parameter(Mandatory = $false)]
    [bool]$SkipSmokeTest = $false,

    [Parameter(Mandatory = $false)]
    [bool]$InPlaceUpdate = $true,

    [Parameter(Mandatory = $false)]
    [string]$TargetContainerAppName = "",

    [Parameter(Mandatory = $false)]
    [string]$TargetAcrName = "",

    [Parameter(Mandatory = $false)]
    [bool]$SkipM365Credentials = $false
)

$ErrorActionPreference = "Stop"

function Assert-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found in PATH."
    }
}

function Invoke-Az {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Args,
        [switch]$Capture
    )

    if ($Capture) {
        $output = & az @Args
        if ($LASTEXITCODE -ne 0) {
            throw "Azure CLI command failed: az $($Args -join ' ')"
        }
        return ($output | Out-String).Trim()
    }

    & az @Args
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI command failed: az $($Args -join ' ')"
    }
}

function Set-ContainerAppSecret {
    param(
        [Parameter(Mandatory = $true)]
        [string]$App,
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    $secretOutput = & az containerapp secret set `
        --name $App `
        --resource-group $ResourceGroup `
        --secrets "$Name=$Value" `
        --only-show-errors 2>&1

    if ($LASTEXITCODE -ne 0) {
        $detail = ($secretOutput | Out-String).Trim()
        if ([string]::IsNullOrWhiteSpace($detail)) {
            $detail = "No additional error details returned by Azure CLI."
        }
        throw "Failed to set secret '$Name' on container app '$App'. Details: $detail"
    }
}

function Invoke-WithRetry {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Action,
        [Parameter(Mandatory = $true)]
        [string]$Description,
        [int]$MaxAttempts = 15,
        [int]$DelaySeconds = 6
    )

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            return & $Action
        } catch {
            if ($attempt -eq $MaxAttempts) {
                throw "Failed after $MaxAttempts attempts: $Description. Last error: $($_.Exception.Message)"
            }
            Start-Sleep -Seconds $DelaySeconds
        }
    }
}

function Resolve-BotName {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TargetResourceGroup,
        [string]$ConfiguredBotName,
        [string]$TargetBotAppId
    )

    if (-not [string]::IsNullOrWhiteSpace($ConfiguredBotName)) {
        return $ConfiguredBotName
    }

    if ([string]::IsNullOrWhiteSpace($TargetBotAppId)) {
        return ""
    }

    $resolved = Invoke-Az -Capture -Args @(
        "bot", "list",
        "--resource-group", $TargetResourceGroup,
        "--query", "[?properties.msaAppId=='$TargetBotAppId'].name | [0]",
        "-o", "tsv"
    )

    return $resolved
}

function Resolve-AppNameFromBotEndpoint {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TargetResourceGroup,
        [string]$ConfiguredBotName,
        [string]$TargetBotAppId
    )

    $botName = Resolve-BotName -TargetResourceGroup $TargetResourceGroup -ConfiguredBotName $ConfiguredBotName -TargetBotAppId $TargetBotAppId
    if ([string]::IsNullOrWhiteSpace($botName)) {
        return ""
    }

    $endpoint = Invoke-Az -Capture -Args @(
        "bot", "show",
        "--resource-group", $TargetResourceGroup,
        "--name", $botName,
        "--query", "properties.endpoint",
        "-o", "tsv"
    )

    if ([string]::IsNullOrWhiteSpace($endpoint)) {
        return ""
    }

    try {
        $uri = [Uri]$endpoint
        if ([string]::IsNullOrWhiteSpace($uri.Host)) {
            return ""
        }
        return ($uri.Host -split "\\.")[0]
    } catch {
        return ""
    }
}

Assert-Command -Name "az"

$sanitizedPrefix = ($Prefix.ToLower() -replace "[^a-z0-9-]", "")
if ([string]::IsNullOrWhiteSpace($sanitizedPrefix)) {
    throw "Prefix must contain at least one alphanumeric character."
}

$suffix = (Get-Date -Format "MMddHHmm")
$baseName = "$sanitizedPrefix-$suffix"
$imageName = "haeronclaw"

$effectiveBotResourceGroup = $BotResourceGroup
if ([string]::IsNullOrWhiteSpace($effectiveBotResourceGroup)) {
    $effectiveBotResourceGroup = $ResourceGroup
}

$storageName = (($sanitizedPrefix -replace "-", "") + "st" + $suffix).ToLower()
if ($storageName.Length -gt 24) {
    $storageName = $storageName.Substring(0, 24)
}

$logName = "$baseName-law"
$envName = "$baseName-env"
$appName = "$baseName-func"
$acrName = $TargetAcrName

$useExistingIdentityAndStorage =
    (-not [string]::IsNullOrWhiteSpace($ExistingStorageAccountName)) -and
    (-not [string]::IsNullOrWhiteSpace($ExistingIdentityId)) -and
    (-not [string]::IsNullOrWhiteSpace($ExistingIdentityClientId))

$effectiveBotAppId = $BotAppId
if ([string]::IsNullOrWhiteSpace($effectiveBotAppId) -and $useExistingIdentityAndStorage) {
    # In reuse mode, the existing UAMI client ID is usually the Bot App ID.
    $effectiveBotAppId = $ExistingIdentityClientId
}

$inPlaceMode = $InPlaceUpdate
if ($inPlaceMode) {
    if ([string]::IsNullOrWhiteSpace($appName) -or $appName -eq "$baseName-func") {
        $appName = $TargetContainerAppName
    }

    if ([string]::IsNullOrWhiteSpace($appName)) {
        $appName = Resolve-AppNameFromBotEndpoint `
            -TargetResourceGroup $effectiveBotResourceGroup `
            -ConfiguredBotName $BotName `
            -TargetBotAppId $effectiveBotAppId
    }

    if ([string]::IsNullOrWhiteSpace($appName)) {
        throw "In-place update is enabled but target app could not be resolved. Provide -TargetContainerAppName or ensure Bot endpoint is configured."
    }

    $existingAppId = Invoke-Az -Capture -Args @(
        "containerapp", "show",
        "--name", $appName,
        "--resource-group", $ResourceGroup,
        "--query", "id",
        "-o", "tsv"
    )
    if ([string]::IsNullOrWhiteSpace($existingAppId)) {
        throw "In-place target container app '$appName' was not found in resource group '$ResourceGroup'."
    }

    $existingEnvId = Invoke-Az -Capture -Args @(
        "containerapp", "show",
        "--name", $appName,
        "--resource-group", $ResourceGroup,
        "--query", "properties.managedEnvironmentId",
        "-o", "tsv"
    )
    if (-not [string]::IsNullOrWhiteSpace($existingEnvId)) {
        $envName = ($existingEnvId -split "/")[-1]
    }

    if ([string]::IsNullOrWhiteSpace($acrName)) {
        $acrServerFromApp = Invoke-Az -Capture -Args @(
            "containerapp", "show",
            "--name", $appName,
            "--resource-group", $ResourceGroup,
            "--query", "properties.configuration.registries[0].server",
            "-o", "tsv"
        )
        if (-not [string]::IsNullOrWhiteSpace($acrServerFromApp)) {
            $acrName = ($acrServerFromApp -split "\\.")[0]
        }
    }

    if ([string]::IsNullOrWhiteSpace($acrName)) {
        throw "Unable to determine ACR for in-place update. Provide -TargetAcrName explicitly."
    }

    Write-Host "==> In-place update mode enabled"
    Write-Host "    Target Function App : $appName"
    Write-Host "    Target ACA Env      : $envName"
    Write-Host "    Target ACR          : $acrName"
} else {
    $acrName = (($sanitizedPrefix -replace "-", "") + "acr" + $suffix).ToLower()
    if ($acrName.Length -gt 50) {
        $acrName = $acrName.Substring(0, 50)
    }

    Write-Host "==> Using naming prefix: $sanitizedPrefix"
    Write-Host "==> Creating stack names:"
    Write-Host "    Storage Account : $storageName"
    Write-Host "    ACR             : $acrName"
    Write-Host "    Log Analytics   : $logName"
    Write-Host "    ACA Environment : $envName"
    Write-Host "    Function on ACA : $appName"
}

if ($useExistingIdentityAndStorage) {
    Write-Host "==> Reuse mode enabled: existing storage + user-assigned identity"
    Write-Host "    Existing Storage : $ExistingStorageAccountName"
    Write-Host "    Existing UAMI ID : $ExistingIdentityId"
}

if (-not [string]::IsNullOrWhiteSpace($effectiveBotAppId)) {
    Write-Host "==> Bot App ID configured for runtime auth"
}

Write-Host "==> Preparing deployment payload (infra/tmp)..."
$skipPrepackage = ($env:SKIP_PREPACKAGE ?? "").ToLowerInvariant()
if ($skipPrepackage -in @("1", "true", "yes")) {
    Write-Host "    Reusing existing infra/tmp (SKIP_PREPACKAGE enabled)."
} else {
    & ./infra/hooks/prepackage.ps1
    if ($LASTEXITCODE -ne 0) {
        throw "prepackage.ps1 failed."
    }
}

Write-Host "==> Ensuring Azure Container Apps CLI extension is installed..."
Invoke-Az -Args @("extension", "add", "--name", "containerapp", "--upgrade", "--allow-preview", "true", "--only-show-errors")

if (-not $inPlaceMode) {
    Write-Host "==> Creating Log Analytics workspace..."
    Invoke-Az -Args @(
        "monitor", "log-analytics", "workspace", "create",
        "--resource-group", $ResourceGroup,
        "--location", $Location,
        "--workspace-name", $logName,
        "--only-show-errors"
    )

    $workspaceId = Invoke-Az -Capture -Args @(
        "monitor", "log-analytics", "workspace", "show",
        "--resource-group", $ResourceGroup,
        "--workspace-name", $logName,
        "--query", "customerId",
        "-o", "tsv"
    )

    $workspaceKey = Invoke-Az -Capture -Args @(
        "monitor", "log-analytics", "workspace", "get-shared-keys",
        "--resource-group", $ResourceGroup,
        "--workspace-name", $logName,
        "--query", "primarySharedKey",
        "-o", "tsv"
    )

    Write-Host "==> Creating Container Apps environment..."
    Invoke-Az -Args @(
        "containerapp", "env", "create",
        "--name", $envName,
        "--resource-group", $ResourceGroup,
        "--location", $Location,
        "--logs-workspace-id", $workspaceId,
        "--logs-workspace-key", $workspaceKey,
        "--no-wait",
        "--only-show-errors"
    )

    Write-Host "==> Waiting for Container Apps environment provisioning..."
    $envReady = $false
    for ($attempt = 1; $attempt -le 40; $attempt++) {
        $envState = & az containerapp env show --name $envName --resource-group $ResourceGroup --query properties.provisioningState -o tsv 2>$null
        if ($LASTEXITCODE -eq 0 -and $envState -eq "Succeeded") {
            $envReady = $true
            break
        }
        Start-Sleep -Seconds 15
    }

    if (-not $envReady) {
        throw "Container Apps environment '$envName' did not reach Succeeded state in time."
    }

    if (-not $useExistingIdentityAndStorage) {
        Write-Host "==> Creating Storage Account for AzureWebJobsStorage..."
        Invoke-Az -Args @(
            "storage", "account", "create",
            "--name", $storageName,
            "--resource-group", $ResourceGroup,
            "--location", $Location,
            "--sku", "Standard_LRS",
            "--kind", "StorageV2",
            "--allow-blob-public-access", "false",
            "--allow-shared-key-access", "false",
            "--min-tls-version", "TLS1_2",
            "--only-show-errors"
        )

        $storageId = Invoke-Az -Capture -Args @(
            "storage", "account", "show",
            "--name", $storageName,
            "--resource-group", $ResourceGroup,
            "--query", "id",
            "-o", "tsv"
        )
    } else {
        $storageName = $ExistingStorageAccountName
        $storageId = ""
    }

    Write-Host "==> Creating Azure Container Registry..."
    Invoke-Az -Args @(
        "acr", "create",
        "--name", $acrName,
        "--resource-group", $ResourceGroup,
        "--location", $Location,
        "--sku", "Basic",
        "--admin-enabled", "true",
        "--only-show-errors"
    )
} else {
    if ($useExistingIdentityAndStorage) {
        $storageName = $ExistingStorageAccountName
    }
    $storageId = ""
    Write-Host "==> In-place mode: skipping creation of Log Analytics / ACA Environment / ACR"
}

Write-Host "==> Building and pushing image in ACR..."
Invoke-Az -Args @(
    "acr", "build",
    "--registry", $acrName,
    "--image", "$imageName`:$ImageTag",
    "--file", "infra/tmp/Dockerfile",
    "--no-logs",
    "infra/tmp"
)

$acrServer = Invoke-Az -Capture -Args @("acr", "show", "--name", $acrName, "--resource-group", $ResourceGroup, "--query", "loginServer", "-o", "tsv")
$acrResourceId = Invoke-Az -Capture -Args @("acr", "show", "--name", $acrName, "--resource-group", $ResourceGroup, "--query", "id", "-o", "tsv")
$acrAdminEnabled = (Invoke-Az -Capture -Args @("acr", "show", "--name", $acrName, "--resource-group", $ResourceGroup, "--query", "adminUserEnabled", "-o", "tsv")).ToLowerInvariant()
$registryIdentityId = if ($useExistingIdentityAndStorage -and -not [string]::IsNullOrWhiteSpace($ExistingIdentityId)) { $ExistingIdentityId } else { "" }
$useRegistryIdentity = $false
$acrUser = ""
$acrPass = ""
if (-not [string]::IsNullOrWhiteSpace($registryIdentityId)) {
    $registryIdentityPrincipalId = Invoke-Az -Capture -Args @("identity", "show", "--ids", $registryIdentityId, "--query", "principalId", "-o", "tsv")
    if (-not [string]::IsNullOrWhiteSpace($registryIdentityPrincipalId)) {
        $existingAcrPull = Invoke-Az -Capture -Args @(
            "role", "assignment", "list",
            "--assignee-object-id", $registryIdentityPrincipalId,
            "--scope", $acrResourceId,
            "--query", "[?roleDefinitionName=='AcrPull'] | length(@)",
            "-o", "tsv"
        )

        if ($existingAcrPull -ne "0") {
            $useRegistryIdentity = $true
        } else {
            Write-Warning "Registry identity '$registryIdentityId' does not currently have AcrPull on '$acrName'. Falling back to ACR admin credentials."
        }
    }
}

if (-not $useRegistryIdentity) {
    if ($acrAdminEnabled -ne "true") {
        Write-Warning "Enabling ACR admin user on '$acrName' because managed identity pull is not available."
        Invoke-Az -Args @("acr", "update", "--name", $acrName, "--resource-group", $ResourceGroup, "--admin-enabled", "true", "--only-show-errors")
    }

    $acrUser = Invoke-Az -Capture -Args @("acr", "credential", "show", "--name", $acrName, "--resource-group", $ResourceGroup, "--query", "username", "-o", "tsv")
    $acrPass = Invoke-Az -Capture -Args @("acr", "credential", "show", "--name", $acrName, "--resource-group", $ResourceGroup, "--query", "passwords[0].value", "-o", "tsv")
}

Write-Host "==> Deploying Azure Functions on Container Apps service..."

if ($inPlaceMode) {
    $deployStamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddHHmmss")

    # Refresh registry auth before switching the app to a newly built image.
    if ($useRegistryIdentity) {
        Invoke-WithRetry -Description "Refresh container app registry identity" -Action {
            Invoke-Az -Args @(
                "containerapp", "registry", "set",
                "--name", $appName,
                "--resource-group", $ResourceGroup,
                "--server", $acrServer,
                "--identity", $registryIdentityId,
                "--only-show-errors"
            )
        } | Out-Null
    } else {
        Invoke-WithRetry -Description "Refresh container app registry credentials" -Action {
            Invoke-Az -Args @(
                "containerapp", "registry", "set",
                "--name", $appName,
                "--resource-group", $ResourceGroup,
                "--server", $acrServer,
                "--username", $acrUser,
                "--password", $acrPass,
                "--only-show-errors"
            )
        } | Out-Null
    }

    $updateArgs = @(
        "containerapp", "update",
        "--name", $appName,
        "--resource-group", $ResourceGroup,
        "--image", "$acrServer/$imageName`:$ImageTag",
        "--set-env-vars",
        "COPILOT_MODEL=$Model",
        "ENABLE_BEARER_AUTH=true",
        "REQUIRE_GITHUB_USER_TOKEN=true",
        "PYTHON_ENABLE_INIT_INDEXING=1",
        "AzureWebJobsDisableHomepage=true",
        "DEPLOY_TIMESTAMP=$deployStamp",
        "--only-show-errors"
    )

    Invoke-Az -Args $updateArgs
    $principalId = "(existing identity)"
} elseif ($useExistingIdentityAndStorage) {
    $createArgs = @(
        "containerapp", "create",
        "--name", $appName,
        "--resource-group", $ResourceGroup,
        "--environment", $envName,
        "--kind", "functionapp",
        "--image", "$acrServer/$imageName`:$ImageTag",
        "--registry-server", $acrServer
    )

    if ($useRegistryIdentity) {
        $createArgs += @("--registry-identity", $registryIdentityId)
    } else {
        $createArgs += @("--registry-username", $acrUser, "--registry-password", $acrPass)
    }

    $createArgs += @(
        "--user-assigned", $ExistingIdentityId,
        "--ingress", "external",
        "--target-port", "80",
        "--min-replicas", "1",
        "--max-replicas", "10",
        "--no-wait",
        "--env-vars",
        "AzureWebJobsStorage__accountName=$storageName",
        "AzureWebJobsStorage__credential=managedidentity",
        "AzureWebJobsStorage__clientId=$ExistingIdentityClientId",
        "AzureWebJobsStorage__blobServiceUri=https://$storageName.blob.core.windows.net",
        "AzureWebJobsStorage__queueServiceUri=https://$storageName.queue.core.windows.net",
        "AzureWebJobsStorage__tableServiceUri=https://$storageName.table.core.windows.net",
        "AZURE_MANAGED_IDENTITY_CLIENT_ID=$ExistingIdentityClientId",
        "BOT_APP_ID=$effectiveBotAppId",
        "COPILOT_MODEL=$Model",
        "ENABLE_BEARER_AUTH=true",
        "REQUIRE_GITHUB_USER_TOKEN=true",
        "PYTHON_ENABLE_INIT_INDEXING=1",
        "AzureWebJobsDisableHomepage=true",
        "--only-show-errors"
    )

    Invoke-Az -Args $createArgs
    $principalId = "(reused user-assigned identity)"
} else {
    Invoke-Az -Args @(
        "containerapp", "create",
        "--name", $appName,
        "--resource-group", $ResourceGroup,
        "--environment", $envName,
        "--kind", "functionapp",
        "--image", "$acrServer/$imageName`:$ImageTag",
        "--registry-server", $acrServer,
        "--registry-username", $acrUser,
        "--registry-password", $acrPass,
        "--system-assigned",
        "--ingress", "external",
        "--target-port", "80",
        "--min-replicas", "1",
        "--max-replicas", "10",
        "--no-wait",
        "--env-vars",
        "AzureWebJobsStorage__accountName=$storageName",
        "AzureWebJobsStorage__credential=managedidentity",
        "AzureWebJobsStorage__blobServiceUri=https://$storageName.blob.core.windows.net",
        "AzureWebJobsStorage__queueServiceUri=https://$storageName.queue.core.windows.net",
        "AzureWebJobsStorage__tableServiceUri=https://$storageName.table.core.windows.net",
        "COPILOT_MODEL=$Model",
        "ENABLE_BEARER_AUTH=true",
        "REQUIRE_GITHUB_USER_TOKEN=true",
        "PYTHON_ENABLE_INIT_INDEXING=1",
        "AzureWebJobsDisableHomepage=true",
        "--only-show-errors"
    )

    Write-Host "==> Waiting for container app provisioning..."
    $appReady = $false
    for ($attempt = 1; $attempt -le 40; $attempt++) {
        $appState = & az containerapp show --name $appName --resource-group $ResourceGroup --query properties.provisioningState -o tsv 2>$null
        if ($LASTEXITCODE -eq 0 -and $appState -eq "Succeeded") {
            $appReady = $true
            break
        }
        if ($LASTEXITCODE -eq 0 -and $appState -eq "Failed") {
            throw "Container app '$appName' provisioning failed."
        }
        Start-Sleep -Seconds 15
    }

    if (-not $appReady) {
        throw "Container app '$appName' did not reach Succeeded state in time."
    }

    $principalId = Invoke-Az -Capture -Args @(
        "containerapp", "show",
        "--name", $appName,
        "--resource-group", $ResourceGroup,
        "--query", "identity.principalId",
        "-o", "tsv"
    )

    Write-Host "==> Assigning storage data-plane roles to container app identity..."
    $roles = @(
        "Storage Blob Data Contributor",
        "Storage Queue Data Contributor",
        "Storage Table Data Contributor"
    )

    foreach ($role in $roles) {
        Invoke-Az -Args @(
            "role", "assignment", "create",
            "--assignee-object-id", $principalId,
            "--assignee-principal-type", "ServicePrincipal",
            "--role", $role,
            "--scope", $storageId,
            "--only-show-errors"
        )
    }

    Start-Sleep -Seconds 20

    Write-Host "==> Triggering an update to pick up role assignments..."
    $rbacSyncTs = (Get-Date).ToUniversalTime().ToString("yyyyMMddHHmmss")
    Invoke-Az -Args @(
        "containerapp", "update",
        "--name", $appName,
        "--resource-group", $ResourceGroup,
        "--set-env-vars", "RBAC_SYNC_TS=$rbacSyncTs",
        "--only-show-errors"
    )
}

Write-Host "==> Waiting for container app provisioning..."
$appReady = $false
for ($attempt = 1; $attempt -le 40; $attempt++) {
    $appState = & az containerapp show --name $appName --resource-group $ResourceGroup --query properties.provisioningState -o tsv 2>$null
    if ($LASTEXITCODE -eq 0 -and $appState -eq "Succeeded") {
        $appReady = $true
        break
    }
    if ($LASTEXITCODE -eq 0 -and $appState -eq "Failed") {
        throw "Container app '$appName' provisioning failed."
    }
    Start-Sleep -Seconds 15
}

if (-not $appReady) {
    throw "Container app '$appName' did not reach Succeeded state in time."
}

# Persist an app-level GitHub token so Teams bot replies continue to work
# even when no per-user x-github-token is provided.
if (-not [string]::IsNullOrWhiteSpace($GitHubToken)) {
    Write-Host "==> Setting GitHub token secret on container app..."
    Invoke-WithRetry -Description "Set container app github-token secret" -Action {
        Set-ContainerAppSecret -App $appName -Name "github-token" -Value $GitHubToken
    } | Out-Null

    Invoke-WithRetry -Description "Set GITHUB_TOKEN env var from secret" -Action {
        Invoke-Az -Args @(
            "containerapp", "update",
            "--name", $appName,
            "--resource-group", $ResourceGroup,
            "--set-env-vars", "GITHUB_TOKEN=secretref:github-token",
            "--only-show-errors"
        )
    } | Out-Null
} elseif ($Model.StartsWith("github:")) {
    Write-Warning "No GitHub token configured. Teams bot may fail to reply unless each request carries a user GitHub OAuth token."
}

# ── M365 CLI Credentials ──
if (-not $SkipM365Credentials) {
    $m365CredsPath = Join-Path $env:USERPROFILE ".m365-cli\credentials.json"
    if (Test-Path $m365CredsPath) {
        Write-Host "==> Uploading M365 CLI credentials as encrypted Container App secret..."
        $credsRaw = Get-Content $m365CredsPath -Raw -Encoding UTF8
        try {
            $null = $credsRaw | ConvertFrom-Json  # validate JSON
            $b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($credsRaw.Trim()))

            Invoke-WithRetry -Description "Set m365-creds secret" -Action {
                Set-ContainerAppSecret -App $appName -Name "m365-creds" -Value $b64
            } | Out-Null

            Invoke-WithRetry -Description "Map M365_CLI_CREDENTIALS env var" -Action {
                Invoke-Az -Args @(
                    "containerapp", "update",
                    "--name", $appName,
                    "--resource-group", $ResourceGroup,
                    "--set-env-vars", "M365_CLI_CREDENTIALS=secretref:m365-creds",
                    "--only-show-errors"
                )
            } | Out-Null

            Write-Host "    M365 credentials configured (secretref:m365-creds)."
        } catch {
            Write-Warning "M365 credentials file found but invalid JSON — skipping. Error: $_"
        }
    } else {
        Write-Warning "No local M365 credentials (~/.m365-cli/credentials.json) — M365 features (email, SharePoint) will be unavailable."
    }
}

if (-not [string]::IsNullOrWhiteSpace($effectiveBotAppId)) {
    Invoke-Az -Args @(
        "containerapp", "update",
        "--name", $appName,
        "--resource-group", $ResourceGroup,
        "--set-env-vars", "BOT_APP_ID=$effectiveBotAppId",
        "--only-show-errors"
    )
}

$fqdn = Invoke-Az -Capture -Args @(
    "containerapp", "show",
    "--name", $appName,
    "--resource-group", $ResourceGroup,
    "--query", "properties.configuration.ingress.fqdn",
    "-o", "tsv"
)

$baseUrl = "https://$fqdn"

if (-not $SkipBotEndpointUpdate) {
    Write-Host "==> Validating Azure Bot CLI command availability..."
    & az bot --help 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI 'bot' command is unavailable. Install or upgrade Azure CLI to include Bot commands, or rerun with -SkipBotEndpointUpdate:$true."
    }

    $resolvedBotName = Resolve-BotName -TargetResourceGroup $effectiveBotResourceGroup -ConfiguredBotName $BotName -TargetBotAppId $effectiveBotAppId

    if ([string]::IsNullOrWhiteSpace($resolvedBotName)) {
        if (-not [string]::IsNullOrWhiteSpace($effectiveBotAppId)) {
            throw "Unable to resolve Azure Bot in resource group '$effectiveBotResourceGroup' for msaAppId '$effectiveBotAppId'. Provide -BotName explicitly or disable with -SkipBotEndpointUpdate."
        }
        Write-Warning "Skipping bot endpoint update because BotName/BotAppId was not provided."
    } else {
        $targetEndpoint = "$baseUrl/messages"
        Write-Host "==> Updating Azure Bot endpoint to: $targetEndpoint"
        Invoke-Az -Args @(
            "bot", "update",
            "--resource-group", $effectiveBotResourceGroup,
            "--name", $resolvedBotName,
            "--endpoint", $targetEndpoint,
            "--only-show-errors"
        )

        $actualEndpoint = Invoke-Az -Capture -Args @(
            "bot", "show",
            "--resource-group", $effectiveBotResourceGroup,
            "--name", $resolvedBotName,
            "--query", "properties.endpoint",
            "-o", "tsv"
        )

        if ($actualEndpoint -ne $targetEndpoint) {
            throw "Bot endpoint verification failed. Expected '$targetEndpoint', got '$actualEndpoint'."
        }

        Write-Host "==> Azure Bot endpoint updated successfully."
    }
} else {
    Write-Host "==> Skipping Azure Bot endpoint update by request."
}

if (-not $SkipSmokeTest) {
    Write-Host "==> Running smoke tests against deployed app..."

    Invoke-WithRetry -Description "GET / health check" -Action {
        $response = Invoke-WebRequest -Uri "$baseUrl/" -Method Get -TimeoutSec 30 -ErrorAction Stop
        if ($response.StatusCode -lt 200 -or $response.StatusCode -gt 299) {
            throw "Unexpected health status code: $($response.StatusCode)"
        }
        $response
    } | Out-Null

    $headers = @{ Authorization = "Bearer smoke-test-token" }
    if (-not [string]::IsNullOrWhiteSpace($GitHubToken)) {
        $headers["x-github-token"] = $GitHubToken
    }

    $chatBody = '{"prompt":"Reply exactly: online-check-ok"}'
    Invoke-WithRetry -Description "POST /agent/chat smoke test" -Action {
        $chatResponse = Invoke-RestMethod `
            -Uri "$baseUrl/agent/chat" `
            -Method Post `
            -Headers $headers `
            -ContentType "application/json" `
            -Body $chatBody `
            -TimeoutSec 180 `
            -ErrorAction Stop

        if (-not $chatResponse) {
            throw "Empty response from /agent/chat"
        }

        $reply = [string]$chatResponse.response
        if ([string]::IsNullOrWhiteSpace($reply)) {
            throw "No 'response' field returned from /agent/chat"
        }

        $chatResponse
    } | Out-Null

    Write-Host "==> Smoke tests passed."
} else {
    Write-Host "==> Skipping smoke tests by request."
}

Write-Host ""
Write-Host "Deployment completed."
Write-Host "Function on ACA URL: $baseUrl"
Write-Host "Resource prefix used: $sanitizedPrefix"
Write-Host "Container app identity principalId: $principalId"
Write-Host ""
Write-Host "Tip: call /agent/chat with Authorization bearer token and x-github-token header."
