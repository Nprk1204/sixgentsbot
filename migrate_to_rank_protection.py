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


def migrate_to_rank_protection():
    """
    Add rank protection and momentum tracking fields to all existing players
    """
    print("Starting migration to add rank protection and momentum features...")

    # Count players needing migration
    players_without_protection = players_collection.count_documents({"last_promotion": {"$exists": False}})
    print(f"Found {players_without_protection} players needing rank protection fields...")

    # Step 1: Add basic rank protection fields to all players
    print("Step 1: Adding rank protection fields to all players...")
    result = players_collection.update_many(
        {"last_promotion": {"$exists": False}},
        {"$set": {
            "last_promotion": None,  # Will be set when player gets promoted
        }}
    )
    print(f"Added rank protection fields to {result.modified_count} players")

    # Step 2: For players who are currently above rank boundaries,
    # we need to determine if they were recently promoted
    print("Step 2: Analyzing recent promotions for existing high-rank players...")

    # Get all players with Rank A or B MMR
    high_rank_players = list(players_collection.find({
        "$or": [
            {"mmr": {"$gte": 1100}},  # Rank B or higher
        ]
    }))

    print(f"Found {len(high_rank_players)} players with Rank B+ MMR to analyze...")

    promotion_count = 0
    for player in high_rank_players:
        player_id = player.get('id')
        current_mmr = player.get('mmr', 0)
        current_matches = player.get('matches', 0)

        if not player_id or current_matches == 0:
            continue

        # Look at their recent matches to see if they crossed a boundary
        recent_matches = list(matches_collection.find(
            {"$or": [
                {"team1.id": player_id},
                {"team2.id": player_id}
            ], "status": "completed"}
        ).sort("completed_at", -1).limit(10))

        # Check if they crossed a rank boundary in recent matches
        promotion_found = False
        for i, match in enumerate(recent_matches):
            if not match.get('mmr_changes'):
                continue

            # Find this player's MMR change in this match
            player_mmr_change = None
            for mmr_change in match.get('mmr_changes', []):
                if mmr_change.get('player_id') == player_id and not mmr_change.get('is_global', False):
                    player_mmr_change = mmr_change
                    break

            if not player_mmr_change:
                continue

            old_mmr = player_mmr_change.get('old_mmr', 0)
            new_mmr = player_mmr_change.get('new_mmr', 0)

            # Check if they crossed 1100 (Rank C to B) or 1600 (Rank B to A)
            if (old_mmr < 1100 <= new_mmr) or (old_mmr < 1600 <= new_mmr):
                # They got promoted in this match!
                matches_at_promotion = current_matches - i
                promotion_data = {
                    "matches_at_promotion": matches_at_promotion,
                    "promoted_at": match.get('completed_at', datetime.datetime.utcnow()),
                    "from_rank": get_rank_from_mmr(old_mmr),
                    "to_rank": get_rank_from_mmr(new_mmr),
                    "mmr_at_promotion": new_mmr
                }

                # Update player with promotion data
                players_collection.update_one(
                    {"id": player_id},
                    {"$set": {"last_promotion": promotion_data}}
                )

                promotion_count += 1
                print(
                    f"Found promotion for {player.get('name', 'Unknown')}: {promotion_data['from_rank']} -> {promotion_data['to_rank']} ({i} matches ago)")
                promotion_found = True
                break

        if not promotion_found and current_mmr >= 1100:
            # Player is high rank but we couldn't find recent promotion
            # This means they were probably promoted long ago, so no protection needed
            print(
                f"Player {player.get('name', 'Unknown')} is {get_rank_from_mmr(current_mmr)} but no recent promotion found")

    print(f"Step 2 complete: Found and recorded {promotion_count} recent promotions")

    # Step 3: Verify the migration worked
    print("Step 3: Verifying migration...")

    # Count players with new fields
    players_with_protection = players_collection.count_documents({"last_promotion": {"$exists": True}})
    recent_promotions = players_collection.count_documents({"last_promotion": {"$ne": None}})

    print(f"✅ Migration complete!")
    print(f"   - Players with rank protection fields: {players_with_protection}")
    print(f"   - Players with recent promotion data: {recent_promotions}")
    print(f"   - Players eligible for promotion protection: {recent_promotions}")

    return {
        "total_players_updated": players_with_protection,
        "recent_promotions_found": recent_promotions
    }


def get_rank_from_mmr(mmr):
    """Helper function to determine rank from MMR"""
    if mmr >= 1600:
        return "Rank A"
    elif mmr >= 1100:
        return "Rank B"
    else:
        return "Rank C"


def test_rank_protection():
    """
    Test function to verify rank protection is working
    """
    print("\n" + "=" * 50)
    print("TESTING RANK PROTECTION SYSTEM")
    print("=" * 50)

    # Find players who should have promotion protection
    recently_promoted = list(players_collection.find({
        "last_promotion": {"$ne": None}
    }))

    print(f"Found {len(recently_promoted)} players with promotion data:")

    for player in recently_promoted:
        promotion = player.get('last_promotion', {})
        current_matches = player.get('matches', 0)
        matches_at_promotion = promotion.get('matches_at_promotion', 0)
        games_since = current_matches - matches_at_promotion

        protection_status = "🛡️ PROTECTED" if games_since < 3 else "⚡ No protection"

        print(f"  • {player.get('name', 'Unknown')}: {promotion.get('from_rank')} → {promotion.get('to_rank')}")
        print(f"    Games since promotion: {games_since} | {protection_status}")

    print("\n" + "=" * 50)


if __name__ == "__main__":
    # Run the migration
    result = migrate_to_rank_protection()

    # Test the system
    test_rank_protection()

    print(f"\n🎉 Rank protection migration completed successfully!")
    print(
        f"📊 Summary: {result['total_players_updated']} players updated, {result['recent_promotions_found']} recent promotions found")