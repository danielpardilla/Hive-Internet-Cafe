#!/bin/bash
# ICMS-ASI Setup Script (Mac/Linux)

echo ""
echo "==================================================="
echo "  ICMS-ASI Setup — Internet Café Management System"
echo "==================================================="
echo ""

# Create virtual environment
echo "[1/4] Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Install dependencies
echo "[2/4] Installing dependencies..."
pip install -r requirements.txt

# Copy .env
echo "[3/4] Setting up .env..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo " -> Created .env from template."
else
    echo " -> .env already exists."
fi

# Uploads dir
echo "[4/4] Creating uploads directory..."
mkdir -p uploads

echo ""
echo "==================================================="
echo "  Setup complete!"
echo ""
echo "  To START the server:"
echo "    source venv/bin/activate"
echo "    python app.py"
echo ""
echo "  Find your LAN IP: ifconfig | grep 'inet '"
echo "==================================================="
echo ""
