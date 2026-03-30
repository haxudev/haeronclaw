# Functions on Container Apps Path

This repository deploys to **Azure Functions on Azure Container Apps**.

## What is added

- `infra/assets/Dockerfile`: container image build for Azure Functions Python runtime.
- `scripts/deploy-aca-functions.ps1`: one-command deployment for a **new** ACA-based service stack.

## Naming convention

All new resources share one prefix (`-Prefix`) plus a timestamp suffix.

Example with `-Prefix fmaaca`:

- `fmaaca-02282030-func` (Function app on ACA)
- `fmaaca-02282030-env` (Container Apps environment)
- `fmaacaacr02282030` (ACR)
- `fmaacast02282030` (Storage)

## Deploy

Run from repo root:

```powershell
./scripts/deploy-aca-functions.ps1 `
  -ResourceGroup "<your-resource-group>" `
  -Location "eastus2" `
  -Prefix "fmaaca" `
  -Model "github:gpt-5.4" `
  -ImageTag "v1"
```

If your RBAC/policy blocks Storage creation or role assignment, deploy in reuse mode:

```powershell
./scripts/deploy-aca-functions.ps1 `
  -ResourceGroup "<your-resource-group>" `
  -Location "eastus2" `
  -Prefix "fmaaca" `
  -Model "github:gpt-5.4" `
  -ImageTag "v1" `
  -ExistingStorageAccountName "<existing-storage-account>" `
  -ExistingIdentityId "<uami-resource-id>" `
  -ExistingIdentityClientId "<uami-client-id>"
```

## Runtime notes

- `REQUIRE_GITHUB_USER_TOKEN=true` is enabled by default in the ACA deployment script.
- Use per-user GitHub OAuth token in request headers (`x-github-token` or Bearer token).
