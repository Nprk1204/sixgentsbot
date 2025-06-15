from flask import Flask, render_template, jsonify, request, redirect, url_for, flash, session
from pymongo import MongoClient
from pymongo.server_api import ServerApi
import os
import datetime  # Keep only this import
import time
import requests
import json
from bson import json_util
from functools import lru_cache
from dotenv import load_dotenv
import functools
import re
import json
import threading

# Import our Discord OAuth integration
from discord_oauth import DiscordOAuth, login_required, get_current_user

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'your-secret-key-here')

# Load environment variables
load_dotenv()
MONGO_URI = os.getenv('MONGO_URI')
RLTRACKER_API_KEY = os.getenv('RLTRACKER_API_KEY', '')
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', '')
DISCORD_GUILD_ID = os.getenv('DISCORD_GUILD_ID', '')

# Discord OAuth configuration
DISCORD_CLIENT_ID = os.getenv('DISCORD_CLIENT_ID', '')
DISCORD_CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET', '')
DISCORD_REDIRECT_URI = os.getenv('DISCORD_REDIRECT_URI', '')
BOT_KEEPALIVE_URL = os.getenv('BOT_KEEPALIVE_URL', '')

#bot runs on pc now, used to be keepalive

# Initialize Discord OAuth
discord_oauth = DiscordOAuth(
    app=app,
    client_id=DISCORD_CLIENT_ID,
    client_secret=DISCORD_CLIENT_SECRET,
    redirect_uri=DISCORD_REDIRECT_URI
)

# Debug environment variables
print("\n=== ENVIRONMENT VARIABLES DEBUG ===")
print(f"DISCORD_TOKEN exists: {'Yes' if DISCORD_TOKEN else 'No'}")
print(f"DISCORD_TOKEN length: {len(DISCORD_TOKEN) if DISCORD_TOKEN else 0}")
print(f"DISCORD_GUILD_ID exists: {'Yes' if DISCORD_GUILD_ID else 'No'}")
print(f"DISCORD_GUILD_ID value: '{DISCORD_GUILD_ID}'")
print(f"DISCORD_CLIENT_ID exists: {'Yes' if DISCORD_CLIENT_ID else 'No'}")
print(f"DISCORD_CLIENT_SECRET exists: {'Yes' if DISCORD_CLIENT_SECRET else 'No'}")
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
    client.admin.command('ping')
    print("MongoDB connection successful!")
    db = client['sixgents_db']
    players_collection = db['players']
    matches_collection = db['matches']
    ranks_collection = db['ranks']
    resets_collection = db['resets']

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

        def insert_one(self, *args, **kwargs):
            return None

        def update_one(self, *args, **kwargs):
            return None


    db = {'players': FallbackDB(), 'matches': FallbackDB(), 'ranks': FallbackDB(), 'resets': FallbackDB()}
    players_collection = db['players']
    matches_collection = db['matches']
    ranks_collection = db['ranks']
    resets_collection = db['resets']


@app.template_filter('tojsonfilter')
def to_json_filter(obj):
    """Convert Python object to JSON string for safe template usage"""

    def json_serial(obj):
        """JSON serializer for objects not serializable by default json code"""
        if isinstance(obj, datetime.datetime):  # Fixed: Use datetime.datetime instead of datetime
            return obj.isoformat()
        raise TypeError(f"Type {type(obj)} not serializable")

    try:
        return json.dumps(obj, default=json_serial)
    except (TypeError, ValueError):
        return '[]'  # Return empty array if conversion fails

# Context processor to make current user available in all templates
@app.context_processor
def inject_user():
    return dict(current_user=get_current_user())


# Discord OAuth Routes
@app.route('/auth/discord/login')
def discord_login():
    """Initiate Discord OAuth login"""
    return redirect(discord_oauth.get_oauth_url())


@app.route('/auth/discord/callback')
def discord_callback_enhanced():
    """Enhanced Discord OAuth callback with rate limit handling"""

    code = request.args.get('code')
    error = request.args.get('error')
    error_description = request.args.get('error_description')

    print(f" OAuth callback received:")
    print(f"   Code: {code[:10] + '...' if code else 'None'}")
    print(f"   Error: {error}")
    print(f"   Error Description: {error_description}")

    if error:
        error_msg = f"Discord OAuth error: {error}"
        if error_description:
            error_msg += f" - {error_description}"
        flash(error_msg, 'error')
        return redirect(url_for('home'))

    if not code:
        flash('Authentication failed: No authorization code received', 'error')
        return redirect(url_for('home'))

    try:
        # FIXED: Only retry on rate limits, not all failures
        max_attempts = 3
        base_delay = 5

        for attempt in range(max_attempts):
            print(f" Token exchange attempt {attempt + 1}/{max_attempts}")

            token_data = discord_oauth.exchange_code(code)

            # FIXED: Only retry on specific rate limit errors
            if 'error' in token_data:
                error_msg = token_data['error']

                # Only retry for rate limits
                if 'rate limit' in error_msg.lower() or 'try again' in error_msg.lower():
                    if attempt < max_attempts - 1:  # Not the last attempt
                        delay = base_delay * (2 ** attempt)  # Exponential backoff
                        print(f" Rate limited - waiting {delay} seconds before retry")
                        import time
                        time.sleep(delay)
                        continue
                else:
                    # For non-rate-limit errors, don't retry - fail immediately
                    print(f" OAuth error (no retry): {error_msg}")
                    flash(f'Authentication failed: {error_msg}', 'error')
                    return redirect(url_for('home'))

            # Check for access token
            if 'access_token' not in token_data:
                flash('Authentication failed: No access token received', 'error')
                return redirect(url_for('home'))

            # SUCCESS - continue with normal flow
            access_token = token_data['access_token']
            print(f" Access token received")

            # Get user information
            user_info = discord_oauth.get_user_info(access_token)
            if not user_info:
                flash('Authentication failed: Could not get user information', 'error')
                return redirect(url_for('home'))

            # Store user in session
            session['discord_user'] = {
                'id': user_info['id'],
                'username': user_info['username'],
                'global_name': user_info.get('global_name'),
                'discriminator': user_info.get('discriminator'),
                'avatar': user_info.get('avatar'),
                'access_token': access_token
            }

            flash(f'Successfully logged in as {user_info["username"]}!', 'success')
            return redirect(url_for('profile'))

        # If we get here, all attempts failed due to rate limiting
        flash('Discord is temporarily limiting access. Please wait 5-10 minutes and try again.', 'warning')
        return redirect(url_for('home'))

    except Exception as e:
        print(f" Critical callback error: {e}")
        import traceback
        traceback.print_exc()
        flash('Authentication failed: Internal error', 'error')
        return redirect(url_for('home'))


@app.route('/auth/discord/logout')
def discord_logout():
    """Log out the user"""
    session.pop('discord_user', None)
    flash('Successfully logged out!', 'info')
    return redirect(url_for('home'))

@app.route('/debug/oauth-test')
def oauth_test():
    """Test OAuth configuration without full flow"""
    return {
        'client_id_length': len(DISCORD_CLIENT_ID) if DISCORD_CLIENT_ID else 0,
        'client_secret_length': len(DISCORD_CLIENT_SECRET) if DISCORD_CLIENT_SECRET else 0,
        'redirect_uri': DISCORD_REDIRECT_URI,
        'api_endpoint': 'https://discord.com/api/v10',
        'oauth_url_test': discord_oauth.get_oauth_url() if discord_oauth else 'No OAuth instance'
    }

# Main Routes
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
    }).sort("mmr", -1).limit(6))

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
    }).sort("global_mmr", -1).limit(6))

    # Calculate win rates for featured global players
    for player in featured_global_players:
        matches = player.get("global_matches", 0)
        wins = player.get("global_wins", 0)
        player["win_rate"] = round((wins / matches) * 100, 2) if matches > 0 else 0
        player["mmr"] = player.get("global_mmr", 0)

    # Get recent matches
    recent_matches = list(matches_collection.find(
        {"status": "completed"},
        {"_id": 0}
    ).sort("completed_at", -1).limit(6))

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
    """Display the main leaderboard page - default to global"""
    return render_template('leaderboard.html', board_type='global')


@app.route('/leaderboard/<board_type>')
@cached(timeout=60)
def leaderboard_by_type(board_type):
    """Display the leaderboard page for a specific type"""
    valid_types = ['global', 'rank-a', 'rank-b', 'rank-c', 'all']
    if board_type not in valid_types:
        board_type = 'global'

    return render_template('leaderboard.html', board_type=board_type)


@app.route('/profile')
@login_required
def profile():
    """Display the user's profile page with current stats"""
    try:
        user = get_current_user()
        if not user:
            flash('Please log in to view your profile.', 'error')
            return redirect(url_for('discord_login'))

        print(f"Loading profile for user: {user.get('username')} (ID: {user.get('id')})")

        # Get player data from database
        player_data = None
        try:
            player_data = players_collection.find_one({"id": user['id']})
            print(f"Player data found: {player_data is not None}")
        except Exception as db_error:
            print(f"Database error fetching player: {db_error}")

        # Get rank verification data
        rank_data = None
        try:
            rank_data = ranks_collection.find_one({"discord_id": user['id']})
            print(f"Rank data found: {rank_data is not None}")
        except Exception as rank_error:
            print(f"Error fetching rank data: {rank_error}")

        # Get recent matches for the player
        recent_matches = []
        if player_data:
            try:
                print("Fetching recent matches...")
                match_history = list(matches_collection.find({
                    "$or": [
                        {"team1.id": user['id']},
                        {"team2.id": user['id']}
                    ],
                    "status": "completed"
                }).sort("completed_at", -1).limit(10))

                print(f"Found {len(match_history)} recent matches")

                # Process match history
                for match in match_history:
                    try:
                        # Determine if player won
                        player_in_team1 = any(p.get("id") == user['id'] for p in match.get("team1", []))
                        winner = match.get("winner")
                        player_won = (player_in_team1 and winner == 1) or (not player_in_team1 and winner == 2)

                        # Get completed date
                        completed_at = match.get('completed_at')
                        if not completed_at:
                            continue

                        match_data = {
                            'date': completed_at.strftime("%Y-%m-%d") if hasattr(completed_at, 'strftime') else str(completed_at),
                            'player_result': 'Win' if player_won else 'Loss',
                            'is_global': match.get("is_global", False),
                            'mmr_change': 0
                        }

                        # Find MMR change for this player
                        mmr_changes = match.get("mmr_changes", [])
                        for mmr_change in mmr_changes:
                            if mmr_change.get("player_id") == user['id']:
                                match_data['mmr_change'] = mmr_change.get("mmr_change", 0)
                                break

                        recent_matches.append(match_data)

                    except Exception as process_error:
                        print(f"Error processing match {match.get('match_id', 'unknown')}: {process_error}")
                        continue

            except Exception as match_error:
                print(f"Error fetching match history: {match_error}")

        print(f"Processed {len(recent_matches)} matches for profile")

        # If no player data exists, create default structure
        if not player_data:
            player_data = {
                'id': user['id'],
                'name': user.get('global_name') or user.get('username'),
                'mmr': 0,
                'global_mmr': 300,
                'wins': 0,
                'losses': 0,
                'matches': 0,
                'global_wins': 0,
                'global_losses': 0,
                'global_matches': 0,
                'current_streak': 0,
                'global_current_streak': 0
            }

        return render_template('profile.html',
                             user=user,
                             player_data=player_data,
                             rank_data=rank_data,
                             recent_matches=recent_matches)

    except Exception as e:
        print(f"Error in profile route: {e}")
        import traceback
        traceback.print_exc()
        flash('An error occurred loading your profile.', 'error')
        return redirect(url_for('home'))


@app.route('/profile/stats')
@login_required
def profile_stats():
    """Display detailed player stats"""
    try:
        user = get_current_user()
        if not user:
            flash('Please log in to view your stats.', 'error')
            return redirect(url_for('discord_login'))

        print(f"Loading stats for user: {user.get('username')} (ID: {user.get('id')})")

        # Get player data from database
        player_data = None
        try:
            player_data = players_collection.find_one({"id": user['id']})
            print(f"Player data found: {player_data is not None}")
        except Exception as db_error:
            print(f"Database error fetching player: {db_error}")

        if not player_data:
            print("No player data found, creating empty structure")
            # No stats available - create empty data structure
            player_data = {
                'id': user['id'],
                'name': user.get('global_name') or user.get('username'),
                'mmr': 0,
                'global_mmr': 300,
                'wins': 0,
                'losses': 0,
                'matches': 0,
                'global_wins': 0,
                'global_losses': 0,
                'global_matches': 0,
                'current_streak': 0,
                'longest_win_streak': 0,
                'longest_loss_streak': 0,
                'global_current_streak': 0,
                'global_longest_win_streak': 0,
                'global_longest_loss_streak': 0
            }

        # Ensure all required fields exist with defaults
        required_fields = {
            'mmr': 0,
            'global_mmr': 300,
            'wins': 0,
            'losses': 0,
            'matches': 0,
            'global_wins': 0,
            'global_losses': 0,
            'global_matches': 0,
            'current_streak': 0,
            'longest_win_streak': 0,
            'longest_loss_streak': 0,
            'global_current_streak': 0,
            'global_longest_win_streak': 0,
            'global_longest_loss_streak': 0
        }

        for field, default_value in required_fields.items():
            if field not in player_data:
                player_data[field] = default_value

        print(
            f"Player data prepared: matches={player_data.get('matches')}, global_matches={player_data.get('global_matches')}")

        # Get match history for performance graphs
        ranked_matches = []
        global_matches = []

        try:
            print("Fetching match history...")
            match_history = list(matches_collection.find({
                "$or": [
                    {"team1.id": user['id']},
                    {"team2.id": user['id']}
                ],
                "status": "completed"
            }).sort("completed_at", 1).limit(50))

            print(f"Found {len(match_history)} matches in history")

            # Process match history for graphs
            for match in match_history:
                try:
                    # Get completed date
                    completed_at = match.get('completed_at')
                    if not completed_at:
                        print(f"Skipping match {match.get('match_id')} - no completed_at timestamp")
                        continue

                    # Find MMR change for this player first
                    mmr_change_for_player = 0
                    mmr_changes = match.get("mmr_changes", [])
                    for mmr_change in mmr_changes:
                        if mmr_change.get("player_id") == user['id']:
                            mmr_change_for_player = mmr_change.get("mmr_change", 0)
                            break

                    # Simple logic: if MMR went up, it's a win
                    player_won = mmr_change_for_player > 0

                    match_data = {
                        'date': completed_at.isoformat() if hasattr(completed_at, 'isoformat') else str(completed_at),
                        'won': player_won,
                        'match_id': match.get('match_id', ''),
                        'mmr_change': mmr_change_for_player,
                        'new_mmr': 0,
                        'player_result': 'Win' if player_won else 'Loss',  # Add this for template
                        'is_global': match.get("is_global", False)  # Add this for template
                    }

                    # Get new MMR if available
                    for mmr_change in mmr_changes:
                        if mmr_change.get("player_id") == user['id']:
                            match_data['new_mmr'] = mmr_change.get("new_mmr", 0)
                            break

                    # Sort into ranked vs global based on match type
                    if match.get("is_global", False):
                        # Only include if this was a global MMR change
                        global_matches.append(match_data)
                    else:
                        # Only include if this was a ranked MMR change
                        ranked_matches.append(match_data)

                    print(
                        f"Processed match {match.get('match_id')}: {match_data['player_result']}, MMR: {mmr_change_for_player}")

                except Exception as process_error:
                    print(f"Error processing match {match.get('match_id', 'unknown')}: {process_error}")
                    continue

                    # Find MMR change for this player
                    mmr_changes = match.get("mmr_changes", [])
                    player_mmr_change = None

                    for mmr_change in mmr_changes:
                        if mmr_change.get("player_id") == user['id']:
                            player_mmr_change = mmr_change
                            break

                    if player_mmr_change:
                        match_data['mmr_change'] = player_mmr_change.get("mmr_change", 0)
                        match_data['new_mmr'] = player_mmr_change.get("new_mmr", 0)

                        # Sort into ranked vs global based on match type
                        if match.get("is_global", False):
                            # Only include if this was a global MMR change
                            if player_mmr_change.get("is_global", False):
                                global_matches.append(match_data)
                        else:
                            # Only include if this was a ranked MMR change
                            if not player_mmr_change.get("is_global", False):
                                ranked_matches.append(match_data)
                    else:
                        print(f"No MMR change found for player in match {match.get('match_id')}")

                except Exception as process_error:
                    print(f"Error processing match {match.get('match_id', 'unknown')}: {process_error}")
                    continue

        except Exception as match_error:
            print(f"Error fetching match history: {match_error}")

        print(f"Processed matches: {len(ranked_matches)} ranked, {len(global_matches)} global")

        # Combine ranked and global matches for recent_matches
        recent_matches = []
        if ranked_matches:
            recent_matches.extend(ranked_matches)
        if global_matches:
            recent_matches.extend(global_matches)

        # Sort by date (most recent first)
        recent_matches.sort(key=lambda x: x.get('date', ''), reverse=True)

        return render_template('profile_stats.html',
                               user=user,
                               player_data=player_data,
                               ranked_matches=ranked_matches,
                               global_matches=global_matches,
                               recent_matches=recent_matches)

    except Exception as e:
        print(f"Error in profile_stats route: {e}")
        import traceback
        traceback.print_exc()
        flash('An error occurred loading your statistics.', 'error')
        return redirect(url_for('profile'))


@app.route('/debug/my-data-fixed')
@login_required
def debug_my_data_fixed():
    """Debug the current user's data - JSON serializable version"""
    user = get_current_user()
    if not user:
        return {"error": "Not logged in"}

    try:
        player_id = user['id']
        print(f"=== DEBUG: Checking data for player {player_id} ===")

        # Get player data
        player = players_collection.find_one({"id": player_id})

        # Get matches
        matches = list(matches_collection.find({
            "$or": [
                {"team1.id": player_id},
                {"team2.id": player_id}
            ]
        }).sort("completed_at", -1))

        print(f"Found {len(matches)} matches for player {player_id}")

        # Process matches like the profile route does
        recent_matches = []
        for match in matches:
            try:
                # Determine if player won
                player_in_team1 = any(p.get("id") == player_id for p in match.get("team1", []))
                winner = match.get("winner")
                player_won = (player_in_team1 and winner == 1) or (not player_in_team1 and winner == 2)

                # Get completed date
                completed_at = match.get('completed_at')
                if not completed_at:
                    print(f"Skipping match {match.get('match_id')} - no completed_at")
                    continue

                match_data = {
                    'date': completed_at.strftime("%Y-%m-%d") if hasattr(completed_at, 'strftime') else str(
                        completed_at),
                    'player_result': 'Win' if player_won else 'Loss',
                    'is_global': match.get("is_global", False),
                    'mmr_change': 0,
                    'match_id': match.get('match_id')
                }

                # Find MMR change for this player
                mmr_changes = match.get("mmr_changes", [])
                for mmr_change in mmr_changes:
                    if mmr_change.get("player_id") == player_id:
                        match_data['mmr_change'] = mmr_change.get("mmr_change", 0)
                        match_data['streak'] = mmr_change.get("streak", 0)
                        break

                recent_matches.append(match_data)
                print(f"Processed match: {match_data}")

            except Exception as process_error:
                print(f"Error processing match {match.get('match_id', 'unknown')}: {process_error}")
                continue

        # Convert to JSON-safe format
        player_safe = json.loads(json_util.dumps(player)) if player else None
        matches_safe = json.loads(json_util.dumps(matches))

        return {
            "status": "success",
            "player_exists": player is not None,
            "player_stats": {
                "name": player.get("name") if player else None,
                "mmr": player.get("mmr") if player else None,
                "global_mmr": player.get("global_mmr") if player else None,
                "matches": player.get("matches") if player else None,
                "global_matches": player.get("global_matches") if player else None,
                "wins": player.get("wins") if player else None,
                "global_wins": player.get("global_wins") if player else None
            },
            "total_matches_found": len(matches),
            "processed_matches": len(recent_matches),
            "recent_matches": recent_matches,
            "issue_diagnosis": {
                "matches_have_completed_at": all(m.get("completed_at") for m in matches),
                "matches_have_status_completed": all(m.get("status") == "completed" for m in matches),
                "player_found_in_teams": all(
                    any(p.get("id") == player_id for p in m.get("team1", [])) or
                    any(p.get("id") == player_id for p in m.get("team2", []))
                    for m in matches
                ),
                "matches_have_mmr_changes": all(m.get("mmr_changes") for m in matches)
            }
        }

    except Exception as e:
        import traceback
        return {
            "error": str(e),
            "traceback": traceback.format_exc()
        }


@app.route('/debug/profile-match-query')
@login_required
def debug_profile_match_query():
    """Debug exactly what the profile route queries"""
    user = get_current_user()
    if not user:
        return {"error": "Not logged in"}

    player_id = user['id']

    try:
        # This is the EXACT query from your profile route
        match_history = list(matches_collection.find({
            "$or": [
                {"team1.id": player_id},
                {"team2.id": player_id}
            ],
            "status": "completed"  # THIS might be the issue!
        }).sort("completed_at", -1).limit(10))

        print(f"Profile route query found {len(match_history)} matches")

        # Also check without the status filter
        all_matches = list(matches_collection.find({
            "$or": [
                {"team1.id": player_id},
                {"team2.id": player_id}
            ]
        }).sort("completed_at", -1).limit(10))

        print(f"Query without status filter found {len(all_matches)} matches")

        return {
            "query_with_status_completed": len(match_history),
            "query_without_status_filter": len(all_matches),
            "status_values_in_db": [m.get("status") for m in all_matches],
            "issue_found": len(match_history) != len(all_matches)
        }

    except Exception as e:
        return {"error": str(e)}


# Check if your profile route has the right data
@app.route('/debug/simulate-profile')
@login_required
def debug_simulate_profile():
    """Simulate exactly what the profile route should return"""
    user = get_current_user()
    if not user:
        return {"error": "Not logged in"}

    try:
        # Get player data from database - EXACT same query as profile route
        player_data = players_collection.find_one({"id": user['id']})
        print(f"Player data found: {player_data is not None}")

        # Get recent matches for the player - EXACT same query as profile route
        recent_matches = []
        if player_data:
            print("Fetching recent matches...")
            match_history = list(matches_collection.find({
                "$or": [
                    {"team1.id": user['id']},
                    {"team2.id": user['id']}
                ],
                "status": "completed"
            }).sort("completed_at", -1).limit(10))

            print(f"Found {len(match_history)} recent matches")

            # Process match history - EXACT same logic as profile route
            for match in match_history:
                try:
                    # Determine if player won
                    player_in_team1 = any(p.get("id") == user['id'] for p in match.get("team1", []))
                    winner = match.get("winner")
                    player_won = (player_in_team1 and winner == 1) or (not player_in_team1 and winner == 2)

                    # Get completed date
                    completed_at = match.get('completed_at')
                    if not completed_at:
                        continue

                    match_data = {
                        'date': completed_at.strftime("%Y-%m-%d") if hasattr(completed_at, 'strftime') else str(
                            completed_at),
                        'player_result': 'Win' if player_won else 'Loss',
                        'is_global': match.get("is_global", False),
                        'mmr_change': 0
                    }

                    # Find MMR change for this player
                    mmr_changes = match.get("mmr_changes", [])
                    for mmr_change in mmr_changes:
                        if mmr_change.get("player_id") == user['id']:
                            match_data['mmr_change'] = mmr_change.get("mmr_change", 0)
                            break

                    recent_matches.append(match_data)

                except Exception as process_error:
                    print(f"Error processing match {match.get('match_id', 'unknown')}: {process_error}")
                    continue

        return {
            "player_data_exists": player_data is not None,
            "recent_matches_count": len(recent_matches),
            "recent_matches": recent_matches,
            "player_has_matches": (player_data.get('matches', 0) > 0 or player_data.get('global_matches',
                                                                                        0) > 0) if player_data else False,
            "template_would_show_matches": len(recent_matches) > 0
        }

    except Exception as e:
        import traceback
        return {
            "error": str(e),
            "traceback": traceback.format_exc()
        }

@app.route('/debug/player-data/<player_id>')
def debug_player_data(player_id):
    """Debug route to check what data exists for a player"""
    try:
        print(f"=== DEBUG: Checking data for player {player_id} ===")

        # 1. Check if player exists in players collection
        player = players_collection.find_one({"id": player_id})
        print(f"Player document: {player}")

        # 2. Check matches where this player participated
        matches = list(matches_collection.find({
            "$or": [
                {"team1.id": player_id},
                {"team2.id": player_id}
            ]
        }).sort("completed_at", -1))

        print(f"Found {len(matches)} matches for player {player_id}")

        # 3. Check each match structure
        for i, match in enumerate(matches):
            print(f"\n--- Match {i + 1} ---")
            print(f"Match ID: {match.get('match_id')}")
            print(f"Status: {match.get('status')}")
            print(f"Completed at: {match.get('completed_at')}")
            print(f"Is global: {match.get('is_global')}")
            print(f"Team1: {match.get('team1')}")
            print(f"Team2: {match.get('team2')}")
            print(f"Winner: {match.get('winner')}")
            print(f"MMR Changes: {match.get('mmr_changes')}")

            # Check if player is actually in the teams
            player_in_team1 = any(p.get("id") == player_id for p in match.get("team1", []))
            player_in_team2 = any(p.get("id") == player_id for p in match.get("team2", []))
            print(f"Player in team1: {player_in_team1}")
            print(f"Player in team2: {player_in_team2}")

        # 4. Check what the profile route would return
        from datetime import datetime, timedelta

        recent_matches = []
        for match in matches:
            try:
                # Determine if player won
                player_in_team1 = any(p.get("id") == player_id for p in match.get("team1", []))
                winner = match.get("winner")
                player_won = (player_in_team1 and winner == 1) or (not player_in_team1 and winner == 2)

                # Get completed date
                completed_at = match.get('completed_at')
                if not completed_at:
                    print(f"Skipping match {match.get('match_id')} - no completed_at")
                    continue

                match_data = {
                    'date': completed_at.strftime("%Y-%m-%d") if hasattr(completed_at, 'strftime') else str(
                        completed_at),
                    'player_result': 'Win' if player_won else 'Loss',
                    'is_global': match.get("is_global", False),
                    'mmr_change': 0
                }

                # Find MMR change for this player
                mmr_changes = match.get("mmr_changes", [])
                for mmr_change in mmr_changes:
                    if mmr_change.get("player_id") == player_id:
                        match_data['mmr_change'] = mmr_change.get("mmr_change", 0)
                        break

                recent_matches.append(match_data)
                print(f"Processed match: {match_data}")

            except Exception as process_error:
                print(f"Error processing match {match.get('match_id', 'unknown')}: {process_error}")
                continue

        return {
            "player_exists": player is not None,
            "player_data": player,
            "total_matches_found": len(matches),
            "processed_matches": len(recent_matches),
            "recent_matches": recent_matches,
            "raw_matches": [
                {
                    "match_id": m.get("match_id"),
                    "status": m.get("status"),
                    "completed_at": str(m.get("completed_at")),
                    "is_global": m.get("is_global"),
                    "has_teams": bool(m.get("team1") and m.get("team2")),
                    "has_mmr_changes": bool(m.get("mmr_changes"))
                } for m in matches
            ]
        }

    except Exception as e:
        import traceback
        return {
            "error": str(e),
            "traceback": traceback.format_exc()
        }


# Also add this simpler debug route
@app.route('/debug/collections')
def debug_collections():
    """Check the state of all collections"""
    try:
        return {
            "players_count": players_collection.count_documents({}),
            "matches_count": matches_collection.count_documents({}),
            "ranks_count": ranks_collection.count_documents({}),
            "sample_player": players_collection.find_one({}),
            "sample_match": matches_collection.find_one({}),
            "completed_matches": matches_collection.count_documents({"status": "completed"}),
            "matches_with_teams": matches_collection.count_documents({
                "team1": {"$exists": True, "$ne": []},
                "team2": {"$exists": True, "$ne": []}
            })
        }
    except Exception as e:
        return {"error": str(e)}


# Add this to check your specific user ID
@app.route('/debug/my-data')
@login_required
def debug_my_data():
    """Debug the current user's data"""
    user = get_current_user()
    if not user:
        return {"error": "Not logged in"}

    return debug_player_data(user['id'])

@app.route('/profile/rank-check')
@login_required
def profile_rank_check():
    """Display rank check page for authenticated user - using existing rank_check.html"""
    try:
        user = get_current_user()
        if not user:
            flash('Please log in to verify your rank.', 'error')
            return redirect(url_for('discord_login'))

        # Check if user already has rank verification
        rank_data = None
        try:
            rank_data = ranks_collection.find_one({"discord_id": user['id']})
        except Exception as rank_error:
            print(f"Error fetching rank data: {rank_error}")

        # Use the existing rank_check.html template
        # We'll pass the user data and any existing rank data
        return render_template('rank_check.html',
                               user=user,
                               rank_data=rank_data,
                               current_user=user)  # Make sure current_user is available

    except Exception as e:
        print(f"Error in profile_rank_check route: {e}")
        flash('An error occurred loading the rank verification page.', 'error')
        return redirect(url_for('profile'))

# API Routes
@app.route('/api/leaderboard/<board_type>')
@cached(timeout=60)
def get_leaderboard_by_type(board_type):
    """API endpoint to get leaderboard data with pagination for specific type - FIXED VERSION"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)

    if per_page > 100:
        per_page = 100

    query = {}
    sort_field = "mmr"
    mmr_field = "mmr"

    if board_type == "global":
        query = {"global_matches": {"$gt": 0}}
        sort_field = "global_mmr"
        mmr_field = "global_mmr"
    elif board_type == "rank-a":
        query = {"mmr": {"$gte": 1600}, "matches": {"$gt": 0}}
    elif board_type == "rank-b":
        query = {"mmr": {"$gte": 1100, "$lt": 1600}, "matches": {"$gt": 0}}
    elif board_type == "rank-c":
        query = {"mmr": {"$lt": 1100}, "matches": {"$gt": 0}}
    elif board_type != "all":
        board_type = "global"
        query = {"global_matches": {"$gt": 0}}
        sort_field = "global_mmr"
        mmr_field = "global_mmr"

    total_players = players_collection.count_documents(query)
    skip = (page - 1) * per_page

    # FIXED: Include ALL streak fields in projection
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
        "last_updated": 1,
        # FIXED: Add ALL streak fields that were missing
        "current_streak": 1,
        "longest_win_streak": 1,
        "longest_loss_streak": 1,
        "global_current_streak": 1,
        "global_longest_win_streak": 1,
        "global_longest_loss_streak": 1
    }

    top_players = list(players_collection.find(query, projection)
                       .sort(sort_field, -1)
                       .skip(skip).limit(per_page))

    # Process players for display
    for player in top_players:
        player_id = player.get("id")

        if board_type == "global":
            matches = player.get("global_matches", 0)
            wins = player.get("global_wins", 0)
            # FIXED: Use global streak for global leaderboard
            current_streak = player.get("global_current_streak", 0)
            longest_win_streak = player.get("global_longest_win_streak", 0)
            longest_loss_streak = player.get("global_longest_loss_streak", 0)
            player["mmr_display"] = player.get(mmr_field, 0)
        else:
            matches = player.get("matches", 0)
            wins = player.get("wins", 0)
            # FIXED: Use ranked streak for ranked leaderboards
            current_streak = player.get("current_streak", 0)
            longest_win_streak = player.get("longest_win_streak", 0)
            longest_loss_streak = player.get("longest_loss_streak", 0)
            player["mmr_display"] = player.get(mmr_field, 0)

        player["win_rate"] = round((wins / matches) * 100, 2) if matches > 0 else 0

        if "last_updated" in player and player["last_updated"]:
            player["last_match"] = player["last_updated"].strftime("%Y-%m-%d")
        else:
            player["last_match"] = "Unknown"

        # FIXED: Format streak for display with proper logic
        if current_streak > 0:
            if current_streak >= 3:
                player["streak_display"] = f" {current_streak}"
            else:
                player["streak_display"] = f"{current_streak}"
        elif current_streak < 0:
            if current_streak <= -3:
                player["streak_display"] = f" {abs(current_streak)}"
            else:
                player["streak_display"] = f"{abs(current_streak)}"
        else:
            player["streak_display"] = ""

        # FIXED: Add longest streak info for tooltips/details
        player["longest_win_streak_display"] = f"{longest_win_streak} Wins" if longest_win_streak > 0 else "None"
        player["longest_loss_streak_display"] = f"{abs(longest_loss_streak)} Losses" if longest_loss_streak < 0 else "None"

        # Get recent MMR change (existing code)
        recent_mmr_change = get_recent_mmr_change(player_id, board_type == "global")
        player["recent_mmr_change"] = recent_mmr_change

        if "last_updated" in player:
            del player["last_updated"]

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


def get_recent_mmr_change(player_id, is_global=False):
    """Get the most recent MMR change for a player - FIXED VERSION"""
    try:
        query = {
            "$or": [
                {"team1.id": player_id},
                {"team2.id": player_id}
            ],
            "status": "completed",
            "mmr_changes": {"$exists": True, "$ne": []}
        }

        if is_global is not None:
            query["is_global"] = is_global

        recent_match = matches_collection.find_one(
            query,
            sort=[("completed_at", -1)]
        )

        if not recent_match or "mmr_changes" not in recent_match:
            return {
                "change": 0,
                "display": "",
                "class": "text-muted",
                "streak": ""  # FIXED: Add streak info
            }

        for mmr_change in recent_match.get("mmr_changes", []):
            if mmr_change.get("player_id") == player_id:
                # FIXED: Check if this matches the global/ranked type we want
                mmr_change_is_global = mmr_change.get("is_global", False)
                if is_global and mmr_change_is_global:
                    change = mmr_change.get("mmr_change", 0)
                elif not is_global and not mmr_change_is_global:
                    change = mmr_change.get("mmr_change", 0)
                else:
                    continue

                # FIXED: Get streak info from MMR change record
                streak = mmr_change.get("streak", 0)
                if streak > 0:
                    if streak >= 3:
                        streak_display = f" {streak}W"
                    else:
                        streak_display = f"{streak}W"
                elif streak < 0:
                    if streak <= -3:
                        streak_display = f" {abs(streak)}L"
                    else:
                        streak_display = f"{abs(streak)}L"
                else:
                    streak_display = ""

                if change > 0:
                    return {
                        "change": change,
                        "display": f"+{change}",
                        "class": "text-success",
                        "streak": streak_display
                    }
                elif change < 0:
                    return {
                        "change": change,
                        "display": str(change),
                        "class": "text-danger",
                        "streak": streak_display
                    }
                else:
                    return {
                        "change": 0,
                        "display": "0",
                        "class": "text-muted",
                        "streak": streak_display
                    }

        return {
            "change": 0,
            "display": "",
            "class": "text-muted",
            "streak": ""
        }

    except Exception as e:
        print(f"Error getting recent MMR change for player {player_id}: {str(e)}")
        return {
            "change": 0,
            "display": "",
            "class": "text-muted",
            "streak": ""
        }


@app.route('/api/player/<player_id>')
def get_player(player_id):
    """API endpoint to get player data with improved error handling and proper ObjectId serialization"""
    try:
        # Remove any unexpected suffixes from player_id (like :1)
        if ':' in player_id:
            print(f"Warning: Player ID contains colon, cleaning: {player_id}")
            player_id = player_id.split(':')[0]

        print(f"Fetching player data for ID: {player_id}")

        # Validate player_id format
        if not player_id or not isinstance(player_id, str):
            return jsonify({"error": "Invalid player ID format"}), 400

        # Try to find the player
        player = players_collection.find_one({"id": player_id})

        if not player:
            print(f"Player not found: {player_id}")
            return jsonify({"error": "Player not found"}), 404

        # Remove MongoDB _id field which is not JSON serializable
        if '_id' in player:
            del player['_id']

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

        # Format streaks for display - RANKED STREAKS
        current_streak = player.get("current_streak", 0)
        longest_win_streak = player.get("longest_win_streak", 0)
        longest_loss_streak = player.get("longest_loss_streak", 0)

        # Add ranked streak display info with emoji
        if current_streak > 0:
            if current_streak >= 3:
                player["streak_display"] = f" {current_streak} Win Streak"
            else:
                player["streak_display"] = f"{current_streak} Win Streak"
        elif current_streak < 0:
            if current_streak <= -3:
                player["streak_display"] = f" {abs(current_streak)} Loss Streak"
            else:
                player["streak_display"] = f"{abs(current_streak)} Loss Streak"
        else:
            player["streak_display"] = "No current streak"

        # Add extra ranked streak stats
        player["longest_win_streak_display"] = f"{longest_win_streak} Wins" if longest_win_streak > 0 else "None"
        player[
            "longest_loss_streak_display"] = f"{abs(longest_loss_streak)} Losses" if longest_loss_streak < 0 else "None"

        # Format streaks for display - GLOBAL STREAKS
        global_current_streak = player.get("global_current_streak", 0)
        global_longest_win_streak = player.get("global_longest_win_streak", 0)
        global_longest_loss_streak = player.get("global_longest_loss_streak", 0)

        # Add global streak display info with emoji
        if global_current_streak > 0:
            if global_current_streak >= 3:
                player["global_streak_display"] = f" {global_current_streak} Win Streak"
            else:
                player["global_streak_display"] = f"{global_current_streak} Win Streak"
        elif global_current_streak < 0:
            if global_current_streak <= -3:
                player["global_streak_display"] = f" {abs(global_current_streak)} Loss Streak"
            else:
                player["global_streak_display"] = f"{abs(global_current_streak)} Loss Streak"
        else:
            player["global_streak_display"] = "No current streak"

        # Add extra global streak stats
        player[
            "global_longest_win_streak_display"] = f"{global_longest_win_streak} Wins" if global_longest_win_streak > 0 else "None"
        player[
            "global_longest_loss_streak_display"] = f"{abs(global_longest_loss_streak)} Losses" if global_longest_loss_streak < 0 else "None"

        # Get recent matches for this player - handle potential errors
        try:
            query = {"$or": [
                {"team1.id": player_id},
                {"team2.id": player_id}
            ], "status": "completed"}

            recent_matches = list(matches_collection.find(
                query
            ).sort("completed_at", -1).limit(20))  # Increased limit to get more matches

            print(f"Found {len(recent_matches)} recent matches for player {player_id}")
        except Exception as match_error:
            print(f"Error fetching matches for player {player_id}: {str(match_error)}")
            recent_matches = []

        # Format match data and include is_global flag
        formatted_matches = []
        for match in recent_matches:
            try:
                # Remove MongoDB _id from match
                if '_id' in match:
                    del match['_id']

                # Remove MongoDB _id from team members
                if 'team1' in match and isinstance(match['team1'], list):
                    for team_member in match['team1']:
                        if '_id' in team_member:
                            del team_member['_id']

                if 'team2' in match and isinstance(match['team2'], list):
                    for team_member in match['team2']:
                        if '_id' in team_member:
                            del team_member['_id']

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
                    # Default to non-global
                    match["is_global"] = False

                # Format date - protect against missing/invalid dates
                if "completed_at" in match and match["completed_at"]:
                    try:
                        match["date"] = match["completed_at"].strftime("%Y-%m-%d")
                    except (AttributeError, ValueError) as e:
                        match["date"] = "Unknown"
                    # Convert completed_at to string to avoid serialization issues
                    match["completed_at"] = str(match["completed_at"])
                else:
                    match["date"] = "Unknown"

                # Add MMR change info if available
                for change in match.get("mmr_changes", []):
                    if change.get("player_id") == player_id:
                        # Add MMR change and streak info to match data
                        match["mmr_change"] = change.get("mmr_change", 0)
                        match["streak"] = change.get("streak", 0)

                        # Format streak with emojis for display
                        streak = change.get("streak", 0)
                        if streak > 0:
                            if streak >= 3:
                                match["streak_display"] = f" {streak}"
                            else:
                                match["streak_display"] = f"{streak}"
                        elif streak < 0:
                            if streak <= -3:
                                match["streak_display"] = f" {abs(streak)}"
                            else:
                                match["streak_display"] = f"{abs(streak)}"
                        else:
                            match["streak_display"] = "No streak"

                        break

                formatted_matches.append(match)
            except Exception as format_error:
                print(f"Error formatting match {match.get('match_id', 'unknown')}: {str(format_error)}")
                # Skip this match if there's an error formatting it
                continue

        # Add match history
        player["recent_matches"] = formatted_matches

        # Use Flask's jsonify which properly handles serialization
        return jsonify(player)
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"Error getting player {player_id}: {str(e)}")
        print(f"Detailed error: {error_details}")
        return jsonify({"error": "Internal server error", "message": str(e)}), 500


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


# Profile-specific API routes
@app.route('/api/profile/rank-check', methods=['POST'])
@login_required
def profile_rank_check_api():
    """Handle rank check for authenticated user"""
    try:
        user = get_current_user()
        if not user:
            return jsonify({"success": False, "message": "Not authenticated"}), 401

        # Get JSON data from request
        try:
            data = request.get_json()
            if not data:
                return jsonify({"success": False, "message": "No data provided"}), 400
        except Exception as json_error:
            print(f"JSON parsing error: {json_error}")
            return jsonify({"success": False, "message": "Invalid JSON data"}), 400

        manual_tier = data.get('manual_tier')
        manual_mmr = data.get('manual_mmr')

        if not manual_tier or not manual_mmr:
            return jsonify({"success": False, "message": "Missing tier or MMR data"}), 400

        print(f"Processing rank check for user {user.get('username')} - Tier: {manual_tier}, MMR: {manual_mmr}")

        try:
            # Convert MMR to integer
            manual_mmr = int(manual_mmr)
        except (ValueError, TypeError):
            return jsonify({"success": False, "message": "Invalid MMR value"}), 400

        # Check if user already has a rank record
        try:
            existing_rank = ranks_collection.find_one({"discord_id": user['id']})
            if existing_rank:
                print(f"User {user.get('username')} already has rank verification: {existing_rank.get('tier')}")
                return jsonify({
                    "success": False,
                    "message": "You have already verified your rank. Contact an admin if you need to make changes."
                }), 400
        except Exception as db_check_error:
            print(f"Error checking existing rank: {db_check_error}")
            # Continue with verification if we can't check existing rank

        try:
            # Store rank data
            rank_document = {
                "discord_username": user.get('global_name') or user.get('username'),
                "discord_id": user['id'],
                "rank": manual_tier,  # This is the tier (Rank A, B, or C)
                "tier": manual_tier,  # Keep both for compatibility
                "mmr": manual_mmr,
                "global_mmr": 300,  # Default global MMR
                "timestamp": datetime.datetime.utcnow()  # Fixed: Use datetime.datetime
            }

            print(f"Storing rank document: {rank_document}")

            # Insert rank record
            result = ranks_collection.insert_one(rank_document)
            print(f"Rank record inserted with ID: {result.inserted_id}")

        except Exception as db_error:
            print(f"Database error storing rank: {db_error}")
            return jsonify({
                "success": False,
                "message": f"Database error: {str(db_error)}"
            }), 500

        # Try to assign Discord role
        try:
            role_result = assign_discord_role(
                username=user.get('global_name') or user.get('username'),
                role_name=manual_tier,
                discord_id=user['id']
            )
            print(f"Discord role assignment result: {role_result}")
        except Exception as role_error:
            print(f"Error assigning Discord role: {role_error}")
            role_result = {
                "success": False,
                "message": f"Role assignment error: {str(role_error)}"
            }

        return jsonify({
            "success": True,
            "message": "Rank verified successfully",
            "rank": manual_tier,
            "tier": manual_tier,
            "mmr": manual_mmr,
            "role_assignment": role_result
        })

    except Exception as e:
        print(f"Error in profile rank check API: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "message": f"Server error: {str(e)}"
        }), 500


# Rank verification functions (existing code)
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


def assign_discord_role(username, role_name=None, role_id=None, discord_id=None):
    """Improved Discord role assignment with better error handling and user matching"""
    print("\n===== DISCORD ROLE ASSIGNMENT DEBUG =====")
    print(f"Attempting to assign role to user: {username}")
    print(f"Discord ID provided: {discord_id if discord_id else 'No'}")
    print(f"Role name: {role_name}")
    print(f"Role ID: {role_id}")

    # Check for missing required information
    if not username:
        return {"success": False, "message": "Username is required"}

    if not DISCORD_TOKEN:
        return {"success": False, "message": "Discord bot token not configured"}

    if not DISCORD_GUILD_ID:
        return {"success": False, "message": "Discord guild ID not configured"}

    # Headers for all API requests
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        # STEP 1: Verify bot authentication
        print("\n1. Verifying bot authentication...")
        auth_url = "https://discord.com/api/v10/users/@me"

        try:
            auth_response = requests.get(auth_url, headers=headers, timeout=10)
        except requests.exceptions.RequestException as e:
            print(f" Network error during authentication: {e}")
            return {"success": False, "message": f"Network error: {str(e)}"}

        if auth_response.status_code != 200:
            print(f" Authentication failed: {auth_response.status_code}")
            return {"success": False, "message": f"Bot authentication failed: {auth_response.status_code}"}

        bot_user = auth_response.json()
        bot_id = bot_user.get('id')
        bot_name = bot_user.get('username')
        print(f" Bot authenticated as: {bot_name} (ID: {bot_id})")

        # STEP 2: Get server information
        print("\n2. Getting server information...")
        guild_url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}"

        try:
            guild_response = requests.get(guild_url, headers=headers, timeout=10)
        except requests.exceptions.RequestException as e:
            print(f" Network error getting guild info: {e}")
            return {"success": False, "message": f"Network error: {str(e)}"}

        if guild_response.status_code != 200:
            print(f" Failed to get server info: {guild_response.status_code}")
            return {"success": False, "message": f"Failed to get server info: {guild_response.status_code}"}

        guild_data = guild_response.json()
        print(f" Connected to server: {guild_data.get('name')}")

        # STEP 3: Find user - prioritize Discord ID if provided
        print(f"\n3. Finding user...")
        user_id = None
        matched_name = None

        if discord_id:
            print(f"3a. Attempting to find user using provided Discord ID: {discord_id}")
            member_url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/members/{discord_id}"

            try:
                member_response = requests.get(member_url, headers=headers, timeout=10)

                if member_response.status_code == 200:
                    member_data = member_response.json()
                    user_id = discord_id
                    member_user = member_data.get('user', {})
                    matched_name = member_user.get('global_name') or member_user.get('username', '')
                    print(f" Found user by ID: {matched_name} (ID: {user_id})")
                else:
                    print(f" Failed to find user by ID: {member_response.status_code}")

            except requests.exceptions.RequestException as e:
                print(f" Network error finding user by ID: {e}")

        # If Discord ID didn't work or wasn't provided, try username search
        if not user_id:
            print("\n3b. Finding user with username search...")
            search_url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/members/search?query={username}&limit=10"

            try:
                search_response = requests.get(search_url, headers=headers, timeout=10)

                if search_response.status_code == 200:
                    search_results = search_response.json()
                    print(f"Found {len(search_results)} potential matches")

                    # First try exact matches on username, global_name, or nickname
                    for member in search_results:
                        member_user = member.get('user', {})
                        member_username = member_user.get('username', '')
                        member_global_name = member_user.get('global_name', '')
                        member_nickname = member.get('nick', '')
                        member_id = member_user.get('id')

                        # Check for exact match first (case insensitive)
                        if (username.lower() == member_username.lower() or
                                username.lower() == member_global_name.lower() or
                                username.lower() == member_nickname.lower()):
                            user_id = member_id
                            matched_name = member_global_name or member_username
                            print(f" Found exact match: {matched_name} (ID: {user_id})")
                            break
                else:
                    print(f" Failed to search for users: {search_response.status_code}")

            except requests.exceptions.RequestException as e:
                print(f" Network error searching for users: {e}")

        # If we still couldn't find the user
        if not user_id:
            print(f" No matching user found for '{username}'")
            return {"success": False, "message": f"Could not find user '{username}' in Discord server"}

        # STEP 4: Get all server roles
        print("\n4. Getting server roles...")
        roles_url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/roles"

        try:
            roles_response = requests.get(roles_url, headers=headers, timeout=10)
        except requests.exceptions.RequestException as e:
            print(f" Network error getting roles: {e}")
            return {"success": False, "message": f"Network error: {str(e)}"}

        if roles_response.status_code != 200:
            print(f" Failed to get roles: {roles_response.status_code}")
            return {"success": False, "message": f"Failed to retrieve roles: {roles_response.status_code}"}

        roles = roles_response.json()
        print(f" Found {len(roles)} roles in the server")

        # Find the bot member to get its roles
        bot_member_url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/members/{bot_id}"

        try:
            bot_member_response = requests.get(bot_member_url, headers=headers, timeout=10)
        except requests.exceptions.RequestException as e:
            print(f" Network error getting bot member: {e}")
            return {"success": False, "message": f"Network error: {str(e)}"}

        if bot_member_response.status_code != 200:
            print(f" Failed to get bot member: {bot_member_response.status_code}")
            return {"success": False, "message": "Failed to retrieve bot member information"}

        bot_member = bot_member_response.json()
        bot_roles = bot_member.get('roles', [])

        # Find the highest position of the bot's roles
        bot_highest_role_position = 0
        for role in roles:
            if role.get('id') in bot_roles and role.get('position', 0) > bot_highest_role_position:
                bot_highest_role_position = role.get('position', 0)

        print(f"\nBot's highest role position: {bot_highest_role_position}")

        # STEP 5: Find target role (by name or ID)
        print("\n5. Finding target role...")
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
                        f" Found role by name: '{target_role_name}' (ID: {target_role_id}, Position: {target_role_position})")
                    break

            if not target_role_id:
                print(f" No role found with name: '{role_name}'")
                return {"success": False, "message": f"Role '{role_name}' not found in server"}

        # STEP 6: Check role hierarchy
        print("\n6. Checking role hierarchy...")
        if target_role_position >= bot_highest_role_position:
            print(
                f" Role hierarchy issue: Bot's highest role ({bot_highest_role_position}) must be higher than the role to assign ({target_role_position})")
            return {"success": False, "message": "Bot's role is not high enough to assign this role"}

        print(" Bot's role position is higher than target role - hierarchy check passed")

        # STEP 7: Check if user already has the role
        print("\n7. Checking if user already has the role...")
        member_url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/members/{user_id}"

        try:
            member_response = requests.get(member_url, headers=headers, timeout=10)
        except requests.exceptions.RequestException as e:
            print(f" Network error getting member info: {e}")
            return {"success": False, "message": f"Network error: {str(e)}"}

        if member_response.status_code != 200:
            print(f" Failed to get member info: {member_response.status_code}")
            return {"success": False, "message": "Failed to retrieve member information"}

        member_data = member_response.json()
        member_roles = member_data.get('roles', [])

        if target_role_id in member_roles:
            print(f"User already has role '{target_role_name}'")
            return {"success": True, "message": f"User already has role '{target_role_name}'"}

        # STEP 8: Assign the role
        print("\n8. Attempting to assign role...")
        assign_url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/members/{user_id}/roles/{target_role_id}"

        try:
            assign_response = requests.put(assign_url, headers=headers, timeout=10)
        except requests.exceptions.RequestException as e:
            print(f" Network error during role assignment: {e}")
            return {"success": False, "message": f"Network error: {str(e)}"}

        if assign_response.status_code in [204, 200]:
            print(f" Role assignment successful! Status code: {assign_response.status_code}")
            return {"success": True, "message": f"Role '{target_role_name}' assigned successfully to {matched_name}"}
        else:
            print(f" Role assignment failed: {assign_response.status_code}")
            error_text = assign_response.text[:500] if assign_response.text else "No error details"
            print(f"Response: {error_text}")
            return {"success": False, "message": f"Failed to assign role (HTTP {assign_response.status_code})"}

    except Exception as e:
        import traceback
        print(f" Exception occurred: {str(e)}")
        traceback.print_exc()
        return {"success": False, "message": f"Unexpected error: {str(e)}"}
    finally:
        print("\n===== ROLE ASSIGNMENT COMPLETED =====\n")


# Legacy rank check API endpoint (keep for backwards compatibility)
@app.route('/api/rank-check', methods=['GET'])
def check_rank():
    """API endpoint to check Rocket League rank with Discord username verification"""
    platform = request.args.get('platform', '')
    username = request.args.get('username', '')
    discord_username = request.args.get('discord_username', '')
    discord_id = request.args.get('discord_id', '')
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
        print(f"Using MMR: {mmr}")

        manual_result = {
            "success": True,
            "username": username or "Manual Entry",
            "platform": platform or "unknown",
            "rank": manual_tier,
            "tier": manual_tier,
            "mmr": mmr,
            "global_mmr": 300,
            "timestamp": time.time(),
            "manual_verification": True
        }

        # Handle Discord role assignment if username provided
        role_result = {"success": False, "message": "No Discord username provided"}

        if discord_username:
            print(f"Storing manual rank data for Discord user: {discord_username}")
            store_rank_data(discord_username, username or discord_username, platform or "unknown", manual_result,
                            discord_id=discord_id)

            # Try the role assignment
            role_result = assign_discord_role(
                username=discord_username,
                role_name=manual_tier,
                discord_id=discord_id
            )

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

        role_result = assign_discord_role(
            username=discord_username,
            role_name=tier,
            discord_id=discord_id
        )

        mock_data["role_assignment"] = role_result

    return jsonify(mock_data)


def store_rank_data(discord_username, game_username, platform, rank_data, discord_id=None):
    """Store rank check data in the database"""
    try:
        print(f"Storing rank data for {discord_username} with MMR: {rank_data.get('mmr')}")
        print(f"Game username: {game_username}")

        # Create simplified rank document
        rank_document = {
            "discord_username": discord_username,
            "discord_id": discord_id,
            "game_username": game_username,
            "platform": platform,
            "rank": rank_data.get("rank"),
            "tier": rank_data.get("tier"),
            "mmr": rank_data.get("mmr"),
            "global_mmr": 300,
            "timestamp": datetime.datetime.utcnow()  # Fixed: Use datetime.datetime
        }

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


def get_mock_rank_data(username, platform):
    """Generate varied mock data for testing"""
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


@app.route('/api/reset-leaderboard', methods=['POST'])
def reset_leaderboard():
    """Reset leaderboard data including ranks"""
    # Check for authorization
    auth_token = request.headers.get('Authorization')
    if not auth_token or auth_token != os.getenv('ADMIN_TOKEN', 'admin-secret-token'):
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    try:
        # Get current counts for reporting
        player_count = players_collection.count_documents({})
        match_count = matches_collection.count_documents({})
        rank_count = ranks_collection.count_documents({})

        # Create backup collections with timestamp
        timestamp = datetime.datetime.utcnow()  # Fixed: Use datetime.datetime
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
        reset_timestamp = datetime.datetime.utcnow()  # Fixed: Use datetime.datetime

        # Store reset event in the resets collection
        resets_collection.insert_one({
            "type": "verification_reset",
            "timestamp": reset_timestamp,
            "performed_by": request.json.get("admin_id", "unknown") if request.json else "unknown",
            "reason": request.json.get("reason",
                                       "Rank verification reset") if request.json else "Rank verification reset"
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
            "timestamp": datetime.datetime.utcnow()  # Fixed: Use datetime.datetime
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


@app.route('/health')
def health_check():
    """Health check endpoint for monitoring"""
    try:
        # Test database connection
        client.admin.command('ping')
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"

    # Check environment variables
    env_check = {
        "discord_token": "✓" if DISCORD_TOKEN else "✗",
        "discord_guild_id": "✓" if DISCORD_GUILD_ID else "✗",
        "mongo_uri": "✓" if MONGO_URI else "✗",
        "rltracker_api_key": "✓" if RLTRACKER_API_KEY else "✗"
    }

    return jsonify({
        "status": "healthy",
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "database": db_status,
        "environment": env_check,
        "version": "1.0.0"
    })


@app.route('/test')
def test_route():
    """Simple test route to verify Flask is working"""
    return jsonify({
        "message": "Flask app is working!",
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "routes_working": True,
        "database_collections": {
            "players": players_collection.count_documents({}),
            "matches": matches_collection.count_documents({}),
            "ranks": ranks_collection.count_documents({})
        }
    })


@app.route('/status')
def status():
    """Detailed status information"""
    try:
        # Get collection counts
        player_count = players_collection.count_documents({})
        match_count = matches_collection.count_documents({})
        rank_count = ranks_collection.count_documents({})

        # Get recent activity
        recent_matches = matches_collection.count_documents({
            "completed_at": {
                "$gte": datetime.datetime.utcnow() - datetime.timedelta(hours=24)
            }
        })

        return jsonify({
            "app_status": "running",
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "statistics": {
                "total_players": player_count,
                "total_matches": match_count,
                "verified_ranks": rank_count,
                "matches_last_24h": recent_matches
            },
            "services": {
                "database": "connected",
                "discord_oauth": "configured" if DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET else "not configured",
                "bot_integration": "configured" if DISCORD_TOKEN else "not configured"
            }
        })

    except Exception as e:
        return jsonify({
            "app_status": "error",
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "error": str(e)
        }), 500


@app.route('/debug/environment')
def debug_environment():
    """Debug route to check environment variables (remove in production)"""
    return jsonify({
        "environment_variables": {
            "MONGO_URI": "Set" if MONGO_URI else "Not set",
            "DISCORD_TOKEN": f"Set ({len(DISCORD_TOKEN)} chars)" if DISCORD_TOKEN else "Not set",
            "DISCORD_GUILD_ID": DISCORD_GUILD_ID if DISCORD_GUILD_ID else "Not set",
            "DISCORD_CLIENT_ID": f"Set ({len(DISCORD_CLIENT_ID)} chars)" if DISCORD_CLIENT_ID else "Not set",
            "DISCORD_CLIENT_SECRET": "Set" if DISCORD_CLIENT_SECRET else "Not set",
            "DISCORD_REDIRECT_URI": DISCORD_REDIRECT_URI if DISCORD_REDIRECT_URI else "Not set",
            "RLTRACKER_API_KEY": "Set" if RLTRACKER_API_KEY else "Not set"
        },
        "warning": "This endpoint should be removed in production!"
    })


@app.route('/debug/routes')
def debug_routes():
    """Debug route to list all available routes"""
    routes = []
    for rule in app.url_map.iter_rules():
        routes.append({
            "endpoint": rule.endpoint,
            "methods": list(rule.methods),
            "rule": str(rule)
        })

    return jsonify({
        "total_routes": len(routes),
        "routes": sorted(routes, key=lambda x: x["rule"])
    })


@app.route('/api/debug/database')
def debug_database():
    """Debug route to check database collections"""
    try:
        # Test database connection
        client.admin.command('ping')

        # Get collection info
        collections_info = {}

        for collection_name, collection in [
            ("players", players_collection),
            ("matches", matches_collection),
            ("ranks", ranks_collection),
            ("resets", resets_collection)
        ]:
            try:
                count = collection.count_documents({})
                # Get a sample document if available
                sample = collection.find_one({}, {"_id": 0}) if count > 0 else None

                collections_info[collection_name] = {
                    "count": count,
                    "sample_fields": list(sample.keys()) if sample else []
                }
            except Exception as e:
                collections_info[collection_name] = {
                    "error": str(e)
                }

        return jsonify({
            "database_status": "connected",
            "collections": collections_info
        })

    except Exception as e:
        return jsonify({
            "database_status": "error",
            "error": str(e)
        }), 500

if __name__ == '__main__':
    import platform

    is_local = platform.system() in ['Windows', 'Darwin']

    print(" Starting Flask leaderboard app...")
    if is_local:
        print(" Local development mode")
        print(" Use Cloudflare tunnel for public access")

    port = int(os.environ.get('PORT', 5000))  # Changed from 10000 to 5000
    app.run(host='0.0.0.0', port=port, debug=is_local)