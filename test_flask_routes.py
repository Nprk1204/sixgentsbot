#!/usr/bin/env python3
"""
Improved test script for Flask routes
"""

import requests
import json
from datetime import datetime


def test_local_routes():
    """Test various routes locally with better formatting"""
    print("🧪 Testing Flask Application Routes")
    print("=" * 50)

    # Basic routes
    basic_routes = [
        ('/', 'Home page'),
        ('/leaderboard', 'Leaderboard page'),
        ('/profile', 'Profile page (may redirect)'),
    ]

    # API routes
    api_routes = [
        ('/api/leaderboard/global', 'Global leaderboard API'),
        ('/api/leaderboard/rank-a', 'Rank A leaderboard API'),
        ('/api/search?q=test', 'Search API'),
        ('/api/reset-timestamp', 'Reset timestamp API'),
    ]

    # New testing/monitoring routes
    monitoring_routes = [
        ('/health', 'Health check'),
        ('/test', 'Test route'),
        ('/status', 'Status information'),
        ('/debug/environment', 'Environment debug'),
        ('/debug/routes', 'Routes debug'),
        ('/api/debug/database', 'Database debug'),
    ]

    base_url = 'http://localhost:5000'

    def test_route_group(routes, group_name):
        print(f"\n📋 {group_name}")
        print("-" * 30)

        for route, description in routes:
            try:
                url = f"{base_url}{route}"
                print(f"  Testing {route:<25} ({description})")

                response = requests.get(url, timeout=10)

                if response.status_code == 200:
                    print(f"    ✅ Status: 200 OK")

                    # Try to parse JSON for API routes
                    if route.startswith('/api') or route.startswith('/debug') or route in ['/health', '/test',
                                                                                           '/status']:
                        try:
                            data = response.json()
                            if isinstance(data, dict):
                                # Show some key info
                                if 'players' in data:
                                    print(f"    📊 Players: {len(data['players'])}")
                                if 'total' in data.get('pagination', {}):
                                    print(f"    📊 Total: {data['pagination']['total']}")
                                if 'status' in data:
                                    print(f"    📊 Status: {data['status']}")
                                if 'message' in data:
                                    print(f"    📊 Message: {data['message']}")
                        except json.JSONDecodeError:
                            print(f"    📄 HTML response ({len(response.text)} chars)")
                    else:
                        print(f"    📄 HTML response ({len(response.text)} chars)")

                elif response.status_code == 404:
                    print(f"    ❌ Status: 404 Not Found")
                elif response.status_code == 302:
                    print(f"    🔄 Status: 302 Redirect to {response.headers.get('Location', 'unknown')}")
                elif response.status_code == 500:
                    print(f"    💥 Status: 500 Server Error")
                    try:
                        error_data = response.json()
                        if 'error' in error_data:
                            print(f"    🐛 Error: {error_data['error']}")
                    except:
                        print(f"    🐛 Error: {response.text[:100]}...")
                else:
                    print(f"    ⚠️  Status: {response.status_code}")

            except requests.exceptions.ConnectError:
                print(f"    🔌 Connection Error - Is Flask running on {base_url}?")
            except requests.exceptions.Timeout:
                print(f"    ⏱️  Timeout - Server took too long to respond")
            except Exception as e:
                print(f"    ❌ Error: {str(e)[:50]}...")

    # Test all route groups
    test_route_group(basic_routes, "Basic Pages")
    test_route_group(api_routes, "API Endpoints")
    test_route_group(monitoring_routes, "Monitoring & Debug")

    print(f"\n📝 Summary")
    print("-" * 30)
    print("✅ = Working correctly")
    print("❌ = Not found (404) or error")
    print("🔄 = Redirect (may be normal)")
    print("🔌 = Flask not running")
    print("\n💡 Add the monitoring routes to your Flask app if they're returning 404!")


def test_specific_functionality():
    """Test specific functionality"""
    print(f"\n🔧 Testing Specific Functionality")
    print("=" * 50)

    base_url = 'http://localhost:5000'

    # Test if we can get database status
    try:
        response = requests.get(f"{base_url}/health", timeout=5)
        if response.status_code == 200:
            data = response.json()
            print(f"📊 Database Status: {data.get('database', 'unknown')}")
            print(f"📊 Environment Check:")
            for key, value in data.get('environment', {}).items():
                print(f"    {key}: {value}")
        else:
            print("❌ Health check endpoint not available")
    except:
        print("❌ Could not check health status")

    # Test player count
    try:
        response = requests.get(f"{base_url}/api/leaderboard/global", timeout=5)
        if response.status_code == 200:
            data = response.json()
            total_players = data.get('pagination', {}).get('total', 0)
            print(f"📊 Total Players in Database: {total_players}")
        else:
            print("❌ Could not get player count")
    except:
        print("❌ Could not test player count")


if __name__ == '__main__':
    print("🚀 Flask Application Tester")
    print(f"⏰ Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("🔧 Make sure Flask is running on http://localhost:5000")
    print()

    test_local_routes()
    test_specific_functionality()

    print(f"\n✨ Testing completed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")