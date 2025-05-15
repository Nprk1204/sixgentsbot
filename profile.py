# profile.py
from flask import Blueprint, render_template, jsonify, request, session, redirect, url_for
import uuid
import datetime
from pymongo import MongoClient
import os
from dotenv import load_dotenv

# Load environment variables if needed
load_dotenv()

# Create a Blueprint for profile routes
profile_bp = Blueprint('profile', __name__)


# Setup database connection - this will be accessed via app context
def get_db():
    return profile_bp.app.db


# Route for the profile page
@profile_bp.route('/profile')
def profile_page():
    """Display the player profile page"""
    # Check if user is logged in
    if 'user_id' not in session:
        return render_template('profile.html', logged_in=False)

    # Get user data
    user_id = session['user_id']
    username = session.get('username', '')

    return render_template('profile.html', logged_in=True, username=username)


# API endpoint for user login
@profile_bp.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '').strip()

    # For demo purposes, allow any username
    if username:
        user_id = str(uuid.uuid4())

        # Set user data in session
        session.permanent = True
        session['user_id'] = user_id
        session['username'] = username
        return jsonify({"success": True})

    return jsonify({"success": False, "message": "Username is required"}), 400


# API endpoint for user logout
@profile_bp.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({"success": True})


# API endpoint to get player profile data
@profile_bp.route('/api/profile', methods=['GET'])
def get_profile():
    """Get current user's profile data"""
    # Get database access
    db = get_db()
    players_collection = db['players']
    matches_collection = db['matches']

    # Check if user is logged in
    if 'user_id' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    # Get username from session
    username = session.get('username', '')

    # Try to find player data in database
    player_data = players_collection.find_one({"name": username})

    # If not found, create placeholder
    if not player_data:
        player_data = {
            "id": str(uuid.uuid4()),
            "name": username,
            "mmr": 0,
            "global_mmr": 0,
            "wins": 0,
            "losses": 0,
            "matches": 0,
            "global_wins": 0,
            "global_losses": 0,
            "global_matches": 0,
            "rank_tier": "Unranked",
            "recent_matches": []
        }

    # Format the data
    formatted_data = {
        "id": player_data.get("id", ""),
        "name": player_data.get("name", username),
        "mmr": player_data.get("mmr", 0),
        "global_mmr": player_data.get("global_mmr", 0),
        "wins": player_data.get("wins", 0),
        "losses": player_data.get("losses", 0),
        "matches": player_data.get("matches", 0),
        "global_wins": player_data.get("global_wins", 0),
        "global_losses": player_data.get("global_losses", 0),
        "global_matches": player_data.get("global_matches", 0)
    }

    # Calculate win rates
    if formatted_data["matches"] > 0:
        formatted_data["win_rate"] = round((formatted_data["wins"] / formatted_data["matches"]) * 100, 1)
    else:
        formatted_data["win_rate"] = 0

    if formatted_data["global_matches"] > 0:
        formatted_data["global_win_rate"] = round(
            (formatted_data["global_wins"] / formatted_data["global_matches"]) * 100, 1)
    else:
        formatted_data["global_win_rate"] = 0

    # Determine rank tier based on MMR
    if formatted_data["mmr"] >= 1600:
        formatted_data["rank_tier"] = "Rank A"
    elif formatted_data["mmr"] >= 1100:
        formatted_data["rank_tier"] = "Rank B"
    else:
        formatted_data["rank_tier"] = "Rank C"

    # Check queue status
    queue_status = {
        "inQueue": False,
        "channel": "",
        "players": 0,
        "totalPlayers": 6
    }

    # In a real implementation, check the queue collection
    queue_entry = db['queue'].find_one({"name": username})
    if queue_entry:
        queue_status["inQueue"] = True
        queue_status["channel"] = queue_entry.get("channel_id", "")

        # Count players in that channel's queue
        channel_id = queue_entry.get("channel_id")
        if channel_id:
            queue_count = db['queue'].count_documents({"channel_id": channel_id})
            queue_status["players"] = queue_count

    # Get recent matches (last 10)
    recent_matches = list(matches_collection.find(
        {"$or": [
            {"team1.name": username},
            {"team2.name": username}
        ], "status": "completed"},
        {"_id": 0}
    ).sort("completed_at", -1).limit(10))

    # Format match data
    formatted_matches = []
    for match in recent_matches:
        # Check if player is in team1 or team2
        in_team1 = any(p.get("name") == username for p in match.get("team1", []))

        winner = match.get("winner")
        if (in_team1 and winner == 1) or (not in_team1 and winner == 2):
            player_result = "Win"
        else:
            player_result = "Loss"

        formatted_match = {
            "date": match.get("completed_at").strftime("%Y-%m-%d") if match.get("completed_at") else "Unknown",
            "player_result": player_result,
            "match_id": match.get("match_id", ""),
            "team1": match.get("team1", []),
            "team2": match.get("team2", []),
            "is_global": match.get("is_global", False)
        }

        formatted_matches.append(formatted_match)

    formatted_data["recent_matches"] = formatted_matches

    return jsonify({
        "success": True,
        "player": formatted_data,
        "queue_status": queue_status
    })