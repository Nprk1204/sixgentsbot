"""
Start everything with your named tunnel
"""

import subprocess
import threading
import time
import os
import glob
from multiprocessing import Process


def find_tunnel_config():
    """Find tunnel config file"""
    configs = glob.glob('tunnel-*.yml')
    if configs:
        return configs[0]
    return None


def start_bot():
    """Start Discord bot"""
    print("🤖 Starting Discord bot...")
    os.system('python main.py')


def start_website():
    """Start Flask website"""
    print("🌐 Starting website...")
    os.system('python leaderboard_app.py')


def start_named_tunnel(config_file):
    """Start named tunnel"""
    print("🌩️ Starting named tunnel...")
    time.sleep(3)  # Wait for website

    try:
        subprocess.run(['cloudflared', 'tunnel', '--config', config_file, 'run'])
    except KeyboardInterrupt:
        print("\n🛑 Tunnel stopped")
    except Exception as e:
        print(f"❌ Tunnel error: {e}")


if __name__ == '__main__':
    print("🚀 Starting Everything with Named Tunnel")
    print("=" * 45)

    # Find config file
    config_file = find_tunnel_config()

    if not config_file:
        print("❌ No tunnel config found!")
        print("🏗️ Run this first: python setup_named_tunnel.py")
        exit(1)

    # Extract tunnel name
    tunnel_name = config_file.replace('tunnel-', '').replace('.yml', '')
    stable_url = f"https://{tunnel_name}.cfargotunnel.com"

    print(f"✅ Found config: {config_file}")
    print(f"🔗 Your permanent URL: {stable_url}")
    print(f"🏠 Local URL: http://localhost:5000")

    # Start everything
    print("\n🎬 Starting all services...")

    # Start bot
    bot_process = Process(target=start_bot)
    bot_process.start()

    # Start website
    website_process = Process(target=start_website)
    website_process.start()

    # Start named tunnel
    print("⏳ Services starting... tunnel will connect in 3 seconds")

    try:
        start_named_tunnel(config_file)
    except KeyboardInterrupt:
        print("\n🛑 Shutting down everything...")
    finally:
        bot_process.terminate()
        website_process.terminate()
        print("✅ All services stopped")