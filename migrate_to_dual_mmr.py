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
ranks_collection = db['ranks']


def migrate_to_dual_mmr():
    """Add global_mmr field to all existing players"""
    print("Starting migration to dual MMR system...")

    # Count players needing migration
    players_without_global = players_collection.count_documents({"global_mmr": {"$exists": False}})
    print(f"Found {players_without_global} players needing global MMR fields...")

    # Update players collection
    player_result = players_collection.update_many(
        {"global_mmr": {"$exists": False}},
        {"$set": {
            "global_mmr": 300,
            "global_wins": 0,
            "global_losses": 0,
            "global_matches": 0
        }}
    )

    print(f"Updated {player_result.modified_count} players with global MMR fields")

    # Update ranks collection
    ranks_without_global = ranks_collection.count_documents({"global_mmr": {"$exists": False}})
    print(f"Found {ranks_without_global} rank records needing global MMR fields...")

    ranks_result = ranks_collection.update_many(
        {"global_mmr": {"$exists": False}},
        {"$set": {"global_mmr": 300}}
    )

    print(f"Updated {ranks_result.modified_count} rank records with global MMR")

    # Add is_global flag to matches
    matches_without_flag = matches_collection.count_documents({"is_global": {"$exists": False}})
    print(f"Found {matches_without_flag} matches needing is_global flag...")

    # For existing matches, determine if they're global based on channel name
    global_channels = []
    channels = matches_collection.distinct("channel_id")

    for channel_id in channels:
        # Look up a match with this channel to check the context
        match = matches_collection.find_one({"channel_id": channel_id})
        if match:
            # Here we'd typically check for channel name, but we don't have direct access
            # So we'll provide a way to manually specify global channels
            channel_name = input(f"Is channel ID {channel_id} the global channel? (y/n): ")
            if channel_name.lower() == 'y':
                global_channels.append(channel_id)

    # Update the matches
    for channel_id in global_channels:
        result = matches_collection.update_many(
            {"channel_id": channel_id, "is_global": {"$exists": False}},
            {"$set": {"is_global": True}}
        )
        print(f"Updated {result.modified_count} matches in channel {channel_id} as global")

    # Set remaining matches as non-global
    result = matches_collection.update_many(
        {"is_global": {"$exists": False}},
        {"$set": {"is_global": False}}
    )
    print(f"Updated {result.modified_count} remaining matches as non-global")

    print("Migration complete!")


if __name__ == "__main__":
    migrate_to_dual_mmr()