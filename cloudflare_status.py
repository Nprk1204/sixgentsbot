import requests


def check_discord_ban_status():
    test_url = "https://discord.com/api/v10/gateway"

    try:
        response = requests.get(test_url, timeout=10)

        if response.status_code == 200:
            print("✅ NOT BANNED - Discord API is accessible")
            return False
        else:
            print(f"🚫 STILL BANNED - HTTP {response.status_code}")
            return True

    except Exception as e:
        if "1015" in str(e) or "rate limited" in str(e).lower():
            print("🚫 BANNED - Cloudflare 1015 error still active")
            return True
        else:
            print(f"❓ CONNECTION ERROR - {e}")
            return True


# Quick check
check_discord_ban_status()