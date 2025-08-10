from __future__ import annotations
from typing import Dict, Any
import os
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential

_DI_ENDPOINT = os.environ["AZURE_DI_ENDPOINT"].rstrip("/")
_DI_KEY = os.environ["AZURE_DI_KEY"]

_client = DocumentIntelligenceClient(_DI_ENDPOINT, AzureKeyCredential(_DI_KEY))

# Minimal wrapper for prebuilt receipt extraction
async def extract_receipt_fields_from_url(file_url: str) -> Dict[str, Any]:
    poller = _client.begin_analyze_document(
        model_id="prebuilt-receipt",
        document_url=file_url,
        features=["ocr.highResolution"]
    )
    result = poller.result()

    out: Dict[str, Any] = {
        "fields": {},
        "raw_text": [],
        "confidence": {},
    }

    # collect text lines
    if result.pages:
        for page in result.pages:
            for line in getattr(page, "lines", []) or []:
                out["raw_text"].append(line.content)

    # collect key fields (names kept simple)
    for doc in getattr(result, "documents", []) or []:
        f = doc.fields or {}
        def getv(name: str):
            if name in f and f[name] and getattr(f[name], "value", None) is not None:
                return f[name].value, f[name].confidence or 0.0
            return None, 0.0

        vendor, v_conf = getv("MerchantName")
        total, t_conf = getv("Total")
        subtotal, s_conf = getv("Subtotal")
        tax, x_conf = getv("TotalTax")
        date, d_conf = getv("TransactionDate")
        currency, c_conf = getv("Currency")

        out["fields"].update({
            "vendor": vendor,
            "total": float(total) if total is not None else None,
            "subtotal": float(subtotal) if subtotal is not None else None,
            "tax": float(tax) if tax is not None else None,
            "date": str(date) if date is not None else None,
            "currency": currency,
        })
        out["confidence"].update({
            "vendor": v_conf,
            "total": t_conf,
            "subtotal": s_conf,
            "tax": x_conf,
            "date": d_conf,
            "currency": c_conf,
        })

    out["raw_text"] = "\n".join(out["raw_text"]) if out["raw_text"] else ""
    return out
