import asyncio
from datetime import datetime

import discord

class QueueHandler:
    def __init__(self, db):
        self.db = db
        self.queue_collection = db.get_collection('queue')
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
                    "joined_at": datetime.datetime.utcnow()  # Add timestamp
                })

                count = self.queue_collection.count_documents({"channel_id": channel_id})
                return f"{player_mention} has joined the queue! There are {count}/6 players"

            # Only format as mention if we have a valid channel ID
            if other_channel_id and other_channel_id.isdigit():
                channel_mention = f"<#{other_channel_id}>"
                return f"{player_mention} is already in a queue in {channel_mention}. Please leave that queue first."
            else:
                # Fallback if channel ID is invalid but not None
                return f"{player_mention} is already in another queue. Please leave that queue first."

        # Store player in queue with channel ID and timestamp
        self.queue_collection.insert_one({
            "id": player_id,
            "name": player_name,
            "mention": player_mention,
            "channel_id": channel_id,
            "is_global": is_global,
            "joined_at": datetime.datetime.utcnow()  # Add timestamp
        })

        # Count players in this channel's queue
        count = self.queue_collection.count_documents({"channel_id": channel_id})

        # Check if team selection is active in this channel
        vote_active = False
        captains_active = False

        if channel_id in self.vote_systems:
            vote_active = self.vote_systems[channel_id].is_voting_active(channel_id)

        if channel_id in self.captains_systems:
            captains_active = self.captains_systems[channel_id].is_selection_active(channel_id)

        # If team selection is active, inform the player but don't start another vote
        if vote_active or captains_active:
            return f"{player_mention} has joined the queue! There are {count}/6 players. (Another team selection is in progress and you'll be in the next match)"

        # Start vote if queue is full for this channel and no selection is active
        if count >= 6 and not (vote_active or captains_active):
            return f"{player_mention} has joined the queue! Queue is now full!\n\nStarting team selection vote..."

        return f"{player_mention} has joined the queue! There are {count}/6 players"

    def remove_player(self, player, channel_id):
        """Remove a player from a specific channel's queue"""
        player_id = str(player.id)
        channel_id = str(channel_id)

        # Only delete from the specific channel queue
        result = self.queue_collection.delete_one({"id": player_id, "channel_id": channel_id})

        # Cancel any active votes or selections for this channel
        if channel_id in self.vote_systems:
            self.vote_systems[channel_id].cancel_voting()

        if channel_id in self.captains_systems:
            self.captains_systems[channel_id].cancel_selection()

        if result.deleted_count > 0:
            return f"{player.mention} has left the queue!"
        else:
            return f"{player.mention} was not in this queue!"

    def get_queue_status(self, channel_id):
        """Get the current status of a specific channel's queue"""
        channel_id = str(channel_id)

        # Get all players currently in this channel's queue
        players = list(self.queue_collection.find({"channel_id": channel_id}))
        count = len(players)

        # Create an embed instead of plain text
        embed = discord.Embed(
            title="Queue Status",
            description=f"**Current Queue: {count}/6 players**",
            color=0x3498db
        )

        if count == 0:
            embed.add_field(name="Status", value="Queue is empty! Use `/join` to join the queue.", inline=False)
            return embed

        # Create a list of player mentions
        player_mentions = [player['mention'] for player in players]

        # Add player list to embed
        embed.add_field(name="Players", value=", ".join(player_mentions), inline=False)

        # Add info about how many more players are needed
        if count < 6:
            more_needed = 6 - count
            embed.add_field(name="Info", value=f"{more_needed} more player(s) needed for a match.", inline=False)
        elif channel_id in self.vote_systems and self.vote_systems[channel_id].is_voting_active():
            embed.add_field(name="Status", value="**Voting in progress!** React to the vote message.",
                            inline=False)
        elif channel_id in self.captains_systems and self.captains_systems[channel_id].is_selection_active():
            embed.add_field(name="Status", value="**Captain selection in progress!**", inline=False)

        return embed

    def get_players_for_match(self, channel_id):
        """Get players in the queue for a match in a specific channel"""
        channel_id = str(channel_id)
        return list(self.queue_collection.find({"channel_id": channel_id}).limit(6))

    def remove_players_from_queue(self, players, channel_id=None):
        """Remove players from the queue, optionally filtering by channel"""
        for player in players:
            if channel_id:
                self.queue_collection.delete_one({"id": player['id'], "channel_id": str(channel_id)})
            else:
                self.queue_collection.delete_one({"id": player['id']})

    def is_player_in_queue(self, player_id, channel_id=None):
        """Check if a player is in a specific channel's queue or any queue"""
        if channel_id:
            return self.queue_collection.find_one({"id": player_id, "channel_id": str(channel_id)}) is not None
        else:
            return self.queue_collection.find_one({"id": player_id}) is not None

    def get_queue_channels(self):
        """Get all channel IDs that have active queues"""
        channels = self.queue_collection.distinct("channel_id")
        return channels

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