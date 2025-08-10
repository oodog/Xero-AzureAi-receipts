from __future__ import annotations
from typing import Dict, Any, List, Optional
import os
from azure.core.credentials import AzureKeyCredential
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex, SimpleField, SearchableField, SearchField, SearchFieldDataType,
    VectorSearch, HnswParameters, VectorSearchAlgorithmConfiguration
)
from azure.search.documents import SearchClient

_SEARCH_ENDPOINT = os.environ["AZURE_SEARCH_ENDPOINT"].rstrip("/")
_SEARCH_KEY = os.environ["AZURE_SEARCH_API_KEY"]
_INDEX_NAME = os.environ.get("AZURE_SEARCH_INDEX", "receipts")

_FIELDS = [
    SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
    SimpleField(name="tenantId", type=SearchFieldDataType.String, filterable=True, facetable=True),
    SearchableField(name="vendorKey", type=SearchFieldDataType.String, filterable=True),
    SearchableField(name="rawText", type=SearchFieldDataType.String),
    SearchField(name="embedding", type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                searchable=True, vector_search_dimensions=1536,  # t-e-3-small dims
                vector_search_configuration="hnsw"),
    SimpleField(name="total", type=SearchFieldDataType.Double, filterable=True, sortable=True),
    SimpleField(name="tax", type=SearchFieldDataType.Double, filterable=True),
    SimpleField(name="date", type=SearchFieldDataType.String, filterable=True),
    SimpleField(name="accountCode", type=SearchFieldDataType.String, filterable=True),
]

_vs = VectorSearch(algorithms=[VectorSearchAlgorithmConfiguration(name="hnsw", kind="hnsw", parameters=HnswParameters())])

_idx_client = SearchIndexClient(endpoint=_SEARCH_ENDPOINT, credential=AzureKeyCredential(_SEARCH_KEY))
_s_client: Optional[SearchClient] = None

def ensure_index():
    try:
        _idx_client.get_index(_INDEX_NAME)
    except Exception:
        index = SearchIndex(name=_INDEX_NAME, fields=_FIELDS, vector_search=_vs)
        _idx_client.create_index(index)

def _search_client() -> SearchClient:
    global _s_client
    if _s_client is None:
        _s_client = SearchClient(endpoint=_SEARCH_ENDPOINT, index_name=_INDEX_NAME, credential=AzureKeyCredential(_SEARCH_KEY))
    return _s_client

# Upsert a single receipt feature doc
# doc must include keys: id, tenantId, vendorKey, rawText, embedding, (optional) total, tax, date, accountCode

def upsert(doc: Dict[str, Any]):
    ensure_index()
    cli = _search_client()
    cli.upload_documents(documents=[doc])

# KNN by embedding within a tenant (and optional vendor filter)

def knn(tenant_id: str, embedding: List[float], vendor_key: Optional[str] = None, k: int = 10) -> List[Dict[str, Any]]:
    ensure_index()
    cli = _search_client()
    filter_expr = f"tenantId eq '{tenant_id}'"
    if vendor_key:
        filter_expr += f" and vendorKey eq '{vendor_key}'"
    results = cli.search(
        search_text="",
        filter=filter_expr,
        vectors=[{"value": embedding, "k": k, "fields": "embedding"}],
        select=["id","tenantId","vendorKey","total","tax","date","accountCode","rawText"],
        top=k
    )
    out = []
    for r in results:
        d = dict(r)
        out.append(d)
    return out
