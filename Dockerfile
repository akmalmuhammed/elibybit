FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create data directory
RUN mkdir -p data

# Environment variables (set at runtime)
ENV BYBIT_API_KEY=""
ENV BYBIT_API_SECRET=""
ENV BYBIT_TESTNET="false"
ENV TELEGRAM_BOT_TOKEN=""
ENV TELEGRAM_CHAT_ID=""
ENV DB_PATH="./data/bot.db"
ENV LOG_LEVEL="INFO"

# Run
CMD ["python", "main.py"]
