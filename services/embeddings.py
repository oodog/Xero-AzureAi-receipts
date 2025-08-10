from __future__ import annotations
from typing import List
import os
from openai import OpenAI

# Supports Azure OpenAI or OpenAI depending on env

def _client() -> OpenAI:
    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    if azure_endpoint:
        # Azure mode
        return OpenAI(
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            base_url=f"{azure_endpoint}openai/deployments/{os.environ['AZURE_OPENAI_EMBEDDING_MODEL']}/",
            default_headers={"api-key": os.environ["AZURE_OPENAI_API_KEY"]},
        )
    # OpenAI mode
    base = os.getenv("OPENAI_BASE_URL")
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url=base)

def embed_text(texts: List[str]) -> List[List[float]]:
    if not texts:
        return []
    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    model = os.getenv("AZURE_OPENAI_EMBEDDING_MODEL") if azure_endpoint else os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
    cli = _client()
    # new OpenAI client returns .data embeddings
    resp = cli.embeddings.create(model=model, input=texts)
    return [d.embedding for d in resp.data]
