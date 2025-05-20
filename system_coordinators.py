import discord
import asyncio


class VoteSystemCoordinator:
    def __init__(self, vote_systems):
        self.vote_systems = vote_systems
        self.bot = None

    def set_bot(self, bot):
        """Set the bot instance for all vote systems"""
        self.bot = bot
        for vs in self.vote_systems.values():
            vs.set_bot(bot)

    async def handle_reaction(self, reaction, user):
        """Forward reaction to the appropriate vote system"""
        channel_name = reaction.message.channel.name.lower()
        if channel_name in self.vote_systems:
            await self.vote_systems[channel_name].handle_reaction(reaction, user)

    def is_voting_active(self, channel_id=None):
        """Check if voting is active in a specific channel or any channel"""
        if channel_id:
            channel = self.bot.get_channel(int(channel_id))
            if not channel:
                return False

            channel_name = channel.name.lower()
            if channel_name in self.vote_systems:
                return self.vote_systems[channel_name].is_voting_active(channel_id)
            return False
        else:
            return any(vs.is_voting_active() for vs in self.vote_systems.values())

    def cancel_voting(self, channel_id=None):
        """Cancel voting in a specific channel or all channels"""
        if channel_id:
            channel = self.bot.get_channel(int(channel_id))
            if not channel:
                return

            channel_name = channel.name.lower()
            if channel_name in self.vote_systems:
                self.vote_systems[channel_name].cancel_voting(channel_id)
        else:
            for vs in self.vote_systems.values():
                vs.cancel_voting()

    async def start_vote(self, channel):
        """Start vote in the appropriate channel"""
        channel_name = channel.name.lower()
        if channel_name in self.vote_systems:
            return await self.vote_systems[channel_name].start_vote(channel)
        return False


class CaptainSystemCoordinator:
    def __init__(self, captains_systems):
        self.captains_systems = captains_systems
        self.bot = None

    def set_bot(self, bot):
        """Set the bot instance for all captains systems"""
        self.bot = bot
        for cs in self.captains_systems.values():
            cs.set_bot(bot)

    def is_selection_active(self, channel_id=None):
        """Check if selection is active in a specific channel or any channel"""
        if channel_id:
            channel = self.bot.get_channel(int(channel_id))
            if not channel:
                return False

            channel_name = channel.name.lower()
            if channel_name in self.captains_systems:
                return self.captains_systems[channel_name].is_selection_active(channel_id)
            return False
        else:
            return any(cs.is_selection_active() for cs in self.captains_systems.values())

    def cancel_selection(self, channel_id=None):
        """Cancel selection in a specific channel or all channels"""
        if channel_id:
            channel = self.bot.get_channel(int(channel_id))
            if not channel:
                return

            channel_name = channel.name.lower()
            if channel_name in self.captains_systems:
                self.captains_systems[channel_name].cancel_selection(channel_id)
        else:
            for cs in self.captains_systems.values():
                cs.cancel_selection()

    def start_captains_selection(self, players, channel):
        """Start captain selection in the appropriate channel"""
        channel_name = channel.name.lower()
        channel_id = str(channel.id)

        if channel_name in self.captains_systems:
            return self.captains_systems[channel_name].start_captains_selection(players, channel_id)
        return "Error: Invalid channel for captains selection"

    async def execute_captain_selection(self, channel):
        """Execute captain selection in the appropriate channel"""
        channel_name = channel.name.lower()

        if channel_name in self.captains_systems:
            await self.captains_systems[channel_name].execute_captain_selection(channel)