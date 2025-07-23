#!/bin/bash

# XeroFlow - Automated Receipt Processing SaaS
# Azure Infrastructure Deployment Script

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILE="$SCRIPT_DIR/azure-template.json"
PARAMETERS_FILE="$SCRIPT_DIR/parameters.json"

# Functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check prerequisites
check_prerequisites() {
    log_info "Checking prerequisites..."
    
    # Check if Azure CLI is installed
    if ! command -v az &> /dev/null; then
        log_error "Azure CLI is not installed. Please install it first."
        log_info "Visit: https://docs.microsoft.com/en-us/cli/azure/install-azure-cli"
        exit 1
    fi
    
    # Check if logged in to Azure
    if ! az account show &> /dev/null; then
        log_error "You are not logged in to Azure. Please run 'az login' first."
        exit 1
    fi
    
    log_success "Prerequisites check passed"
}

# Get user input
get_deployment_config() {
    echo
    log_info "=== XeroFlow Deployment Configuration ==="
    echo
    
    # Company name
    read -p "Enter your company name (no spaces, lowercase): " COMPANY_NAME
    COMPANY_NAME=$(echo "$COMPANY_NAME" | tr '[:upper:]' '[:lower:]' | tr -d ' ')
    
    # Environment
    echo
    echo "Select environment:"
    echo "1) dev - Development"
    echo "2) staging - Staging"
    echo "3) prod - Production"
    read -p "Enter choice (1-3): " ENV_CHOICE
    
    case $ENV_CHOICE in
        1) ENVIRONMENT="dev" ;;
        2) ENVIRONMENT="staging" ;;
        3) ENVIRONMENT="prod" ;;
        *) log_error "Invalid choice"; exit 1 ;;
    esac
    
    # Azure region
    echo
    log_info "Available Azure regions for Document Intelligence:"
    echo "- eastus"
    echo "- westus2"
    echo "- westeurope"
    echo "- australiaeast"
    read -p "Enter Azure region (default: eastus): " LOCATION
    LOCATION=${LOCATION:-eastus}
    
    # Admin email
    read -p "Enter admin email for notifications: " ADMIN_EMAIL
    
    # Resource group
    RESOURCE_GROUP="${COMPANY_NAME}-${ENVIRONMENT}-rg"
    
    echo
    log_info "Configuration Summary:"
    echo "- Company: $COMPANY_NAME"
    echo "- Environment: $ENVIRONMENT"
    echo "- Location: $LOCATION"
    echo "- Resource Group: $RESOURCE_GROUP"
    echo "- Admin Email: $ADMIN_EMAIL"
    echo
    
    read -p "Continue with this configuration? (y/N): " CONFIRM
    if [[ ! $CONFIRM =~ ^[Yy]$ ]]; then
        log_info "Deployment cancelled by user"
        exit 0
    fi
}

# Create resource group
create_resource_group() {
    log_info "Creating resource group: $RESOURCE_GROUP"
    
    az group create \
        --name "$RESOURCE_GROUP" \
        --location "$LOCATION" \
        --output table
    
    log_success "Resource group created"
}

# Generate parameters file
generate_parameters() {
    log_info "Generating deployment parameters..."
    
    cat > "$PARAMETERS_FILE" << EOF
{
  "\$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
  "contentVersion": "1.0.0.0",
  "parameters": {
    "companyName": {
      "value": "$COMPANY_NAME"
    },
    "environment": {
      "value": "$ENVIRONMENT"
    },
    "location": {
      "value": "$LOCATION"
    },
    "adminEmail": {
      "value": "$ADMIN_EMAIL"
    }
  }
}
EOF
    
    log_success "Parameters file generated"
}

# Deploy Azure infrastructure
deploy_infrastructure() {
    log_info "Deploying Azure infrastructure..."
    log_warning "This may take 10-15 minutes..."
    
    DEPLOYMENT_NAME="${COMPANY_NAME}-${ENVIRONMENT}-$(date +%Y%m%d-%H%M%S)"
    
    az deployment group create \
        --resource-group "$RESOURCE_GROUP" \
        --template-file "$TEMPLATE_FILE" \
        --parameters "@$PARAMETERS_FILE" \
        --name "$DEPLOYMENT_NAME" \
        --output table
    
    log_success "Infrastructure deployment completed"
}

# Get deployment outputs
get_deployment_outputs() {
    log_info "Retrieving deployment outputs..."
    
    WEB_APP_URL=$(az deployment group show \
        --resource-group "$RESOURCE_GROUP" \
        --name "$DEPLOYMENT_NAME" \
        --query 'properties.outputs.webAppUrl.value' \
        --output tsv)
    
    STORAGE_ACCOUNT=$(az deployment group show \
        --resource-group "$RESOURCE_GROUP" \
        --name "$DEPLOYMENT_NAME" \
        --query 'properties.outputs.storageAccountName.value' \
        --output tsv)
    
    DOC_INTELLIGENCE_ENDPOINT=$(az deployment group show \
        --resource-group "$RESOURCE_GROUP" \
        --name "$DEPLOYMENT_NAME" \
        --query 'properties.outputs.documentIntelligenceEndpoint.value' \
        --output tsv)
    
    COSMOS_ENDPOINT=$(az deployment group show \
        --resource-group "$RESOURCE_GROUP" \
        --name "$DEPLOYMENT_NAME" \
        --query 'properties.outputs.cosmosDbEndpoint.value' \
        --output tsv)
    
    KEY_VAULT_URL=$(az deployment group show \
        --resource-group "$RESOURCE_GROUP" \
        --name "$DEPLOYMENT_NAME" \
        --query 'properties.outputs.keyVaultUrl.value' \
        --output tsv)
}

# Create storage containers
setup_storage_containers() {
    log_info "Setting up storage containers..."
    
    # Get storage account key
    STORAGE_KEY=$(az storage account keys list \
        --resource-group "$RESOURCE_GROUP" \
        --account-name "$STORAGE_ACCOUNT" \
        --query '[0].value' \
        --output tsv)
    
    # Create containers
    containers=("uploads" "processing" "json" "complete" "templates")
    
    for container in "${containers[@]}"; do
        az storage container create \
            --name "$container" \
            --account-name "$STORAGE_ACCOUNT" \
            --account-key "$STORAGE_KEY" \
            --public-access off \
            --output table
    done
    
    log_success "Storage containers created"
}

# Create Cosmos DB database and containers
setup_cosmos_db() {
    log_info "Setting up Cosmos DB database..."
    
    COSMOS_ACCOUNT="${COMPANY_NAME}-${ENVIRONMENT}-cosmos"
    
    # Create database
    az cosmosdb sql database create \
        --account-name "$COSMOS_ACCOUNT" \
        --resource-group "$RESOURCE_GROUP" \
        --name "xeroflow" \
        --output table
    
    # Create containers
    containers=(
        "tenants:/tenantId"
        "users:/userId"
        "receipts:/tenantId"
        "integrations:/tenantId"
        "audit:/tenantId"
    )
    
    for container_def in "${containers[@]}"; do
        container_name="${container_def%:*}"
        partition_key="${container_def#*:}"
        
        az cosmosdb sql container create \
            --account-name "$COSMOS_ACCOUNT" \
            --resource-group "$RESOURCE_GROUP" \
            --database-name "xeroflow" \
            --name "$container_name" \
            --partition-key-path "$partition_key" \
            --throughput 400 \
            --output table
    done
    
    log_success "Cosmos DB setup completed"
}

# Generate configuration file
generate_config_file() {
    log_info "Generating configuration file..."
    
    cat > "$SCRIPT_DIR/deployment-config.env" << EOF
# XeroFlow Deployment Configuration
# Generated on $(date)

# Environment
COMPANY_NAME=$COMPANY_NAME
ENVIRONMENT=$ENVIRONMENT
RESOURCE_GROUP=$RESOURCE_GROUP
AZURE_LOCATION=$LOCATION

# Application URLs
WEB_APP_URL=$WEB_APP_URL

# Azure Storage
AZURE_STORAGE_ACCOUNT_NAME=$STORAGE_ACCOUNT

# Document Intelligence
DOCUMENT_INTELLIGENCE_ENDPOINT=$DOC_INTELLIGENCE_ENDPOINT

# Cosmos DB
COSMOS_DB_ENDPOINT=$COSMOS_ENDPOINT

# Key Vault
KEY_VAULT_URL=$KEY_VAULT_URL

# Admin
ADMIN_EMAIL=$ADMIN_EMAIL
EOF
    
    log_success "Configuration file created: deployment-config.env"
}

# Create application code deployment package
create_deployment_package() {
    log_info "Creating application deployment package..."
    
    PACKAGE_DIR="$SCRIPT_DIR/deployment-package"
    mkdir -p "$PACKAGE_DIR"
    
    # Copy application files (you'll need to create these)
    cp -r "$SCRIPT_DIR/app" "$PACKAGE_DIR/" 2>/dev/null || log_warning "App directory not found"
    cp -r "$SCRIPT_DIR/functions" "$PACKAGE_DIR/" 2>/dev/null || log_warning "Functions directory not found"
    
    # Create requirements.txt
    cat > "$PACKAGE_DIR/requirements.txt" << EOF
flask>=2.3.0
azure-storage-blob>=12.17.0
azure-ai-documentintelligence>=1.0.0b1
azure-cosmos>=4.5.0
azure-keyvault-secrets>=4.7.0
azure-identity>=1.14.0
python-dotenv>=1.0.0
requests>=2.31.0
cryptography>=41.0.0
flask-session>=0.5.0
authlib>=1.2.0
gunicorn>=21.2.0
EOF
    
    log_success "Deployment package created"
}

# Deploy application code
deploy_application() {
    log_info "Deploying application code..."
    
    WEB_APP_NAME="${COMPANY_NAME}-${ENVIRONMENT}-webapp"
    FUNCTION_APP_NAME="${COMPANY_NAME}-${ENVIRONMENT}-functions"
    
    # Create ZIP package
    cd "$SCRIPT_DIR/deployment-package"
    zip -r "../app-deployment.zip" . > /dev/null
    cd "$SCRIPT_DIR"
    
    # Deploy to web app
    az webapp deployment source config-zip \
        --resource-group "$RESOURCE_GROUP" \
        --name "$WEB_APP_NAME" \
        --src "app-deployment.zip" \
        --output table
    
    log_success "Application deployed to web app"
}

# Print deployment summary
print_summary() {
    echo
    log_success "=== XeroFlow Deployment Complete ==="
    echo
    echo "🌐 Web Application: $WEB_APP_URL"
    echo "📁 Resource Group: $RESOURCE_GROUP"
    echo "🗄️  Storage Account: $STORAGE_ACCOUNT"
    echo "🧠 Document Intelligence: $DOC_INTELLIGENCE_ENDPOINT"
    echo "🌍 Cosmos DB: $COSMOS_ENDPOINT"
    echo "🔐 Key Vault: $KEY_VAULT_URL"
    echo
    log_info "Next Steps:"
    echo "1. Visit your web application to complete setup"
    echo "2. Configure Xero OAuth integration"
    echo "3. Test receipt upload and processing"
    echo "4. Configure monitoring and alerts"
    echo
    log_info "Configuration saved to: deployment-config.env"
    echo
}

# Cleanup function
cleanup() {
    log_info "Cleaning up temporary files..."
    rm -f "$PARAMETERS_FILE"
    rm -f "$SCRIPT_DIR/app-deployment.zip"
    rm -rf "$SCRIPT_DIR/deployment-package"
}

# Main execution
main() {
    echo
    log_info "🚀 XeroFlow - Automated Receipt Processing SaaS"
    log_info "Azure Infrastructure Deployment"
    echo
    
    check_prerequisites
    get_deployment_config
    create_resource_group
    generate_parameters
    deploy_infrastructure
    get_deployment_outputs
    setup_storage_containers
    setup_cosmos_db
    generate_config_file
    create_deployment_package
    deploy_application
    print_summary
    cleanup
    
    log_success "Deployment completed successfully! 🎉"
}

# Error handling
trap 'log_error "Deployment failed. Check the output above for details."; cleanup; exit 1' ERR

# Run main function
main "$@"
