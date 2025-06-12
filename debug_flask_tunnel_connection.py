#!/usr/bin/env python3
"""
Debug the connection between Flask and the tunnel
"""

import requests
import time
import subprocess
import threading
from flask import Flask


def test_flask_directly():
    """Test Flask with direct requests"""
    print("🧪 Testing Flask directly...")

    tests = [
        "http://localhost:5000",
        "http://127.0.0.1:5000",
        "http://0.0.0.0:5000"
    ]

    for url in tests:
        try:
            print(f"  Testing {url}...")
            response = requests.get(url, timeout=10)
            print(f"    ✅ {url} - Status: {response.status_code}")
            print(f"    📏 Content length: {len(response.text)} bytes")
            if response.status_code == 200:
                return True
        except Exception as e:
            print(f"    ❌ {url} - Error: {str(e)[:50]}...")

    return False


def start_simple_test_server():
    """Start a super simple test server"""
    print("🌐 Starting simple test server...")

    app = Flask(__name__)

    @app.route('/')
    def home():
        return f"""
        <h1>🎉 TUNNEL TEST SUCCESS!</h1>
        <p>If you see this, the tunnel is working!</p>
        <p>Time: {time.time()}</p>
        <p>This is a simple test server to verify tunnel connectivity.</p>
        """

    @app.route('/health')
    def health():
        return "OK"

    @app.route('/test')
    def test():
        return {"status": "working", "message": "Simple server operational"}

    try:
        print("📍 Simple server starting on http://localhost:5000")
        app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
    except Exception as e:
        print(f"❌ Simple server error: {e}")


def check_flask_binding():
    """Check how Flask is binding to ports"""
    print("🔍 Checking Flask port binding...")

    try:
        # Check what's listening on port 5000
        result = subprocess.run(['netstat', '-an'], capture_output=True, text=True)
        lines = result.stdout.split('\n')

        port_5000_lines = [line for line in lines if ':5000' in line]

        if port_5000_lines:
            print("📊 Port 5000 status:")
            for line in port_5000_lines:
                print(f"    {line.strip()}")
        else:
            print("❌ Nothing listening on port 5000!")

    except Exception as e:
        print(f"⚠️ Could not check port status: {e}")


def test_tunnel_to_flask():
    """Test if tunnel can reach Flask"""
    print("🔗 Testing tunnel → Flask connection...")

    # Check if Flask is responding locally
    flask_working = False
    try:
        response = requests.get('http://localhost:5000', timeout=5)
        if response.status_code == 200:
            flask_working = True
            print("✅ Flask is responding locally")
        else:
            print(f"⚠️ Flask returned status {response.status_code}")
    except Exception as e:
        print(f"❌ Flask not responding locally: {e}")

    if not flask_working:
        print("💡 Flask must work locally before tunnel can work!")
        return False

    # Test the tunnel URL
    tunnel_url = "https://sixgents-website.cfargotunnel.com"
    print(f"🌐 Testing tunnel URL: {tunnel_url}")

    try:
        # Try with a longer timeout
        response = requests.get(tunnel_url, timeout=60)
        print(f"🎉 SUCCESS! Tunnel is working!")
        print(f"📊 Status: {response.status_code}")
        print(f"📏 Content: {len(response.text)} bytes")
        return True
    except requests.exceptions.Timeout:
        print("⏰ Tunnel URL timed out (still might be starting)")
    except requests.exceptions.ConnectionError as e:
        print(f"❌ Tunnel connection error: {e}")
    except Exception as e:
        print(f"❌ Tunnel error: {e}")

    return False


def comprehensive_debug():
    """Run comprehensive debugging"""
    print("🔧 COMPREHENSIVE FLASK ↔ TUNNEL DEBUG")
    print("=" * 50)

    print("\n1️⃣ CHECKING PORT BINDING")
    check_flask_binding()

    print("\n2️⃣ TESTING FLASK DIRECTLY")
    flask_working = test_flask_directly()

    if not flask_working:
        print("\n❌ PROBLEM: Flask is not responding!")
        print("💡 Solutions:")
        print("   • Make sure Flask is running")
        print("   • Check if it's binding to 0.0.0.0:5000")
        print("   • Try restarting Flask")
        return False

    print("\n3️⃣ TESTING TUNNEL CONNECTION")
    tunnel_working = test_tunnel_to_flask()

    if tunnel_working:
        print("\n🎉 SUCCESS! Everything is working!")
    else:
        print("\n❌ TUNNEL ISSUE: Flask works but tunnel doesn't")
        print("💡 Possible solutions:")
        print("   • Wait longer (tunnels can take 5+ minutes)")
        print("   • Restart the tunnel")
        print("   • Check Cloudflare dashboard for errors")
        print("   • Try a simple test server")

    return tunnel_working


def simple_server_test():
    """Test with a super simple server"""
    print("🧪 SIMPLE SERVER TEST")
    print("=" * 30)
    print("This will start a minimal test server to verify tunnel connectivity")
    print("🚀 Starting in 3 seconds...")
    time.sleep(3)

    # Start simple server
    start_simple_test_server()


if __name__ == '__main__':
    print("🔧 Flask ↔ Tunnel Connection Debugger")
    print("=" * 45)

    choice = input("""Choose debug method:
1. Comprehensive debug (test everything)
2. Simple server test (minimal Flask app)
3. Quick connection test

Enter choice (1-3): """).strip()

    if choice == '1':
        comprehensive_debug()
    elif choice == '2':
        simple_server_test()
    elif choice == '3':
        print("⚡ Quick test...")
        test_tunnel_to_flask()
    else:
        print("❌ Invalid choice")