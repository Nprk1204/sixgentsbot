#!/usr/bin/env python3
"""
Fix encoding issues in leaderboard_app.py
"""

import os


def check_file_encoding(filename):
    """Check and fix file encoding issues"""
    print(f"🔍 Checking encoding in {filename}...")

    # Try different encodings
    encodings = ['utf-8', 'latin-1', 'cp1252', 'iso-8859-1']

    content = None
    working_encoding = None

    for encoding in encodings:
        try:
            with open(filename, 'r', encoding=encoding) as f:
                content = f.read()
            working_encoding = encoding
            print(f"✅ File readable with {encoding} encoding")
            break
        except UnicodeDecodeError as e:
            print(f"❌ {encoding} failed: {e}")
            continue

    if not content:
        print("❌ Could not read file with any encoding!")
        return False

    # Check for problematic characters around position 7414
    problem_area_start = max(0, 7414 - 50)
    problem_area_end = min(len(content), 7414 + 50)
    problem_area = content[problem_area_start:problem_area_end]

    print(f"\n📍 Content around position 7414:")
    print(f"'{problem_area}'")

    # Look for non-ASCII characters
    non_ascii_chars = []
    for i, char in enumerate(content):
        if ord(char) > 127:
            non_ascii_chars.append((i, char, ord(char)))

    if non_ascii_chars:
        print(f"\n🚨 Found {len(non_ascii_chars)} non-ASCII characters:")
        for pos, char, code in non_ascii_chars[:10]:  # Show first 10
            print(f"   Position {pos}: '{char}' (code {code})")

        # Create a fixed version
        fixed_content = content.encode('ascii', 'ignore').decode('ascii')

        # Backup original
        backup_name = f"{filename}.backup"
        with open(backup_name, 'w', encoding=working_encoding) as f:
            f.write(content)
        print(f"📄 Backed up original to: {backup_name}")

        # Write fixed version
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(fixed_content)
        print(f"✅ Created ASCII-only version of {filename}")

        return True
    else:
        print("✅ No non-ASCII characters found")
        return True


def simple_flask_test():
    """Simple Flask test without multiprocessing"""
    print("\n🧪 Simple Flask Test...")

    try:
        # Test importing leaderboard_app
        print("📋 Testing leaderboard_app import...")
        from leaderboard_app import app
        print("✅ Import successful!")

        # Test if we can create the app
        print("🏗️ Testing app creation...")
        test_client = app.test_client()
        print("✅ Test client created!")

        # Test a simple route
        print("🌐 Testing home route...")
        with app.app_context():
            response = test_client.get('/')
            print(f"✅ Home route returned status: {response.status_code}")

            if response.status_code == 200:
                print("🎉 SUCCESS: leaderboard_app is working!")
                return True
            else:
                print(f"⚠️ Unexpected status code: {response.status_code}")
                return False

    except Exception as e:
        print(f"❌ Error testing leaderboard_app: {e}")
        import traceback
        traceback.print_exc()
        return False


def run_actual_server():
    """Run the actual Flask server"""
    print("\n🚀 Starting actual Flask server...")
    print("🌐 Visit: http://localhost:5000")
    print("🛑 Press Ctrl+C to stop")

    try:
        from leaderboard_app import app
        app.run(host='0.0.0.0', port=5000, debug=True)
    except Exception as e:
        print(f"❌ Server error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    print("🔧 Encoding & Flask Fixer")
    print("=" * 30)

    # Fix encoding issues
    if os.path.exists('leaderboard_app.py'):
        if not check_file_encoding('leaderboard_app.py'):
            print("❌ Could not fix encoding issues")
            exit(1)
    else:
        print("❌ leaderboard_app.py not found!")
        exit(1)

    # Test Flask without multiprocessing
    print("\n" + "=" * 30)
    if simple_flask_test():
        print("\n✅ Flask tests passed!")

        choice = input("\n🚀 Start the actual server? (y/n): ").lower().strip()
        if choice == 'y':
            run_actual_server()
    else:
        print("\n❌ Flask tests failed!")
        print("💡 Try using simple_website.py instead:")
        print("   python simple_website.py")