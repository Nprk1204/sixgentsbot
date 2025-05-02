import discord
from discord.ext import commands
import datetime
import uuid


class MatchSystem:
    def __init__(self, db):
        self.db = db
        self.matches = db.get_collection('matches')
        self.players = db.get_collection('players')
        self.active_matches = {}  # Store active matches in memory

    def create_match(self, match_id, team1, team2, channel_id):
        """Create a new match entry"""
        # Generate a shorter match ID that's easier for users to type
        short_id = str(uuid.uuid4().hex)[:6]  # Just use first 6 characters of a UUID

        match_data = {
            "match_id": short_id,  # Use the shorter ID
            "team1": team1,
            "team2": team2,
            "status": "in_progress",
            "winner": None,
            "score": {"team1": 0, "team2": 0},
            "channel_id": channel_id,
            "created_at": datetime.datetime.utcnow(),
            "completed_at": None,
            "reported_by": None
        }

        # Store in database
        self.matches.insert_one(match_data)

        # Store in memory for quick access
        self.active_matches[short_id] = match_data

        return short_id  # Return the short ID

    def get_active_match_by_channel(self, channel_id):
        """Get active match by channel ID"""
        match = self.matches.find_one({"channel_id": channel_id, "status": "in_progress"})
        return match

    def report_match_by_id(self, match_id, reporter_id, result):
        """Report a match result by match ID and win/loss"""
        # Find the match by ID
        match = self.matches.find_one({"match_id": match_id})

        if not match:
            return None, "No match found with that ID."

        if match["status"] != "in_progress":
            return None, "This match has already been reported."

        # Get player ID to determine which team they're on
        player_id = reporter_id

        # Check if reporter is in either team
        team1_ids = [p["id"] for p in match["team1"]]
        team2_ids = [p["id"] for p in match["team2"]]

        if player_id in team1_ids:
            reporter_team = 1
        elif player_id in team2_ids:
            reporter_team = 2
        else:
            return None, "You must be a player in this match to report results."

        # Determine winner based on reporter's team and their reported result
        if result.lower() == "win":
            winner = reporter_team
        elif result.lower() == "loss":
            winner = 2 if reporter_team == 1 else 1
        else:
            return None, "Invalid result. Please use 'win' or 'loss'."

        # Set scores (simplified to 1-0 or 0-1)
        if winner == 1:
            team1_score = 1
            team2_score = 0
        else:
            team1_score = 0
            team2_score = 1

        # Update match data
        self.matches.update_one(
            {"match_id": match_id},
            {"$set": {
                "status": "completed",
                "winner": winner,
                "score": {"team1": team1_score, "team2": team2_score},
                "completed_at": datetime.datetime.utcnow(),
                "reported_by": reporter_id
            }}
        )

        # Update MMR for all players
        if winner == 1:
            winning_team = match["team1"]
            losing_team = match["team2"]
        else:
            winning_team = match["team2"]
            losing_team = match["team1"]

        # Update MMR
        self.update_player_mmr(winning_team, losing_team)

        # Remove from active matches
        if match["match_id"] in self.active_matches:
            del self.active_matches[match["match_id"]]

        return match, None

    def update_player_mmr(self, winning_team, losing_team):
        """Update MMR for all players in the match"""
        # MMR gain/loss values
        MMR_GAIN = 15
        MMR_LOSS = 12

        # Update winners
        for player in winning_team:
            player_id = player["id"]

            # Get player data or create new
            player_data = self.players.find_one({"id": player_id})

            if player_data:
                # Update existing player
                new_mmr = player_data.get("mmr", 1000) + MMR_GAIN
                wins = player_data.get("wins", 0) + 1
                matches = player_data.get("matches", 0) + 1

                self.players.update_one(
                    {"id": player_id},
                    {"$set": {
                        "mmr": new_mmr,
                        "wins": wins,
                        "matches": matches,
                        "last_updated": datetime.datetime.utcnow()
                    }}
                )
            else:
                # Create new player
                self.players.insert_one({
                    "id": player_id,
                    "name": player["name"],
                    "mmr": 1000 + MMR_GAIN,
                    "wins": 1,
                    "losses": 0,
                    "matches": 1,
                    "created_at": datetime.datetime.utcnow(),
                    "last_updated": datetime.datetime.utcnow()
                })

        # Update losers
        for player in losing_team:
            player_id = player["id"]

            # Get player data or create new
            player_data = self.players.find_one({"id": player_id})

            if player_data:
                # Update existing player
                new_mmr = max(0, player_data.get("mmr", 1000) - MMR_LOSS)  # Don't go below 0
                losses = player_data.get("losses", 0) + 1
                matches = player_data.get("matches", 0) + 1

                self.players.update_one(
                    {"id": player_id},
                    {"$set": {
                        "mmr": new_mmr,
                        "losses": losses,
                        "matches": matches,
                        "last_updated": datetime.datetime.utcnow()
                    }}
                )
            else:
                # Create new player
                self.players.insert_one({
                    "id": player_id,
                    "name": player["name"],
                    "mmr": max(0, 1000 - MMR_LOSS),
                    "wins": 0,
                    "losses": 1,
                    "matches": 1,
                    "created_at": datetime.datetime.utcnow(),
                    "last_updated": datetime.datetime.utcnow()
                })

    def get_leaderboard(self, limit=10):
        """Get top players by MMR"""
        leaderboard = list(self.players.find().sort("mmr", -1).limit(limit))
        return leaderboard

    def get_player_stats(self, player_id):
        """Get stats for a specific player"""
        player = self.players.find_one({"id": player_id})
        return player