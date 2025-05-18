import discord
import datetime

import asyncio


class QueueHandler:
    def __init__(self, db):
        self.db = db
        self.queue_collection = db.get_collection('queue')
        self.vote_systems = {}  # Map of channel_id to VoteSystem
        self.captains_systems = {}  # Map of channel_id to CaptainsSystem
        self.bot = None
        self.active_selection_queues = {}  # Track channels with active selections

    def set_bot(self, bot):
        """Set the bot instance"""
        self.bot = bot
        # Start background task to remove inactive players
        if bot:
            bot.loop.create_task(self.remove_inactive_players())

    # ... other existing methods ...

    def add_player(self, player, channel_id):
        player_id = str(player.id)
        player_mention = player.mention
        player_name = player.display_name
        channel_id = str(channel_id)  # Ensure channel_id is a string

        # Determine if this is a global queue
        channel = self.bot.get_channel(int(channel_id)) if self.bot else None
        is_global = channel and channel.name.lower() == "global"

        # Debug output
        print(f"Adding player {player_name} (ID: {player_id}) to channel {channel_id}, is_global: {is_global}")

        # Check if player is already in this channel's queue
        if self.queue_collection.find_one({"id": player_id, "channel_id": channel_id}):
            return f"{player_mention} is already in this queue!"

        # Check if player is in any other queue
        other_queue = self.queue_collection.find_one({"id": player_id})

        if other_queue:
            # Get the channel ID from the other queue
            other_channel_id = other_queue.get("channel_id")
            print(f"Player is in another queue. Channel ID: {other_channel_id}")

            # Fix for missing or None channel ID
            if not other_channel_id:
                # Delete the broken entry and create a new one
                self.queue_collection.delete_one({"id": player_id})
                print(f"Deleted broken entry for {player_name} with missing channel ID")

                # Now proceed to add the player to the current channel
                self.queue_collection.insert_one({
                    "id": player_id,
                    "name": player_name,
                    "mention": player_mention,
                    "channel_id": channel_id,
                    "is_global": is_global,
                    "joined_at": datetime.datetime.utcnow(),
                    "active_selection": False  # Flag to track if in active selection
                })

                count = self.queue_collection.count_documents({"channel_id": channel_id, "active_selection": False})
                return f"{player_mention} has joined the queue! There are {count}/6 players"

            # Only format as mention if we have a valid channel ID
            if other_channel_id and other_channel_id.isdigit():
                channel_mention = f"<#{other_channel_id}>"
                return f"{player_mention} is already in a queue in {channel_mention}. Please leave that queue first."
            else:
                # Fallback if channel ID is invalid but not None
                return f"{player_mention} is already in another queue. Please leave that queue first."

        # Check if team selection is active in this channel
        selection_active = False

        if channel_id in self.vote_systems and self.vote_systems[channel_id].is_voting_active(channel_id):
            selection_active = True

        if channel_id in self.captains_systems and self.captains_systems[channel_id].is_selection_active(channel_id):
            selection_active = True

        # Store player in queue with channel ID, timestamp, and active_selection flag
        self.queue_collection.insert_one({
            "id": player_id,
            "name": player_name,
            "mention": player_mention,
            "channel_id": channel_id,
            "is_global": is_global,
            "joined_at": datetime.datetime.utcnow(),
            "active_selection": False  # New players always join the next queue
        })

        # Update the active_selection_queues tracking
        if selection_active and channel_id not in self.active_selection_queues:
            # Mark existing players as part of active selection
            self.mark_active_selection_players(channel_id)

        # Count players in the new queue (not in active selection)
        count = self.queue_collection.count_documents({
            "channel_id": channel_id,
            "active_selection": False
        })

        # Count players in the active queue (already in selection)
        active_count = self.queue_collection.count_documents({
            "channel_id": channel_id,
            "active_selection": True
        })

        # If team selection is active in this channel, inform the player they'll be in the next match
        if selection_active:
            return f"{player_mention} has joined the queue! There are {count}/6 players. (Another team selection is in progress and you'll be in the next match)"

        # Start vote if queue is full for this channel and no selection is active
        if count >= 6 and not selection_active:
            return f"{player_mention} has joined the queue! Queue is now full!\n\nStarting team selection vote..."

        return f"{player_mention} has joined the queue! There are {count}/6 players"

    def mark_active_selection_players(self, channel_id):
        """Mark the first 6 players in a channel's queue as part of the active selection"""
        channel_id = str(channel_id)
        self.active_selection_queues[channel_id] = True

        # Get the first 6 players
        players = list(self.queue_collection.find({"channel_id": channel_id}).limit(6))

        # Mark them as part of active selection
        for player in players:
            self.queue_collection.update_one(
                {"_id": player["_id"]},
                {"$set": {"active_selection": True}}
            )

    def get_queue_status(self, channel_id):
        """Get the current status of a specific channel's queue"""
        channel_id = str(channel_id)

        # Get all players currently in this channel's queue, separated by active_selection
        active_players = list(self.queue_collection.find({
            "channel_id": channel_id,
            "active_selection": True
        }))

        waiting_players = list(self.queue_collection.find({
            "channel_id": channel_id,
            "active_selection": False
        }))

        # Combined players for total count (for backward compatibility)
        all_players = active_players + waiting_players
        count = len(all_players)
        waiting_count = len(waiting_players)

        # Create an embed instead of plain text
        embed = discord.Embed(
            title="Queue Status",
            description=f"**Current Queue: {waiting_count}/6 players**",
            color=0x3498db
        )

        if count == 0:
            embed.add_field(name="Status", value="Queue is empty! Use `/join` to join the queue.", inline=False)
            return embed

        # Create lists of player mentions
        if active_players:
            active_mentions = [player['mention'] for player in active_players]
            embed.add_field(
                name="Players in Active Selection",
                value=", ".join(active_mentions),
                inline=False
            )

        if waiting_players:
            waiting_mentions = [player['mention'] for player in waiting_players]
            embed.add_field(
                name="Players in Queue",
                value=", ".join(waiting_mentions),
                inline=False
            )
        else:
            embed.add_field(
                name="Players in Queue",
                value="No players waiting in queue",
                inline=False
            )

        # Add info about how many more players are needed
        if waiting_count < 6:
            more_needed = 6 - waiting_count
            embed.add_field(name="Info", value=f"{more_needed} more player(s) needed for a match.", inline=False)
        elif channel_id in self.vote_systems and self.vote_systems[channel_id].is_voting_active():
            embed.add_field(name="Status", value="**Voting in progress!** React to the vote message.",
                            inline=False)
        elif channel_id in self.captains_systems and self.captains_systems[channel_id].is_selection_active():
            embed.add_field(name="Status", value="**Captain selection in progress!**", inline=False)
        else:
            # Queue is full but no selection active
            embed.add_field(name="Status", value="**Queue is FULL!** Ready to start match.", inline=False)

        return embed

    def get_players_for_match(self, channel_id):
        """Get players in the queue for a match in a specific channel"""
        channel_id = str(channel_id)

        # Check if there's an active selection in this channel
        if channel_id in self.active_selection_queues and self.active_selection_queues[channel_id]:
            # Get players marked as part of active selection
            return list(self.queue_collection.find({
                "channel_id": channel_id,
                "active_selection": True
            }))
        else:
            # The key fix: Just get all players in the channel's queue if no active selection
            return list(self.queue_collection.find({"channel_id": channel_id}))

    def remove_players_from_queue(self, players, channel_id=None):
        """Remove players from the queue, optionally filtering by channel"""
        # Get player IDs
        player_ids = [player['id'] for player in players]

        if channel_id:
            # Delete specified players in specified channel
            self.queue_collection.delete_many({
                "id": {"$in": player_ids},
                "channel_id": str(channel_id)
            })

            # Check if we should clear active selection tracking
            if channel_id in self.active_selection_queues:
                # Check if any players still have active_selection=True
                remaining = self.queue_collection.find_one({
                    "channel_id": str(channel_id),
                    "active_selection": True
                })

                if not remaining:
                    # No more players in active selection, clear tracking
                    del self.active_selection_queues[channel_id]
        else:
            # Delete specified players from all channels
            self.queue_collection.delete_many({"id": {"$in": player_ids}})

    async def remove_inactive_players(self):
        """Check and remove players who have been in queue for too long (60 minutes)"""
        while True:
            try:
                # Sleep first to avoid immediate checks on startup
                await asyncio.sleep(300)  # Check every 5 minutes

                # Calculate cutoff time (60 minutes ago)
                cutoff_time = datetime.datetime.utcnow() - datetime.timedelta(minutes=60)

                # Find players to remove
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