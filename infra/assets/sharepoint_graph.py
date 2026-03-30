import base64
import json
import logging
import os
from typing import Any, Optional

import aiohttp

_GRAPH_SCOPE_DEFAULT = "https://graph.microsoft.com/.default"
_GRAPH_API_ROOT = "https://graph.microsoft.com/v1.0"


def get_unverified_jwt_claims(token: str) -> dict[str, Any]:
    """Best-effort JWT payload parsing without signature verification."""
    if not token or token.count(".") < 2:
        return {}

    try:
        payload = token.split(".")[1]
        padding = "=" * ((4 - len(payload) % 4) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding)
        data = json.loads(decoded.decode("utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def looks_like_user_assertion(token: str) -> bool:
    """Heuristic check that a JWT resembles a user-delegated assertion."""
    claims = get_unverified_jwt_claims(token)
    if not claims:
        return False

    has_user_identity = any(
        claims.get(k)
        for k in ("oid", "preferred_username", "upn", "unique_name")
    )
    has_delegated_signal = bool(claims.get("scp") or claims.get("roles"))
    return bool(has_user_identity and has_delegated_signal)


def _build_share_id(share_url: str) -> str:
    # Graph shares API uses: u! + base64url(share_url) with no '=' padding.
    encoded = base64.urlsafe_b64encode(share_url.encode("utf-8")).decode("utf-8")
    encoded = encoded.rstrip("=")
    return f"u!{encoded}"


def _acquire_graph_token_via_obo(user_assertion: str, tenant_id: Optional[str]) -> Optional[str]:
    """Acquire Microsoft Graph token with OAuth2 OBO flow via MSAL."""
    client_id = (
        os.environ.get("GRAPH_OBO_CLIENT_ID")
        or os.environ.get("BOT_APP_ID")
        or os.environ.get("AZURE_CLIENT_ID")
        or os.environ.get("AZURE_MANAGED_IDENTITY_CLIENT_ID")
        or ""
    ).strip()
    client_secret = (os.environ.get("GRAPH_OBO_CLIENT_SECRET") or "").strip()
    configured_tenant = (os.environ.get("GRAPH_OBO_TENANT_ID") or "").strip()
    authority_host = (os.environ.get("GRAPH_OBO_AUTHORITY_HOST") or "https://login.microsoftonline.com").strip().rstrip("/")
    effective_tenant = configured_tenant or (tenant_id or "").strip()
    scopes_env = (os.environ.get("GRAPH_OBO_SCOPES") or "").strip()

    if not client_id or not client_secret:
        logging.info(
            "[Graph OBO] Missing GRAPH_OBO_CLIENT_ID or GRAPH_OBO_CLIENT_SECRET; skipping OBO fetch"
        )
        return None
    if not effective_tenant:
        logging.info("[Graph OBO] Missing tenant id for OBO flow; skipping OBO fetch")
        return None

    scopes = [s.strip() for s in scopes_env.split(",") if s.strip()] or [_GRAPH_SCOPE_DEFAULT]
    authority = f"{authority_host}/{effective_tenant}"

    try:
        import msal
    except Exception:
        logging.warning("[Graph OBO] msal not installed; cannot perform OBO flow")
        return None

    app = msal.ConfidentialClientApplication(
        client_id=client_id,
        authority=authority,
        client_credential=client_secret,
    )

    result = app.acquire_token_on_behalf_of(user_assertion=user_assertion, scopes=scopes)
    access_token = (result or {}).get("access_token")
    if access_token:
        return str(access_token)

    logging.warning(
        "[Graph OBO] acquire_token_on_behalf_of failed: %s",
        (result or {}).get("error_description") or (result or {}).get("error") or "unknown",
    )
    return None


async def download_sharepoint_link_via_obo(
    share_url: str,
    *,
    user_assertion: str,
    tenant_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Download a SharePoint/OneDrive shared link via Graph using OBO token."""
    graph_token = _acquire_graph_token_via_obo(user_assertion, tenant_id)
    if not graph_token:
        return None

    share_id = _build_share_id(share_url)
    meta_url = f"{_GRAPH_API_ROOT}/shares/{share_id}/driveItem"
    content_url = f"{_GRAPH_API_ROOT}/shares/{share_id}/driveItem/content"

    headers = {"Authorization": f"Bearer {graph_token}"}
    timeout = aiohttp.ClientTimeout(total=45)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(meta_url, headers=headers) as meta_resp:
                if meta_resp.status >= 400:
                    detail = await meta_resp.text()
                    logging.warning(
                        "[Graph OBO] driveItem metadata request failed (%s): %s",
                        meta_resp.status,
                        detail[:300],
                    )
                    return None
                metadata = await meta_resp.json()

            file_name = (metadata or {}).get("name") or "shared-document"
            mime_type = (
                ((metadata or {}).get("file") or {}).get("mimeType")
                or "application/octet-stream"
            )
            download_url = (metadata or {}).get("@microsoft.graph.downloadUrl")

            if download_url:
                async with session.get(download_url) as file_resp:
                    if file_resp.status == 200:
                        data = await file_resp.read()
                        return {
                            "data": data,
                            "content_type": mime_type,
                            "filename": file_name,
                        }
                    logging.warning(
                        "[Graph OBO] pre-auth downloadUrl failed (%s) for %s",
                        file_resp.status,
                        file_name,
                    )

            async with session.get(content_url, headers=headers) as content_resp:
                if content_resp.status == 200:
                    data = await content_resp.read()
                    return {
                        "data": data,
                        "content_type": mime_type,
                        "filename": file_name,
                    }
                detail = await content_resp.text()
                logging.warning(
                    "[Graph OBO] driveItem/content request failed (%s): %s",
                    content_resp.status,
                    detail[:300],
                )
    except Exception as exc:
        logging.warning("[Graph OBO] Share link download failed: %s", exc)

    return None
