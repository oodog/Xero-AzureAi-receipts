# XeroFlow – Automated Receipt Processing for Xero (AU)

XeroFlow is a multi‑tenant SaaS that automates receipt processing from capture → AI extraction → smart auto‑fill → **Xero bill creation**. It runs on Azure with enterprise‑grade security. This edition includes **learning from past receipts**, **AU ABN extraction/validation**, and **auto account‑code suggestions**.

---

## 🚀 Quick install (one‑liner)

> **Tip:** Commit `scripts/install.sh` from this repo. Then you (or customers) can bootstrap in one command:

```bash
# Local dev
curl -sS https://raw.githubusercontent.com/oodog/Xero-AzureAi-receipts/main/scripts/install.sh | bash -s -- --mode local

# Or deploy to Azure Web App (Linux)
curl -sS https://raw.githubusercontent.com/oodog/Xero-AzureAi-receipts/main/scripts/install.sh | bash -s -- \
  --mode azure-webapp \
  --app-name <your-app-name> \
  --resource-group <your-rg> \
  --location australiaeast
```

If your default branch or path differs, update the URL accordingly.

---

## ✅ What’s included (highlights)

- **Stable Azure Document Intelligence (v4/GA)** for receipt parsing
- **Learning from history** using embeddings + **Azure AI Search (vector index)**
- **Auto‑fill** missing fields (date/currency, vendor‑level hints) with confidence scores
- **AU** enhancements: ABN extraction + checksum validation
- **Auto account code suggestion** based on vendor history + org accounts
- Azure Functions (Blob trigger) to process uploads and create JSON summaries

---

## 🧩 Architecture Overview

**Web (Flask)**

- Multi‑tenant auth, dashboard, upload UI
- Xero OAuth setup + bill posting endpoints
- `/api/receipts/analyze` returns extracted fields + suggestions

**Azure Functions**

- **ProcessReceipt** (Blob trigger): runs extraction → vector upsert → JSON output
- (Optional) Nightly vendor profile refresh (Timer)

**Data & services**

- **Azure Storage**: per‑tenant containers `tenant-{id}-{uploads|processing|json|complete}`
- **Azure AI Search**: vector index of receipts for similarity/learning
- **Cosmos DB**: tenants/users/receipts/integrations/audit
- **Key Vault**: secrets
- **Application Insights**: monitoring (optional)

**Security**

- **User Delegation SAS** (RBAC) for uploads; optional account‑key fallback
- Tenant isolation across storage/search/DB

---

## 📦 Requirements

### Backend (from `requirements.txt`)

```
flask==3.0.2
flask-session==0.6.0
gunicorn==21.2.0
python-dotenv==1.0.1

azure-ai-documentintelligence==1.0.2
azure-storage-blob==12.19.0
azure-cosmos==4.7.0
azure-identity==1.17.1
azure-keyvault-secrets==4.8.0
azure-functions==1.19.0

azure-search-documents==11.5.1

authlib==1.3.1
requests==2.32.3
openai==1.40.0

pydantic==2.8.2
python-dateutil==2.9.0.post0
pillow==10.4.0
cryptography==43.0.1
tenacity==8.5.0
```

### Frontend

- Tailwind CSS (CDN)
- Font Awesome 6
- Vanilla JS

---

## ⚙️ Configuration

Create `.env` (or use Azure App Settings). Minimal variables:

```bash
# Flask
FLASK_SECRET_KEY=...
SESSION_TYPE=filesystem

# Azure Storage
AZURE_STORAGE_ACCOUNT_NAME=yourstorage
# Optional fallback if not using RBAC user‑delegation SAS:
AZURE_STORAGE_ACCOUNT_KEY=your-key
AZURE_STORAGE_BLOB_ENDPOINT=https://yourstorage.blob.core.windows.net

# Azure Document Intelligence (v4)
# Prefer AAD; or supply a key via AZURE_DI_KEY
DOCUMENT_INTELLIGENCE_ENDPOINT=https://<region>.cognitiveservices.azure.com/
AZURE_DI_ENDPOINT=             # alt env name supported
AZURE_DI_KEY=                  # optional

# Cosmos DB
COSMOS_DB_ENDPOINT=https://your-cosmos.documents.azure.com:443/

# Key Vault
KEY_VAULT_URL=https://your-kv.vault.azure.net/

# Azure AI Search (vector)
AZURE_SEARCH_ENDPOINT=https://your-search.search.windows.net
AZURE_SEARCH_API_KEY=...
AZURE_SEARCH_INDEX=receipts

# Embeddings (choose one)
# A) Azure OpenAI
AZURE_OPENAI_ENDPOINT=https://your-aoai.openai.azure.com/
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_API_VERSION=2024-06-01
AZURE_OPENAI_EMBEDDING_MODEL=text-embedding-3-small
# B) OpenAI (if not using Azure)
OPENAI_API_KEY=
OPENAI_BASE_URL=
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
```

> Xero OAuth credentials are saved per‑tenant inside Cosmos (`integrations` container). Supply them through the in‑app Settings page.

---

## 🔁 Install / Deploy

### A) One‑liner installer (recommended)

Use the **Quick install** above. The script can:

- clone the repo
- set up a Python venv & install deps
- scaffold `.env`
- (Azure mode) create plan/app + deploy a zip

### B) Manual steps

```bash
# Clone
git clone https://github.com/oodog/Xero-AzureAi-receipts.git
cd Xero-AzureAi-receipts

# Python env
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Configure env
cp .env.example .env  # then edit values

# Run locally
flask --app main_web_app run --port 5000
# or
gunicorn main_web_app:app --workers 3 --threads 8 --timeout 180
```

---

## 🔌 Xero OAuth Setup (AU)

1. Create an app in the [Xero Developer Portal](https://developer.xero.com/).
2. Redirect URI example: `https://<your-domain>/auth/xero/callback` (or your local/public URL).
3. Scopes (minimum):

```
openid profile email
accounting.transactions
accounting.contacts
accounting.settings
accounting.attachments
offline_access
```

4. In **Settings → Xero** inside XeroFlow, paste the Client ID/Secret and save.

---

## 🧠 Learning & Auto‑fill

XeroFlow stores an **embedding** of each receipt’s text in Azure AI Search. For a new receipt it:

1. Extracts fields via **Azure DI** (Receipt)
2. Builds an embedding and finds similar past receipts (same tenant)
3. **Imputes** missing fields (date/currency/ABN, stable vendor fields)
4. Suggests an **Account Code** (vendor majority → heuristics → fallback)

AU‑specific: detects and validates **ABN** via checksum.

---

## 🔗 Key API endpoints

### Analyze a receipt

```
POST /api/receipts/analyze
Headers: X-Tenant-Id: <tenantId>
Body: { "fileUrl": "https://<acct>.blob.core.windows.net/tenant-<id>-processing/receipt.jpg" }
```

**Response** (truncated):

```json
{
  "fields": {"vendor": "BP", "total": 45.60, "tax": 4.15, "date": "2025-08-10", "currency": "AUD", "abn": "53004085616"},
  "suggestedAccount": {"code": "420", "taxType": "GST on Expenses", "confidence": 0.86},
  "neighbors": [ ... ],
  "id": "..."
}
```

---

## 🖥️ Running the Functions (optional)

If you use the Azure Functions pipeline:

```bash
cd functions
func start
```

- **ProcessReceipt** blob trigger listens on your `processing` container.
- Writes a JSON summary to `json` for the web app to show.

---

## 📊 Monitoring

- Add Application Insights to the Web App and Functions (optional)
- Log key events (extractions, auto‑fills, suggestions) in the `audit` container

---

## 🛠️ Development workflow

```bash
# Lint/test (if you add tests)
pytest -q
```

**Tips**

- Keep learning **per tenant**; don’t leak cross‑tenant neighbors.
- When a user corrects fields, write the final choice back so future suggestions improve.

---

## 🔐 Security checklist

- Prefer **RBAC + user‑delegation SAS**; only use account keys where needed
- Store secrets in **Azure Key Vault**; keep `.env` for local dev only
- Enforce HTTPS; set secure cookies in production

---

## 🧭 Roadmap

- Xero end‑to‑end posting with chart-of-accounts fetch (auto‑apply predicted AccountCode)
- Vendor‑specific custom extraction models (composed) for tricky layouts
- ABN lookup/validation against public registry (optional)

---

## 📝 Changelog (recent)

- Added **installer** (`scripts/install.sh`) for curl|bash
- Switched to **Azure DI v4 GA**
- Added **Azure AI Search** vector learning
- AU **ABN** extraction/validation
- New endpoint ``
- Safer SAS generation (user‑delegation SAS + fallback)

---

## 📞 Support

- Issues: GitHub Issues
- Email: [support@xeroflow.com](mailto\:support@xeroflow.com)

---

**Deploy XeroFlow and let it learn your receipts — so you spend time on business, not paperwork.** 🚀

