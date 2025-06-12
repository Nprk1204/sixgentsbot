# discord_oauth.py - Fixed Discord OAuth integration
import os
import requests
from flask import session, redirect, url_for, request
from functools import wraps
import urllib.parse


class DiscordOAuth:
    def __init__(self, app, client_id, client_secret, redirect_uri):
        self.app = app
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.api_endpoint = 'https://discord.com/api/v10'

    def get_oauth_url(self):
        """Generate Discord OAuth URL"""
        params = {
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'response_type': 'code',
            'scope': 'identify guilds.members.read'
        }

        # Properly encode parameters
        query_string = urllib.parse.urlencode(params)
        return f"https://discord.com/api/oauth2/authorize?{query_string}"

    def exchange_code(self, code):
        """Exchange authorization code for access token with anti-rate-limiting headers"""

        data = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': self.redirect_uri
        }

        # ENHANCED headers to bypass Cloudflare bot detection
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache'
        }

        try:
            print(f"🔄 OAuth token exchange attempt with enhanced headers...")
            print(f"   Client ID: {self.client_id[:8]}...")
            print(f"   Redirect URI: {self.redirect_uri}")
            print(f"   Code length: {len(code) if code else 0}")

            # Add delay to space out requests
            import time
            time.sleep(3)  # 3 second delay before request

            response = requests.post(
                f"{self.api_endpoint}/oauth2/token",
                data=data,
                headers=headers,
                timeout=45,  # Longer timeout
                verify=True
            )

            print(f"📊 OAuth token exchange status: {response.status_code}")

            if response.status_code == 200:
                token_data = response.json()
                print(f"✅ Token exchange successful")
                return token_data
            elif response.status_code == 429:
                print(f"🚫 Still rate limited - implementing backoff")
                # Try to get retry-after header
                retry_after = response.headers.get('Retry-After', '60')
                print(f"   Retry after: {retry_after} seconds")
                return {"error": f"Rate limited - try again in {retry_after} seconds"}
            else:
                error_text = response.text[:500]  # Limit error text
                print(f"❌ OAuth failed: {response.status_code} - {error_text}")
                return {"error": f"OAuth failed: {response.status_code}"}

        except requests.exceptions.Timeout:
            print(f"⏰ OAuth request timed out")
            return {"error": "Request timed out - try again"}
        except requests.exceptions.ConnectionError:
            print(f"🌐 Connection error to Discord API")
            return {"error": "Cannot connect to Discord - try again later"}
        except requests.exceptions.RequestException as e:
            print(f"🚫 Request exception: {e}")
            return {"error": f"Network error: {str(e)}"}
        except Exception as e:
            print(f"💥 Unexpected error: {e}")
            return {"error": f"Unexpected error: {str(e)}"}

    def get_user_info(self, access_token):
        """Get user information from Discord API"""
        headers = {
            'Authorization': f'Bearer {access_token}'
        }

        try:
            response = requests.get(f"{self.api_endpoint}/users/@me", headers=headers, timeout=10)
            if response.status_code == 200:
                return response.json()
            else:
                print(f"Failed to get user info: {response.status_code} - {response.text}")
                return None
        except requests.exceptions.RequestException as e:
            print(f"Request exception getting user info: {e}")
            return None

    def get_guild_member(self, access_token, guild_id, user_id):
        """Get guild member information"""
        headers = {
            'Authorization': f'Bearer {access_token}'
        }

        try:
            response = requests.get(
                f"{self.api_endpoint}/guilds/{guild_id}/members/{user_id}",
                headers=headers,
                timeout=10
            )
            if response.status_code == 200:
                return response.json()
            else:
                print(f"Failed to get guild member: {response.status_code} - {response.text}")
                return None
        except requests.exceptions.RequestException as e:
            print(f"Request exception getting guild member: {e}")
            return None


def login_required(f):
    """Decorator to require Discord authentication"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'discord_user' not in session:
            return redirect(url_for('discord_login'))
        return f(*args, **kwargs)

    return decorated_function


def get_current_user():
    """Get current authenticated user from session"""
    return session.get('discord_user')