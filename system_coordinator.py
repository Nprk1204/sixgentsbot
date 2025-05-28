import discord
from discord.ext import commands
import asyncio
import os
from database import Database
from queue_manager import QueueManager
from votesystem import VoteSystem
from captainssystem import CaptainsSystem
from matchsystem import MatchSystem


class SystemCoordinator:
    """
    Coordinates and initializes all the systems for the 6 Mans bot
    """

    def __init__(self, db, bot=None):
        self.db = db
        self.bot = bot

        # Create systems
        self.queue_manager = QueueManager(db)
        self.match_system = MatchSystem(db, self.queue_manager)

        # Create channel-specific systems
        self.channel_names = ["rank-a", "rank-b", "rank-c", "global"]
        self.vote_systems = {}
        self.captains_systems = {}

        # Initialize all systems
        self.initialize_systems()

        # Set bot if provided
        if bot:
            self.set_bot(bot)

    def initialize_systems(self):
        """Initialize all systems and set up connections between them"""
        # First create channel-specific systems
        for channel_name in self.channel_names:
            # Create CaptainsSystem for channel
            captain_sys = CaptainsSystem(self.db, self.queue_manager, self.match_system)
            self.captains_systems[channel_name] = captain_sys

            # Create VoteSystem for channel
            vote_sys = VoteSystem(self.db, self.queue_manager, captain_sys, self.match_system)
            self.vote_systems[channel_name] = vote_sys

        # Link all systems together
        self.queue_manager.set_match_system(self.match_system)
        self.match_system.set_queue_manager(self.queue_manager)

    def set_bot(self, bot):
        """Set the bot instance in all systems"""
        self.bot = bot

        # Set bot in all systems
        self.queue_manager.set_bot(bot)
        self.match_system.set_bot(bot)

        # Set bot in all channel-specific systems
        for channel_name in self.channel_names:
            self.vote_systems[channel_name].set_bot(bot)
            self.captains_systems[channel_name].set_bot(bot)

        # Configure queue_manager with vote and captain systems for all channels
        self.connect_channel_systems()

    def connect_channel_systems(self):
        """Connect channel-specific systems with the queue manager"""
        if not self.bot:
            return

        print("Connecting channel systems to queue manager...")

        # Find all channels across all guilds
        for guild in self.bot.guilds:
            for channel in guild.text_channels:
                channel_name = channel.name.lower()
                channel_id = str(channel.id)

                # Connect if the channel is one of our supported types
                if channel_name in self.channel_names:
                    print(f"Found channel: {channel.name} ({channel_id})")

                    # Connect vote system for this channel
                    if channel_name in self.vote_systems:
                        self.queue_manager.set_vote_system(channel_id, self.vote_systems[channel_name])
                        # ALSO set it by channel name for easier access
                        self.queue_manager.vote_systems[channel_name] = self.vote_systems[channel_name]
                        print(f"Connected vote system for {channel.name}")

                    # Connect captains system for this channel
                    if channel_name in self.captains_systems:
                        self.queue_manager.set_captains_system(channel_id, self.captains_systems[channel_name])
                        print(f"Connected captains system for {channel.name}")

    async def handle_reaction(self, reaction, user):
        """Handle reactions for voting"""
        if user.bot:
            return  # Ignore bot reactions

        channel_name = reaction.message.channel.name.lower()

        # Forward to the appropriate vote system if channel is supported
        if channel_name in self.vote_systems:
            await self.vote_systems[channel_name].handle_reaction(reaction, user)

    async def check_for_ready_matches(self):
        """Background task disabled to prevent duplicate vote triggers"""
        while True:
            try:
                # DISABLED: This background task was causing duplicate vote starts
                # All voting will be handled by the main command flow instead
                print("Background match check disabled to prevent duplicates")

            except Exception as e:
                print(f"Error in check_for_ready_matches: {e}")

            # Check every 60 seconds (increased interval, but mostly inactive)
            await asyncio.sleep(60)

    def is_voting_active(self, channel_id=None):
        """Check if voting is active in any/specific channel"""
        if channel_id:
            # Get the channel name from ID
            channel = None
            if self.bot:
                channel = self.bot.get_channel(int(channel_id))

            if channel:
                channel_name = channel.name.lower()
                if channel_name in self.vote_systems:
                    return self.vote_systems[channel_name].is_voting_active(channel_id=channel_id)
            return False
        else:
            # Check all vote systems
            return any(vs.is_voting_active() for vs in self.vote_systems.values())

    def is_selection_active(self, channel_id=None):
        """Check if captain selection is active in any/specific channel"""
        if channel_id:
            # Get the channel name from ID
            channel = None
            if self.bot:
                channel = self.bot.get_channel(int(channel_id))

            if channel:
                channel_name = channel.name.lower()
                if channel_name in self.captains_systems:
                    return self.captains_systems[channel_name].is_selection_active(channel_id=channel_id)
            return False
        else:
            # Check all captain systems
            return any(cs.is_selection_active() for cs in self.captains_systems.values())

    def cancel_voting(self, channel_id=None):
        """Cancel voting in a specific channel or all channels"""
        if channel_id:
            # Get the channel name from ID
            channel = None
            if self.bot:
                channel = self.bot.get_channel(int(channel_id))

            if channel:
                channel_name = channel.name.lower()
                if channel_name in self.vote_systems:
                    self.vote_systems[channel_name].cancel_voting(channel_id=channel_id)
        else:
            # Cancel voting in all systems
            for vs in self.vote_systems.values():
                vs.cancel_voting()

    def cancel_selection(self, channel_id=None):
        """Cancel captain selection in a specific channel or all channels"""
        if channel_id:
            # Get the channel name from ID
            channel = None
            if self.bot:
                channel = self.bot.get_channel(int(channel_id))

            if channel:
                channel_name = channel.name.lower()
                if channel_name in self.captains_systems:
                    self.captains_systems[channel_name].cancel_selection(channel_id=channel_id)
        else:
            # Cancel selection in all systems
            for cs in self.captains_systems.values():
                cs.cancel_selection()

    async def start_vote(self, channel):
        """Start voting for a channel"""
        channel_name = channel.name.lower()

        if channel_name in self.vote_systems:
            return await self.vote_systems[channel_name].start_vote(channel)

        return False