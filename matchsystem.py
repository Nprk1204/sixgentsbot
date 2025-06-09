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

        # ENHANCED: Rank boundaries for protection system
        self.RANK_BOUNDARIES = {
            "Rank C": {"min": 0, "max": 1099},
            "Rank B": {"min": 1100, "max": 1599},
            "Rank A": {"min": 1600, "max": 9999}
        }

    def set_bot(self, bot):
        """Set the bot instance"""
        self.bot = bot

    def set_queue_manager(self, queue_manager):
        """Set the queue manager reference"""
        self.queue_manager = queue_manager

    def create_match(self, match_id, team1, team2, channel_id, is_global=False):
        """Create a completed match entry in the database"""
        print(
            f"MatchSystem.create_match called with match_id: {match_id}, channel_id: {channel_id}, is_global: {is_global}")

        # Generate a shorter match ID if needed
        if not match_id or len(match_id) > 8:
            match_id = str(uuid.uuid4().hex)[:6]
            print(f"Generated new short match ID: {match_id}")

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

        # Check if this match already exists in the database
        existing_match = self.matches.find_one({"match_id": match_id})
        if existing_match:
            print(f"Match {match_id} already exists in database. Updating it.")
            # Update the existing match
            self.matches.update_one(
                {"match_id": match_id},
                {"$set": {
                    "team1": team1,
                    "team2": team2,
                    "status": "in_progress",
                    "is_global": is_global
                }}
            )
        else:
            # Insert as a new match
            print(f"Creating new match in database: {match_id}")
            self.matches.insert_one(match_data)

        print(f"Match {match_id} successfully created/updated in database")
        return match_id

    def get_active_match_by_channel(self, channel_id):
        """Get active match by channel ID (delegates to queue_manager)"""
        if self.queue_manager:
            return self.queue_manager.get_match_by_channel(channel_id, status="in_progress")
        return None

    async def report_match_by_id(self, match_id, reporter_id, result, ctx=None):
        """Report a match result by match ID and win/loss"""
        # Clean the match ID first (remove any potential long format)
        match_id = match_id.strip()
        if len(match_id) > 8:  # If it's longer than our standard format
            match_id = match_id[:6]  # Take just the first 6 characters

        # Debug print match ID being searched
        print(f"Looking for match with ID: {match_id}")

        # Check if this is an active match in the queue manager
        active_match = None
        if self.queue_manager:
            active_match = self.queue_manager.get_match_by_id(match_id)
            if active_match:
                print(f"Found active match with ID {match_id}")
            else:
                print(f"No active match found with ID {match_id}")

        # If not found in active matches, check the completed matches
        if not active_match:
            completed_match = self.matches.find_one({"match_id": match_id})
            if completed_match:
                print(f"Found match in completed matches collection: {match_id}")

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
        print(f"Reporter ID: {reporter_id}")

        team1 = match.get("team1", [])
        team2 = match.get("team2", [])

        # Check if teams are empty and try to get them from the database
        if (not team1 or not team2) and self.matches is not None:
            print(f"Teams are empty or missing. Looking up match in database: {match_id}")
            db_match = self.matches.find_one({"match_id": match_id})
            if db_match:
                db_team1 = db_match.get("team1", [])
                db_team2 = db_match.get("team2", [])
                if db_team1 and db_team2:
                    print(f"Found match in database with teams. Using that data instead.")
                    team1 = db_team1
                    team2 = db_team2
                    match = db_match

        # Convert IDs to strings for consistent comparison
        team1_ids = [str(p.get("id", "")) for p in team1]
        team2_ids = [str(p.get("id", "")) for p in team2]

        # Debug print team members and their IDs
        print(f"Team 1 IDs: {team1_ids}")
        print(f"Team 2 IDs: {team2_ids}")
        print(f"Checking if reporter ID: {reporter_id} is in either team")

        # Fix: Convert reporter_id to string to ensure consistent comparison
        reporter_id = str(reporter_id)

        # Check both teams for reporter's ID
        reporter_in_team1 = reporter_id in team1_ids
        reporter_in_team2 = reporter_id in team2_ids

        if reporter_in_team1:
            reporter_team = 1
            print(f"Reporter found in team 1")
        elif reporter_in_team2:
            reporter_team = 2
            print(f"Reporter found in team 2")
        else:
            print(f"Reporter {reporter_id} not found in either team")

            # Check if reporter is in player_matches tracking
            if self.queue_manager and reporter_id in self.queue_manager.player_matches:
                player_match_id = self.queue_manager.player_matches[reporter_id]
                if player_match_id == match_id:
                    print(f"Reporter found in player_matches tracking for this match. Allowing report.")
                    # Determine team based on other evidence
                    if len(team1) > 0 and len(team2) > 0:
                        # If there are players in both teams, just assign to team 1 for now
                        reporter_team = 1
                    else:
                        return None, "Match teams are not properly set up. Please contact an admin."
                else:
                    return None, f"You are in a different match (ID: {player_match_id})."
            else:
                # If we got here, the reporter is not found anywhere
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

        # Check if this is a global match
        is_global_match = match.get("is_global", False)
        print(f"Match is global: {is_global_match}")

        # Determine winning and losing teams
        if winner == 1:
            winning_team = match.get("team1", [])
            losing_team = match.get("team2", [])
        else:
            winning_team = match.get("team2", [])
            losing_team = match.get("team1", [])

        print(f"Processing MMR updates for {len(winning_team)} winners and {len(losing_team)} losers")

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

        # Initialize MMR changes list to track all changes
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

                    # Get current global streak info and update for winner
                    global_current_streak = player_data.get("global_current_streak", 0)
                    new_global_streak = global_current_streak + 1 if global_current_streak >= 0 else 1
                    global_longest_win_streak = max(player_data.get("global_longest_win_streak", 0), new_global_streak)

                    # Calculate MMR gain with enhanced dynamic algorithm
                    mmr_gain = self.calculate_dynamic_mmr(
                        old_mmr,
                        player_team_avg,
                        opponent_avg,
                        global_matches,
                        is_win=True,
                        streak=new_global_streak,
                        player_data=player_data
                    )

                    new_mmr = old_mmr + mmr_gain
                    print(
                        f"Player {player.get('name', 'Unknown')} GLOBAL MMR update: {old_mmr} + {mmr_gain} = {new_mmr}")

                    # Update with ALL global streak fields
                    self.players.update_one(
                        {"id": player_id},
                        {"$set": {
                            "global_mmr": new_mmr,
                            "global_wins": global_wins,
                            "global_matches": global_matches,
                            "global_current_streak": new_global_streak,
                            "global_longest_win_streak": global_longest_win_streak,
                            "global_longest_loss_streak": player_data.get("global_longest_loss_streak", 0),
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
                        "is_global": True,
                        "streak": new_global_streak
                    })
                    print(f"Added global MMR change for {player.get('name', 'Unknown')}: +{mmr_gain}")
                else:
                    # Regular ranked match win handling
                    matches_played = player_data.get("matches", 0) + 1
                    wins = player_data.get("wins", 0) + 1
                    old_mmr = player_data.get("mmr", 600)

                    # Get current streak info and update for winner
                    current_streak = player_data.get("current_streak", 0)
                    new_streak = current_streak + 1 if current_streak >= 0 else 1
                    longest_win_streak = max(player_data.get("longest_win_streak", 0), new_streak)

                    # Calculate MMR gain with enhanced dynamic algorithm
                    mmr_gain = self.calculate_dynamic_mmr(
                        old_mmr,
                        player_team_avg,
                        opponent_avg,
                        matches_played,
                        is_win=True,
                        streak=new_streak,
                        player_data=player_data
                    )

                    new_mmr = old_mmr + mmr_gain
                    print(
                        f"Player {player.get('name', 'Unknown')} RANKED MMR update: {old_mmr} + {mmr_gain} = {new_mmr}")

                    # Check for rank changes and track promotions
                    old_rank_tier = self.get_rank_tier_from_mmr(old_mmr)
                    new_rank_tier = self.get_rank_tier_from_mmr(new_mmr)

                    update_data = {
                        "mmr": new_mmr,
                        "wins": wins,
                        "matches": matches_played,
                        "current_streak": new_streak,
                        "longest_win_streak": longest_win_streak,
                        "longest_loss_streak": player_data.get("longest_loss_streak", 0),
                        "last_updated": datetime.datetime.utcnow()
                    }

                    # Track promotions for rank protection
                    if new_rank_tier != old_rank_tier and new_rank_tier > old_rank_tier:
                        update_data["last_promotion"] = {
                            "matches_at_promotion": matches_played,
                            "promoted_at": datetime.datetime.utcnow(),
                            "from_rank": old_rank_tier,
                            "to_rank": new_rank_tier,
                            "mmr_at_promotion": new_mmr
                        }
                        print(
                            f"🎉 Player {player.get('name', 'Unknown')} promoted from {old_rank_tier} to {new_rank_tier}!")

                    # Update with ALL ranked streak fields
                    self.players.update_one({"id": player_id}, {"$set": update_data})

                    # Track MMR change for ranked
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": old_mmr,
                        "new_mmr": new_mmr,
                        "mmr_change": mmr_gain,
                        "is_win": True,
                        "is_global": False,
                        "streak": new_streak
                    })
                    print(f"Added ranked MMR change for {player.get('name', 'Unknown')}: +{mmr_gain}")
            else:
                # New player logic
                if is_global_match:
                    # New player's first global match - win
                    rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                    starting_global_mmr = 300  # Default global MMR

                    if rank_record and "global_mmr" in rank_record:
                        starting_global_mmr = rank_record.get("global_mmr", 300)

                    # Calculate first win MMR with the enhanced algorithm
                    mmr_gain = self.calculate_dynamic_mmr(
                        starting_global_mmr,
                        player_team_avg,
                        opponent_avg,
                        1,  # First match
                        is_win=True,
                        streak=1,
                        player_data=None  # No existing data for new player
                    )

                    new_global_mmr = starting_global_mmr + mmr_gain
                    print(
                        f"NEW PLAYER {player.get('name', 'Unknown')} FIRST GLOBAL WIN: {starting_global_mmr} + {mmr_gain} = {new_global_mmr}")

                    # Get default ranked MMR from rank verification if available
                    starting_ranked_mmr = 600  # Default ranked MMR
                    if rank_record:
                        tier = rank_record.get("tier", "Rank C")
                        starting_ranked_mmr = self.TIER_MMR.get(tier, 600)

                    # Initialize new global player with ALL streak fields
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
                        "current_streak": 0,
                        "longest_win_streak": 0,
                        "longest_loss_streak": 0,
                        "global_current_streak": 1,
                        "global_longest_win_streak": 1,
                        "global_longest_loss_streak": 0,
                        "last_promotion": None,  # Initialize promotion tracking
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
                        "is_global": True,
                        "streak": 1
                    })
                    print(f"Added new player global MMR change for {player.get('name', 'Unknown')}: +{mmr_gain}")
                else:
                    # New player's first ranked match - win
                    rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                    starting_mmr = 600  # Default MMR

                    if rank_record:
                        tier = rank_record.get("tier", "Rank C")
                        starting_mmr = self.TIER_MMR.get(tier, 600)

                    # Calculate first win MMR with the enhanced algorithm
                    mmr_gain = self.calculate_dynamic_mmr(
                        starting_mmr,
                        player_team_avg,
                        opponent_avg,
                        1,  # First match
                        is_win=True,
                        streak=1,
                        player_data=None  # No existing data for new player
                    )

                    new_mmr = starting_mmr + mmr_gain
                    print(
                        f"NEW PLAYER {player.get('name', 'Unknown')} FIRST RANKED WIN: {starting_mmr} + {mmr_gain} = {new_mmr}")

                    # Initialize new ranked player with ALL streak fields
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
                        "current_streak": 1,
                        "longest_win_streak": 1,
                        "longest_loss_streak": 0,
                        "global_current_streak": 0,
                        "global_longest_win_streak": 0,
                        "global_longest_loss_streak": 0,
                        "last_promotion": None,  # Initialize promotion tracking
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
                        "is_global": False,
                        "streak": 1
                    })
                    print(f"Added new player ranked MMR change for {player.get('name', 'Unknown')}: +{mmr_gain}")

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

                    # Get current global streak info and update for loser
                    global_current_streak = player_data.get("global_current_streak", 0)
                    new_global_streak = global_current_streak - 1 if global_current_streak <= 0 else -1
                    global_longest_loss_streak = min(player_data.get("global_longest_loss_streak", 0),
                                                     new_global_streak)

                    # Calculate MMR loss with enhanced dynamic algorithm
                    mmr_loss = self.calculate_dynamic_mmr(
                        old_mmr,
                        player_team_avg,
                        opponent_avg,
                        global_matches,
                        is_win=False,
                        streak=new_global_streak,
                        player_data=player_data
                    )

                    new_mmr = max(0, old_mmr - mmr_loss)  # Don't go below 0
                    print(
                        f"Player {player.get('name', 'Unknown')} GLOBAL MMR update: {old_mmr} - {mmr_loss} = {new_mmr}")

                    # Update with ALL global streak fields
                    self.players.update_one(
                        {"id": player_id},
                        {"$set": {
                            "global_mmr": new_mmr,
                            "global_losses": global_losses,
                            "global_matches": global_matches,
                            "global_current_streak": new_global_streak,
                            "global_longest_loss_streak": global_longest_loss_streak,
                            "global_longest_win_streak": player_data.get("global_longest_win_streak", 0),
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
                        "is_global": True,
                        "streak": new_global_streak
                    })
                    print(f"Added global MMR change for {player.get('name', 'Unknown')}: -{mmr_loss}")
                else:
                    # Regular ranked match loss handling
                    matches_played = player_data.get("matches", 0) + 1
                    losses = player_data.get("losses", 0) + 1
                    old_mmr = player_data.get("mmr", 600)

                    # Get current streak info and update for loser
                    current_streak = player_data.get("current_streak", 0)
                    new_streak = current_streak - 1 if current_streak <= 0 else -1
                    longest_loss_streak = min(player_data.get("longest_loss_streak", 0), new_streak)

                    # Calculate MMR loss with enhanced dynamic algorithm
                    mmr_loss = self.calculate_dynamic_mmr(
                        old_mmr,
                        player_team_avg,
                        opponent_avg,
                        matches_played,
                        is_win=False,
                        streak=new_streak,
                        player_data=player_data
                    )

                    new_mmr = max(0, old_mmr - mmr_loss)  # Don't go below 0
                    print(
                        f"Player {player.get('name', 'Unknown')} RANKED MMR update: {old_mmr} - {mmr_loss} = {new_mmr}")

                    # Check for rank changes (demotions)
                    old_rank_tier = self.get_rank_tier_from_mmr(old_mmr)
                    new_rank_tier = self.get_rank_tier_from_mmr(new_mmr)

                    update_data = {
                        "mmr": new_mmr,
                        "losses": losses,
                        "matches": matches_played,
                        "current_streak": new_streak,
                        "longest_loss_streak": longest_loss_streak,
                        "longest_win_streak": player_data.get("longest_win_streak", 0),
                        "last_updated": datetime.datetime.utcnow()
                    }

                    # Track demotions (though we don't give protection for demotions currently)
                    if new_rank_tier != old_rank_tier and new_rank_tier < old_rank_tier:
                        print(
                            f"📉 Player {player.get('name', 'Unknown')} demoted from {old_rank_tier} to {new_rank_tier}")

                    # Update with ALL ranked streak fields
                    self.players.update_one({"id": player_id}, {"$set": update_data})

                    # Track MMR change for ranked loss
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": old_mmr,
                        "new_mmr": new_mmr,
                        "mmr_change": -mmr_loss,  # Negative for loss
                        "is_win": False,
                        "is_global": False,
                        "streak": new_streak
                    })
                    print(f"Added ranked MMR change for {player.get('name', 'Unknown')}: -{mmr_loss}")
            else:
                # New player logic for losers
                if is_global_match:
                    # New player's first global match - loss
                    rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                    starting_global_mmr = 300  # Default global MMR

                    if rank_record and "global_mmr" in rank_record:
                        starting_global_mmr = rank_record.get("global_mmr", 300)

                    # Calculate first loss MMR with the enhanced algorithm
                    mmr_loss = self.calculate_dynamic_mmr(
                        starting_global_mmr,
                        player_team_avg,
                        opponent_avg,
                        1,  # First match
                        is_win=False,
                        streak=-1,
                        player_data=None  # No existing data for new player
                    )

                    new_global_mmr = max(0, starting_global_mmr - mmr_loss)
                    print(
                        f"NEW PLAYER {player.get('name', 'Unknown')} FIRST GLOBAL LOSS: {starting_global_mmr} - {mmr_loss} = {new_global_mmr}")

                    # Get default ranked MMR from rank verification if available
                    starting_ranked_mmr = 600  # Default ranked MMR
                    if rank_record:
                        tier = rank_record.get("tier", "Rank C")
                        starting_ranked_mmr = self.TIER_MMR.get(tier, 600)

                    # Initialize new global player with ALL streak fields
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
                        "current_streak": 0,
                        "longest_win_streak": 0,
                        "longest_loss_streak": 0,
                        "global_current_streak": -1,
                        "global_longest_win_streak": 0,
                        "global_longest_loss_streak": -1,
                        "last_promotion": None,  # Initialize promotion tracking
                        "created_at": datetime.datetime.utcnow(),
                        "last_updated": datetime.datetime.utcnow()
                    })

                    # Track MMR change for global
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": starting_global_mmr,
                        "new_mmr": new_global_mmr,
                        "mmr_change": -mmr_loss,
                        "is_win": False,
                        "is_global": True,
                        "streak": -1
                    })
                    print(f"Added new player global MMR change for {player.get('name', 'Unknown')}: -{mmr_loss}")
                else:
                    # New player's first ranked match - loss
                    rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                    starting_mmr = 600  # Default MMR

                    if rank_record:
                        tier = rank_record.get("tier", "Rank C")
                        starting_mmr = self.TIER_MMR.get(tier, 600)

                    # Calculate first loss MMR with the enhanced algorithm
                    mmr_loss = self.calculate_dynamic_mmr(
                        starting_mmr,
                        player_team_avg,
                        opponent_avg,
                        1,  # First match
                        is_win=False,
                        streak=-1,
                        player_data=None  # No existing data for new player
                    )

                    new_mmr = max(0, starting_mmr - mmr_loss)
                    print(
                        f"NEW PLAYER {player.get('name', 'Unknown')} FIRST RANKED LOSS: {starting_mmr} - {mmr_loss} = {new_mmr}")

                    # Initialize new ranked player with ALL streak fields
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
                        "current_streak": -1,
                        "longest_win_streak": 0,
                        "longest_loss_streak": -1,
                        "global_current_streak": 0,
                        "global_longest_win_streak": 0,
                        "global_longest_loss_streak": 0,
                        "last_promotion": None,  # Initialize promotion tracking
                        "created_at": datetime.datetime.utcnow(),
                        "last_updated": datetime.datetime.utcnow()
                    })

                    # Track MMR change for ranked
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": starting_mmr,
                        "new_mmr": new_mmr,
                        "mmr_change": -mmr_loss,
                        "is_win": False,
                        "is_global": False,
                        "streak": -1
                    })
                    print(f"Added new player ranked MMR change for {player.get('name', 'Unknown')}: -{mmr_loss}")

        # Store the MMR changes in the match document
        print(f"Storing {len(mmr_changes)} MMR changes in match document")
        self.matches.update_one(
            {"match_id": match_id},
            {"$set": {
                "mmr_changes": mmr_changes,
                "team1_avg_mmr": team1_avg_mmr,
                "team2_avg_mmr": team2_avg_mmr
            }}
        )

        print(f"MMR changes stored successfully for match {match_id}")

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

        if self.queue_manager:
            self.queue_manager.remove_match(match_id)

        # Return a match result object that includes the MMR changes
        match_result = {
            "match_id": match_id,
            "team1": team1,
            "team2": team2,
            "winner": winner,
            "score": {"team1": team1_score, "team2": team2_score},
            "completed_at": now,
            "reported_by": reporter_id,
            "is_global": is_global_match,
            "mmr_changes": mmr_changes,
            "team1_avg_mmr": team1_avg_mmr,
            "team2_avg_mmr": team2_avg_mmr,
            "status": "completed"
        }

        return match_result, None

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

            # Add the new role
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
        """Update MMR for all players in the match with enhanced dynamic MMR changes"""
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
                    tier = rank_record.get("tier", "Rank C")
                    winning_team_mmrs.append(self.TIER_MMR.get(tier, 600))
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
                    tier = rank_record.get("tier", "Rank C")
                    losing_team_mmrs.append(self.TIER_MMR.get(tier, 600))
                else:
                    # Use tier-based default
                    losing_team_mmrs.append(600)  # Default to Rank C MMR

        # Calculate average MMRs for each team
        winning_team_avg_mmr = sum(winning_team_mmrs) / len(winning_team_mmrs) if winning_team_mmrs else 0
        losing_team_avg_mmr = sum(losing_team_mmrs) / len(losing_team_mmrs) if losing_team_mmrs else 0

        print(f"Winning team avg MMR: {winning_team_avg_mmr}")
        print(f"Losing team avg MMR: {losing_team_avg_mmr}")

        # Add tracking for streak changes
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

                # Get current streak info or initialize
                current_streak = player_data.get("current_streak", 0)
                # Positive number means win streak, negative means loss streak

                # Update streak - player won, so streak increases or resets from negative
                new_streak = current_streak + 1 if current_streak >= 0 else 1
                longest_win_streak = max(player_data.get("longest_win_streak", 0), new_streak)

                # Calculate MMR gain with enhanced algorithm
                mmr_gain = self.calculate_dynamic_mmr(
                    old_mmr,
                    winning_team_avg_mmr,
                    losing_team_avg_mmr,
                    matches_played,
                    is_win=True,
                    streak=new_streak,
                    player_data=player_data
                )

                new_mmr = old_mmr + mmr_gain
                print(f"Player {player['name']} MMR update: {old_mmr} + {mmr_gain} = {new_mmr} (Streak: {new_streak})")

                # Check for rank changes and track promotions
                old_rank_tier = self.get_rank_tier_from_mmr(old_mmr)
                new_rank_tier = self.get_rank_tier_from_mmr(new_mmr)

                update_data = {
                    "mmr": new_mmr,
                    "wins": wins,
                    "matches": matches_played,
                    "current_streak": new_streak,
                    "longest_win_streak": longest_win_streak,
                    "longest_loss_streak": player_data.get("longest_loss_streak", 0),
                    "last_updated": datetime.datetime.utcnow()
                }

                # Track promotions for rank protection
                if new_rank_tier != old_rank_tier and new_rank_tier > old_rank_tier:
                    update_data["last_promotion"] = {
                        "matches_at_promotion": matches_played,
                        "promoted_at": datetime.datetime.utcnow(),
                        "from_rank": old_rank_tier,
                        "to_rank": new_rank_tier,
                        "mmr_at_promotion": new_mmr
                    }
                    print(f"🎉 Player {player['name']} promoted from {old_rank_tier} to {new_rank_tier}!")

                # Update with ALL streak fields for winners
                self.players.update_one({"id": player_id}, {"$set": update_data})

                # Track MMR change
                mmr_changes.append({
                    "player_id": player_id,
                    "old_mmr": old_mmr,
                    "new_mmr": new_mmr,
                    "mmr_change": mmr_gain,
                    "is_win": True,
                    "streak": new_streak
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

                # Calculate first win MMR with the enhanced algorithm
                mmr_gain = self.calculate_dynamic_mmr(
                    starting_mmr,
                    winning_team_avg_mmr,
                    losing_team_avg_mmr,
                    1,  # First match
                    is_win=True,
                    streak=1,
                    player_data=None  # No existing data for new player
                )

                new_mmr = starting_mmr + mmr_gain
                print(f"NEW PLAYER {player['name']} FIRST WIN: {starting_mmr} + {mmr_gain} = {new_mmr}")

                # Initialize player record with ALL streak information
                self.players.insert_one({
                    "id": player_id,
                    "name": player["name"],
                    "mmr": new_mmr,
                    "global_mmr": 300,  # Default global MMR
                    "wins": 1,
                    "global_wins": 0,
                    "losses": 0,
                    "global_losses": 0,
                    "matches": 1,
                    "global_matches": 0,
                    "current_streak": 1,  # Start with a win streak of 1
                    "longest_win_streak": 1,
                    "longest_loss_streak": 0,
                    "global_current_streak": 0,
                    "global_longest_win_streak": 0,
                    "global_longest_loss_streak": 0,
                    "last_promotion": None,  # Initialize promotion tracking
                    "created_at": datetime.datetime.utcnow(),
                    "last_updated": datetime.datetime.utcnow()
                })

                # Track MMR change for new player
                mmr_changes.append({
                    "player_id": player_id,
                    "old_mmr": starting_mmr,
                    "new_mmr": new_mmr,
                    "mmr_change": mmr_gain,
                    "is_win": True,
                    "streak": 1
                })

        # Process losers with enhanced logic
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

                # Get current streak info
                current_streak = player_data.get("current_streak", 0)

                # Update streak - player lost, so streak decreases or resets from positive
                new_streak = current_streak - 1 if current_streak <= 0 else -1
                longest_loss_streak = min(player_data.get("longest_loss_streak", 0), new_streak)

                # Calculate MMR loss with enhanced algorithm
                mmr_loss = self.calculate_dynamic_mmr(
                    old_mmr,
                    losing_team_avg_mmr,
                    winning_team_avg_mmr,
                    matches_played,
                    is_win=False,
                    streak=new_streak,
                    player_data=player_data
                )

                new_mmr = max(0, old_mmr - mmr_loss)  # Don't go below 0
                print(f"Player {player['name']} MMR update: {old_mmr} - {mmr_loss} = {new_mmr} (Streak: {new_streak})")

                # Check for rank changes (demotions)
                old_rank_tier = self.get_rank_tier_from_mmr(old_mmr)
                new_rank_tier = self.get_rank_tier_from_mmr(new_mmr)

                update_data = {
                    "mmr": new_mmr,
                    "losses": losses,
                    "matches": matches_played,
                    "current_streak": new_streak,
                    "longest_win_streak": player_data.get("longest_win_streak", 0),
                    "longest_loss_streak": longest_loss_streak,
                    "last_updated": datetime.datetime.utcnow()
                }

                # Track demotions (though we don't give protection for demotions currently)
                if new_rank_tier != old_rank_tier and new_rank_tier < old_rank_tier:
                    print(f"📉 Player {player['name']} demoted from {old_rank_tier} to {new_rank_tier}")

                # Update with ALL streak fields for losers
                self.players.update_one({"id": player_id}, {"$set": update_data})

                # Track MMR change
                mmr_changes.append({
                    "player_id": player_id,
                    "old_mmr": old_mmr,
                    "new_mmr": new_mmr,
                    "mmr_change": -mmr_loss,  # Negative for loss
                    "is_win": False,
                    "streak": new_streak
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

                # Calculate first loss MMR with enhanced algorithm
                mmr_loss = self.calculate_dynamic_mmr(
                    starting_mmr,
                    losing_team_avg_mmr,
                    winning_team_avg_mmr,
                    1,  # First match
                    is_win=False,
                    streak=-1,
                    player_data=None  # No existing data for new player
                )

                new_mmr = max(0, starting_mmr - mmr_loss)  # Don't go below 0
                print(f"NEW PLAYER {player['name']} FIRST LOSS: {starting_mmr} - {mmr_loss} = {new_mmr}")

                # Initialize player record with ALL streak information
                self.players.insert_one({
                    "id": player_id,
                    "name": player["name"],
                    "mmr": new_mmr,
                    "global_mmr": 300,  # Default global MMR
                    "wins": 0,
                    "global_wins": 0,
                    "losses": 1,
                    "global_losses": 0,
                    "matches": 1,
                    "global_matches": 0,
                    "current_streak": -1,  # Start with a loss streak of -1
                    "longest_win_streak": 0,
                    "longest_loss_streak": -1,
                    "global_current_streak": 0,
                    "global_longest_win_streak": 0,
                    "global_longest_loss_streak": 0,
                    "last_promotion": None,  # Initialize promotion tracking
                    "created_at": datetime.datetime.utcnow(),
                    "last_updated": datetime.datetime.utcnow()
                })

                # Track MMR change for new player
                mmr_changes.append({
                    "player_id": player_id,
                    "old_mmr": starting_mmr,
                    "new_mmr": new_mmr,
                    "mmr_change": -mmr_loss,  # Negative for loss
                    "is_win": False,
                    "streak": -1
                })

        # Store the MMR changes and team average MMRs in the match document
        if match_id:
            self.matches.update_one(
                {"match_id": match_id},
                {"$set": {
                    "mmr_changes": mmr_changes,
                    "team1_avg_mmr": winning_team_avg_mmr,
                    "team2_avg_mmr": losing_team_avg_mmr
                }}
            )

            print(f"Stored MMR changes and team averages for match {match_id}")

    def calculate_dynamic_mmr(self, player_mmr, team_avg_mmr, opponent_avg_mmr, matches_played, is_win=True, streak=0,
                              player_data=None):
        """
        ENHANCED Calculate dynamic MMR change based on:
        1. MMR difference between teams
        2. Number of matches played (for decay)
        3. Win/loss streak with 2x multiplier
        4. Momentum system (recent performance)
        5. Rank boundary protection

        Parameters:
        - player_mmr: Current MMR of the player
        - team_avg_mmr: Average MMR of the player's team
        - opponent_avg_mmr: Average MMR of the opposing team
        - matches_played: Number of matches the player has played (including the current one)
        - is_win: True if calculating for a win, False for a loss
        - streak: Current streak value (positive for win streak, negative for loss streak)
        - player_data: Full player data object for momentum and rank protection calculations

        Returns:
        - MMR change amount
        """
        # Base values for MMR changes
        BASE_MMR_CHANGE = 25  # Standard MMR change for evenly matched teams for experienced players

        # First 15 games give higher MMR changes for placement
        FIRST_GAME_WIN = 110  # Base value for first win
        FIRST_GAME_LOSS = 80  # Base value for first loss

        MAX_MMR_CHANGE = 200  # Maximum for extreme cases with multipliers
        MIN_MMR_CHANGE = 15  # Minimum MMR change even after many games

        # ENHANCED: Extended placement period to 15 games
        PLACEMENT_GAMES = 15
        DECAY_RATE = 0.08  # Reduced decay rate for longer placement period

        # ENHANCED: 2x Streak multiplier settings
        MAX_STREAK_MULTIPLIER = 2.0  # Maximum multiplier for long streaks (100% bonus)
        STREAK_THRESHOLD = 2  # Reduced threshold - kicks in after 2 wins/losses
        STREAK_SCALING = 0.2  # Increased scaling (20% per win/loss after threshold)

        # NEW: Momentum system settings
        MOMENTUM_GAMES = 10  # Look at last 10 games for momentum
        MOMENTUM_THRESHOLD = 0.7  # 70% win rate for momentum bonus
        MOMENTUM_MULTIPLIER = 1.2  # 20% bonus for good momentum

        # NEW: Rank boundary protection settings
        RANK_BOUNDARIES = [1100, 1600]  # Rank B and Rank A thresholds
        PROMOTION_PROTECTION_GAMES = 3  # 3 games of protection after ranking up
        DEMOTION_PROTECTION_RANGE = 50  # 50 MMR buffer before demotion penalties kick in

        # Calculate the MMR difference between teams
        mmr_difference = opponent_avg_mmr - team_avg_mmr
        difference_factor = 1 + (mmr_difference / 400)  # More dramatic for underdog victories
        difference_factor = max(0.5, min(1.5, difference_factor))  # Wider range

        # ENHANCED: Extended placement period (first 15 games)
        if matches_played <= PLACEMENT_GAMES:
            # Linearly interpolate between first game value and regular base value
            progress = (matches_played - 1) / (PLACEMENT_GAMES - 1)  # 0 for first match, 1 for 15th match

            if is_win:
                base_value = FIRST_GAME_WIN * (1 - progress) + BASE_MMR_CHANGE * progress
            else:
                base_value = FIRST_GAME_LOSS * (1 - progress) + BASE_MMR_CHANGE * progress

            # Apply difference factor
            if is_win:
                base_change = base_value * difference_factor
            else:
                base_change = base_value * (2 - difference_factor)
        else:
            # After placement, use the regular base value with decay
            if is_win:
                base_change = BASE_MMR_CHANGE * difference_factor
            else:
                base_change = BASE_MMR_CHANGE * (2 - difference_factor)

        # Apply decay based on number of matches played after the initial placement games
        if matches_played <= PLACEMENT_GAMES:
            decay_multiplier = 1.0
        else:
            decay_multiplier = 1.0 * math.exp(-DECAY_RATE * (matches_played - PLACEMENT_GAMES))
            decay_multiplier = max(0.6, decay_multiplier)  # Don't decay below 60%

        # Calculate initial MMR change
        mmr_change = base_change * decay_multiplier

        # ENHANCED: 2x Streak multiplier system
        streak_abs = abs(streak)
        if streak_abs >= STREAK_THRESHOLD:
            streak_bonus = min(
                (streak_abs - STREAK_THRESHOLD + 1) * STREAK_SCALING,
                MAX_STREAK_MULTIPLIER - 1.0
            )
            streak_multiplier = 1.0 + streak_bonus

            # Apply streak multiplier for continuing streaks
            if (is_win and streak > 0) or (not is_win and streak < 0):
                mmr_change *= streak_multiplier
                print(f"Streak multiplier applied: {streak_multiplier:.2f}x (Streak: {streak})")

        # NEW: Momentum system bonus
        if player_data and matches_played > MOMENTUM_GAMES:
            momentum_bonus = self.calculate_momentum_bonus(player_data, is_win)
            if momentum_bonus > 1.0:
                mmr_change *= momentum_bonus
                print(f"Momentum bonus applied: {momentum_bonus:.2f}x")

        # NEW: Rank boundary protection
        if player_data:
            protection_modifier = self.calculate_rank_protection(
                player_data, player_mmr, is_win, matches_played
            )
            mmr_change *= protection_modifier
            if protection_modifier != 1.0:
                protection_type = "promotion protection" if protection_modifier < 1.0 else "demotion assistance"
                print(f"Rank boundary {protection_type}: {protection_modifier:.2f}x modifier")

        # Ensure the change is within bounds
        mmr_change = max(MIN_MMR_CHANGE, min(MAX_MMR_CHANGE, mmr_change))

        return round(mmr_change)

    def calculate_momentum_bonus(self, player_data, is_win):
        """
        Calculate momentum bonus based on recent performance
        """
        try:
            # Get recent matches for this player
            player_id = player_data.get('id')
            if not player_id:
                return 1.0

            # Look at last 10 completed matches
            recent_matches = list(self.matches.find(
                {"$or": [
                    {"team1.id": player_id},
                    {"team2.id": player_id}
                ], "status": "completed"}
            ).sort("completed_at", -1).limit(10))

            if len(recent_matches) < 5:  # Need at least 5 games for momentum
                return 1.0

            # Calculate win rate in recent matches
            wins = 0
            for match in recent_matches:
                player_won = self.did_player_win_match(match, player_id)
                if player_won:
                    wins += 1

            win_rate = wins / len(recent_matches)

            # Apply momentum bonus for good recent performance
            if win_rate >= 0.7 and is_win:  # 70%+ win rate and currently winning
                return 1.2  # 20% bonus
            elif win_rate <= 0.3 and not is_win:  # 30% or lower win rate and currently losing
                return 1.1  # 10% penalty reduction (mercy)

            return 1.0

        except Exception as e:
            print(f"Error calculating momentum bonus: {e}")
            return 1.0

    def calculate_rank_protection(self, player_data, current_mmr, is_win, matches_played):
        """
        Calculate rank boundary protection modifiers
        """
        try:
            RANK_BOUNDARIES = [1100, 1600]  # Rank B and Rank A thresholds
            PROMOTION_PROTECTION_GAMES = 3
            DEMOTION_PROTECTION_RANGE = 50

            # Check if player recently got promoted
            recent_promotion = self.check_recent_promotion(player_data)
            if recent_promotion and not is_win:
                games_since_promotion = recent_promotion.get('games_since', 0)
                if games_since_promotion < PROMOTION_PROTECTION_GAMES:
                    return 0.5  # 50% loss reduction for recently promoted players

            # Check for demotion protection (close to rank boundary)
            for boundary in RANK_BOUNDARIES:
                if current_mmr >= boundary:  # Player is above this boundary
                    distance_from_boundary = current_mmr - boundary
                    if distance_from_boundary <= DEMOTION_PROTECTION_RANGE and not is_win:
                        # Reduce loss when close to demotion
                        protection_factor = distance_from_boundary / DEMOTION_PROTECTION_RANGE
                        return 0.7 + (0.3 * protection_factor)  # 70-100% of normal loss

            # Check for promotion assistance (close to ranking up)
            for boundary in RANK_BOUNDARIES:
                if current_mmr < boundary:  # Player is below this boundary
                    distance_to_boundary = boundary - current_mmr
                    if distance_to_boundary <= DEMOTION_PROTECTION_RANGE and is_win:
                        # Boost gains when close to promotion
                        assistance_factor = (
                                                        DEMOTION_PROTECTION_RANGE - distance_to_boundary) / DEMOTION_PROTECTION_RANGE
                        return 1.0 + (0.2 * assistance_factor)  # 100-120% of normal gain

            return 1.0

        except Exception as e:
            print(f"Error calculating rank protection: {e}")
            return 1.0

    def check_recent_promotion(self, player_data):
        """
        Check if player was recently promoted and track games since promotion
        """
        try:
            # Check if player has promotion data
            promotion_data = player_data.get('last_promotion')
            if promotion_data:
                current_matches = player_data.get('matches', 0)
                matches_at_promotion = promotion_data.get('matches_at_promotion', 0)
                games_since = current_matches - matches_at_promotion

                if games_since <= 3:  # Within last 3 games
                    return {'games_since': games_since}

            return None

        except Exception as e:
            print(f"Error checking recent promotion: {e}")
            return None

    def did_player_win_match(self, match, player_id):
        """
        Helper function to determine if a player won a specific match
        """
        try:
            # Check which team the player was on
            player_in_team1 = any(p.get("id") == player_id for p in match.get("team1", []))
            winner = match.get("winner")

            if player_in_team1:
                return winner == 1
            else:
                return winner == 2

        except Exception as e:
            print(f"Error checking if player won match: {e}")
            return False

    def get_rank_tier_from_mmr(self, mmr):
        """
        Helper function to determine rank tier from MMR
        """
        if mmr >= 1600:
            return "Rank A"
        elif mmr >= 1100:
            return "Rank B"
        else:
            return "Rank C"

    def get_player_protection_status(self, player_data):
        """
        Get comprehensive protection status for a player
        Returns dict with protection info for display in /rank command
        """
        try:
            status = {
                "has_protection": False,
                "games_left": 0,
                "protection_type": None,
                "momentum_bonus": False,
                "streak_bonus": False,
                "close_to_promotion": False,
                "close_to_demotion": False
            }

            if not player_data:
                return status

            # Check for recent promotion protection
            promotion_data = player_data.get('last_promotion')
            if promotion_data:
                current_matches = player_data.get('matches', 0)
                matches_at_promotion = promotion_data.get('matches_at_promotion', 0)
                games_since = current_matches - matches_at_promotion

                if games_since < 3:
                    status["has_protection"] = True
                    status["games_left"] = 3 - games_since
                    status["protection_type"] = "promotion"

            # Check for momentum bonus eligibility
            if player_data.get('matches', 0) > 10:
                # This would need to query recent matches, simplified for now
                status["momentum_bonus"] = True  # Could be enhanced with actual calculation

            # Check for streak bonus
            current_streak = abs(player_data.get('current_streak', 0))
            if current_streak >= 2:
                status["streak_bonus"] = True

            # Check proximity to rank boundaries
            current_mmr = player_data.get('mmr', 600)

            # Close to promotion
            if 1050 <= current_mmr < 1100 or 1550 <= current_mmr < 1600:
                status["close_to_promotion"] = True

            # Close to demotion
            if 1100 <= current_mmr <= 1150 or 1600 <= current_mmr <= 1650:
                status["close_to_demotion"] = True

            return status

        except Exception as e:
            print(f"Error getting protection status: {e}")
            return status