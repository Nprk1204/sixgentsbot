#!/usr/bin/env python3
"""
Debug Flask startup issues
"""

import sys
import traceback


def test_basic_imports():
    """Test basic imports first"""
    print("🧪 Testing basic imports...")

    try:
        print("  Testing Flask...")
        from flask import Flask
        print("  ✅ Flask OK")

        print("  Testing pymongo...")
        from pymongo import MongoClient
        print("  ✅ PyMongo OK")

        print("  Testing dotenv...")
        from dotenv import load_dotenv
        print("  ✅ python-dotenv OK")

        print("  Testing requests...")
        import requests
        print("  ✅ requests OK")

        return True

    except ImportError as e:
        print(f"  ❌ Import error: {e}")
        return False


def test_env_file():
    """Test .env file loading"""
    print("\n🔍 Testing .env file...")

    import os
    from dotenv import load_dotenv

    if not os.path.exists('.env'):
        print("  ❌ .env file not found!")
        return False

    print("  ✅ .env file exists")

    load_dotenv()

    # Check critical variables
    mongo_uri = os.getenv('MONGO_URI')
    if mongo_uri:
        print("  ✅ MONGO_URI loaded")
    else:
        print("  ❌ MONGO_URI missing")

    return True


def test_minimal_flask():
    """Test a minimal Flask app"""
    print("\n🌐 Testing minimal Flask app...")

    try:
        from flask import Flask

        app = Flask(__name__)

        @app.route('/')
        def home():
            return "<h1>🎉 Minimal Flask Works!</h1><p>If you see this, basic Flask is working.</p>"

        @app.route('/test')
        def test():
            return "Test endpoint OK"

        print("  ✅ Minimal Flask app created")
        print("  🚀 Starting on http://localhost:5000...")
        print("  🛑 Press Ctrl+C to stop")

        app.run(host='0.0.0.0', port=5000, debug=True)

    except Exception as e:
        print(f"  ❌ Flask error: {e}")
        traceback.print_exc()


def test_leaderboard_imports():
    """Test imports from leaderboard_app.py"""
    print("\n📋 Testing leaderboard_app imports...")

    try:
        print("  Testing leaderboard_app import...")

        # Try to import the main app
        from leaderboard_app import app
        print("  ✅ leaderboard_app imported successfully")

        return True

    except ImportError as e:
        print(f"  ❌ Import error in leaderboard_app: {e}")
        print("  🔍 This is likely the problem!")
        traceback.print_exc()
        return False
    except Exception as e:
        print(f"  ❌ Other error in leaderboard_app: {e}")
        traceback.print_exc()
        return False


def test_database_connection():
    """Test database connection"""
    print("\n🗄️ Testing database connection...")

    try:
        import os
        from dotenv import load_dotenv
        from pymongo import MongoClient
        from pymongo.server_api import ServerApi

        load_dotenv()
        mongo_uri = os.getenv('MONGO_URI')

        if not mongo_uri:
            print("  ❌ No MONGO_URI in environment")
            return False

        print("  🔗 Attempting connection...")
        client = MongoClient(mongo_uri, server_api=ServerApi('1'), serverSelectionTimeoutMS=5000)

        # Quick ping test
        client.admin.command('ping')
        print("  ✅ Database connection OK")

        return True

    except Exception as e:
        print(f"  ⚠️ Database connection failed: {e}")
        print("  💡 This might be OK for testing - database issues shouldn't stop Flask")
        return False


if __name__ == '__main__':
    print("🔧 Flask Startup Debugger")
    print("=" * 40)

    # Test 1: Basic imports
    if not test_basic_imports():
        print("\n❌ PROBLEM: Basic imports failing")
        print("💡 Fix: Install missing packages")
        exit(1)

    # Test 2: .env file
    test_env_file()

    # Test 3: Database (optional)
    test_database_connection()

    # Test 4: leaderboard_app imports
    if not test_leaderboard_imports():
        print("\n❌ PROBLEM: leaderboard_app.py has import issues")
        print("💡 This is likely why your website gets stuck!")
        print("\n🧪 Let's try a minimal Flask app instead...")

        choice = input("\nTry minimal Flask app? (y/n): ").lower().strip()
        if choice == 'y':
            test_minimal_flask()
    else:
        print("\n✅ All tests passed!")
        print("💡 leaderboard_app should work - there might be a different issue")

        choice = input("\nTry starting leaderboard_app? (y/n): ").lower().strip()
        if choice == 'y':
            print("🚀 Starting leaderboard_app...")
            try:
                from leaderboard_app import app

                app.run(host='0.0.0.0', port=5000, debug=True)
            except Exception as e:
                print(f"❌ Error starting leaderboard_app: {e}")
                traceback.print_exc()