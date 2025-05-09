import math
import discord
from discord.ext import commands
import datetime
import uuid

from leaderboard_app import db


class MatchSystem:
    def __init__(self, db):
        self.db = db
        self.matches = db.get_collection('matches')
        self.players = db.get_collection('players')
        self.active_matches = {}  # Store active matches in memory

        # Simplified - keep just the three tier-based MMR values
        self.TIER_MMR = {
            "Rank A": 1600,  # Grand Champion I and above
            "Rank B": 1100,  # Champion I to Champion III
            "Rank C": 600  # Diamond III and below - default
        }

    def create_match(self, match_id, team1, team2, channel_id):
        """Create a new match entry"""
        # Generate a shorter match ID that's easier for users to type
        short_id = str(uuid.uuid4().hex)[:6]  # Just use first 6 characters of a UUID

        match_data = {
            "match_id": short_id,  # Use the shorter ID
            "team1": team1,
            "team2": team2,
            "status": "in_progress",  # Make sure this is set correctly
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

        # Debug print to confirm match creation
        print(f"Created match with ID: {short_id}, status: {match_data['status']}")

        return short_id  # Return the short ID

    def get_active_match_by_channel(self, channel_id):
        """Get active match by channel ID"""
        match = self.matches.find_one({"channel_id": channel_id, "status": "in_progress"})
        return match

    async def report_match_by_id(self, match_id, reporter_id, result, ctx=None):
        """Report a match result by match ID and win/loss"""
        # Find the match by ID
        match = self.matches.find_one({"match_id": match_id})

        if not match:
            return None, "No match found with that ID."

        # Debug print to troubleshoot
        print(f"Reporting match {match_id}, current status: {match.get('status')}")

        # Make sure we're checking for "in_progress" status correctly
        if match.get("status") != "in_progress":
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

        # Update match data with a timestamp to ensure it's updated correctly
        result = self.matches.update_one(
            {"match_id": match_id, "status": "in_progress"},  # Only update if still in progress
            {"$set": {
                "status": "completed",
                "winner": winner,
                "score": {"team1": team1_score, "team2": team2_score},
                "completed_at": datetime.datetime.utcnow(),
                "reported_by": reporter_id
            }}
        )

        # Check if the update was successful
        if result.modified_count == 0:
            # This means the match wasn't updated - either doesn't exist or already reported
            # Double check if it exists but is already completed
            completed_match = self.matches.find_one({"match_id": match_id, "status": "completed"})
            if completed_match:
                return None, "This match has already been reported."
            else:
                return None, "Failed to update match. Please check the match ID."

        # Now get the updated match document
        updated_match = self.matches.find_one({"match_id": match_id})

        # Update MMR for all players
        if winner == 1:
            winning_team = match["team1"]
            losing_team = match["team2"]
        else:
            winning_team = match["team2"]
            losing_team = match["team1"]

        # Track MMR changes for each player
        mmr_changes = []

        # Update MMR for winners
        for player in winning_team:
            player_id = player["id"]

            # Skip dummy players
            if player_id.startswith('9000'):
                continue

            # Get player data or create new
            player_data = self.players.find_one({"id": player_id})

            if player_data:
                # Existing player logic remains the same
                matches_played = player_data.get("matches", 0) + 1
                wins = player_data.get("wins", 0) + 1
                mmr_gain = self.calculate_dynamic_mmr(matches_played, is_win=True)
                old_mmr = player_data.get("mmr", 600)
                new_mmr = old_mmr + mmr_gain
                print(f"Player {player['name']} MMR update: {old_mmr} + {mmr_gain} = {new_mmr}")

                self.players.update_one(
                    {"id": player_id},
                    {"$set": {
                        "mmr": new_mmr,
                        "wins": wins,
                        "matches": matches_played,
                        "last_updated": datetime.datetime.utcnow()
                    }}
                )

                # Track MMR change
                mmr_changes.append({
                    "player_id": player_id,
                    "old_mmr": old_mmr,
                    "new_mmr": new_mmr,
                    "mmr_change": mmr_gain,
                    "is_win": True
                })
            else:
                # Look up player's rank in ranks collection
                print(f"New player {player['name']} (ID: {player_id}), determining starting MMR")

                # Try to find rank record
                rank_record = db.get_collection('ranks').find_one({"discord_id": player_id})
                if not rank_record:
                    rank_record = db.get_collection('ranks').find_one({"discord_username": player["name"]})

                # Default values
                starting_mmr = 600  # Default MMR

                if rank_record:
                    print(f"Found rank record: {rank_record}")

                    # Simplified logic - just use tier-based MMR
                    tier = rank_record.get("tier", "Rank C")
                    starting_mmr = self.TIER_MMR.get(tier, 600)
                    print(f"Using tier-based MMR for {tier}: {starting_mmr}")
                else:
                    print(f"No rank record found, using default MMR: {starting_mmr}")

                # Calculate first win MMR
                mmr_gain = self.calculate_dynamic_mmr(1, is_win=True)
                new_mmr = starting_mmr + mmr_gain
                print(f"NEW PLAYER {player['name']} FIRST WIN: {starting_mmr} + {mmr_gain} = {new_mmr}")

                self.players.insert_one({
                    "id": player_id,
                    "name": player["name"],
                    "mmr": new_mmr,
                    "wins": 1,
                    "losses": 0,
                    "matches": 1,
                    "created_at": datetime.datetime.utcnow(),
                    "last_updated": datetime.datetime.utcnow()
                })

                # Track MMR change for new player
                mmr_changes.append({
                    "player_id": player_id,
                    "old_mmr": starting_mmr,
                    "new_mmr": new_mmr,
                    "mmr_change": mmr_gain,
                    "is_win": True
                })

        # Update MMR for losing team
        for player in losing_team:
            player_id = player["id"]

            # Skip dummy players
            if player_id.startswith('9000'):
                continue

            # Get player data or create new
            player_data = self.players.find_one({"id": player_id})

            if player_data:
                # Update existing player
                matches_played = player_data.get("matches", 0) + 1
                losses = player_data.get("losses", 0) + 1
                mmr_loss = self.calculate_dynamic_mmr(matches_played, is_win=False)
                old_mmr = player_data.get("mmr", 600)
                new_mmr = max(0, old_mmr - mmr_loss)  # Don't go below 0
                print(f"Player {player['name']} MMR update: {old_mmr} - {mmr_loss} = {new_mmr}")

                self.players.update_one(
                    {"id": player_id},
                    {"$set": {
                        "mmr": new_mmr,
                        "losses": losses,
                        "matches": matches_played,
                        "last_updated": datetime.datetime.utcnow()
                    }}
                )

                # Track MMR change
                mmr_changes.append({
                    "player_id": player_id,
                    "old_mmr": old_mmr,
                    "new_mmr": new_mmr,
                    "mmr_change": -mmr_loss,  # Negative for loss
                    "is_win": False
                })
            else:
                # Look up player's rank in ranks collection
                print(f"New player {player['name']} (ID: {player_id}), determining starting MMR")

                # Try to find rank record
                rank_record = db.get_collection('ranks').find_one({"discord_id": player_id})
                if not rank_record:
                    rank_record = db.get_collection('ranks').find_one({"discord_username": player["name"]})

                # Default values
                starting_mmr = 600  # Default MMR

                if rank_record:
                    print(f"Found rank record: {rank_record}")

                    # Simplified logic - just use tier-based MMR
                    tier = rank_record.get("tier", "Rank C")
                    starting_mmr = self.TIER_MMR.get(tier, 600)
                    print(f"Using tier-based MMR for {tier}: {starting_mmr}")
                else:
                    print(f"No rank record found, using default MMR: {starting_mmr}")

                # Calculate first loss MMR
                mmr_loss = self.calculate_dynamic_mmr(1, is_win=False)
                new_mmr = max(0, starting_mmr - mmr_loss)  # Don't go below 0
                print(f"NEW PLAYER {player['name']} FIRST LOSS: {starting_mmr} - {mmr_loss} = {new_mmr}")

                self.players.insert_one({
                    "id": player_id,
                    "name": player["name"],
                    "mmr": new_mmr,
                    "wins": 0,
                    "losses": 1,
                    "matches": 1,
                    "created_at": datetime.datetime.utcnow(),
                    "last_updated": datetime.datetime.utcnow()
                })

                # Track MMR change for new player
                mmr_changes.append({
                    "player_id": player_id,
                    "old_mmr": starting_mmr,
                    "new_mmr": new_mmr,
                    "mmr_change": -mmr_loss,  # Negative for loss
                    "is_win": False
                })

        # Store the MMR changes in the match document
        self.matches.update_one(
            {"match_id": match_id},
            {"$set": {"mmr_changes": mmr_changes}}
        )

        # After updating MMR for winners and losers:
        if ctx:
            # Update roles for winners
            for player in winning_team:
                player_id = player["id"]
                # Skip dummy players (those with IDs starting with 9000)
                if player_id.startswith('9000'):
                    continue

                # Get updated MMR from database
                player_data = self.players.find_one({"id": player_id})
                if player_data:
                    mmr = player_data.get("mmr", 600)
                    # Update Discord role based on new MMR
                    await self.update_discord_role(ctx, player_id, mmr)

            # Update roles for losers
            for player in losing_team:
                player_id = player["id"]
                # Skip dummy players
                if player_id.startswith('9000'):
                    continue

                # Get updated MMR from database
                player_data = self.players.find_one({"id": player_id})
                if player_data:
                    mmr = player_data.get("mmr", 600)
                    # Update Discord role based on new MMR
                    await self.update_discord_role(ctx, player_id, mmr)

        # Remove from active matches
        if match["match_id"] in self.active_matches:
            del self.active_matches[match["match_id"]]

        return updated_match, None

    async def update_discord_role(self, ctx, player_id, new_mmr):
        """Update a player's Discord role based on their new MMR"""
        try:
            # Define MMR thresholds for ranks
            RANK_A_THRESHOLD = 1600
            RANK_B_THRESHOLD = 1100

            # Get the player's Discord member object
            member = await ctx.guild.fetch_member(int(player_id))
            if not member:
                print(f"Could not find Discord member with ID {player_id}")
                return

            # Get the rank roles
            rank_a_role = discord.utils.get(ctx.guild.roles, name="Rank A")
            rank_b_role = discord.utils.get(ctx.guild.roles, name="Rank B")
            rank_c_role = discord.utils.get(ctx.guild.roles, name="Rank C")

            if not rank_a_role or not rank_b_role or not rank_c_role:
                print("Could not find one or more rank roles")
                return

            # Determine which role the player should have based on MMR
            new_role = None
            if new_mmr >= RANK_A_THRESHOLD:
                new_role = rank_a_role
            elif new_mmr >= RANK_B_THRESHOLD:
                new_role = rank_b_role
            else:
                new_role = rank_c_role

            # Check current roles
            current_rank_role = None
            for role in member.roles:
                if role in [rank_a_role, rank_b_role, rank_c_role]:
                    current_rank_role = role
                    break

            # If role hasn't changed, do nothing
            if current_rank_role == new_role:
                return

            # Remove current rank role if they have one
            if current_rank_role:
                await member.remove_roles(current_rank_role, reason="MMR rank update")

            # Add the new role - Fixed: using "add_roles" (plural) instead of "add_role"
            await member.add_roles(new_role, reason=f"MMR update: {new_mmr}")

            # Log the role change
            print(
                f"Updated roles for {member.display_name}: {current_rank_role.name if current_rank_role else 'None'} -> {new_role.name}")

            # Announce the rank change if it's a promotion
            if not current_rank_role or (
                    (current_rank_role == rank_c_role and new_role in [rank_b_role, rank_a_role]) or
                    (current_rank_role == rank_b_role and new_role == rank_a_role)
            ):
                await ctx.send(f"🎉 Congratulations {member.mention}! You've been promoted to **{new_role.name}**!")

        except Exception as e:
            print(f"Error updating Discord role: {str(e)}")

    def update_player_mmr(self, winning_team, losing_team):
        """Update MMR for all players in the match with dynamic MMR changes"""
        # Process winners
        for player in winning_team:
            player_id = player["id"]

            # Get player data or create new
            player_data = self.players.find_one({"id": player_id})

            if player_data:
                # Existing player logic remains the same
                matches_played = player_data.get("matches", 0) + 1
                wins = player_data.get("wins", 0) + 1
                mmr_gain = self.calculate_dynamic_mmr(matches_played, is_win=True)
                new_mmr = player_data.get("mmr", 600) + mmr_gain
                print(f"Player {player['name']} MMR update: {player_data.get('mmr', 600)} + {mmr_gain} = {new_mmr}")

                self.players.update_one(
                    {"id": player_id},
                    {"$set": {
                        "mmr": new_mmr,
                        "wins": wins,
                        "matches": matches_played,
                        "last_updated": datetime.datetime.utcnow()
                    }}
                )
            else:
                # Look up player's rank in ranks collection
                print(f"New player {player['name']} (ID: {player_id}), determining starting MMR")

                # Try to find rank record
                rank_record = db.get_collection('ranks').find_one({"discord_id": player_id})
                if not rank_record:
                    rank_record = db.get_collection('ranks').find_one({"discord_username": player["name"]})

                # Default values
                starting_mmr = 600  # Default MMR

                if rank_record:
                    print(f"Found rank record: {rank_record}")

                    # Simplified logic - just use tier-based MMR
                    tier = rank_record.get("tier", "Rank C")
                    starting_mmr = self.TIER_MMR.get(tier, 600)
                    print(f"Using tier-based MMR for {tier}: {starting_mmr}")
                else:
                    print(f"No rank record found, using default MMR: {starting_mmr}")

                # Calculate first win MMR
                mmr_gain = self.calculate_dynamic_mmr(1, is_win=True)
                new_mmr = starting_mmr + mmr_gain
                print(f"NEW PLAYER {player['name']} FIRST WIN: {starting_mmr} + {mmr_gain} = {new_mmr}")

                self.players.insert_one({
                    "id": player_id,
                    "name": player["name"],
                    "mmr": new_mmr,
                    "wins": 1,
                    "losses": 0,
                    "matches": 1,
                    "created_at": datetime.datetime.utcnow(),
                    "last_updated": datetime.datetime.utcnow()
                })

        # Process losers with the same simplified logic
        for player in losing_team:
            player_id = player["id"]

            # Get player data or create new
            player_data = self.players.find_one({"id": player_id})

            if player_data:
                # Update existing player
                matches_played = player_data.get("matches", 0) + 1
                losses = player_data.get("losses", 0) + 1
                mmr_loss = self.calculate_dynamic_mmr(matches_played, is_win=False)
                new_mmr = max(0, player_data.get("mmr", 600) - mmr_loss)  # Don't go below 0
                print(f"Player {player['name']} MMR update: {player_data.get('mmr', 600)} - {mmr_loss} = {new_mmr}")

                self.players.update_one(
                    {"id": player_id},
                    {"$set": {
                        "mmr": new_mmr,
                        "losses": losses,
                        "matches": matches_played,
                        "last_updated": datetime.datetime.utcnow()
                    }}
                )
            else:
                # Look up player's rank in ranks collection
                print(f"New player {player['name']} (ID: {player_id}), determining starting MMR")

                # Try to find rank record
                rank_record = db.get_collection('ranks').find_one({"discord_id": player_id})
                if not rank_record:
                    rank_record = db.get_collection('ranks').find_one({"discord_username": player["name"]})

                # Default values
                starting_mmr = 600  # Default MMR

                if rank_record:
                    print(f"Found rank record: {rank_record}")

                    # Simplified logic - just use tier-based MMR
                    tier = rank_record.get("tier", "Rank C")
                    starting_mmr = self.TIER_MMR.get(tier, 600)
                    print(f"Using tier-based MMR for {tier}: {starting_mmr}")
                else:
                    print(f"No rank record found, using default MMR: {starting_mmr}")

                # Calculate first loss MMR
                mmr_loss = self.calculate_dynamic_mmr(1, is_win=False)
                new_mmr = max(0, starting_mmr - mmr_loss)  # Don't go below 0
                print(f"NEW PLAYER {player['name']} FIRST LOSS: {starting_mmr} - {mmr_loss} = {new_mmr}")

                self.players.insert_one({
                    "id": player_id,
                    "name": player["name"],
                    "mmr": new_mmr,
                    "wins": 0,
                    "losses": 1,
                    "matches": 1,
                    "created_at": datetime.datetime.utcnow(),
                    "last_updated": datetime.datetime.utcnow()
                })

    def calculate_dynamic_mmr(self, matches_played, is_win=True):
        """
        Calculate dynamic MMR change based on matches played

        Parameters:
        - matches_played: Number of matches the player has played (including the current one)
        - is_win: True if calculating for a win, False for a loss

        Returns:
        - MMR change amount
        """
        # Higher starting values and faster decay
        BASE_MMR_GAIN = 120  # Starting MMR gain for wins (much higher)
        BASE_MMR_LOSS = 90  # Starting MMR loss for losses (much higher)
        MIN_MMR_GAIN = 20  # Minimum MMR gain for wins after many games (higher minimum)
        MIN_MMR_LOSS = 18  # Minimum MMR loss for losses after many games (higher minimum)

        # Faster decay factor
        DECAY_RATE = 0.18  # Much quicker drop-off

        if is_win:
            # Calculate MMR gain (decreasing with more matches)
            mmr_change = BASE_MMR_GAIN * math.exp(-DECAY_RATE * (matches_played - 1))

            # Ensure it doesn't go below the minimum
            return max(MIN_MMR_GAIN, round(mmr_change))
        else:
            # Calculate MMR loss (decreasing with more matches)
            mmr_change = BASE_MMR_LOSS * math.exp(-DECAY_RATE * (matches_played - 1))

            # Ensure it doesn't go below the minimum
            return max(MIN_MMR_LOSS, round(mmr_change))

    def get_leaderboard(self, limit=10):
        """Get top players by MMR"""
        leaderboard = list(self.players.find().sort("mmr", -1).limit(limit))
        return leaderboard

    def get_player_stats(self, player_id):
        """Get stats for a specific player"""
        player = self.players.find_one({"id": player_id})
        return player