#!/bin/bash
# Deploy HA Flip Bot to GCP

set -e

PROJECT_ID="your-gcp-project"
ZONE="asia-southeast1-b"          # Singapore — closest to Bybit
INSTANCE_NAME="ha-flip-bot"
MACHINE_TYPE="e2-medium"           # 2 vCPU, 4GB RAM — ~$25/mo

echo "=== Creating GCP instance ==="
gcloud compute instances create $INSTANCE_NAME \
    --project=$PROJECT_ID \
    --zone=$ZONE \
    --machine-type=$MACHINE_TYPE \
    --image-family=ubuntu-2404-lts-amd64 \
    --image-project=ubuntu-os-cloud \
    --boot-disk-size=20GB \
    --boot-disk-type=pd-ssd \
    --tags=ha-flip-bot

echo "=== Waiting for instance to start ==="
sleep 30

echo "=== Copying files ==="
gcloud compute scp --recurse \
    --zone=$ZONE \
    . $INSTANCE_NAME:~/ha-flip-bot/

echo "=== Setting up instance ==="
gcloud compute ssh $INSTANCE_NAME --zone=$ZONE --command="
    cd ~/ha-flip-bot

    # Install Docker
    sudo apt-get update
    sudo apt-get install -y docker.io docker-compose
    sudo systemctl enable docker
    sudo usermod -aG docker \$USER

    # Create .env from template
    cp .env.example .env
    echo '>>> Edit .env with your API keys: nano ~/ha-flip-bot/.env'
"

echo ""
echo "=== NEXT STEPS ==="
echo "1. SSH into instance: gcloud compute ssh $INSTANCE_NAME --zone=$ZONE"
echo "2. Edit .env: nano ~/ha-flip-bot/.env"
echo "3. Build: docker build -t ha-flip-bot ."
echo "4. Run: docker run -d --restart=unless-stopped --name bot --env-file .env -v ./data:/app/data ha-flip-bot"
echo "5. Logs: docker logs -f bot"
