import random
import asyncio
import discord
import uuid


class CaptainsSystem:
    def __init__(self, db, queue_handler, match_system=None):
        self.queue = queue_handler
        self.match_system = match_system
        self.bot = None

        # Track active selections by channel
        self.active_selections = {}  # Map of channel_id to selection state

    def set_match_system(self, match_system):
        """Set the match system reference"""
        self.match_system = match_system

    def set_bot(self, bot):
        """Set the bot instance"""
        self.bot = bot

    def is_selection_active(self, channel_id=None):
        """Check if captain selection is active in a specific channel or any channel"""
        if channel_id:
            return str(channel_id) in self.active_selections
        else:
            return len(self.active_selections) > 0

    def cancel_selection(self, channel_id=None):
        """Cancel the current selection process for a specific channel or all channels"""
        if channel_id:
            if str(channel_id) in self.active_selections:
                del self.active_selections[str(channel_id)]
        else:
            self.active_selections.clear()

    def start_captains_selection(self, players, channel_id):
        """Start the captains selection process for a specific channel"""
        channel_id = str(channel_id)

        if len(players) < 6:
            return "Not enough players to start captain selection!"

        # Choose two random players as captains
        random.shuffle(players)
        captain1 = players[0]
        captain2 = players[1]
        remaining_players = players[2:]

        # Initialize selection state for this channel
        self.active_selections[channel_id] = {
            'captain1': captain1,
            'captain2': captain2,
            'remaining_players': remaining_players,
            'captain1_team': [captain1],
            'captain2_team': [captain2],
            'match_players': players,
            'announcement_channel': None
        }

        # Format remaining players for display
        remaining_mentions = [p['mention'] for p in remaining_players]

        # Create an embed instead of plain text
        embed = discord.Embed(
            title="Match Setup: Captains Mode!",
            color=0xf1c40f
        )

        embed.add_field(name="Captain 1", value=captain1['mention'], inline=True)
        embed.add_field(name="Captain 2", value=captain2['mention'], inline=True)
        embed.add_field(name="Available Players", value=", ".join(remaining_mentions), inline=False)
        embed.set_footer(text="Captains will be contacted via DM to make their selections.")

        return embed

    # Continue with the rest of the CaptainsSystem methods (execute_captain_selection, etc.)
    # making sure to adapt them to work with channel-specific selection state

    async def execute_captain_selection(self, channel):
        """Execute the captain selection process via DMs"""
        channel_id = str(channel.id)

        # Check if selection is active for this channel
        if not self.is_selection_active(channel_id):
            return

        # Get the selection state for this channel
        selection_state = self.active_selections[channel_id]

        # Get captains from the selection state
        captain1 = selection_state['captain1']
        captain2 = selection_state['captain2']
        remaining_players = selection_state['remaining_players']
        captain1_team = selection_state['captain1_team']
        captain2_team = selection_state['captain2_team']
        match_players = selection_state['match_players']

        # Set announcement channel
        selection_state['announcement_channel'] = channel

        try:
            # Check if captains are dummy players
            if captain1['id'].startswith('9000') or captain2['id'].startswith('9000'):
                await channel.send(
                    "One or both captains are dummy players for testing. Falling back to random team selection."
                )
                await self.fallback_to_random(channel_id)
                return

            # Get discord users from IDs
            try:
                captain1_user = await self.bot.fetch_user(int(captain1['id']))
                captain2_user = await self.bot.fetch_user(int(captain2['id']))
            except (ValueError, discord.NotFound, discord.HTTPException) as e:
                await channel.send(
                    f"Error fetching captain users: {str(e)}. Falling back to random team selection."
                )
                await self.fallback_to_random(channel_id)
                return

            # Initial message to players
            await channel.send(
                f"📨 DMing captains for team selection... {captain1['mention']} will pick first.")

            # Format player list for selection
            player_options = []
            for i, player in enumerate(remaining_players):
                player_options.append(f"{i + 1}. {player['name']} ({player['mention']})")

            players_list = "\n".join(player_options)

            # DM the first captain
            try:
                captain1_dm = await captain1_user.send(
                    f"**You are Captain 1!**\n\n"
                    f"Please select **ONE** player by replying with their number:\n\n"
                    f"{players_list}\n\n"
                    "You have 60 seconds to choose."
                )

                # Wait for Captain 1's response
                response = await self.wait_for_captain_response(captain1_user, 60)

                if response is None:
                    # Timeout - make random selection
                    selection_index = random.randint(0, len(remaining_players) - 1)
                    await channel.send(
                        f"⏱️ {captain1['mention']} didn't respond in time. Random player selected."
                    )
                    await captain1_user.send("Time's up! A random player has been selected for you.")
                else:
                    try:
                        selection_index = int(response.content) - 1
                        if selection_index < 0 or selection_index >= len(remaining_players):
                            # Invalid number - make random selection
                            selection_index = random.randint(0, len(remaining_players) - 1)
                            await captain1_user.send(
                                f"Invalid selection number. A random player has been selected for you.")
                    except ValueError:
                        # Non-number input - make random selection
                        selection_index = random.randint(0, len(remaining_players) - 1)
                        await captain1_user.send(f"Invalid selection. A random player has been selected for you.")

                # Process Captain 1's selection
                selected_player = remaining_players[selection_index]
                captain1_team.append(selected_player)

                await channel.send(
                    f"🔄 **Captain 1** ({captain1['name']}) selected {selected_player['name']}"
                )

                # Update remaining players
                remaining_players.pop(selection_index)

                # Update selection state with modified lists
                selection_state['remaining_players'] = remaining_players
                selection_state['captain1_team'] = captain1_team

                # Now Captain 2 gets to select 2 players
                # ... rest of the method using the same pattern ...

                # Always update the selection state after modifications
                self.active_selections[channel_id] = selection_state

            except discord.Forbidden:
                # Cannot DM captain(s)
                await channel.send(
                    "❌ Unable to DM one or both captains. Falling back to random team selection."
                )
                await self.fallback_to_random(channel_id)

        except Exception as e:
            # Something went wrong
            await channel.send(
                f"❌ An error occurred during captain selection: {str(e)}. Falling back to random team selection."
            )
            await self.fallback_to_random(channel_id)

    async def wait_for_captain_response(self, captain, timeout):
        """Wait for a captain to respond to a DM"""
        try:
            def check(m):
                return m.author == captain and m.guild is None

            return await self.bot.wait_for('message', check=check, timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def fallback_to_random(self):
        """Fall back to random team selection if captain selection fails"""
        # Reconstruct all players
        all_players = [self.captain1, self.captain2] + self.remaining_players

        # Create random teams
        random.shuffle(all_players)
        team1 = all_players[:3]
        team2 = all_players[3:6]

        # Format team mentions
        team1_mentions = [player['mention'] for player in team1]
        team2_mentions = [player['mention'] for player in team2]

        # Create match record - using self.match_system
        match_id = self.match_system.create_match(
            str(uuid.uuid4()),
            team1,
            team2,
            str(self.announcement_channel.id)
        )

        # Create an embed for team announcement
        embed = discord.Embed(
            title="Teams Assigned Randomly!",
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
        await self.announcement_channel.send(embed=embed)

        # Clean up
        self.cleanup_after_match()

    async def finalize_teams(self):
        """Finalize and announce the teams after captain selection"""
        # Format team mentions
        team1_mentions = [player['mention'] for player in self.captain1_team]
        team2_mentions = [player['mention'] for player in self.captain2_team]

        # Create match record - using self.match_system
        match_id = self.match_system.create_match(
            str(uuid.uuid4()),
            self.captain1_team,
            self.captain2_team,
            str(self.announcement_channel.id)
        )

        # Create an embed for team announcement
        embed = discord.Embed(
            title="🏆 Teams Finalized!",
            color=0x2ecc71
        )

        embed.add_field(name="Match ID", value=f"`{match_id}`", inline=False)  # Add match ID field

        embed.add_field(
            name=f"Team 1 (Captain: {self.captain1['name']})",
            value=", ".join(team1_mentions),
            inline=False
        )
        embed.add_field(
            name=f"Team 2 (Captain: {self.captain2['name']})",
            value=", ".join(team2_mentions),
            inline=False
        )
        embed.add_field(
            name="Report Results",
            value=f"Play your match and report the result using `/report <match id> win` or `/report <match id> loss`",
            inline=False
        )

        # Send team announcement as embed
        await self.announcement_channel.send(embed=embed)

        # Clean up
        self.cleanup_after_match()

    def cleanup_after_match(self):
        """Clean up all data after a match is created"""
        # Remove players from queue
        self.queue.remove_players_from_queue(self.match_players)

        # Reset captain-related data
        self.selection_active = False
        self.captain1 = None
        self.captain2 = None
        self.remaining_players = []
        self.captain1_team = []
        self.captain2_team = []
        self.match_players = []
        self.announcement_channel = None