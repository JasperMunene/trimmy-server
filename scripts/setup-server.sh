#!/bin/bash
set -euo pipefail

echo "=== Trimmy Server Setup ==="
echo "This script installs Docker, creates a deploy user, and sets up directories."
echo "Run as root on a fresh Ubuntu/Debian server."
echo ""

# Install Docker
if ! command -v docker &> /dev/null; then
    echo ">>> Installing Docker..."
    apt-get update
    apt-get install -y ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
    echo ">>> Docker installed."
else
    echo ">>> Docker already installed."
fi

# Create deploy user
if ! id "deploy" &>/dev/null; then
    echo ">>> Creating deploy user..."
    useradd -m -s /bin/bash deploy
    usermod -aG docker deploy
    echo ">>> Deploy user created."
else
    echo ">>> Deploy user already exists."
fi

# Create app directories
echo ">>> Creating app directories..."
mkdir -p /opt/trimmy-staging /opt/trimmy-prod
chown deploy:deploy /opt/trimmy-staging /opt/trimmy-prod

# Generate SSH key for GitHub Actions (if not exists)
DEPLOY_KEY="/home/deploy/.ssh/deploy_key"
if [ ! -f "$DEPLOY_KEY" ]; then
    echo ">>> Generating SSH deploy key..."
    sudo -u deploy mkdir -p /home/deploy/.ssh
    sudo -u deploy ssh-keygen -t ed25519 -C "github-actions-deploy" -f "$DEPLOY_KEY" -N ""
    echo ""
    echo "=== IMPORTANT: Add this public key to your GitHub repo as a deploy key ==="
    echo "(Settings > Deploy keys > Add deploy key, check 'Allow write access')"
    echo ""
    cat "${DEPLOY_KEY}.pub"
    echo ""
    echo "=== Copy the PRIVATE key below into GitHub Actions secret SSH_PRIVATE_KEY ==="
    echo ""
    cat "$DEPLOY_KEY"
    echo ""
else
    echo ">>> Deploy key already exists at $DEPLOY_KEY"
fi

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "1. Add the deploy public key to GitHub repo (Settings > Deploy keys)"
echo "2. Add the private key to GitHub Actions secrets (SSH_PRIVATE_KEY)"
echo "3. Clone the repo:"
echo "   su - deploy"
echo "   cd /opt/trimmy-staging && git clone -b sandbox git@github.com:JasperMunene/trimmy-server.git ."
echo "   cd /opt/trimmy-prod && git clone -b main git@github.com:JasperMunene/trimmy-server.git ."
echo "4. Create PostgreSQL databases on 62.169.18.91:"
echo "   CREATE USER trimmy_app WITH PASSWORD 'trimmy@2026';"
echo "   CREATE DATABASE trimmy_staging OWNER trimmy_app;"
echo "   CREATE DATABASE trimmy_prod OWNER trimmy_app;"
