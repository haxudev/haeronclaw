"""
Upload agent-generated files to Azure Blob Storage and return download URLs.

When the Copilot agent's skills generate files (PDF, PPTX, DOCX, etc.),
they are written to /tmp on Azure Functions (since wwwroot is read-only
in the hosted runtime).  This module:

  1. Takes snapshots of /tmp before and after agent execution to detect
     newly created files (reliable even if the agent response text does
     not include the file path).
  2. Also scans the agent's response text for explicit file path references
     as a secondary detection mechanism.
  3. Uploads matching files to an 'agent-files' container in the Function
     App's storage account using Managed Identity.
  4. Returns proxy download URLs (``/files/{blob_name}``) that route through
     the Function App — no SAS tokens or extra RBAC roles needed.
  5. Replaces local file paths in the response text with markdown download
     links and appends a download section for files not mentioned in the text.

Dependencies (already in extra-requirements.txt):
  - azure-storage-blob >= 12.19.0
  - azure-identity >= 1.17.0
"""

import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CONTAINER_NAME = "agent-files"

# File extensions that agent skills can produce as user-facing deliverables.
# NOTE: .json is intentionally excluded — the Copilot SDK writes UUID-named
# session state .json files to /tmp that are NOT deliverables.
_DELIVERABLE_EXTENSIONS = frozenset({
    ".pdf", ".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".tsv",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".tiff", ".tif",
    ".msg", ".eml",
    ".txt", ".md", ".html", ".htm",
    ".zip", ".tar", ".gz", ".7z", ".rar",
    ".mp4", ".mov", ".webm", ".m4v", ".mp3", ".wav",
})

_FILE_EXTENSIONS = tuple(sorted(ext.lstrip(".") for ext in _DELIVERABLE_EXTENSIONS))

# Regex: internal artifacts written by Copilot tooling (NOT deliverables)
_INTERNAL_ARTIFACT_RE = re.compile(
    r'^(?:copilot-tool-output|copilot-tool-error|copilot-tool-debug)-',
    re.IGNORECASE,
)

# Regex to detect UUID-style filenames (with or without hyphens).
# These are internal session/state artifacts, NOT deliverables.
_UUID_FILENAME_RE = re.compile(
    r'^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}\.',
    re.IGNORECASE,
)

# Text-file suffixes that need content-based heuristic filtering.
# MCP tools and the Copilot SDK write search results, API responses,
# and paste buffers as .txt/.md files to /tmp — these are NOT deliverables.
_TEXT_SUFFIXES_NEEDING_CONTENT_CHECK = {".txt", ".md"}

# Regex: absolute paths under /tmp (or /var/tmp) ending with a known extension
_FILE_PATH_PATTERN = re.compile(
    r'(/(?:tmp|var/tmp)(?:/[^\s\'"<>|()]+)?\.(?:' + "|".join(_FILE_EXTENSIONS) + r'))',
    re.IGNORECASE,
)


class UploadedFile(NamedTuple):
    """Metadata for a file that was uploaded to blob storage."""
    original_path: str
    download_url: str
    filename: str


# ---------------------------------------------------------------------------
# Lazy-initialized blob client
# ---------------------------------------------------------------------------
_blob_service_client = None


def _get_blob_service_client():
    """Get or create a BlobServiceClient using Managed Identity."""
    global _blob_service_client
    if _blob_service_client is not None:
        return _blob_service_client

    blob_uri = os.environ.get("AzureWebJobsStorage__blobServiceUri")
    if not blob_uri:
        logging.warning("[FileUpload] AzureWebJobsStorage__blobServiceUri not configured")
        return None

    client_id = os.environ.get("AZURE_MANAGED_IDENTITY_CLIENT_ID")
    try:
        from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
        from azure.storage.blob import BlobServiceClient

        credential = (
            ManagedIdentityCredential(client_id=client_id)
            if client_id
            else DefaultAzureCredential()
        )
        _blob_service_client = BlobServiceClient(account_url=blob_uri, credential=credential)
        return _blob_service_client
    except Exception as exc:
        logging.error(f"[FileUpload] Failed to create BlobServiceClient: {exc}")
        return None


def _ensure_container(client) -> bool:
    """Create the agent-files container if it doesn't already exist."""
    try:
        container = client.get_container_client(_CONTAINER_NAME)
        if not container.exists():
            container.create_container()
            logging.info(f"[FileUpload] Created container '{_CONTAINER_NAME}'")
        return True
    except Exception as exc:
        logging.error(f"[FileUpload] Failed to ensure container '{_CONTAINER_NAME}': {exc}")
        return False


def _generate_download_url(blob_name: str) -> Optional[str]:
    """Generate a proxy download URL through the Function App.

    Instead of User Delegation SAS (which requires Storage Blob Delegator role),
    we route downloads through a ``/files/`` endpoint on the Function App itself.
    The endpoint uses the Managed Identity to read blobs — only ``Storage Blob
    Data Contributor`` is needed, which the MI already has.
    """
    hostname = os.environ.get("WEBSITE_HOSTNAME", "")
    if not hostname:
        logging.error("[FileUpload] WEBSITE_HOSTNAME env var not set; cannot build download URL")
        return None
    scheme = "https" if "localhost" not in hostname else "http"
    # URL-encode the blob name in case it has special chars
    from urllib.parse import quote
    return f"{scheme}://{hostname}/files/{quote(blob_name, safe='/')}"


def _generate_download_url_with_filename(blob_name: str, filename: str) -> Optional[str]:
    """Generate proxy URL with a suggested download filename.

    Passing the filename via query parameter avoids needing non-ASCII values
    in HTTP headers at upload time.
    """
    base = _generate_download_url(blob_name)
    if not base:
        return None
    from urllib.parse import quote
    return f"{base}?filename={quote(filename, safe='')}"


def _slugify_ascii(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    raw = raw.encode("ascii", errors="ignore").decode("ascii")
    raw = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._-")
    return raw


def upload_single_file(filepath: str, blob_prefix: str = None) -> Optional[str]:
    """Upload a single file to blob storage and return its download URL.

    Used by tools (e.g. m365_cli) to upload files that need to be linked
    rather than attached directly (e.g. restricted attachment types in emails).

    Returns the download URL, or None if upload fails.
    """
    path = Path(filepath)
    if not path.is_file():
        logging.warning("[FileUpload] upload_single_file: file not found: %s", filepath)
        return None

    client = _get_blob_service_client()
    if not client:
        return None
    if not _ensure_container(client):
        return None

    from azure.storage.blob import ContentSettings
    import uuid

    prefix = blob_prefix or uuid.uuid4().hex[:12]
    ext = path.suffix or ""
    stem = path.stem or "file"
    safe_stem = _slugify_ascii(stem) or "file"
    safe_blob_filename = f"{safe_stem}_{uuid.uuid4().hex[:8]}{ext}"
    blob_name = f"{prefix}/{safe_blob_filename}"

    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    try:
        blob_client = client.get_blob_client(_CONTAINER_NAME, blob_name)
        with open(filepath, "rb") as f:
            blob_client.upload_blob(
                f,
                overwrite=True,
                content_settings=ContentSettings(content_type=content_type),
            )
        url = _generate_download_url_with_filename(blob_name, path.name)
        logging.info("[FileUpload] upload_single_file: %s → %s", filepath, url)
        return url
    except Exception as exc:
        logging.error("[FileUpload] upload_single_file failed for %s: %s", filepath, exc)
        return None


# ---------------------------------------------------------------------------
# File detection helpers
# ---------------------------------------------------------------------------

def _is_deliverable_file(path: Path) -> bool:
    """Return True if a file looks like a genuine user-facing deliverable.

    Filters out:
      - UUID-named files (Copilot SDK session state, temp artifacts)
      - Copilot tool output artifacts (copilot-tool-output-*.txt, etc.)
      - Hidden files / dotfiles
      - Text files (.txt, .md) whose content looks like tool output
        (JSON search results, API responses, paste buffers)
    """
    filename = path.name
    if path.suffix.lower() not in _DELIVERABLE_EXTENSIONS:
        return False
    # Reject source / build scripts — not user-facing deliverables
    if path.suffix.lower() in {".py", ".js", ".cjs", ".mjs", ".ts", ".sh", ".bat", ".ps1"}:
        return False
    # Reject UUID-named files (e.g. "10953933-cc5a-468a-a0f6-aff046388d83.json")
    if _UUID_FILENAME_RE.match(filename):
        return False
    # Reject internal Copilot tool artifacts (e.g. "copilot-tool-output-...txt")
    if _INTERNAL_ARTIFACT_RE.match(filename):
        return False
    # Reject hidden files / dotfiles
    if filename.startswith("."):
        return False
    # For plain-text files, apply content-based heuristic to reject tool output
    if path.suffix.lower() in _TEXT_SUFFIXES_NEEDING_CONTENT_CHECK:
        if _looks_like_tool_output(path):
            logging.debug(
                "[FileUpload] Skipping text file (tool output heuristic): %s", path
            )
            return False
    return True


def _looks_like_tool_output(path: Path) -> bool:
    """Heuristic: does a text file look like MCP/SDK tool output?

    Checks the first 512 bytes of the file for patterns that indicate
    it is a search result, API response, or Copilot SDK paste buffer
    rather than a user-requested deliverable.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(512)
    except Exception:
        return False

    stripped = head.lstrip()

    # JSON object or array — API responses, search results, tool output
    if stripped[:1] in ("{", "["):
        return True

    # Copilot SDK paste-buffer marker: "[Pasted ~N lines] ..."
    if stripped.startswith("[Pasted"):
        return True

    # MCP search-result structure (e.g. microsoft_docs_search output)
    if '"results"' in head and '"title"' in head:
        return True

    return False


def snapshot_tmp_files(scope_dir: Optional[str] = None) -> Dict[str, float]:
    """Take a snapshot of candidate deliverable files in /tmp (or a subdirectory).

    Args:
        scope_dir: If provided, only scan this specific directory (for request
                   isolation in multi-user scenarios).  Falls back to scanning
                   all of /tmp if not provided.

    Returns a dict of {absolute_path: mtime} for concurrency-safe diffing.
    Only includes files that pass the deliverable-file filter.

    Notes:
      - When scanning the full /tmp (scope_dir is None), we only consider files
        with known deliverable extensions to avoid picking up runtime artifacts.
      - When scanning a per-request output directory (scope_dir is provided), we
                still scan the directory broadly, but only keep files that match the
                deliverable allowlist plus heuristic filters.
    """
    result: Dict[str, float] = {}
    tmp_dir = Path(scope_dir) if scope_dir else Path("/tmp")
    if not tmp_dir.exists():
        return result

    if scope_dir:
        # Per-request output dir: scan all files (direct children + one level deep)
        # and rely on the deliverable allowlist + heuristic filters.
        for f in list(tmp_dir.glob("*")) + list(tmp_dir.glob("*/*")):
            if not f.is_file():
                continue
            if not _is_deliverable_file(f):
                continue
            try:
                result[str(f)] = f.stat().st_mtime
            except OSError:
                pass
        return result

    # Full /tmp scan: extensions allowlist only
    for ext in _FILE_EXTENSIONS:
        # Direct children
        for f in tmp_dir.glob(f"*.{ext}"):
            if f.is_file() and _is_deliverable_file(f):
                try:
                    result[str(f)] = f.stat().st_mtime
                except OSError:
                    pass
        # One level deep
        for f in tmp_dir.glob(f"*/*.{ext}"):
            if f.is_file() and _is_deliverable_file(f):
                try:
                    result[str(f)] = f.stat().st_mtime
                except OSError:
                    pass
    return result


def create_request_output_dir() -> str:
    """Create a unique per-request output directory under /tmp.

    Returns the absolute path (e.g. ``/tmp/req_a1b2c3d4e5f6``).
    Using per-request dirs prevents file collisions and cross-talk
    between concurrent requests on the same Function App instance.
    """
    import uuid
    request_id = uuid.uuid4().hex[:12]
    dir_path = f"/tmp/req_{request_id}"
    os.makedirs(dir_path, exist_ok=True)
    return dir_path


def find_new_files(before: Dict[str, float], after: Dict[str, float]) -> List[str]:
    """Identify files that appeared or were modified between two snapshots."""
    new_files = []
    for path, mtime in after.items():
        if path not in before or mtime > before[path]:
            new_files.append(path)
    return sorted(new_files)


def find_file_paths_in_text(text: str) -> List[str]:
    """Extract file paths from text that match known extensions and exist on disk."""
    matches = _FILE_PATH_PATTERN.findall(text)
    seen = set()
    result = []
    for path_str in matches:
        if path_str not in seen and os.path.isfile(path_str):
            seen.add(path_str)
            result.append(path_str)
    return result


# ---------------------------------------------------------------------------
# Main upload & replace pipeline
# ---------------------------------------------------------------------------

def upload_and_replace(
    response_text: str,
    new_file_paths: Optional[List[str]] = None,
    append_download_section: bool = True,
) -> Tuple[str, List[UploadedFile]]:
    """Upload generated files to blob storage and replace paths with download links.

    Args:
        response_text: The agent's response text (may contain file path references).
        new_file_paths: Optional list of file paths detected via /tmp snapshot diffing.
                        If None, only text-based detection is used.

    Returns:
        (updated_text, list_of_uploaded_files)
        If no files are found or upload fails, the original text is returned.
    """
    # Combine file paths from text scanning and snapshot diffing
    text_paths = find_file_paths_in_text(response_text)
    snapshot_paths = new_file_paths or []

    # Merge and deduplicate, preserving order (text paths first)
    seen = set()
    all_paths: List[str] = []
    for p in text_paths + snapshot_paths:
        if p in seen or not os.path.isfile(p):
            continue
        # Apply deliverable filter to BOTH text-discovered and snapshot-discovered files
        # so internal artifacts (e.g. copilot-tool-output-*.txt) never get uploaded.
        try:
            if not _is_deliverable_file(Path(p)):
                continue
        except Exception:
            continue
        seen.add(p)
        all_paths.append(p)

    if not all_paths:
        return response_text, []

    client = _get_blob_service_client()
    if not client:
        logging.warning("[FileUpload] No blob client available; skipping file upload")
        return response_text, []

    if not _ensure_container(client):
        return response_text, []

    from azure.storage.blob import ContentSettings

    uploaded: List[UploadedFile] = []
    # Use UUID prefix for blob path isolation (prevents collisions in concurrent requests)
    import uuid
    request_prefix = uuid.uuid4().hex[:12]

    for file_path in all_paths:
        path = Path(file_path)
        original_filename = path.name

        # Ensure blob names are ASCII-safe; keep original filename for display.
        ext = path.suffix or ""
        stem = path.stem or "file"
        safe_stem = _slugify_ascii(stem) or "file"
        import uuid
        safe_blob_filename = f"{safe_stem}_{uuid.uuid4().hex[:8]}{ext}"
        blob_name = f"{request_prefix}/{safe_blob_filename}"

        content_type = mimetypes.guess_type(original_filename)[0] or "application/octet-stream"

        try:
            blob_client = client.get_blob_client(_CONTAINER_NAME, blob_name)
            with open(file_path, "rb") as f:
                blob_client.upload_blob(
                    f,
                    overwrite=True,
                    content_settings=ContentSettings(
                        content_type=content_type,
                    ),
                )
            logging.info(f"[FileUpload] Blob uploaded: {file_path} -> {blob_name}")

            download_url = _generate_download_url_with_filename(blob_name, original_filename)
            if download_url:
                # Replace the local path in the text with a markdown link
                if file_path in response_text:
                    markdown_link = f"[{original_filename}]({download_url})"
                    response_text = response_text.replace(file_path, markdown_link)

                uploaded.append(UploadedFile(file_path, download_url, original_filename))
                logging.info(f"[FileUpload] Download URL: {download_url}")
            else:
                logging.warning(f"[FileUpload] Uploaded {blob_name} but URL generation failed")
        except Exception as exc:
            logging.error(f"[FileUpload] Failed to upload {file_path}: {exc}", exc_info=True)

    # Append download links for files that were NOT referenced in the original text
    # (detected only via /tmp snapshot diffing)
    if append_download_section:
        unreferenced = [uf for uf in uploaded if uf.original_path not in text_paths]
        if unreferenced:
            links = "\n".join(f"- [{uf.filename}]({uf.download_url})" for uf in unreferenced)
            response_text += f"\n\n**Downloads:**\n{links}"

    return response_text, uploaded
