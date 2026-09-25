#!/bin/bash
set -euo pipefail

ENVIRONMENT=${1:?Usage: deploy.sh <staging|prod> <branch>}
BRANCH=${2:?Usage: deploy.sh <staging|prod> <branch>}

case "$ENVIRONMENT" in
    staging)
        APP_DIR="/opt/trimmy-staging"
        COMPOSE_FILE="docker-compose.staging.yml"
        ;;
    prod)
        APP_DIR="/opt/trimmy-prod"
        COMPOSE_FILE="docker-compose.prod.yml"
        ;;
    *)
        echo "Unknown environment: $ENVIRONMENT (use 'staging' or 'prod')"
        exit 1
        ;;
esac

echo "=== Deploying Trimmy (${ENVIRONMENT}) ==="
echo "Branch: ${BRANCH}"
echo "Directory: ${APP_DIR}"
echo "Compose: ${COMPOSE_FILE}"
echo ""

cd "$APP_DIR"

# Write .env from environment variables
cat > .env <<EOF
DATABASE_URL=${DATABASE_URL}
SECRET_KEY=${SECRET_KEY}
REDIS_URL=${REDIS_URL:-redis://redis:6379/0}
RABBITMQ_PASSWORD=${RABBITMQ_PASSWORD:-}
RESEND_API_KEY=${RESEND_API_KEY:-}
RESEND_FROM_EMAIL=${RESEND_FROM_EMAIL:-noreply@trimmy.com}
CLIENT_URL=${CLIENT_URL:-}
CORS_ORIGINS=${CORS_ORIGINS:-}
GOOGLE_AUTH_CLIENT_ID=${GOOGLE_AUTH_CLIENT_ID:-}
GOOGLE_AUTH_CLIENT_SECRET=${GOOGLE_AUTH_CLIENT_SECRET:-}
GOOGLE_REDIRECT_URI=${GOOGLE_REDIRECT_URI:-}
GOOGLE_WEB_REDIRECT_URL=${GOOGLE_WEB_REDIRECT_URL:-}
GOOGLE_MOBILE_REDIRECT_URL=${GOOGLE_MOBILE_REDIRECT_URL:-trimmy://auth/google}
R2_ENDPOINT=${R2_ENDPOINT:-}
R2_ACCESS_KEY_ID=${R2_ACCESS_KEY_ID:-}
R2_SECRET_ACCESS_KEY=${R2_SECRET_ACCESS_KEY:-}
R2_BUCKET=${R2_BUCKET:-}
R2_PUBLIC_URL=${R2_PUBLIC_URL:-}
EOF

echo ">>> Pulling latest code..."
git fetch origin "$BRANCH"
git reset --hard "origin/$BRANCH"

echo ">>> Building and restarting containers..."
docker compose -f "$COMPOSE_FILE" down
docker compose -f "$COMPOSE_FILE" up -d --build

echo ">>> Waiting for services to start..."
sleep 8

echo ">>> Container status:"
docker compose -f "$COMPOSE_FILE" ps

echo ""
echo "=== Deploy complete (${ENVIRONMENT}) ==="
