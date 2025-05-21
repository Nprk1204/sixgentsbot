import math
import discord
import datetime
import uuid


class MatchSystem:
    def __init__(self, db, queue_manager=None):
        self.db = db
        self.matches = db.get_collection('matches')  # For completed match history
        self.players = db.get_collection('players')  # For player stats
        self.queue_manager = queue_manager  # Reference to queue manager for active matches
        self.bot = None

        # Tier-based MMR values
        self.TIER_MMR = {
            "Rank A": 1850,  # Grand Champion I and above
            "Rank B": 1350,  # Champion I to Champion III
            "Rank C": 600  # Diamond III and below - default
        }

    def set_bot(self, bot):
        """Set the bot instance"""
        self.bot = bot

    def set_queue_manager(self, queue_manager):
        """Set the queue manager reference"""
        self.queue_manager = queue_manager

    def create_match(self, match_id, team1, team2, channel_id, is_global=False):
        """Create a completed match entry in the database"""
        print(f"MatchSystem.create_match called with channel_id: {channel_id}, is_global: {is_global}")

        # Generate a shorter match ID if needed
        if not match_id or len(match_id) > 8:
            match_id = str(uuid.uuid4().hex)[:6]

        # If is_global wasn't explicitly provided, try to detect from channel
        if not is_global and self.bot:
            try:
                channel = self.bot.get_channel(int(channel_id))
                if channel:
                    is_global = channel.name.lower() == "global"
            except Exception as e:
                print(f"Error in channel detection: {e}")

        # Create match data
        match_data = {
            "match_id": match_id,
            "team1": team1,
            "team2": team2,
            "status": "in_progress",
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
        print(f"Created match with ID: {match_id}, status: in_progress, is_global: {is_global}")

        return match_id

    def get_active_match_by_channel(self, channel_id):
        """Get active match by channel ID (delegates to queue_manager)"""
        if self.queue_manager:
            return self.queue_manager.get_match_by_channel(channel_id, status="in_progress")
        return None

    async def report_match_by_id(self, match_id, reporter_id, result, ctx=None):
        """Report a match result by match ID and win/loss"""
        # Check if this is an active match in the queue manager
        active_match = None
        if self.queue_manager:
            active_match = self.queue_manager.get_match_by_id(match_id)

        # If not found in active matches, check the completed matches
        if not active_match:
            completed_match = self.matches.find_one({"match_id": match_id})
            if not completed_match:
                return None, "No match found with that ID."

            # If match exists but is already completed, return error
            if completed_match.get("status") != "in_progress":
                return None, "This match has already been reported."

            # Use the completed match data
            match = completed_match
        else:
            # Use the active match data
            match = active_match

        # Debug print to troubleshoot
        print(f"Reporting match {match_id}, current status: {match.get('status')}")

        # Check if reporter is in either team
        team1_ids = [p.get("id") for p in match.get("team1", [])]
        team2_ids = [p.get("id") for p in match.get("team2", [])]

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

        # Update match data with completion info
        now = datetime.datetime.utcnow()

        # Update match in the database
        result = self.matches.update_one(
            {"match_id": match_id, "status": "in_progress"},
            {"$set": {
                "status": "completed",
                "winner": winner,
                "score": {"team1": team1_score, "team2": team2_score},
                "completed_at": now,
                "reported_by": reporter_id
            }}
        )

        # If the match update was successful
        if result.modified_count == 0:
            # Double check if it exists but is already completed
            completed_match = self.matches.find_one({"match_id": match_id, "status": "completed"})
            if completed_match:
                return None, "This match has already been reported."
            else:
                return None, "Failed to update match. Please check the match ID."

        # Remove the match from active matches if it exists there
        if self.queue_manager:
            self.queue_manager.remove_match(match_id)

        # Get the updated match document
        updated_match = self.matches.find_one({"match_id": match_id})

        # Check if this is a global match
        is_global_match = updated_match.get("is_global", False)

        # Determine winning and losing teams
        if winner == 1:
            winning_team = match.get("team1", [])
            losing_team = match.get("team2", [])
        else:
            winning_team = match.get("team2", [])
            losing_team = match.get("team1", [])

        # Calculate team average MMRs for MMR adjustment calculation
        team1_mmrs = []
        team2_mmrs = []

        # Determine which MMR to use based on match type
        if is_global_match:
            # For global matches, use global MMR for calculations
            for player in match.get("team1", []):
                player_id = player.get("id")
                if player_id and not player_id.startswith('9000'):  # Skip dummy players
                    player_data = self.players.find_one({"id": player_id})
                    if player_data and "global_mmr" in player_data:
                        team1_mmrs.append(player_data.get("global_mmr", 300))
                    else:
                        team1_mmrs.append(300)  # Default global MMR

            for player in match.get("team2", []):
                player_id = player.get("id")
                if player_id and not player_id.startswith('9000'):  # Skip dummy players
                    player_data = self.players.find_one({"id": player_id})
                    if player_data and "global_mmr" in player_data:
                        team2_mmrs.append(player_data.get("global_mmr", 300))
                    else:
                        team2_mmrs.append(300)  # Default global MMR
        else:
            # For ranked matches, use regular MMR for calculations
            for player in match.get("team1", []):
                player_id = player.get("id")
                if player_id and not player_id.startswith('9000'):  # Skip dummy players
                    player_data = self.players.find_one({"id": player_id})
                    if player_data:
                        team1_mmrs.append(player_data.get("mmr", 600))
                    else:
                        # For new players, check rank record or use default
                        rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                        if rank_record:
                            tier = rank_record.get("tier", "Rank C")
                            team1_mmrs.append(self.TIER_MMR.get(tier, 600))
                        else:
                            team1_mmrs.append(600)  # Default MMR

            for player in match.get("team2", []):
                player_id = player.get("id")
                if player_id and not player_id.startswith('9000'):  # Skip dummy players
                    player_data = self.players.find_one({"id": player_id})
                    if player_data:
                        team2_mmrs.append(player_data.get("mmr", 600))
                    else:
                        # For new players, check rank record or use default
                        rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                        if rank_record:
                            tier = rank_record.get("tier", "Rank C")
                            team2_mmrs.append(self.TIER_MMR.get(tier, 600))
                        else:
                            team2_mmrs.append(600)  # Default MMR

        # Calculate average MMRs
        team1_avg_mmr = sum(team1_mmrs) / len(team1_mmrs) if team1_mmrs else 0
        team2_avg_mmr = sum(team2_mmrs) / len(team2_mmrs) if team2_mmrs else 0

        print(f"Team 1 avg MMR: {team1_avg_mmr}")
        print(f"Team 2 avg MMR: {team2_avg_mmr}")

        # Track MMR changes for each player
        mmr_changes = []

        # Update MMR for winners
        for player in winning_team:
            player_id = player.get("id")

            # Skip dummy players
            if not player_id or player_id.startswith('9000'):
                continue

            # Determine which team this player is on for average MMR calculations
            is_team1 = player in match.get("team1", [])
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

                    # Calculate MMR gain with dynamic algorithm
                    mmr_gain = self.calculate_dynamic_mmr(
                        old_mmr,
                        player_team_avg,
                        opponent_avg,
                        global_matches,
                        is_win=True
                    )

                    new_mmr = old_mmr + mmr_gain
                    print(
                        f"Player {player.get('name', 'Unknown')} GLOBAL MMR update: {old_mmr} + {mmr_gain} = {new_mmr}")

                    self.players.update_one(
                        {"id": player_id},
                        {"$set": {
                            "global_mmr": new_mmr,
                            "global_wins": global_wins,
                            "global_matches": global_matches,
                            "last_updated": datetime.datetime.utcnow()
                        }}
                    )

                    # Track MMR change for global
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": old_mmr,
                        "new_mmr": new_mmr,
                        "mmr_change": mmr_gain,
                        "is_win": True,
                        "is_global": True
                    })
                else:
                    # Regular ranked match win handling
                    matches_played = player_data.get("matches", 0) + 1
                    wins = player_data.get("wins", 0) + 1
                    old_mmr = player_data.get("mmr", 600)

                    # Calculate MMR gain with dynamic algorithm
                    mmr_gain = self.calculate_dynamic_mmr(
                        old_mmr,
                        player_team_avg,
                        opponent_avg,
                        matches_played,
                        is_win=True
                    )

                    new_mmr = old_mmr + mmr_gain
                    print(
                        f"Player {player.get('name', 'Unknown')} RANKED MMR update: {old_mmr} + {mmr_gain} = {new_mmr}")

                    self.players.update_one(
                        {"id": player_id},
                        {"$set": {
                            "mmr": new_mmr,
                            "wins": wins,
                            "matches": matches_played,
                            "last_updated": datetime.datetime.utcnow()
                        }}
                    )

                    # Track MMR change for ranked
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": old_mmr,
                        "new_mmr": new_mmr,
                        "mmr_change": mmr_gain,
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

                    # Calculate first win MMR with the dynamic algorithm
                    mmr_gain = self.calculate_dynamic_mmr(
                        starting_global_mmr,
                        player_team_avg,
                        opponent_avg,
                        1,  # First match
                        is_win=True
                    )

                    new_global_mmr = starting_global_mmr + mmr_gain
                    print(
                        f"NEW PLAYER {player.get('name', 'Unknown')} FIRST GLOBAL WIN: {starting_global_mmr} + {mmr_gain} = {new_global_mmr}")

                    # Get default ranked MMR from rank verification if available
                    starting_ranked_mmr = 600  # Default ranked MMR
                    if rank_record:
                        tier = rank_record.get("tier", "Rank C")
                        starting_ranked_mmr = self.TIER_MMR.get(tier, 600)

                    self.players.insert_one({
                        "id": player_id,
                        "name": player.get("name", "Unknown"),
                        "mmr": starting_ranked_mmr,  # Default ranked MMR
                        "global_mmr": new_global_mmr,  # Updated global MMR
                        "wins": 0,
                        "global_wins": 1,
                        "losses": 0,
                        "global_losses": 0,
                        "matches": 0,
                        "global_matches": 1,
                        "created_at": datetime.datetime.utcnow(),
                        "last_updated": datetime.datetime.utcnow()
                    })

                    # Track MMR change for global
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": starting_global_mmr,
                        "new_mmr": new_global_mmr,
                        "mmr_change": mmr_gain,
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

                    # Calculate first win MMR with the dynamic algorithm
                    mmr_gain = self.calculate_dynamic_mmr(
                        starting_mmr,
                        player_team_avg,
                        opponent_avg,
                        1,  # First match
                        is_win=True
                    )

                    new_mmr = starting_mmr + mmr_gain
                    print(
                        f"NEW PLAYER {player.get('name', 'Unknown')} FIRST RANKED WIN: {starting_mmr} + {mmr_gain} = {new_mmr}")

                    self.players.insert_one({
                        "id": player_id,
                        "name": player.get("name", "Unknown"),
                        "mmr": new_mmr,  # Updated ranked MMR
                        "global_mmr": 300,  # Default global MMR
                        "wins": 1,
                        "global_wins": 0,
                        "losses": 0,
                        "global_losses": 0,
                        "matches": 1,
                        "global_matches": 0,
                        "created_at": datetime.datetime.utcnow(),
                        "last_updated": datetime.datetime.utcnow()
                    })

                    # Track MMR change for ranked
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": starting_mmr,
                        "new_mmr": new_mmr,
                        "mmr_change": mmr_gain,
                        "is_win": True,
                        "is_global": False
                    })

        # Update MMR for losers
        for player in losing_team:
            player_id = player.get("id")

            # Skip dummy players
            if not player_id or player_id.startswith('9000'):
                continue

            # Determine which team this player is on for average MMR calculations
            is_team1 = player in match.get("team1", [])
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

                    # Calculate MMR loss with dynamic algorithm
                    mmr_loss = self.calculate_dynamic_mmr(
                        old_mmr,
                        player_team_avg,
                        opponent_avg,
                        global_matches,
                        is_win=False
                    )

                    new_mmr = max(0, old_mmr - mmr_loss)  # Don't go below 0
                    print(
                        f"Player {player.get('name', 'Unknown')} GLOBAL MMR update: {old_mmr} - {mmr_loss} = {new_mmr}")

                    self.players.update_one(
                        {"id": player_id},
                        {"$set": {
                            "global_mmr": new_mmr,
                            "global_losses": global_losses,
                            "global_matches": global_matches,
                            "last_updated": datetime.datetime.utcnow()
                        }}
                    )

                    # Track MMR change for global loss
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": old_mmr,
                        "new_mmr": new_mmr,
                        "mmr_change": -mmr_loss,  # Negative for loss
                        "is_win": False,
                        "is_global": True
                    })
                else:
                    # Regular ranked match loss handling
                    matches_played = player_data.get("matches", 0) + 1
                    losses = player_data.get("losses", 0) + 1
                    old_mmr = player_data.get("mmr", 600)

                    # Calculate MMR loss with dynamic algorithm
                    mmr_loss = self.calculate_dynamic_mmr(
                        old_mmr,
                        player_team_avg,
                        opponent_avg,
                        matches_played,
                        is_win=False
                    )

                    new_mmr = max(0, old_mmr - mmr_loss)  # Don't go below 0
                    print(
                        f"Player {player.get('name', 'Unknown')} RANKED MMR update: {old_mmr} - {mmr_loss} = {new_mmr}")

                    self.players.update_one(
                        {"id": player_id},
                        {"$set": {
                            "mmr": new_mmr,
                            "losses": losses,
                            "matches": matches_played,
                            "last_updated": datetime.datetime.utcnow()
                        }}
                    )

                    # Track MMR change for ranked loss
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": old_mmr,
                        "new_mmr": new_mmr,
                        "mmr_change": -mmr_loss,  # Negative for loss
                        "is_win": False,
                        "is_global": False
                    })
            else:
                # New player logic
                if is_global_match:
                    # New player's first global match - loss
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
                        is_win=False
                    )

                    new_global_mmr = max(0, starting_global_mmr - mmr_loss)  # Don't go below 0
                    print(
                        f"NEW PLAYER {player.get('name', 'Unknown')} FIRST GLOBAL LOSS: {starting_global_mmr} - {mmr_loss} = {new_global_mmr}")

                    # Get default ranked MMR from rank verification if available
                    starting_ranked_mmr = 600  # Default ranked MMR
                    if rank_record:
                        tier = rank_record.get("tier", "Rank C")
                        starting_ranked_mmr = self.TIER_MMR.get(tier, 600)

                    self.players.insert_one({
                        "id": player_id,
                        "name": player.get("name", "Unknown"),
                        "mmr": starting_ranked_mmr,  # Default ranked MMR
                        "global_mmr": new_global_mmr,  # Updated global MMR
                        "wins": 0,
                        "global_wins": 0,
                        "losses": 0,
                        "global_losses": 1,
                        "matches": 0,
                        "global_matches": 1,
                        "created_at": datetime.datetime.utcnow(),
                        "last_updated": datetime.datetime.utcnow()
                    })

                    # Track MMR change for global loss
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": starting_global_mmr,
                        "new_mmr": new_global_mmr,
                        "mmr_change": -mmr_loss,  # Negative for loss
                        "is_win": False,
                        "is_global": True
                    })
                else:
                    # New player's first ranked match - loss
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
                    print(
                        f"NEW PLAYER {player.get('name', 'Unknown')} FIRST RANKED LOSS: {starting_mmr} - {mmr_loss} = {new_mmr}")

                    self.players.insert_one({
                        "id": player_id,
                        "name": player.get("name", "Unknown"),
                        "mmr": new_mmr,  # Updated ranked MMR
                        "global_mmr": 300,  # Default global MMR
                        "wins": 0,
                        "global_wins": 0,
                        "losses": 1,
                        "global_losses": 0,
                        "matches": 1,
                        "global_matches": 0,
                        "created_at": datetime.datetime.utcnow(),
                        "last_updated": datetime.datetime.utcnow()
                    })

                    # Track MMR change for ranked loss
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": starting_mmr,
                        "new_mmr": new_mmr,
                        "mmr_change": -mmr_loss,  # Negative for loss
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

        # Update Discord roles for players if ctx is provided
        if ctx:
            # Update roles for winners based on ranked MMR (not global)
            for player in winning_team:
                player_id = player.get("id")
                if not player_id or player_id.startswith('9000'):  # Skip dummy players
                    continue

                # Only update roles for ranked MMR changes, not global
                if not is_global_match:
                    player_data = self.players.find_one({"id": player_id})
                    if player_data:
                        mmr = player_data.get("mmr", 600)
                        await self.update_discord_role(ctx, player_id, mmr)

            # Update roles for losers based on ranked MMR (not global)
            for player in losing_team:
                player_id = player.get("id")
                if not player_id or player_id.startswith('9000'):  # Skip dummy players
                    continue

                # Only update roles for ranked MMR changes, not global
                if not is_global_match:
                    player_data = self.players.find_one({"id": player_id})
                    if player_data:
                        mmr = player_data.get("mmr", 600)
                        await self.update_discord_role(ctx, player_id, mmr)

        return updated_match, None