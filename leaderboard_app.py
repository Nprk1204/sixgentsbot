from flask import Flask, render_template, jsonify, request, abort
from pymongo import MongoClient
from pymongo.server_api import ServerApi
import os
from dotenv import load_dotenv
from functools import wraps
import datetime


class SimpleCache:
    def __init__(self, default_timeout=300):
        self.cache = {}
        self.default_timeout = default_timeout

    def get(self, key):
        item = self.cache.get(key, None)
        if item is None:
            return None
        if item['expiry'] is not None and item['expiry'] <= datetime.datetime.now().timestamp():
            del self.cache[key]
            return None
        return item['value']

    def set(self, key, value, timeout=None):
        if timeout is None:
            timeout = self.default_timeout

        expiry = None
        if timeout > 0:
            expiry = datetime.datetime.now().timestamp() + timeout

        self.cache[key] = {
            'value': value,
            'expiry': expiry
        }
        return True

# Initialize app
app = Flask(__name__)
cache = SimpleCache()

# Load environment variables
load_dotenv()
MONGO_URI = os.getenv('MONGO_URI')

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
    # You could implement a fallback strategy here

# Cache decorator
def cached(timeout=5 * 60, key='view/%s'):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            cache_key = key % request.path
            rv = cache.get(cache_key)
            if rv is not None:
                return rv
            rv = f(*args, **kwargs)
            cache.set(cache_key, rv, timeout=timeout)
            return rv
        return decorated_function
    return decorator

@app.route('/')
@cached(timeout=60)  # Cache for 1 minute
def home():
    """Display the home page with leaderboard"""
    return render_template('index.html')

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
            team1_ids = [p.get("id") for p in match.get("team1", [])]
            player_in_team1 = player_id in team1_ids
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

@app.errorhandler(404)
def page_not_found(e):
    return jsonify({"error": "Resource not found"}), 404

@app.errorhandler(500)
def internal_server_error(e):
    return jsonify({"error": "Internal server error"}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)