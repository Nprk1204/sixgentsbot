#!/usr/bin/env python3
"""
Fixed tunnel debug with correct cloudflared syntax
"""

import subprocess
import time
import requests
import threading
import os


def test_local_connection():
    """Test local Flask connection"""
    print("🧪 Testing local Flask...")

    for i in range(5):
        try:
            response = requests.get('http://localhost:5000', timeout=5)
            if response.status_code == 200:
                print(f"✅ Local Flask working! Status: {response.status_code}")
                return True
        except:
            print(f"⏳ Attempt {i + 1}: Waiting for Flask...")
            time.sleep(2)

    print("❌ Local Flask not responding!")
    return False


def start_tunnel_simple(token):
    """Start tunnel with simple, working command"""
    print("🌩️ Starting tunnel...")

    cmd = [
        'cloudflared', 'tunnel', 'run',
        '--token', token,
        '--url', 'http://localhost:5000'
    ]

    print(f"🔧 Command: {' '.join(cmd[:3])} --token [HIDDEN] --url http://localhost:5000")

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        tunnel_url = None
        connection_registered = False

        print("📊 Tunnel status:")

        for line in iter(process.stdout.readline, ''):
            line = line.strip()
            if not line:
                continue

            print(f"  {line}")

            # Look for key status messages
            if "registered tunnel connection" in line.lower():
                connection_registered = True
                print("  ✅ GOOD: Tunnel connection registered!")

            elif "tunnel" in line.lower() and "ready" in line.lower():
                print("  ✅ GOOD: Tunnel is ready!")

            elif "failed" in line.lower() or "error" in line.lower():
                print(f"  ❌ BAD: {line}")

            # Try to extract tunnel URL from status
            if "https://" in line and "cfargotunnel.com" in line:
                parts = line.split()
                for part in parts:
                    if "https://" in part and "cfargotunnel.com" in part:
                        tunnel_url = part.strip()
                        print(f"  🔗 Found URL: {tunnel_url}")
                        break

        return tunnel_url, connection_registered

    except KeyboardInterrupt:
        print("\n🛑 Tunnel stopped")
        process.terminate()
        return None, False
    except Exception as e:
        print(f"❌ Tunnel error: {e}")
        return None, False


def test_tunnel_url(url, max_wait_minutes=3):
    """Test tunnel URL with patience"""
    print(f"\n🌐 Testing tunnel URL: {url}")
    print(f"⏰ Will wait up to {max_wait_minutes} minutes...")

    max_attempts = max_wait_minutes * 2  # Check every 30 seconds

    for attempt in range(max_attempts):
        try:
            print(f"🧪 Attempt {attempt + 1}/{max_attempts}: Testing {url}")

            response = requests.get(url, timeout=45)  # Long timeout

            if response.status_code == 200:
                print(f"🎉 SUCCESS! Tunnel is working!")
                print(f"📊 Status: {response.status_code}")
                print(f"📏 Response size: {len(response.text)} bytes")
                return True
            else:
                print(f"⚠️ Got status {response.status_code}")

        except requests.exceptions.Timeout:
            print(f"⏰ Request timed out (tunnel might be slow)")
        except requests.exceptions.ConnectionError:
            print(f"❌ Connection failed (tunnel not ready yet)")
        except Exception as e:
            print(f"❌ Error: {str(e)[:100]}...")

        if attempt < max_attempts - 1:
            print("⏳ Waiting 30 seconds before retry...")
            time.sleep(30)

    print(f"❌ Tunnel URL failed after {max_wait_minutes} minutes")
    return False


def manual_test_workflow():
    """Manual test workflow"""
    print("📋 MANUAL TEST WORKFLOW")
    print("=" * 40)

    print("\n1️⃣ STEP 1: Test Local Flask")
    if not test_local_connection():
        print("❌ Flask not working locally!")
        print("💡 Fix: Start Flask first with:")
        print("   python fix_encoding_issue.py")
        return False

    print("\n2️⃣ STEP 2: Get Tunnel Token")
    print("🔗 Get token from: https://dash.cloudflare.com")
    print("   → Zero Trust → Access → Tunnels → sixgents-website → Configure")
    token = input("\n🔑 Paste tunnel token: ").strip()

    if not token:
        print("❌ No token provided")
        return False

    print(f"✅ Token received ({len(token)} chars)")

    print("\n3️⃣ STEP 3: Start Tunnel")
    print("🚀 Starting tunnel... (this may take 1-2 minutes)")

    # Start tunnel in background
    def run_tunnel():
        start_tunnel_simple(token)

    tunnel_thread = threading.Thread(target=run_tunnel, daemon=True)
    tunnel_thread.start()

    # Wait for tunnel to start
    time.sleep(10)

    print("\n4️⃣ STEP 4: Test Public URL")
    tunnel_url = "https://sixgents-website.cfargotunnel.com"

    success = test_tunnel_url(tunnel_url, max_wait_minutes=3)

    if success:
        print(f"\n🎉 COMPLETE SUCCESS!")
        print(f"🔗 Public URL working: {tunnel_url}")
        print(f"💡 Share this URL with your Discord server!")
    else:
        print(f"\n❌ Tunnel test failed")
        print(f"🤔 Possible issues:")
        print(f"   • Wrong token")
        print(f"   • Cloudflare service issues")
        print(f"   • Network/firewall blocking")
        print(f"   • Need to wait longer (tunnels can be slow)")

    return success


def quick_test():
    """Quick test with simple commands"""
    print("⚡ QUICK TEST")
    print("=" * 20)

    # Test local first
    if not test_local_connection():
        print("❌ Start Flask first!")
        return

    # Get token
    token = input("🔑 Tunnel token: ").strip()
    if not token:
        return

    print("🚀 Starting tunnel (press Ctrl+C to stop)...")
    print("🌐 Your URL: https://sixgents-website.cfargotunnel.com")
    print("⏳ Wait 1-2 minutes, then test the URL")

    # Start tunnel and keep it running
    start_tunnel_simple(token)


if __name__ == '__main__':
    print("🔧 Fixed Tunnel Debugger")
    print("=" * 30)

    choice = input("Choose:\n1. Manual test workflow\n2. Quick test\nChoice (1-2): ").strip()

    if choice == '1':
        manual_test_workflow()
    elif choice == '2':
        quick_test()
    else:
        print("❌ Invalid choice")