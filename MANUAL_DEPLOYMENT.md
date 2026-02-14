# Manual Deployment Instructions - Feed Me Chinese Bot

## Local Machine Setup

**Working Directory:** Your local project folder

### Step 1: Download Files from Repository

Clone or download all files to your local folder.

Required files:
- vocab_based_chinese_bot.py
- requirements.txt
- .gitignore
- .env.example
- README.md
- DEPLOYMENT.md
- LICENSE
- systemd/feed-me-chinese.service

### Step 2: Create .env File

Create a file called `.env` in the folder with:
```
TELEGRAM_BOT_TOKEN=your_actual_token_here
DEEPSEEK_API_KEY=your_actual_key_here
```

**DO NOT** add this file to Git (it's in .gitignore)

### Step 3: Add google-creds.json

Place your `google-creds.json` file in this folder.

**DO NOT** add this file to Git (it's in .gitignore)

---

## GitHub Setup

### Option A: Use PowerShell Script (Automated)

Edit `deploy.ps1` to add your VM details:
```powershell
$REPO_URL = "https://github.com/YOUR_USERNAME/YOUR_REPO.git"
$VM_IP = "YOUR_VM_IP_HERE"
$VM_USER = "YOUR_VM_USERNAME"
```

Then run:
```powershell
cd "path\to\your\feed_me_chinese"
.\deploy.ps1
```

This will automatically:
1. Initialize Git (if needed)
2. Commit and push to GitHub
3. Deploy to VM
4. Setup systemd service

### Option B: Manual GitHub Push

```bash
# Initialize Git
git init
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git

# Add files
git add .
git commit -m "Initial commit: Feed Me Chinese bot"

# Push to GitHub
git push -u origin main
```

---

## VM Deployment (Manual)

### Step 1: SSH to VM

```bash
ssh YOUR_USERNAME@YOUR_VM_IP
```

### Step 2: Create Folder and Clone

```bash
cd ~
mkdir feed-me-chinese
cd feed-me-chinese
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git .
```

### Step 3: Create Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Step 4: Upload Credentials from Local Machine

**From NEW terminal window (local machine):**

**Windows PowerShell:**
```powershell
# Upload Google credentials
scp "path\to\google-creds.json" YOUR_USERNAME@YOUR_VM_IP:~/feed-me-chinese/

# Upload .env file
scp "path\to\.env" YOUR_USERNAME@YOUR_VM_IP:~/feed-me-chinese/
```

**Linux/Mac:**
```bash
# Upload Google credentials
scp /path/to/google-creds.json YOUR_USERNAME@YOUR_VM_IP:~/feed-me-chinese/

# Upload .env file
scp /path/to/.env YOUR_USERNAME@YOUR_VM_IP:~/feed-me-chinese/
```

**Return to VM SSH session**

Verify files uploaded:
```bash
ls -la ~/feed-me-chinese/google-creds.json
ls -la ~/feed-me-chinese/.env
```

### Step 5: Test Bot Manually (Optional)

```bash
cd ~/feed-me-chinese
source venv/bin/activate
python vocab_based_chinese_bot.py
```

If working, press Ctrl+C to stop.

### Step 6: Setup Systemd Service

```bash
# Copy service file
sudo cp ~/feed-me-chinese/systemd/feed-me-chinese.service /etc/systemd/system/

# Edit the service file to use your username
sudo nano /etc/systemd/system/feed-me-chinese.service
# Change YOUR_USERNAME to your actual username

# Reload systemd
sudo systemctl daemon-reload

# Enable service
sudo systemctl enable feed-me-chinese.service

# Start service
sudo systemctl start feed-me-chinese.service

# Check status
sudo systemctl status feed-me-chinese.service
```

### Step 7: View Logs

```bash
# Live logs
sudo journalctl -u feed-me-chinese.service -f

# Recent logs
sudo journalctl -u feed-me-chinese.service -n 100
```

---

## Quick Update Workflow (After Initial Setup)

### When you update code locally:

```bash
# Commit changes
git add .
git commit -m "Update bot code"
git push origin main

# Deploy to VM
ssh YOUR_USERNAME@YOUR_VM_IP "cd ~/feed-me-chinese && git pull origin main && sudo systemctl restart feed-me-chinese.service"

# Check status
ssh YOUR_USERNAME@YOUR_VM_IP "sudo systemctl status feed-me-chinese.service"
```

**Or using the script:**
```powershell
.\deploy.ps1
```

---

## Troubleshooting

### Bot not starting

```bash
# View logs
sudo journalctl -u feed-me-chinese.service -n 50

# Test manually
cd ~/feed-me-chinese
source venv/bin/activate
python vocab_based_chinese_bot.py
```

### Missing Google credentials

```bash
# Check file exists
ls -la ~/feed-me-chinese/google-creds.json

# If missing, upload again from local machine
```

### Environment variables not loading

```bash
# Check .env file
cat ~/feed-me-chinese/.env

# If missing, upload again from local machine
```

---

## Service Commands Reference

```bash
# Start
sudo systemctl start feed-me-chinese.service

# Stop
sudo systemctl stop feed-me-chinese.service

# Restart
sudo systemctl restart feed-me-chinese.service

# Status
sudo systemctl status feed-me-chinese.service

# Logs (live)
sudo journalctl -u feed-me-chinese.service -f

# Logs (recent 100 lines)
sudo journalctl -u feed-me-chinese.service -n 100
```

---

## Summary

**Service name:** `feed-me-chinese.service`  
**VM folder:** `~/feed-me-chinese`  

**Quick deploy:** `.\deploy.ps1`  
**Quick update:** `ssh YOUR_USERNAME@YOUR_VM_IP "cd ~/feed-me-chinese && git pull && sudo systemctl restart feed-me-chinese.service"`
