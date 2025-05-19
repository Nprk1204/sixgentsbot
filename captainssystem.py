import random
import asyncio
import discord
from discord import app_commands
from discord.ui import Button, View, Select
from discord import ButtonStyle
import uuid


class CaptainsSystem:
    def __init__(self, db, queue_handler, match_system=None):
        self.queue = queue_handler
        self.match_system = match_system
        self.bot = None
        self.db = db

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
                print(f"Canceling captain selection for channel_id: {channel_id}")
                del self.active_selections[str(channel_id)]
        else:
            print("Canceling all captain selections")
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
            color=0xf1c40f  # Warm yellow color
        )

        embed.add_field(name="Captain 1", value=captain1['mention'], inline=True)
        embed.add_field(name="Captain 2", value=captain2['mention'], inline=True)
        embed.add_field(name="Available Players", value=", ".join(remaining_mentions), inline=False)
        embed.set_footer(text="Captains will be contacted via DM to make their selections.")

        return embed

    async def execute_captain_selection(self, channel):
        channel_id = str(channel.id)
        is_global = channel.name.lower() == "global"

        print(f"Captain selection for channel: {channel.name}, is_global: {is_global}")

        # Check if selection is active for this channel
        if not self.is_selection_active(channel_id):
            return

        # Get the selection state for this channel
        selection_state = self.active_selections[channel_id]

        # NEW: Verify active match exists and still in selection state
        active_match = self.match_system.matches.find_one({
            "channel_id": channel_id,
            "status": "selection"
        })

        if not active_match:
            print(f"No active match in selection state found for channel {channel_id}")
            await channel.send("⚠️ Error: The captain selection process has been cancelled.")
            self.cancel_selection(channel_id)
            return

        # Use player list from the active match for consistency
        selection_state['match_players'] = active_match.get("players", [])

        # Set announcement channel
        selection_state['announcement_channel'] = channel

        # Get captains and players from the selection state
        captain1 = selection_state['captain1']
        captain2 = selection_state['captain2']
        remaining_players = selection_state['remaining_players']
        captain1_team = selection_state['captain1_team']
        captain2_team = selection_state['captain2_team']

        try:
            # Check if captains are dummy players
            if captain1['id'].startswith('9000') or captain2['id'].startswith('9000'):
                await channel.send(
                    "One or both captains are dummy players for testing. Falling back to random team selection.")
                await self.fallback_to_random(channel_id)
                return

            # Get discord users from IDs
            try:
                captain1_user = await self.bot.fetch_user(int(captain1['id']))
                captain2_user = await self.bot.fetch_user(int(captain2['id']))
            except (ValueError, discord.NotFound, discord.HTTPException) as e:
                await channel.send(f"Error fetching captain users: {str(e)}. Falling back to random team selection.")
                await self.fallback_to_random(channel_id)
                return

            # Initial message to players
            await channel.send(f"📨 DMing captains for team selection... {captain1['mention']} will pick first.")

            # Get player MMRs for display - now passing the channel to determine if global or ranked
            player_mmrs = await self.get_player_mmrs(remaining_players, channel)

            # PHASE 1: Captain 1 selects one player
            selected_player = await self.captain1_selection(captain1_user, remaining_players, player_mmrs, channel)

            if selected_player is None:
                # Random selection if timeout
                selection_index = random.randint(0, len(remaining_players) - 1)
                selected_player = remaining_players[selection_index]
                await channel.send(f"⏱️ {captain1['mention']} didn't respond in time. Random player selected.")
                try:
                    await captain1_user.send("Time's up! A random player has been selected for you.")
                except:
                    pass  # Ignore if DM fails

            # Add selected player to team1
            captain1_team.append(selected_player)
            await channel.send(f"🔄 **Captain 1** ({captain1['name']}) selected {selected_player['name']}")

            # Remove selected player from remaining players
            remaining_players.remove(selected_player)

            # PHASE 2: Captain 2 selects two players
            selected_players = await self.captain2_selection(captain2_user, remaining_players, player_mmrs, channel)

            if not selected_players or len(selected_players) < 2:
                # Random selection for missing picks
                needed = 2 - len(selected_players)
                for _ in range(needed):
                    if remaining_players:
                        selection_index = random.randint(0, len(remaining_players) - 1)
                        selected_player = remaining_players.pop(selection_index)
                        captain2_team.append(selected_player)
                        await channel.send(
                            f"🔄 **Captain 2** ({captain2['name']}) randomly selected {selected_player['name']}")
            else:
                # Add selected players to team2
                for player in selected_players:
                    captain2_team.append(player)
                    if player in remaining_players:  # Use this check to avoid errors
                        remaining_players.remove(player)
                    await channel.send(f"🔄 **Captain 2** ({captain2['name']}) selected {player['name']}")

            # Last player automatically goes to Team 1
            if remaining_players:
                last_player = remaining_players[0]
                captain1_team.append(last_player)
                await channel.send(f"🔄 Last player {last_player['name']} automatically assigned to Team 1")

            # Create the match
            match_id = self.match_system.create_match(
                str(uuid.uuid4()),
                captain1_team,
                captain2_team,
                channel_id,
                is_global=is_global
            )

            # Ensure the match status is explicitly set to "in_progress"
            self.match_system.matches.update_one(
                {"match_id": match_id},
                {"$set": {"status": "in_progress"}}
            )

            # Debug print to confirm the status
            print(f"DEBUG: Match {match_id} status set to 'in_progress'")

            # Create team announcement embed
            embed = self.create_teams_embed(match_id, captain1, captain2, captain1_team, captain2_team)

            # Send team announcement as embed
            await channel.send(embed=embed)

            # Clean up
            self.queue.remove_players_from_queue(selection_state['match_players'])
            self.cancel_selection(channel_id)

        except Exception as e:
            import traceback
            print(f"Error in captain selection: {e}")
            traceback.print_exc()
            # Something went wrong
            await channel.send(
                f"❌ An error occurred during captain selection: {str(e)}. Falling back to random team selection.")
            await self.fallback_to_random(channel_id)

    async def get_player_mmrs(self, players, channel=None):
        """
        Get MMR for each player, considering whether it's a global or ranked queue

        Args:
            players: List of player dictionaries
            channel: The Discord channel object (to determine if it's a global queue)

        Returns:
            Dictionary mapping player IDs to their appropriate MMR values
        """
        player_mmrs = {}
        is_global = channel and channel.name.lower() == "global"

        print(f"Getting player MMRs for {'global' if is_global else 'ranked'} queue")

        for player in players:
            player_id = player['id']

            # Check for dummy players with stored MMR
            if player_id.startswith('9000') and 'dummy_mmr' in player:
                player_mmrs[player_id] = player['dummy_mmr']
                continue

            # Get player data for real players
            player_data = self.match_system.players.find_one({"id": player_id})
            if player_data:
                # Use global or ranked MMR based on channel type
                if is_global:
                    # Use global MMR or default if not available
                    player_mmrs[player_id] = player_data.get("global_mmr", 300)
                    print(f"Using global MMR for {player['name']}: {player_mmrs[player_id]}")
                else:
                    # Use regular ranked MMR
                    player_mmrs[player_id] = player_data.get("mmr", 600)
                    print(f"Using ranked MMR for {player['name']}: {player_mmrs[player_id]}")
            else:
                # For new players, check rank record
                rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                if rank_record:
                    if is_global:
                        # Use global MMR from rank record or default
                        player_mmrs[player_id] = rank_record.get("global_mmr", 300)
                        print(f"Using global MMR from rank record for {player['name']}: {player_mmrs[player_id]}")
                    else:
                        # Use tier-based MMR for ranked
                        tier = rank_record.get("tier", "Rank C")
                        player_mmrs[player_id] = self.match_system.TIER_MMR.get(tier, 600)
                        print(f"Using tier-based MMR for {player['name']}: {player_mmrs[player_id]}")
                else:
                    # Default MMR values
                    if is_global:
                        player_mmrs[player_id] = 300  # Default global MMR
                        print(f"Using default global MMR for {player['name']}: 300")
                    else:
                        player_mmrs[player_id] = 600  # Default ranked MMR
                        print(f"Using default ranked MMR for {player['name']}: 600")

        return player_mmrs

    async def captain1_selection(self, captain, players, player_mmrs, channel):
        """Handle captain 1's selection with buttons - 5 minute timeout"""
        # Determine if this is a global match
        is_global = channel.name.lower() == "global"
        mmr_type = "Global MMR" if is_global else "MMR"

        # Create an embed with player information including MMR
        embed = discord.Embed(
            title="**You are Captain 1!**",
            description="Please select **ONE** player by clicking the button next to their name.",
            color=0xf1c40f
        )

        # Add player information with MMR
        for i, player in enumerate(players):
            player_id = player['id']
            mmr = player_mmrs.get(player_id, "Unknown")
            embed.add_field(
                name=f"{i + 1}. {player['name']}",
                value=f"{mmr_type}: **{mmr}**",
                inline=False
            )

        embed.set_footer(text="You have 5 minutes to choose.")

        # Create a view with buttons for each player
        view = View(timeout=300)  # 5 minute timeout (300 seconds)

        # Create a dictionary to store the selected player
        result = {"selected_player": None}

        # Add a button for each player
        for i, player in enumerate(players):
            button = Button(
                style=ButtonStyle.primary,
                label=f"{i + 1}. {player['name']} ({mmr_type}: {player_mmrs.get(player['id'], 'Unknown')})",
                custom_id=f"select_{i}"
            )

            # Define button callback
            async def button_callback(interaction, player_index=i):
                if interaction.user.id == int(captain.id):
                    result["selected_player"] = players[player_index]
                    await interaction.response.send_message(f"You selected {players[player_index]['name']}!")
                    view.stop()

            button.callback = button_callback
            view.add_item(button)

        # Send the message with buttons
        message = await captain.send(embed=embed, view=view)

        # Wait for the captain to select a player or timeout
        await view.wait()

        # Return the selected player or None if timed out
        return result["selected_player"]

    async def captain2_selection(self, captain, players, player_mmrs, channel):
        """Handle captain 2's selection with buttons, allowing TWO selections - 5 minute timeout"""
        # Determine if this is a global match
        is_global = channel.name.lower() == "global"
        mmr_type = "Global MMR" if is_global else "MMR"

        # Create an embed with player information including MMR
        embed = discord.Embed(
            title="**You are Captain 2!**",
            description="Please select **TWO** players by clicking the buttons next to their names.",
            color=0x3498db
        )

        # Add player information with MMR
        for i, player in enumerate(players):
            player_id = player['id']
            mmr = player_mmrs.get(player_id, "Unknown")
            embed.add_field(
                name=f"{i + 1}. {player['name']}",
                value=f"{mmr_type}: **{mmr}**",
                inline=False
            )

        embed.set_footer(text="You have 5 minutes to choose.")

        # Create a view with buttons for each player
        view = View(timeout=300)  # 5 minute timeout (300 seconds)

        # Create lists to track selections
        selected_indices = []
        selected_players = []

        # Add a button for each player
        for i, player in enumerate(players):
            button = Button(
                style=ButtonStyle.primary,
                label=f"{i + 1}. {player['name']} ({mmr_type}: {player_mmrs.get(player['id'], 'Unknown')})",
                custom_id=f"select_{i}"
            )

            # Define button callback
            async def button_callback(interaction, player_index=i):
                if interaction.user.id == int(captain.id):
                    # Only allow 2 selections
                    if player_index in selected_indices:
                        # Remove if already selected
                        selected_indices.remove(player_index)
                        selected_players.remove(players[player_index])
                        await interaction.response.send_message(
                            f"Removed {players[player_index]['name']} from selection!")
                    elif len(selected_indices) < 2:
                        # Add if less than 2 selected
                        selected_indices.append(player_index)
                        selected_players.append(players[player_index])

                        if len(selected_indices) == 2:
                            await interaction.response.send_message(
                                f"You selected {players[selected_indices[0]]['name']} and {players[selected_indices[1]]['name']}!")
                            view.stop()
                        else:
                            await interaction.response.send_message(
                                f"You selected {players[player_index]['name']}! Please select one more player.")
                    else:
                        # Already selected 2 players
                        await interaction.response.send_message("You already selected 2 players!", ephemeral=True)

            button.callback = button_callback
            view.add_item(button)

        # Send the message with buttons
        message = await captain.send(embed=embed, view=view)

        # Wait for the captain to select 2 players or timeout
        await view.wait()

        # Return the selected players
        return selected_players

    def create_teams_embed(self, match_id, captain1, captain2, team1, team2):
        """Create a nice embed for team announcement"""
        # Format team mentions
        team1_mentions = [player['mention'] for player in team1]
        team2_mentions = [player['mention'] for player in team2]

        # Calculate average MMR for each team
        team1_mmr = self.calculate_team_mmr(team1)
        team2_mmr = self.calculate_team_mmr(team2)

        # Create embed
        embed = discord.Embed(
            title="🏆 Teams Finalized!",
            color=0x2ecc71
        )

        # Add match ID
        embed.add_field(name="Match ID", value=f"`{match_id}`", inline=False)

        # Add team 1 with average MMR
        embed.add_field(
            name=f"Team 1 (Captain: {captain1['name']}) - Avg MMR: {team1_mmr}",
            value=", ".join(team1_mentions),
            inline=False
        )

        # Add team 2 with average MMR
        embed.add_field(
            name=f"Team 2 (Captain: {captain2['name']}) - Avg MMR: {team2_mmr}",
            value=", ".join(team2_mentions),
            inline=False
        )

        # Add reporting instructions
        embed.add_field(
            name="Report Results",
            value=f"Play your match and report the result using `/report <match id> win` or `/report <match id> loss`",
            inline=False
        )

        return embed

    def calculate_team_mmr(self, team):
        """Calculate the average MMR for a team"""
        total_mmr = 0
        player_count = 0

        # Check if the team has any players
        if not team:
            print("Warning: Attempting to calculate MMR for empty team")
            return 0

        print(f"Calculating MMR for team with {len(team)} players")

        for player in team:
            player_id = player['id']
            print(f"Processing player: {player.get('name', 'Unknown')} (ID: {player_id})")

            # Check for dummy players with stored MMR
            if player_id.startswith('9000') and 'dummy_mmr' in player:
                mmr = player['dummy_mmr']
                print(f"Using dummy_mmr for {player.get('name', 'Unknown')}: {mmr}")
                total_mmr += mmr
                player_count += 1
                continue

            # Skip dummy players without MMR
            if player_id.startswith('9000') and 'dummy_mmr' not in player:
                print(f"Skipping dummy player without MMR: {player.get('name', 'Unknown')}")
                continue

            # Get player data for real players
            player_data = self.match_system.players.find_one({"id": player_id})
            if player_data:
                # For real players, use their stored MMR
                mmr = player_data.get("mmr", 0)
                print(f"Using stored MMR for {player.get('name', 'Unknown')}: {mmr}")
                total_mmr += mmr
                player_count += 1
            else:
                # For new players, check rank record
                rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                if rank_record:
                    tier = rank_record.get("tier", "Rank C")
                    mmr = self.match_system.TIER_MMR.get(tier, 600)
                    print(f"Using tier-based MMR for {player.get('name', 'Unknown')}: {mmr} (Tier: {tier})")
                    total_mmr += mmr
                    player_count += 1
                else:
                    # Default MMR for players with no data
                    mmr = 600
                    print(f"Using default MMR for {player.get('name', 'Unknown')}: {mmr}")
                    total_mmr += mmr
                    player_count += 1

        # Return average MMR rounded to nearest integer
        avg_mmr = round(total_mmr / player_count) if player_count > 0 else 0
        print(f"Team average MMR: {avg_mmr} (Total: {total_mmr}, Players: {player_count})")
        return avg_mmr

    async def wait_for_captain_response(self, captain, timeout):
        """Wait for a captain to respond to a DM"""
        try:
            def check(m):
                return m.author == captain and m.guild is None

            return await self.bot.wait_for('message', check=check, timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def fallback_to_random(self, channel_id):
        """Fall back to random team selection if captain selection fails"""
        channel_id = str(channel_id)
        print(f"Starting fallback_to_random for channel_id: {channel_id}")

        # Get selection state for this channel
        if channel_id not in self.active_selections:
            print(f"No active selection found for channel_id: {channel_id}")
            return

        selection_state = self.active_selections[channel_id]

        # Get the announcement channel
        channel = selection_state.get('announcement_channel')
        if not channel:
            print("Cannot proceed with fallback: No announcement channel found")
            return  # Can't proceed without a channel to send messages to

        # Get all players
        all_players = selection_state['match_players']
        print(f"Fallback: Got {len(all_players)} players for random team assignment")

        # Create random teams
        random.shuffle(all_players)
        team1 = all_players[:3]
        team2 = all_players[3:6]

        # Format team mentions
        team1_mentions = [player['mention'] for player in team1]
        team2_mentions = [player['mention'] for player in team2]

        # Calculate team average MMRs
        team1_mmr = self.calculate_team_mmr(team1)
        team2_mmr = self.calculate_team_mmr(team2)

        # Determine if this is a global match
        is_global = channel.name.lower() == "global"
        print(f"Fallback: Channel {channel.name}, is_global: {is_global}")

        # Create match record with explicit is_global flag
        try:
            # CRITICAL: Cancel any existing match for these players
            for player in all_players:
                player_id = player["id"]
                if not player_id.startswith('9000'):  # Skip dummy players
                    self.match_system.matches.update_many(
                        {
                            "players.id": player_id,
                            "status": {"$in": ["voting", "selection"]}
                        },
                        {
                            "$set": {"status": "cancelled"}
                        }
                    )

            # Now create the new match
            match_id = self.match_system.create_match(
                str(uuid.uuid4()),
                team1,
                team2,
                channel_id,
                is_global=is_global
            )
            print(f"Fallback: Created match with ID {match_id}")

            # Double-check the status is set to in_progress
            self.match_system.matches.update_one(
                {"match_id": match_id},
                {"$set": {"status": "in_progress"}}
            )
        except Exception as e:
            print(f"Error creating match in fallback: {str(e)}")
            # Even if match creation fails, still try to send a message to the channel
            await channel.send(f"❌ Error creating match: {str(e)}")
            self.cancel_selection(channel_id)
            return

        # Create an embed for team announcement
        embed = discord.Embed(
            title="Teams Assigned Randomly!",
            color=0xe74c3c
        )

        embed.add_field(name="Match ID", value=f"`{match_id}`", inline=False)
        embed.add_field(name=f"Team 1 - Avg MMR: {team1_mmr}", value=", ".join(team1_mentions), inline=False)
        embed.add_field(name=f"Team 2 - Avg MMR: {team2_mmr}", value=", ".join(team2_mentions), inline=False)
        embed.add_field(
            name="Report Results",
            value=f"Play your match and report the result using `/report {match_id} win` or `/report {match_id} loss`",
            inline=False
        )

        # Send team announcement as embed
        try:
            await channel.send(embed=embed)
            print(f"Fallback: Sent team announcement embed to channel {channel.name}")
        except Exception as e:
            print(f"Error sending team announcement in fallback: {str(e)}")
            # Try sending a plain text message if embed fails
            try:
                await channel.send(f"Teams have been randomly assigned. Match ID: {match_id}")
            except:
                pass

        # Clean up
        try:
            print(f"Fallback: Cancelling selection for channel {channel_id}")
            self.cancel_selection(channel_id)
        except Exception as e:
            print(f"Error in cleanup during fallback: {str(e)}")
            # Try to cancel the selection anyway
            self.cancel_selection(channel_id)