import random
import asyncio
import discord
import uuid


class CaptainsSystem:
    def __init__(self, db, queue_handler, match_system=None):
        self.queue = queue_handler
        self.match_system = match_system  # Store match_system as an instance variable
        self.selection_active = False
        self.captain1 = None
        self.captain2 = None
        self.remaining_players = []
        self.captain1_team = []
        self.captain2_team = []
        self.match_players = []
        self.announcement_channel = None
        self.bot = None

    def set_match_system(self, match_system):
        """Set the match system reference"""
        self.match_system = match_system

    def set_bot(self, bot):
        """Set the bot instance"""
        self.bot = bot

    def is_selection_active(self):
        """Check if captain selection is active"""
        return self.selection_active

    def cancel_selection(self):
        """Cancel the current selection process"""
        self.selection_active = False
        self.captain1 = None
        self.captain2 = None
        self.remaining_players = []
        self.captain1_team = []
        self.captain2_team = []
        self.match_players = []
        self.announcement_channel = None

    def start_captains_selection(self, players):
        """Start the captains selection process"""
        if len(players) < 6:
            return "Not enough players to start captain selection!"

        # Choose two random players as captains
        random.shuffle(players)
        self.captain1 = players[0]
        self.captain2 = players[1]
        self.remaining_players = players[2:]

        # Set up captain teams
        self.captain1_team = [self.captain1]
        self.captain2_team = [self.captain2]

        # Store all players for later cleanup
        self.match_players = players

        # Format remaining players for display
        remaining_mentions = [p['mention'] for p in self.remaining_players]

        # Set captains selection active
        self.selection_active = True

        # Create an embed instead of plain text
        embed = discord.Embed(
            title="Match Setup: Captains Mode!",
            color=0xf1c40f
        )

        embed.add_field(name="Captain 1", value=self.captain1['mention'], inline=True)
        embed.add_field(name="Captain 2", value=self.captain2['mention'], inline=True)
        embed.add_field(name="Available Players", value=", ".join(remaining_mentions), inline=False)
        embed.set_footer(text="Captains will be contacted via DM to make their selections.")

        return embed  # Return the embed

    async def execute_captain_selection(self, channel):
        """Execute the captain selection process via DMs"""
        if not self.selection_active or not self.captain1 or not self.captain2:
            return

        self.announcement_channel = channel

        try:
            # Check if captains are dummy players
            if self.captain1['id'].startswith('9000') or self.captain2['id'].startswith('9000'):
                await self.announcement_channel.send(
                    "One or both captains are dummy players for testing. Falling back to random team selection."
                )
                await self.fallback_to_random()
                return

            # Get discord users from IDs
            try:
                captain1_user = await self.bot.fetch_user(int(self.captain1['id']))
                captain2_user = await self.bot.fetch_user(int(self.captain2['id']))
            except (ValueError, discord.NotFound, discord.HTTPException) as e:
                await self.announcement_channel.send(
                    f"Error fetching captain users: {str(e)}. Falling back to random team selection."
                )
                await self.fallback_to_random()
                return

            # Initial message to players
            await self.announcement_channel.send(
                f"📨 DMing captains for team selection... {self.captain1['mention']} will pick first.")

            # Format player list for selection
            player_options = []
            for i, player in enumerate(self.remaining_players):
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
                    selection_index = random.randint(0, len(self.remaining_players) - 1)
                    await self.announcement_channel.send(
                        f"⏱️ {self.captain1['mention']} didn't respond in time. Random player selected."
                    )
                    await captain1_user.send("Time's up! A random player has been selected for you.")
                else:
                    try:
                        selection_index = int(response.content) - 1
                        if selection_index < 0 or selection_index >= len(self.remaining_players):
                            # Invalid number - make random selection
                            selection_index = random.randint(0, len(self.remaining_players) - 1)
                            await captain1_user.send(
                                f"Invalid selection number. A random player has been selected for you.")
                    except ValueError:
                        # Non-number input - make random selection
                        selection_index = random.randint(0, len(self.remaining_players) - 1)
                        await captain1_user.send(f"Invalid selection. A random player has been selected for you.")

                # Process Captain 1's selection
                selected_player = self.remaining_players[selection_index]
                self.captain1_team.append(selected_player)

                await self.announcement_channel.send(
                    f"🔄 **Captain 1** ({self.captain1['name']}) selected {selected_player['name']}"
                )

                # Update remaining players
                self.remaining_players.pop(selection_index)

                # Now Captain 2 gets to select 2 players

                # Update player options
                player_options = []
                for i, player in enumerate(self.remaining_players):
                    player_options.append(f"{i + 1}. {player['name']} ({player['mention']})")

                updated_players_list = "\n".join(player_options)

                await captain2_user.send(
                    f"**You are Captain 2!**\n\n"
                    f"Please select **TWO** players by replying with their numbers separated by a space (e.g., '1 3'):\n\n"
                    f"{updated_players_list}\n\n"
                    "You have 60 seconds to choose."
                )

                # Wait for Captain 2's response
                response = await self.wait_for_captain_response(captain2_user, 60)

                if response is None:
                    # Timeout - make random selections
                    if len(self.remaining_players) >= 2:
                        selection_indices = random.sample(range(len(self.remaining_players)), 2)
                    else:
                        selection_indices = [0]

                    await self.announcement_channel.send(
                        f"⏱️ {self.captain2['mention']} didn't respond in time. Random players selected."
                    )
                    await captain2_user.send("Time's up! Random players have been selected for you.")
                else:
                    try:
                        # Parse two numbers from the response
                        selections = response.content.split()
                        selection_indices = [int(s) - 1 for s in selections[:2]]

                        # Validate selections
                        valid_indices = []
                        for idx in selection_indices:
                            if 0 <= idx < len(self.remaining_players):
                                valid_indices.append(idx)

                        if len(valid_indices) < min(2, len(self.remaining_players)):
                            # Not enough valid selections - make random selections
                            if len(self.remaining_players) >= 2:
                                selection_indices = random.sample(range(len(self.remaining_players)), 2)
                            else:
                                selection_indices = [0]
                            await captain2_user.send(f"Invalid selection. Random players have been selected for you.")
                        else:
                            selection_indices = valid_indices[:2]
                    except (ValueError, IndexError):
                        # Invalid input - make random selections
                        if len(self.remaining_players) >= 2:
                            selection_indices = random.sample(range(len(self.remaining_players)), 2)
                        else:
                            selection_indices = [0]
                        await captain2_user.send(f"Invalid selection. Random players have been selected for you.")

                # Sort indices in descending order to avoid index shifting
                selection_indices.sort(reverse=True)

                # Process Captain 2's selections
                selected_players = []
                for idx in selection_indices:
                    selected_player = self.remaining_players[idx]
                    self.captain2_team.append(selected_player)
                    selected_players.append(selected_player)
                    self.remaining_players.pop(idx)

                selections_text = ", ".join([p['name'] for p in selected_players])
                await self.announcement_channel.send(
                    f"🔄 **Captain 2** ({self.captain2['name']}) selected {selections_text}"
                )

                # Any remaining player goes to team 1
                if self.remaining_players:
                    last_player = self.remaining_players[0]
                    self.captain1_team.append(last_player)
                    await self.announcement_channel.send(
                        f"🔄 Remaining player {last_player['name']} goes to Team 1"
                    )

                # Finalize the teams
                await self.finalize_teams()

            except discord.Forbidden:
                # Cannot DM captain(s)
                await self.announcement_channel.send(
                    "❌ Unable to DM one or both captains. Falling back to random team selection."
                )
                await self.fallback_to_random()

        except Exception as e:
            # Something went wrong
            await self.announcement_channel.send(
                f"❌ An error occurred during captain selection: {str(e)}. Falling back to random team selection."
            )
            await self.fallback_to_random()

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
            value=f"Play your match and report the result using `/report win` or `/report loss`",
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
            value=f"Play your match and report the result using `/report win` or `/report loss`",
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