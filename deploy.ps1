# Feed Me Chinese Bot - PowerShell Deployment Script
# CONFIGURE THESE VARIABLES FOR YOUR SETUP

$REPO_URL = "https://github.com/YOUR_USERNAME/YOUR_REPO.git"
$VM_IP = "YOUR_VM_IP_HERE"
$VM_USER = "YOUR_VM_USERNAME"
$VM_FOLDER = "feed-me-chinese"
$LOCAL_CREDS = "google-creds.json"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Feed Me Chinese Bot - Deployment Script" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Step 1: Initialize Git Repository (if needed)
Write-Host "[1/7] Checking Git repository..." -ForegroundColor Yellow
if (-not (Test-Path ".git")) {
    Write-Host "Initializing Git repository..." -ForegroundColor Green
    git init
    git remote add origin $REPO_URL
    git branch -M main
} else {
    Write-Host "Git repository already initialized" -ForegroundColor Green
}

# Step 2: Add and commit files
Write-Host ""
Write-Host "[2/7] Adding files to Git..." -ForegroundColor Yellow
git add .
git commit -m "Update bot code and configuration"

# Step 3: Push to GitHub
Write-Host ""
Write-Host "[3/7] Pushing to GitHub..." -ForegroundColor Yellow
git push -u origin main

Write-Host "✓ Code pushed to GitHub" -ForegroundColor Green

# Step 4: SSH to VM and setup
Write-Host ""
Write-Host "[4/7] Setting up VM..." -ForegroundColor Yellow
Write-Host "Creating folder and cloning repository on VM..." -ForegroundColor Cyan

$SSH_SETUP = @"
cd ~ && \
if [ ! -d '$VM_FOLDER' ]; then \
    echo 'Creating folder...' && \
    mkdir $VM_FOLDER && \
    cd $VM_FOLDER && \
    echo 'Cloning repository...' && \
    git clone $REPO_URL . \
else \
    echo 'Folder exists, pulling latest...' && \
    cd $VM_FOLDER && \
    git pull origin main \
fi
"@

ssh "${VM_USER}@${VM_IP}" $SSH_SETUP

# Step 5: Create virtual environment
Write-Host ""
Write-Host "[5/7] Creating virtual environment on VM..." -ForegroundColor Yellow

$SSH_VENV = @"
cd ~/$VM_FOLDER && \
if [ ! -d 'venv' ]; then \
    echo 'Creating virtual environment...' && \
    python3 -m venv venv && \
    source venv/bin/activate && \
    pip install --upgrade pip && \
    pip install -r requirements.txt \
else \
    echo 'Virtual environment exists, updating packages...' && \
    source venv/bin/activate && \
    pip install --upgrade pip && \
    pip install -r requirements.txt \
fi
"@

ssh "${VM_USER}@${VM_IP}" $SSH_VENV

# Step 6: Upload Google credentials
Write-Host ""
Write-Host "[6/7] Uploading Google credentials..." -ForegroundColor Yellow

if (Test-Path $LOCAL_CREDS) {
    scp $LOCAL_CREDS "${VM_USER}@${VM_IP}:~/$VM_FOLDER/"
    Write-Host "✓ Google credentials uploaded" -ForegroundColor Green
} else {
    Write-Host "⚠ WARNING: google-creds.json not found!" -ForegroundColor Red
    Write-Host "Please upload it manually:" -ForegroundColor Yellow
    Write-Host "scp google-creds.json ${VM_USER}@${VM_IP}:~/$VM_FOLDER/" -ForegroundColor Cyan
}

# Step 7: Setup and start systemd service
Write-Host ""
Write-Host "[7/7] Setting up systemd service..." -ForegroundColor Yellow

$SSH_SERVICE = @"
cd ~/$VM_FOLDER && \
sudo cp systemd/feed-me-chinese.service /etc/systemd/system/ && \
sudo systemctl daemon-reload && \
sudo systemctl enable feed-me-chinese.service && \
sudo systemctl restart feed-me-chinese.service && \
echo '' && \
echo '=== Service Status ===' && \
sudo systemctl status feed-me-chinese.service --no-pager
"@

ssh "${VM_USER}@${VM_IP}" $SSH_SERVICE

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "✓ Deployment Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "1. Check if .env file exists on VM with your tokens" -ForegroundColor White
Write-Host "2. View logs: ssh ${VM_USER}@${VM_IP} 'sudo journalctl -u feed-me-chinese.service -f'" -ForegroundColor White
Write-Host "3. Test the bot in Telegram" -ForegroundColor White
Write-Host ""
Write-Host "Useful commands:" -ForegroundColor Yellow
Write-Host "  Check status: ssh ${VM_USER}@${VM_IP} 'sudo systemctl status feed-me-chinese.service'" -ForegroundColor Cyan
Write-Host "  View logs:    ssh ${VM_USER}@${VM_IP} 'sudo journalctl -u feed-me-chinese.service -f'" -ForegroundColor Cyan
Write-Host "  Restart:      ssh ${VM_USER}@${VM_IP} 'sudo systemctl restart feed-me-chinese.service'" -ForegroundColor Cyan
Write-Host ""
