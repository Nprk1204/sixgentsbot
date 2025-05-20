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
        self.bot = None

        # Simplified - keep just the three tier-based MMR values
        self.TIER_MMR = {
            "Rank A": 1850,  # Grand Champion I and above
            "Rank B": 1350,  # Champion I to Champion III
            "Rank C": 600  # Diamond III and below - default
        }

    def set_bot(self, bot):
        """Set the bot instance"""
        self.bot = bot

    def create_match(self, match_id, team1, team2, channel_id, is_global=False):
        """
        Create a new match entry or update an existing one

        Args:
            match_id: The ID to use for this match. Either an existing ID or a new one.
            team1: List of player data for team 1
            team2: List of player data for team 2
            channel_id: The channel ID where the match is happening
            is_global: Whether this is a global match

        Returns:
            The match ID used (might be the same as input or a new one if none provided)
        """
        # Always ensure 6-character match IDs
        if not match_id or match_id.lower() == "none":
            # Generate a new 6-character ID
            short_id = str(uuid.uuid4().hex)[:6]
            print(f"No match ID provided. Creating match with new ID: {short_id}")
        else:
            # Check if the provided ID is the correct format
            if len(match_id) != 6:
                # Not a 6-character ID, generate a new one
                original_id = match_id
                short_id = str(uuid.uuid4().hex)[:6]
                print(f"WARNING: Non-standard match ID format: {original_id}. Using new ID: {short_id}")
            else:
                # Use the provided 6-character match_id
                short_id = match_id
                print(f"Using provided match ID: {short_id}")

        print(f"Creating/updating match with ID: {short_id}, channel: {channel_id}, is_global: {is_global}")

        # CRITICAL: Before creating a new match, find and cancel any active selection
        # with these players to prevent overlap issues
        all_players = team1 + team2
        for player in all_players:
            player_id = player["id"]
            # Skip dummy players
            if not player_id.startswith('9000'):
                print(f"Checking if player {player['name']} (ID: {player_id}) is in any active selections")
                # Update any voting/selection matches containing this player to be cancelled
                self.matches.update_many(
                    {
                        "players.id": player_id,
                        "status": {"$in": ["voting", "selection"]},
                        "match_id": {"$ne": short_id}  # Don't cancel the current match
                    },
                    {
                        "$set": {"status": "cancelled"}
                    }
                )

        # Add better debugging for channel detection, but ONLY if is_global wasn't explicitly provided
        if not is_global:  # Only try to detect if is_global wasn't explicitly True
            try:
                if self.bot:
                    channel = self.bot.get_channel(int(channel_id))
                    print(f"Channel lookup result: {channel}")
                    if channel:
                        channel_name = channel.name.lower()
                        print(f"Channel name: {channel_name}")
                        is_global = channel_name == "global"
                    else:
                        print(f"Failed to find channel with ID: {channel_id}")
                else:
                    print("Bot reference is None during match creation")
            except Exception as e:
                print(f"Error in channel detection: {e}")
        else:
            print(f"Using provided is_global={is_global} without re-detection")

        # Check if the match already exists
        existing_match = self.matches.find_one({"match_id": short_id})

        if existing_match:
            # Update the existing match with the new teams and status
            print(f"Updating existing match with ID: {short_id}")
            self.matches.update_one(
                {"match_id": short_id},
                {"$set": {
                    "team1": team1,
                    "team2": team2,
                    "status": "in_progress",
                    "winner": None,
                    "score": {"team1": 0, "team2": 0},
                    "is_global": is_global
                }}
            )
        else:
            # Create match data with explicit in_progress status
            match_data = {
                "match_id": short_id,
                "team1": team1,
                "team2": team2,
                "status": "in_progress",  # CRITICAL: Ensure default status is "in_progress"
                "winner": None,
                "score": {"team1": 0, "team2": 0},
                "channel_id": channel_id,
                "created_at": datetime.datetime.utcnow(),
                "completed_at": None,
                "reported_by": None,
                "is_global": is_global
            }

            # Store in database
            self.matches.insert_one(match_data)

        # Store in memory for quick access
        self.active_matches[short_id] = {
            "match_id": short_id,
            "team1": team1,
            "team2": team2,
            "status": "in_progress",
            "channel_id": channel_id,
            "is_global": is_global
        }

        # Debug print to confirm match creation
        print(f"Created/updated match with ID: {short_id}, status: in_progress, is_global: {is_global}")

        return short_id

    def get_match_by_id_or_channel(self, match_id=None, channel_id=None, status=None):
        """
        Get a match by its ID or channel ID, prioritizing match_id.

        Args:
            match_id: The match ID to search for
            channel_id: The channel ID to search for (fallback)
            status: Optional status filter (e.g., "in_progress")

        Returns:
            The match document or None if not found
        """
        query = {}

        # Build query based on provided parameters
        if match_id:
            query["match_id"] = match_id
        elif channel_id:
            query["channel_id"] = str(channel_id)
        else:
            return None

        if status:
            if isinstance(status, list):
                query["status"] = {"$in": status}
            else:
                query["status"] = status

        # Execute the query
        return self.matches.find_one(query)

    def get_active_match_by_channel(self, channel_id):
        """Get active match by channel ID"""
        match = self.matches.find_one({"channel_id": channel_id, "status": "in_progress"})
        return match

    async def report_match_by_id(self, match_id, reporter_id, result, ctx=None):
        """Report a match result by match ID and win/loss"""
        # Find the match by ID
        match = self.matches.find_one({"match_id": match_id})

        # ADDED: Also search by match ID in voting or selection status with the same reporter
        if not match:
            # Try to find any match in voting or selection with this player
            active_matches = list(self.matches.find({
                "status": {"$in": ["voting", "selection", "in_progress"]},
                "players.id": reporter_id
            }))

            if active_matches:
                # Update the log for debugging
                print(f"Found {len(active_matches)} active matches for reporter {reporter_id}")
                for active_match in active_matches:
                    print(f"Active match: {active_match.get('match_id')} with status {active_match.get('status')}")

                    # Cancel these matches as they're stale
                    self.matches.update_one(
                        {"_id": active_match["_id"]},
                        {"$set": {"status": "cancelled"}}
                    )

                # Log that we're cancelling stale matches
                print(f"Cancelled stale matches for reporter {reporter_id}")

        # Try again with the original ID - original match lookup
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

        # ADD debugging output right after this:
        print(f"Match report update for match {match_id} - modified: {result.modified_count}")

        # Check if the update was successful
        if result.modified_count == 0:
            # This means the match wasn't updated - either doesn't exist or already reported
            # Double check if it exists but is already completed
            completed_match = self.matches.find_one({"match_id": match_id})

            if completed_match and completed_match.get("status") == "completed":
                return None, "This match has already been reported."
            elif completed_match and completed_match.get("status") == "selection":
                # The match is still in selection phase
                return None, "This match is still in team selection phase and cannot be reported yet."
            else:
                return None, f"Failed to update match {match_id}. Please check the match ID."

        # Now get the updated match document
        updated_match = self.matches.find_one({"match_id": match_id})

        # Check if this is a global match
        is_global_match = updated_match.get("is_global", False)
        channel_id = updated_match.get("channel_id")
        print(f"Match type: {'Global' if is_global_match else 'Ranked'}, Channel ID: {channel_id}")

        # IMPROVED: Clear player status for ALL players in the match
        all_players = team1_ids + team2_ids

        # Clear matches for all players in the match to ensure complete cleanup
        for player_id in all_players:
            # Skip dummy players
            if player_id.startswith('9000'):
                continue

            # Cancel ALL potential matches for this player (voting, selection, in_progress)
            self.matches.update_many(
                {
                    "players.id": player_id,
                    "status": {"$in": ["voting", "selection", "in_progress"]},
                    "match_id": {"$ne": match_id}  # Don't modify the current match
                },
                {"$set": {"status": "cancelled"}}
            )

        # Calculate team average MMRs
        team1_mmrs = []
        team2_mmrs = []

        # IMPORTANT: Define winning_team and losing_team variables BEFORE using them
        if winner == 1:
            winning_team = match["team1"]
            losing_team = match["team2"]
        else:
            winning_team = match["team2"]
            losing_team = match["team1"]

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
                # Use global or ranked MMR based on match type
                if is_global_match:
                    mmr = player_data.get("global_mmr", 300)  # Default global MMR is 300
                else:
                    mmr = player_data.get("mmr", 600)  # Default ranked MMR
                team1_mmrs.append(mmr)
            else:
                # For new players, get MMR from rank verification or use default
                rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                if rank_record:
                    if is_global_match:
                        mmr = rank_record.get("global_mmr", 300)
                    else:
                        tier = rank_record.get("tier", "Rank C")
                        mmr = self.TIER_MMR.get(tier, 600)
                    team1_mmrs.append(mmr)
                else:
                    # Use default MMR
                    if is_global_match:
                        team1_mmrs.append(300)  # Default global MMR
                    else:
                        team1_mmrs.append(600)  # Default ranked MMR

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
                # Use global or ranked MMR based on match type
                if is_global_match:
                    mmr = player_data.get("global_mmr", 300)
                else:
                    mmr = player_data.get("mmr", 600)
                team2_mmrs.append(mmr)
            else:
                # For new players, get MMR from rank verification or use default
                rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                if rank_record:
                    if is_global_match:
                        mmr = rank_record.get("global_mmr", 300)
                    else:
                        tier = rank_record.get("tier", "Rank C")
                        mmr = self.TIER_MMR.get(tier, 600)
                    team2_mmrs.append(mmr)
                else:
                    # Use default MMR
                    if is_global_match:
                        team2_mmrs.append(300)  # Default global MMR
                    else:
                        team2_mmrs.append(600)  # Default ranked MMR

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
                if is_global_match:
                    # Global match win handling
                    global_matches = player_data.get("global_matches", 0) + 1
                    global_wins = player_data.get("global_wins", 0) + 1
                    old_mmr = player_data.get("global_mmr", 300)

                    # Update streak
                    current_streak = player_data.get("global_streak", 0)
                    # If current streak is negative (losing streak), reset it to 1
                    # Otherwise increment the streak
                    new_streak = 1 if current_streak <= 0 else current_streak + 1

                    # Calculate MMR gain with new algorithm including streak
                    mmr_gain = self.calculate_dynamic_mmr(
                        old_mmr,
                        player_team_avg,
                        opponent_avg,
                        global_matches,
                        is_win=True,
                        streak=new_streak
                    )

                    new_mmr = old_mmr + mmr_gain
                    print(
                        f"Player {player['name']} GLOBAL MMR update: {old_mmr} + {mmr_gain} = {new_mmr} (Streak: {new_streak})")

                    self.players.update_one(
                        {"id": player_id},
                        {"$set": {
                            "global_mmr": new_mmr,
                            "global_wins": global_wins,
                            "global_matches": global_matches,
                            "global_streak": new_streak,
                            "last_updated": datetime.datetime.utcnow()
                        }}
                    )

                    # Track MMR change for global
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": old_mmr,
                        "new_mmr": new_mmr,
                        "mmr_change": mmr_gain,
                        "streak": new_streak,
                        "is_win": True,
                        "is_global": True
                    })
                else:
                    # Regular ranked match win handling
                    matches_played = player_data.get("matches", 0) + 1
                    wins = player_data.get("wins", 0) + 1
                    old_mmr = player_data.get("mmr", 600)

                    # Update streak
                    current_streak = player_data.get("streak", 0)
                    # If current streak is negative (losing streak), reset it to 1
                    # Otherwise increment the streak
                    new_streak = 1 if current_streak <= 0 else current_streak + 1

                    # Calculate MMR gain with new algorithm including streak
                    mmr_gain = self.calculate_dynamic_mmr(
                        old_mmr,
                        player_team_avg,
                        opponent_avg,
                        matches_played,
                        is_win=True,
                        streak=new_streak
                    )

                    new_mmr = old_mmr + mmr_gain
                    print(
                        f"Player {player['name']} RANKED MMR update: {old_mmr} + {mmr_gain} = {new_mmr} (Streak: {new_streak})")

                    self.players.update_one(
                        {"id": player_id},
                        {"$set": {
                            "mmr": new_mmr,
                            "wins": wins,
                            "matches": matches_played,
                            "streak": new_streak,
                            "last_updated": datetime.datetime.utcnow()
                        }}
                    )

                    # Track MMR change for ranked
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": old_mmr,
                        "new_mmr": new_mmr,
                        "mmr_change": mmr_gain,
                        "streak": new_streak,
                        "is_win": True,
                        "is_global": False
                    })
            else:
                # New player logic
                if is_global_match:
                    # New player's first global match - win
                    # Get starting MMR from rank record or use default
                    rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                    starting_global_mmr = 300  # Default global MMR

                    if rank_record and "global_mmr" in rank_record:
                        starting_global_mmr = rank_record.get("global_mmr", 300)

                    # Calculate first win MMR with the new algorithm
                    mmr_gain = self.calculate_dynamic_mmr(
                        starting_global_mmr,
                        player_team_avg,
                        opponent_avg,
                        1,  # First match
                        is_win=True,
                        streak=1  # First win, streak of 1
                    )

                    new_global_mmr = starting_global_mmr + mmr_gain
                    print(
                        f"NEW PLAYER {player['name']} FIRST GLOBAL WIN: {starting_global_mmr} + {mmr_gain} = {new_global_mmr} (Streak: 1)")

                    # Get default ranked MMR from rank verification if available
                    starting_ranked_mmr = 600  # Default ranked MMR
                    if rank_record:
                        tier = rank_record.get("tier", "Rank C")
                        starting_ranked_mmr = self.TIER_MMR.get(tier, 600)

                    self.players.insert_one({
                        "id": player_id,
                        "name": player["name"],
                        "mmr": starting_ranked_mmr,  # Default ranked MMR
                        "global_mmr": new_global_mmr,  # Updated global MMR
                        "wins": 0,
                        "global_wins": 1,
                        "losses": 0,
                        "global_losses": 0,
                        "matches": 0,
                        "global_matches": 1,
                        "streak": 0,  # No streak for ranked yet
                        "global_streak": 1,  # First win, streak of 1
                        "created_at": datetime.datetime.utcnow(),
                        "last_updated": datetime.datetime.utcnow()
                    })

                    # Track MMR change for global
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": starting_global_mmr,
                        "new_mmr": new_global_mmr,
                        "mmr_change": mmr_gain,
                        "streak": 1,
                        "is_win": True,
                        "is_global": True
                    })
                else:
                    # New player's first ranked match - win
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
                        is_win=True,
                        streak=1  # First win, streak of 1
                    )

                    new_mmr = starting_mmr + mmr_gain
                    print(
                        f"NEW PLAYER {player['name']} FIRST RANKED WIN: {starting_mmr} + {mmr_gain} = {new_mmr} (Streak: 1)")

                    self.players.insert_one({
                        "id": player_id,
                        "name": player["name"],
                        "mmr": new_mmr,  # Updated ranked MMR
                        "global_mmr": 300,  # Default global MMR
                        "wins": 1,
                        "global_wins": 0,
                        "losses": 0,
                        "global_losses": 0,
                        "matches": 1,
                        "global_matches": 0,
                        "streak": 1,  # First win, streak of 1
                        "global_streak": 0,  # No global streak yet
                        "created_at": datetime.datetime.utcnow(),
                        "last_updated": datetime.datetime.utcnow()
                    })

                    # Track MMR change for ranked
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": starting_mmr,
                        "new_mmr": new_mmr,
                        "mmr_change": mmr_gain,
                        "streak": 1,
                        "is_win": True,
                        "is_global": False
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
                # Existing player logic
                if is_global_match:
                    # Global match loss handling
                    global_matches = player_data.get("global_matches", 0) + 1
                    global_losses = player_data.get("global_losses", 0) + 1
                    old_mmr = player_data.get("global_mmr", 300)

                    # Update streak
                    current_streak = player_data.get("global_streak", 0)
                    # If current streak is positive (winning streak), reset it to -1
                    # Otherwise decrement the streak (making it more negative)
                    new_streak = -1 if current_streak >= 0 else current_streak - 1

                    # Calculate MMR loss with new algorithm including streak
                    mmr_loss = self.calculate_dynamic_mmr(
                        old_mmr,
                        player_team_avg,
                        opponent_avg,
                        global_matches,
                        is_win=False,
                        streak=new_streak
                    )

                    new_mmr = max(0, old_mmr - mmr_loss)  # Don't go below 0
                    print(
                        f"Player {player['name']} GLOBAL MMR update: {old_mmr} - {mmr_loss} = {new_mmr} (Streak: {new_streak})")

                    self.players.update_one(
                        {"id": player_id},
                        {"$set": {
                            "global_mmr": new_mmr,
                            "global_losses": global_losses,
                            "global_matches": global_matches,
                            "global_streak": new_streak,
                            "last_updated": datetime.datetime.utcnow()
                        }}
                    )

                    # Track MMR change for global loss
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": old_mmr,
                        "new_mmr": new_mmr,
                        "mmr_change": -mmr_loss,  # Negative for loss
                        "streak": new_streak,
                        "is_win": False,
                        "is_global": True
                    })
                else:
                    # Regular ranked match loss handling
                    matches_played = player_data.get("matches", 0) + 1
                    losses = player_data.get("losses", 0) + 1
                    old_mmr = player_data.get("mmr", 600)

                    # Update streak
                    current_streak = player_data.get("streak", 0)
                    # If current streak is positive (winning streak), reset it to -1
                    # Otherwise decrement the streak (making it more negative)
                    new_streak = -1 if current_streak >= 0 else current_streak - 1

                    # Calculate MMR loss with new algorithm including streak
                    mmr_loss = self.calculate_dynamic_mmr(
                        old_mmr,
                        player_team_avg,
                        opponent_avg,
                        matches_played,
                        is_win=False,
                        streak=new_streak
                    )

                    new_mmr = max(0, old_mmr - mmr_loss)  # Don't go below 0
                    print(
                        f"Player {player['name']} RANKED MMR update: {old_mmr} - {mmr_loss} = {new_mmr} (Streak: {new_streak})")

                    self.players.update_one(
                        {"id": player_id},
                        {"$set": {
                            "mmr": new_mmr,
                            "losses": losses,
                            "matches": matches_played,
                            "streak": new_streak,
                            "last_updated": datetime.datetime.utcnow()
                        }}
                    )

                    # Track MMR change for ranked loss
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": old_mmr,
                        "new_mmr": new_mmr,
                        "mmr_change": -mmr_loss,  # Negative for loss
                        "streak": new_streak,
                        "is_win": False,
                        "is_global": False
                    })
            else:
                # New player logic
                if is_global_match:
                    # New player's first global match - loss
                    # Logic for new player who loses their first global match
                    rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                    starting_global_mmr = 300  # Default global MMR

                    if rank_record and "global_mmr" in rank_record:
                        starting_global_mmr = rank_record.get("global_mmr", 300)

                    # Calculate first loss MMR
                    mmr_loss = self.calculate_dynamic_mmr(
                        starting_global_mmr,
                        player_team_avg,
                        opponent_avg,
                        1,  # First match
                        is_win=False,
                        streak=-1  # First loss, streak of -1
                    )

                    new_global_mmr = max(0, starting_global_mmr - mmr_loss)  # Don't go below 0
                    print(
                        f"NEW PLAYER {player['name']} FIRST GLOBAL LOSS: {starting_global_mmr} - {mmr_loss} = {new_global_mmr} (Streak: -1)")

                    # Get default ranked MMR from rank verification if available
                    starting_ranked_mmr = 600  # Default ranked MMR
                    if rank_record:
                        tier = rank_record.get("tier", "Rank C")
                        starting_ranked_mmr = self.TIER_MMR.get(tier, 600)

                    self.players.insert_one({
                        "id": player_id,
                        "name": player["name"],
                        "mmr": starting_ranked_mmr,  # Default ranked MMR
                        "global_mmr": new_global_mmr,  # Updated global MMR
                        "wins": 0,
                        "global_wins": 0,
                        "losses": 0,
                        "global_losses": 1,
                        "matches": 0,
                        "global_matches": 1,
                        "streak": 0,  # No ranked streak yet
                        "global_streak": -1,  # First loss, streak of -1
                        "created_at": datetime.datetime.utcnow(),
                        "last_updated": datetime.datetime.utcnow()
                    })

                    # Track MMR change for global loss
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": starting_global_mmr,
                        "new_mmr": new_global_mmr,
                        "mmr_change": -mmr_loss,  # Negative for loss
                        "streak": -1,
                        "is_win": False,
                        "is_global": True
                    })
                else:
                    # New player's first ranked match - loss
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
                        is_win=False,
                        streak=-1  # First loss, streak of -1
                    )

                    new_mmr = max(0, starting_mmr - mmr_loss)  # Don't go below 0
                    print(
                        f"NEW PLAYER {player['name']} FIRST RANKED LOSS: {starting_mmr} - {mmr_loss} = {new_mmr} (Streak: -1)")

                    self.players.insert_one({
                        "id": player_id,
                        "name": player["name"],
                        "mmr": new_mmr,  # Updated ranked MMR
                        "global_mmr": 300,  # Default global MMR
                        "wins": 0,
                        "global_wins": 0,
                        "losses": 1,
                        "global_losses": 0,
                        "matches": 1,
                        "global_matches": 0,
                        "streak": -1,  # First loss, streak of -1
                        "global_streak": 0,  # No global streak yet
                        "created_at": datetime.datetime.utcnow(),
                        "last_updated": datetime.datetime.utcnow()
                    })

                    # Track MMR change for ranked loss
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": starting_mmr,
                        "new_mmr": new_mmr,
                        "mmr_change": -mmr_loss,  # Negative for loss
                        "streak": -1,
                        "is_win": False,
                        "is_global": False
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

        # IMPROVED: Explicitly remove all players from the queue
        if hasattr(self, 'queue') and hasattr(self.queue, 'queue_collection'):
            try:
                # Remove all players from the queue for all teams
                for player in winning_team + losing_team:
                    player_id = player.get("id", "")
                    if player_id and not player_id.startswith('9000'):
                        self.queue.queue_collection.delete_many({"id": player_id})

                print(f"Successfully removed all players from the queue after match {match_id} completion")
            except Exception as e:
                print(f"Error clearing queue after match completion: {e}")

        # Clear any other active matches for this player
        await self.clear_player_active_matches(reporter_id)

        # Also clear for all other players in the match
        for player in winning_team + losing_team:
            player_id = player.get("id", "")
            if player_id and player_id != reporter_id and not player_id.startswith('9000'):
                await self.clear_player_active_matches(player_id)

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
                    # Only update Discord role based on ranked MMR, not global MMR
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
                    # Only update Discord role based on ranked MMR, not global MMR
                    mmr = player_data.get("mmr", 600)
                    # Update Discord role based on new MMR
                    await self.update_discord_role(ctx, player_id, mmr)

        # Remove from active matches
        if match["match_id"] in self.active_matches:
            del self.active_matches[match["match_id"]]

        # ADDED: Make sure to check and cancel any active votes or selections related to the match
        if hasattr(self, 'bot') and self.bot:
            # Check if vote_system and captains_system exist directly on the bot object
                if hasattr(self.bot, 'vote_system') and self.bot.vote_system:
                    self.bot.vote_system.cancel_voting(channel_id=channel_id)

                if hasattr(self.bot, 'captains_system') and self.bot.captains_system:
                    self.bot.captains_system.cancel_selection(channel_id=channel_id)

                # Also check for guild-level vote/captain coordinators
                for guild in self.bot.guilds:
                    for channel in guild.channels:
                        if str(channel.id) == channel_id:
                            try:
                                # Find and use the vote system coordinator
                                if hasattr(self.bot, 'vote_system_coordinator'):
                                    self.bot.vote_system_coordinator.cancel_voting(channel_id=channel_id)
                                    print(
                                        f"Cancelled votes via coordinator in channel {channel_id} after match completion")

                                # Find and use the captains system coordinator
                                if hasattr(self.bot, 'captains_system_coordinator'):
                                    self.bot.captains_system_coordinator.cancel_selection(channel_id=channel_id)
                                    print(
                                        f"Cancelled selections via coordinator in channel {channel_id} after match completion")
                            except Exception as e:
                                print(f"Error clearing votes/selections via coordinators: {e}")
                            break

        # Remove from active matches
        if match["match_id"] in self.active_matches:
            del self.active_matches[match["match_id"]]
            print(f"Removed match {match_id} from active_matches dictionary")

        # Return the updated match
        return updated_match, None

    def clear_player_match_status(self, player_id, new_match_id=None):
        """
        Clear a player's status in all matches except for a specific new match.
        This ensures no "ghost" statuses remain when a player joins a new match.
        """
        print(f"Clearing match status for player {player_id}")

        # Find all matches the player is in
        matches = list(self.matches.find({"players.id": player_id}))

        for match in matches:
            match_id = match.get("match_id")

            # Skip the new match we're creating (if provided)
            if new_match_id and match_id == new_match_id:
                continue

            match_status = match.get("status")
            print(f"Found match {match_id} with status {match_status}")

            # For matches in voting or selection, mark them as cancelled
            if match_status in ["voting", "selection"]:
                print(f"Cancelling match {match_id} for player {player_id}")
                self.matches.update_one(
                    {"match_id": match_id},
                    {"$set": {"status": "cancelled"}}
                )

    async def clear_player_active_matches(self, player_id):
        print(f"Clearing active matches for player: {player_id}")

        # Find all active matches for this player
        active_matches = list(self.matches.find({
            "players.id": player_id,
            "status": {"$in": ["voting", "selection", "in_progress"]}
        }))

        for match in active_matches:
            match_id = match.get("match_id", "unknown")
            status = match.get("status", "unknown")
            print(f"Found active match {match_id} (status: {status}) for player {player_id}, setting to completed")

            # Update match status to completed - use match_id for consistent updating
            result = self.matches.update_one(
                {"match_id": match_id},
                {"$set": {"status": "completed"}}
            )

            if result.modified_count == 0:
                print(f"WARNING: Failed to update match {match_id} by match_id, trying by _id")
                # Try again with _id
                self.matches.update_one(
                    {"_id": match["_id"]},
                    {"$set": {"status": "completed"}}
                )

            # Verify the match is now completed
            updated_match = self.matches.find_one({"match_id": match_id})
            if updated_match and updated_match.get("status") != "completed":
                print(
                    f"ERROR: Match {match_id} still has status '{updated_match.get('status')}' after attempted update")

        return len(active_matches)

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

    def calculate_dynamic_mmr(self, player_mmr, team_avg_mmr, opponent_avg_mmr, matches_played, is_win=True, streak=0):
        """
        Calculate dynamic MMR change based on:
        1. MMR difference between teams
        2. Number of matches played (for decay)
        3. Player's current win/loss streak

        Parameters:
        - player_mmr: Current MMR of the player
        - team_avg_mmr: Average MMR of the player's team
        - opponent_avg_mmr: Average MMR of the opposing team
        - matches_played: Number of matches the player has played (including the current one)
        - is_win: True if calculating for a win, False for a loss
        - streak: Current win/loss streak of the player (positive for wins, negative for losses)

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

        # Calculate streak factor (clamped between 0.8 and 1.5)
        # For wins: positive streak increases MMR gain (1.0 to 1.5)
        # For losses: negative streak increases MMR loss (1.0 to 1.5)
        streak_abs = abs(streak)
        streak_factor = 1.0

        if streak_abs > 0:
            # Cap the streak effect at 5 games
            capped_streak = min(streak_abs, 5)
            # Each win/loss in a streak increases the factor by 10% (up to 50% more)
            streak_factor = 1.0 + (capped_streak * 0.1)

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

        # Apply streak factor after all other calculations
        mmr_change = base_change * decay_multiplier * streak_factor

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

        # If found, ensure it has global MMR fields
        if player and "global_mmr" not in player:
            # Update the player with default global MMR fields
            self.players.update_one(
                {"id": player_id},
                {"$set": {
                    "global_mmr": 300,
                    "global_wins": 0,
                    "global_losses": 0,
                    "global_matches": 0
                }}
            )

            # Update our player object with the new fields
            player["global_mmr"] = 300
            player["global_wins"] = 0
            player["global_losses"] = 0
            player["global_matches"] = 0

        return player