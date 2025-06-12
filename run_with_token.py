#!/usr/bin/env python3
"""
Run tunnel using token from Cloudflare dashboard
"""

import subprocess
import threading
import time
import os
from multiprocessing import Process


def start_website():
    """Start the Flask website"""
    print("🌐 Starting website...")
    os.system('python leaderboard_app.py')


def start_bot():
    """Start Discord bot"""
    print("🤖 Starting Discord bot...")
    os.system('python main.py')


def start_tunnel_with_token(token):
    """Start tunnel using token"""
    print("🌩️ Starting tunnel with token...")
    time.sleep(3)  # Wait for website

    try:
        # Run tunnel with token
        subprocess.run(['cloudflared', 'tunnel', 'run', '--token', token])
    except KeyboardInterrupt:
        print("\n🛑 Tunnel stopped")
    except Exception as e:
        print(f"❌ Tunnel error: {e}")


def get_token_from_user():
    """Get token from user input"""
    print("🎫 TUNNEL TOKEN SETUP")
    print("=" * 30)
    print("1. Go to: https://dash.cloudflare.com")
    print("2. Navigate to: Zero Trust → Access → Tunnels")
    print("3. Find your 'sixgents-website' tunnel")
    print("4. Click the 3 dots → Configure")
    print("5. Look for 'Install and run a connector'")
    print("6. Copy the long token from the command")
    print("")
    print("Example command:")
    print("cloudflared.exe service install eyJhIjoiYWJjMTIz...")
    print("                              ^^^^^^^^^^^^^ Copy this part")
    print("")

    token = input("🔑 Paste your tunnel token here: ").strip()

    if not token:
        print("❌ No token provided")
        return None

    if len(token) < 50:
        print("⚠️ Token seems too short - make sure you copied the full token")

    return token


if __name__ == '__main__':
    print("🎫 Tunnel Token Runner")
    print("=" * 25)

    # Get token from user
    token = get_token_from_user()

    if not token:
        print("❌ Cannot run without token")
        exit(1)

    print(f"\n✅ Token received (length: {len(token)} characters)")

    # Ask what to start
    print("\nWhat do you want to start?")
    print("1. Just tunnel (test)")
    print("2. Website + tunnel")
    print("3. Everything (bot + website + tunnel)")

    choice = input("Enter choice (1-3): ").strip()

    if choice == '1':
        print("🧪 Starting tunnel only...")
        print("⚠️ Make sure your website is running on port 5000!")
        input("Press Enter when ready...")
        start_tunnel_with_token(token)

    elif choice == '2':
        print("🌐 Starting website + tunnel...")

        # Start website
        website_process = Process(target=start_website)
        website_process.start()

        # Start tunnel
        try:
            start_tunnel_with_token(token)
        except KeyboardInterrupt:
            print("\n🛑 Stopping...")
        finally:
            website_process.terminate()

    elif choice == '3':
        print("🚀 Starting everything...")

        # Start bot
        bot_process = Process(target=start_bot)
        bot_process.start()

        # Start website
        website_process = Process(target=start_website)
        website_process.start()

        # Start tunnel
        try:
            start_tunnel_with_token(token)
        except KeyboardInterrupt:
            print("\n🛑 Stopping everything...")
        finally:
            bot_process.terminate()
            website_process.terminate()
            print("✅ All stopped")
    else:
        print("❌ Invalid choice")