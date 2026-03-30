# Deployment on ACA

This repository now supports a single Azure deployment path: Azure Functions on Azure Container Apps.

## Model configuration contract

Use `COPILOT_MODEL` as the only runtime model environment variable.

- Always pass a full model id such as `github:gpt-5.4` or `foundry:gpt-5.2-codex`.
- Do not add alias variables such as `GHCP_MODEL_NAME`.
- Treat deployment parameters as input only; the deployed app should read `COPILOT_MODEL` at runtime.

Deployment injects the runtime value by passing `-Model` to the ACA deployment scripts, which then set `COPILOT_MODEL` on the Container App.

## Deployment at a glance

| Runtime | Primary command | Best for |
| --- | --- | --- |
| Azure Functions on Azure Container Apps | `./scripts/deploy-aca-functions.ps1` | Heavy dependencies, custom container control, and ACA-native operations |

## Unified entry point

Use the repository entry point:

```powershell
./scripts/deploy.ps1 -Mode aca -ResourceGroup rg-example -Location eastus2 -Prefix fmaaca -Model github:gpt-5.4 -ImageTag v3 -GitHubToken <gho_or_ghu_token> -BotAppId <bot-app-id> -BotName <bot-resource-name> -InPlaceUpdate:$true
```

## ACA mode

ACA mode builds and pushes a container image, then deploys Functions on ACA.

```powershell
./scripts/deploy-aca-functions.ps1 `
  -ResourceGroup rg-example `
  -Location eastus2 `
  -Prefix fmaaca `
  -Model github:gpt-5.4 `
  -ImageTag v3 `
  -GitHubToken <gho_or_ghu_token> `
  -BotAppId <bot-app-id> `
  -BotName <bot-resource-name> `
  -InPlaceUpdate:$true
```

Pin to a specific existing app/ACR for deterministic in-place updates:

```powershell
./scripts/deploy-aca-functions.ps1 `
  -ResourceGroup rg-example `
  -Location eastus2 `
  -Prefix fmaaca `
  -Model github:gpt-5.4 `
  -ImageTag v3 `
  -GitHubToken <gho_or_ghu_token> `
  -BotAppId <bot-app-id> `
  -BotName <bot-resource-name> `
  -InPlaceUpdate:$true `
  -TargetContainerAppName <existing-container-app-name> `
  -TargetAcrName <existing-acr-name>
```

Reuse mode (existing storage + UAMI):

```powershell
./scripts/deploy-aca-functions.ps1 `
  -ResourceGroup rg-example `
  -Location eastus2 `
  -Prefix fmaaca `
  -Model github:gpt-5.4 `
  -ImageTag v3 `
  -GitHubToken <gho_or_ghu_token> `
  -ExistingStorageAccountName <storage-account-name> `
  -ExistingIdentityId <uami-resource-id> `
  -ExistingIdentityClientId <uami-client-id> `
  -BotAppId <bot-app-id> `
  -BotName <bot-resource-name>
```

Notes for Teams bots:

- `-InPlaceUpdate` defaults to `true`. The script updates image and runtime settings on an existing app instead of creating a new ACA app each run.
- If `-TargetContainerAppName` is omitted in in-place mode, the script first tries to resolve it from the current bot endpoint, then falls back to latest app by prefix.
- For `github:*` models, pass `-GitHubToken` so the runtime stores an app-level token (`GITHUB_TOKEN`) and can reply to Teams messages even when no per-user token header is present.
- In UAMI reuse mode, if `-BotAppId` is omitted, the script falls back to `-ExistingIdentityClientId`.
- The ACA script now auto-updates Azure Bot endpoint to `https://<fqdn>/messages` and runs smoke tests (`/` and `/agent/chat`) by default.
- Use `-SkipBotEndpointUpdate:$true` or `-SkipSmokeTest:$true` only for emergency/manual scenarios.

## Parameter templates

Use these sample files as deployment runbook templates:

- `scripts/deploy.aca.parameters.sample.json`
- `scripts/deploy.dev.parameters.sample.json`

Copy them to local, non-committed files and fill in values for each environment.

- `scripts/deploy.aca.parameters.sample.json` -> `scripts/deploy.aca.parameters.local.json`
- `scripts/deploy.dev.parameters.sample.json` -> `scripts/deploy.dev.parameters.local.json`

## CI/CD recommendation

Use the shared test/build steps before an ACA deployment job.
