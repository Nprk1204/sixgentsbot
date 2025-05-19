import discord
import datetime
import asyncio
import uuid


class QueueHandler:
    def __init__(self, db):
        self.db = db
        self.queue_collection = db.get_collection('queue')
        self.matches_collection = db.get_collection('active_matches')  # New collection for active matches
        self.vote_systems = {}  # Map of channel_id to VoteSystem
        self.captains_systems = {}  # Map of channel_id to CaptainsSystem
        self.bot = None

    def set_bot(self, bot):
        """Set the bot instance"""
        self.bot = bot
        # Start background task to remove inactive players
        if bot:
            bot.loop.create_task(self.remove_inactive_players())

    def set_vote_system(self, channel_id, vote_system):
        """Set the vote system reference for a specific channel"""
        self.vote_systems[channel_id] = vote_system

    def set_captains_system(self, channel_id, captains_system):
        """Set the captains system reference for a specific channel"""
        self.captains_systems[channel_id] = captains_system

    # In queue_handler.py, further modify the add_player method:

    def add_player(self, player, channel_id):
        """Add a player to a queue"""
        player_id = str(player.id)
        player_mention = player.mention
        player_name = player.display_name
        channel_id = str(channel_id)

        print(f"=== ADD PLAYER: {player_name} (ID: {player_id}) to channel {channel_id} ===")

        # STEP 1: First, check if player is in ACTIVE team selection in ANY channel
        active_selection = self.matches_collection.find_one({
            "players.id": player_id,
            "status": {"$in": ["voting", "selection"]}
        })

        if active_selection:
            selection_channel_id = active_selection.get("channel_id")

            # Get channel name for better messaging
            selection_channel = None
            if self.bot and selection_channel_id and selection_channel_id.isdigit():
                selection_channel = self.bot.get_channel(int(selection_channel_id))

            channel_identifier = f"<#{selection_channel_id}>" if selection_channel_id else "another channel"
            if selection_channel:
                channel_identifier = f"#{selection_channel.name}"

            return f"{player_mention} cannot join the queue while team selection is in progress in {channel_identifier}!"

        # STEP 2: Check if already in this channel's queue
        existing_queue = self.queue_collection.find_one({"id": player_id, "channel_id": channel_id})
        if existing_queue:
            return f"{player_mention} is already in this queue!"

        # STEP 3: Check if in another queue
        other_queue = self.queue_collection.find_one({"id": player_id, "channel_id": {"$ne": channel_id}})
        if other_queue:
            other_channel_id = other_queue.get("channel_id")
            channel_identifier = "another channel"

            if other_channel_id and other_channel_id.isdigit():
                channel_identifier = f"<#{other_channel_id}>"

            return f"{player_mention} is already in a queue in {channel_identifier}. Please leave that queue first."

        # STEP 4: Check for an active vote in this channel before adding player
        # This ensures we don't add a player if a vote is already in progress
        active_vote = self.matches_collection.find_one({
            "channel_id": channel_id,
            "status": {"$in": ["voting", "selection"]}
        })

        if active_vote:
            return f"{player_mention} cannot join the queue while team selection is in progress in this channel!"

        # STEP 5: Determine if this is a global queue
        channel = self.bot.get_channel(int(channel_id)) if self.bot else None
        is_global = channel and channel.name.lower() == "global"

        # STEP 6: Add player to queue
        self.queue_collection.insert_one({
            "id": player_id,
            "name": player_name,
            "mention": player_mention,
            "channel_id": channel_id,
            "is_global": is_global,
            "joined_at": datetime.datetime.utcnow()
        })

        # STEP 7: Count players in the queue
        queue_count = self.queue_collection.count_documents({"channel_id": channel_id})

        # STEP 8: If queue is full, check again if another process has started a vote
        if queue_count >= 6:
            # Double-check that no vote has started in this channel since we checked last
            active_vote = self.matches_collection.find_one({
                "channel_id": channel_id,
                "status": {"$in": ["voting", "selection"]}
            })

            if active_vote:
                # A vote has started since we checked last - remove this player
                self.queue_collection.delete_one({"id": player_id, "channel_id": channel_id})
                return f"{player_mention} cannot join because team selection has already started!"

            # No active vote - get 6 players and create a match
            players = list(self.queue_collection.find({"channel_id": channel_id}).limit(6))

            # Generate a unique match ID for the active match
            match_id = str(uuid.uuid4())[:8]

            # Create active match entry
            active_match = {
                "match_id": match_id,
                "channel_id": channel_id,
                "players": players,
                "created_at": datetime.datetime.utcnow(),
                "is_global": is_global,
                "status": "voting"  # Initial status is voting
            }

            # Remove these players from the queue before inserting the match
            for player in players:
                self.queue_collection.delete_one({"_id": player["_id"]})

            # Insert into active matches collection
            self.matches_collection.insert_one(active_match)

            return f"{player_mention} has joined the queue! Queue is now full!\n\nStarting team selection vote..."
        else:
            return f"{player_mention} has joined the queue! There are {queue_count}/6 players"

    def remove_player(self, player, channel_id):
        """Remove a player from a channel's queue or active match"""
        player_id = str(player.id)
        channel_id = str(channel_id)

        # Check if player is in this channel's active match
        active_match = self.matches_collection.find_one({
            "channel_id": channel_id,
            "players.id": player_id
        })

        if active_match:
            # If match is in voting or selection, don't allow leaving
            if active_match["status"] in ["voting", "selection"]:
                return f"{player.mention} cannot leave while team selection is in progress!"

            # If match is in another state, allow leaving and replace with dummy player
            player_index = None
            for i, p in enumerate(active_match["players"]):
                if p["id"] == player_id:
                    player_index = i
                    break

            if player_index is not None:
                # Generate dummy player
                dummy_id = f"dummy_{uuid.uuid4()}"[:10]
                dummy_player = {
                    "id": dummy_id,
                    "name": f"DummyPlayer_{player_index + 1}",
                    "mention": f"@DummyPlayer_{player_index + 1}"
                }

                # Update active match by replacing player with dummy
                self.matches_collection.update_one(
                    {"match_id": active_match["match_id"]},
                    {"$set": {f"players.{player_index}": dummy_player}}
                )

                return f"{player.mention} has left the active match and been replaced by a dummy player."

        # If not in active match, check if in queue
        result = self.queue_collection.delete_one({"id": player_id, "channel_id": channel_id})

        if result.deleted_count > 0:
            return f"{player.mention} has left the queue!"
        else:
            return f"{player.mention} was not in this queue or active match!"

    def get_queue_status(self, channel_id):
        """Get the status of a channel's queue and active match"""
        channel_id = str(channel_id)

        # Get active match info
        active_match = self.matches_collection.find_one({"channel_id": channel_id})

        # Get waiting queue info
        waiting_players = list(self.queue_collection.find({"channel_id": channel_id}))
        waiting_count = len(waiting_players)

        # Create embed
        embed = discord.Embed(
            title="Queue Status",
            color=0x3498db
        )

        # If no active match and no waiting players
        if not active_match and waiting_count == 0:
            embed.description = "Queue is empty! Use `/queue` to join the queue."
            return embed

        # Add active match info if exists
        if active_match:
            match_players = active_match["players"]
            match_status = active_match["status"].upper()

            player_mentions = [p["mention"] for p in match_players]

            embed.add_field(
                name=f"Active Match - {match_status}",
                value=", ".join(player_mentions),
                inline=False
            )

        # Add waiting queue info
        if waiting_count > 0:
            embed.description = f"**Waiting Queue: {waiting_count}/6 players**"

            waiting_mentions = [p["mention"] for p in waiting_players]
            embed.add_field(
                name="Players in Queue",
                value=", ".join(waiting_mentions),
                inline=False
            )

            # Add info about how many more players needed
            more_needed = 6 - waiting_count
            if more_needed > 0:
                embed.add_field(
                    name="Info",
                    value=f"{more_needed} more player(s) needed for a match.",
                    inline=False
                )
            else:
                embed.add_field(
                    name="Status",
                    value="**Queue is FULL!** Ready to start match.",
                    inline=False
                )
        else:
            embed.description = "No players waiting in queue."

        return embed

    def get_players_for_match(self, channel_id):
        """Get players for the active match in a channel"""
        channel_id = str(channel_id)

        # Check if channel has an active match
        active_match = self.matches_collection.find_one({"channel_id": channel_id})

        if active_match:
            # Return players from active match
            return active_match["players"]
        else:
            # No active match - get players from queue (first 6)
            return list(self.queue_collection.find({"channel_id": channel_id}).limit(6))

    def update_match_status(self, channel_id, new_status):
        """Update the status of an active match"""
        channel_id = str(channel_id)
        self.matches_collection.update_one(
            {"channel_id": channel_id},
            {"$set": {"status": new_status}}
        )

    def remove_active_match(self, channel_id):
        """Remove an active match when it's complete"""
        channel_id = str(channel_id)
        self.matches_collection.delete_one({"channel_id": channel_id})

    async def remove_inactive_players(self):
        """Check and remove players who have been in queue for too long (60 minutes)"""
        while True:
            try:
                # Sleep first to avoid immediate checks on startup
                await asyncio.sleep(300)  # Check every 5 minutes

                # Calculate cutoff time (60 minutes ago)
                cutoff_time = datetime.datetime.utcnow() - datetime.timedelta(minutes=60)

                # Find players to remove (only from queue, not active matches)
                expired_players = list(self.queue_collection.find({"joined_at": {"$lt": cutoff_time}}))

                # Remove them and send notifications
                for player in expired_players:
                    player_id = player.get("id")
                    player_mention = player.get("mention")
                    channel_id = player.get("channel_id")

                    # Remove from queue
                    self.queue_collection.delete_one({"id": player_id})

                    # Send notification if channel exists
                    if self.bot:
                        try:
                            channel = self.bot.get_channel(int(channel_id))
                            if channel:
                                await channel.send(
                                    f"{player_mention} has been removed from the queue due to inactivity (60+ minutes)."
                                )
                        except Exception as e:
                            print(f"Error sending queue timeout notification: {e}")

                # Log how many players were removed
                if expired_players:
                    print(f"Removed {len(expired_players)} players from queue due to inactivity")

            except Exception as e:
                print(f"Error in remove_inactive_players task: {e}")