from flask import Flask, render_template, jsonify, request, redirect, url_for, flash
from pymongo import MongoClient
from pymongo.server_api import ServerApi
import os
import datetime
import time
import requests
import json
from functools import lru_cache
from dotenv import load_dotenv
import functools

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'sixgents-rocket-league-default-key')

# Load environment variables
load_dotenv()
MONGO_URI = os.getenv('MONGO_URI')
RLTRACKER_API_KEY = os.getenv('RLTRACKER_API_KEY', '')
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', '')
DISCORD_GUILD_ID = os.getenv('DISCORD_GUILD_ID', '')


# Simple cache implementation
class SimpleCache:
    def __init__(self, default_timeout=300):
        self.cache = {}
        self.default_timeout = default_timeout

    def get(self, key):
        item = self.cache.get(key, None)
        if item is None:
            return None
        if item['expiry'] is not None and item['expiry'] <= time.time():
            del self.cache[key]
            return None
        return item['value']

    def set(self, key, value, timeout=None):
        if timeout is None:
            timeout = self.default_timeout

        expiry = None
        if timeout > 0:
            expiry = time.time() + timeout

        self.cache[key] = {
            'value': value,
            'expiry': expiry
        }
        return True


# Initialize cache
cache = SimpleCache()


# Cache decorator
def cached(timeout=5 * 60, key_prefix='view/%s'):
    def decorator(f):
        @functools.wraps(f)
        def decorated_function(*args, **kwargs):
            cache_key = key_prefix % request.path
            rv = cache.get(cache_key)
            if rv is not None:
                return rv
            rv = f(*args, **kwargs)
            cache.set(cache_key, rv, timeout=timeout)
            return rv

        return decorated_function

    return decorator


# Connect to MongoDB with error handling
try:
    client = MongoClient(MONGO_URI, server_api=ServerApi('1'))
    # Ping the database to verify connection
    client.admin.command('ping')
    print("MongoDB connection successful!")
    db = client['sixgents_db']
    players_collection = db['players']
    matches_collection = db['matches']
except Exception as e:
    print(f"MongoDB connection error: {e}")


    # Fallback data for development if MongoDB connection fails
    class FallbackDB:
        def __init__(self):
            self.players = []
            self.matches = []

        def find(self, *args, **kwargs):
            return self

        def sort(self, *args, **kwargs):
            return self

        def limit(self, limit):
            return []

        def skip(self, skip):
            return self

        def count_documents(self, *args, **kwargs):
            return 0

        def find_one(self, *args, **kwargs):
            return None

        def delete_many(self, *args, **kwargs):
            return None


    db = {'players': FallbackDB(), 'matches': FallbackDB()}
    players_collection = db['players']
    matches_collection = db['matches']


# Routes
@app.route('/')
def home():
    """Display the home page with stats and featured players"""
    # Get total player count
    player_count = players_collection.count_documents({})

    # Get total match count
    match_count = matches_collection.count_documents({})

    # Get top 5 players for featured section
    featured_players = list(players_collection.find({}, {
        "_id": 0,
        "id": 1,
        "name": 1,
        "mmr": 1,
        "wins": 1,
        "losses": 1,
        "matches": 1
    }).sort("mmr", -1).limit(5))

    # Calculate win rates for featured players
    for player in featured_players:
        matches = player.get("matches", 0)
        wins = player.get("wins", 0)
        player["win_rate"] = round((wins / matches) * 100, 2) if matches > 0 else 0

    # Get recent matches
    recent_matches = list(matches_collection.find(
        {"status": "completed"},
        {"_id": 0}
    ).sort("completed_at", -1).limit(5))

    # Format match data for display
    formatted_matches = []
    for match in recent_matches:
        if "completed_at" in match:
            date_str = match["completed_at"].strftime("%Y-%m-%d %H:%M")
        else:
            date_str = "Unknown date"

        winner = match.get("winner", 0)
        team1 = [p.get("name", "Unknown") for p in match.get("team1", [])]
        team2 = [p.get("name", "Unknown") for p in match.get("team2", [])]

        formatted_match = {
            "date": date_str,
            "team1": team1,
            "team2": team2,
            "winner": winner,
            "match_id": match.get("match_id", ""),
            "score": match.get("score", {"team1": 0, "team2": 0})
        }

        formatted_matches.append(formatted_match)

    return render_template('home.html',
                           player_count=player_count,
                           match_count=match_count,
                           featured_players=featured_players,
                           recent_matches=formatted_matches)


@app.route('/leaderboard')
@cached(timeout=60)  # Cache for 1 minute
def leaderboard():
    """Display the leaderboard page"""
    return render_template('leaderboard.html')


@app.route('/rank-check')
def rank_check():
    """Display the rank check page"""
    # Get all discord roles
    roles = ["Rank A", "Rank B", "Rank C"]

    return render_template('rank_check.html', roles=roles)


@app.route('/api/leaderboard')
@cached(timeout=60)  # Cache for 1 minute
def get_leaderboard():
    """API endpoint to get leaderboard data with pagination"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)

    # Limit per_page to reasonable values
    if per_page > 100:
        per_page = 100

    # Calculate skip value for pagination
    skip = (page - 1) * per_page

    # Get total count for pagination info
    total_players = players_collection.count_documents({})

    # Get players with pagination
    top_players = list(players_collection.find({}, {
        "_id": 0,
        "id": 1,
        "name": 1,
        "mmr": 1,
        "wins": 1,
        "losses": 1,
        "matches": 1,
        "last_updated": 1
    }).sort("mmr", -1).skip(skip).limit(per_page))

    # Calculate additional stats
    for player in top_players:
        matches = player.get("matches", 0)
        wins = player.get("wins", 0)

        # Calculate win rate
        player["win_rate"] = round((wins / matches) * 100, 2) if matches > 0 else 0

        # Format the last_updated date
        if "last_updated" in player and player["last_updated"]:
            player["last_match"] = player["last_updated"].strftime("%Y-%m-%d")
        else:
            player["last_match"] = "Unknown"

        # Remove the datetime object before jsonifying
        if "last_updated" in player:
            del player["last_updated"]

    # Return with pagination info
    return jsonify({
        "players": top_players,
        "pagination": {
            "total": total_players,
            "page": page,
            "per_page": per_page,
            "pages": (total_players + per_page - 1) // per_page
        }
    })


@app.route('/api/player/<player_id>')
def get_player(player_id):
    """API endpoint to get player data"""
    try:
        player = players_collection.find_one({"id": player_id}, {"_id": 0})

        if not player:
            return jsonify({"error": "Player not found"}), 404

        # Calculate additional stats
        matches = player.get("matches", 0)
        wins = player.get("wins", 0)

        player["win_rate"] = round((wins / matches) * 100, 2) if matches > 0 else 0

        # Get recent matches for this player
        recent_matches = list(matches_collection.find(
            {"$or": [
                {"team1.id": player_id},
                {"team2.id": player_id}
            ], "status": "completed"},
            {"_id": 0}
        ).sort("completed_at", -1).limit(10))

        # Format match data
        for match in recent_matches:
            # Determine if the player won or lost
            player_in_team1 = False
            for p in match.get("team1", []):
                if p.get("id") == player_id:
                    player_in_team1 = True
                    break

            winner = match.get("winner")

            if (player_in_team1 and winner == 1) or (not player_in_team1 and winner == 2):
                match["player_result"] = "Win"
            else:
                match["player_result"] = "Loss"

            # Format date
            if "completed_at" in match:
                match["date"] = match["completed_at"].strftime("%Y-%m-%d")
                del match["completed_at"]

        # Add match history
        player["recent_matches"] = recent_matches

        return jsonify(player)

    except Exception as e:
        print(f"Error getting player {player_id}: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500


@app.route('/api/search')
def search_players():
    """Search for players by name"""
    query = request.args.get('q', '')
    if not query or len(query) < 2:
        return jsonify({"error": "Search query must be at least 2 characters"}), 400

    # Search for players with name containing the query (case insensitive)
    results = list(players_collection.find(
        {"name": {"$regex": query, "$options": "i"}},
        {"_id": 0, "id": 1, "name": 1, "mmr": 1}
    ).sort("mmr", -1).limit(10))

    return jsonify(results)


@lru_cache(maxsize=100)
def get_cached_rank(platform, username, cache_time=300):
    """Get Rocket League rank with caching to avoid API rate limits"""
    # Generate a cache key that includes timestamp rounded to cache_time
    cache_key = f"{platform}:{username}:{int(time.time() / cache_time)}"
    return fetch_rank_from_api(platform, username)


def fetch_rank_from_api(platform, username):
    """Fetch rank data from RLTracker or similar API"""
    try:
        # Example for RLTracker Network API
        api_url = f"https://api.tracker.gg/api/v2/rocket-league/standard/profile/{platform}/{username}"

        headers = {
            "TRN-Api-Key": RLTRACKER_API_KEY,
            "Accept": "application/json",
            "Accept-Encoding": "gzip"
        }

        print(f"Fetching rank data for {username} on {platform} with API key: {RLTRACKER_API_KEY[:5]}...")

        response = requests.get(api_url, headers=headers)

        if response.status_code == 200:
            data = response.json()

            # For debugging
            print("API Response received successfully")

            # Parse the response to extract 3v3 competitive rank
            rank_data = extract_3v3_rank(data)

            return {
                "success": True,
                "username": username,
                "platform": platform,
                "rank": rank_data["rank"],
                "tier": get_tier_from_rank(rank_data["rank"]),
                "mmr": get_mmr_from_rank(rank_data["rank"]),
                "profileUrl": f"https://rocketleague.tracker.network/rocket-league/profile/{platform}/{username}",
                "timestamp": time.time()
            }
        else:
            print(f"API Error: Status code {response.status_code}")
            print(f"Response body: {response.text[:200]}")
            return {
                "success": False,
                "error": f"API returned status code {response.status_code}",
                "message": "Could not retrieve rank information"
            }

    except Exception as e:
        print(f"Exception in fetch_rank_from_api: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "message": "An error occurred while fetching rank data"
        }


def extract_3v3_rank(api_data):
    """Extract 3v3 rank from API response data"""
    try:
        # Check if data exists
        if "data" not in api_data:
            print("No data found in API response")
            return {"rank": "Unknown", "tierGroup": "Unknown"}

        segments = api_data.get("data", {}).get("segments", [])

        # For debugging
        playlist_types = [segment.get("type") for segment in segments]
        print(f"Found segment types: {playlist_types}")

        # Try to find the ranked standard 3v3 playlist
        for segment in segments:
            if segment.get("type") == "playlist" and segment.get("metadata", {}).get("name") == "Ranked Standard 3v3":
                tier = segment.get("stats", {}).get("tier", {}).get("metadata", {}).get("name", "Unranked")
                division = segment.get("stats", {}).get("division", {}).get("metadata", {}).get("name", "I")

                return {
                    "rank": f"{tier} {division}",
                    "tierGroup": tier.split()[0] if tier != "Unranked" else "Unranked"
                }

        # If we didn't find Ranked Standard 3v3, try looking for other playlists
        for segment in segments:
            if segment.get("type") == "playlist" and "Ranked" in segment.get("metadata", {}).get("name", ""):
                tier = segment.get("stats", {}).get("tier", {}).get("metadata", {}).get("name", "Unranked")
                division = segment.get("stats", {}).get("division", {}).get("metadata", {}).get("name", "I")

                print(f"Found alternate playlist: {segment.get('metadata', {}).get('name')}")

                return {
                    "rank": f"{tier} {division}",
                    "tierGroup": tier.split()[0] if tier != "Unranked" else "Unranked"
                }

        # If no ranked playlists found at all, use a fallback
        return {"rank": "Unranked", "tierGroup": "Unranked"}

    except Exception as e:
        print(f"Error extracting rank: {str(e)}")
        return {"rank": "Unknown", "tierGroup": "Unknown"}


def get_tier_from_rank(rank):
    """Determine 6 Mans tier from Rocket League rank"""
    rank_lower = rank.lower()

    if "grand champion" in rank_lower or "supersonic" in rank_lower:
        return "Rank A"
    elif "champion" in rank_lower:
        return "Rank B"
    else:
        return "Rank C"  # Default tier for Diamond and below


def get_mmr_from_rank(rank):
    """Determine starting MMR from Rocket League rank"""
    rank_lower = rank.lower()

    if "grand champion" in rank_lower or "supersonic" in rank_lower:
        return 1500
    elif "champion" in rank_lower:
        return 1300
    else:
        return 1000  # Default MMR for Diamond and below


def assign_discord_role(username, role_name):
    """Assign a Discord role to a user by username"""
    print("\n=== DISCORD ROLE ASSIGNMENT DEBUG ===")
    print(f"Starting role assignment for user: {username} to role: {role_name}")

    # Print environment variable status for debugging
    print(f"Bot token exists: {'Yes' if DISCORD_TOKEN else 'No'}")
    print(f"Guild ID exists: {'Yes' if DISCORD_GUILD_ID else 'No'}")
    print(f"Guild ID value: {DISCORD_GUILD_ID}")

    # Validate required inputs
    if not username:
        print("Username not provided")
        return {"success": False, "message": "Username is required for role assignment"}

    if not role_name:
        print("Role name not provided")
        return {"success": False, "message": "Role name is required for role assignment"}

    if not DISCORD_TOKEN:
        print("Discord token not found in environment variables")
        return {"success": False, "message": "Discord token not configured"}

    if not DISCORD_GUILD_ID:
        print("Discord guild ID not found in environment variables")
        return {"success": False, "message": "Discord guild ID not configured"}

    try:
        # Get all guild members
        api_url_members = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/members?limit=1000"

        headers = {
            "Authorization": f"Bot {DISCORD_TOKEN}",
            "Content-Type": "application/json"
        }

        print(f"Making API request to get server members...")
        response = requests.get(api_url_members, headers=headers)

        print(f"Response status code: {response.status_code}")
        if response.status_code != 200:
            print(f"API Error: {response.text[:200]}")
            return {"success": False, "message": f"Discord API error: {response.status_code}"}

        members = response.json()
        print(f"Successfully retrieved {len(members)} members from server")

        # Find user by username
        user_id = None
        matched_name = None
        search_name = username.lower().strip()

        print(f"Looking for user matching '{search_name}'")
        for member in members:
            member_user = member.get('user', {})
            member_username = (member_user.get('username') or '').lower().strip()
            member_global_name = (member_user.get('global_name') or '').lower().strip()
            member_nickname = (member.get('nick') or '').lower().strip()

            print(
                f"Checking member: username={member_username}, global_name={member_global_name}, nickname={member_nickname}")

            if (search_name == member_username or
                    search_name == member_global_name or
                    search_name == member_nickname or
                    search_name in member_username or
                    search_name in member_global_name or
                    search_name in member_nickname):
                user_id = member_user.get('id')
                matched_name = member_user.get('username') or member_global_name
                print(f"✓ Found matching user: {matched_name} (ID: {user_id})")
                break

        if not user_id:
            print("✗ No matching user found")
            return {"success": False, "message": f"Could not find Discord user with username: {username}"}

        # Get all server roles
        api_url_roles = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/roles"
        print("Getting server roles...")
        roles_response = requests.get(api_url_roles, headers=headers)

        print(f"Roles response status code: {roles_response.status_code}")
        if roles_response.status_code != 200:
            print(f"Roles API Error: {roles_response.text[:200]}")
            return {"success": False, "message": f"Failed to get roles: {roles_response.status_code}"}

        roles = roles_response.json()
        print(f"Successfully retrieved {len(roles)} roles")

        # Print all roles for debugging
        print("Available roles:")
        for role in roles:
            role_id = role.get('id', 'None')
            role_name_from_server = role.get('name', 'Unknown')
            role_position = role.get('position', 0)
            print(f"  '{role_name_from_server}' (ID: {role_id}, Position: {role_position})")

        # Find role ID by name (case-insensitive)
        role_id = None
        for role in roles:
            if role.get('name', '').lower() == role_name.lower():
                role_id = role.get('id')
                print(f"✓ Found matching role: {role.get('name')} with ID: {role_id}")
                break

        if not role_id:
            print(f"✗ No matching role found for '{role_name}'")
            print(f"Available roles: {', '.join([r.get('name', 'Unknown') for r in roles])}")
            return {"success": False, "message": f"Could not find Discord role with name: {role_name}"}

        # Now assign the role
        api_url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/members/{user_id}/roles/{role_id}"
        print(f"Assigning role {role_name} (ID: {role_id}) to user {matched_name} (ID: {user_id})")

        role_response = requests.put(api_url, headers=headers)

        print(f"Role assignment response status code: {role_response.status_code}")
        if role_response.status_code in [204, 200]:
            print("✓ Role assignment successful!")
            return {"success": True, "message": f"Role {role_name} assigned successfully to {matched_name}"}
        else:
            print(f"✗ Role assignment failed with status code {role_response.status_code}")
            print(f"Response body: {role_response.text[:200]}")

            if role_response.status_code == 403:
                print("This is likely a permissions issue. Check that your bot has 'Manage Roles' permission.")
                return {"success": False, "message": "Permission denied. Check bot's role hierarchy and permissions."}

            return {"success": False, "message": f"Failed to assign role: {role_response.status_code}"}

    except Exception as e:
        import traceback
        print(f"✗ Exception during role assignment: {str(e)}")
        traceback.print_exc()
        return {"success": False, "message": f"Error: {str(e)}"}
    finally:
        print("=== END ROLE ASSIGNMENT DEBUG ===\n")


@app.route('/api/rank-check', methods=['GET'])
def check_rank():
    """API endpoint to check Rocket League rank"""
    platform = request.args.get('platform', '')
    username = request.args.get('username', '')
    discord_username = request.args.get('discord_username', '')

    if not platform or not username:
        return jsonify({"success": False, "message": "Platform and username are required"}), 400

    # Add debug logging
    print(f"=== RANK CHECK DEBUG ===")
    print(f"Platform: {platform}")
    print(f"Username: {username}")
    print(f"Discord username: {discord_username}")
    print(f"API Key present: {'Yes, starts with ' + RLTRACKER_API_KEY[:5] if RLTRACKER_API_KEY else 'No'}")
    print(f"API Key length: {len(RLTRACKER_API_KEY)}")
    print(f"========================")

    # Use mock data with variety if no API key is provided or API key isn't approved yet
    if not RLTRACKER_API_KEY:
        print("WARNING: No API key provided, using varied mock data")
        mock_data = get_mock_rank_data(username, platform)

        # If Discord username was provided, attempt role assignment
        if discord_username:
            tier = mock_data.get("tier")
            role_result = assign_discord_role(discord_username, tier)
            mock_data["role_assignment"] = role_result

        return jsonify(mock_data)

    # Get rank from API (with caching)
    rank_data = get_cached_rank(platform, username)
    print(f"Rank data returned: {rank_data}")

    # If Discord username was provided, attempt to assign role
    if discord_username and rank_data.get("success", False):
        tier = rank_data.get("tier")
        role_result = assign_discord_role(discord_username, tier)
        rank_data["role_assignment"] = role_result
        print(f"Role assignment result: {role_result}")

    return jsonify(rank_data)


def get_mock_rank_data(username, platform):
    """Generate varied mock data for testing"""
    # Use username to deterministically generate different ranks
    import hashlib

    # Generate a hash based on username for consistency
    hash_value = int(hashlib.md5(username.encode()).hexdigest(), 16) % 100

    # Determine rank based on hash value
    if hash_value < 10:  # 10% chance
        rank = "Supersonic Legend I"
        tier = "Rank A"
        mmr = 1500
    elif hash_value < 20:  # 10% chance
        rank = "Grand Champion III"
        tier = "Rank A"
        mmr = 1500
    elif hash_value < 40:  # 20% chance
        rank = "Champion III"
        tier = "Rank B"
        mmr = 1300
    elif hash_value < 60:  # 20% chance
        rank = "Champion I"
        tier = "Rank B"
        mmr = 1300
    elif hash_value < 80:  # 20% chance
        rank = "Diamond III"
        tier = "Rank C"
        mmr = 1000
    else:  # 20% chance
        rank = "Platinum II"
        tier = "Rank C"
        mmr = 1000

    return {
        "success": True,
        "username": username,
        "platform": platform,
        "rank": rank,
        "tier": tier,
        "mmr": mmr,
        "profileUrl": f"https://rocketleague.tracker.network/rocket-league/profile/{platform}/{username}",
        "timestamp": time.time()
    }


@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404


@app.errorhandler(500)
def internal_server_error(e):
    return render_template('500.html'), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)