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
import re

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', '')

# Load environment variables
load_dotenv()
MONGO_URI = os.getenv('MONGO_URI')
RLTRACKER_API_KEY = os.getenv('RLTRACKER_API_KEY', '')
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', '')
DISCORD_GUILD_ID = os.getenv('DISCORD_GUILD_ID', '')  # Provide hardcoded fallback

# Debug environment variables
print("\n=== ENVIRONMENT VARIABLES DEBUG ===")
print(f"DISCORD_TOKEN exists: {'Yes' if DISCORD_TOKEN else 'No'}")
print(f"DISCORD_TOKEN length: {len(DISCORD_TOKEN) if DISCORD_TOKEN else 0}")
print(f"DISCORD_GUILD_ID exists: {'Yes' if DISCORD_GUILD_ID else 'No'}")
print(f"DISCORD_GUILD_ID value: '{DISCORD_GUILD_ID}'")
print("===================================\n")


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
    ranks_collection = db['ranks']
    resets_collection = db['resets']  # New collection for tracking resets
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


    db = {'players': FallbackDB(), 'matches': FallbackDB(), 'ranks': FallbackDB(), 'resets': FallbackDB()}
    players_collection = db['players']
    matches_collection = db['matches']
    ranks_collection = db['ranks']
    resets_collection = db['resets']


# Routes
@app.route('/')
def home():
    """Display the home page with stats and featured players"""
    # Get total player count
    player_count = players_collection.count_documents({})

    # Get total match count
    match_count = matches_collection.count_documents({})

    # Get global match count
    global_match_count = matches_collection.count_documents({"is_global": True})

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

    # Get featured global players
    featured_global_players = list(players_collection.find({"global_matches": {"$gt": 0}}, {
        "_id": 0,
        "id": 1,
        "name": 1,
        "global_mmr": 1,
        "global_wins": 1,
        "global_losses": 1,
        "global_matches": 1
    }).sort("global_mmr", -1).limit(5))

    # Calculate win rates for featured global players
    for player in featured_global_players:
        matches = player.get("global_matches", 0)
        wins = player.get("global_wins", 0)
        player["win_rate"] = round((wins / matches) * 100, 2) if matches > 0 else 0
        player["mmr"] = player.get("global_mmr", 0)  # For consistency in template

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
        is_global = match.get("is_global", False)

        formatted_match = {
            "date": date_str,
            "team1": team1,
            "team2": team2,
            "winner": winner,
            "match_id": match.get("match_id", ""),
            "score": match.get("score", {"team1": 0, "team2": 0}),
            "is_global": is_global
        }

        formatted_matches.append(formatted_match)

    return render_template('home.html',
                           player_count=player_count,
                           match_count=match_count,
                           global_match_count=global_match_count,
                           featured_players=featured_players,
                           featured_global_players=featured_global_players,
                           recent_matches=formatted_matches)


@app.route('/leaderboard')
@cached(timeout=60)
def leaderboard():
    """Display the main leaderboard page"""
    return render_template('leaderboard.html', board_type='all')

@app.route('/leaderboard/<board_type>')
@cached(timeout=60)
def leaderboard_by_type(board_type):
    """Display the leaderboard page for a specific type"""
    return render_template('leaderboard.html', board_type=board_type)


@app.route('/rank-check')
def rank_check():
    """Display the rank check page"""
    # Get all discord roles
    roles = ["Rank A", "Rank B", "Rank C"]

    return render_template('rank_check.html', roles=roles)


@app.route('/api/leaderboard')
@cached(timeout=60)
def get_leaderboard():
    """API endpoint to get leaderboard data with pagination"""
    return get_leaderboard_by_type('all')


@app.route('/api/leaderboard/<board_type>')
@cached(timeout=60)
def get_leaderboard_by_type(board_type):
    """API endpoint to get leaderboard data with pagination for specific type"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)

    # Limit per_page to reasonable values
    if per_page > 100:
        per_page = 100

    # Define query and sort field based on board type
    query = {}
    sort_field = "mmr"
    mmr_field = "mmr"

    if board_type == "global":
        # For global leaderboard, filter players with global matches
        query = {"global_matches": {"$gt": 0}}
        sort_field = "global_mmr"
        mmr_field = "global_mmr"
    elif board_type == "rank-a":
        # For Rank A, filter players with MMR >= 1600
        query = {"mmr": {"$gte": 1600}, "matches": {"$gt": 0}}
    elif board_type == "rank-b":
        # For Rank B, filter players with MMR between 1100 and 1599
        query = {"mmr": {"$gte": 1100, "$lt": 1600}, "matches": {"$gt": 0}}
    elif board_type == "rank-c":
        # For Rank C, filter players with MMR < 1100
        query = {"mmr": {"$lt": 1100}, "matches": {"$gt": 0}}
    elif board_type != "all":
        # Invalid board type, fallback to all
        board_type = "all"

    # Get total count for pagination info
    total_players = players_collection.count_documents(query)

    # Calculate skip value for pagination
    skip = (page - 1) * per_page

    # Get players with pagination
    projection = {
        "_id": 0,
        "id": 1,
        "name": 1,
        "mmr": 1,
        "global_mmr": 1,
        "wins": 1,
        "global_wins": 1,
        "losses": 1,
        "global_losses": 1,
        "matches": 1,
        "global_matches": 1,
        "last_updated": 1
    }

    top_players = list(players_collection.find(query, projection)
                       .sort(sort_field, -1)
                       .skip(skip).limit(per_page))

    # Calculate additional stats
    for player in top_players:
        if board_type == "global":
            matches = player.get("global_matches", 0)
            wins = player.get("global_wins", 0)
            # Add this field to standardize what's displayed
            player["mmr_display"] = player.get(mmr_field, 0)
        else:
            matches = player.get("matches", 0)
            wins = player.get("wins", 0)
            player["mmr_display"] = player.get(mmr_field, 0)

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
        "board_type": board_type,
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

        # Ensure player has global MMR fields
        if "global_mmr" not in player:
            player["global_mmr"] = 300
            player["global_wins"] = 0
            player["global_losses"] = 0
            player["global_matches"] = 0

        # Calculate additional stats for ranked
        matches = player.get("matches", 0)
        wins = player.get("wins", 0)
        player["win_rate"] = round((wins / matches) * 100, 2) if matches > 0 else 0

        # Calculate global win rate
        global_matches = player.get("global_matches", 0)
        global_wins = player.get("global_wins", 0)
        player["global_win_rate"] = round((global_wins / global_matches) * 100, 2) if global_matches > 0 else 0

        # Get recent matches for this player
        recent_matches = list(matches_collection.find(
            {"$or": [
                {"team1.id": player_id},
                {"team2.id": player_id}
            ], "status": "completed"},
            {"_id": 0}
        ).sort("completed_at", -1).limit(20))  # Increased limit to get more matches

        # Format match data and include is_global flag
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

            # Add is_global flag if missing
            if "is_global" not in match:
                # For backwards compatibility, determine based on channel
                channel_id = match.get("channel_id")
                is_global = False

                # Infer from channel ID - this is a best guess
                if channel_id:
                    # Try to find channel name somehow
                    # This is a placeholder - in real implementation you'd need to determine this
                    is_global = (channel_id == "your_global_channel_id")

                match["is_global"] = is_global

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


def get_tier_from_rank(rank):
    """Determine 6 Mans tier from Rocket League rank"""
    rank_lower = rank.lower()

    if "grand champion" in rank_lower or "supersonic" in rank_lower or "gc" in rank_lower or "ssl" in rank_lower:
        return "Rank A"
    elif "champion" in rank_lower:
        return "Rank B"
    else:
        return "Rank C"  # Default tier for Diamond and below


def get_mmr_from_rank(rank):
    """Determine starting MMR from Rocket League rank"""
    rank_lower = rank.lower()

    if "grand champion" in rank_lower or "supersonic" in rank_lower or "gc" in rank_lower or "ssl" in rank_lower:
        return 1850
    elif "champion" in rank_lower:
        return 1350
    else:
        return 600  # Default MMR for Diamond and below


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


def store_rank_data(discord_username, game_username, platform, rank_data, discord_id=None):
    """Store rank check data in the database"""
    try:
        # Debug print
        print(f"Storing rank data for {discord_username} with MMR: {rank_data.get('mmr')}")
        print(f"Game username: {game_username}")

        # Create simplified rank document
        rank_document = {
            "discord_username": discord_username,
            "discord_id": discord_id,  # Store the verified Discord ID if available
            "game_username": game_username,
            "platform": platform,
            "rank": rank_data.get("rank"),
            "tier": rank_data.get("tier"),
            "mmr": rank_data.get("mmr"),  # This should be the MMR from manual input
            "global_mmr": 300,  # Initialize global MMR at 300
            "timestamp": datetime.datetime.utcnow()
        }

        # Debug print
        print(f"Rank document to store: {rank_document}")

        # Check if this user already has a rank record
        existing_rank = ranks_collection.find_one({"discord_username": discord_username})

        if existing_rank:
            # Update existing record but preserve global_mmr if it exists
            update_data = rank_document.copy()
            if "global_mmr" in existing_rank:
                del update_data["global_mmr"]

            ranks_collection.update_one(
                {"discord_username": discord_username},
                {"$set": update_data}
            )
            print(f"Updated rank record for {discord_username} with MMR: {rank_data.get('mmr')}")
        else:
            # Insert new record
            ranks_collection.insert_one(rank_document)
            print(f"Created new rank record for {discord_username} with MMR: {rank_data.get('mmr')}")

    except Exception as e:
        print(f"Error storing rank data: {str(e)}")


def assign_discord_role(username, role_name=None, role_id=None):
    """Test Discord role assignment with detailed logging"""
    print("\n===== DISCORD ROLE ASSIGNMENT DEBUG =====")
    print(f"Attempting to assign role to user: {username}")
    print(f"Role name: {role_name}")
    print(f"Role ID: {role_id}")

    if not username or not DISCORD_TOKEN or not DISCORD_GUILD_ID:
        print("❌ Missing required information:")
        print(f"- Username provided: {'Yes' if username else 'No'}")
        print(f"- Bot token provided: {'Yes' if DISCORD_TOKEN else 'No'}")
        print(f"- Guild ID provided: {'Yes' if DISCORD_GUILD_ID else 'No'}")
        return {"success": False, "message": "Missing required information for role assignment"}

    # Headers for all API requests
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        # STEP 1: Verify bot authentication
        print("\n1. Verifying bot authentication...")
        auth_url = "https://discord.com/api/v10/users/@me"
        auth_response = requests.get(auth_url, headers=headers)

        if auth_response.status_code != 200:
            print(f"❌ Authentication failed: {auth_response.status_code}")
            print(f"Response: {auth_response.text[:200]}")
            return {"success": False, "message": "Bot authentication failed"}

        bot_user = auth_response.json()
        bot_id = bot_user.get('id')
        bot_name = bot_user.get('username')
        print(f"✅ Bot authenticated as: {bot_name} (ID: {bot_id})")

        # STEP 2: Get server information
        print("\n2. Getting server information...")
        guild_url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}"
        guild_response = requests.get(guild_url, headers=headers)

        if guild_response.status_code != 200:
            print(f"❌ Failed to get server info: {guild_response.status_code}")
            print(f"Response: {guild_response.text[:200]}")
            return {"success": False, "message": "Failed to retrieve guild information"}

        guild_data = guild_response.json()
        print(f"✅ Connected to server: {guild_data.get('name')}")

        # STEP 3: Get all server members
        print("\n3. Getting server members...")
        members_url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/members?limit=1000"
        members_response = requests.get(members_url, headers=headers)

        if members_response.status_code != 200:
            print(f"❌ Failed to get members: {members_response.status_code}")
            print(f"Response: {members_response.text[:200]}")
            return {"success": False, "message": "Failed to retrieve guild members"}

        members = members_response.json()
        print(f"✅ Found {len(members)} members in the server")

        # STEP 4: Find target user
        print(f"\n4. Looking for user matching '{username}'...")
        user_id = None
        matched_name = None
        search_name = username.lower().strip()

        for member in members:
            member_user = member.get('user', {})
            member_username = (member_user.get('username') or '').lower().strip()
            member_global_name = (member_user.get('global_name') or '').lower().strip()
            member_nickname = (member.get('nick') or '').lower().strip()
            member_id = member_user.get('id')

            print(
                f"  Checking member: id={member_id}, username={member_username}, global_name={member_global_name}, nickname={member_nickname}")

            if (search_name == member_username or
                    search_name == member_global_name or
                    search_name == member_nickname or
                    search_name in member_username or
                    search_name in member_global_name or
                    search_name in member_nickname):
                user_id = member_id
                matched_name = member_user.get('username') or member_global_name
                print(f"✅ Found matching user: {matched_name} (ID: {user_id})")
                break

        if not user_id:
            print(f"❌ No matching user found for '{username}'")
            return {"success": False, "message": "Could not find user in Discord server"}

        # STEP 5: Get all server roles and bot's highest role
        print("\n5. Getting server roles...")
        roles_url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/roles"
        roles_response = requests.get(roles_url, headers=headers)

        if roles_response.status_code != 200:
            print(f"❌ Failed to get roles: {roles_response.status_code}")
            print(f"Response: {roles_response.text[:200]}")
            return {"success": False, "message": "Failed to retrieve roles"}

        roles = roles_response.json()
        print(f"✅ Found {len(roles)} roles in the server")

        # Find the bot's roles and determine highest position
        bot_member = None
        for member in members:
            member_user = member.get('user', {})
            if member_user.get('id') == bot_id:
                bot_member = member
                break

        if not bot_member:
            print(f"❌ Could not find bot in member list (ID: {bot_id})")
            return {"success": False, "message": "Bot not found in member list"}

        bot_roles = bot_member.get('roles', [])
        bot_highest_role_position = 0

        # Print all roles for debugging
        print("\nServer roles:")
        for role in roles:
            role_id_value = role.get('id')
            role_name_value = role.get('name')
            role_position = role.get('position')
            print(f"  Role: '{role_name_value}' (ID: {role_id_value}, Position: {role_position})")

            # Check if this is a bot role
            if role_id_value in bot_roles and role_position > bot_highest_role_position:
                bot_highest_role_position = role_position

        print(f"\nBot's highest role position: {bot_highest_role_position}")
        print(f"Bot roles: {bot_roles}")

        # STEP 6: Find target role (by name or ID)
        print("\n6. Finding target role...")
        target_role_id = role_id
        target_role_position = 0
        target_role_name = None

        if not target_role_id and role_name:
            # Find by name if ID not provided
            for role in roles:
                if role.get('name', '').lower() == role_name.lower():
                    target_role_id = role.get('id')
                    target_role_position = role.get('position')
                    target_role_name = role.get('name')
                    print(
                        f"✅ Found role by name: '{target_role_name}' (ID: {target_role_id}, Position: {target_role_position})")
                    break

            if not target_role_id:
                print(f"❌ No role found with name: '{role_name}'")
                return {"success": False, "message": f"Role '{role_name}' not found"}
        elif target_role_id:
            # Verify the ID exists
            role_found = False
            for role in roles:
                if role.get('id') == target_role_id:
                    target_role_position = role.get('position')
                    target_role_name = role.get('name')
                    print(
                        f"✅ Found role by ID: '{target_role_name}' (ID: {target_role_id}, Position: {target_role_position})")
                    role_found = True
                    break

            if not role_found:
                print(f"❌ No role found with ID: '{target_role_id}'")
                return {"success": False, "message": "Role ID not found"}
        else:
            print("❌ No role name or ID provided")
            return {"success": False, "message": "No role specified"}

        # STEP 7: Check role hierarchy
        print("\n7. Checking role hierarchy...")
        if target_role_position >= bot_highest_role_position:
            print(
                f"❌ Role hierarchy issue: Bot's highest role ({bot_highest_role_position}) must be higher than the role to assign ({target_role_position})")
            return {"success": False,
                    "message": "Role hierarchy issue: Bot's role must be higher than the role to assign"}

        print("✅ Bot's role position is higher than target role - hierarchy check passed")

        # STEP 8: Check if user already has the role
        print("\n8. Checking if user already has the role...")
        user_roles = []
        for member in members:
            if member.get('user', {}).get('id') == user_id:
                user_roles = member.get('roles', [])
                break

        if target_role_id in user_roles:
            print(f"User already has role '{target_role_name}'")
            return {"success": True, "message": f"User already has role '{target_role_name}'"}

        # STEP 9: Assign the role
        print("\n9. Attempting to assign role...")
        assign_url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/members/{user_id}/roles/{target_role_id}"
        assign_response = requests.put(assign_url, headers=headers)

        if assign_response.status_code in [204, 200]:
            print(f"✅ Role assignment successful! Status code: {assign_response.status_code}")
            return {"success": True, "message": f"Role '{target_role_name}' assigned successfully to {matched_name}"}
        else:
            print(f"❌ Role assignment failed: {assign_response.status_code}")
            print(f"Response: {assign_response.text[:500]}")

            if assign_response.status_code == 403:
                print("This is likely a permissions issue. Check that your bot has 'Manage Roles' permission.")

                # Try to get detailed error message
                try:
                    error_data = assign_response.json()
                    error_message = error_data.get('message', 'Unknown error')
                    print(f"Error message: {error_message}")
                    return {"success": False, "message": f"Permission denied: {error_message}"}
                except:
                    pass

                return {"success": False, "message": "Permission denied. Check bot's role hierarchy and permissions."}

            return {"success": False, "message": f"Failed to assign role: {assign_response.status_code}"}

    except Exception as e:
        import traceback
        print(f"❌ Exception occurred: {str(e)}")
        traceback.print_exc()
        return {"success": False, "message": f"Error: {str(e)}"}
    finally:
        print("\n===== ROLE ASSIGNMENT COMPLETED =====\n")


def handle_verify_rank_check(discord_username, tier):
    """
    Main handler for rank verification and role assignment

    Args:
        discord_username: The Discord username to assign the role to
        tier: The tier/role to assign (e.g., "Rank A", "Rank B", "Rank C")

    Returns:
        Dictionary with success status and message
    """
    print(f"\n====== VERIFY RANK CHECK ======")
    print(f"Processing verification for user: {discord_username}")
    print(f"Requested tier: {tier}")

    # Verify we have all required information
    if not discord_username or not tier:
        print("❌ Missing required information")
        return {
            "success": False,
            "message": "Both Discord username and tier are required"
        }

    # Validate tier format
    if tier not in ["Rank A", "Rank B", "Rank C"]:
        print(f"❌ Invalid tier format: {tier}")
        return {
            "success": False,
            "message": f"Invalid tier: {tier}. Must be 'Rank A', 'Rank B', or 'Rank C'."
        }

    # Check environment variables
    if not DISCORD_TOKEN:
        print("❌ DISCORD_TOKEN not found in environment")
        return {
            "success": False,
            "message": "Missing Discord bot token in server configuration"
        }

    if not DISCORD_GUILD_ID:
        print("❌ DISCORD_GUILD_ID not found in environment")
        return {
            "success": False,
            "message": "Missing Discord guild ID in server configuration"
        }

    print("✅ All required information is present")

    # Perform the Discord role assignment
    assignment_result = assign_discord_role(discord_username, role_name=tier)

    if not assignment_result["success"]:
        print(f"❌ Role assignment failed: {assignment_result['message']}")
        # Create a response that will be displayed to the user
        return {
            "success": False,
            "message": "Could not assign Discord role automatically. Please contact an admin.",
            "error_details": assignment_result['message']
        }

    print(f"✅ Role assignment successful: {assignment_result['message']}")
    return {
        "success": True,
        "message": f"Successfully assigned {tier} role to {discord_username}"
    }


@app.route('/api/rank-check', methods=['GET'])
def check_rank():
    """API endpoint to check Rocket League rank with Discord username verification"""
    platform = request.args.get('platform', '')
    username = request.args.get('username', '')
    discord_username = request.args.get('discord_username', '')
    discord_id = request.args.get('discord_id', '')  # Add this if possible
    manual_tier = request.args.get('manual_tier', '')
    manual_mmr = request.args.get('manual_mmr', '')

    # Debug logging
    print(f"=== RANK CHECK DEBUG ===")
    print(f"Platform: {platform}")
    print(f"Username: {username}")
    print(f"Discord username: {discord_username}")
    print(f"Discord ID: {discord_id}")
    print(f"Manual tier: {manual_tier}")
    print(f"Manual MMR: {manual_mmr}")
    print(f"========================")

    # PRIORITY 1: Handle manual tier selection if provided
    if manual_tier:
        print(f"Using manually provided tier: {manual_tier}")
        # Use provided MMR if available, otherwise fallback
        mmr = int(manual_mmr) if manual_mmr and manual_mmr.isdigit() else get_mmr_from_rank(manual_tier)
        print(f"Using MMR: {mmr}")  # Debug MMR value

        manual_result = {
            "success": True,
            "username": username or "Manual Entry",
            "platform": platform or "unknown",
            "rank": manual_tier,
            "tier": manual_tier,
            "mmr": mmr,
            "global_mmr": 300,  # Initialize Global MMR at 300
            "timestamp": time.time(),
            "manual_verification": True
        }

        # NEW: Add debug print to confirm MMR
        print(f"DEBUG: Setting MMR to {mmr} for player with Discord username {discord_username}")

        # Handle Discord role assignment if username provided
        role_result = {"success": False, "message": "No Discord username provided"}

        if discord_username:
            print(f"Storing manual rank data for Discord user: {discord_username}")
            # Pass the Discord ID if available
            store_rank_data(discord_username, username or discord_username, platform or "unknown", manual_result,
                            discord_id=discord_id)

            # Try the role assignment with debugging
            role_result = handle_verify_rank_check(discord_username, manual_tier)

            # Store the result
            manual_result["role_assignment"] = role_result

        return jsonify(manual_result)

    # Since we don't use the RLTracker API anymore, use mock data directly
    print("Using mock data for rank verification")
    mock_data = get_mock_rank_data(username, platform)
    mock_data["fallback_method"] = "This is mock data as no manual tier was provided"

    # Handle Discord verification for mock data
    if discord_username:
        tier = mock_data.get("tier")
        store_rank_data(discord_username, username or discord_username, platform, mock_data, discord_id=discord_id)
        role_result = handle_verify_rank_check(discord_username, tier)
        mock_data["role_assignment"] = role_result

    return jsonify(mock_data)


@app.route('/api/user-rank/<discord_username>')
def get_user_rank(discord_username):
    """Get stored rank data for a user"""
    try:
        # Find rank record
        rank_data = ranks_collection.find_one({"discord_username": discord_username}, {"_id": 0})

        if not rank_data:
            return jsonify({"success": False, "message": "No rank data found for this user"}), 404

        # Format timestamp for output
        if "timestamp" in rank_data:
            rank_data["checked_at"] = rank_data["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
            del rank_data["timestamp"]

        return jsonify({
            "success": True,
            "rank_data": rank_data
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"Error retrieving rank data: {str(e)}"
        }), 500


# NEW API ENDPOINT: Get the timestamp of the last leaderboard reset
@app.route('/api/reset-timestamp', methods=['GET'])
def get_last_reset_timestamp():
    """Get the timestamp of the last reset (leaderboard or verification)"""
    try:
        # Find the most recent reset of any type
        last_reset = resets_collection.find_one(
            {"type": {"$in": ["leaderboard_reset", "verification_reset"]}},
            sort=[("timestamp", -1)]
        )

        if last_reset:
            # Convert timestamp to ISO format string
            timestamp_str = last_reset["timestamp"].isoformat() if isinstance(last_reset["timestamp"],
                                                                            datetime.datetime) else str(
                last_reset["timestamp"])

            return jsonify({
                "success": True,
                "last_reset": timestamp_str,
                "reset_type": last_reset.get("type", "unknown")
            })
        else:
            return jsonify({
                "success": False,
                "last_reset": None,
                "message": "No reset events found"
            })
    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"Error retrieving reset timestamp: {str(e)}"
        }), 500


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
        mmr = 1600
    elif hash_value < 20:  # 10% chance
        rank = "Grand Champion III"
        tier = "Rank A"
        mmr = 1600
    elif hash_value < 40:  # 20% chance
        rank = "Champion III"
        tier = "Rank B"
        mmr = 1100
    elif hash_value < 60:  # 20% chance
        rank = "Champion I"
        tier = "Rank B"
        mmr = 1100
    elif hash_value < 80:  # 20% chance
        rank = "Diamond III"
        tier = "Rank C"
        mmr = 600
    else:  # 20% chance
        rank = "Platinum II"
        tier = "Rank C"
        mmr = 600

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


@app.route('/api/reset-leaderboard', methods=['POST'])
def reset_leaderboard():
    """Reset leaderboard data including ranks"""
    # Check for authorization (you might want to add an admin password or token)
    auth_token = request.headers.get('Authorization')
    if not auth_token or auth_token != os.getenv('ADMIN_TOKEN', 'admin-secret-token'):
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    try:
        # Get current counts for reporting
        player_count = players_collection.count_documents({})
        match_count = matches_collection.count_documents({})
        rank_count = ranks_collection.count_documents({})

        # Create backup collections with timestamp
        timestamp = datetime.datetime.utcnow()
        timestamp_str = timestamp.strftime("%Y%m%d_%H%M%S")

        # Backup players
        if player_count > 0:
            db[f'players_backup_{timestamp_str}'].insert_many(players_collection.find())

        # Backup matches
        if match_count > 0:
            db[f'matches_backup_{timestamp_str}'].insert_many(matches_collection.find())

        # Backup ranks
        if rank_count > 0:
            db[f'ranks_backup_{timestamp_str}'].insert_many(ranks_collection.find())

        # Clear collections
        players_collection.delete_many({})
        matches_collection.delete_many({})
        ranks_collection.delete_many({})

        # Record the reset event
        resets_collection.insert_one({
            "type": "leaderboard_reset",
            "timestamp": timestamp,
            "performed_by": request.json.get("admin_id", "unknown") if request.json else "unknown",
            "reason": request.json.get("reason", "Season reset") if request.json else "Season reset"
        })

        return jsonify({
            "success": True,
            "message": "Leaderboard data reset successfully",
            "data": {
                "players_removed": player_count,
                "matches_removed": match_count,
                "ranks_removed": rank_count,
                "backup_timestamp": timestamp_str
            }
        })

    except Exception as e:
        print(f"Error in reset_leaderboard: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Error resetting leaderboard: {str(e)}"
        }), 500


@app.route('/api/reset-verification', methods=['POST'])
def reset_verification():
    """Reset all rank verification status - called during leaderboard reset"""
    try:
        # Check for authorization
        auth_token = request.headers.get('Authorization')
        if not auth_token or auth_token != os.getenv('ADMIN_TOKEN', 'admin-secret-token'):
            return jsonify({"success": False, "message": "Unauthorized"}), 401

        # Create a record of the reset
        reset_timestamp = datetime.datetime.utcnow()

        # Store reset event in the resets collection
        resets_collection.insert_one({
            "type": "verification_reset",
            "timestamp": reset_timestamp,
            "performed_by": request.json.get("admin_id", "unknown") if request.json else "unknown",
            "reason": request.json.get("reason", "Rank verification reset") if request.json else "Rank verification reset"
        })

        return jsonify({
            "success": True,
            "message": "Verification reset successful",
            "timestamp": reset_timestamp.isoformat()
        })

    except Exception as e:
        print(f"Error in reset_verification: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Error resetting verification: {str(e)}"
        }), 500

@app.route('/api/verify-rank', methods=['POST'])
def verify_rank():
    """API endpoint to verify a player's rank and assign a Discord role"""
    data = request.json
    if not data:
        return jsonify({"success": False, "message": "No data provided"}), 400

    discord_username = data.get('discord_username')
    rank = data.get('rank')
    tier = data.get('tier')
    mmr = data.get('mmr')

    if not discord_username or not rank or not tier or not mmr:
        return jsonify({"success": False, "message": "Missing required fields"}), 400

    try:
        # Store the rank data in the database
        ranks_collection.insert_one({
            "discord_username": discord_username,
            "rank": rank,
            "tier": tier,
            "mmr": int(mmr),
            "timestamp": datetime.datetime.utcnow()
        })

        # Try to assign the Discord role
        role_result = assign_discord_role(discord_username, tier)

        return jsonify({
            "success": True,
            "message": "Rank verified successfully",
            "role_assignment": role_result
        })
    except Exception as e:
        print(f"Error in verify_rank: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Error: {str(e)}"
        }), 500

@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404


@app.errorhandler(500)
def internal_server_error(e):
    return render_template('500.html'), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)