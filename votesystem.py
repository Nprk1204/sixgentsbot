import discord
import asyncio
import random
import uuid


class VoteSystem:
    def __init__(self, db, queue_handler, match_system=None):
        self.db = db
        self.queue = queue_handler
        self.match_system = match_system
        self.bot = None

        # Store voting state by channel
        self.active_votes = {}  # Map of channel_id to voting state

        # Emojis for voting
        self.random_emoji = "🎲"
        self.captains_emoji = "👑"

    def set_match_system(self, match_system):
        """Set the match system reference"""
        self.match_system = match_system

    def set_bot(self, bot):
        """Set the bot instance"""
        self.bot = bot

    def is_voting_active(self, channel_id=None):
        """Check if voting is active in a specific channel or any channel"""
        if channel_id:
            return str(channel_id) in self.active_votes
        else:
            return len(self.active_votes) > 0

    def cancel_voting(self, channel_id=None):
        """Cancel current voting in a specific channel or all channels"""
        if channel_id:
            if str(channel_id) in self.active_votes:
                del self.active_votes[str(channel_id)]
        else:
            self.active_votes.clear()

    async def start_vote(self, channel):
        """Start a vote for team selection using reactions in a specific channel"""
        channel_id = str(channel.id)

        # Make sure any existing vote in this channel is canceled first
        if channel_id in self.active_votes:
            self.cancel_voting(channel_id)

        players = self.queue.get_players_for_match(channel_id)
        if len(players) < 6:
            await channel.send("Not enough players to start voting!")
            return False

        # Initialize voting state for this channel
        self.active_votes[channel_id] = {
            'message': None,
            'channel': channel,
            'voters': set(),
            'user_votes': {},
            'random_votes': 0,
            'captains_votes': 0
        }

        # Get mentions of queued players
        player_mentions = [p['mention'] for p in players]

        # Create and send vote message
        embed = discord.Embed(
            title="🗳️ Team Selection Vote",
            description=(
                "Vote for team selection method:\n\n"
                f"{self.random_emoji} - Random Teams\n"
                f"{self.captains_emoji} - Captains Pick\n\n"
                f"Queued players: {', '.join(player_mentions)}\n"
                "All 6 players must vote! (60 second timeout)"
            ),
            color=0x3498db
        )

        # Send the vote message
        vote_message = await channel.send(embed=embed)
        self.active_votes[channel_id]['message'] = vote_message

        # Add reaction options
        await vote_message.add_reaction(self.random_emoji)
        await vote_message.add_reaction(self.captains_emoji)

        # Start vote timeout
        self.bot.loop.create_task(self.vote_timeout(channel_id, 60))

        return True  # Vote started successfully

    async def vote_timeout(self, channel_id, seconds):
        """Handle vote timeout for a specific channel"""
        channel_id = str(channel_id)
        await asyncio.sleep(seconds)

        # Check if voting is still active
        if channel_id not in self.active_votes:
            return  # Vote was already completed or canceled

        vote_state = self.active_votes[channel_id]

        # Check if voting is complete
        if len(vote_state['voters']) >= 6:
            return  # Voting already complete

        # If not, announce timeout and create teams based on current votes
        await vote_state['channel'].send("⏱️ The vote has timed out! Creating teams based on current votes...")

        # Finalize vote regardless of vote count
        await self.finalize_vote(channel_id, force=True)

    async def handle_reaction(self, reaction, user):
        """Handle reaction to vote message"""
        # Ignore bot reactions
        if user.bot:
            return

        # Find which channel this reaction belongs to
        message_id = reaction.message.id
        channel_id = str(reaction.message.channel.id)

        # Check if this is a vote message in any active votes
        if channel_id not in self.active_votes or self.active_votes[channel_id]['message'].id != message_id:
            return

        vote_state = self.active_votes[channel_id]

        # Check if user is in queue
        player_id = str(user.id)
        if not self.queue.is_player_in_queue(player_id, channel_id):
            return

        # Check if reaction is valid
        emoji = str(reaction.emoji)
        if emoji not in [self.random_emoji, self.captains_emoji]:
            return

        # Add user to voters if not already tracked
        if user.id not in vote_state['voters']:
            vote_state['voters'].add(user.id)

        # Update vote counts
        old_vote = vote_state['user_votes'].get(user.id)
        if old_vote:
            # Remove old vote count
            if old_vote == self.random_emoji:
                vote_state['random_votes'] -= 1
            else:
                vote_state['captains_votes'] -= 1

        # Update user's vote
        vote_state['user_votes'][user.id] = emoji

        # Add new vote count
        if emoji == self.random_emoji:
            vote_state['random_votes'] += 1
        else:
            vote_state['captains_votes'] += 1

        # Update vote message
        await self.update_vote_message(channel_id)

        # Check if all 6 players have voted
        if len(vote_state['voters']) >= 6:
            await self.finalize_vote(channel_id)

    # Continue with the rest of the VoteSystem methods (update_vote_message, finalize_vote)
    # making sure to adapt them to work with channel-specific voting state

    async def update_vote_message(self):
        """Update the vote message with current counts"""
        if not self.voting_active or self.vote_message is None:
            return

        total_votes = len(self.voters)
        votes_needed = 6 - total_votes

        embed = discord.Embed(
            title="🗳️ Team Selection Vote",
            description=(
                "Vote for team selection method:\n\n"
                f"{self.random_emoji} Random Teams: **{self.random_votes}** votes\n"
                f"{self.captains_emoji} Captains Pick: **{self.captains_votes}** votes\n\n"
                f"Votes received: **{total_votes}/6**\n"
                f"Votes needed: **{votes_needed}**"
            ),
            color=0x3498db
        )

        await self.vote_message.edit(embed=embed)

    async def finalize_vote(self, force=False):
        """Finalize the vote and create teams"""
        if not self.voting_active:
            return

        players = self.queue.get_players_for_match()

        # If forced, we'll create teams even with incomplete voting
        if not force and len(self.voters) < 6:
            # Not all players voted and not forced
            return

        # Determine winner (default to random if tied or no votes)
        if self.captains_votes > self.random_votes:
            # Start captains selection
            self.voting_active = False
            result = self.captains_system.start_captains_selection(players)
            await self.vote_channel.send(embed=result)
            await self.captains_system.execute_captain_selection(self.vote_channel)
        else:
            # Create random teams
            random.shuffle(players)
            team1 = players[:3]
            team2 = players[3:6]

            # Format team mentions
            team1_mentions = [player['mention'] for player in team1]
            team2_mentions = [player['mention'] for player in team2]

            # Remove players from queue
            self.queue.remove_players_from_queue(players)

            # Create match record - using self.match_system
            match_id = self.match_system.create_match(
                str(uuid.uuid4()),
                team1,
                team2,
                str(self.vote_channel.id)
            )

            # Create an embed for team announcement
            embed = discord.Embed(
                title="Match Created! (Random Teams)",
                color=0xe74c3c
            )

            embed.add_field(name="Match ID", value=f"`{match_id}`", inline=False)  # Add match ID field
            embed.add_field(name="Team 1", value=", ".join(team1_mentions), inline=False)
            embed.add_field(name="Team 2", value=", ".join(team2_mentions), inline=False)
            embed.add_field(
                name="Report Results",
                value=f"Play your match and report the result using `/report <match id> win` or `/report <match id> loss`",
                inline=False
            )

            # Send team announcement as embed
            await self.vote_channel.send(embed=embed)

            # Reset vote state
            self.cancel_voting()