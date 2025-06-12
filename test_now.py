"""
Immediate test script - no extra files needed!
"""

import subprocess
import threading
import time
import os


def start_website():
    """Start just the website"""
    print("🌐 Starting website on http://localhost:5000")
    os.system('python leaderboard_app.py')


def start_tunnel():
    """Start cloudflare tunnel"""
    print("🌩️ Starting Cloudflare tunnel...")
    time.sleep(3)  # Wait for website to start

    try:
        # This creates a public URL automatically
        subprocess.run(['cloudflared', 'tunnel', '--url', 'http://localhost:5000'])
    except FileNotFoundError:
        print("❌ cloudflared not found!")
        print("📥 Download from: https://github.com/cloudflare/cloudflared/releases")
        print("🔧 Put cloudflared.exe in this folder or add to PATH")


if __name__ == '__main__':
    print("🧪 QUICK TEST - Website + Cloudflare")
    print("=" * 40)
    print("📋 What this does:")
    print("   1. Starts your website on localhost:5000")
    print("   2. Creates a public Cloudflare tunnel")
    print("   3. Shows you the public URL")
    print("=" * 40)

    # Check if cloudflared exists
    try:
        result = subprocess.run(['cloudflared', 'version'], capture_output=True)
        print("✅ cloudflared found")
    except FileNotFoundError:
        print("❌ cloudflared not installed")
        print("📥 Download: https://github.com/cloudflare/cloudflared/releases")
        print("🔧 Get: cloudflared-windows-amd64.exe (rename to cloudflared.exe)")
        exit(1)

    # Start website in background
    website_thread = threading.Thread(target=start_website, daemon=True)
    website_thread.start()

    # Start tunnel (this will show the public URL)
    print("\n🚀 Starting tunnel...")
    print("⏳ Wait for the public URL to appear...")
    start_tunnel()