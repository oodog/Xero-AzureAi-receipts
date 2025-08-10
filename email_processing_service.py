import io
import os
import time
import base64
import logging
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import quote

import requests
from azure.storage.blob import BlobServiceClient, ContentSettings

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ALLOWED = set([x.strip().lower() for x in os.getenv("EMAIL_ALLOWED_EXTENSIONS", "pdf,jpg,jpeg,png,heic").split(",")])
MAX_MB = int(os.getenv("EMAIL_MAX_MB", "15"))

BLOB_ENDPOINT = os.getenv("AZURE_STORAGE_BLOB_ENDPOINT") or f"https://{os.getenv('AZURE_STORAGE_ACCOUNT_NAME')}.blob.core.windows.net"
ACCOUNT_NAME = os.getenv("AZURE_STORAGE_ACCOUNT_NAME")
ACCOUNT_KEY = os.getenv("AZURE_STORAGE_ACCOUNT_KEY")

@dataclass
class EmailAttachment:
    filename: str
    content_type: str
    content_bytes: bytes

@dataclass
class EmailMessageMeta:
    tenant_id: str
    mailbox: str
    message_id: str
    from_address: Optional[str] = None
    subject: Optional[str] = None
    received_utc: Optional[str] = None

class EmailProcessingService:
    """
    Normalizes email attachments into the tenant's 'processing' container.
    """

    def __init__(self):
        if not BLOB_ENDPOINT or not ACCOUNT_NAME:
            raise RuntimeError("Missing storage configuration (AZURE_STORAGE_ACCOUNT_NAME / BLOB endpoint).")
        self._blob = self._build_blob_client()

    def _build_blob_client(self) -> BlobServiceClient:
        if ACCOUNT_KEY:
            return BlobServiceClient(account_url=BLOB_ENDPOINT, credential=ACCOUNT_KEY)
        return BlobServiceClient(account_url=BLOB_ENDPOINT)

    def _validate_attachment(self, att: EmailAttachment) -> bool:
        if not att.filename:
            return False
        ext = att.filename.rsplit(".", 1)[-1].lower() if "." in att.filename else ""
        if ext not in ALLOWED:
            logger.info("Skipping attachment %s due to disallowed extension %s", att.filename, ext)
            return False
        size_mb = len(att.content_bytes) / (1024 * 1024)
        if size_mb > MAX_MB:
            logger.info("Skipping attachment %s: %.2fMB exceeds limit %dMB", att.filename, size_mb, MAX_MB)
            return False
        return True

    def _target_container(self, tenant_id: str) -> str:
        return f"tenant-{tenant_id}-processing"

    def _blob_name(self, meta: EmailMessageMeta, filename: str) -> str:
        ts = int(time.time())
        safe_name = quote(filename, safe="")
        return f"email/{meta.message_id}/{ts}_{safe_name}"

    def save_attachments(self, meta: EmailMessageMeta, attachments: List[EmailAttachment]) -> List[str]:
        container = self._target_container(meta.tenant_id)
        container_client = self._blob.get_container_client(container)
        try:
            container_client.get_container_properties()
        except Exception:
            container_client.create_container(public_access=None)

        saved_urls = []
        for att in attachments:
            if not self._validate_attachment(att):
                continue
            blob_name = self._blob_name(meta, att.filename)
            ct = att.content_type or "application/octet-stream"
            content_settings = ContentSettings(content_type=ct)
            blob_client = container_client.get_blob_client(blob_name)
            blob_client.upload_blob(
                io.BytesIO(att.content_bytes),
                overwrite=True,
                content_settings=content_settings
            )
            saved_urls.append(f"{BLOB_ENDPOINT}/{container}/{blob_name}")
            logger.info("Saved attachment to %s", saved_urls[-1])
        return saved_urls

    # ---------- Microsoft Graph helpers ----------

    @staticmethod
    def graph_get_access_token(tenant_id: str, client_id: str, client_secret: str, scope: str = "https://graph.microsoft.com/.default") -> str:
        token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": scope,
            "grant_type": "client_credentials",
        }
        resp = requests.post(token_url, data=data, timeout=30)
        resp.raise_for_status()
        return resp.json()["access_token"]

    @staticmethod
    def graph_list_message_attachments(access_token: str, mailbox: str, message_id: str) -> List[EmailAttachment]:
        url = f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{message_id}/attachments?$expand=content"
        headers = {"Authorization": f"Bearer {access_token}"}
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        out: List[EmailAttachment] = []
        for a in data.get("value", []):
            if a.get("@odata.type") == "#microsoft.graph.fileAttachment":
                filename = a.get("name") or "attachment"
                content_type = a.get("contentType") or "application/octet-stream"
                b64 = a.get("contentBytes")
                if not b64:
                    continue
                content_bytes = base64.b64decode(b64)
                out.append(EmailAttachment(filename=filename, content_type=content_type, content_bytes=content_bytes))
        return out
