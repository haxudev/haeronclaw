param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("aca")]
    [string]$Mode,

    [Parameter(Mandatory = $false)]
    [string]$ResourceGroup = "",

    [Parameter(Mandatory = $false)]
    [string]$Location = "eastus2",

    [Parameter(Mandatory = $false)]
    [string]$Prefix = "fma",

    [Parameter(Mandatory = $false)]
    [string]$Model = "github:gpt-5.4",

    [Parameter(Mandatory = $false)]
    [string]$GitHubToken = "",

    [Parameter(Mandatory = $false)]
    [bool]$VnetEnabled = $false,

    [Parameter(Mandatory = $false)]
    [bool]$SkipRbac = $false,

    [Parameter(Mandatory = $false)]
    [string]$ImageTag = "v1",

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

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

switch ($Mode) {
    "aca" {
        if ([string]::IsNullOrWhiteSpace($ResourceGroup)) {
            throw "-ResourceGroup is required in aca mode."
        }

        & "$PSScriptRoot/deploy-aca-functions.ps1" `
            -ResourceGroup $ResourceGroup `
            -Location $Location `
            -Prefix $Prefix `
            -Model $Model `
            -ImageTag $ImageTag `
            -GitHubToken $GitHubToken `
            -ExistingStorageAccountName $ExistingStorageAccountName `
            -ExistingIdentityId $ExistingIdentityId `
            -ExistingIdentityClientId $ExistingIdentityClientId `
            -BotAppId $BotAppId `
            -BotName $BotName `
            -BotResourceGroup $BotResourceGroup `
            -SkipBotEndpointUpdate:$SkipBotEndpointUpdate `
            -SkipSmokeTest:$SkipSmokeTest `
            -InPlaceUpdate:$InPlaceUpdate `
            -TargetContainerAppName $TargetContainerAppName `
            -TargetAcrName $TargetAcrName `
            -SkipM365Credentials:$SkipM365Credentials

        if ($LASTEXITCODE -ne 0) {
            throw "ACA deployment failed."
        }
    }
}
