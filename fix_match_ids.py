import os
from dotenv import load_dotenv
from pymongo import MongoClient
import uuid

# Load environment variables
load_dotenv()
MONGO_URI = os.getenv('MONGO_URI')

# Connect to MongoDB
client = MongoClient(MONGO_URI)
db = client['sixgents_db']
matches_collection = db['matches']
active_matches_collection = db['active_matches']  # If you use a separate collection for active matches

print("Starting match ID format fix...")


def fix_collection(collection_name, collection):
    """Fix match IDs in a specific collection"""
    all_matches = list(collection.find({}))
    fixed_count = 0

    print(f"Checking {len(all_matches)} matches in {collection_name}...")

    for match in all_matches:
        match_id = match.get("match_id", "")

        # Skip if match_id doesn't exist
        if not match_id:
            continue

        # Check if match_id doesn't follow the 6-character format
        if len(match_id) != 6:
            # Generate a new 6-character ID
            original_id = match_id
            new_id = str(uuid.uuid4().hex)[:6]

            print(f"Fixing match ID: {original_id} -> {new_id}")

            # Update the match record
            collection.update_one(
                {"_id": match["_id"]},
                {"$set": {"match_id": new_id}}
            )
            fixed_count += 1

    return fixed_count


# Fix the main matches collection
fixed_matches = fix_collection("matches", matches_collection)

# Fix the active matches collection if separate
fixed_active = fix_collection("active_matches", active_matches_collection)

print(f"Fixed {fixed_matches} match IDs in main collection")
print(f"Fixed {fixed_active} match IDs in active matches collection")
print("Done!")