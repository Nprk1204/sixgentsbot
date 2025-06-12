"""
Set up your named tunnel from Cloudflare dashboard
"""

import subprocess
import os
import json


def authenticate_cloudflare():
    """Make sure we're logged into Cloudflare"""
    print("🔑 Checking Cloudflare authentication...")

    try:
        # Check if we're already logged in
        result = subprocess.run(['cloudflared', 'tunnel', 'list'],
                                capture_output=True, text=True, timeout=10)

        if result.returncode == 0:
            print("✅ Already authenticated with Cloudflare")
            return True
        elif "login" in result.stderr.lower():
            print("❌ Not authenticated with Cloudflare")
            return False
        else:
            print("✅ Authentication check passed")
            return True

    except subprocess.TimeoutExpired:
        print("⚠️ Authentication check timed out")
        return False
    except Exception as e:
        print(f"⚠️ Could not check authentication: {e}")
        return False


def login_to_cloudflare():
    """Log in to Cloudflare"""
    print("🔐 Logging into Cloudflare...")
    print("📱 This will open your browser - use the SAME account where you created the tunnel")

    try:
        subprocess.run(['cloudflared', 'tunnel', 'login'], timeout=60)
        print("✅ Login completed!")
        return True
    except subprocess.TimeoutExpired:
        print("⚠️ Login timed out - but might still work")
        return True
    except KeyboardInterrupt:
        print("❌ Login cancelled")
        return False
    except Exception as e:
        print(f"❌ Login error: {e}")
        return False


def list_available_tunnels():
    """Show available tunnels"""
    print("📋 Available tunnels:")

    try:
        result = subprocess.run(['cloudflared', 'tunnel', 'list'],
                                capture_output=True, text=True)

        if result.returncode == 0:
            print(result.stdout)
            return result.stdout
        else:
            print(f"❌ Error listing tunnels: {result.stderr}")
            return None

    except Exception as e:
        print(f"❌ Error: {e}")
        return None


def setup_tunnel(tunnel_name):
    """Set up the named tunnel"""
    print(f"🏗️ Setting up tunnel: {tunnel_name}")

    # Get tunnel info
    try:
        result = subprocess.run(['cloudflared', 'tunnel', 'list'],
                                capture_output=True, text=True)

        if result.returncode != 0:
            print(f"❌ Error getting tunnel list: {result.stderr}")
            return None

        # Find the tunnel ID
        tunnel_id = None
        for line in result.stdout.split('\n'):
            if tunnel_name in line and len(line.strip()) > 0:
                parts = line.split()
                if parts:
                    tunnel_id = parts[0]
                    break

        if not tunnel_id:
            print(f"❌ Tunnel '{tunnel_name}' not found!")
            print("Available tunnels:")
            print(result.stdout)
            return None

        print(f"✅ Found tunnel ID: {tunnel_id}")

        # Create config file
        config_content = f"""tunnel: {tunnel_id}
credentials-file: C:\\Users\\{os.getenv('USERNAME', 'user')}\\.cloudflared\\{tunnel_id}.json

ingress:
  - hostname: {tunnel_name}.cfargotunnel.com
    service: http://localhost:5000
  - service: http_status:404
"""

        config_filename = f'tunnel-{tunnel_name}.yml'

        with open(config_filename, 'w') as f:
            f.write(config_content)

        print(f"✅ Config created: {config_filename}")

        # Update .env file
        stable_url = f"https://{tunnel_name}.cfargotunnel.com"
        update_env_file(stable_url)

        return {
            'tunnel_name': tunnel_name,
            'tunnel_id': tunnel_id,
            'config_file': config_filename,
            'url': stable_url
        }

    except Exception as e:
        print(f"❌ Error setting up tunnel: {e}")
        return None


def update_env_file(url):
    """Update .env file with stable URL"""
    print(f"📝 Updating .env file with: {url}")

    try:
        # Read current .env
        env_lines = []
        if os.path.exists('.env'):
            with open('.env', 'r') as f:
                env_lines = f.readlines()

        # Update or add lines
        updated_public = False
        updated_redirect = False

        for i, line in enumerate(env_lines):
            if line.startswith('PUBLIC_URL='):
                env_lines[i] = f'PUBLIC_URL={url}\n'
                updated_public = True
            elif line.startswith('DISCORD_REDIRECT_URI='):
                env_lines[i] = f'DISCORD_REDIRECT_URI={url}/auth/discord/callback\n'
                updated_redirect = True

        if not updated_public:
            env_lines.append(f'PUBLIC_URL={url}\n')
        if not updated_redirect:
            env_lines.append(f'DISCORD_REDIRECT_URI={url}/auth/discord/callback\n')

        # Write back
        with open('.env', 'w') as f:
            f.writelines(env_lines)

        print("✅ .env file updated")

    except Exception as e:
        print(f"⚠️ Could not update .env file: {e}")


def test_tunnel(tunnel_info):
    """Test the tunnel"""
    print(f"🧪 Testing tunnel: {tunnel_info['url']}")
    print("🌐 Make sure your website is running first!")
    print("⏹️ Press Ctrl+C to stop\n")

    try:
        subprocess.run(['cloudflared', 'tunnel', '--config', tunnel_info['config_file'], 'run'])
    except KeyboardInterrupt:
        print("\n🛑 Tunnel stopped")
    except Exception as e:
        print(f"❌ Tunnel error: {e}")


if __name__ == '__main__':
    print("🏗️ Named Tunnel Setup")
    print("=" * 30)

    # Step 1: Check authentication
    if not authenticate_cloudflare():
        print("\n🔐 Need to login to Cloudflare first...")
        if not login_to_cloudflare():
            print("❌ Login failed")
            exit(1)

    # Step 2: List available tunnels
    print("\n📋 Checking available tunnels...")
    tunnel_list = list_available_tunnels()

    if not tunnel_list or "no tunnels" in tunnel_list.lower():
        print("❌ No tunnels found!")
        print("💡 Create one first in the Cloudflare dashboard:")
        print("   https://dash.cloudflare.com → Zero Trust → Access → Tunnels")
        exit(1)

    # Step 3: Get tunnel name
    tunnel_name = input("\n🏷️ Enter your tunnel name from the dashboard: ").strip()

    if not tunnel_name:
        print("❌ No tunnel name provided")
        exit(1)

    # Step 4: Set up the tunnel
    tunnel_info = setup_tunnel(tunnel_name)

    if not tunnel_info:
        print("❌ Failed to set up tunnel")
        exit(1)

    print(f"\n🎉 SUCCESS!")
    print(f"🔗 Your permanent URL: {tunnel_info['url']}")
    print(f"📝 Discord OAuth redirect: {tunnel_info['url']}/auth/discord/callback")
    print(f"📁 Config file: {tunnel_info['config_file']}")

    # Step 5: Test it
    test_now = input("\n🧪 Test the tunnel now? (y/n): ").lower().strip()

    if test_now == 'y':
        print("\n💡 Start your website first:")
        print("   python leaderboard_app.py")
        input("Press Enter when website is running...")
        test_tunnel(tunnel_info)
    else:
        print(f"\n💡 To test later:")
        print(f"   cloudflared tunnel --config {tunnel_info['config_file']} run")