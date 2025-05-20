import discord
import datetime
import asyncio
import uuid


class QueueHandler:
    def __init__(self, db):
        self.db = db
        self.queue_collection = db.get_collection('queue')
        self.matches_collection = db.get_collection('active_matches')
        self.vote_systems = {}  # Map of channel_id to VoteSystem
        self.captains_systems = {}  # Map of channel_id to CaptainsSystem
        self.bot = None

        # Track multiple queues by channel
        self.active_queues = {}  # Map of channel_id to list of queues
        # Track players in active matches
        self.players_in_match = set()  # Set of player IDs currently in a match

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

    def add_player(self, player, channel_id):
        """Add a player to a queue"""
        player_id = str(player.id)
        player_mention = player.mention
        player_name = player.display_name
        channel_id = str(channel_id)

        # Check if player is in an active match (ongoing match that hasn't been reported)
        if player_id in self.players_in_match:
            return f"{player_mention} is already in an active match! Please report your match results before joining a new queue."

        # Determine which queue the player is trying to join
        requested_queue_num = len(self.active_queues.get(channel_id, [])) + 1

        # Check if player is already in any queue
        existing_queue = self.queue_collection.find_one({"id": player_id})
        if existing_queue:
            queue_channel_id = existing_queue.get("channel_id")
            existing_queue_num = existing_queue.get("queue_num", 1)

            if queue_channel_id == channel_id and existing_queue_num == requested_queue_num:
                return f"{player_mention} is already in this queue!"
            elif queue_channel_id == channel_id:
                # Allow joining a different queue number in the same channel
                # First remove from old queue
                self.queue_collection.delete_one({"id": player_id})
                # Continue with adding to new queue
            else:
                channel_mention = f"<#{queue_channel_id}>"
                return f"{player_mention} is already in a queue in {channel_mention}. Please leave that queue first."

        # Initialize the active_queues structure for this channel if it doesn't exist
        if channel_id not in self.active_queues:
            self.active_queues[channel_id] = []

        # Determine the queue number
        queue_num = requested_queue_num

        # Determine if this is a global queue
        channel = self.bot.get_channel(int(channel_id)) if self.bot else None
        is_global = channel and channel.name.lower() == "global"

        # Add player to queue
        self.queue_collection.insert_one({
            "id": player_id,
            "name": player_name,
            "mention": player_mention,
            "channel_id": channel_id,
            "is_global": is_global,
            "joined_at": datetime.datetime.utcnow(),
            "queue_num": queue_num  # Assign queue number
        })

        # Count players in the current queue
        queue_count = self.queue_collection.count_documents({
            "channel_id": channel_id,
            "queue_num": queue_num
        })

        # If queue reached 6 players, create active match
        if queue_count == 6:
            return self.create_active_match(channel_id, player_mention)
        else:
            return f"{player_mention} has joined queue #{queue_num}! There are {queue_count}/6 players"

    def create_active_match(self, channel_id, trigger_player_mention):
        """Create an active match from the first 6 players in queue"""
        channel_id = str(channel_id)

        # Determine current queue number
        current_queue_num = len(self.active_queues[channel_id]) + 1

        # Get the first 6 players from the current queue
        queue_players = list(self.queue_collection.find({
            "channel_id": channel_id,
            "queue_num": current_queue_num
        }).limit(6))

        if len(queue_players) < 6:
            return f"Not enough players to start match (need 6, have {len(queue_players)})"

        # Generate a unique match ID
        match_id = str(uuid.uuid4())[:8]

        # Determine if this is a global match
        is_global = False
        if queue_players and "is_global" in queue_players[0]:
            is_global = queue_players[0]["is_global"]

        # Create active match
        active_match = {
            "match_id": match_id,
            "channel_id": channel_id,
            "players": queue_players,
            "created_at": datetime.datetime.utcnow(),
            "is_global": is_global,
            "status": "voting",  # Initial status is voting
            "queue_num": current_queue_num  # Store the queue number
        }

        # Insert into active matches collection
        self.matches_collection.insert_one(active_match)

        # Add this queue to active_queues list
        self.active_queues[channel_id].append({
            "queue_num": current_queue_num,
            "match_id": match_id,
            "status": "active"
        })

        # Add players to the players_in_match set
        for player in queue_players:
            self.players_in_match.add(player["id"])

        # Format player mentions
        player_mentions = [p["mention"] for p in queue_players]

        # Return message
        return f"Queue #{current_queue_num} is now full with players: {', '.join(player_mentions)}\n\nStarting team selection vote... Other players can now join Queue #{current_queue_num + 1}!"

    def remove_player(self, player, channel_id):
        """Remove a player from a queue"""
        player_id = str(player.id)
        channel_id = str(channel_id)

        # Check if player is in an active match
        if player_id in self.players_in_match:
            # Find the match to see if voting/team selection is still happening
            active_match = self.matches_collection.find_one({
                "players.id": player_id,
                "status": {"$in": ["voting", "selection"]}
            })

            if active_match:
                return f"{player.mention} cannot leave while team selection is in progress!"

            # If match is already in progress, don't allow leaving
            return f"{player.mention} is in an active match and cannot leave. Please report match results first."

        # Find which queue the player is in
        player_queue = self.queue_collection.find_one({
            "id": player_id,
            "channel_id": channel_id
        })

        if not player_queue:
            return f"{player.mention} is not in any queue!"

        queue_num = player_queue.get("queue_num", 1)

        # Remove from queue
        result = self.queue_collection.delete_one({
            "id": player_id,
            "channel_id": channel_id,
            "queue_num": queue_num
        })

        if result.deleted_count > 0:
            return f"{player.mention} has left queue #{queue_num}!"
        else:
            return f"Error removing {player.mention} from the queue. Please try again."

    def get_queue_status(self, channel_id):
        """Get the status of all queues in a channel"""
        channel_id = str(channel_id)

        # Get all queues in this channel grouped by queue_num
        all_queues = {}

        queue_players = list(self.queue_collection.find({"channel_id": channel_id}))
        for player in queue_players:
            queue_num = player.get("queue_num", 1)
            if queue_num not in all_queues:
                all_queues[queue_num] = []
            all_queues[queue_num].append(player)

        # Get active matches in this channel
        active_matches = list(self.matches_collection.find({
            "channel_id": channel_id,
            "status": {"$ne": "completed"}
        }))

        # Create embed
        embed = discord.Embed(
            title="Queue Status",
            color=0x3498db
        )

        # No queues or matches
        if not all_queues and not active_matches:
            embed.description = "Queue is empty! Use `/queue` to join the queue."
            return embed

        # Add active matches first
        if active_matches:
            for match in active_matches:
                match_players = match["players"]
                match_status = match["status"].upper()
                queue_num = match.get("queue_num", 1)

                player_mentions = [p["mention"] for p in match_players]

                embed.add_field(
                    name=f"Queue #{queue_num} - ACTIVE MATCH ({match_status})",
                    value=", ".join(player_mentions),
                    inline=False
                )

        # Add waiting queues
        for queue_num, players in sorted(all_queues.items()):
            waiting_count = len(players)
            if waiting_count > 0:
                waiting_mentions = [p["mention"] for p in players]

                # Add info about how many more players needed
                more_needed = 6 - waiting_count
                if more_needed > 0:
                    status_text = f"Waiting Queue: {waiting_count}/6 players\n{more_needed} more player(s) needed for a match."
                else:
                    status_text = "**Queue is FULL!** Ready to start match."

                embed.add_field(
                    name=f"Queue #{queue_num}",
                    value=f"{status_text}\nPlayers: {', '.join(waiting_mentions)}",
                    inline=False
                )

        return embed

    def get_players_for_match(self, channel_id):
        """Get players for the active match in a channel"""
        channel_id = str(channel_id)

        # Check if channel has an active match
        active_match = self.matches_collection.find_one({
            "channel_id": channel_id,
            "status": {"$ne": "completed"}
        })

        if active_match:
            # Return players from active match
            return active_match["players"]
        else:
            # Find the highest queue number
            latest_queue = self.queue_collection.find({"channel_id": channel_id}).sort("queue_num", -1).limit(1)
            latest_queue = list(latest_queue)

            if not latest_queue:
                return []

            queue_num = latest_queue[0].get("queue_num", 1)

            # Get players from latest queue (first 6)
            return list(self.queue_collection.find({
                "channel_id": channel_id,
                "queue_num": queue_num
            }).limit(6))

    def update_match_status(self, channel_id, new_status):
        """Update the status of an active match"""
        channel_id = str(channel_id)
        self.matches_collection.update_one(
            {"channel_id": channel_id, "status": {"$ne": "completed"}},
            {"$set": {"status": new_status}}
        )

    def mark_match_completed(self, match_id):
        """Mark a match as completed and release players"""
        match = self.matches_collection.find_one({"match_id": match_id})

        if not match:
            return False

        # Get player IDs from the match
        player_ids = [p["id"] for p in match["players"]]

        # Remove players from players_in_match set
        for player_id in player_ids:
            if player_id in self.players_in_match:
                self.players_in_match.remove(player_id)

        # Update match status
        self.matches_collection.update_one(
            {"match_id": match_id},
            {"$set": {"status": "completed"}}
        )

        # Update the queue in active_queues
        channel_id = match.get("channel_id")
        queue_num = match.get("queue_num", 1)

        if channel_id in self.active_queues:
            for i, queue in enumerate(self.active_queues[channel_id]):
                if queue.get("queue_num") == queue_num:
                    self.active_queues[channel_id][i]["status"] = "completed"
                    break

        return True

    def remove_active_match(self, channel_id):
        """Remove an active match when it's complete"""
        channel_id = str(channel_id)
        match = self.matches_collection.find_one({"channel_id": channel_id, "status": {"$ne": "completed"}})

        if match:
            self.mark_match_completed(match["match_id"])

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
                    queue_num = player.get("queue_num", 1)

                    # Remove from queue
                    self.queue_collection.delete_one({"id": player_id})

                    # Send notification if channel exists
                    if self.bot:
                        try:
                            channel = self.bot.get_channel(int(channel_id))
                            if channel:
                                await channel.send(
                                    f"{player_mention} has been removed from queue #{queue_num} due to inactivity (60+ minutes)."
                                )
                        except Exception as e:
                            print(f"Error sending queue timeout notification: {e}")

                # Log how many players were removed
                if expired_players:
                    print(f"Removed {len(expired_players)} players from queue due to inactivity")

            except Exception as e:
                print(f"Error in remove_inactive_players task: {e}")

    def remove_players_from_queue(self, players, channel_id=None):
        """Remove a list of players from the queue when they enter a match"""
        # Add players to players_in_match set
        for player in players:
            player_id = player["id"]
            self.players_in_match.add(player_id)

        for player in players:
            player_id = player["id"]

            # If channel_id is provided, only remove from that channel
            if channel_id:
                self.queue_collection.delete_one({"id": player_id, "channel_id": str(channel_id)})
            else:
                # Otherwise remove from any queue
                self.queue_collection.delete_one({"id": player_id})

        print(f"Removed {len(players)} players from queue and marked them as in-match")