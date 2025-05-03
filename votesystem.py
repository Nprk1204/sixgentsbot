import discord
import asyncio
import random
import uuid


class VoteSystem:
    def __init__(self, db, queue_handler, captains_system, match_system=None):
        self.queue = queue_handler
        self.captains_system = captains_system
        self.match_system = match_system  # Store match_system as an instance variable
        self.bot = None
        self.voting_active = False
        self.vote_message = None
        self.vote_channel = None

        # Emojis for voting
        self.random_emoji = "🎲"
        self.captains_emoji = "👑"

        # Voters tracking
        self.voters = set()
        self.user_votes = {}

        # Vote counts
        self.random_votes = 0
        self.captains_votes = 0

    def set_match_system(self, match_system):
        """Set the match system reference"""
        self.match_system = match_system

    def set_bot(self, bot):
        """Set the bot instance"""
        self.bot = bot
        self.captains_system.set_bot(bot)

    def is_voting_active(self):
        """Check if voting is active"""
        return self.voting_active

    def cancel_voting(self):
        """Cancel current voting"""
        self.voting_active = False
        self.vote_message = None
        self.vote_channel = None
        self.voters.clear()
        self.user_votes = {}
        self.random_votes = 0
        self.captains_votes = 0

    async def start_vote(self, channel):
        """Start a vote for team selection using reactions"""
        # Make sure any existing vote is canceled first
        if self.voting_active:
            self.cancel_voting()

        players = self.queue.get_players_for_match()
        if len(players) < 6:
            await channel.send("Not enough players to start voting!")
            return False

        self.voting_active = True
        self.vote_channel = channel
        self.voters.clear()
        self.user_votes = {}
        self.random_votes = 0
        self.captains_votes = 0

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

        # Delete previous vote message if it exists
        if self.vote_message:
            try:
                await self.vote_message.delete()
            except:
                pass  # Ignore if message is already deleted

        self.vote_message = await channel.send(embed=embed)

        # Add reaction options
        await self.vote_message.add_reaction(self.random_emoji)
        await self.vote_message.add_reaction(self.captains_emoji)

        # Start vote timeout
        self.bot.loop.create_task(self.vote_timeout(60))

        return True  # Vote started successfully

    async def vote_timeout(self, seconds):
        """Handle vote timeout"""
        await asyncio.sleep(seconds)

        if not self.voting_active or not self.vote_message:
            return  # Vote was already completed or canceled

        # Check if voting is complete
        if len(self.voters) >= 6:
            return  # Voting already complete

        # If not, announce timeout and create teams based on current votes
        await self.vote_channel.send("⏱️ The vote has timed out! Creating teams based on current votes...")

        # Make sure to call finalize_vote regardless of vote count
        await self.finalize_vote(force=True)

    async def handle_reaction(self, reaction, user):
        """Handle reaction to vote message"""
        if not self.voting_active or self.vote_message is None:
            return

        # Ignore bot reactions
        if user.bot:
            return

        # Check if reaction is on vote message
        if reaction.message.id != self.vote_message.id:
            return

        # Check if user is in queue
        player_id = str(user.id)
        if not self.queue.is_player_in_queue(player_id):
            return

        # Check if reaction is valid
        emoji = str(reaction.emoji)
        if emoji not in [self.random_emoji, self.captains_emoji]:
            return

        # Add user to voters if not already tracked
        if user.id not in self.voters:
            self.voters.add(user.id)

        # Update vote counts
        old_vote = self.user_votes.get(user.id)
        if old_vote:
            # Remove old vote count
            if old_vote == self.random_emoji:
                self.random_votes -= 1
            else:
                self.captains_votes -= 1

        # Update user's vote
        self.user_votes[user.id] = emoji

        # Add new vote count
        if emoji == self.random_emoji:
            self.random_votes += 1
        else:
            self.captains_votes += 1

        # Update vote message
        await self.update_vote_message()

        # Check if all 6 players have voted
        if len(self.voters) >= 6:
            await self.finalize_vote()

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