import discord
import asyncio
import datetime
import uuid
from typing import Dict, List, Set, Optional, Tuple, Any


class QueueManager:
    """
    Redesigned queue management system that supports:
    - Multiple concurrent active matches in the same channel
    - Global and ranked MMR tracking
    - Separate queues for each channel (rank a, b, c, global)
    - Real-time WebSocket updates for queue and match status
    """

    def __init__(self, db, match_system=None):
        self.db = db
        self.match_system = match_system
        self.bot = None
        self.socketio = None  # Will be set by the main app

        # Database collections
        self.queue_collection = db.get_collection('queue')
        self.active_matches_collection = db.get_collection('active_matches')

        # In-memory data for faster access
        self.channel_queues = {}  # channel_id -> list of players waiting
        self.active_matches = {}  # match_id -> match data
        self.player_matches = {}  # player_id -> match_id (tracking which match a player is in)

        # Systems for team selection
        self.vote_systems = {}  # channel_name -> VoteSystem
        self.captains_systems = {}  # channel_id -> CaptainsSystem

        # Background tasks
        self.tasks = []

    def set_socketio(self, socketio):
        """Set the SocketIO instance for real-time updates"""
        self.socketio = socketio

    def _broadcast_queue_update(self, channel_id):
        """Broadcast queue updates via WebSocket"""
        if self.socketio:
            try:
                from leaderboard_app import broadcast_queue_update
                broadcast_queue_update(channel_id)
            except Exception as e:
                print(f"Error broadcasting queue update: {e}")

    def _broadcast_player_update(self, player_id):
        """Broadcast player status update via WebSocket"""
        if self.socketio:
            try:
                from leaderboard_app import broadcast_player_status_update
                broadcast_player_status_update(player_id)
            except Exception as e:
                print(f"Error broadcasting player update: {e}")

    def _broadcast_match_update(self, match_id):
        """Broadcast match status update via WebSocket"""
        if self.socketio:
            try:
                from leaderboard_app import broadcast_match_update
                broadcast_match_update(match_id)
            except Exception as e:
                print(f"Error broadcasting match update: {e}")

    def set_bot(self, bot):
        """Set the bot instance and start background tasks"""
        self.bot = bot

        # Cancel any existing tasks
        for task in self.tasks:
            task.cancel()

        # Start new background tasks
        self.tasks = [
            self.bot.loop.create_task(self.remove_inactive_players()),
            self.bot.loop.create_task(self.sync_db_to_memory())
        ]

    def set_match_system(self, match_system):
        """Set the match system reference"""
        self.match_system = match_system

    def set_vote_system(self, channel_id, vote_system):
        """Set the vote system for a specific channel"""
        self.vote_systems[str(channel_id)] = vote_system

    def set_captains_system(self, channel_id, captains_system):
        """Set the captains system for a specific channel"""
        self.captains_systems[str(channel_id)] = captains_system

    async def sync_db_to_memory(self):
        """Background task to sync database with in-memory state"""
        while True:
            try:
                # Load all queued players into memory
                all_queued = list(self.queue_collection.find())

                # Reset in-memory queues
                new_channel_queues = {}

                # Group players by channel_id
                for player in all_queued:
                    channel_id = str(player.get('channel_id', ''))
                    if channel_id not in new_channel_queues:
                        new_channel_queues[channel_id] = []
                    new_channel_queues[channel_id].append(player)

                # Update channel_queues atomically
                self.channel_queues = new_channel_queues

                # Load all active matches into memory
                all_matches = list(self.active_matches_collection.find())

                # Reset in-memory matches and player_matches
                new_active_matches = {}
                new_player_matches = {}

                # Process each match
                for match in all_matches:
                    match_id = match.get('match_id')
                    if not match_id:
                        continue

                    new_active_matches[match_id] = match

                    # Track which players are in which match
                    for team_key in ['team1', 'team2']:
                        if team_key in match:
                            for player in match.get(team_key, []):
                                player_id = player.get('id')
                                if player_id:
                                    new_player_matches[player_id] = match_id

                # Update active_matches and player_matches atomically
                self.active_matches = new_active_matches
                self.player_matches = new_player_matches

            except Exception as e:
                print(f"Error syncing DB to memory: {e}")

            # Run every 30 seconds
            await asyncio.sleep(30)

    async def remove_inactive_players(self):
        """Background task to remove players who have been in queue too long"""
        while True:
            try:
                # Sleep first to avoid immediate execution
                await asyncio.sleep(300)  # Check every 5 minutes

                # Calculate cutoff time (60 minutes ago)
                cutoff_time = datetime.datetime.utcnow() - datetime.timedelta(minutes=60)

                # Find players to remove
                expired_query = {"joined_at": {"$lt": cutoff_time}}
                expired_players = list(self.queue_collection.find(expired_query))

                # Skip if no expired players
                if not expired_players:
                    continue

                # Remove them from database
                result = self.queue_collection.delete_many(expired_query)

                print(f"Removed {result.deleted_count} inactive players from queue")

                # Send notifications with enhanced embeds
                for player in expired_players:
                    player_id = player.get('id')
                    player_mention = player.get('mention')
                    player_name = player.get('name', 'Unknown Player')
                    channel_id = player.get('channel_id')
                    joined_at = player.get('joined_at')
                    is_global = player.get('is_global', False)

                    # Broadcast player update
                    self._broadcast_player_update(str(player_id))
                    self._broadcast_queue_update(str(channel_id))

                    if self.bot and channel_id:
                        try:
                            channel = self.bot.get_channel(int(channel_id))
                            if channel:
                                # Calculate how long they were in queue
                                time_in_queue = "60+ minutes"
                                if joined_at:
                                    duration = datetime.datetime.utcnow() - joined_at
                                    hours = int(duration.total_seconds() // 3600)
                                    minutes = int((duration.total_seconds() % 3600) // 60)

                                    if hours > 0:
                                        time_in_queue = f"{hours}h {minutes}m"
                                    else:
                                        time_in_queue = f"{minutes}m"

                                # Create enhanced timeout embed
                                embed = discord.Embed(
                                    title="⏰ Queue Timeout",
                                    description=f"{player_mention} has been automatically removed from the queue due to inactivity.",
                                    color=0xff9900
                                )

                                embed.add_field(
                                    name="⏱️ Time in Queue",
                                    value=time_in_queue,
                                    inline=True
                                )

                                embed.add_field(
                                    name="🎮 Queue Type",
                                    value="Global Queue" if is_global else f"#{channel.name.title()} Queue",
                                    inline=True
                                )

                                embed.add_field(
                                    name="📢 Reason",
                                    value="60+ minutes of inactivity",
                                    inline=True
                                )

                                embed.add_field(
                                    name="🔄 To Rejoin",
                                    value="Simply use `/queue` again when you're ready to play!",
                                    inline=False
                                )

                                embed.add_field(
                                    name="💡 Tip",
                                    value="Stay active in Discord to avoid timeouts, or leave and rejoin the queue when ready.",
                                    inline=False
                                )

                                embed.set_footer(text="Queue management system • Stay active to avoid timeouts")
                                embed.timestamp = datetime.datetime.utcnow()

                                # Add player avatar if possible
                                try:
                                    if player_id and player_id.isdigit():
                                        member = await channel.guild.fetch_member(int(player_id))
                                        if member and member.avatar:
                                            embed.set_thumbnail(url=member.avatar.url)
                                except:
                                    pass  # Avatar is optional

                                await channel.send(embed=embed)
                                print(f"✅ Sent timeout notification for {player_name} in #{channel.name}")

                        except Exception as e:
                            print(f"❌ Error sending queue timeout notification: {e}")

            except Exception as e:
                print(f"❌ Error in remove_inactive_players task: {e}")

    async def add_player(self, player, channel):
        """Add a player to the queue for a specific channel with WebSocket updates"""
        channel_id = str(channel.id)
        player_id = str(player.id)
        player_mention = player.mention
        player_name = player.display_name

        # Check if player is already in a match (including ALL phases)
        if player_id in self.player_matches:
            match_id = self.player_matches[player_id]
            match = self.active_matches.get(match_id)

            if match:
                match_channel_id = match.get('channel_id')
                match_status = match.get('status', '')

                # Return formatted error message with match ID
                return f"QUEUE_ERROR: You're already in an active match! Match ID: `{match_id}`"

        # Additional check: Look for matches in database where player might be stuck
        try:
            db_match = self.active_matches_collection.find_one({
                "$or": [
                    {"team1.id": player_id},
                    {"team2.id": player_id},
                    {"players.id": player_id}
                ],
                "status": {"$in": ["voting", "selection", "in_progress", "completed"]}
            })

            if db_match:
                db_match_id = db_match.get("match_id")
                db_status = db_match.get("status")

                # Fix tracking if it's missing
                if player_id not in self.player_matches:
                    self.player_matches[player_id] = db_match_id
                    print(f"Fixed missing player tracking: {player_name} -> {db_match_id}")

                # Return formatted error message with match ID
                return f"QUEUE_ERROR: You're already in an active match! Match ID: `{db_match_id}`"

        except Exception as e:
            print(f"Error checking database for player matches: {e}")

        # Check if player is already in any queue
        queued_player = self.queue_collection.find_one({"id": player_id})
        if queued_player:
            queued_channel_id = queued_player.get('channel_id')

            if queued_channel_id == channel_id:
                return "QUEUE_ERROR: You're already in this queue!"
            else:
                # Get the actual channel and show the channel name properly
                try:
                    other_channel = self.bot.get_channel(int(queued_channel_id))
                    if other_channel:
                        return f"QUEUE_ERROR: {player_mention}, you're already in the queue for #{other_channel.name}!"
                    else:
                        return f"QUEUE_ERROR: {player_mention}, you're already in a queue in another channel!"
                except:
                    return f"QUEUE_ERROR: {player_mention}, you're already in a queue in another channel!"

        # Determine if this is a global queue
        is_global = channel.name.lower() == "global"

        # Add player to queue
        player_data = {
            "id": player_id,
            "name": player_name,
            "mention": player_mention,
            "channel_id": channel_id,
            "is_global": is_global,
            "joined_at": datetime.datetime.utcnow()
        }

        # Insert to database
        self.queue_collection.insert_one(player_data)

        # Update in-memory state
        if channel_id not in self.channel_queues:
            self.channel_queues[channel_id] = []
        self.channel_queues[channel_id].append(player_data)

        # Get queue count for this channel
        queue_count = len(self.channel_queues.get(channel_id, []))

        # Broadcast updates
        self._broadcast_player_update(str(player_id))
        self._broadcast_queue_update(str(channel_id))

        # Check if we have 6 players to start a match
        if queue_count >= 6:
            match_id = await self.create_match(channel, player_mention)
            return match_id
        else:
            return f"SUCCESS: {player_mention} has joined the queue! There are {queue_count}/6 players"

    async def remove_player(self, player, channel):
        """Remove a player from the queue with WebSocket updates"""
        channel_id = str(channel.id)
        player_id = str(player.id)
        player_mention = player.mention

        # Check if player is in any active match (including voting/selection) FIRST
        if player_id in self.player_matches:
            match_id = self.player_matches[player_id]
            match = self.active_matches.get(match_id)

            if match:
                match_status = match.get('status', '')
                print(f"Player {player.display_name} is in match {match_id} with status: {match_status}")

                # Provide specific messages with match ID for all match phases
                if match_status == "voting":
                    return f"MATCH_ERROR: {player_mention}, you cannot leave during team selection voting. Match ID: `{match_id}`"
                elif match_status == "selection":
                    return f"MATCH_ERROR: {player_mention}, you cannot leave during captain selection. Match ID: `{match_id}`"
                elif match_status == "in_progress":
                    return f"MATCH_ERROR: {player_mention}, you cannot leave while in an active match. Complete your match first using `/report {match_id} win` or `/report {match_id} loss`"
                elif match_status == "completed":
                    return f"MATCH_ERROR: {player_mention}, you cannot leave until the completed match is reported. Use `/report {match_id} win` or `/report {match_id} loss`"
                else:
                    # Generic message for unknown status
                    return f"MATCH_ERROR: {player_mention}, you cannot leave while in an active match. Match ID: `{match_id}`"
            else:
                # If match isn't in active_matches but player is tracked, check database
                print(f"Player {player.display_name} is tracked in match {match_id} but match not in active_matches")

                # Check database for the match
                db_match = self.active_matches_collection.find_one({"match_id": match_id})
                if db_match:
                    match_status = db_match.get('status', 'unknown')
                    print(f"Found match {match_id} in database with status: {match_status}")

                    if match_status == "voting":
                        return f"MATCH_ERROR: {player_mention}, you cannot leave during team selection voting. Match ID: `{match_id}`"
                    elif match_status == "selection":
                        return f"MATCH_ERROR: {player_mention}, you cannot leave during captain selection. Match ID: `{match_id}`"
                    elif match_status == "in_progress":
                        return f"MATCH_ERROR: {player_mention}, you cannot leave while in an active match. Complete your match first using `/report {match_id} win` or `/report {match_id} loss`"
                    else:
                        return f"MATCH_ERROR: {player_mention}, you cannot leave while in an active match. Match ID: `{match_id}`"
                else:
                    # Match not found anywhere, clean up the tracking
                    print(f"Cleaning up orphaned player tracking for {player.display_name}")
                    del self.player_matches[player_id]

        # Additional check - look for the player in any active match directly
        for match_id, match in self.active_matches.items():
            # Check if player is in this match's players list
            players = match.get('players', [])
            player_in_match = any(p.get('id') == player_id for p in players)

            if player_in_match:
                match_status = match.get('status', '')
                print(
                    f"Found player {player.display_name} in match {match_id} (status: {match_status}) via direct search")

                # Update tracking if missing
                if player_id not in self.player_matches:
                    self.player_matches[player_id] = match_id
                    print(f"Fixed missing player tracking for {player.display_name}")

                # Return appropriate error message
                if match_status == "voting":
                    return f"MATCH_ERROR: {player_mention}, you cannot leave during team selection voting. Match ID: `{match_id}`"
                elif match_status == "selection":
                    return f"MATCH_ERROR: {player_mention}, you cannot leave during captain selection. Match ID: `{match_id}`"
                elif match_status == "in_progress":
                    return f"MATCH_ERROR: {player_mention}, you cannot leave while in an active match. Complete your match first using `/report {match_id} win` or `/report {match_id} loss`"
                else:
                    return f"MATCH_ERROR: {player_mention}, you cannot leave while in an active match. Match ID: `{match_id}`"

        # Check if player is in this channel's queue
        player_in_queue = False
        if channel_id in self.channel_queues:
            # Check if player is in this channel's queue
            for i, p in enumerate(self.channel_queues[channel_id]):
                if p.get('id') == player_id:
                    player_in_queue = True
                    break

        if not player_in_queue:
            # Check if they're in any queue at all
            for other_channel_id, players in self.channel_queues.items():
                for p in players:
                    if p.get('id') == player_id:
                        try:
                            other_channel = self.bot.get_channel(int(other_channel_id))
                            if other_channel:
                                return f"QUEUE_ERROR: {player_mention}, you are not in this channel's queue. You are in #{other_channel.name}'s queue."
                            else:
                                return f"QUEUE_ERROR: {player_mention}, you are in another channel's queue, not this one."
                        except:
                            return f"QUEUE_ERROR: {player_mention}, you are in another channel's queue, not this one."

            return f"QUEUE_ERROR: {player_mention}, you are not in any queue!"

        # Remove player from database
        result = self.queue_collection.delete_one({"id": player_id, "channel_id": channel_id})

        # Update in-memory state
        if channel_id in self.channel_queues:
            self.channel_queues[channel_id] = [p for p in self.channel_queues[channel_id] if p.get('id') != player_id]

        if result.deleted_count > 0:
            # Broadcast updates after successful removal
            self._broadcast_player_update(str(player_id))
            self._broadcast_queue_update(str(channel_id))

            return f"SUCCESS: {player_mention} has left the queue!"
        else:
            return f"ERROR: Error removing {player_mention} from the queue. Please try again."

    async def create_match(self, channel, trigger_player_mention):
        """Create a match with the first 6 players in queue with WebSocket updates"""
        channel_id = str(channel.id)

        # Get the first 6 players from the queue
        players = []
        if channel_id in self.channel_queues:
            players = self.channel_queues[channel_id][:6]

        if len(players) < 6:
            return f"Not enough players to start match (need 6, have {len(players)})"

        # Generate a unique match ID - make it shorter and more readable
        match_id = str(uuid.uuid4().hex)[:6]

        # Determine if this is a global match
        is_global = channel.name.lower() == "global"

        # Create an active match
        match_data = {
            "match_id": match_id,
            "channel_id": channel_id,
            "players": players,
            "created_at": datetime.datetime.utcnow(),
            "is_global": is_global,
            "status": "voting"  # Initial status is voting
        }

        # Insert into database
        self.active_matches_collection.insert_one(match_data)

        # Update in-memory state
        self.active_matches[match_id] = match_data

        # Track all players in this match immediately - even during voting phase
        for player in players:
            player_id = str(player.get('id', ''))
            if player_id:
                self.player_matches[player_id] = match_id
                print(
                    f"Tracking player {player.get('name', 'Unknown')} (ID: {player_id}) in match {match_id} from start")

        # Remove these players from the queue in database
        player_ids = [p.get('id') for p in players]
        self.queue_collection.delete_many({"id": {"$in": player_ids}, "channel_id": channel_id})

        # Update in-memory queue
        if channel_id in self.channel_queues:
            # Remove the first 6 players
            self.channel_queues[channel_id] = self.channel_queues[channel_id][6:]

        # Broadcast match creation to all players
        if match_id:
            self._broadcast_match_update(match_id)
            self._broadcast_queue_update(str(channel_id))

            # Update status for all players in the match
            for player in players:
                player_id = player.get('id', '')
                if player_id:
                    self._broadcast_player_update(str(player_id))

        # Return the match ID
        return match_id

    def get_queue_status(self, channel):
        """Get the status of a channel's queue and active matches"""
        channel_id = str(channel.id)

        # Get queue information
        queue_players = self.channel_queues.get(channel_id, [])
        queue_count = len(queue_players)

        # Get active matches in this channel
        channel_matches = []
        for match_id, match in self.active_matches.items():
            if match.get('channel_id') == channel_id:
                channel_matches.append(match)

        # Convert to a format usable for display
        status_data = {
            "queue_players": queue_players,
            "queue_count": queue_count,
            "active_matches": channel_matches
        }

        return status_data

    def get_player_match(self, player_id):
        """Get the active match that a player is in"""
        player_id = str(player_id)

        # Check if player is in a match
        match_id = self.player_matches.get(player_id)
        if not match_id:
            return None

        # Return the match
        return self.active_matches.get(match_id)

    def get_match_by_id(self, match_id):
        """Get a match by its ID"""
        return self.active_matches.get(match_id)

    def get_match_by_channel(self, channel_id, status=None):
        """
        Get an active match in a specific channel with optional status filter
        Returns the first match that matches criteria or None if no match found
        """
        channel_id = str(channel_id)

        for match_id, match in self.active_matches.items():
            if match.get('channel_id') == channel_id:
                # If status filter is provided, check it
                if status is not None and match.get('status') != status:
                    continue
                return match

        return None

    def get_players_for_match(self, match_id):
        """Get players for a specific match by ID"""
        match = self.active_matches.get(match_id)
        if not match:
            return []

        return match.get('players', [])

    def get_players_in_queue(self, channel_id):
        """Get players waiting in a channel's queue"""
        channel_id = str(channel_id)
        return self.channel_queues.get(channel_id, [])

    def update_match_status(self, match_id, new_status):
        """Update the status of a match with WebSocket broadcast"""
        # Update in database
        self.active_matches_collection.update_one(
            {"match_id": match_id},
            {"$set": {"status": new_status}}
        )

        # Update in memory
        if match_id in self.active_matches:
            self.active_matches[match_id]["status"] = new_status

        # Broadcast the update
        self._broadcast_match_update(match_id)

    def assign_teams_to_match(self, match_id, team1, team2):
        """Assign teams to a match after selection with WebSocket updates"""
        # Normalize match ID
        match_id = str(match_id).strip()
        if len(match_id) > 8:
            match_id = match_id[:6]

        print(f"Assigning teams to match {match_id}")
        print(f"Team 1: {[p.get('name', 'Unknown') + ' (ID: ' + str(p.get('id', 'None')) + ')' for p in team1]}")
        print(f"Team 2: {[p.get('name', 'Unknown') + ' (ID: ' + str(p.get('id', 'None')) + ')' for p in team2]}")

        # Update in database
        self.active_matches_collection.update_one(
            {"match_id": match_id},
            {"$set": {
                "team1": team1,
                "team2": team2,
                "status": "in_progress"
            }}
        )

        # Update in memory
        if match_id in self.active_matches:
            self.active_matches[match_id]["team1"] = team1
            self.active_matches[match_id]["team2"] = team2
            self.active_matches[match_id]["status"] = "in_progress"

            # Update player_matches mapping - convert all IDs to strings for consistency
            for player in team1 + team2:
                player_id = str(player.get('id', ''))
                if player_id:
                    self.player_matches[player_id] = match_id
                    print(f"Added player {player.get('name', 'Unknown')} (ID: {player_id}) to match {match_id}")
        else:
            print(f"Warning: Match {match_id} not found in active_matches during team assignment")

        # Broadcast team assignment
        self._broadcast_match_update(match_id)

    def remove_match(self, match_id):
        """Remove a match (typically when completed) with WebSocket updates"""
        match = self.active_matches.get(match_id)
        player_ids = []

        if match:
            # Collect all player IDs from the match
            for team_key in ['team1', 'team2']:
                if team_key in match:
                    for player in match.get(team_key, []):
                        player_id = player.get('id')
                        if player_id:
                            player_ids.append(str(player_id))

            # Also check players list
            for player in match.get('players', []):
                player_id = player.get('id')
                if player_id:
                    player_ids.append(str(player_id))

        # Remove from database
        self.active_matches_collection.delete_one({"match_id": match_id})

        # Update in-memory state
        if match_id in self.active_matches:
            del self.active_matches[match_id]
            print(f"Removed match {match_id} from active_matches")

        # Remove player-match associations
        for player_id, pid in list(self.player_matches.items()):
            if pid == match_id:
                del self.player_matches[player_id]

        # Broadcast updates to affected players
        for player_id in set(player_ids):  # Remove duplicates
            self._broadcast_player_update(player_id)

        return True

    def assign_teams_to_match(self, match_id, team1, team2):
        """Assign teams to a match after selection"""
        # Normalize match ID
        match_id = str(match_id).strip()
        if len(match_id) > 8:
            match_id = match_id[:6]

        print(f"Assigning teams to match {match_id}")
        print(f"Team 1: {[p.get('name', 'Unknown') + ' (ID: ' + str(p.get('id', 'None')) + ')' for p in team1]}")
        print(f"Team 2: {[p.get('name', 'Unknown') + ' (ID: ' + str(p.get('id', 'None')) + ')' for p in team2]}")

        # Update in database
        self.active_matches_collection.update_one(
            {"match_id": match_id},
            {"$set": {
                "team1": team1,
                "team2": team2,
                "status": "in_progress"
            }}
        )

        # Update in memory
        if match_id in self.active_matches:
            self.active_matches[match_id]["team1"] = team1
            self.active_matches[match_id]["team2"] = team2
            self.active_matches[match_id]["status"] = "in_progress"

            # Update player_matches mapping - convert all IDs to strings for consistency
            for player in team1 + team2:
                player_id = str(player.get('id', ''))
                if player_id:
                    self.player_matches[player_id] = match_id
                    print(f"Added player {player.get('name', 'Unknown')} (ID: {player_id}) to match {match_id}")
        else:
            print(f"Warning: Match {match_id} not found in active_matches during team assignment")