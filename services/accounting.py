from __future__ import annotations
from typing import Dict, Any, List, Optional
import os
import requests

# This client assumes you already have a valid access token + tenant_id for the org.
# Store/refresh tokens in your existing OAuth flow; inject here when posting bills.

XERO_BASE = "https://api.xero.com/api.xro/2.0"

class XeroClient:
    def __init__(self, access_token: str, tenant_id: str):
        self.access_token = access_token
        self.tenant_id = tenant_id

    def _headers(self) -> Dict[str,str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Xero-tenant-id": self.tenant_id,
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

    def get_accounts(self) -> List[Dict[str,Any]]:
        r = requests.get(f"{XERO_BASE}/Accounts", headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json().get("Accounts", [])

    def get_tax_rates(self) -> List[Dict[str,Any]]:
        r = requests.get(f"{XERO_BASE}/TaxRates", headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json().get("TaxRates", [])

    def post_bill(self, contact_name: str, line_items: List[Dict[str,Any]], date_iso: Optional[str] = None, reference: Optional[str] = None) -> Dict[str,Any]:
        payload = {
            "Type": "ACCPAY",
            "Contact": {"Name": contact_name},
            "Date": date_iso,
            "LineItems": line_items,
            "Reference": reference,
            "Status": "SUBMITTED"  # or "AUTHORISED" if you want immediate posting
        }
        r = requests.post(f"{XERO_BASE}/Invoices", headers=self._headers(), json=payload, timeout=30)
        r.raise_for_status()
        return r.json()
