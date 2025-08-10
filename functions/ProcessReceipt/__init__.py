import os
import json
import logging
from azure.storage.blob import BlobClient
import azure.functions as func
from services.di_client import extract_receipt_fields_from_url
from services.embeddings import embed_text
from services.ai_search import knn, upsert
from services.impute import Imputer
from services.accounting import AccountChooser

TENANT_ID_META = "x-ms-meta-tenant-id"

async def main(myblob: func.InputStream):
    logging.info(f"Processing blob: name={myblob.name}, size={myblob.length}")

    # Build a SAS URL or use the blob URL if public (recommend SAS). Here we reuse the event URL if provided via metadata.
    storage_endpoint = os.environ["AZURE_STORAGE_BLOB_ENDPOINT"].rstrip("/")
    account = os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
    container = os.environ.get("RECEIPTS_CONTAINER_PROCESSING", "processing")
    blob_name = myblob.name.split(container + "/",1)[-1]

    file_url = f"{storage_endpoint}/{container}/{blob_name}"

    # Tenant from metadata (ensure your uploader sets it)
    tenant_id = None
    try:
        # If running in Azure, use BlobClient to fetch metadata
        bc = BlobClient(account_url=storage_endpoint, container_name=container, blob_name=blob_name, credential=os.environ["AZURE_STORAGE_ACCOUNT_KEY"])
        props = bc.get_blob_properties()
        tenant_id = props.metadata.get("tenantId") or props.metadata.get("tenant-id")
    except Exception:
        pass

    if not tenant_id:
        logging.warning("No tenantId metadata found; defaulting to 'default'")
        tenant_id = "default"

    # 1) DI extract
    ocr = await extract_receipt_fields_from_url(file_url)

    # 2) Embedding
    vendor = ocr["fields"].get("vendor") or ""
    mix = f"{vendor}\n{ocr['raw_text']}"[:5000]
    vec = embed_text([mix])[0]

    # 3) Neighbors & impute
    neighbors = knn(tenant_id, vec, vendor.lower().strip() if vendor else None)
    fields = Imputer().impute(tenant_id, ocr, neighbors)

    # 4) Account suggestion (org accounts would require a token; store suggestion now)
    acc_code, tax_type, acc_conf = AccountChooser().choose(vendor.lower().strip() if vendor else None, neighbors, org_accounts=[])

    # 5) Upsert into AI Search for learning
    upsert({
        "id": blob_name,  # stable id per file
        "tenantId": tenant_id,
        "vendorKey": (vendor or "").lower(),
        "rawText": ocr["raw_text"],
        "embedding": vec,
        "total": fields.get("total"),
        "tax": fields.get("tax"),
        "date": fields.get("date"),
        "accountCode": acc_code,
    })

    # Optionally, write a JSON alongside the blob for your web app to display
    out_container = os.environ.get("RECEIPTS_CONTAINER_JSON", "json")
    out_name = blob_name.rsplit(".",1)[0] + ".json"
    out_blob = BlobClient(account_url=storage_endpoint, container_name=out_container, blob_name=out_name, credential=os.environ["AZURE_STORAGE_ACCOUNT_KEY"])
    payload = {
        "fields": fields,
        "suggestedAccount": {"code": acc_code, "taxType": tax_type, "confidence": acc_conf},
        "sourceBlob": myblob.name
    }
    out_blob.upload_blob(json.dumps(payload).encode("utf-8"), overwrite=True)
