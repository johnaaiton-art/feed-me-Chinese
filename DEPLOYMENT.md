# Deployment Guide - Feed Me Chinese Bot

This guide covers deploying the bot to a cloud VM (tested on Yandex Cloud with Ubuntu).

## Prerequisites

- Cloud VM with Ubuntu (Yandex Cloud, AWS, DigitalOcean, etc.)
- SSH access to the VM
- GitHub repository access
- Bot credentials (Telegram token, DeepSeek API key, Google credentials)

## VM Information

**SSH Access:**
```bash
ssh YOUR_USERNAME@YOUR_VM_IP
```

## Step 1: Create Project Folder on VM

```bash
ssh YOUR_USERNAME@YOUR_VM_IP
cd ~
mkdir feed-me-chinese
cd feed-me-chinese
```

## Step 2: Clone Repository

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git .
```

## Step 3: Create Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate
```

## Step 4: Install Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

## Step 5: Set Up Environment Variables

Create `.env` file:
```bash
nano .env
```

Add the following:
```
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
DEEPSEEK_API_KEY=your_deepseek_api_key_here
```

Save and exit (Ctrl+X, then Y, then Enter)

## Step 6: Upload Google Credentials

Upload `google-creds.json` to the VM using SCP from your local machine:

```powershell
# From Windows PowerShell
scp "path\to\your\google-creds.json" YOUR_USERNAME@YOUR_VM_IP:~/feed-me-chinese/
```

```bash
# From Linux/Mac
scp /path/to/your/google-creds.json YOUR_USERNAME@YOUR_VM_IP:~/feed-me-chinese/
```

Verify the file exists:
```bash
ls -la ~/feed-me-chinese/google-creds.json
```

## Step 7: Test the Bot Manually

```bash
source venv/bin/activate
python vocab_based_chinese_bot.py
```

If it starts successfully, press Ctrl+C to stop it.

## Step 8: Create Systemd Service

Create the service file:
```bash
sudo nano /etc/systemd/system/feed-me-chinese.service
```

Paste this content (adjust YOUR_USERNAME):
```ini
[Unit]
Description=Feed Me Chinese Vocabulary Bot
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/feed-me-chinese
Environment="PATH=/home/YOUR_USERNAME/feed-me-chinese/venv/bin"
ExecStart=/home/YOUR_USERNAME/feed-me-chinese/venv/bin/python vocab_based_chinese_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Save and exit.

## Step 9: Enable and Start Service

```bash
# Reload systemd
sudo systemctl daemon-reload

# Enable service to start on boot
sudo systemctl enable feed-me-chinese.service

# Start the service
sudo systemctl start feed-me-chinese.service

# Check status
sudo systemctl status feed-me-chinese.service
```

## Step 10: View Logs

To view live logs:
```bash
sudo journalctl -u feed-me-chinese.service -f
```

To view recent logs:
```bash
sudo journalctl -u feed-me-chinese.service -n 100
```

## Updating the Bot

When you make changes and push to GitHub:

```bash
ssh YOUR_USERNAME@YOUR_VM_IP
cd ~/feed-me-chinese
git pull origin main
sudo systemctl restart feed-me-chinese.service
sudo systemctl status feed-me-chinese.service
```

## Troubleshooting

### Bot not starting
```bash
# Check logs
sudo journalctl -u feed-me-chinese.service -n 50

# Test manually
cd ~/feed-me-chinese
source venv/bin/activate
python vocab_based_chinese_bot.py
```

### Missing dependencies
```bash
cd ~/feed-me-chinese
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart feed-me-chinese.service
```

### Google credentials error
```bash
# Verify file exists
ls -la ~/feed-me-chinese/google-creds.json

# Check permissions
chmod 600 ~/feed-me-chinese/google-creds.json
```

### Environment variables not loading
```bash
# Check .env file
cat ~/feed-me-chinese/.env

# Verify file permissions
chmod 600 ~/feed-me-chinese/.env
```

## Service Management Commands

```bash
# Start service
sudo systemctl start feed-me-chinese.service

# Stop service
sudo systemctl stop feed-me-chinese.service

# Restart service
sudo systemctl restart feed-me-chinese.service

# Check status
sudo systemctl status feed-me-chinese.service

# Enable on boot
sudo systemctl enable feed-me-chinese.service

# Disable on boot
sudo systemctl disable feed-me-chinese.service

# View logs
sudo journalctl -u feed-me-chinese.service -f
```

## Quick Reference

**Service Name:** `feed-me-chinese.service`  
**Folder:** `~/feed-me-chinese/`  

**Update Workflow:**
```bash
ssh YOUR_USERNAME@YOUR_VM_IP
cd ~/feed-me-chinese
git pull origin main
sudo systemctl restart feed-me-chinese.service
sudo systemctl status feed-me-chinese.service
```
