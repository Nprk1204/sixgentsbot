#!/usr/bin/env python3
"""
Check tunnel status and try different approaches
"""

import requests
import time
import subprocess
import threading


def test_url_continuously(url, duration_minutes=10):
    """Test URL continuously for a specified duration"""
    print(f"🌐 Testing {url} for {duration_minutes} minutes...")

    end_time = time.time() + (duration_minutes * 60)
    attempt = 1

    while time.time() < end_time:
        try:
            print(f"🧪 Attempt {attempt}: {time.strftime('%H:%M:%S')}")

            response = requests.get(url, timeout=30)

            if response.status_code == 200:
                print(f"🎉 SUCCESS! {url} is working!")
                print(f"📊 Status: {response.status_code}")
                print(f"📏 Content: {len(response.text)} bytes")
                print(f"⏰ Working after {attempt} attempts")
                return True
            else:
                print(f"⚠️ Status: {response.status_code}")

        except requests.exceptions.Timeout:
            print(f"⏰ Timeout (attempt {attempt})")
        except requests.exceptions.ConnectionError:
            print(f"❌ Connection refused (attempt {attempt})")
        except Exception as e:
            print(f"❌ Error: {str(e)[:50]}... (attempt {attempt})")

        attempt += 1
        print("⏳ Waiting 30 seconds...")
        time.sleep(30)

    print(f"❌ URL still not working after {duration_minutes} minutes")
    return False


def try_quick_tunnel():
    """Try a quick tunnel instead"""
    print("🎲 Trying quick tunnel...")
    print("⚠️ Make sure Flask is running on http://localhost:5000")

    input("Press Enter when Flask is ready...")

    try:
        # Start quick tunnel
        process = subprocess.Popen(
            ['cloudflared', 'tunnel', '--url', 'http://localhost:5000'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )

        print("📊 Quick tunnel output:")
        quick_url = None

        # Look for the URL in output
        for line in iter(process.stdout.readline, ''):
            line = line.strip()
            if line:
                print(f"  {line}")

                if "https://" in line and "trycloudflare.com" in line:
                    # Extract URL
                    parts = line.split()
                    for part in parts:
                        if "https://" in part and "trycloudflare.com" in part:
                            quick_url = part.strip()
                            print(f"\n🔗 QUICK TUNNEL URL: {quick_url}")

                            # Test this URL
                            def test_quick():
                                time.sleep(10)  # Wait 10 seconds
                                test_url_continuously(quick_url, duration_minutes=3)

                            test_thread = threading.Thread(target=test_quick)
                            test_thread.start()
                            break

        return quick_url

    except KeyboardInterrupt:
        print("\n🛑 Quick tunnel stopped")
        process.terminate()
        return None


def check_flask_is_running():
    """Double-check Flask is actually running"""
    print("🔍 Checking if Flask is actually running...")

    try:
        response = requests.get('http://localhost:5000', timeout=5)
        if response.status_code == 200:
            print("✅ Flask is running and responding")
            print(f"📄 Content preview: {response.text[:100]}...")
            return True
        else:
            print(f"⚠️ Flask returned status: {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ Flask not responding: {e}")
        return False


if __name__ == '__main__':
    print("🔧 Tunnel Status Checker")
    print("=" * 30)

    # First check Flask
    if not check_flask_is_running():
        print("❌ Flask is not running! Start it first:")
        print("   python leaderboard_app.py")
        exit(1)

    print("\nChoose option:")
    print("1. Keep testing named tunnel (wait longer)")
    print("2. Try quick tunnel instead")
    print("3. Test both URLs continuously")

    choice = input("Choice (1-3): ").strip()

    if choice == '1':
        print("⏰ Testing named tunnel for 10 minutes...")
        named_url = "https://sixgents-website.cfargotunnel.com"
        test_url_continuously(named_url, duration_minutes=10)

    elif choice == '2':
        quick_url = try_quick_tunnel()

    elif choice == '3':
        print("🔄 Testing both URLs...")

        # Test named tunnel in background
        named_url = "https://sixgents-website.cfargotunnel.com"


        def test_named():
            print("📋 Testing named tunnel...")
            test_url_continuously(named_url, duration_minutes=5)


        named_thread = threading.Thread(target=test_named)
        named_thread.start()

        # Also try quick tunnel
        time.sleep(2)
        try_quick_tunnel()

    else:
        print("❌ Invalid choice")