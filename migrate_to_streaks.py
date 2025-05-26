from pymongo import MongoClient
import os
from dotenv import load_dotenv
import datetime

# Load environment variables
load_dotenv()
MONGO_URI = os.getenv('MONGO_URI')

# Connect to MongoDB
client = MongoClient(MONGO_URI)
db = client['sixgents_db']
players_collection = db['players']
matches_collection = db['matches']


def migrate_to_streaks():
    """Add streak tracking fields to all existing players - ENHANCED VERSION"""
    print("Starting enhanced migration to add complete streak tracking...")

    # Count players needing migration
    players_without_streaks = players_collection.count_documents({"current_streak": {"$exists": False}})
    print(f"Found {players_without_streaks} players needing streak fields...")

    # ALSO count players missing global streaks
    players_without_global_streaks = players_collection.count_documents({"global_current_streak": {"$exists": False}})
    print(f"Found {players_without_global_streaks} players needing global streak fields...")

    # First, add ALL streak fields to players that don't have them
    print("Step 1: Adding basic streak fields to all players...")
    result = players_collection.update_many(
        {"current_streak": {"$exists": False}},
        {"$set": {
            # Ranked streaks
            "current_streak": 0,
            "longest_win_streak": 0,
            "longest_loss_streak": 0,
            # Global streaks - THESE WERE MISSING
            "global_current_streak": 0,
            "global_longest_win_streak": 0,
            "global_longest_loss_streak": 0
        }}
    )
    print(f"Added basic streak fields to {result.modified_count} players")

    # Second, add global streak fields to existing players who might not have them
    print("Step 2: Adding global streak fields to existing players...")
    global_result = players_collection.update_many(
        {"global_current_streak": {"$exists": False}},
        {"$set": {
            "global_current_streak": 0,
            "global_longest_win_streak": 0,
            "global_longest_loss_streak": 0
        }}
    )
    print(f"Added global streak fields to {global_result.modified_count} players")

    # Get all players that need streak calculation
    players_to_update = list(players_collection.find({}))
    print(f"Calculating streaks for {len(players_to_update)} total players...")

    updated_count = 0

    # Process each player
    for player in players_to_update:
        player_id = player.get('id')
        if not player_id:
            continue

        print(f"Processing player: {player.get('name', 'Unknown')} (ID: {player_id})")

        # Get ALL matches for this player (both ranked and global)
        all_matches = list(matches_collection.find(
            {"$or": [
                {"team1.id": player_id},
                {"team2.id": player_id}
            ], "status": "completed"}
        ).sort("completed_at", -1).limit(50))

        # Separate ranked and global matches
        ranked_matches = [m for m in all_matches if not m.get("is_global", False)]
        global_matches = [m for m in all_matches if m.get("is_global", False)]

        # Calculate RANKED streaks
        ranked_current_streak = 0
        ranked_longest_win_streak = 0
        ranked_longest_loss_streak = 0

        if ranked_matches:
            # Current streak calculation
            for i, match in enumerate(ranked_matches):
                player_won = calculate_if_player_won(match, player_id)

                if i == 0:  # Most recent match
                    ranked_current_streak = 1 if player_won else -1
                    current_streak_type = "win" if player_won else "loss"
                elif (current_streak_type == "win" and player_won) or (
                        current_streak_type == "loss" and not player_won):
                    # Continue streak
                    if player_won:
                        ranked_current_streak += 1
                    else:
                        ranked_current_streak -= 1
                else:
                    # Streak broken
                    break

            # Longest streaks calculation
            temp_win_streak = 0
            temp_loss_streak = 0
            for match in ranked_matches:
                player_won = calculate_if_player_won(match, player_id)

                if player_won:
                    temp_win_streak += 1
                    temp_loss_streak = 0
                    ranked_longest_win_streak = max(ranked_longest_win_streak, temp_win_streak)
                else:
                    temp_loss_streak -= 1
                    temp_win_streak = 0
                    ranked_longest_loss_streak = min(ranked_longest_loss_streak, temp_loss_streak)

        # Calculate GLOBAL streaks
        global_current_streak = 0
        global_longest_win_streak = 0
        global_longest_loss_streak = 0

        if global_matches:
            # Current streak calculation
            for i, match in enumerate(global_matches):
                player_won = calculate_if_player_won(match, player_id)

                if i == 0:  # Most recent match
                    global_current_streak = 1 if player_won else -1
                    current_streak_type = "win" if player_won else "loss"
                elif (current_streak_type == "win" and player_won) or (
                        current_streak_type == "loss" and not player_won):
                    # Continue streak
                    if player_won:
                        global_current_streak += 1
                    else:
                        global_current_streak -= 1
                else:
                    # Streak broken
                    break

            # Longest streaks calculation
            temp_win_streak = 0
            temp_loss_streak = 0
            for match in global_matches:
                player_won = calculate_if_player_won(match, player_id)

                if player_won:
                    temp_win_streak += 1
                    temp_loss_streak = 0
                    global_longest_win_streak = max(global_longest_win_streak, temp_win_streak)
                else:
                    temp_loss_streak -= 1
                    temp_win_streak = 0
                    global_longest_loss_streak = min(global_longest_loss_streak, temp_loss_streak)

        # Update player with ALL streak data
        result = players_collection.update_one(
            {"id": player_id},
            {"$set": {
                # Ranked streaks
                "current_streak": ranked_current_streak,
                "longest_win_streak": ranked_longest_win_streak,
                "longest_loss_streak": ranked_longest_loss_streak,
                # Global streaks
                "global_current_streak": global_current_streak,
                "global_longest_win_streak": global_longest_win_streak,
                "global_longest_loss_streak": global_longest_loss_streak
            }}
        )

        if result.modified_count > 0:
            updated_count += 1
            print(f"Updated {player.get('name', 'Unknown')} with streaks:")
            print(
                f"  Ranked: current={ranked_current_streak}, best_win={ranked_longest_win_streak}, worst_loss={ranked_longest_loss_streak}")
            print(
                f"  Global: current={global_current_streak}, best_win={global_longest_win_streak}, worst_loss={global_longest_loss_streak}")

    print(f"Enhanced migration complete! Updated {updated_count} players with complete streak information.")


def calculate_if_player_won(match, player_id):
    """Helper function to determine if player won the match"""
    player_in_team1 = any(p.get("id") == player_id for p in match.get("team1", []))
    winner = match.get("winner")
    return (player_in_team1 and winner == 1) or (not player_in_team1 and winner == 2)


if __name__ == "__main__":
    migrate_to_streaks()