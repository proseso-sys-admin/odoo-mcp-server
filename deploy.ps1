# deploy.ps1 — Deploy odoo-mcp-server to Cloud Run
# Run from PowerShell in the folder where this file lives:
#   cd C:\Users\Admin\odoo-mcp-server
#   .\deploy.ps1

$ErrorActionPreference = "Stop"

# ── CONFIG ─────────────────────────────────────────────────────────────────────
$PROJECT_ID   = "odoo-ocr-487104"
$REGION       = "asia-southeast1"
$SERVICE_NAME = "odoo-mcp-server"
$IMAGE        = "gcr.io/$PROJECT_ID/$SERVICE_NAME"

# Odoo connections — update api_key if it ever rotates
$ODOO_CONNECTIONS = '{"connections":{},"default":null}'

# AP Worker — fill in your existing Cloud Run AP worker URL and secret
$ODOO_AP_WORKER_URL    = "https://ap-bill-ocr-worker-727082425075.asia-southeast1.run.app"   # <-- update this
$ODOO_AP_WORKER_SECRET = "Papaya3562"                # <-- update this

# Fixed secret to protect the MCP endpoint
$MCP_SECRET = "km46op1ljw9c8syugv7adx30qe25birn"
# ──────────────────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "=> Switching to gcloud config: odoo-ap-worker" -ForegroundColor Cyan
gcloud config configurations activate odoo-ap-worker

Write-Host ""
Write-Host "=> Enabling required APIs..." -ForegroundColor Cyan
gcloud services enable cloudbuild.googleapis.com run.googleapis.com `
  --project $PROJECT_ID

Write-Host ""
Write-Host "=> Building and pushing image via Cloud Build..." -ForegroundColor Cyan
gcloud builds submit . `
  --tag $IMAGE `
  --project $PROJECT_ID

Write-Host ""
Write-Host "=> Writing env vars file..." -ForegroundColor Cyan
$envFile = ".\env-vars.yaml"
@"
MCP_SECRET: "$MCP_SECRET"
ODOO_CONNECTIONS: '$ODOO_CONNECTIONS'
ODOO_AP_WORKER_URL: "$ODOO_AP_WORKER_URL"
ODOO_AP_WORKER_SECRET: "$ODOO_AP_WORKER_SECRET"
"@ | Out-File -FilePath $envFile -Encoding utf8

Write-Host ""
Write-Host "=> Deploying to Cloud Run..." -ForegroundColor Cyan
gcloud run deploy $SERVICE_NAME `
  --image $IMAGE `
  --platform managed `
  --region $REGION `
  --project $PROJECT_ID `
  --allow-unauthenticated `
  --env-vars-file $envFile `
  --min-instances 0 `
  --max-instances 2 `
  --memory 256Mi `
  --timeout 60

$SERVICE_URL = gcloud run services describe $SERVICE_NAME `
  --platform managed `
  --region $REGION `
  --project $PROJECT_ID `
  --format "value(status.url)"

$MCP_ENDPOINT = "$SERVICE_URL/sse"

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " Deployed!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "MCP endpoint:  $MCP_ENDPOINT" -ForegroundColor Yellow
Write-Host "Health check:  $SERVICE_URL/healthz" -ForegroundColor Yellow
Write-Host ""
Write-Host "Next: set this in Cowork as the ODOO_MCP_URL environment variable:" -ForegroundColor Cyan
Write-Host "  ODOO_MCP_URL=$MCP_ENDPOINT" -ForegroundColor White
Write-Host ""

# Save the endpoint to a file so you don't lose it
"ODOO_MCP_URL=$MCP_ENDPOINT" | Out-File -FilePath ".\mcp-endpoint.txt" -Encoding utf8
Write-Host "Also saved to: mcp-endpoint.txt" -ForegroundColor Gray
