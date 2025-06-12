#!/usr/bin/env python3
"""
Debug tunnel connection issues
"""

import subprocess
import time
import requests
import threading


def test_local_flask():
    """Test if Flask is responding locally"""
    print("🧪 Testing local Flask connection...")

    max_attempts = 5
    for i in range(max_attempts):
        try:
            response = requests.get('http://localhost:5000', timeout=10)
            if response.status_code == 200:
                print(f"✅ Local Flask is working! Status: {response.status_code}")
                return True
            else:
                print(f"⚠️ Local Flask returned status: {response.status_code}")
                return False
        except requests.exceptions.ConnectionError:
            print(f"❌ Attempt {i + 1}: Local Flask not responding")
            if i < max_attempts - 1:
                time.sleep(2)
        except Exception as e:
            print(f"❌ Error testing local Flask: {e}")
            return False

    print("❌ Local Flask is not responding!")
    return False


def start_tunnel_with_debug(token):
    """Start tunnel with detailed debugging"""
    print("🌩️ Starting tunnel with debug output...")

    cmd = [
        'cloudflared', 'tunnel', 'run',
        '--token', token,
        '--loglevel', 'debug'
    ]

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )

        print("📊 Tunnel output:")
        print("-" * 40)

        # Monitor tunnel output
        for line in iter(process.stdout.readline, ''):
            line = line.strip()
            if line:
                print(f"TUNNEL: {line}")

                # Look for important status messages
                if "registered tunnel connection" in line.lower():
                    print("✅ GOOD: Tunnel connection registered!")
                elif "failed to connect" in line.lower():
                    print("❌ BAD: Tunnel failed to connect!")
                elif "connection established" in line.lower():
                    print("✅ GOOD: Connection established!")
                elif "error" in line.lower() and "http" in line.lower():
                    print("❌ BAD: HTTP error detected!")

    except KeyboardInterrupt:
        print("\n🛑 Tunnel stopped by user")
        process.terminate()
    except Exception as e:
        print(f"❌ Tunnel error: {e}")


def test_tunnel_health(tunnel_url):
    """Test if tunnel URL is responding"""
    print(f"🌐 Testing tunnel URL: {tunnel_url}")

    print("⏳ Waiting 30 seconds for tunnel to stabilize...")
    time.sleep(30)

    max_attempts = 5
    for i in range(max_attempts):
        try:
            print(f"🧪 Attempt {i + 1}: Testing {tunnel_url}")
            response = requests.get(tunnel_url, timeout=30)

            if response.status_code == 200:
                print(f"✅ SUCCESS! Tunnel is working!")
                print(f"📄 Response length: {len(response.text)} bytes")
                return True
            else:
                print(f"⚠️ Tunnel returned status: {response.status_code}")

        except requests.exceptions.Timeout:
            print(f"⏰ Attempt {i + 1}: Timeout (tunnel might be slow)")
        except requests.exceptions.ConnectionError as e:
            print(f"❌ Attempt {i + 1}: Connection error - {str(e)[:100]}...")
        except Exception as e:
            print(f"❌ Attempt {i + 1}: Error - {str(e)[:100]}...")

        if i < max_attempts - 1:
            print("⏳ Waiting 10 seconds before retry...")
            time.sleep(10)

    print("❌ Tunnel URL is not responding!")
    return False


def comprehensive_test():
    """Run comprehensive tunnel test"""
    print("🔍 COMPREHENSIVE TUNNEL TEST")
    print("=" * 50)

    # Step 1: Test local Flask
    print("\n1️⃣ TESTING LOCAL FLASK")
    if not test_local_flask():
        print("❌ STOP: Fix local Flask first!")
        print("💡 Run: python fix_encoding_issue.py")
        return False

    # Step 2: Get token
    print("\n2️⃣ GETTING TUNNEL TOKEN")
    token = input("🔑 Paste your tunnel token: ").strip()

    if not token:
        print("❌ No token provided")
        return False

    # Step 3: Start tunnel in background
    print("\n3️⃣ STARTING TUNNEL")

    def run_tunnel():
        start_tunnel_with_debug(token)

    tunnel_thread = threading.Thread(target=run_tunnel, daemon=True)
    tunnel_thread.start()

    # Step 4: Test tunnel URL
    print("\n4️⃣ TESTING TUNNEL URL")
    tunnel_url = "https://sixgents-website.cfargotunnel.com"

    success = test_tunnel_health(tunnel_url)

    if success:
        print("\n🎉 SUCCESS! Everything is working!")
    else:
        print("\n❌ FAILURE! Tunnel is not responding")
        print("💡 Possible issues:")
        print("   - Token is wrong")
        print("   - Tunnel configuration issue")
        print("   - Network/firewall problem")
        print("   - Cloudflare service issue")

    return success


if __name__ == '__main__':
    print("🔧 Tunnel Connection Debugger")
    print("=" * 35)

    print("📋 This will:")
    print("1. Test local Flask server")
    print("2. Start tunnel with debug output")
    print("3. Test public URL")
    print("4. Show detailed error messages")

    input("\nPress Enter to start comprehensive test...")
    comprehensive_test()