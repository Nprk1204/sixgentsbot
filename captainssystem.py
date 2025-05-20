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

        # Track active selections by channel or match_id
        self.active_selections = {}  # Map of channel_id/match_id to selection state

    def set_match_system(self, match_system):
        """Set the match system reference"""
        self.match_system = match_system

    def set_bot(self, bot):
        """Set the bot instance"""
        self.bot = bot

    def is_selection_active(self, channel_id=None, match_id=None):
        """
        Check if captain selection is active

        Args:
            channel_id: Check for active selection in this channel
            match_id: Check for active selection with this match ID

        Returns:
            bool: Whether selection is active
        """
        if match_id:
            # Check if this match ID is in active selections
            return match_id in self.active_selections
        elif channel_id:
            # Check if this channel ID is in active selections
            return str(channel_id) in self.active_selections
        else:
            # Check if any selections are active
            return len(self.active_selections) > 0

    def cancel_selection(self, channel_id=None, match_id=None):
        """
        Cancel the current selection process

        Args:
            channel_id: Cancel selection in this specific channel
            match_id: Cancel selection for this specific match
        """
        if match_id:
            # Cancel selection for this match ID if it exists
            if match_id in self.active_selections:
                print(f"Canceling captain selection for match_id: {match_id}")
                del self.active_selections[match_id]
        elif channel_id:
            # Cancel selection for this channel ID if it exists
            if str(channel_id) in self.active_selections:
                print(f"Canceling captain selection for channel_id: {channel_id}")
                del self.active_selections[str(channel_id)]
        else:
            # Cancel all selections
            print("Canceling all captain selections")
            self.active_selections.clear()

    def start_captains_selection(self, players, channel_id, match_id=None):
        """Start the captains selection process for a specific match"""
        channel_id = str(channel_id)

        # If no match_id provided, try to find it from the active match
        if not match_id:
            active_match = self.match_system.matches.find_one({
                "channel_id": channel_id,
                "status": "selection"
            })

            if active_match:
                match_id = active_match.get("match_id")

        # If we found a match_id, validate its format
        if match_id and len(match_id) != 6:
            print(f"WARNING: Non-standard match ID format in start_captains_selection: {match_id}")
            original_id = match_id
            match_id = str(uuid.uuid4().hex)[:6]  # Generate new 6-character ID
            print(f"Generated new standard match ID: {match_id} (was: {original_id})")

            # Update the match record if it exists
            if active_match:
                self.match_system.matches.update_one(
                    {"_id": active_match["_id"]},
                    {"$set": {"match_id": match_id}}
                )
                print(f"Updated match ID in database: {original_id} -> {match_id}")

        if len(players) < 6:
            return "Not enough players to start captain selection!"

        # Choose two random players as captains
        random.shuffle(players)
        captain1 = players[0]
        captain2 = players[1]
        remaining_players = players[2:]

        # IMPORTANT FIX: Store selection state with BOTH match_id AND channel_id as keys
        # Determine primary selection key (prioritize match_id if available)
        selection_key = match_id if match_id else channel_id

        # Initialize selection state for this match
        self.active_selections[selection_key] = {
            'captain1': captain1,
            'captain2': captain2,
            'remaining_players': remaining_players,
            'captain1_team': [captain1],
            'captain2_team': [captain2],
            'match_players': players,
            'match_id': match_id,  # Store the match ID
            'channel_id': channel_id,  # Also store channel ID for cross-referencing
            'announcement_channel': None
        }

        # CRITICAL FIX: ALSO store the selection state with channel_id as key if match_id exists
        # This creates a second reference to the same selection state
        if match_id and channel_id != match_id:
            self.active_selections[channel_id] = self.active_selections[selection_key]
            print(f"Stored captain selection state with both keys: match_id={match_id} and channel_id={channel_id}")

        # Format remaining players for display
        remaining_mentions = [p['mention'] for p in remaining_players]

        # Create an embed instead of plain text
        embed = discord.Embed(
            title=f"Match Setup: Captains Mode - Match {match_id}",
            color=0xf1c40f  # Warm yellow color
        )

        embed.add_field(name="Captain 1", value=captain1['mention'], inline=True)
        embed.add_field(name="Captain 2", value=captain2['mention'], inline=True)
        embed.add_field(name="Available Players", value=", ".join(remaining_mentions), inline=False)
        embed.set_footer(text="Captains will be contacted via DM to make their selections.")

        return embed

    async def execute_captain_selection(self, channel, match_id=None):
        """Execute captain selection for a specific channel"""
        channel_id = str(channel.id)
        is_global = channel.name.lower() == "global"

        print(f"Captain selection for channel: {channel.name}, is_global: {is_global}")

        # First check if we have a match_id and if there's an active selection for it
        if match_id and match_id in self.active_selections:
            selection_state = self.active_selections[match_id]
            print(f"Found selection state using match_id: {match_id}")
        # If not, try to find by channel_id
        elif channel_id in self.active_selections:
            selection_state = self.active_selections[channel_id]
            print(f"Found selection state using channel_id: {channel_id}")
            # Get match_id from selection state if it exists
            match_id = selection_state.get('match_id')
            print(f"Retrieved match_id from selection state: {match_id}")
        else:
            # No active selection found by either match_id or channel_id
            print(f"No active selection found for channel {channel_id} or match_id {match_id}")
            return

        # Look up the active match in the database - first try by match_id if available
        active_match = None
        if match_id:
            active_match = self.match_system.matches.find_one({
                "match_id": match_id,
                "status": "selection"
            })
            print(f"Looking up match by match_id {match_id}: {'Found' if active_match else 'Not found'}")

        # If not found by match_id, fall back to channel_id
        if not active_match:
            active_match = self.match_system.matches.find_one({
                "channel_id": channel_id,
                "status": "selection"
            })
            print(f"Looking up match by channel_id {channel_id}: {'Found' if active_match else 'Not found'}")

            # If found by channel_id but we have a different match_id in selection state,
            # update our selection state to use the database match_id for consistency
            if active_match and match_id and active_match.get('match_id') != match_id:
                old_match_id = match_id
                match_id = active_match.get('match_id')
                print(f"WARNING: Mismatched match IDs. Database: {match_id}, Selection state: {old_match_id}")
                selection_state['match_id'] = match_id

        if not active_match:
            print(f"No active match found for channel {channel_id} or match_id {match_id}")
            await channel.send("⚠️ Error: The captain selection process has been cancelled.")
            # IMPORTANT: Make sure to call fallback_to_random here
            await self.fallback_to_random(channel_id, match_id=match_id)
            return

        # Use player list from the active match for consistency
        selection_state['match_players'] = active_match.get("players", [])

        # Make sure match_id is stored in selection state
        if active_match and 'match_id' in active_match and not match_id:
            match_id = active_match.get('match_id')
            selection_state['match_id'] = match_id
            print(f"Updated selection state with match_id {match_id} from database")

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
                await self.fallback_to_random(channel_id, match_id=match_id)
                return

            # Get discord users from IDs
            try:
                captain1_user = await self.bot.fetch_user(int(captain1['id']))
                captain2_user = await self.bot.fetch_user(int(captain2['id']))
            except (ValueError, discord.NotFound, discord.HTTPException) as e:
                await channel.send(f"Error fetching captain users: {str(e)}. Falling back to random team selection.")
                await self.fallback_to_random(channel_id, match_id=match_id)
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

            # Create or update the match using the match_id from selection_state
            # This ensures we use the SAME match_id throughout the process
            if match_id:
                match_id = self.match_system.create_match(
                    match_id,  # Use the existing match_id
                    captain1_team,
                    captain2_team,
                    channel_id,
                    is_global=is_global
                )
            else:
                # No match_id available, create a new one (should rarely happen)
                match_id = self.match_system.create_match(
                    str(uuid.uuid4().hex)[:6],  # Generate a 6-character ID
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
            if match_id:
                self.cancel_selection(match_id=match_id)

        except Exception as e:
            import traceback
            print(f"Error in captain selection: {e}")
            traceback.print_exc()
            # Something went wrong
            await channel.send(
                f"❌ An error occurred during captain selection: {str(e)}. Falling back to random team selection.")
            await self.fallback_to_random(channel_id, match_id=match_id)

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

    async def fallback_to_random(self, channel_id, match_id=None):
        """Fall back to random team selection if captain selection fails"""
        channel_id = str(channel_id)
        print(f"===== STARTING FALLBACK TO RANDOM =====")
        print(f"Channel ID: {channel_id}")
        print(f"Match ID: {match_id}")

        # Get the channel object directly from the bot
        channel = None
        if self.bot:
            try:
                channel = self.bot.get_channel(int(channel_id))
                print(f"Retrieved channel from bot: {channel.name if channel else 'Not found'}")
            except Exception as e:
                print(f"Error getting channel from bot: {e}")

        if not channel:
            print(f"Cannot proceed with fallback: No channel found for ID {channel_id}")
            return

        # Determine if this is a global match
        is_global = channel.name.lower() == "global"
        print(f"Fallback: Channel {channel.name}, is_global: {is_global}")

        # Get players from active selections or via direct database query
        players = []
        active_match = None

        # First check for players in the active selections
        if channel_id in self.active_selections:
            selection_state = self.active_selections[channel_id]
            if 'match_players' in selection_state:
                players = selection_state['match_players']
                print(f"Found {len(players)} players in active selection state")
                # Also get the match_id from selection_state if not provided
                if not match_id and 'match_id' in selection_state:
                    match_id = selection_state['match_id']
                    print(f"Using match_id {match_id} from selection state")

        # If no players found in selection state or no match_id yet, try from database
        if not players or not match_id:
            # IMPORTANT: First try looking for match by match_id if we have one
            if match_id:
                active_match = self.match_system.matches.find_one({
                    "match_id": match_id,
                    "status": {"$in": ["voting", "selection", "in_progress"]}
                })
                if active_match:
                    players = active_match.get("players", [])
                    print(f"Found {len(players)} players in database match with ID: {match_id}")

            # If still no match or players, try by channel_id
            if not active_match:
                active_match = self.match_system.matches.find_one({
                    "channel_id": channel_id,
                    "status": {"$in": ["voting", "selection", "in_progress"]}
                })

                if active_match:
                    players = active_match.get("players", [])
                    # If we found a match but no match_id was provided, get it from the match
                    if not match_id:
                        match_id = active_match.get("match_id")
                        print(f"Found active match by channel, using match_id: {match_id}")

        # If still no players, try checking the queue
        if not players and hasattr(self.queue, 'get_players_for_match'):
            try:
                queue_players = self.queue.get_players_for_match(channel_id)
                if queue_players and len(queue_players) >= 6:
                    players = queue_players
                    print(f"Found {len(players)} players in queue")
            except Exception as e:
                print(f"Error getting players from queue: {e}")

        # Final check if we have enough players
        if not players or len(players) < 6:
            print(f"Cannot proceed with fallback: Not enough players ({len(players) if players else 0}) found")
            await channel.send("⚠️ Error: Cannot create random teams - not enough players found.")
            return

        # Only take the first 6 players if we somehow get more
        if len(players) > 6:
            players = players[:6]
            print(f"Limiting to 6 players from the {len(players)} found")

        # Tell users what's happening
        await channel.send("Captain selection failed. Creating balanced random teams instead...")

        # Calculate team average MMRs
        player_mmrs = []
        for player in players:
            player_id = player["id"]

            # Check for dummy players with stored MMR
            if player_id.startswith('9000') and "dummy_mmr" in player:
                mmr = player["dummy_mmr"]
                player_mmrs.append((player, mmr))
                continue

            # Skip dummy players without MMR
            if player_id.startswith('9000'):
                # Generate a random MMR based on channel
                channel_name = channel.name.lower()
                if channel_name == "rank-a":
                    mmr = random.randint(1600, 2100)
                elif channel_name == "rank-b":
                    mmr = random.randint(1100, 1599)
                else:  # rank-c or global
                    mmr = random.randint(600, 1099)
                player_mmrs.append((player, mmr))
                continue

            # Get player data for real players
            player_data = self.match_system.players.find_one({"id": player_id})
            if player_data:
                # Use global or ranked MMR based on match type
                if is_global:
                    mmr = player_data.get("global_mmr", 300)
                else:
                    mmr = player_data.get("mmr", 600)
                player_mmrs.append((player, mmr))
            else:
                # For new players, check rank record
                rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                if rank_record:
                    if is_global:
                        mmr = rank_record.get("global_mmr", 300)
                    else:
                        tier = rank_record.get("tier", "Rank C")
                        mmr = self.match_system.TIER_MMR.get(tier, 600)
                    player_mmrs.append((player, mmr))
                else:
                    # Use default MMR
                    if is_global:
                        player_mmrs.append((player, 300))
                    else:
                        player_mmrs.append((player, 600))

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
                    player, mmr = player_mmrs.pop(0)
                    team1.append(player)
                    team1_mmr += mmr
                else:
                    player, mmr = player_mmrs.pop(0)
                    team2.append(player)
                    team2_mmr += mmr

            # Get lowest MMR player
            if player_mmrs:
                if team1_mmr <= team2_mmr:
                    player, mmr = player_mmrs.pop(-1)
                    team1.append(player)
                    team1_mmr += mmr
                else:
                    player, mmr = player_mmrs.pop(-1)
                    team2.append(player)
                    team2_mmr += mmr

        # Format team mentions
        team1_mentions = [player['mention'] for player in team1]
        team2_mentions = [player['mention'] for player in team2]

        # Calculate average MMR per team for display
        team1_avg_mmr = round(team1_mmr / len(team1), 1) if team1 else 0
        team2_avg_mmr = round(team2_mmr / len(team2), 1) if team2 else 0

        # Create match record with explicit is_global flag
        try:
            # If we have a match_id already, update that match
            if match_id:
                print(f"Using existing match_id: {match_id}")
                # Check if match_id is the correct format
                if len(match_id) != 6:
                    original_id = match_id
                    match_id = str(uuid.uuid4().hex)[:6]  # Generate a new 6-character ID
                    print(f"WARNING: Non-standard match ID format: {original_id}. Using new ID: {match_id}")

                # Cancel any existing matches with this ID
                self.match_system.matches.update_one(
                    {"match_id": match_id},
                    {"$set": {"status": "cancelled"}}
                )
                print(f"Cancelled any existing matches with ID {match_id}")

                # Create a new match with this ID
                self.match_system.create_match(
                    match_id,  # Use the existing ID
                    team1,
                    team2,
                    channel_id,
                    is_global=is_global
                )
            else:
                # No match ID provided, create a new match with a 6-character ID
                match_id = str(uuid.uuid4().hex)[:6]  # Generate a 6-character ID
                print(f"Creating new match with 6-character ID: {match_id}")

                self.match_system.create_match(
                    match_id,
                    team1,
                    team2,
                    channel_id,
                    is_global=is_global
                )

            # Double-check the status is set to in_progress
            self.match_system.matches.update_one(
                {"match_id": match_id},
                {"$set": {"status": "in_progress"}}
            )
            print(f"Set match {match_id} to status 'in_progress'")

            match_check = self.match_system.matches.find_one({"match_id": match_id})
            if match_check:
                print(f"Match {match_id} status after update: {match_check.get('status', 'unknown')}")
            else:
                print(f"WARNING: Could not find match {match_id} in database after creation!")

            # Make sure players are removed from the queue
            try:
                self.queue.remove_players_from_queue(players, channel_id)
                print(f"Removed players from queue in channel {channel_id}")
            except Exception as e:
                print(f"Error removing players from queue: {e}")

        except Exception as e:
            print(f"Error creating match in fallback: {str(e)}")
            # Even if match creation fails, still try to send a message to the channel
            await channel.send(f"❌ Error creating match: {str(e)}")
            return

        # Create an embed for team announcement
        embed = discord.Embed(
            title="Teams Assigned Randomly!",
            color=0xe74c3c
        )

        embed.add_field(name="Match ID", value=f"`{match_id}`", inline=False)
        embed.add_field(name=f"Team 1 - Avg MMR: {team1_avg_mmr}", value=", ".join(team1_mentions), inline=False)
        embed.add_field(name=f"Team 2 - Avg MMR: {team2_avg_mmr}", value=", ".join(team2_mentions), inline=False)
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
            except Exception as e2:
                print(f"Could not send even a plain text message: {e2}")

        # Make sure to cancel selection again at the end, just to be safe
        if channel_id in self.active_selections:
            self.cancel_selection(channel_id)

        print("===== FALLBACK TO RANDOM COMPLETED =====")