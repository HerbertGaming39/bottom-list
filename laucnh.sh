#!/bin/bash
# launch.sh — runs the Python app in a virtual environment

# Create venv if it doesn't exist
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

# Activate the virtual environment
source venv/bin/activate

# Upgrade pip and install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Run app.py in the background and log output
nohup python app.py > logs.txt 2>&1 &

echo "App started in background with logs.txt capturing output."

