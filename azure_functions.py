import azure.functions as func
import logging
import json
import os
import time
import random
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List

from azure.storage.blob import BlobServiceClient
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.cosmos import CosmosClient
from azure.keyvault.secrets import SecretClient
from azure.identity import DefaultAzureCredential
import requests

# Initialize Azure clients
credential = DefaultAzureCredential()
storage_client = BlobServiceClient(
    account_url=f"https://{os.environ['AZURE_STORAGE_ACCOUNT_NAME']}.blob.core.windows.net",
    credential=credential
)
cosmos_client = CosmosClient(
    url=os.environ['COSMOS_DB_ENDPOINT'],
    credential=credential
)
doc_intelligence_client = DocumentIntelligenceClient(
    endpoint=os.environ['DOCUMENT_INTELLIGENCE_ENDPOINT'],
    credential=credential
)
keyvault_client = SecretClient(
    vault_url=os.environ['KEY_VAULT_URL'],
    credential=credential
)

# Database containers
database = cosmos_client.get_database_client("xeroflow")
tenants_container = database.get_container_client("tenants")
receipts_container = database.get_container_client("receipts")
integrations_container = database.get_container_client("integrations")
audit_container = database.get_container_client("audit")

# Rate limiting for Xero API
class XeroRateLimiter:
    def __init__(self):
        self.request_times = []
        self.max_requests_per_minute = 50
        self.last_rate_limit_time = None
        
    def wait_if_needed(self):
        """Wait if approaching rate limits"""
        now = time.time()
        self.request_times = [t for t in self.request_times if now - t < 60]
        
        if self.last_rate_limit_time and now - self.last_rate_limit_time < 120:
            wait_time = 2 + random.uniform(0.5, 1.5)
            time.sleep(wait_time)
        elif len(self.request_times) >= self.max_requests_per_minute:
            wait_time = 65 - (now - self.request_times[0])
            if wait_time > 0:
                time.sleep(wait_time)
        else:
            time.sleep(random.uniform(0.2, 0.5))
        
        self.request_times.append(now)

rate_limiter = XeroRateLimiter()

def main(myblob: func.InputStream) -> None:
    """
    Azure Function triggered when new file uploaded to uploads container
    """
    logging.info(f"Processing blob: {myblob.name}, Size: {myblob.length} bytes")
    
    try:
        # Extract tenant ID from blob path
        tenant_id = extract_tenant_id_from_path(myblob.name)
        if not tenant_id:
            logging.error(f"Could not extract tenant ID from path: {myblob.name}")
            return
        
        # Process the receipt
        success = process_receipt_blob(myblob, tenant_id)
        
        if success:
            logging.info(f"Successfully processed receipt for tenant: {tenant_id}")
        else:
            logging.error(f"Failed to process receipt for tenant: {tenant_id}")
            
    except Exception as e:
        logging.error(f"Error in main function: {str(e)}")

def extract_tenant_id_from_path(blob_path: str) -> Optional[str]:
    """Extract tenant ID from blob container path"""
    # Path format: tenant-{tenant_id}-uploads/{filename}
    parts = blob_path.split('/')
    if len(parts) >= 1:
        container_part = parts[0] if '/' not in blob_path else blob_path.split('/')[0]
        if container_part.startswith('tenant-') and container_part.endswith('-uploads'):
            return container_part.replace('tenant-', '').replace('-uploads', '')
    return None

def process_receipt_blob(blob: func.InputStream, tenant_id: str) -> bool:
    """Process a single receipt blob"""
    try:
        # Read blob content
        blob_content = blob.read()
        
        # Get tenant configuration
        tenant = get_tenant(tenant_id)
        if not tenant or not tenant['settings']['processingEnabled']:
            logging.info(f"Processing disabled for tenant: {tenant_id}")
            return False
        
        # Extract receipt data using Document Intelligence
        receipt_data = extract_receipt_data_from_blob(blob_content, blob.name)
        if not receipt_data:
            logging.error(f"Failed to extract data from: {blob.name}")
            return False
        
        # Store receipt in database
        receipt_id = store_receipt_data(tenant_id, blob.name, receipt_data)
        
        # Move blob to processing container
        move_blob_to_processing(tenant_id, blob.name, blob_content)
        
        # Process to Xero if configured
        xero_success = process_to_xero(tenant_id, receipt_data, receipt_id)
        
        # Move to appropriate final container
        if xero_success:
            move_blob_to_complete(tenant_id, blob.name, blob_content)
            update_receipt_status(receipt_id, "completed")
        else:
            update_receipt_status(receipt_id, "failed")
        
        # Update tenant usage statistics
        update_tenant_usage(tenant_id)
        
        # Clean up - remove from uploads
        delete_from_uploads(tenant_id, blob.name)
        
        return xero_success
        
    except Exception as e:
        logging.error(f"Error processing receipt blob: {str(e)}")
        return False

def get_tenant(tenant_id: str) -> Optional[Dict[str, Any]]:
    """Get tenant configuration"""
    try:
        return tenants_container.read_item(item=tenant_id, partition_key=tenant_id)
    except Exception:
        return None

def extract_receipt_data_from_blob(content: bytes, filename: str) -> Optional[Dict[str, Any]]:
    """Extract receipt data using Azure Document Intelligence"""
    try:
        from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
        
        # Analyze document
        request = AnalyzeDocumentRequest(bytes_source=content)
        poller = doc_intelligence_client.begin_analyze_document(
            model_id="prebuilt-receipt",
            body=request
        )
        result = poller.result()
        
        # Extract key information
        vendor = "Unknown Vendor"
        transaction_date = None
        total_amount = 0.0
        items = []
        
        if result.documents:
            fields = result.documents[0].fields
            
            # Merchant name
            merchant = fields.get("MerchantName") or fields.get("VendorName")
            if merchant and hasattr(merchant, "value_string"):
                vendor = merchant.value_string.strip()
            
            # Transaction date
            date_field = fields.get("TransactionDate")
            if date_field and hasattr(date_field, "value_date"):
                transaction_date = date_field.value_date.isoformat()
            
            # Total amount
            total_field = fields.get("Total")
            if total_field:
                if hasattr(total_field, "value_currency"):
                    total_amount = total_field.value_currency.amount
                elif hasattr(total_field, "value_number"):
                    total_amount = total_field.value_number
            
            # Line items
            items_field = fields.get("Items")
            if items_field and hasattr(items_field, "value_array"):
                for item in items_field.value_array:
                    if hasattr(item, "value_object"):
                        item_data = {}
                        item_fields = item.value_object
                        
                        # Item description
                        desc = item_fields.get("Description")
                        if desc and hasattr(desc, "value_string"):
                            item_data["description"] = desc.value_string
                        
                        # Quantity
                        qty = item_fields.get("Quantity")
                        if qty and hasattr(qty, "value_number"):
                            item_data["quantity"] = qty.value_number
                        else:
                            item_data["quantity"] = 1
                        
                        # Total price
                        price = item_fields.get("TotalPrice")
                        if price and hasattr(price, "value_currency"):
                            item_data["unit_amount"] = price.value_currency.amount
                        elif price and hasattr(price, "value_number"):
                            item_data["unit_amount"] = price.value_number
                        
                        if item_data.get("description") and item_data.get("unit_amount"):
                            items.append(item_data)
        
        # Calculate tax (assume 10% GST for Australia)
        tax_amount = total_amount * 0.1 if total_amount > 0 else 0
        
        return {
            "merchant": vendor,
            "date": transaction_date or datetime.utcnow().isoformat(),
            "total": total_amount,
            "tax": tax_amount,
            "items": items,
            "raw_result": result.as_dict()
        }
        
    except Exception as e:
        logging.error(f"Error extracting receipt data: {str(e)}")
        return None

def store_receipt_data(tenant_id: str, filename: str, receipt_data: Dict[str, Any]) -> str:
    """Store receipt data in Cosmos DB"""
    receipt_id = f"{tenant_id}-{int(time.time())}-{hash(filename) % 10000}"
    
    receipt_document = {
        "id": receipt_id,
        "tenantId": tenant_id,
        "filename": filename,
        "merchant": receipt_data["merchant"],
        "date": receipt_data["date"],
        "total": receipt_data["total"],
        "tax": receipt_data["tax"],
        "items": receipt_data["items"],
        "status": "processing",
        "createdAt": datetime.utcnow().isoformat(),
        "processedAt": None,
        "xeroInvoiceId": None,
        "xeroStatus": None
    }
    
    receipts_container.create_item(receipt_document)
    return receipt_id

def process_to_xero(tenant_id: str, receipt_data: Dict[str, Any], receipt_id: str) -> bool:
    """Process receipt to Xero"""
    try:
        # Get Xero integration config
        xero_config = get_xero_integration(tenant_id)
        if not xero_config:
            logging.info(f"No Xero integration configured for tenant: {tenant_id}")
            return False
        
        # Get or refresh Xero token
        access_token = get_xero_access_token(tenant_id, xero_config)
        if not access_token:
            logging.error(f"Failed to get Xero access token for tenant: {tenant_id}")
            return False
        
        # Create contact if needed
        contact_id = create_or_get_xero_contact(access_token, xero_config["xeroTenantId"], receipt_data["merchant"])
        if not contact_id:
            logging.error(f"Failed to create/get Xero contact for: {receipt_data['merchant']}")
            return False
        
        # Create invoice (bill) in Xero
        invoice_id = create_xero_invoice(access_token, xero_config["xeroTenantId"], receipt_data, contact_id)
        if not invoice_id:
            logging.error(f"Failed to create Xero invoice for receipt: {receipt_id}")
            return False
        
        # Update receipt with Xero info
        update_receipt_xero_info(receipt_id, invoice_id, "success")
        
        logging.info(f"Successfully created Xero invoice: {invoice_id} for receipt: {receipt_id}")
        return True
        
    except Exception as e:
        logging.error(f"Error processing to Xero: {str(e)}")
        update_receipt_xero_info(receipt_id, None, "error")
        return False

def get_xero_integration(tenant_id: str) -> Optional[Dict[str, Any]]:
    """Get Xero integration configuration"""
    try:
        return integrations_container.read_item(
            item=f"xero-{tenant_id}",
            partition_key=tenant_id
        )
    except Exception:
        return None

def get_xero_access_token(tenant_id: str, xero_config: Dict[str, Any]) -> Optional[str]:
    """Get or refresh Xero access token"""
    try:
        # Check if we have a valid token in Key Vault
        secret_name = f"xero-token-{tenant_id}"
        
        try:
            secret = keyvault_client.get_secret(secret_name)
            token_data = json.loads(secret.value)
            
            # Check if token is still valid
            if datetime.utcnow().timestamp() < token_data.get("expires_at", 0) - 300:
                return token_data["access_token"]
        except Exception:
            pass
        
        # Token expired or doesn't exist, try to refresh
        if "refresh_token" in token_data:
            return refresh_xero_token(tenant_id, xero_config, token_data["refresh_token"])
        
        return None
        
    except Exception as e:
        logging.error(f"Error getting Xero access token: {str(e)}")
        return None

def refresh_xero_token(tenant_id: str, xero_config: Dict[str, Any], refresh_token: str) -> Optional[str]:
    """Refresh Xero access token"""
    try:
        token_data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token
        }
        
        response = requests.post(
            "https://identity.xero.com/connect/token",
            data=token_data,
            auth=(xero_config["clientId"], xero_config["clientSecret"]),
            timeout=30
        )
        
        if response.ok:
            new_token = response.json()
            new_token["expires_at"] = int(datetime.utcnow().timestamp() + new_token["expires_in"])
            
            # Store updated token in Key Vault
            secret_name = f"xero-token-{tenant_id}"
            keyvault_client.set_secret(secret_name, json.dumps(new_token))
            
            return new_token["access_token"]
        
def create_or_get_xero_contact(access_token: str, xero_tenant_id: str, merchant_name: str) -> Optional[str]:
    """Create or get existing Xero contact"""
    try:
        rate_limiter.wait_if_needed()
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "xero-tenant-id": xero_tenant_id,
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        
        # Search for existing contact
        search_url = "https://api.xero.com/api.xro/2.0/Contacts"
        params = {"where": f"Name.Contains(\"{merchant_name}\")"}
        
        response = requests.get(search_url, headers=headers, params=params, timeout=30)
        
        if response.ok:
            contacts = response.json().get("Contacts", [])
            for contact in contacts:
                if contact["Name"].upper() == merchant_name.upper():
                    return contact["ContactID"]
        
        # Create new contact
        rate_limiter.wait_if_needed()
        
        contact_data = {
            "Contacts": [{
                "Name": merchant_name,
                "IsSupplier": True,
                "IsCustomer": False
            }]
        }
        
        response = requests.put(search_url, headers=headers, json=contact_data, timeout=30)
        
        if response.ok:
            new_contact = response.json()["Contacts"][0]
            return new_contact["ContactID"]
        
        return None
        
    except Exception as e:
        logging.error(f"Error creating/getting Xero contact: {str(e)}")
        return None

def create_xero_invoice(access_token: str, xero_tenant_id: str, receipt_data: Dict[str, Any], contact_id: str) -> Optional[str]:
    """Create invoice (bill) in Xero"""
    try:
        rate_limiter.wait_if_needed()
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "xero-tenant-id": xero_tenant_id,
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        
        # Prepare line items
        line_items = []
        if receipt_data["items"]:
            for item in receipt_data["items"]:
                line_items.append({
                    "Description": item["description"][:4000],
                    "Quantity": item["quantity"],
                    "UnitAmount": item["unit_amount"],
                    "AccountCode": "310",  # Default expense account
                    "TaxType": "INPUT" if receipt_data["tax"] > 0 else "NONE"
                })
        else:
            # Fallback single line item
            line_items.append({
                "Description": f"{receipt_data['merchant']} - {receipt_data['date']}",
                "Quantity": 1,
                "UnitAmount": receipt_data["total"],
                "AccountCode": "310",
                "TaxType": "INPUT" if receipt_data["tax"] > 0 else "NONE"
            })
        
        # Create invoice data
        invoice_data = {
            "Invoices": [{
                "Type": "ACCPAY",  # Accounts Payable (Bill)
                "Contact": {"ContactID": contact_id},
                "Date": receipt_data["date"][:10],  # YYYY-MM-DD format
                "DueDate": receipt_data["date"][:10],
                "LineAmountTypes": "Inclusive",
                "LineItems": line_items,
                "Status": "DRAFT",
                "CurrencyCode": "AUD"
            }]
        }
        
        url = "https://api.xero.com/api.xro/2.0/Invoices"
        response = requests.post(url, headers=headers, json=invoice_data, timeout=60)
        
        if response.ok:
            invoice = response.json()["Invoices"][0]
            return invoice["InvoiceID"]
        else:
            logging.error(f"Failed to create Xero invoice: {response.status_code} - {response.text}")
            return None
        
    except Exception as e:
        logging.error(f"Error creating Xero invoice: {str(e)}")
        return None

def move_blob_to_processing(tenant_id: str, filename: str, content: bytes):
    """Move blob to processing container"""
    try:
        processing_container = f"tenant-{tenant_id}-processing"
        blob_client = storage_client.get_blob_client(
            container=processing_container,
            blob=filename
        )
        blob_client.upload_blob(content, overwrite=True)
    except Exception as e:
        logging.error(f"Error moving blob to processing: {str(e)}")

def move_blob_to_complete(tenant_id: str, filename: str, content: bytes):
    """Move blob to complete container"""
    try:
        complete_container = f"tenant-{tenant_id}-complete"
        blob_client = storage_client.get_blob_client(
            container=complete_container,
            blob=filename
        )
        blob_client.upload_blob(content, overwrite=True)
    except Exception as e:
        logging.error(f"Error moving blob to complete: {str(e)}")

def delete_from_uploads(tenant_id: str, filename: str):
    """Delete blob from uploads container"""
    try:
        uploads_container = f"tenant-{tenant_id}-uploads"
        blob_client = storage_client.get_blob_client(
            container=uploads_container,
            blob=filename
        )
        blob_client.delete_blob()
    except Exception as e:
        logging.error(f"Error deleting from uploads: {str(e)}")

def update_receipt_status(receipt_id: str, status: str):
    """Update receipt status in database"""
    try:
        receipt = receipts_container.read_item(item=receipt_id, partition_key=receipt_id.split('-')[0])
        receipt["status"] = status
        receipt["processedAt"] = datetime.utcnow().isoformat()
        receipts_container.replace_item(item=receipt_id, body=receipt)
    except Exception as e:
        logging.error(f"Error updating receipt status: {str(e)}")

def update_receipt_xero_info(receipt_id: str, invoice_id: Optional[str], xero_status: str):
    """Update receipt with Xero information"""
    try:
        receipt = receipts_container.read_item(item=receipt_id, partition_key=receipt_id.split('-')[0])
        receipt["xeroInvoiceId"] = invoice_id
        receipt["xeroStatus"] = xero_status
        receipts_container.replace_item(item=receipt_id, body=receipt)
    except Exception as e:
        logging.error(f"Error updating receipt Xero info: {str(e)}")

def update_tenant_usage(tenant_id: str):
    """Update tenant usage statistics"""
    try:
        tenant = tenants_container.read_item(item=tenant_id, partition_key=tenant_id)
        tenant["usage"]["receiptsProcessed"] += 1
        tenant["usage"]["lastProcessing"] = datetime.utcnow().isoformat()
        tenants_container.replace_item(item=tenant_id, body=tenant)
    except Exception as e:
        logging.error(f"Error updating tenant usage: {str(e)}")

# Additional Azure Function for scheduled processing
def scheduled_processing_function(mytimer: func.TimerRequest) -> None:
    """
    Scheduled function to process any missed receipts and perform maintenance
    Runs every 15 minutes
    """
    if mytimer.past_due:
        logging.info('The timer is past due!')

    logging.info('Starting scheduled processing...')
    
    try:
        # Get all active tenants
        tenants = list(tenants_container.query_items(
            query="SELECT * FROM c WHERE c.status = 'active' AND c.settings.processingEnabled = true",
            enable_cross_partition_query=True
        ))
        
        for tenant in tenants:
            tenant_id = tenant["tenantId"]
            logging.info(f"Checking pending uploads for tenant: {tenant_id}")
            
            # Check for pending uploads
            uploads_container_name = f"tenant-{tenant_id}-uploads"
            try:
                container_client = storage_client.get_container_client(uploads_container_name)
                blobs = list(container_client.list_blobs())
                
                if blobs:
                    logging.info(f"Found {len(blobs)} pending uploads for tenant: {tenant_id}")
                    
                    # Process each blob
                    for blob in blobs[:5]:  # Limit to 5 per run to avoid timeouts
                        try:
                            blob_client = container_client.get_blob_client(blob.name)
                            content = blob_client.download_blob().readall()
                            
                            # Create a mock InputStream object
                            class MockInputStream:
                                def __init__(self, name, content):
                                    self.name = name
                                    self.length = len(content)
                                    self._content = content
                                
                                def read(self):
                                    return self._content
                            
                            mock_blob = MockInputStream(f"{uploads_container_name}/{blob.name}", content)
                            
                            # Process the blob
                            success = process_receipt_blob(mock_blob, tenant_id)
                            
                            if success:
                                logging.info(f"Successfully processed pending upload: {blob.name}")
                            else:
                                logging.error(f"Failed to process pending upload: {blob.name}")
                                
                        except Exception as e:
                            logging.error(f"Error processing pending blob {blob.name}: {str(e)}")
                            
            except Exception as e:
                logging.error(f"Error checking uploads for tenant {tenant_id}: {str(e)}")
        
        logging.info('Scheduled processing completed')
        
    except Exception as e:
        logging.error(f"Error in scheduled processing: {str(e)}")

# Auto-pay bills function
def auto_pay_bills_function(mytimer: func.TimerRequest) -> None:
    """
    Scheduled function to automatically pay bills in Xero
    Runs daily at 2 PM
    """
    if mytimer.past_due:
        logging.info('Auto-pay timer is past due!')

    logging.info('Starting auto-pay processing...')
    
    try:
        # Get all tenants with auto-pay enabled
        tenants = list(tenants_container.query_items(
            query="SELECT * FROM c WHERE c.status = 'active' AND c.settings.autoPayEnabled = true",
            enable_cross_partition_query=True
        ))
        
        for tenant in tenants:
            tenant_id = tenant["tenantId"]
            logging.info(f"Processing auto-pay for tenant: {tenant_id}")
            
            try:
                process_auto_payments(tenant_id)
            except Exception as e:
                logging.error(f"Error in auto-pay for tenant {tenant_id}: {str(e)}")
        
        logging.info('Auto-pay processing completed')
        
    except Exception as e:
        logging.error(f"Error in auto-pay processing: {str(e)}")

def process_auto_payments(tenant_id: str):
    """Process automatic payments for a tenant"""
    try:
        # Get Xero integration
        xero_config = get_xero_integration(tenant_id)
        if not xero_config:
            logging.info(f"No Xero integration for tenant: {tenant_id}")
            return
        
        access_token = get_xero_access_token(tenant_id, xero_config)
        if not access_token:
            logging.error(f"Failed to get Xero access token for auto-pay: {tenant_id}")
            return
        
        # Get bank account ID from tenant settings
        tenant = get_tenant(tenant_id)
        bank_account_id = tenant.get("settings", {}).get("bankAccountId")
        if not bank_account_id:
            logging.info(f"No bank account configured for auto-pay: {tenant_id}")
            return
        
        # Get awaiting payment bills
        bills = get_awaiting_payment_bills(access_token, xero_config["xeroTenantId"])
        
        for bill in bills:
            try:
                create_automatic_payment(access_token, xero_config["xeroTenantId"], bill, bank_account_id)
                logging.info(f"Created auto-payment for bill: {bill['InvoiceID']}")
            except Exception as e:
                logging.error(f"Error creating auto-payment: {str(e)}")
        
    except Exception as e:
        logging.error(f"Error in process_auto_payments: {str(e)}")

def get_awaiting_payment_bills(access_token: str, xero_tenant_id: str) -> List[Dict[str, Any]]:
    """Get bills awaiting payment from Xero"""
    try:
        rate_limiter.wait_if_needed()
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "xero-tenant-id": xero_tenant_id,
            "Accept": "application/json"
        }
        
        url = "https://api.xero.com/api.xro/2.0/Invoices"
        params = {
            "where": 'Type=="ACCPAY" AND Status=="AUTHORISED"',
            "order": "DueDate ASC"
        }
        
        response = requests.get(url, headers=headers, params=params, timeout=30)
        
        if response.ok:
            invoices = response.json().get("Invoices", [])
            return [inv for inv in invoices if inv.get("AmountDue", 0) > 0]
        
        return []
        
    except Exception as e:
        logging.error(f"Error getting awaiting payment bills: {str(e)}")
        return []

def create_automatic_payment(access_token: str, xero_tenant_id: str, bill: Dict[str, Any], bank_account_id: str):
    """Create automatic payment for a bill"""
    try:
        rate_limiter.wait_if_needed()
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "xero-tenant-id": xero_tenant_id,
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        
        # Use due date as payment date
        due_date = bill["DueDate"]
        if due_date.startswith("/Date(") and due_date.endswith(")/"):
            timestamp = int(due_date[6:19])
            payment_date = datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m-%d")
        else:
            payment_date = datetime.utcnow().strftime("%Y-%m-%d")
        
        payment_data = {
            "Payments": [{
                "Invoice": {"InvoiceID": bill["InvoiceID"]},
                "Account": {"AccountID": bank_account_id},
                "Date": payment_date,
                "Amount": bill["AmountDue"]
            }]
        }
        
        url = "https://api.xero.com/api.xro/2.0/Payments"
        response = requests.put(url, headers=headers, json=payment_data, timeout=30)
        
        if not response.ok:
            raise Exception(f"Payment creation failed: {response.status_code} - {response.text}")
        
        # Log payment creation
        audit_log = {
            "id": str(uuid.uuid4()),
            "tenantId": bill.get("Contact", {}).get("ContactID", "unknown"),
            "action": "auto_payment_created",
            "details": {
                "invoiceId": bill["InvoiceID"],
                "amount": bill["AmountDue"],
                "paymentDate": payment_date
            },
            "timestamp": datetime.utcnow().isoformat()
        }
        
        try:
            audit_container.create_item(audit_log)
        except Exception as e:
            logging.error(f"Failed to create audit log: {str(e)}")
        
    except Exception as e:
        logging.error(f"Error creating automatic payment: {str(e)}")
        raise