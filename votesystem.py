import discord
from discord.ext import commands
import asyncio
import random
import uuid
from discord.ui import Button, View


class VoteSystem:
    def __init__(self, db, queue_handler, captains_system=None, match_system=None):
        self.db = db
        self.queue = queue_handler
        self.match_system = match_system
        self.captains_system = captains_system
        self.bot = None

        # Store voting state by match_id
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

    def is_voting_active(self, channel_id=None, match_id=None):
        """Check if voting is active by channel or match ID"""
        if match_id:
            return match_id in self.active_votes
        elif channel_id:
            # Look for any votes for this channel
            channel_id_str = str(channel_id)
            for vote_id, vote_state in self.active_votes.items():
                if vote_state.get('channel_id') == channel_id_str:
                    return True
            return False
        else:
            return len(self.active_votes) > 0

    def cancel_voting(self, channel_id=None, match_id=None):
        """Cancel current voting by channel or match ID"""
        if match_id:
            if match_id in self.active_votes:
                del self.active_votes[match_id]
        elif channel_id:
            # Find and remove all votes for this channel
            channel_id_str = str(channel_id)
            matches_to_remove = []
            for vote_id, vote_state in self.active_votes.items():
                if vote_state.get('channel_id') == channel_id_str:
                    matches_to_remove.append(vote_id)

            for match_id in matches_to_remove:
                del self.active_votes[match_id]
        else:
            self.active_votes.clear()

    async def start_vote(self, channel):
        """Start a vote for team selection using buttons in a specific channel"""
        channel_id = str(channel.id)

        # Get the latest voting match for this channel
        active_match = self.queue.matches_collection.find_one({
            "channel_id": channel_id,
            "status": "voting"
        })

        if not active_match:
            await channel.send("No active match found for voting!")
            return False

        match_id = active_match["match_id"]

        # CRITICAL: Don't cancel existing votes for other matches in the same channel!
        # Only cancel a vote for THIS specific match if it already exists
        if match_id in self.active_votes:
            self.cancel_voting(match_id=match_id)

        # Get the match players
        players = active_match.get("players", [])
        if len(players) < 6:
            await channel.send("Not enough players to start voting!")
            return False

        # Initialize voting state for this match
        self.active_votes[match_id] = {
            'message': None,
            'channel': channel,
            'channel_id': channel_id,
            'match_id': match_id,
            'voters': set(),
            'user_votes': {},
            'random_votes': 0,
            'captains_votes': 0,
            'view': None,
            'player_ids': [p['id'] for p in players]
        }

        # Get mentions of match players
        player_mentions = [p['mention'] for p in players]

        # Create and send vote message with buttons
        embed = discord.Embed(
            title=f"🗳️ Team Selection Vote - Match {match_id}",
            description=(
                    "Vote for team selection method:\n\n"
                    "Match players: " + ", ".join(player_mentions) + "\n"
                                                                     "All 6 players must vote! (30 second timeout)"
            ),
            color=0x3498db
        )

        # Create buttons
        random_button = Button(
            style=discord.ButtonStyle.primary,
            custom_id=f"random_{match_id}",
            label="Random Teams",
            emoji="🎲"
        )

        captains_button = Button(
            style=discord.ButtonStyle.primary,
            custom_id=f"captains_{match_id}",
            label="Captains Pick",
            emoji="👑"
        )

        # Create View with 30 second timeout
        view = View(timeout=30)
        view.add_item(random_button)
        view.add_item(captains_button)

        # Set up button callbacks
        async def random_callback(interaction):
            await self.handle_button_vote(interaction, "random", match_id)

        async def captains_callback(interaction):
            await self.handle_button_vote(interaction, "captains", match_id)

        random_button.callback = random_callback
        captains_button.callback = captains_callback

        # Send the vote message
        vote_message = await channel.send(embed=embed, view=view)
        self.active_votes[match_id]['message'] = vote_message
        self.active_votes[match_id]['view'] = view

        # Start vote timeout
        self.bot.loop.create_task(self.vote_timeout(match_id, 30))

        return True  # Vote started successfully

    async def handle_button_vote(self, interaction, vote_type, match_id):
        """Handle a button vote for a specific match"""
        channel_id = str(interaction.channel.id)
        user_id = interaction.user.id

        # Check if this is a vote message in the active votes
        if match_id not in self.active_votes:
            await interaction.response.send_message("This vote is no longer active.", ephemeral=True)
            return

        vote_state = self.active_votes[match_id]

        # Get the active match from database to ensure latest data
        active_match = self.queue.matches_collection.find_one({
            "match_id": match_id,
            "status": "voting"
        })

        if not active_match:
            await interaction.response.send_message("This vote is no longer active.", ephemeral=True)
            # Clean up the vote state
            self.cancel_voting(match_id=match_id)
            return

        # Check if player is in this specific match
        player_id = str(user_id)
        player_in_match = False

        for player in active_match.get("players", []):
            if player.get("id") == player_id:
                player_in_match = True
                break

        if not player_in_match:
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
        """Update the vote message with current counts"""
        if match_id not in self.active_votes or self.active_votes[match_id]['message'] is None:
            return

        vote_state = self.active_votes[match_id]
        total_votes = len(vote_state['voters'])
        votes_needed = 6 - total_votes

        embed = discord.Embed(
            title=f"🗳️ Team Selection Vote - Match {match_id}",
            description=(
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
            print(f"Vote timeout handler: No active vote found for match {match_id}")
            return  # Vote was already completed or canceled

        print(f"Vote timeout triggered for match {match_id}")

        vote_state = self.active_votes[match_id]
        channel_id = vote_state.get('channel_id')

        # Verify the match is still in voting status in the database
        active_vote = None
        try:
            active_vote = self.queue.matches_collection.find_one({
                "match_id": match_id,
                "status": "voting"
            })
        except Exception as e:
            print(f"Error checking for active vote: {e}")

        if not active_vote:
            # No active vote in the database - cancel the vote state
            print(f"No active vote found in database for match {match_id}")
            self.cancel_voting(match_id=match_id)
            return

        # Check if voting is complete
        if len(vote_state['voters']) >= 6:
            print(f"Vote timeout handler: All 6 players have already voted in match {match_id}")
            return  # Voting already complete

        # Get the channel object
        channel = vote_state.get('channel')
        if not channel:
            # Can't proceed without a channel
            print(f"Vote timeout handler: No channel object found for match {match_id}")
            self.cancel_voting(match_id=match_id)
            return

        # Announce timeout and create teams based on current votes
        try:
            await channel.send(f"⏱️ Match {match_id}: The vote has timed out! Creating teams based on current votes...")
        except Exception as e:
            print(f"Error sending timeout message: {e}")
            # Try to cancel the vote even if we can't send the message
            self.cancel_voting(match_id=match_id)
            return

        # Disable the buttons in the view
        if vote_state.get('view'):
            for item in vote_state['view'].children:
                item.disabled = True

            try:
                if vote_state.get('message'):
                    await vote_state['message'].edit(view=vote_state['view'])
                    print(f"Vote timeout handler: Disabled vote buttons for match {match_id}")
            except Exception as e:
                print(f"Error disabling buttons: {e}")

        # Create a lock to prevent race conditions during timeout
        if not hasattr(self, '_timeout_locks'):
            self._timeout_locks = {}

        if match_id not in self._timeout_locks:
            self._timeout_locks[match_id] = asyncio.Lock()

        async with self._timeout_locks[match_id]:
            # Check again if vote is still active after acquiring the lock
            if match_id not in self.active_votes:
                print(f"Vote timeout handler: Vote is no longer active after lock for match {match_id}")
                return

            # Finalize vote regardless of vote count
            await self.finalize_vote(match_id, force=True)

    async def finalize_vote(self, match_id, force=False):
        """Finalize the vote and create teams for a specific match"""
        if match_id not in self.active_votes:
            return

        vote_state = self.active_votes[match_id]
        channel = vote_state.get('channel')
        channel_id = vote_state.get('channel_id')

        if not channel:
            print(f"No channel found for finalizing vote for match {match_id}")
            self.cancel_voting(match_id=match_id)
            return

        # If forced, we'll create teams even with incomplete voting
        if not force and len(vote_state['voters']) < 6:
            # Not all players voted and not forced
            return

        try:
            # Get the match data from the database
            db_match = self.queue.matches_collection.find_one({"match_id": match_id, "status": "voting"})

            if not db_match:
                print(f"No active match found in database for match {match_id}")
                await channel.send(f"⚠️ Error: Match {match_id} not found. The vote has been cancelled.")
                self.cancel_voting(match_id=match_id)
                return

            players = db_match.get("players", [])

            if len(players) < 6:
                print(f"Not enough players ({len(players)}) found in match {match_id}")
                await channel.send(
                    f"⚠️ Error: Not enough players found for match {match_id}. The vote has been cancelled.")
                self.cancel_voting(match_id=match_id)
                return

            # Disable the buttons in the view
            if vote_state.get('view'):
                for item in vote_state['view'].children:
                    item.disabled = True

                if vote_state.get('message'):
                    try:
                        await vote_state['message'].edit(view=vote_state['view'])
                    except Exception as e:
                        print(f"Error disabling buttons on finalize: {e}")

            # Determine winner (default to random if tied or no votes)
            if vote_state['captains_votes'] > vote_state['random_votes']:
                # Update match status to "selection"
                self.queue.matches_collection.update_one(
                    {"_id": db_match["_id"]},
                    {"$set": {"status": "selection"}}
                )

                # Cancel this vote since we're moving to the next stage
                self.cancel_voting(match_id=match_id)

                # Use the captains_system
                if self.captains_system:
                    try:
                        captains_result = self.captains_system.start_captains_selection(players, channel_id)
                        if captains_result:
                            await channel.send(embed=captains_result)
                            # Add a small delay to ensure the embed is sent before starting selection
                            await asyncio.sleep(0.5)
                            await self.captains_system.execute_captain_selection(channel)
                        else:
                            print(f"Failed to start captains selection for match {match_id}")
                            # Fallback to random teams
                            await channel.send(
                                f"Match {match_id}: Unable to start captains selection. Falling back to random teams...")
                            await self.create_balanced_random_teams(channel, players, channel_id, match_id)
                    except Exception as e:
                        import traceback
                        print(f"Error in captains selection for match {match_id}: {e}")
                        traceback.print_exc()
                        # Fallback to random teams
                        await channel.send(
                            f"Match {match_id}: Error in captains selection: {str(e)}. Falling back to random teams...")
                        await self.create_balanced_random_teams(channel, players, channel_id, match_id)
                else:
                    # Fallback to random teams
                    await channel.send(
                        f"Match {match_id}: Captains system not available. Falling back to random teams...")
                    await self.create_balanced_random_teams(channel, players, channel_id, match_id)
            else:
                # Random teams won - update match status to playing
                self.queue.matches_collection.update_one(
                    {"_id": db_match["_id"]},
                    {"$set": {"status": "in_progress"}}
                )

                # Cancel this vote
                self.cancel_voting(match_id=match_id)

                # Create balanced random teams
                await self.create_balanced_random_teams(channel, players, channel_id, match_id)
        except Exception as e:
            # Log the error and try to recover
            import traceback
            print(f"Error in finalize_vote for match {match_id}: {e}")
            traceback.print_exc()

            try:
                # Try to inform users
                await channel.send(
                    f"⚠️ Match {match_id}: An error occurred during team selection: {str(e)}. The vote has been cancelled.")

                # Cancel the vote
                self.cancel_voting(match_id=match_id)

                # Mark the match as cancelled in the database
                self.queue.matches_collection.update_one(
                    {"match_id": match_id},
                    {"$set": {"status": "cancelled"}}
                )
            except Exception as e2:
                print(f"Error in error handling during finalize_vote for match {match_id}: {e2}")

    # New method to create balanced random teams
    async def create_balanced_random_teams(self, channel, players, channel_id):
        """Create balanced random teams instead of completely random"""
        # Check if this is a global match by examining the channel name
        is_global = channel.name.lower() == "global"
        print(f"Creating balanced random teams in channel: {channel.name}, is_global: {is_global}")

        # Get MMR for each player (real or dummy)
        player_mmrs = []
        for player in players:
            player_id = player["id"]

            # Check if this is a dummy player with MMR
            if "dummy_mmr" in player:
                mmr = player["dummy_mmr"]
                player_mmrs.append((player, mmr))
            # Otherwise look up in database
            elif not player_id.startswith('9000'):  # Skip dummy players without MMR
                player_data = self.match_system.players.find_one({"id": player_id})
                if player_data:
                    mmr = player_data.get("mmr", 0)
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

        # Assign players to teams for balance (alternating with highest and lowest)
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

            # Get lowest MMR player
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
        team1_mentions = [player['mention'] for player in team1]
        team2_mentions = [player['mention'] for player in team2]

        # Calculate average MMR per team for display
        team1_avg_mmr = round(team1_mmr / len(team1), 1)
        team2_avg_mmr = round(team2_mmr / len(team2), 1)

        # Remove players from queue
        self.queue.remove_players_from_queue(players, channel_id)

        # Create match record - using self.match_system
        match_id = self.match_system.create_match(
            str(uuid.uuid4()),
            team1,
            team2,
            channel_id,
            is_global
        )

        # Ensure the match status is explicitly set to "in_progress"
        self.match_system.matches.update_one(
            {"match_id": match_id},
            {"$set": {"status": "in_progress"}}
        )

        # Debug print to confirm the status
        print(f"DEBUG: Match {match_id} status set to 'in_progress'")

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
            value=f"Play your match and report the result using `/report <match id> win` or `/report <match id> loss`",
            inline=False
        )

        # Send team announcement as embed
        await channel.send(embed=embed)

        # NEW: After creating a match, explicitly clear the queue for this channel
        try:
            self.queue.queue_collection.delete_many({"channel_id": channel_id})
            print(f"Cleared all queued players in channel {channel_id} after match creation")
        except Exception as e:
            print(f"Error clearing queue after match creation: {e}")

        # Cancel this vote
        self.cancel_voting(channel_id)