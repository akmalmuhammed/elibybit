# Deploying Eli Bot to GCP

## Prerequisites

1. **Google Cloud Account**: [Create here](https://console.cloud.google.com/)
2. **Google Cloud SDK**: [Install here](https://cloud.google.com/sdk/docs/install)
3. **Bybit API Keys**: Ensure they are **IP Whitelisted** for your GCP VM's external IP.

## Deployment Methods

**Recommendation**: Use **Option 1 (GitHub)** for long-term reliability and version control. Use **Option 2 (Direct)** for quick testing.

## Option 1: Deploy via GitHub (Recommended)

1. **Create a Repo**: Create a new private repository on GitHub.
2. **Push Code**:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin <your-repo-url>
   git push -u origin main
   ```
3. **SSH into VM** and Clone:
   ```bash
   # Generate SSH key for GitHub (if needed) or use HTTPS token
   git clone <your-repo-url>
   cd eli
   ```
4. **Setup**:
   ```bash
   cp .env.example .env
   nano .env  # Add your keys (NEVER commit .env to GitHub!)
   docker build -t eli-bot .
   docker run -d --restart=unless-stopped --name bot --env-file .env -v $(pwd)/data:/app/data eli-bot
   ```

## Option 2: Quick Deploy (Direct Copy)

1. **Initialize gcloud**:

   ```powershell
   gcloud init
   ```

2. **Create the VM**:

   ```powershell
   gcloud compute instances create eli-bot ^
       --project=YOUR_PROJECT_ID ^
       --zone=asia-southeast1-b ^
       --machine-type=e2-medium ^
       --image-family=ubuntu-2404-lts-amd64 \
       --image-project=ubuntu-os-cloud \
       --boot-disk-size=20GB
   ```

   _(Note: `asia-southeast1` (Singapore) is recommended for Bybit latency)_

3. **SSH into the VM**:

   ```powershell
   gcloud compute ssh eli-bot --zone=asia-southeast1-b
   ```

4. **Install Docker on VM**:

   ```bash
   # In the SSH terminal:
   sudo apt-get update
   sudo apt-get install -y docker.io
   sudo usermod -aG docker $USER
   exit
   # Re-login to apply group changes
   gcloud compute ssh eli-bot --zone=asia-southeast1-b
   ```

5. **Copy Files to VM**:

   ```powershell
   # From your local machine (eli workspace):
   gcloud compute scp --recurse . eli-bot:~/eli-bot --zone=asia-southeast1-b
   ```

6. **Build and Run**:

   ```bash
   # In the SSH terminal:
   cd ~/eli-bot

   # Create .env file with your keys
   nano .env
   # Paste your keys:
   # BYBIT_API_KEY=...
   # BYBIT_API_SECRET=...
   # BYBIT_TESTNET=false

   # Build
   docker build -t eli-bot .

   # Run in background
   docker run -d --restart=unless-stopped --name bot --env-file .env -v $(pwd)/data:/app/data eli-bot
   ```

7. **Check Logs**:
   ```bash
   docker logs -f bot
   ```

## Option 2: Manual Setup (No Docker)

1. **SSH into VM**.
2. **Install Python 3.12**:
   ```bash
   sudo apt-get update
   sudo apt-get install -y python3.12 python3.12-venv
   ```
3. **Setup**:
   ```bash
   python3.12 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
4. **Run**:
   ```bash
   nohup python main.py > bot.log 2>&1 &
   ```

## Important: IP Whitelisting

After creating your VM, get its **External IP**:

```bash
gcloud compute instances describe eli-bot --zone=asia-southeast1-b --format='get(networkInterfaces[0].accessConfigs[0].natIP)'
```

**Add this IP to your Bybit API Key settings.**
