from __future__ import annotations
import re
from typing import Optional

ABN_RE = re.compile(r"\b(\d[\s-]?){11}\b")

# ABN checksum: subtract 1 from the first digit, then multiply by weights and mod 89
# Weights: 10 1 3 5 7 9 11 13 15 17 19
_WEIGHTS = [10,1,3,5,7,9,11,13,15,17,19]

def _digits_only(s: str) -> str:
    return re.sub(r"\D", "", s)

def is_valid_abn(s: str) -> bool:
    ds = _digits_only(s)
    if len(ds) != 11 or not ds.isdigit():
        return False
    nums = [int(c) for c in ds]
    nums[0] -= 1
    total = sum(n*w for n,w in zip(nums,_WEIGHTS))
    return total % 89 == 0

def find_abn(text: str) -> Optional[str]:
    if not text:
        return None
    for m in ABN_RE.finditer(text):
        cand = m.group(0)
        if is_valid_abn(cand):
            return _digits_only(cand)
    return None
