#!/bin/bash
# Deploy odoo-mcp-server to Cloud Run
# Run this once to set up. Update ODOO_CONNECTIONS or MCP_SECRET by re-running with --update-env-vars.

set -e

# ── CONFIGURE THESE ────────────────────────────────────────────────────────────
PROJECT_ID="your-gcp-project-id"
REGION="asia-southeast1"          # change if needed
SERVICE_NAME="odoo-mcp-server"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

# ──────────────────────────────────────────────────────────────────────────────
# Paste your full connections.json content here as a single-line JSON string:
ODOO_CONNECTIONS='{"connections":{},"default":null}'

# AP Worker (optional — only needed if you use odoo_trigger_ap_worker)
ODOO_AP_WORKER_URL="https://your-ap-worker.run.app"

# ──────────────────────────────────────────────────────────────────────────────

echo "▶ Building image..."
gcloud builds submit --tag "$IMAGE" --project "$PROJECT_ID"

echo "▶ Deploying to Cloud Run with Secret Manager..."
gcloud run deploy "$SERVICE_NAME" \
  --image "$IMAGE" \
  --platform managed \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --allow-unauthenticated \
  --set-env-vars "ODOO_CONNECTIONS=${ODOO_CONNECTIONS}" \
  --set-env-vars "ODOO_AP_WORKER_URL=${ODOO_AP_WORKER_URL}" \
  --set-secrets "MCP_SECRET=odoo-mcp-secret:latest,ODOO_AP_WORKER_SECRET=odoo-ap-worker-secret:latest" \
  --min-instances 0 \
  --max-instances 2 \
  --memory 256Mi \
  --timeout 60

SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" \
  --platform managed \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --format "value(status.url)")

echo ""
echo "✅ Deployed!"
echo ""
echo "MCP endpoint:  ${SERVICE_URL}/sse"
echo "Health check:  ${SERVICE_URL}/healthz"
echo ""
echo "Add this to your plugin .mcp.json:"
echo ""
echo '{'
echo '  "mcpServers": {'
echo '    "odoo-connect": {'
echo '      "type": "http",'
echo "      \"url\": \"${SERVICE_URL}/sse\""
echo '    }'
echo '  }'
echo '}'
