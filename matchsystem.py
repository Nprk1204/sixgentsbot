import math
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

        # Simplified - keep just the three tier-based MMR values
        self.TIER_MMR = {
            "Rank A": 1850,  # Grand Champion I and above
            "Rank B": 1350,  # Champion I to Champion III
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

        # Check if reporter is in either team
        team1_ids = [p["id"] for p in match["team1"]]
        team2_ids = [p["id"] for p in match["team2"]]

        if reporter_id in team1_ids:
            reporter_team = 1
        elif reporter_id in team2_ids:
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

        # Update MMR for all players with the new algorithm
        if winner == 1:
            winning_team = match["team1"]
            losing_team = match["team2"]
        else:
            winning_team = match["team2"]
            losing_team = match["team1"]

        # Calculate team average MMRs
        team1_mmrs = []
        team2_mmrs = []

        # Get MMRs for team 1
        for player in match["team1"]:
            player_id = player["id"]

            # Check for dummy players with stored MMR
            if player_id.startswith('9000') and "dummy_mmr" in player:
                team1_mmrs.append(player["dummy_mmr"])
                continue

            # Skip dummy players without MMR
            if player_id.startswith('9000'):
                continue

            # Get player MMR for real players
            player_data = self.players.find_one({"id": player_id})
            if player_data:
                team1_mmrs.append(player_data.get("mmr", 0))
            else:
                # For new players, get MMR from rank verification or use default
                rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                if rank_record:
                    team1_mmrs.append(rank_record.get("mmr", 600))
                else:
                    # Use tier-based default
                    team1_mmrs.append(600)  # Default to Rank C MMR

        # Get MMRs for team 2
        for player in match["team2"]:
            player_id = player["id"]

            # Check for dummy players with stored MMR
            if player_id.startswith('9000') and "dummy_mmr" in player:
                team2_mmrs.append(player["dummy_mmr"])
                continue

            # Skip dummy players without MMR
            if player_id.startswith('9000'):
                continue

            # Get player MMR for real players
            player_data = self.players.find_one({"id": player_id})
            if player_data:
                team2_mmrs.append(player_data.get("mmr", 0))
            else:
                # For new players, get MMR from rank verification or use default
                rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                if rank_record:
                    team2_mmrs.append(rank_record.get("mmr", 600))
                else:
                    # Use tier-based default
                    team2_mmrs.append(600)  # Default to Rank C MMR

        # Calculate average MMRs for each team
        team1_avg_mmr = sum(team1_mmrs) / len(team1_mmrs) if team1_mmrs else 0
        team2_avg_mmr = sum(team2_mmrs) / len(team2_mmrs) if team2_mmrs else 0

        print(f"Team 1 avg MMR: {team1_avg_mmr}")
        print(f"Team 2 avg MMR: {team2_avg_mmr}")

        # Track MMR changes for each player
        mmr_changes = []

        # Update MMR for winners
        for player in winning_team:
            player_id = player["id"]

            # Skip dummy players
            if player_id.startswith('9000'):
                continue

            # Determine which team this player is on
            is_team1 = player in match["team1"]
            player_team_avg = team1_avg_mmr if is_team1 else team2_avg_mmr
            opponent_avg = team2_avg_mmr if is_team1 else team1_avg_mmr

            # Get player data or create new
            player_data = self.players.find_one({"id": player_id})

            if player_data:
                # Existing player logic
                matches_played = player_data.get("matches", 0) + 1
                wins = player_data.get("wins", 0) + 1
                old_mmr = player_data.get("mmr", 600)

                # Calculate MMR gain with new algorithm
                mmr_gain = self.calculate_dynamic_mmr(
                    old_mmr,
                    player_team_avg,
                    opponent_avg,
                    matches_played,
                    is_win=True
                )

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
                # Get starting MMR from rank record or use default
                rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                starting_mmr = 600  # Default MMR

                if rank_record:
                    tier = rank_record.get("tier", "Rank C")
                    starting_mmr = self.TIER_MMR.get(tier, 600)

                # Calculate first win MMR with the new algorithm
                mmr_gain = self.calculate_dynamic_mmr(
                    starting_mmr,
                    player_team_avg,
                    opponent_avg,
                    1,  # First match
                    is_win=True
                )

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

        # Update MMR for losing team - similar logic as above
        for player in losing_team:
            player_id = player["id"]

            # Skip dummy players
            if player_id.startswith('9000'):
                continue

            # Determine which team this player is on
            is_team1 = player in match["team1"]
            player_team_avg = team1_avg_mmr if is_team1 else team2_avg_mmr
            opponent_avg = team2_avg_mmr if is_team1 else team1_avg_mmr

            # Get player data or create new
            player_data = self.players.find_one({"id": player_id})

            if player_data:
                # Update existing player
                matches_played = player_data.get("matches", 0) + 1
                losses = player_data.get("losses", 0) + 1
                old_mmr = player_data.get("mmr", 600)

                # Calculate MMR loss with new algorithm
                mmr_loss = self.calculate_dynamic_mmr(
                    old_mmr,
                    player_team_avg,
                    opponent_avg,
                    matches_played,
                    is_win=False
                )

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
                # Logic for new player who loses their first match
                rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                starting_mmr = 600  # Default MMR

                if rank_record:
                    tier = rank_record.get("tier", "Rank C")
                    starting_mmr = self.TIER_MMR.get(tier, 600)

                # Calculate first loss MMR
                mmr_loss = self.calculate_dynamic_mmr(
                    starting_mmr,
                    player_team_avg,
                    opponent_avg,
                    1,  # First match
                    is_win=False
                )

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
            {"$set": {
                "mmr_changes": mmr_changes,
                "team1_avg_mmr": team1_avg_mmr,
                "team2_avg_mmr": team2_avg_mmr
            }}
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

    def update_player_mmr(self, winning_team, losing_team, match_id=None):
        """Update MMR for all players in the match with dynamic MMR changes based on team balance"""
        # Retrieve match data if match_id is provided
        match = None
        if match_id:
            match = self.matches.find_one({"match_id": match_id})

        # Calculate team average MMRs
        winning_team_mmrs = []
        losing_team_mmrs = []

        # Get MMRs for winning team
        for player in winning_team:
            player_id = player["id"]

            # Check for dummy players with stored MMR
            if player_id.startswith('9000') and "dummy_mmr" in player:
                winning_team_mmrs.append(player["dummy_mmr"])
                continue

            # Skip dummy players without MMR
            if player_id.startswith('9000'):
                continue

            # Get player MMR for real players
            player_data = self.players.find_one({"id": player_id})
            if player_data:
                winning_team_mmrs.append(player_data.get("mmr", 0))
            else:
                # For new players, get MMR from rank verification or use default
                rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                if rank_record:
                    winning_team_mmrs.append(rank_record.get("mmr", 600))
                else:
                    # Use tier-based default
                    winning_team_mmrs.append(600)  # Default to Rank C MMR

        # Get MMRs for losing team
        for player in losing_team:
            player_id = player["id"]

            # Check for dummy players with stored MMR
            if player_id.startswith('9000') and "dummy_mmr" in player:
                losing_team_mmrs.append(player["dummy_mmr"])
                continue

            # Skip dummy players without MMR
            if player_id.startswith('9000'):
                continue

            # Get player MMR for real players
            player_data = self.players.find_one({"id": player_id})
            if player_data:
                losing_team_mmrs.append(player_data.get("mmr", 0))
            else:
                # For new players, get MMR from rank verification or use default
                rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                if rank_record:
                    losing_team_mmrs.append(rank_record.get("mmr", 600))
                else:
                    # Use tier-based default
                    losing_team_mmrs.append(600)  # Default to Rank C MMR

        # Calculate average MMRs for each team
        winning_team_avg_mmr = sum(winning_team_mmrs) / len(winning_team_mmrs) if winning_team_mmrs else 0
        losing_team_avg_mmr = sum(losing_team_mmrs) / len(losing_team_mmrs) if losing_team_mmrs else 0

        print(f"Winning team avg MMR: {winning_team_avg_mmr}")
        print(f"Losing team avg MMR: {losing_team_avg_mmr}")

        # Track MMR changes for each player
        mmr_changes = []

        # Process winners
        for player in winning_team:
            player_id = player["id"]
            # Skip dummy players
            if player_id.startswith('9000'):
                continue

            # Get player data or create new
            player_data = self.players.find_one({"id": player_id})

            if player_data:
                # Existing player logic
                matches_played = player_data.get("matches", 0) + 1
                wins = player_data.get("wins", 0) + 1
                old_mmr = player_data.get("mmr", 600)

                # Calculate MMR gain with new algorithm
                mmr_gain = self.calculate_dynamic_mmr(
                    old_mmr,
                    winning_team_avg_mmr,
                    losing_team_avg_mmr,
                    matches_played,
                    is_win=True
                )

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
                rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})

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

                # Calculate first win MMR with the new algorithm
                mmr_gain = self.calculate_dynamic_mmr(
                    starting_mmr,
                    winning_team_avg_mmr,
                    losing_team_avg_mmr,
                    1,  # First match
                    is_win=True
                )

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

        # Process losers with similar logic
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
                old_mmr = player_data.get("mmr", 600)

                # Calculate MMR loss with new algorithm
                mmr_loss = self.calculate_dynamic_mmr(
                    old_mmr,
                    losing_team_avg_mmr,
                    winning_team_avg_mmr,
                    matches_played,
                    is_win=False
                )

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
                rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})

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
                mmr_loss = self.calculate_dynamic_mmr(
                    starting_mmr,
                    losing_team_avg_mmr,
                    winning_team_avg_mmr,
                    1,  # First match
                    is_win=False
                )

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

        # Store the MMR changes and team averages in the match document if match_id is provided
        if match_id:
            self.matches.update_one(
                {"match_id": match_id},
                {"$set": {
                    "mmr_changes": mmr_changes,
                    "winning_team_avg_mmr": winning_team_avg_mmr,
                    "losing_team_avg_mmr": losing_team_avg_mmr
                }}
            )

            print(f"Stored MMR changes and team averages for match {match_id}")

    def calculate_dynamic_mmr(self, player_mmr, team_avg_mmr, opponent_avg_mmr, matches_played, is_win=True):
        """
        Calculate dynamic MMR change based on:
        1. MMR difference between teams
        2. Number of matches played (for decay)

        Parameters:
        - player_mmr: Current MMR of the player
        - team_avg_mmr: Average MMR of the player's team
        - opponent_avg_mmr: Average MMR of the opposing team
        - matches_played: Number of matches the player has played (including the current one)
        - is_win: True if calculating for a win, False for a loss

        Returns:
        - MMR change amount
        """
        # Base values for MMR changes
        BASE_MMR_CHANGE = 25  # Standard MMR change for evenly matched teams for experienced players

        # First game gives ~100-120 MMR for wins, slightly less for losses
        FIRST_GAME_WIN = 110  # Base value for first win
        FIRST_GAME_LOSS = 80  # Base value for first loss

        MAX_MMR_CHANGE = 140  # Maximum possible MMR change for extremely unbalanced first matches
        MIN_MMR_CHANGE = 20  # Minimum MMR change even after many games

        # Decay settings
        DECAY_RATE = 0.15  # Slightly increased to create faster decay from high initial values

        # Calculate the MMR difference between teams
        # Positive means opponent team has higher MMR, negative means player's team has higher MMR
        mmr_difference = opponent_avg_mmr - team_avg_mmr

        # Normalize the difference to a factor between 0.7 and 1.3
        # A difference of 200 MMR is considered significant
        difference_factor = 1 + (mmr_difference / 600)  # 300 MMR difference = 0.5 factor change
        difference_factor = max(0.7, min(1.3, difference_factor))  # Clamp between 0.7 and 1.3

        # For the first few games, use much higher base values
        if matches_played <= 5:
            # Linearly interpolate between first game value and regular base value
            # as matches_played goes from 1 to 5
            progress = (matches_played - 1) / 4  # 0 for first match, 1 for fifth match

            if is_win:
                # Start high, gradually decrease toward BASE_MMR_CHANGE
                base_value = FIRST_GAME_WIN * (1 - progress) + BASE_MMR_CHANGE * progress
            else:
                # Start high but less than win, gradually decrease toward BASE_MMR_CHANGE
                base_value = FIRST_GAME_LOSS * (1 - progress) + BASE_MMR_CHANGE * progress

            # Apply difference factor
            if is_win:
                # Winners gain more if they were the underdogs (positive difference)
                base_change = base_value * difference_factor
            else:
                # Losers lose less if they were the underdogs (positive difference)
                base_change = base_value * (2 - difference_factor)
        else:
            # After 5 matches, use the regular base value with full decay
            if is_win:
                # Winners gain more if they were the underdogs (positive difference)
                base_change = BASE_MMR_CHANGE * difference_factor
            else:
                # Losers lose less if they were the underdogs (positive difference)
                base_change = BASE_MMR_CHANGE * (2 - difference_factor)

        # Apply decay based on number of matches played after the initial 5 games
        if matches_played <= 5:
            # First 5 games already have built-in decay via the interpolation
            decay_multiplier = 1.0
        else:
            # After 5 games, apply regular exponential decay
            # Adjusted to start from match 6 (matches_played - 5)
            decay_multiplier = 1.0 * math.exp(-DECAY_RATE * (matches_played - 5))

        # Calculate final MMR change
        mmr_change = base_change * decay_multiplier

        # Ensure the change is within bounds - MIN is applied AFTER decay
        mmr_change = max(MIN_MMR_CHANGE, min(MAX_MMR_CHANGE, mmr_change))

        # Round to nearest integer
        return round(mmr_change)

    def get_leaderboard(self, limit=10):
        """Get top players by MMR"""
        leaderboard = list(self.players.find().sort("mmr", -1).limit(limit))
        return leaderboard

    def get_player_stats(self, player_id):
        """Get stats for a specific player"""
        player = self.players.find_one({"id": player_id})
        return player