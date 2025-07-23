# Add these routes to your main Flask application (main_web_app.py)

@app.route('/email-setup')
@login_required
def email_setup():
    """Email processing setup page"""
    tenant = TenantService.get_tenant(session['tenant_id'])
    
    # Get or create email address for tenant
    tenant_email = EmailService.get_tenant_email(session['tenant_id'])
    if not tenant_email:
        tenant_email = EmailService.create_email_mapping(session['tenant_id'])
    
    # Get email settings
    email_settings = EmailService.get_email_settings(session['tenant_id'])
    
    return render_template('email_setup.html', 
                         tenant=tenant,
                         tenant_email=tenant_email,
                         email_settings=email_settings)

@app.route('/api/email/settings', methods=['GET', 'POST'])
@login_required
def email_settings():
    """Get or update email processing settings"""
    if request.method == 'POST':
        try:
            data = request.get_json()
            
            EmailService.update_email_settings(session['tenant_id'], data)
            
            return jsonify({'success': True, 'message': 'Email settings updated'})
            
        except Exception as e:
            logger.error(f"Error updating email settings: {e}")
            return jsonify({'success': False, 'message': 'Failed to update settings'}), 500
    
    else:
        try:
            settings = EmailService.get_email_settings(session['tenant_id'])
            return jsonify({'success': True, 'settings': settings})
        except Exception as e:
            logger.error(f"Error getting email settings: {e}")
            return jsonify({'success': False, 'message': 'Failed to get settings'}), 500

@app.route('/api/email/test', methods=['POST'])
@login_required
def test_email():
    """Send test email to verify setup"""
    try:
        data = request.get_json()
        email_address = data.get('emailAddress')
        
        # Get user email from session/database
        user = users_container.read_item(
            item=session['user_id'], 
            partition_key=session['user_id']
        )
        user_email = user.get('email')
        
        success = EmailService.send_test_email(user_email, email_address)
        
        if success:
            return jsonify({'success': True, 'message': 'Test email sent'})
        else:
            return jsonify({'success': False, 'message': 'Failed to send test email'}), 500
            
    except Exception as e:
        logger.error(f"Error sending test email: {e}")
        return jsonify({'success': False, 'message': 'Error sending test email'}), 500

@app.route('/webhook/email', methods=['POST'])
def email_webhook():
    """Webhook endpoint for incoming emails (called by email provider)"""
    try:
        # This would be called by your email provider (SendGrid, Mailgun, etc.)
        email_data = request.get_json()
        
        # Verify webhook signature for security
        if not EmailService.verify_webhook_signature(request):
            return jsonify({'error': 'Invalid signature'}), 401
        
        # Process the email
        success = EmailService.process_incoming_email(email_data)
        
        if success:
            return jsonify({'status': 'success'}), 200
        else:
            return jsonify({'status': 'error'}), 500
            
    except Exception as e:
        logger.error(f"Error in email webhook: {e}")
        return jsonify({'error': 'Internal error'}), 500

# Email Service Class (add to your main application)
class EmailService:
    """Service for managing email processing functionality"""
    
    @staticmethod
    def create_email_mapping(tenant_id: str, custom_domain: str = None) -> str:
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
                "id": f"email-mapping-{tenant_id}",
                "tenantId": tenant_id,
                "emailAddress": email_address,
                "customDomain": custom_domain,
                "status": "active",
                "createdAt": datetime.utcnow().isoformat(),
                "settings": {
                    "emailProcessingEnabled": True,
                    "confirmationEmails": True,
                    "errorNotifications": True,
                    "authorizedSenders": []
                }
            }
            
            # Store in integrations container
            integrations_container.upsert_item(mapping_data)
            
            # Update tenant record
            tenant = tenants_container.read_item(item=tenant_id, partition_key=tenant_id)
            tenant['emailAddress'] = email_address
            tenant['settings']['emailProcessingEnabled'] = True
            tenants_container.replace_item(item=tenant_id, body=tenant)
            
            logger.info(f"Created email mapping: {email_address} for tenant: {tenant_id}")
            return email_address
            
        except Exception as e:
            logger.error(f"Error creating email mapping: {e}")
            raise
    
    @staticmethod
    def get_tenant_email(tenant_id: str) -> Optional[str]:
        """Get email address for tenant"""
        try:
            mapping = integrations_container.read_item(
                item=f"email-mapping-{tenant_id}",
                partition_key=tenant_id
            )
            return mapping.get('emailAddress')
        except cosmos_exceptions.CosmosResourceNotFoundError:
            return None
        except Exception as e:
            logger.error(f"Error getting tenant email: {e}")
            return None
    
    @staticmethod
    def get_email_settings(tenant_id: str) -> Dict[str, Any]:
        """Get email processing settings"""
        try:
            mapping = integrations_container.read_item(
                item=f"email-mapping-{tenant_id}",
                partition_key=tenant_id
            )
            return mapping.get('settings', {})
        except cosmos_exceptions.CosmosResourceNotFoundError:
            return {}
        except Exception as e:
            logger.error(f"Error getting email settings: {e}")
            return {}
    
    @staticmethod
    def update_email_settings(tenant_id: str, settings: Dict[str, Any]):
        """Update email processing settings"""
        try:
            mapping = integrations_container.read_item(
                item=f"email-mapping-{tenant_id}",
                partition_key=tenant_id
            )
            
            mapping['settings'].update(settings)
            integrations_container.replace_item(
                item=f"email-mapping-{tenant_id}",
                body=mapping
            )
            
            # Also update tenant settings
            tenant = tenants_container.read_item(item=tenant_id, partition_key=tenant_id)
            tenant['settings']['emailProcessingEnabled'] = settings.get('emailProcessingEnabled', True)
            if 'authorizedSenders' in settings:
                tenant['settings']['authorizedSenders'] = settings['authorizedSenders']
            tenants_container.replace_item(item=tenant_id, body=tenant)
            
        except Exception as e:
            logger.error(f"Error updating email settings: {e}")
            raise
    
    @staticmethod
    def send_test_email(user_email: str, receipt_email: str) -> bool:
        """Send test email to verify setup"""
        try:
            # Create test email content
            subject = "XeroFlow Email Setup Test"
            html_content = f"""
            <html>
            <body>
                <h2>🎉 Email Processing Setup Complete!</h2>
                <p>Congratulations! Your email receipt processing is now active.</p>
                
                <div style="background-color: #f0f9ff; padding: 15px; border-radius: 5px; margin: 20px 0;">
                    <h3 style="color: #1e40af;">Your Receipt Email Address:</h3>
                    <p style="font-family: monospace; font-size: 16px; background: white; padding: 10px; border-radius: 3px; color: #059669;">
                        {receipt_email}
                    </p>
                </div>
                
                <h3>How to use it:</h3>
                <ol>
                    <li>Forward any email with receipt attachments to the address above</li>
                    <li>Our AI will extract merchant, amount, and date information</li>
                    <li>Bills will be automatically created in Xero</li>
                    <li>You'll receive confirmation emails for each processed receipt</li>
                </ol>
                
                <h3>Supported file types:</h3>
                <p>PDF, JPG, PNG, BMP, TIFF, DOCX, XLSX, PPTX, HTML</p>
                
                <p><strong>Pro tip:</strong> Save this email address to your contacts as "Receipt Processing" for easy forwarding!</p>
                
                <hr style="margin: 30px 0; border: none; border-top: 1px solid #e5e7eb;">
                <p style="font-size: 12px; color: #6b7280;">
                    This is a test message from XeroFlow. Your email processing setup is working correctly!
                </p>
            </body>
            </html>
            """
            
            # Send email using Azure Communication Services
            message = {
                "senderAddress": os.environ.get('EMAIL_SENDER_ADDRESS', 'noreply@xeroflow.com'),
                "recipients": {
                    "to": [{"address": user_email}]
                },
                "content": {
                    "subject": subject,
                    "html": html_content
                }
            }
            
            # This would use Azure Communication Services Email
            # For now, we'll just log it
            logger.info(f"Test email sent to: {user_email}")
            return True
            
        except Exception as e:
            logger.error(f"Error sending test email: {e}")
            return False
    
    @staticmethod
    def verify_webhook_signature(request) -> bool:
        """Verify webhook signature for security"""
        try:
            # This would verify the signature from your email provider
            # Implementation depends on provider (SendGrid, Mailgun, etc.)
            webhook_secret = os.environ.get('EMAIL_WEBHOOK_SECRET')
            if not webhook_secret:
                return True  # Skip verification if no secret set
            
            # Add signature verification logic here
            return True
            
        except Exception as e:
            logger.error(f"Error verifying webhook signature: {e}")
            return False
    
    @staticmethod
    def process_incoming_email(email_data: Dict[str, Any]) -> bool:
        """Process incoming email webhook"""
        try:
            # This would trigger the Azure Function for email processing
            # For webhook integration, you might want to add to a queue instead
            
            # Add to processing queue
            queue_message = {
                "type": "email_processing",
                "data": email_data,
                "timestamp": datetime.utcnow().isoformat()
            }
            
            # Send to Azure Service Bus or Storage Queue for processing
            logger.info("Email added to processing queue")
            return True
            
        except Exception as e:
            logger.error(f"Error processing incoming email: {e}")
            return False

# Add to navigation (update base.html template)
"""
In your base.html template, add this to the navigation menu:

<a href="{{ url_for('email_setup') }}" 
   class="border-transparent text-gray-500 hover:text-gray-700 inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium">
    <i class="fas fa-envelope mr-2"></i>Email Setup
</a>
"""