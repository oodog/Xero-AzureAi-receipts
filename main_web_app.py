#!/usr/bin/env python3
"""
XeroFlow - Automated Receipt Processing SaaS
Main Flask Web Application
"""

import os
import json
import uuid
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
from azure.storage.blob import BlobServiceClient, generate_container_sas, ContainerSasPermissions
from azure.cosmos import CosmosClient, PartitionKey, exceptions as cosmos_exceptions
from azure.keyvault.secrets import SecretClient
from azure.identity import DefaultAzureCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential
import requests
from authlib.integrations.flask_client import OAuth
from werkzeug.security import generate_password_hash, check_password_hash

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Flask app setup
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(24))
app.config['SESSION_TYPE'] = 'filesystem'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Azure clients setup
credential = DefaultAzureCredential()
storage_client = BlobServiceClient(
    account_url=f"https://{os.environ['AZURE_STORAGE_ACCOUNT_NAME']}.blob.core.windows.net",
    credential=credential
)
cosmos_client = CosmosClient(
    url=os.environ['COSMOS_DB_ENDPOINT'],
    credential=credential
)
keyvault_client = SecretClient(
    vault_url=os.environ['KEY_VAULT_URL'],
    credential=credential
)
doc_intelligence_client = DocumentIntelligenceClient(
    endpoint=os.environ['DOCUMENT_INTELLIGENCE_ENDPOINT'],
    credential=credential
)

# Database setup
database = cosmos_client.get_database_client("xeroflow")
tenants_container = database.get_container_client("tenants")
users_container = database.get_container_client("users")
receipts_container = database.get_container_client("receipts")
integrations_container = database.get_container_client("integrations")
audit_container = database.get_container_client("audit")

# OAuth setup
oauth = OAuth(app)

class TenantService:
    """Service for managing multi-tenant operations"""
    
    @staticmethod
    def create_tenant(company_name: str, admin_email: str, plan: str = "starter") -> Dict[str, Any]:
        """Create a new tenant"""
        tenant_id = str(uuid.uuid4())
        
        tenant_data = {
            "id": tenant_id,
            "tenantId": tenant_id,
            "companyName": company_name,
            "adminEmail": admin_email,
            "plan": plan,
            "status": "active",
            "createdAt": datetime.utcnow().isoformat(),
            "settings": {
                "processingEnabled": True,
                "autoPayEnabled": False,
                "notificationsEnabled": True
            },
            "usage": {
                "receiptsProcessed": 0,
                "storageUsed": 0,
                "lastProcessing": None
            }
        }
        
        try:
            tenants_container.create_item(tenant_data)
            
            # Create storage containers for tenant
            TenantService._create_tenant_storage(tenant_id)
            
            logger.info(f"Created tenant: {tenant_id} for {company_name}")
            return tenant_data
            
        except cosmos_exceptions.CosmosResourceExistsError:
            raise ValueError("Tenant already exists")
    
    @staticmethod
    def _create_tenant_storage(tenant_id: str):
        """Create storage containers for a tenant"""
        containers = ["uploads", "processing", "json", "complete"]
        
        for container_name in containers:
            full_container_name = f"tenant-{tenant_id}-{container_name}"
            try:
                storage_client.create_container(full_container_name)
                logger.info(f"Created container: {full_container_name}")
            except Exception as e:
                logger.warning(f"Container {full_container_name} may already exist: {e}")
    
    @staticmethod
    def get_tenant(tenant_id: str) -> Optional[Dict[str, Any]]:
        """Get tenant by ID"""
        try:
            return tenants_container.read_item(item=tenant_id, partition_key=tenant_id)
        except cosmos_exceptions.CosmosResourceNotFoundError:
            return None
    
    @staticmethod
    def get_tenant_sas_urls(tenant_id: str) -> Dict[str, str]:
        """Generate SAS URLs for tenant storage containers"""
        sas_urls = {}
        containers = ["uploads", "processing", "json", "complete"]
        
        # SAS token valid for 24 hours
        expiry = datetime.utcnow() + timedelta(hours=24)
        
        for container_name in containers:
            full_container_name = f"tenant-{tenant_id}-{container_name}"
            
            # Different permissions for different containers
            if container_name == "uploads":
                permissions = ContainerSasPermissions(read=True, write=True, create=True, list=True)
            else:
                permissions = ContainerSasPermissions(read=True, write=True, create=True, delete=True, list=True)
            
            sas_token = generate_container_sas(
                account_name=os.environ['AZURE_STORAGE_ACCOUNT_NAME'],
                container_name=full_container_name,
                account_key=storage_client.credential.account_key,
                permission=permissions,
                expiry=expiry
            )
            
            sas_urls[f"{container_name}_sas_url"] = (
                f"https://{os.environ['AZURE_STORAGE_ACCOUNT_NAME']}.blob.core.windows.net/"
                f"{full_container_name}?{sas_token}"
            )
        
        return sas_urls

class UserService:
    """Service for managing users"""
    
    @staticmethod
    def create_user(tenant_id: str, email: str, password: str, role: str = "user") -> Dict[str, Any]:
        """Create a new user"""
        user_id = str(uuid.uuid4())
        
        user_data = {
            "id": user_id,
            "userId": user_id,
            "tenantId": tenant_id,
            "email": email,
            "passwordHash": generate_password_hash(password),
            "role": role,
            "status": "active",
            "createdAt": datetime.utcnow().isoformat(),
            "lastLogin": None
        }
        
        try:
            users_container.create_item(user_data)
            logger.info(f"Created user: {email} for tenant: {tenant_id}")
            return user_data
        except cosmos_exceptions.CosmosResourceExistsError:
            raise ValueError("User already exists")
    
    @staticmethod
    def authenticate_user(email: str, password: str) -> Optional[Dict[str, Any]]:
        """Authenticate user login"""
        try:
            users = list(users_container.query_items(
                query="SELECT * FROM c WHERE c.email = @email",
                parameters=[{"name": "@email", "value": email}],
                enable_cross_partition_query=True
            ))
            
            if users and check_password_hash(users[0]['passwordHash'], password):
                user = users[0]
                # Update last login
                user['lastLogin'] = datetime.utcnow().isoformat()
                users_container.replace_item(item=user['id'], body=user)
                return user
            
            return None
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            return None

class XeroIntegrationService:
    """Service for managing Xero OAuth integration"""
    
    @staticmethod
    def get_xero_oauth_config(tenant_id: str) -> Optional[Dict[str, str]]:
        """Get Xero OAuth configuration for tenant"""
        try:
            integration = integrations_container.read_item(
                item=f"xero-{tenant_id}", 
                partition_key=tenant_id
            )
            return {
                "client_id": integration.get("clientId"),
                "client_secret": integration.get("clientSecret"),
                "redirect_uri": integration.get("redirectUri"),
                "tenant_id": integration.get("xeroTenantId"),
                "scopes": integration.get("scopes", [])
            }
        except cosmos_exceptions.CosmosResourceNotFoundError:
            return None
    
    @staticmethod
    def save_xero_config(tenant_id: str, client_id: str, client_secret: str, redirect_uri: str):
        """Save Xero OAuth configuration"""
        integration_data = {
            "id": f"xero-{tenant_id}",
            "tenantId": tenant_id,
            "provider": "xero",
            "clientId": client_id,
            "clientSecret": client_secret,
            "redirectUri": redirect_uri,
            "scopes": [
                "openid", "profile", "email",
                "accounting.transactions",
                "accounting.contacts",
                "accounting.settings",
                "accounting.attachments",
                "offline_access"
            ],
            "status": "configured",
            "createdAt": datetime.utcnow().isoformat()
        }
        
        integrations_container.upsert_item(integration_data)
        logger.info(f"Saved Xero config for tenant: {tenant_id}")

# Authentication decorators
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        
        user = users_container.read_item(
            item=session['user_id'], 
            partition_key=session['user_id']
        )
        
        if user.get('role') != 'admin':
            flash('Admin access required', 'error')
            return redirect(url_for('dashboard'))
        
        return f(*args, **kwargs)
    return decorated_function

# Routes
@app.route('/')
def index():
    """Landing page"""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    """User registration"""
    if request.method == 'POST':
        data = request.get_json()
        
        try:
            # Create tenant
            tenant = TenantService.create_tenant(
                company_name=data['companyName'],
                admin_email=data['email'],
                plan=data.get('plan', 'starter')
            )
            
            # Create admin user
            user = UserService.create_user(
                tenant_id=tenant['tenantId'],
                email=data['email'],
                password=data['password'],
                role='admin'
            )
            
            # Log user in
            session['user_id'] = user['userId']
            session['tenant_id'] = tenant['tenantId']
            session['user_role'] = user['role']
            
            return jsonify({
                'success': True,
                'message': 'Account created successfully',
                'redirectUrl': url_for('setup')
            })
            
        except ValueError as e:
            return jsonify({'success': False, 'message': str(e)}), 400
        except Exception as e:
            logger.error(f"Signup error: {e}")
            return jsonify({'success': False, 'message': 'Registration failed'}), 500
    
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    """User login"""
    if request.method == 'POST':
        data = request.get_json()
        
        user = UserService.authenticate_user(data['email'], data['password'])
        
        if user:
            session['user_id'] = user['userId']
            session['tenant_id'] = user['tenantId']
            session['user_role'] = user['role']
            
            return jsonify({
                'success': True,
                'message': 'Login successful',
                'redirectUrl': url_for('dashboard')
            })
        else:
            return jsonify({'success': False, 'message': 'Invalid credentials'}), 401
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    """User logout"""
    session.clear()
    flash('You have been logged out successfully', 'info')
    return redirect(url_for('index'))

@app.route('/setup')
@login_required
def setup():
    """Initial setup page"""
    tenant = TenantService.get_tenant(session['tenant_id'])
    xero_config = XeroIntegrationService.get_xero_oauth_config(session['tenant_id'])
    
    return render_template('setup.html', tenant=tenant, xero_config=xero_config)

@app.route('/dashboard')
@login_required
def dashboard():
    """Main dashboard"""
    tenant = TenantService.get_tenant(session['tenant_id'])
    
    # Get recent receipts
    recent_receipts = list(receipts_container.query_items(
        query="SELECT TOP 10 * FROM c WHERE c.tenantId = @tenant_id ORDER BY c.createdAt DESC",
        parameters=[{"name": "@tenant_id", "value": session['tenant_id']}],
        enable_cross_partition_query=True
    ))
    
    # Get SAS URLs for file upload
    sas_urls = TenantService.get_tenant_sas_urls(session['tenant_id'])
    
    return render_template('dashboard.html', 
                         tenant=tenant, 
                         recent_receipts=recent_receipts,
                         sas_urls=sas_urls)

@app.route('/upload')
@login_required
def upload():
    """Receipt upload page"""
    sas_urls = TenantService.get_tenant_sas_urls(session['tenant_id'])
    return render_template('upload.html', sas_urls=sas_urls)

@app.route('/api/xero/config', methods=['POST'])
@login_required
def save_xero_config():
    """Save Xero OAuth configuration"""
    try:
        data = request.get_json()
        
        XeroIntegrationService.save_xero_config(
            tenant_id=session['tenant_id'],
            client_id=data['clientId'],
            client_secret=data['clientSecret'],
            redirect_uri=data['redirectUri']
        )
        
        return jsonify({'success': True, 'message': 'Xero configuration saved'})
        
    except Exception as e:
        logger.error(f"Error saving Xero config: {e}")
        return jsonify({'success': False, 'message': 'Failed to save configuration'}), 500

@app.route('/api/xero/auth')
@login_required
def xero_auth():
    """Start Xero OAuth flow"""
    xero_config = XeroIntegrationService.get_xero_oauth_config(session['tenant_id'])
    
    if not xero_config:
        return jsonify({'success': False, 'message': 'Xero not configured'}), 400
    
    # Build OAuth URL
    auth_url = (
        f"https://login.xero.com/identity/connect/authorize?"
        f"response_type=code&"
        f"client_id={xero_config['client_id']}&"
        f"redirect_uri={xero_config['redirect_uri']}&"
        f"scope={' '.join(xero_config['scopes'])}&"
        f"state={session['tenant_id']}&"
        f"prompt=consent"
    )
    
    return jsonify({'success': True, 'authUrl': auth_url})

@app.route('/api/receipts')
@login_required
def get_receipts():
    """Get receipts for tenant"""
    try:
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 20))
        offset = (page - 1) * limit
        
        receipts = list(receipts_container.query_items(
            query=f"SELECT * FROM c WHERE c.tenantId = @tenant_id ORDER BY c.createdAt DESC OFFSET {offset} LIMIT {limit}",
            parameters=[{"name": "@tenant_id", "value": session['tenant_id']}],
            enable_cross_partition_query=True
        ))
        
        return jsonify({'success': True, 'receipts': receipts})
        
    except Exception as e:
        logger.error(f"Error fetching receipts: {e}")
        return jsonify({'success': False, 'message': 'Failed to fetch receipts'}), 500

@app.route('/api/processing/status')
@login_required
def processing_status():
    """Get processing status"""
    try:
        tenant = TenantService.get_tenant(session['tenant_id'])
        
        # Count pending uploads
        upload_container = f"tenant-{session['tenant_id']}-uploads"
        upload_blobs = storage_client.get_container_client(upload_container).list_blobs()
        pending_count = sum(1 for _ in upload_blobs)
        
        return jsonify({
            'success': True,
            'status': {
                'pendingUploads': pending_count,
                'totalProcessed': tenant['usage']['receiptsProcessed'],
                'lastProcessing': tenant['usage']['lastProcessing'],
                'processingEnabled': tenant['settings']['processingEnabled']
            }
        })
        
    except Exception as e:
        logger.error(f"Error getting processing status: {e}")
        return jsonify({'success': False, 'message': 'Failed to get status'}), 500

@app.route('/api/settings', methods=['GET', 'POST'])
@login_required
def settings():
    """Get or update tenant settings"""
    if request.method == 'POST':
        try:
            data = request.get_json()
            tenant = TenantService.get_tenant(session['tenant_id'])
            
            # Update settings
            tenant['settings'].update(data)
            
            tenants_container.replace_item(item=tenant['id'], body=tenant)
            
            return jsonify({'success': True, 'message': 'Settings updated'})
            
        except Exception as e:
            logger.error(f"Error updating settings: {e}")
            return jsonify({'success': False, 'message': 'Failed to update settings'}), 500
    
    else:
        tenant = TenantService.get_tenant(session['tenant_id'])
        return jsonify({'success': True, 'settings': tenant['settings']})

@app.errorhandler(404)
def not_found(error):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal error: {error}")
    return render_template('500.html'), 500

if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_ENV') == 'dev', host='0.0.0.0', port=5000)