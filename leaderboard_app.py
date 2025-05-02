from flask import Flask, render_template, jsonify
from pymongo import MongoClient
from pymongo.server_api import ServerApi
import os
from dotenv import load_dotenv

app = Flask(__name__)

# Load environment variables
load_dotenv()
MONGO_URI = os.getenv('MONGO_URI')

# Connect to MongoDB
client = MongoClient(MONGO_URI, server_api=ServerApi('1'))
db = client['sixgents_db']
players_collection = db['players']


@app.route('/')
def home():
    """Display the home page with leaderboard"""
    return render_template('index.html')


@app.route('/api/leaderboard')
def get_leaderboard():
    """API endpoint to get leaderboard data"""
    top_players = list(players_collection.find({}, {
        "_id": 0,
        "id": 1,
        "name": 1,
        "mmr": 1,
        "wins": 1,
        "losses": 1,
        "matches": 1
    }).sort("mmr", -1).limit(100))

    # Calculate additional stats
    for player in top_players:
        matches = player.get("matches", 0)
        wins = player.get("wins", 0)

        player["win_rate"] = round((wins / matches) * 100, 2) if matches > 0 else 0

    return jsonify(top_players)


@app.route('/api/player/<player_id>')
def get_player(player_id):
    """API endpoint to get player data"""
    player = players_collection.find_one({"id": player_id}, {"_id": 0})

    if not player:
        return jsonify({"error": "Player not found"}), 404

    # Calculate additional stats
    matches = player.get("matches", 0)
    wins = player.get("wins", 0)

    player["win_rate"] = round((wins / matches) * 100, 2) if matches > 0 else 0

    return jsonify(player)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)