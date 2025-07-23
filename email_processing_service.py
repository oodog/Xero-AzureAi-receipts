#!/usr/bin/env python3
"""
XeroFlow Email Processing Service
Handles receipt processing from forwarded emails using Azure Communication Services
"""

import os
import json
import email
import base64
import hashlib
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import azure.functions as func
from azure.storage.blob import BlobServiceClient
from azure.cosmos import CosmosClient
from azure.communication.email import EmailClient
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
email_client = EmailClient.from_connection_string(
    os.environ['COMMUNICATION_SERVICES_CONNECTION_STRING']
)

# Database containers
database = cosmos_client.get_database_client("xeroflow")
tenants_container = database.get_container_client("tenants")
email_mappings_container = database.get_container_client("email_mappings")
receipts_container = database.get_container_client("receipts")

logger = logging.getLogger(__name__)

class EmailReceiptProcessor:
    """Processes receipts from forwarded emails"""
    
    SUPPORTED_EXTENSIONS = {
        '.pdf', '.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.heif',
        '.docx', '.xlsx', '.pptx', '.html'
    }
    
    def __init__(self):
        self.processed_emails = set()
    
    def process_email(self, email_data: Dict[str, Any]) -> bool:
        """Process incoming email with receipt attachments"""
        try:
            # Parse email
            msg = email.message_from_string(email_data['body'])
            
            # Extract email details
            from_email = self._extract_email_address(msg.get('From', ''))
            to_email = self._extract_email_address(msg.get('To', ''))
            subject = msg.get('Subject', 'No Subject')
            message_id = msg.get('Message-ID', '')
            
            logger.info(f"Processing email from {from_email} to {to_email}")
            
            # Check for duplicate processing
            email_hash = hashlib.sha256(f"{message_id}{from_email}".encode()).hexdigest()
            if email_hash in self.processed_emails:
                logger.info(f"Email already processed: {email_hash}")
                return True
            
            # Find tenant by email mapping
            tenant_id = self._get_tenant_by_email(to_email, from_email)
            if not tenant_id:
                logger.warning(f"No tenant found for email: {to_email} from {from_email}")
                self._send_error_email(from_email, "Email address not registered")
                return False
            
            # Extract attachments
            attachments = self._extract_attachments(msg)
            if not attachments:
                logger.warning(f"No valid attachments found in email from {from_email}")
                self._send_error_email(from_email, "No valid receipt attachments found")
                return False
            
            # Process each attachment
            processed_count = 0
            for attachment in attachments:
                success = self._process_attachment(
                    tenant_id, attachment, from_email, subject
                )
                if success:
                    processed_count += 1
            
            # Send confirmation email
            if processed_count > 0:
                self._send_confirmation_email(from_email, processed_count, subject)
                
            # Mark as processed
            self.processed_emails.add(email_hash)
            
            logger.info(f"Successfully processed {processed_count}/{len(attachments)} attachments")
            return processed_count > 0
            
        except Exception as e:
            logger.error(f"Error processing email: {str(e)}")
            return False
    
    def _extract_email_address(self, email_string: str) -> str:
        """Extract clean email address from email header"""
        if '<' in email_string and '>' in email_string:
            return email_string.split('<')[1].split('>')[0].strip().lower()
        return email_string.strip().lower()
    
    def _get_tenant_by_email(self, to_email: str, from_email: str) -> Optional[str]:
        """Get tenant ID by email mapping"""
        try:
            # Query email mappings
            mappings = list(email_mappings_container.query_items(
                query="SELECT * FROM c WHERE c.emailAddress = @email",
                parameters=[{"name": "@email", "value": to_email}],
                enable_cross_partition_query=True
            ))
            
            for mapping in mappings:
                # Check if sender is authorized
                if self._is_sender_authorized(mapping['tenantId'], from_email):
                    return mapping['tenantId']
            
            return None
            
        except Exception as e:
            logger.error(f"Error getting tenant by email: {str(e)}")
            return None
    
    def _is_sender_authorized(self, tenant_id: str, sender_email: str) -> bool:
        """Check if sender is authorized to send receipts for this tenant"""
        try:
            # Check if sender is a user of this tenant
            users = list(tenants_container.query_items(
                query="SELECT * FROM c WHERE c.tenantId = @tenant_id",
                parameters=[{"name": "@tenant_id", "value": tenant_id}],
                enable_cross_partition_query=True
            ))
            
            # Get tenant settings
            tenant = tenants_container.read_item(item=tenant_id, partition_key=tenant_id)
            authorized_emails = tenant.get('settings', {}).get('authorizedSenders', [])
            
            # Check if sender is authorized
            for user in users:
                if user.get('email', '').lower() == sender_email:
                    return True
            
            return sender_email in authorized_emails
            
        except Exception as e:
            logger.error(f"Error checking sender authorization: {str(e)}")
            return False
    
    def _extract_attachments(self, msg) -> List[Dict[str, Any]]:
        """Extract valid receipt attachments from email"""
        attachments = []
        
        try:
            for part in msg.walk():
                if part.get_content_disposition() == 'attachment':
                    filename = part.get_filename()
                    if not filename:
                        continue
                    
                    # Check if file extension is supported
                    file_ext = os.path.splitext(filename.lower())[1]
                    if file_ext not in self.SUPPORTED_EXTENSIONS:
                        continue
                    
                    # Get file content
                    content = part.get_payload(decode=True)
                    if not content:
                        continue
                    
                    attachments.append({
                        'filename': filename,
                        'content': content,
                        'content_type': part.get_content_type(),
                        'size': len(content)
                    })
            
            return attachments
            
        except Exception as e:
            logger.error(f"Error extracting attachments: {str(e)}")
            return []
    
    def _process_attachment(self, tenant_id: str, attachment: Dict[str, Any], 
                          sender_email: str, subject: str) -> bool:
        """Process a single attachment"""
        try:
            filename = attachment['filename']
            content = attachment['content']
            
            # Generate unique filename
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            email_hash = hashlib.sha256(sender_email.encode()).hexdigest()[:8]
            safe_filename = f"email_{timestamp}_{email_hash}_{filename}"
            
            # Upload to tenant's uploads container
            uploads_container = f"tenant-{tenant_id}-uploads"
            blob_client = storage_client.get_blob_client(
                container=uploads_container,
                blob=safe_filename
            )
            
            # Add metadata
            metadata = {
                'source': 'email',
                'sender': sender_email,
                'subject': subject,
                'original_filename': filename,
                'received_at': datetime.utcnow().isoformat()
            }
            
            blob_client.upload_blob(
                content, 
                metadata=metadata,
                overwrite=True
            )
            
            logger.info(f"Uploaded attachment: {safe_filename} for tenant: {tenant_id}")
            
            # Create receipt record
            self._create_receipt_record(
                tenant_id, safe_filename, attachment, sender_email, subject
            )
            
            return True
            
        except Exception as e:
            logger.error(f"Error processing attachment: {str(e)}")
            return False
    
    def _create_receipt_record(self, tenant_id: str, filename: str, 
                             attachment: Dict[str, Any], sender_email: str, subject: str):
        """Create initial receipt record in database"""
        try:
            receipt_id = f"{tenant_id}-email-{int(datetime.utcnow().timestamp())}"
            
            receipt_document = {
                "id": receipt_id,
                "tenantId": tenant_id,
                "filename": filename,
                "originalFilename": attachment['filename'],
                "source": "email",
                "senderEmail": sender_email,
                "emailSubject": subject,
                "fileSize": attachment['size'],
                "contentType": attachment['content_type'],
                "status": "uploaded",
                "createdAt": datetime.utcnow().isoformat(),
                "processedAt": None,
                "merchant": None,
                "total": None,
                "xeroInvoiceId": None,
                "xeroStatus": "pending"
            }
            
            receipts_container.create_item(receipt_document)
            
        except Exception as e:
            logger.error(f"Error creating receipt record: {str(e)}")
    
    def _send_confirmation_email(self, recipient: str, count: int, subject: str):
        """Send confirmation email to sender"""
        try:
            confirmation_subject = f"Receipt Processed - {subject}"
            confirmation_body = f"""
            <html>
            <body>
                <h2>Receipt Successfully Processed</h2>
                <p>Your receipt has been successfully received and is being processed.</p>
                
                <div style="background-color: #f0f9ff; padding: 15px; border-radius: 5px; margin: 20px 0;">
                    <h3 style="color: #1e40af; margin: 0 0 10px 0;">Processing Details:</h3>
                    <ul style="margin: 0; padding-left: 20px;">
                        <li><strong>Attachments processed:</strong> {count}</li>
                        <li><strong>Original subject:</strong> {subject}</li>
                        <li><strong>Processing time:</strong> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</li>
                    </ul>
                </div>
                
                <p>Your receipt will be automatically:</p>
                <ul>
                    <li>✅ Extracted for merchant and amount information</li>
                    <li>✅ Created as a bill in Xero</li>
                    <li>✅ Filed in your receipt storage</li>
                </ul>
                
                <p>You'll receive another email once processing is complete.</p>
                
                <hr style="margin: 30px 0; border: none; border-top: 1px solid #e5e7eb;">
                <p style="font-size: 12px; color: #6b7280;">
                    This is an automated message from XeroFlow. 
                    To manage your email receipt settings, visit your dashboard.
                </p>
            </body>
            </html>
            """
            
            message = {
                "senderAddress": os.environ.get('EMAIL_SENDER_ADDRESS', 'noreply@xeroflow.com'),
                "recipients": {
                    "to": [{"address": recipient}]
                },
                "content": {
                    "subject": confirmation_subject,
                    "html": confirmation_body
                }
            }
            
            email_client.begin_send(message)
            logger.info(f"Sent confirmation email to: {recipient}")
            
        except Exception as e:
            logger.error(f"Error sending confirmation email: {str(e)}")
    
    def _send_error_email(self, recipient: str, error_message: str):
        """Send error email to sender"""
        try:
            error_subject = "Receipt Processing Error - XeroFlow"
            error_body = f"""
            <html>
            <body>
                <h2>Receipt Processing Error</h2>
                <p>We encountered an issue processing your receipt email.</p>
                
                <div style="background-color: #fef2f2; padding: 15px; border-radius: 5px; margin: 20px 0; border-left: 4px solid #ef4444;">
                    <h3 style="color: #dc2626; margin: 0 0 10px 0;">Error Details:</h3>
                    <p style="margin: 0; color: #7f1d1d;">{error_message}</p>
                </div>
                
                <h3>Common Solutions:</h3>
                <ul>
                    <li><strong>Email not registered:</strong> Make sure you're sending from an authorized email address</li>
                    <li><strong>No attachments:</strong> Ensure your email contains PDF, JPG, PNG, or other supported receipt files</li>
                    <li><strong>File format:</strong> We support PDF, JPG, PNG, BMP, TIFF, DOCX, XLSX, PPTX, and HTML files</li>
                </ul>
                
                <p>Need help? Contact our support team or visit your XeroFlow dashboard to manage settings.</p>
                
                <hr style="margin: 30px 0; border: none; border-top: 1px solid #e5e7eb;">
                <p style="font-size: 12px; color: #6b7280;">
                    This is an automated message from XeroFlow.
                </p>
            </body>
            </html>
            """
            
            message = {
                "senderAddress": os.environ.get('EMAIL_SENDER_ADDRESS', 'noreply@xeroflow.com'),
                "recipients": {
                    "to": [{"address": recipient}]
                },
                "content": {
                    "subject": error_subject,
                    "html": error_body
                }
            }
            
            email_client.begin_send(message)
            logger.info(f"Sent error email to: {recipient}")
            
        except Exception as e:
            logger.error(f"Error sending error email: {str(e)}")

class EmailMappingService:
    """Manages email address mappings for tenants"""
    
    @staticmethod
    def create_email_mapping(tenant_id: str, custom_domain: Optional[str] = None) -> str:
        """Create unique email address for tenant"""
        try:
            # Generate unique email address
            if custom_domain:
                email_address = f"receipts-{tenant_id}@{custom_domain}"
            else:
                # Use default domain
                default_domain = os.environ.get('EMAIL_DOMAIN', 'receipts.xeroflow.com')
                email_address = f"{tenant_id}@{default_domain}"
            
            # Create mapping record
            mapping_data = {
                "id": f"mapping-{tenant_id}",
                "tenantId": tenant_id,
                "emailAddress": email_address,
                "customDomain": custom_domain,
                "status": "active",
                "createdAt": datetime.utcnow().isoformat(),
                "settings": {
                    "confirmationEmails": True,
                    "errorEmails": True,
                    "allowedSenders": []  # Empty means all tenant users allowed
                }
            }
            
            email_mappings_container.upsert_item(mapping_data)
            
            logger.info(f"Created email mapping: {email_address} for tenant: {tenant_id}")
            return email_address
            
        except Exception as e:
            logger.error(f"Error creating email mapping: {str(e)}")
            raise
    
    @staticmethod
    def get_tenant_email(tenant_id: str) -> Optional[str]:
        """Get email address for tenant"""
        try:
            mapping = email_mappings_container.read_item(
                item=f"mapping-{tenant_id}",
                partition_key=tenant_id
            )
            return mapping.get('emailAddress')
        except Exception:
            return None
    
    @staticmethod
    def update_email_settings(tenant_id: str, settings: Dict[str, Any]):
        """Update email processing settings"""
        try:
            mapping = email_mappings_container.read_item(
                item=f"mapping-{tenant_id}",
                partition_key=tenant_id
            )
            
            mapping['settings'].update(settings)
            email_mappings_container.replace_item(
                item=f"mapping-{tenant_id}",
                body=mapping
            )
            
        except Exception as e:
            logger.error(f"Error updating email settings: {str(e)}")
            raise

# Azure Function for email processing
def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    HTTP-triggered Azure Function for processing incoming emails
    This would be called by your email provider's webhook
    """
    try:
        # Parse incoming email data
        email_data = req.get_json()
        
        if not email_data:
            return func.HttpResponse(
                "No email data provided",
                status_code=400
            )
        
        # Process the email
        processor = EmailReceiptProcessor()
        success = processor.process_email(email_data)
        
        if success:
            return func.HttpResponse(
                json.dumps({"status": "success", "message": "Email processed successfully"}),
                status_code=200,
                mimetype="application/json"
            )
        else:
            return func.HttpResponse(
                json.dumps({"status": "error", "message": "Failed to process email"}),
                status_code=500,
                mimetype="application/json"
            )
            
    except Exception as e:
        logger.error(f"Error in email processing function: {str(e)}")
        return func.HttpResponse(
            json.dumps({"status": "error", "message": str(e)}),
            status_code=500,
            mimetype="application/json"
        )

# Email setup function for new tenants
def setup_tenant_email(tenant_id: str, custom_domain: Optional[str] = None) -> str:
    """Set up email processing for a new tenant"""
    try:
        # Create email mapping
        email_address = EmailMappingService.create_email_mapping(tenant_id, custom_domain)
        
        # Update tenant with email address
        tenant = tenants_container.read_item(item=tenant_id, partition_key=tenant_id)
        tenant['emailAddress'] = email_address
        tenant['settings']['emailProcessingEnabled'] = True
        tenants_container.replace_item(item=tenant_id, body=tenant)
        
        logger.info(f"Set up email processing for tenant: {tenant_id}")
        return email_address
        
    except Exception as e:
        logger.error(f"Error setting up tenant email: {str(e)}")
        raise