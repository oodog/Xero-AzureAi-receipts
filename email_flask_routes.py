import os
import json
import uuid
import logging
from typing import Dict, Any, Optional

from flask import Blueprint, request, render_template_string, jsonify, abort
import requests

from email_processing_service import (
    EmailProcessingService,
    EmailAttachment,
    EmailMessageMeta,
)

bp_email = Blueprint("email", __name__)
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

TENANT_ENV_DEFAULT = os.getenv("EMAIL_DEFAULT_TENANT_ID")  # optional global default
AAD_TENANT = os.getenv("EMAIL_AAD_TENANT_ID", "")
AAD_CLIENT_ID = os.getenv("EMAIL_AAD_CLIENT_ID", "")
AAD_CLIENT_SECRET = os.getenv("EMAIL_AAD_CLIENT_SECRET", "")
MAILBOX = os.getenv("EMAIL_MAILBOX", "")
FOLDER = os.getenv("EMAIL_FOLDER", "Receipts")
BASE_URL = os.getenv("EMAIL_WEBHOOK_BASEURL", "")

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# In-memory map of subscriptionId -> tenantId (persist to Cosmos in prod)
_subscription_tenant_map: Dict[str, str] = {}

_HTML_SETTINGS = """
<!doctype html>
<html>
<head>
  <title>Email Settings</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
</head>
<body class="p-6">
  <div class="max-w-3xl mx-auto space-y-4">
    <h1 class="text-2xl font-semibold">Email Settings</h1>
    <form method="post" action="/email/settings" class="space-y-3">
      <label class="block">
        <span class="text-sm">Mailbox (user or shared):</span>
        <input name="mailbox" value="{{ mailbox }}" class="border p-2 w-full" />
      </label>
      <label class="block">
        <span class="text-sm">Folder to watch:</span>
        <input name="folder" value="{{ folder }}" class="border p-2 w-full" />
      </label>
      <label class="block">
        <span class="text-sm">Tenant Id to tag uploads with:</span>
        <input name="tenantId" value="{{ tenantId or '' }}" class="border p-2 w-full" />
      </label>
      <div class="flex gap-2">
        <button class="px-4 py-2 rounded bg-blue-600 text-white" formaction="/email/connect">Create/Refresh Webhook</button>
        <a href="/email/test" class="px-4 py-2 rounded bg-gray-200">Run Test</a>
      </div>
    </form>
    <p class="text-xs text-gray-500">Settings are in-memory for dev. Persist to Cosmos in production.</p>
  </div>
</body>
</html>
"""

def _get_access_token() -> str:
    return EmailProcessingService.graph_get_access_token(
        AAD_TENANT, AAD_CLIENT_ID, AAD_CLIENT_SECRET, GRAPH_SCOPE
    )

def _resolve_tenant_for_mailbox(mailbox: str) -> Optional[str]:
    # Replace with a Cosmos lookup if you want per-mailbox tenant mapping.
    return TENANT_ENV_DEFAULT

@bp_email.route("/email/settings", methods=["GET", "POST"])
def email_settings():
    global MAILBOX, FOLDER
    if request.method == "POST":
        MAILBOX = request.form.get("mailbox", MAILBOX).strip()
        FOLDER = request.form.get("folder", FOLDER).strip()
        tenant_id = request.form.get("tenantId", "").strip()
        if tenant_id:
            _subscription_tenant_map["__default__"] = tenant_id
    html = render_template_string(
        _HTML_SETTINGS, mailbox=MAILBOX, folder=FOLDER, tenantId=_subscription_tenant_map.get("__default__")
    )
    return html

@bp_email.route("/email/connect", methods=["POST"])
def email_connect():
    if not BASE_URL:
        abort(400, "EMAIL_WEBHOOK_BASEURL is not configured.")
    token = _get_access_token()

    # Find folder ID
    url_folder = f"{GRAPH_BASE}/users/{MAILBOX}/mailFolders?$filter=displayName eq '{FOLDER}'"
    resp = requests.get(url_folder, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    resp.raise_for_status()
    values = resp.json().get("value", [])
    if not values:
        abort(400, f"Folder '{FOLDER}' not found.")
    folder_id = values[0]["id"]

    # Create subscription for new messages in folder
    sub_url = f"{GRAPH_BASE}/subscriptions"
    import datetime
    body = {
        "changeType": "created",
        "notificationUrl": f"{BASE_URL}/email/webhook",
        "resource": f"/users/{MAILBOX}/mailFolders/{folder_id}/messages",
        "clientState": uuid.uuid4().hex,
        "expirationDateTime": (datetime.datetime.utcnow() + datetime.timedelta(days=1)).replace(microsecond=0).isoformat() + "Z",
    }

    r = requests.post(sub_url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, data=json.dumps(body), timeout=30)
    r.raise_for_status()
    sub = r.json()

    tenant_id = _subscription_tenant_map.get("__default__") or _resolve_tenant_for_mailbox(MAILBOX)
    if tenant_id:
        _subscription_tenant_map[sub["id"]] = tenant_id

    return jsonify({"ok": True, "subscription": sub, "tenantId": tenant_id}), 201

@bp_email.route("/email/webhook", methods=["POST", "GET"])
def email_webhook():
    # Validation handshake (Graph passes ?validationToken=)
    validation = request.args.get("validationToken")
    if validation:
        return validation, 200, {"Content-Type": "text/plain"}

    data = request.get_json(force=True, silent=True) or {}
    value = data.get("value", [])
    if not value:
        return jsonify({"ok": True})

    token = _get_access_token()
    svc = EmailProcessingService()

    saved = []
    for n in value:
        sub_id = n.get("subscriptionId")
        resource = n.get("resource", "")  # /users/{mailbox}/messages/{id}
        parts = resource.strip("/").split("/")
        if not parts:
            continue
        msg_id = parts[-1]
        tenant_id = _subscription_tenant_map.get(sub_id) or _resolve_tenant_for_mailbox(MAILBOX)
        if not tenant_id:
            log.warning("No tenant mapping for subscription %s; skipping.", sub_id)
            continue
        atts = EmailProcessingService.graph_list_message_attachments(token, MAILBOX, msg_id)
        meta = EmailMessageMeta(tenant_id=tenant_id, mailbox=MAILBOX, message_id=msg_id)
        urls = svc.save_attachments(meta, atts)
        saved.extend(urls)

    return jsonify({"ok": True, "saved": saved})

@bp_email.route("/email/test", methods=["GET"])
def email_test():
    token = _get_access_token()

    # Resolve folder
    folder_url = f"{GRAPH_BASE}/users/{MAILBOX}/mailFolders?$filter=displayName eq '{FOLDER}'"
    r = requests.get(folder_url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    values = r.json().get("value", [])
    if not values:
        abort(400, f"Folder '{FOLDER}' not found.")
    folder_id = values[0]["id"]

    # Last message
    msgs_url = f"{GRAPH_BASE}/users/{MAILBOX}/mailFolders/{folder_id}/messages?$top=1&$orderby=receivedDateTime desc"
    r2 = requests.get(msgs_url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r2.raise_for_status()
    msgs = r2.json().get("value", [])
    if not msgs:
        return jsonify({"ok": True, "note": "No messages in folder."})
    msg = msgs[0]

    svc = EmailProcessingService()
    atts = EmailProcessingService.graph_list_message_attachments(token, MAILBOX, msg["id"])
    tenant_id = _subscription_tenant_map.get("__default__") or _resolve_tenant_for_mailbox(MAILBOX) or "demo"
    meta = EmailMessageMeta(tenant_id=tenant_id, mailbox=MAILBOX, message_id=msg["id"], subject=msg.get("subject"))
    saved = svc.save_attachments(meta, atts)

    return jsonify({"ok": True, "saved": saved, "messageSubject": meta.subject})
