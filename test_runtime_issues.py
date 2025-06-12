#!/usr/bin/env python3
"""
Test for runtime issues that cause Flask to hang
"""

import sys
import time
import threading
import requests
from multiprocessing import Process


def start_leaderboard_with_timeout():
    """Start leaderboard app with timeout monitoring"""

    def run_app():
        try:
            print("🚀 Starting leaderboard_app...")
            from leaderboard_app import app
            app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)
        except Exception as e:
            print(f"❌ Error in leaderboard app: {e}")
            import traceback
            traceback.print_exc()

    # Start Flask in a separate process
    flask_process = Process(target=run_app)
    flask_process.start()

    # Monitor startup
    print("⏰ Monitoring Flask startup...")

    max_wait = 30  # 30 seconds timeout
    check_interval = 2  # Check every 2 seconds

    for i in range(0, max_wait, check_interval):
        time.sleep(check_interval)

        try:
            # Try to connect
            response = requests.get('http://localhost:5000', timeout=5)
            if response.status_code == 200:
                print(f"✅ Flask responded after {i + check_interval} seconds!")
                print(f"📄 Status: {response.status_code}")
                print(f"🎉 SUCCESS: leaderboard_app is working!")

                # Test a few more endpoints
                try:
                    test_response = requests.get('http://localhost:5000/api/leaderboard/global', timeout=5)
                    print(f"📊 API test: {test_response.status_code}")
                except:
                    print("⚠️ API endpoint test failed (might be normal)")

                flask_process.terminate()
                return True

        except requests.exceptions.ConnectionError:
            print(f"⏳ {i + check_interval}s: Still waiting for Flask to respond...")
        except requests.exceptions.Timeout:
            print(f"⏳ {i + check_interval}s: Flask responding but slowly...")
        except Exception as e:
            print(f"⏳ {i + check_interval}s: Error testing: {e}")

    print(f"❌ TIMEOUT: Flask didn't respond after {max_wait} seconds")
    print("💡 This suggests Flask is hanging during startup")

    flask_process.terminate()
    return False


def check_for_blocking_operations():
    """Check for operations that might block Flask startup"""

    print("\n🔍 Checking for potential blocking operations...")

    # Check 1: Database connection
    print("1️⃣ Testing database connection speed...")

    try:
        import os
        from dotenv import load_dotenv
        from pymongo import MongoClient
        from pymongo.server_api import ServerApi
        import time

        load_dotenv()
        mongo_uri = os.getenv('MONGO_URI')

        if mongo_uri:
            start_time = time.time()
            client = MongoClient(mongo_uri, server_api=ServerApi('1'), serverSelectionTimeoutMS=10000)
            client.admin.command('ping')
            db_time = time.time() - start_time

            print(f"   ✅ Database connected in {db_time:.2f} seconds")

            if db_time > 5:
                print("   ⚠️ Database connection is slow - this might cause startup delays")
        else:
            print("   ⚠️ No MONGO_URI found")

    except Exception as e:
        print(f"   ❌ Database connection error: {e}")

    # Check 2: Discord OAuth setup
    print("2️⃣ Testing Discord OAuth imports...")

    try:
        from discord_oauth import DiscordOAuth
        print("   ✅ Discord OAuth imports OK")
    except Exception as e:
        print(f"   ❌ Discord OAuth error: {e}")

    # Check 3: Bot keepalive (should be removed)
    print("3️⃣ Checking for bot keepalive...")

    try:
        with open('leaderboard_app.py', 'r') as f:
            content = f.read()

        if 'start_bot_keepalive' in content:
            print("   ⚠️ Found bot keepalive code - this might cause hangs")
        else:
            print("   ✅ No bot keepalive found")

    except Exception as e:
        print(f"   ❌ Error reading leaderboard_app.py: {e}")


def test_simple_vs_complex():
    """Compare simple website vs complex leaderboard"""

    print("\n🆚 Testing Simple vs Complex...")

    # Test 1: Simple website
    print("1️⃣ Testing simple website...")

    def run_simple():
        from simple_website import app
        app.run(host='127.0.0.1', port=5001, debug=False, use_reloader=False)

    simple_process = Process(target=run_simple)
    simple_process.start()

    time.sleep(3)

    try:
        response = requests.get('http://localhost:5001', timeout=5)
        if response.status_code == 200:
            print("   ✅ Simple website works perfectly")
        else:
            print(f"   ⚠️ Simple website returned: {response.status_code}")
    except Exception as e:
        print(f"   ❌ Simple website failed: {e}")

    simple_process.terminate()

    # Test 2: Complex leaderboard
    print("2️⃣ Testing complex leaderboard...")
    return start_leaderboard_with_timeout()


if __name__ == '__main__':
    print("🔍 Runtime Issue Detector")
    print("=" * 40)

    # Check for blocking operations
    check_for_blocking_operations()

    # Test startup timing
    print("\n" + "=" * 40)
    success = test_simple_vs_complex()

    if success:
        print("\n🎉 CONCLUSION: leaderboard_app works!")
        print("💡 The issue might be:")
        print("   - Slow database connection")
        print("   - Network timeouts")
        print("   - Tunnel connection issues")
    else:
        print("\n❌ CONCLUSION: leaderboard_app is hanging")
        print("💡 Try using simple_website.py for testing:")
        print("   python simple_website.py")