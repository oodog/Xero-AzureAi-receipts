from __future__ import annotations
from typing import Dict, Any
import os
import uuid
from flask import Blueprint, request, jsonify
from services.di_client import extract_receipt_fields_from_url
from services.embeddings import embed_text
from services.ai_search import knn, upsert
from services.impute import Imputer
from services.accounting import AccountChooser

bp = Blueprint("receipts", __name__, url_prefix="/api/receipts")

TENANT_HEADER = "X-Tenant-Id"  # you likely already have this

@bp.post("/analyze")
async def analyze_receipt():
    data: Dict[str, Any] = request.get_json(force=True)
    file_url = data["fileUrl"]
    tenant_id = request.headers.get(TENANT_HEADER) or data.get("tenantId")
    if not tenant_id:
        return jsonify({"error":"missing tenant id"}), 400

    # 1) Extract via DI
    ocr = await extract_receipt_fields_from_url(file_url)

    # 2) Build embedding on vendor + text
    vendor = ocr["fields"].get("vendor") or ""
    mix = f"{vendor}\n{ocr['raw_text']}"[:5000]
    vec = embed_text([mix])[0]

    # 3) Neighbor search within tenant
    vendor_key = vendor.lower().strip() if vendor else None
    neighbors = knn(tenant_id, vec, vendor_key)

    # 4) Impute
    imputer = Imputer()
    fields = imputer.impute(tenant_id, ocr, neighbors)

    # 5) Choose account (requires client-side to pass accounts or server to fetch via token)
    # For this stateless route we just return the suggestion; your posting route will fetch org accounts
    chooser = AccountChooser()
    account_code, tax_type, acc_conf = chooser.choose(vendor_key, neighbors, org_accounts=[])  # empty list for now

    # 6) Upsert features for future learning
    doc_id = str(uuid.uuid4())
    upsert({
        "id": doc_id,
        "tenantId": tenant_id,
        "vendorKey": vendor_key or "",
        "rawText": ocr["raw_text"],
        "embedding": vec,
        "total": fields.get("total"),
        "tax": fields.get("tax"),
        "date": fields.get("date"),
        "accountCode": account_code,
    })

    return jsonify({
        "fields": fields,
        "suggestedAccount": {
            "code": account_code,
            "taxType": tax_type,
            "confidence": acc_conf
        },
        "neighbors": neighbors[:5],
        "id": doc_id
    })
