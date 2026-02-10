# GCP VM Setup Guide: From Zero to Production

This guide covers setting up a Google Cloud Platform (GCP) Virtual Machine (VM) to host the **Eli Trading Bot** (v4 Production).

---

## 1. Create the VM Instance

1.  Go to the **Google Cloud Console** (https://console.cloud.google.com).
2.  Navigate to **Compute Engine** > **VM instances**.
3.  Click **Create Instance**.
4.  **Configuration**:
    - **Name**: `eli-bot-prod` (example)
    - **Region**: Choose a region close to Bybit servers (e.g., `asia-southeast1` (Singapore) or `europe-west4` (Netherlands)).
    - **Machine configuration**:
      - **Series**: `E2`
      - **Machine type**: `e2-micro` (Free Tier eligible*) or `e2-small` (Better performance, ~$14/mo). *Rec: e2-small for stability.\*
    - **Boot disk**:
      - Click **Change**.
      - **OS**: **Ubuntu**
      - **Version**: **Ubuntu 22.04 LTS** (x86/64).
      - **Size**: 20 GB (Standard Persistent Disk).
    - **Firewall**: Check "Allow HTTP traffic" and "Allow HTTPS traffic" (useful if we add a dashboard later).
5.  Click **Create**.

> **Tip**: Assign a **Static IP** so you don't have to whitelist new IPs if you restart the VM.
>
> - Go to **VPC network** > **IP addresses**.
> - Click **Reserve external static IP**.
> - Attach it to your `eli-bot-prod` VM.

---

## 2. Connect via SSH

1.  In the VM instances list, click the **SSH** button next to your instance.
2.  A browser window will open with a command line. This is your server.

---

## 3. Install Dependencies (Docker & Git)

Run these commands one by one to update the system and install Docker.

```bash
# Update package list
sudo apt-get update
sudo apt-get upgrade -y

# Install Docker
sudo apt-get install -y docker.io

# Install Git (usually pre-installed, but good to check)
sudo apt-get install -y git

# Enable Docker to start on boot
sudo systemctl enable docker
sudo systemctl start docker

# Allow your user to run Docker commands without sudo (Optional but convenient)
sudo usermod -aG docker $USER
# (You might need to exit SSH and reconnect for this group change to take effect)
```

---

## 4. Clone the Repository

Get the latest production code (v4).

```bash
# Clone the repo
git clone https://github.com/akmalmuhammed/elibybit.git

# Enter the directory
cd elibybit
```

---

## 5. Configure Environment

You need to create the `.env` file with your **Production API Keys**.

1.  Copy the example file:

    ```bash
    cp .env.example .env
    ```

2.  Edit the file using `nano`:

    ```bash
    nano .env
    ```

3.  Paste your keys and settings:

    ```ini
    BYBIT_API_KEY=your_production_key_here
    BYBIT_API_SECRET=your_production_secret_here
    BYBIT_TESTNET=false

    # TELEGRAM (Required for notifications)
    TELEGRAM_BOT_TOKEN=your_bot_token
    TELEGRAM_CHAT_ID=your_chat_id

    # CRITICAL: Enable live trading (disable dry run)
    DRY_RUN=false
    ```

4.  Save and exit: Press `Ctrl+O`, `Enter`, then `Ctrl+X`.

---

## 6. Build and Run (Docker)

Using Docker is the most reliable way to run in production. It handles restarts automatically.

### Option A: Using `deploy.sh` (If available)

If the repo has the helper script:

```bash
chmod +x deploy.sh
./deploy.sh
```

### Option B: Manual Docker Command

If you want to run it manually:

1.  **Build the image**:

    ```bash
    docker build -t eli-bot .
    ```

2.  **Run the container** (Detached mode, auto-restart):
    ```bash
    docker run -d \
      --name eli-bot \
      --restart unless-stopped \
      -v $(pwd)/data:/app/data \
      --env-file .env \
      eli-bot
    ```

---

## 7. Verification & Monitoring

1.  **Check if it's running**:

    ```bash
    docker ps
    ```

    You should see `eli-bot` in the list with status `Up`.

2.  **View Logs** (Real-time):

    ```bash
    docker logs -f eli-bot
    ```

    - Look for `[BOOT] ✅ All systems go`.
    - Look for `[RECONCILE]` to confirm it checked your positions.
    - Verify `Health Check` logs are appearing every 5 minutes.

3.  **Check Telegram**: You should have received a "Started ✅" message.

---

## 8. Managing Updates

When you push new code to GitHub, update the production bot:

1.  **Pull changes**:

    ```bash
    cd elibybit
    git pull
    ```

2.  **Rebuild and Restart**:

    ```bash
    # Stop old container
    docker stop eli-bot
    docker rm eli-bot

    # Rebuild image
    docker build -t eli-bot .

    # Run new version
    docker run -d \
      --name eli-bot \
      --restart unless-stopped \
      -v $(pwd)/data:/app/data \
      --env-file .env \
      eli-bot
    ```

---

## Troubleshooting

- **Authentication Failed**:
  - Check `.env` keys.
  - **Check IP Whitelist**: Did you add the GCP VM's **External IP** to your Bybit API Key settings? (If you reserved a static IP, use that).
- **Container keeps restarting**:
  - Check logs: `docker logs eli-bot --tail 100`
  - Common errors: Missing env vars, invalid API permissions.
