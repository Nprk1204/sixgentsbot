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
    """

    def __init__(self, db, match_system=None):
        self.db = db
        self.match_system = match_system
        self.bot = None

        # Database collections
        self.queue_collection = db.get_collection('queue')
        self.active_matches_collection = db.get_collection('active_matches')

        # In-memory data for faster access
        self.channel_queues = {}  # channel_id -> list of players waiting
        self.active_matches = {}  # match_id -> match data
        self.player_matches = {}  # player_id -> match_id (tracking which match a player is in)

        # Systems for team selection
        self.vote_systems = {}  # channel_id -> VoteSystem
        self.captains_systems = {}  # channel_id -> CaptainsSystem

        # Background tasks
        self.tasks = []

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

                # Send notifications
                for player in expired_players:
                    player_id = player.get('id')
                    player_mention = player.get('mention')
                    channel_id = player.get('channel_id')

                    if self.bot and channel_id:
                        try:
                            channel = self.bot.get_channel(int(channel_id))
                            if channel:
                                await channel.send(
                                    f"{player_mention} has been removed from the queue due to inactivity (60+ minutes)."
                                )
                        except Exception as e:
                            print(f"Error sending queue timeout notification: {e}")

            except Exception as e:
                print(f"Error in remove_inactive_players task: {e}")

    async def add_player(self, player, channel):
        """Add a player to the queue for a specific channel"""
        channel_id = str(channel.id)
        player_id = str(player.id)
        player_mention = player.mention
        player_name = player.display_name

        # Check if player is already in a match
        if player_id in self.player_matches:
            match_id = self.player_matches[player_id]
            match = self.active_matches.get(match_id)

            if match:
                match_channel_id = match.get('channel_id')
                if match_channel_id == channel_id:
                    return f"{player_mention} is already in an active match in this channel!"
                else:
                    # If match is in another channel, mention that channel
                    try:
                        other_channel = self.bot.get_channel(int(match_channel_id))
                        other_channel_mention = f"<#{match_channel_id}>"
                        return f"{player_mention} is already in an active match in {other_channel_mention}."
                    except:
                        return f"{player_mention} is already in an active match in another channel."

        # Check if player is already in any queue
        queued_player = self.queue_collection.find_one({"id": player_id})
        if queued_player:
            queued_channel_id = queued_player.get('channel_id')

            if queued_channel_id == channel_id:
                return f"{player_mention} is already in this queue!"
            else:
                # If queued in another channel, mention that channel
                try:
                    other_channel = self.bot.get_channel(int(queued_channel_id))
                    other_channel_mention = f"<#{queued_channel_id}>"
                    return f"{player_mention} is already in a queue in {other_channel_mention}. Please leave that queue first."
                except:
                    return f"{player_mention} is already in a queue in another channel. Please leave that queue first."

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

        # Check if we have 6 players to start a match
        if queue_count >= 6:
            return await self.create_match(channel, player_mention)
        else:
            return f"{player_mention} has joined the queue! There are {queue_count}/6 players"

    async def remove_player(self, player, channel):
        """Remove a player from the queue"""
        channel_id = str(channel.id)
        player_id = str(player.id)
        player_mention = player.mention

        # Check if player is in an active match in this channel
        if player_id in self.player_matches:
            match_id = self.player_matches[player_id]
            match = self.active_matches.get(match_id)

            if match and match.get('channel_id') == channel_id:
                # Check match status - don't allow leaving during voting or captain selection
                status = match.get('status', '')
                if status in ['voting', 'selection']:
                    return f"{player_mention} cannot leave the queue while team selection is in progress!"

                # For matches in other states, we could handle substitution here if needed
                return f"{player_mention} is in an active match and cannot leave. If you need to leave mid-match, please contact an admin."

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
                            other_channel_mention = f"<#{other_channel_id}>"
                            return f"{player_mention} is not in this channel's queue. You are in {other_channel_mention}'s queue."
                        except:
                            return f"{player_mention} is in another channel's queue, not this one."

            return f"{player_mention} is not in any queue!"

        # Remove player from database
        result = self.queue_collection.delete_one({"id": player_id, "channel_id": channel_id})

        # Update in-memory state
        if channel_id in self.channel_queues:
            self.channel_queues[channel_id] = [p for p in self.channel_queues[channel_id] if p.get('id') != player_id]

        if result.deleted_count > 0:
            return f"{player_mention} has left the queue!"
        else:
            return f"Error removing {player_mention} from the queue. Please try again."

    async def create_match(self, channel, trigger_player_mention):
        """Create a match with the first 6 players in queue"""
        channel_id = str(channel.id)

        # Get the first 6 players from the queue
        players = []
        if channel_id in self.channel_queues:
            players = self.channel_queues[channel_id][:6]

        if len(players) < 6:
            return f"Not enough players to start match (need 6, have {len(players)})"

        # Generate a unique match ID
        match_id = str(uuid.uuid4())[:8]

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

        # Remove these players from the queue in database
        player_ids = [p.get('id') for p in players]
        self.queue_collection.delete_many({"id": {"$in": player_ids}, "channel_id": channel_id})

        # Update in-memory queue
        if channel_id in self.channel_queues:
            # Remove the first 6 players
            self.channel_queues[channel_id] = self.channel_queues[channel_id][6:]

        # Start the voting process
        vote_system = self.vote_systems.get(channel_id)
        if vote_system:
            self.bot.loop.create_task(vote_system.start_vote(channel))

        # Return a message about the match starting
        return f"{trigger_player_mention} has joined the queue! Queue is now full!\n\nStarting team selection vote..."

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
        """Update the status of a match"""
        # Update in database
        self.active_matches_collection.update_one(
            {"match_id": match_id},
            {"$set": {"status": new_status}}
        )

        # Update in memory
        if match_id in self.active_matches:
            self.active_matches[match_id]["status"] = new_status

    def remove_match(self, match_id):
        """Remove a match (typically when completed)"""
        match = self.active_matches.get(match_id)
        if not match:
            return False

        # Remove from database
        self.active_matches_collection.delete_one({"match_id": match_id})

        # Update in-memory state
        if match_id in self.active_matches:
            del self.active_matches[match_id]

        # Remove player-match associations
        for player_id, pid in list(self.player_matches.items()):
            if pid == match_id:
                del self.player_matches[player_id]

        return True

    def assign_teams_to_match(self, match_id, team1, team2):
        """Assign teams to a match after selection"""
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

            # Update player_matches mapping
            for player in team1 + team2:
                player_id = player.get('id')
                if player_id:
                    self.player_matches[player_id] = match_id