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
        self.captains_system = captains_system  # Add reference to captains_system
        self.bot = None

        # Store voting state by channel
        self.active_votes = {}  # Map of channel_id to voting state

    def set_match_system(self, match_system):
        """Set the match system reference"""
        self.match_system = match_system

    def set_captains_system(self, captains_system):
        """Set the captains system reference"""
        self.captains_system = captains_system

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
        """Start a vote for team selection using buttons in a specific channel"""
        channel_id = str(channel.id)

        # Make sure any existing vote in this channel is canceled first
        if channel_id in self.active_votes:
            self.cancel_voting(channel_id)

        # Get the active match players from queue handler
        players = self.queue.get_players_for_match(channel_id)
        if len(players) < 6:
            await channel.send("Not enough players to start voting!")
            return False

        # Update match status to "voting"
        if hasattr(self.queue, 'update_match_status'):
            self.queue.update_match_status(channel_id, "voting")

        # Initialize voting state for this channel
        self.active_votes[channel_id] = {
            'message': None,
            'channel': channel,
            'voters': set(),
            'user_votes': {},
            'random_votes': 0,
            'captains_votes': 0,
            'view': None,  # Store the View object
            'player_ids': [p['id'] for p in players]  # Store original player IDs
        }

        # Get mentions of match players
        player_mentions = [p['mention'] for p in players]

        # Create and send vote message with buttons
        embed = discord.Embed(
            title="🗳️ Team Selection Vote",
            description=(
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
        self.active_votes[channel_id]['message'] = vote_message
        self.active_votes[channel_id]['view'] = view

        # Start vote timeout
        self.bot.loop.create_task(self.vote_timeout(channel_id, 30))

        return True  # Vote started successfully

    async def handle_button_vote(self, interaction, vote_type):
        # Get the channel_id from the interaction
        channel_id = str(interaction.channel.id)
        user_id = interaction.user.id

        # Check if this is a vote message in any active votes
        if channel_id not in self.active_votes:
            await interaction.response.send_message("This vote is no longer active.", ephemeral=True)
            return

        vote_state = self.active_votes[channel_id]

        # NEW: Get the active match directly from database to ensure latest data
        active_match = self.queue.matches_collection.find_one({
            "channel_id": channel_id,
            "status": "voting"
        })

        if not active_match:
            await interaction.response.send_message("This vote is no longer active.", ephemeral=True)
            # Clean up the vote state since the database shows no active vote
            self.cancel_voting(channel_id)
            return

        # Check if player is in the active match
        player_id = str(user_id)
        player_in_match = False

        for player in active_match.get("players", []):
            if player.get("id") == player_id:
                player_in_match = True
                break

        if not player_in_match:
            await interaction.response.send_message("Only players in this match can vote!", ephemeral=True)
            return

        # Check if player is in the active match
        player_id = str(user_id)

        # Check if this user is in the list of players for this vote
        if player_id not in vote_state['player_ids']:
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
        await self.update_vote_message(channel_id)

        # Check if all 6 players have voted
        if len(vote_state['voters']) >= 6:
            await self.finalize_vote(channel_id)

    async def update_vote_message(self, channel_id):
        """Update the vote message with current counts for specific channel"""
        channel_id = str(channel_id)

        if channel_id not in self.active_votes or self.active_votes[channel_id]['message'] is None:
            return

        vote_state = self.active_votes[channel_id]
        total_votes = len(vote_state['voters'])
        votes_needed = 6 - total_votes

        embed = discord.Embed(
            title="🗳️ Team Selection Vote",
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

    async def vote_timeout(self, channel_id, seconds):
        """Handle vote timeout for a specific channel"""
        channel_id = str(channel_id)
        await asyncio.sleep(seconds)

        # Check if voting is still active for this channel
        if channel_id not in self.active_votes:
            print(f"Vote timeout handler: No active vote found for channel {channel_id}")
            return  # Vote was already completed or canceled

        print(f"Vote timeout triggered for channel {channel_id}")

        # Get the current active vote to make sure it hasn't changed
        active_vote = None
        try:
            active_vote = self.queue.matches_collection.find_one({
                "channel_id": channel_id,
                "status": "voting"
            })
        except Exception as e:
            print(f"Error checking for active vote: {e}")

        if not active_vote:
            # No active vote in the database - cancel the vote state
            print(f"No active vote found in database for channel {channel_id}")
            self.cancel_voting(channel_id)
            return

        vote_state = self.active_votes[channel_id]

        # Check if voting is complete
        if len(vote_state['voters']) >= 6:
            print(f"Vote timeout handler: All 6 players have already voted in channel {channel_id}")
            return  # Voting already complete

        # Get the channel object
        channel = vote_state.get('channel')
        if not channel:
            # Can't proceed without a channel
            print(f"Vote timeout handler: No channel object found for channel {channel_id}")
            self.cancel_voting(channel_id)
            return

        # Announce timeout and create teams based on current votes
        try:
            await channel.send("⏱️ The vote has timed out! Creating teams based on current votes...")
        except Exception as e:
            print(f"Error sending timeout message: {e}")
            # Try to cancel the vote even if we can't send the message
            self.cancel_voting(channel_id)
            return

        # Disable the buttons in the view
        if vote_state.get('view'):
            for item in vote_state['view'].children:
                item.disabled = True

            try:
                if vote_state.get('message'):
                    await vote_state['message'].edit(view=vote_state['view'])
                    print(f"Vote timeout handler: Disabled vote buttons in channel {channel_id}")
            except Exception as e:
                print(f"Error disabling buttons: {e}")

        # Create a lock to prevent race conditions during timeout
        if not hasattr(self, '_timeout_locks'):
            self._timeout_locks = {}

        if channel_id not in self._timeout_locks:
            self._timeout_locks[channel_id] = asyncio.Lock()

        async with self._timeout_locks[channel_id]:
            # Check again if vote is still active after acquiring the lock
            if channel_id not in self.active_votes:
                print(f"Vote timeout handler: Vote is no longer active after lock for channel {channel_id}")
                return

            # Finalize vote regardless of vote count
            await self.finalize_vote(channel_id, force=True)

    async def finalize_vote(self, channel_id, force=False):
        """Finalize the vote and create teams for a specific channel"""
        channel_id = str(channel_id)

        if channel_id not in self.active_votes:
            return

        vote_state = self.active_votes[channel_id]
        channel = vote_state.get('channel')

        if not channel:
            print(f"No channel found for finalizing vote in channel_id {channel_id}")
            self.cancel_voting(channel_id)
            return

        # If forced, we'll create teams even with incomplete voting
        if not force and len(vote_state['voters']) < 6:
            # Not all players voted and not forced
            return

        try:
            # Get players from the active match in the database to ensure we have the latest
            db_match = self.queue.matches_collection.find_one({"channel_id": channel_id, "status": "voting"})

            if not db_match:
                print(f"No active match found in database for channel {channel_id}")
                await channel.send("⚠️ Error: No active match found. The vote has been cancelled.")
                self.cancel_voting(channel_id)
                return

            players = db_match.get("players", [])

            if len(players) < 6:
                print(f"Not enough players ({len(players)}) found in match for channel {channel_id}")
                await channel.send("⚠️ Error: Not enough players found. The vote has been cancelled.")
                self.cancel_voting(channel_id)
                return

            # Disable the buttons in the view if it exists
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
                if hasattr(self.queue, 'update_match_status'):
                    self.queue.update_match_status(channel_id, "selection")

                # Update the database match status
                self.queue.matches_collection.update_one(
                    {"_id": db_match["_id"]},
                    {"$set": {"status": "selection"}}
                )

                # NEW: Clear the queue for this channel after starting a match
                try:
                    self.queue.queue_collection.delete_many({"channel_id": channel_id})
                    print(f"Cleared all queued players in channel {channel_id} after starting match")
                except Exception as e:
                    print(f"Error clearing queue after starting match: {e}")

                # Cancel this vote - MOVED HERE to ensure we cancel the vote before starting captain selection
                self.cancel_voting(channel_id)

                # Use the captains_system reference
                if self.captains_system:
                    try:
                        captains_result = self.captains_system.start_captains_selection(players, channel_id)
                        if captains_result:
                            await channel.send(embed=captains_result)
                            # Add a small delay to ensure the embed is sent before starting selection
                            await asyncio.sleep(0.5)
                            await self.captains_system.execute_captain_selection(channel)
                        else:
                            print(f"Failed to start captains selection, result was: {captains_result}")
                            # Fallback to random teams if captains_system returns None or empty
                            await channel.send("Unable to start captains selection. Falling back to random teams...")
                            await self.create_balanced_random_teams(channel, players, channel_id)
                    except Exception as e:
                        import traceback
                        print(f"Error in captains selection: {e}")
                        traceback.print_exc()
                        # Fallback to random teams if captains_system throws an exception
                        await channel.send(f"Error in captains selection: {str(e)}. Falling back to random teams...")
                        await self.create_balanced_random_teams(channel, players, channel_id)
                else:
                    # Fallback to random teams if captains_system is not set
                    await channel.send("Captains system not available. Falling back to random teams...")
                    await self.create_balanced_random_teams(channel, players, channel_id)
            else:
                # Random teams won - update match status
                if hasattr(self.queue, 'update_match_status'):
                    self.queue.update_match_status(channel_id, "playing")

                # Update the database match status
                self.queue.matches_collection.update_one(
                    {"_id": db_match["_id"]},
                    {"$set": {"status": "playing"}}
                )

                # NEW: Clear the queue for this channel after starting a match
                try:
                    self.queue.queue_collection.delete_many({"channel_id": channel_id})
                    print(f"Cleared all queued players in channel {channel_id} after starting match")
                except Exception as e:
                    print(f"Error clearing queue after starting match: {e}")

                # Cancel this vote - MOVED HERE to ensure we cancel the vote before creating teams
                self.cancel_voting(channel_id)

                # Create balanced random teams - only call once!
                await self.create_balanced_random_teams(channel, players, channel_id)
        except Exception as e:
            # Log the error and try to recover
            import traceback
            print(f"Error in finalize_vote: {e}")
            traceback.print_exc()

            try:
                # Try to inform users
                await channel.send(
                    f"⚠️ An error occurred during team selection: {str(e)}. The vote has been cancelled.")

                # Cancel the vote
                self.cancel_voting(channel_id)

                # Mark the match as cancelled in the database
                self.queue.matches_collection.update_many(
                    {"channel_id": channel_id, "status": {"$in": ["voting", "selection"]}},
                    {"$set": {"status": "cancelled"}}
                )
            except Exception as e2:
                print(f"Error in error handling during finalize_vote: {e2}")

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