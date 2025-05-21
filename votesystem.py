import discord
from discord.ext import commands
import asyncio
import random
import uuid
from discord.ui import Button, View


class VoteSystem:
    def __init__(self, db, queue_manager, captains_system=None, match_system=None):
        self.db = db
        self.queue_manager = queue_manager
        self.match_system = match_system
        self.captains_system = captains_system
        self.bot = None

        # Store voting state by channel and match
        self.active_votes = {}  # Map of match_id to voting state

    def set_match_system(self, match_system):
        """Set the match system reference"""
        self.match_system = match_system

    def set_captains_system(self, captains_system):
        """Set the captains system reference"""
        self.captains_system = captains_system

    def set_bot(self, bot):
        """Set the bot instance"""
        self.bot = bot

    def is_voting_active(self, match_id=None, channel_id=None):
        """
        Check if voting is active
        If match_id is provided, check for that specific match
        If channel_id is provided, check for any match in that channel
        If neither is provided, check if any voting is active
        """
        if match_id:
            return match_id in self.active_votes
        elif channel_id:
            # Check if any match in this channel has active voting
            for match_id, vote_state in self.active_votes.items():
                match = self.queue_manager.get_match_by_id(match_id)
                if match and str(match.get('channel_id', '')) == str(channel_id):
                    return True
            return False
        else:
            return len(self.active_votes) > 0

    def cancel_voting(self, match_id=None, channel_id=None):
        """
        Cancel voting
        If match_id is provided, cancel for that specific match
        If channel_id is provided, cancel for all matches in that channel
        If neither is provided, cancel all voting
        """
        if match_id:
            if match_id in self.active_votes:
                del self.active_votes[match_id]
        elif channel_id:
            # Cancel voting for all matches in this channel
            match_ids_to_remove = []
            for match_id, vote_state in self.active_votes.items():
                match = self.queue_manager.get_match_by_id(match_id)
                if match and str(match.get('channel_id', '')) == str(channel_id):
                    match_ids_to_remove.append(match_id)

            for match_id in match_ids_to_remove:
                if match_id in self.active_votes:
                    del self.active_votes[match_id]
        else:
            self.active_votes.clear()

    async def start_vote(self, channel):
        """Start a vote for team selection using buttons for the next match in the channel"""
        channel_id = str(channel.id)

        # Find the match in voting status for this channel
        match = self.queue_manager.get_match_by_channel(channel_id, status="voting")
        if not match:
            await channel.send("No match ready for voting in this channel!")
            return False

        match_id = match.get('match_id')
        players = match.get('players', [])

        if len(players) < 6:
            await channel.send("Not enough players to start voting!")
            return False

        # Make sure any existing vote for this match is canceled first
        if match_id in self.active_votes:
            self.cancel_voting(match_id=match_id)

        # Initialize voting state for this match
        self.active_votes[match_id] = {
            'message': None,
            'channel': channel,
            'match_id': match_id,
            'voters': set(),
            'user_votes': {},
            'random_votes': 0,
            'captains_votes': 0,
            'view': None,  # Store the View object
            'player_ids': [p.get('id') for p in players]  # Store player IDs
        }

        # Get mentions of match players
        player_mentions = []
        for p in players:
            mention = p.get('mention', p.get('name', 'Unknown'))
            # Add [BOT] tag for dummy players
            if p.get('id', '').startswith('9000'):
                mention += " [BOT]"
            player_mentions.append(mention)

        # Create and send vote message with buttons
        embed = discord.Embed(
            title="🗳️ Team Selection Vote",
            description=(
                    f"Match ID: `{match_id}`\n\n"
                    "Vote for team selection method:\n\n"
                    "Match players: " + ", ".join(player_mentions) + "\n"
                                                                     "All 6 players must vote! (30 second timeout)"
            ),
            color=0x3498db
        )

        # Create buttons
        random_button = Button(style=discord.ButtonStyle.primary, custom_id="random", label="Random Teams", emoji="🎲")
        captains_button = Button(style=discord.ButtonStyle.primary, custom_id="captains", label="Captains Pick",
                                 emoji="👑")

        # Create View with 30 second timeout
        view = View(timeout=30)
        view.add_item(random_button)
        view.add_item(captains_button)

        # Set up button callbacks
        async def random_callback(interaction):
            await self.handle_button_vote(interaction, "random")

        async def captains_callback(interaction):
            await self.handle_button_vote(interaction, "captains")

        random_button.callback = random_callback
        captains_button.callback = captains_callback

        # Send the vote message
        vote_message = await channel.send(embed=embed, view=view)
        self.active_votes[match_id]['message'] = vote_message
        self.active_votes[match_id]['view'] = view

        # Start vote timeout
        self.bot.loop.create_task(self.vote_timeout(match_id, 30))

        return True

    async def handle_button_vote(self, interaction, vote_type):
        """Handle button presses for voting"""
        channel_id = str(interaction.channel.id)
        user_id = str(interaction.user.id)

        # Find the active match for this channel
        match = self.queue_manager.get_match_by_channel(channel_id, status="voting")
        if not match:
            await interaction.response.send_message("No active voting in this channel!", ephemeral=True)
            return

        match_id = match.get('match_id')

        # Check if this match has active voting
        if match_id not in self.active_votes:
            await interaction.response.send_message("This vote is no longer active.", ephemeral=True)
            return

        vote_state = self.active_votes[match_id]

        # Check if player is in the match
        if user_id not in vote_state['player_ids']:
            await interaction.response.send_message("Only players in this match can vote!", ephemeral=True)
            return

        # Add user to voters if not already tracked
        new_vote = False
        if user_id not in vote_state['voters']:
            vote_state['voters'].add(user_id)
            new_vote = True

        # Update vote counts
        old_vote = vote_state['user_votes'].get(user_id)
        if old_vote and old_vote != vote_type:
            # Remove old vote count
            if old_vote == "random":
                vote_state['random_votes'] -= 1
            else:
                vote_state['captains_votes'] -= 1

        # Update user's vote
        vote_state['user_votes'][user_id] = vote_type

        # Add new vote count
        if vote_type == "random":
            if not old_vote or old_vote != vote_type:
                vote_state['random_votes'] += 1
        else:
            if not old_vote or old_vote != vote_type:
                vote_state['captains_votes'] += 1

        # Acknowledge the interaction
        if new_vote:
            await interaction.response.send_message(f"You voted for {vote_type.capitalize()} teams!", ephemeral=True)
        else:
            await interaction.response.send_message(f"Changed your vote to {vote_type.capitalize()} teams!",
                                                    ephemeral=True)

        # Update vote message
        await self.update_vote_message(match_id)

        # Check if all 6 players have voted
        if len(vote_state['voters']) >= 6:
            await self.finalize_vote(match_id)

    async def update_vote_message(self, match_id):
        """Update the vote message with current counts for a specific match"""
        if match_id not in self.active_votes or self.active_votes[match_id]['message'] is None:
            return

        vote_state = self.active_votes[match_id]
        total_votes = len(vote_state['voters'])
        votes_needed = 6 - total_votes

        # Get match object
        match = self.queue_manager.get_match_by_id(match_id)
        if not match:
            return

        embed = discord.Embed(
            title="🗳️ Team Selection Vote",
            description=(
                f"Match ID: `{match_id}`\n\n"
                "Vote for team selection method:\n\n"
                f"🎲 Random Teams: **{vote_state['random_votes']}** votes\n"
                f"👑 Captains Pick: **{vote_state['captains_votes']}** votes\n\n"
                f"Votes received: **{total_votes}/6**\n"
                f"Votes needed: **{votes_needed}**"
            ),
            color=0x3498db
        )

        await vote_state['message'].edit(embed=embed)

    async def vote_timeout(self, match_id, seconds):
        """Handle vote timeout for a specific match"""
        await asyncio.sleep(seconds)

        # Check if voting is still active for this match
        if match_id not in self.active_votes:
            return  # Vote was already completed or canceled

        vote_state = self.active_votes[match_id]

        # Check if voting is complete
        if len(vote_state['voters']) >= 6:
            return  # Voting already complete

        # If not, announce timeout and create teams based on current votes
        if vote_state['channel']:
            await vote_state['channel'].send("⏱️ The vote has timed out! Creating teams based on current votes...")

        # Disable the buttons in the view
        if vote_state['view']:
            for item in vote_state['view'].children:
                item.disabled = True

            if vote_state['message']:
                await vote_state['message'].edit(view=vote_state['view'])

        # Finalize vote regardless of vote count
        await self.finalize_vote(match_id, force=True)

    async def finalize_vote(self, match_id, force=False):
        """Finalize the vote and create teams for a specific match"""
        if match_id not in self.active_votes:
            return

        vote_state = self.active_votes[match_id]

        # If forced, we'll create teams even with incomplete voting
        if not force and len(vote_state['voters']) < 6:
            # Not all players voted and not forced
            return

        # Get the channel object
        channel = vote_state['channel']
        if not channel:
            return

        # Get the match
        match = self.queue_manager.get_match_by_id(match_id)
        if not match:
            return

        players = match.get('players', [])

        # Disable the buttons in the view
        if vote_state['view']:
            for item in vote_state['view'].children:
                item.disabled = True

            if vote_state['message']:
                await vote_state['message'].edit(view=vote_state['view'])

        # Determine winner (default to random if tied or no votes)
        if vote_state['captains_votes'] > vote_state['random_votes']:
            # Captains wins - update match status and start captains selection
            self.queue_manager.update_match_status(match_id, "selection")

            # Cancel this vote
            self.cancel_voting(match_id=match_id)

            # Use the captains_system reference
            if self.captains_system:
                captains_result = self.captains_system.start_captains_selection(players, match_id, channel)

                if isinstance(captains_result, discord.Embed):
                    await channel.send(embed=captains_result)
                else:
                    await channel.send(captains_result)

                # Execute the captain selection
                await self.captains_system.execute_captain_selection(channel, match_id)
            else:
                # Fallback to random teams if captains_system is not set
                await channel.send("Captains system not available. Falling back to random teams...")
                await self.create_balanced_random_teams(channel, match_id)
        else:
            # Random teams wins - create balanced random teams
            await self.create_balanced_random_teams(channel, match_id)

            # Cancel this vote
            self.cancel_voting(match_id=match_id)

    async def create_balanced_random_teams(self, channel, match_id):
        """Create balanced random teams based on MMR"""
        # Normalize match ID
        match_id = str(match_id).strip()
        if len(match_id) > 8:
            match_id = match_id[:6]

        print(f"Creating balanced random teams for match ID: {match_id}")

        # Get the match
        match = self.queue_manager.get_match_by_id(match_id)
        if not match:
            await channel.send(f"Error: Match with ID {match_id} no longer exists!")
            return

        players = match.get('players', [])
        is_global = match.get('is_global', False)

        # Get MMR for each player (real or dummy)
        player_mmrs = []
        for player in players:
            player_id = player.get("id")

            # Check if this is a dummy player with MMR
            if "dummy_mmr" in player:
                mmr = player.get("dummy_mmr")
                player_mmrs.append((player, mmr))
            # Otherwise look up in database
            elif not player_id.startswith('9000'):  # Skip dummy players without MMR
                if is_global:
                    # For global matches, use global MMR
                    player_data = self.match_system.players.find_one({"id": player_id})
                    if player_data and "global_mmr" in player_data:
                        mmr = player_data.get("global_mmr", 300)
                    else:
                        # For new players, use default global MMR
                        mmr = 300
                else:
                    # For ranked matches, use regular MMR
                    player_data = self.match_system.players.find_one({"id": player_id})
                    if player_data:
                        mmr = player_data.get("mmr", 600)
                    else:
                        # For new players, check rank record
                        rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                        if rank_record:
                            tier = rank_record.get("tier", "Rank C")
                            mmr = self.match_system.TIER_MMR.get(tier, 600)
                        else:
                            # Default MMR
                            mmr = 600

                player_mmrs.append((player, mmr))
            else:
                # Dummy player without MMR (shouldn't happen with our changes)
                # Assign a random MMR based on channel
                channel_name = channel.name.lower()
                if channel_name == "rank-a":
                    mmr = random.randint(1600, 2100)
                elif channel_name == "rank-b":
                    mmr = random.randint(1100, 1599)
                else:  # rank-c or global
                    mmr = random.randint(600, 1099)

                player_mmrs.append((player, mmr))

        # Sort players by MMR (highest to lowest)
        player_mmrs.sort(key=lambda x: x[1], reverse=True)

        # Initialize teams
        team1 = []
        team2 = []
        team1_mmr = 0
        team2_mmr = 0

        # Assign players to teams for balance (snake draft - highest, 2nd highest to team 2, etc.)
        while player_mmrs:
            # Get highest MMR player
            if player_mmrs:
                if team1_mmr <= team2_mmr:
                    player, mmr = player_mmrs.pop(0)  # Take from front (highest MMR)
                    team1.append(player)
                    team1_mmr += mmr
                else:
                    player, mmr = player_mmrs.pop(0)  # Take from front (highest MMR)
                    team2.append(player)
                    team2_mmr += mmr

            # Get lowest MMR player if any remain
            if player_mmrs:
                if team1_mmr <= team2_mmr:
                    player, mmr = player_mmrs.pop(-1)  # Take from end (lowest MMR)
                    team1.append(player)
                    team1_mmr += mmr
                else:
                    player, mmr = player_mmrs.pop(-1)  # Take from end (lowest MMR)
                    team2.append(player)
                    team2_mmr += mmr

        # Format team mentions
        team1_mentions = []
        team2_mentions = []

        for player in team1:
            mention = player.get('mention', player.get('name', 'Unknown'))
            if player.get('id', '').startswith('9000'):
                mention += " [BOT]"
            team1_mentions.append(mention)

        for player in team2:
            mention = player.get('mention', player.get('name', 'Unknown'))
            if player.get('id', '').startswith('9000'):
                mention += " [BOT]"
            team2_mentions.append(mention)

        # Calculate average MMR per team for display
        team1_avg_mmr = round(team1_mmr / len(team1), 1) if team1 else 0
        team2_avg_mmr = round(team2_mmr / len(team2), 1) if team2 else 0

        # Debug log teams before assignment
        print(f"Assigning balanced teams to match {match_id}")
        print(f"Team 1: {[p.get('name', 'Unknown') for p in team1]}")
        print(f"Team 2: {[p.get('name', 'Unknown') for p in team2]}")

        # Update match with teams - MAKE SURE WE'RE USING THE ORIGINAL MATCH ID
        self.queue_manager.assign_teams_to_match(match_id, team1, team2)

        # Ensure players are properly tracked in the system
        for player in team1 + team2:
            player_id = str(player.get('id', ''))
            if player_id:
                self.queue_manager.player_matches[player_id] = match_id
                print(f"Added player {player.get('name', 'Unknown')} (ID: {player_id}) to match {match_id}")

        # Calculate average MMR per team for display
        team1_avg_mmr = round(team1_mmr / len(team1), 1) if team1 else 0
        team2_avg_mmr = round(team2_mmr / len(team2), 1) if team2 else 0

        # Create an embed for team announcement
        embed = discord.Embed(
            title="Match Created! (Balanced Random Teams)",
            color=0xe74c3c
        )

        embed.add_field(name="Match ID", value=f"`{match_id}`", inline=False)
        embed.add_field(name=f"Team 1 (Avg MMR: {team1_avg_mmr})", value=", ".join(team1_mentions), inline=False)
        embed.add_field(name=f"Team 2 (Avg MMR: {team2_avg_mmr})", value=", ".join(team2_mentions), inline=False)
        embed.add_field(
            name="Report Results",
            value=f"Play your match and report the result using `/report {match_id} win` or `/report {match_id} loss`",
            inline=False
        )

        # Send team announcement as embed
        await channel.send(embed=embed)

        # Cancel this vote
        self.cancel_voting(match_id=match_id)