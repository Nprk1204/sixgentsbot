#!/usr/bin/env python3
"""
Test Flask routes to see what's working
"""

import requests


def test_local_routes():
    """Test various routes locally"""
    print("🧪 Testing local Flask routes...")

    routes_to_test = [
        '/',
        '/health',
        '/test',
        '/leaderboard',
        '/api/leaderboard/global',
        '/status'
    ]

    base_url = 'http://localhost:5000'

    for route in routes_to_test:
        try:
            url = f"{base_url}{route}"
            print(f"  Testing {url}...")

            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                print(f"    ✅ {route} - Working!")
                if len(response.text) < 200:
                    print(f"    📄 Content: {response.text[:100]}...")
            elif response.status_code == 404:
                print(f"    ❌ {route} - 404 Not Found")
            else:
                print(f"    ⚠️ {route} - Status: {response.status_code}")

        except Exception as e:
            print(f"    ❌ {route} - Error: {str(e)[:50]}...")

    print("\n💡 If '/' shows 404, your home route has an issue!")


if __name__ == '__main__':
    print("🔧 Flask Route Tester")
    print("=" * 25)
    print("Make sure Flask is running first!")
    print()

    test_local_routes()