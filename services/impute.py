from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
from dateutil import parser as dateparser
from utils.abn import find_abn

# Simple heuristics + neighbor borrowing with confidence thresholds

def _parse_date(raw_text: str) -> Optional[str]:
    try:
        dt = dateparser.parse(raw_text, dayfirst=True, fuzzy=True)
        if dt:
            return dt.date().isoformat()
    except Exception:
        return None
    return None

class Imputer:
    def __init__(self, min_field_conf: float = 0.6, min_neighbor_agree: float = 0.7):
        self.min_field_conf = min_field_conf
        self.min_neighbor_agree = min_neighbor_agree

    def vendor_key(self, vendor: Optional[str]) -> Optional[str]:
        return vendor.lower().strip() if vendor else None

    def from_neighbors(self, field: str, neighbors: List[Dict[str, Any]]) -> Tuple[Optional[Any], float]:
        if not neighbors:
            return None, 0.0
        # majority voting
        counts: Dict[Any,int] = {}
        for n in neighbors:
            val = n.get(field)
            if val in (None, "", 0):
                continue
            counts[val] = counts.get(val, 0) + 1
        if not counts:
            return None, 0.0
        best_val, votes = max(counts.items(), key=lambda kv: kv[1])
        conf = votes / max(1, len(neighbors))
        return (best_val, conf) if conf >= self.min_neighbor_agree else (None, conf)

    def impute(self, tenant_id: str, ocr: Dict[str, Any], neighbors: List[Dict[str, Any]]) -> Dict[str, Any]:
        fields = dict(ocr.get("fields", {}))
        conf = dict(ocr.get("confidence", {}))
        raw_text = ocr.get("raw_text", "")

        # ABN
        abn = find_abn(raw_text)
        if abn:
            fields["abn"] = abn

        # Date fallback
        if not fields.get("date") or conf.get("date",0) < self.min_field_conf:
            guess = _parse_date(raw_text)
            if guess:
                fields["date"] = guess

        # Currency fallback (AUD default)
        if not fields.get("currency"):
            fields["currency"] = "AUD"

        # Vendor-level borrow for account codes etc handled by accounting.py
        return fields
