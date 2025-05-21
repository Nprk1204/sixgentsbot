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
    """Add streak tracking fields to all existing players"""
    print("Starting migration to add streak tracking...")

    # Count players needing migration
    players_without_streaks = players_collection.count_documents({"current_streak": {"$exists": False}})
    print(f"Found {players_without_streaks} players needing streak fields...")

    # Get all players that need updating
    players_to_update = list(players_collection.find({"current_streak": {"$exists": False}}))

    # Initialize a counter for tracking progress
    updated_count = 0

    # Process each player
    for player in players_to_update:
        player_id = player.get('id')

        if not player_id:
            print(f"Skipping player without ID: {player}")
            continue

        print(f"Processing player: {player.get('name', 'Unknown')} (ID: {player_id})")

        # Calculate current streak by looking at recent matches
        matches = list(matches_collection.find(
            {"$or": [
                {"team1.id": player_id},
                {"team2.id": player_id}
            ], "status": "completed"}
        ).sort("completed_at", -1).limit(30))  # Look at last 30 matches

        # Initialize streak values
        current_streak = 0
        longest_win_streak = 0
        longest_loss_streak = 0
        current_streak_type = None  # None, "win", or "loss"
        temp_win_streak = 0
        temp_loss_streak = 0

        # Calculate streaks
        if matches:
            for match in matches:
                # Determine if player won or lost this match
                player_in_team1 = False
                for p in match.get("team1", []):
                    if p.get("id") == player_id:
                        player_in_team1 = True
                        break

                winner = match.get("winner")

                # Calculate if player won or lost
                player_won = (player_in_team1 and winner == 1) or (not player_in_team1 and winner == 2)

                # Process current streak
                if matches.index(match) == 0:
                    # This is the most recent match
                    if player_won:
                        current_streak = 1
                        current_streak_type = "win"
                    else:
                        current_streak = -1
                        current_streak_type = "loss"
                elif current_streak_type == "win" and player_won:
                    # Continuing a win streak
                    current_streak += 1
                elif current_streak_type == "loss" and not player_won:
                    # Continuing a loss streak
                    current_streak -= 1
                else:
                    # Streak is broken, no need to process further for current streak
                    break

                # Process longest streaks (for all matches)
                if player_won:
                    temp_win_streak += 1
                    temp_loss_streak = 0
                    longest_win_streak = max(longest_win_streak, temp_win_streak)
                else:
                    temp_loss_streak -= 1
                    temp_win_streak = 0
                    longest_loss_streak = min(longest_loss_streak, temp_loss_streak)

        # Update the player record with calculated streaks
        result = players_collection.update_one(
            {"id": player_id},
            {"$set": {
                "current_streak": current_streak,
                "longest_win_streak": longest_win_streak,
                "longest_loss_streak": longest_loss_streak
            }}
        )

        if result.modified_count > 0:
            updated_count += 1
            print(f"Updated player {player.get('name', 'Unknown')} with streaks: " +
                  f"current={current_streak}, best_win={longest_win_streak}, worst_loss={longest_loss_streak}")
        else:
            print(f"Failed to update player {player.get('name', 'Unknown')}")

    print(f"Migration complete! Updated {updated_count} players with streak information.")


if __name__ == "__main__":
    migrate_to_streaks()