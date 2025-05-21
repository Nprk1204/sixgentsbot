import discord
import requests
from discord import app_commands
from discord.ext import commands
import logging
import datetime
import os
import asyncio
from threading import Thread
from flask import Flask
from dotenv import load_dotenv
from database import Database
from system_coordinator import SystemCoordinator
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
import uuid
import random

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')

# Set up logging
handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.reactions = True  # Make sure reaction intents are enabled

bot = commands.Bot(command_prefix='/', intents=intents)
bot.remove_command('help')  # Remove default help command

# Create a minimal Flask app for keepalive purposes
keepalive_app = Flask(__name__)


@keepalive_app.route('/')
def bot_status():
    return "Discord bot is running! For the leaderboard, please visit the main leaderboard site."


def run_keepalive_server():
    port = int(os.environ.get("PORT", 8080))
    keepalive_app.run(host='0.0.0.0', port=port)


def start_keepalive_server():
    server_thread = Thread(target=run_keepalive_server)
    server_thread.daemon = True  # This ensures the thread will close when the main program exits
    server_thread.start()
    print("Keepalive web server started on port", os.environ.get("PORT", 8080))


# Simple context class to help with command processing
class SimpleContext:
    def __init__(self, interaction):
        self.interaction = interaction
        self.author = interaction.user
        self.guild = interaction.guild
        self.channel = interaction.channel
        self.message = SimpleMessage(interaction)
        self.command = SimpleCommand(interaction.command)
        self.responded = False

    async def send(self, content=None, embed=None, view=None, ephemeral=False):
        if self.responded:
            return await self.interaction.followup.send(content=content, embed=embed, view=view, ephemeral=ephemeral)
        else:
            await self.interaction.response.send_message(content=content, embed=embed, view=view, ephemeral=ephemeral)
            self.responded = True


class SimpleMessage:
    def __init__(self, interaction):
        self.id = interaction.id
        self.created_at = interaction.created_at
        self.mentions = []
        if hasattr(interaction, 'data') and hasattr(interaction.data, 'resolved') and hasattr(interaction.data.resolved,
                                                                                              'users'):
            self.mentions = list(interaction.data.resolved.users.values())


class SimpleCommand:
    def __init__(self, command):
        self.name = command.name if command else "unknown"


# Track recent commands to prevent duplicates
recent_commands = {}
command_lock = asyncio.Lock()


async def is_duplicate_command(ctx):
    """Thread-safe check if a command is a duplicate"""
    user_id = ctx.author.id
    command_name = ctx.command.name if ctx.command else "unknown"
    channel_id = ctx.channel.id
    message_id = ctx.message.id
    timestamp = ctx.message.created_at.timestamp()

    print(f"Command received: {command_name} from {ctx.author.name} (ID: {message_id})")

    key = f"{user_id}:{command_name}:{channel_id}:{message_id}"

    async with command_lock:
        if key in recent_commands:
            print(f"DUPLICATE FOUND: {command_name} from {ctx.author.name} in {ctx.channel.name} (ID: {message_id})")
            return True

        # Update BEFORE continuing to prevent race conditions
        recent_commands[key] = timestamp
        print(f"Command registered: {command_name} (ID: {message_id})")

        # Keep dict size manageable
        if len(recent_commands) > 100:
            now = datetime.datetime.now(datetime.UTC).timestamp()
            # Only keep commands from last 5 minutes
            old_size = len(recent_commands)
            current_records = recent_commands.copy()
            recent_commands.clear()
            recent_commands.update({k: v for k, v in current_records.items() if now - v < 300})
            print(f"Cleaned command cache: {old_size} → {len(recent_commands)} entries")

    return False


# Helper function to check if command is used in a queue-specific channel
def is_queue_channel(channel):
    """Check if the command is being used in a queue-allowed channel"""
    allowed_channels = ["rank-a", "rank-b", "rank-c", "global"]
    return channel.name.lower() in allowed_channels


# Helper function to check if command is used in a general command channel
def is_command_channel(channel):
    """Check if the command is being used in a general command channel"""
    allowed_channels = ["rank-a", "rank-b", "rank-c", "global", "sixgents"]
    return channel.name.lower() in allowed_channels


# Database setup
client = MongoClient(MONGO_URI, server_api=ServerApi('1'))
try:
    # Test the connection
    client.admin.command('ping')
    print("MongoDB connection successful!")
except Exception as e:
    print(f"MongoDB connection error: {e}")

# Initialize components
db = Database(MONGO_URI)

# Create the system coordinator which will manage all systems
system_coordinator = SystemCoordinator(db)


@bot.event
async def on_ready():
    print(f"{bot.user.name} is now online with ID: {bot.user.id}")
    print(f"Connected to {len(bot.guilds)} guilds")

    # Set bot in system coordinator
    system_coordinator.set_bot(bot)

    # Start background task to check for ready matches
    bot.loop.create_task(system_coordinator.check_for_ready_matches())

    print(f"BOT INSTANCE ACTIVE - {datetime.datetime.now(datetime.UTC)}")

    # Sync command tree with Discord
    await bot.tree.sync()
    print("Successfully synced application commands")


@bot.event
async def on_reaction_add(reaction, user):
    """Handle reactions for voting"""
    if user.bot:
        return  # Ignore bot reactions

    # Pass to system coordinator to handle
    await system_coordinator.handle_reaction(reaction, user)


# Queue commands
@bot.tree.command(name="queue", description="Join the queue for 6 mans")
async def queue_slash(interaction: discord.Interaction):
    # Check if command is used in an allowed channel
    if not is_queue_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, or global channels.",
            ephemeral=True
        )
        return

    # Check if the player has completed rank verification
    player = interaction.user
    player_id = str(player.id)

    # Get all rank roles
    rank_a_role = discord.utils.get(interaction.guild.roles, name="Rank A")
    rank_b_role = discord.utils.get(interaction.guild.roles, name="Rank B")
    rank_c_role = discord.utils.get(interaction.guild.roles, name="Rank C")
    has_rank_role = any(role in player.roles for role in [rank_a_role, rank_b_role, rank_c_role])

    # Check if player has a rank entry in the database OR has a rank role
    rank_record = db.get_collection('ranks').find_one({"discord_id": player_id})

    # Allow joining if either database record exists OR player has a rank role
    if not (rank_record or has_rank_role):
        # Player hasn't completed rank verification either way
        embed = discord.Embed(
            title="Rank Verification Required",
            description="You need to verify your Rocket League rank before joining the queue.",
            color=0xf1c40f
        )
        embed.add_field(
            name="How to Verify",
            value="Visit the rank check page on the website to complete verification.",
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # If player has role but no database record, create one based on their role
    if has_rank_role and not rank_record:
        print(f"Player {player.display_name} has rank role but no database record. Creating one.")

        # Determine which role they have
        tier = None
        if rank_a_role in player.roles:
            tier = "Rank A"
            mmr = 1600
        elif rank_b_role in player.roles:
            tier = "Rank B"
            mmr = 1100
        elif rank_c_role in player.roles:
            tier = "Rank C"
            mmr = 600

        # Create a record in the database
        db.get_collection('ranks').insert_one({
            "discord_id": player_id,
            "discord_username": player.display_name,
            "tier": tier,
            "mmr": mmr,
            "timestamp": datetime.datetime.utcnow()
        })
        print(f"Created rank record for {player.display_name} with tier {tier}")

    # Use the queue manager to add player
    response = await system_coordinator.queue_manager.add_player(player, interaction.channel)
    await interaction.response.send_message(response)


@bot.tree.command(name="leave", description="Leave the queue")
async def leave_slash(interaction: discord.Interaction):
    # Check if command is used in an allowed channel
    if not is_queue_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, or global channels.",
            ephemeral=True
        )
        return

    # Use the queue manager to remove player
    response = await system_coordinator.queue_manager.remove_player(interaction.user, interaction.channel)
    await interaction.response.send_message(response)


@bot.tree.command(name="status", description="Shows the current queue status")
async def status_slash(interaction: discord.Interaction):
    # Check if command is used in an allowed channel
    if not is_queue_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, or global channels.",
            ephemeral=True
        )
        return

    # Use the queue manager to get status
    status_data = system_coordinator.queue_manager.get_queue_status(interaction.channel)

    # Create an embed
    embed = discord.Embed(
        title="Queue Status",
        description=f"**Current Queue: {status_data['queue_count']}/6 players**",
        color=0x3498db
    )

    if status_data['queue_count'] == 0:
        embed.add_field(name="Status", value="Queue is empty! Use `/queue` to join the queue.", inline=False)
        await interaction.response.send_message(embed=embed)
        return

    # Create a list of player mentions
    player_mentions = [player['mention'] for player in status_data['queue_players']]

    # Add player list to embed
    embed.add_field(name="Players", value=", ".join(player_mentions), inline=False)

    # Add info about how many more players are needed
    if status_data['queue_count'] < 6:
        more_needed = 6 - status_data['queue_count']
        embed.add_field(name="Info", value=f"{more_needed} more player(s) needed for a match.", inline=False)
    else:
        # Queue is full
        embed.add_field(name="Status", value="**Queue is FULL!** Ready to start match.", inline=False)

    # Add active matches if any - MODIFIED TO STAY WITHIN 25 FIELD LIMIT
    active_matches = status_data['active_matches']
    if active_matches:
        # First add a header field for active matches
        embed.add_field(name="Active Matches", value=f"{len(active_matches)} match(es) in progress", inline=False)

        # Calculate how many fields we can still add
        # We've already added at least 2 fields (Players + Info/Status)
        # We need to reserve 1 field for the active matches header
        # So we can add up to 22 more fields for match details
        fields_used = len(embed.fields)  # Current count of fields
        fields_available = 25 - fields_used

        # Limit the number of matches to display
        # Each match takes at least 1 field, but usually 3 (match + team1 + team2)
        # So limit to showing details for at most 7 matches (7*3 = 21 fields max)
        max_matches_to_display = min(7, len(active_matches))

        # Only display the first few active matches if there are too many
        for i, match in enumerate(active_matches[:max_matches_to_display]):
            match_id = match.get('match_id', 'Unknown')
            match_status = match.get('status', 'unknown')

            # Add a single field per match with compact info to save space
            match_info = f"ID: `{match_id}` | Status: {match_status}"

            # Add teams info if available (on separate lines)
            if 'team1' in match and 'team2' in match:
                team1_names = [p.get('name', 'Unknown') for p in match['team1']]
                team2_names = [p.get('name', 'Unknown') for p in match['team2']]

                # Limit team names if too many to display (to prevent field value exceeding 1024 chars)
                if len(team1_names) > 3:
                    team1_display = ', '.join(team1_names[:3]) + f" +{len(team1_names) - 3} more"
                else:
                    team1_display = ', '.join(team1_names)

                if len(team2_names) > 3:
                    team2_display = ', '.join(team2_names[:3]) + f" +{len(team2_names) - 3} more"
                else:
                    team2_display = ', '.join(team2_names)

                match_info += f"\nTeam 1: {team1_display}\nTeam 2: {team2_display}"

            embed.add_field(
                name=f"Match {i + 1}",
                value=match_info,
                inline=False
            )

        # If we had to limit the number of matches shown, add a note
        if len(active_matches) > max_matches_to_display:
            remaining = len(active_matches) - max_matches_to_display
            embed.add_field(
                name="Note",
                value=f"{remaining} more active match(es) not displayed. Use `/status` again to see more details.",
                inline=False
            )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="report", description="Report match results")
@app_commands.describe(
    match_id="The ID of the match you want to report",
    result="Your match result (win or loss)"
)
@app_commands.choices(result=[
    app_commands.Choice(name="Win", value="win"),
    app_commands.Choice(name="Loss", value="loss")
])
async def report_slash(interaction: discord.Interaction, match_id: str, result: str):
    # Create context for backward compatibility
    ctx = SimpleContext(interaction)

    # Check if command is used in an allowed channel
    if not is_command_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
            ephemeral=True
        )
        return

    reporter_id = str(interaction.user.id)

    # Validate result argument
    if result.lower() not in ["win", "loss"]:
        await interaction.response.send_message("Invalid result. Please use 'win' or 'loss'.", ephemeral=True)
        return

    # Start with a deferred response since match reporting might take time
    await interaction.response.defer()

    # Get match result
    match_result, error = await system_coordinator.match_system.report_match_by_id(match_id, reporter_id, result, ctx)

    if error:
        await interaction.followup.send(f"Error: {error}")
        return

    if not match_result:
        await interaction.followup.send("Failed to process match report.")
        return

    # Determine winning team
    winner = match_result["winner"]

    if winner == 1:
        winning_team = match_result["team1"]
        losing_team = match_result["team2"]
    else:
        winning_team = match_result["team2"]
        losing_team = match_result["team1"]

    # Initialize empty arrays for MMR changes
    winning_team_mmr_changes = ["?"] * len(winning_team)
    losing_team_mmr_changes = ["?"] * len(losing_team)

    # Extract MMR changes from the match result
    for change in match_result.get("mmr_changes", []):
        player_id = change.get("player_id")
        mmr_change = change.get("mmr_change", 0)
        is_win = change.get("is_win", False)

        # Find the player's index and set their MMR change
        if is_win:
            # This is a winner
            for i, player in enumerate(winning_team):
                if player.get("id") == player_id:
                    winning_team_mmr_changes[i] = f"+{mmr_change}"
                    break
        else:
            # This is a loser
            for i, player in enumerate(losing_team):
                if player.get("id") == player_id:
                    # MMR change is already negative for losers
                    losing_team_mmr_changes[i] = f"{mmr_change}"
                    break

    # Handle dummy players (they don't have MMR changes in the database)
    for i, player in enumerate(winning_team):
        if player.get("id", "").startswith('9000'):  # Dummy player
            winning_team_mmr_changes[i] = "+0"

    for i, player in enumerate(losing_team):
        if player.get("id", "").startswith('9000'):  # Dummy player
            losing_team_mmr_changes[i] = "-0"

    # Check for any remaining unknown MMR changes
    for i in range(len(winning_team_mmr_changes)):
        if winning_team_mmr_changes[i] == "?":
            winning_team_mmr_changes[i] = "+??"

    for i in range(len(losing_team_mmr_changes)):
        if losing_team_mmr_changes[i] == "?":
            losing_team_mmr_changes[i] = "-??"

    # Create the embed with updated formatting
    embed = discord.Embed(
        title="Match Results",
        description=f"Match completed",
        color=0x00ff00  # Green color
    )

    # Match ID field
    embed.add_field(name="Match ID", value=f"`{match_id}`", inline=False)

    # Add Winners header
    embed.add_field(name="🏆 Winners", value="\u200b", inline=False)

    # Create individual fields for each winning player
    for i, player in enumerate(winning_team):
        try:
            member = await interaction.guild.fetch_member(int(player.get("id", 0)))
            name = member.display_name if member else player.get('name', 'Unknown')
        except:
            name = player.get("name", "Unknown")

        # Add a field for this player
        embed.add_field(
            name=f"**{name}**",
            value=f"{winning_team_mmr_changes[i]}",
            inline=True
        )

    # Spacer field if needed to ensure proper alignment (for 3-column layout)
    if len(winning_team) % 3 == 1:
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
    elif len(winning_team) % 3 == 2:
        embed.add_field(name="\u200b", value="\u200b", inline=True)

    # Add Losers header
    embed.add_field(name="😔 Losers", value="\u200b", inline=False)

    # Create individual fields for each losing player
    for i, player in enumerate(losing_team):
        try:
            member = await interaction.guild.fetch_member(int(player.get("id", 0)))
            name = member.display_name if member else player.get('name', 'Unknown')
        except:
            name = player.get("name", "Unknown")

        # Add a field for this player
        embed.add_field(
            name=f"**{name}**",
            value=f"{losing_team_mmr_changes[i]}",
            inline=True
        )

    # Spacer field if needed to ensure proper alignment (for 3-column layout)
    if len(losing_team) % 3 == 1:
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
    elif len(losing_team) % 3 == 2:
        embed.add_field(name="\u200b", value="\u200b", inline=True)

    # MMR System explanation
    embed.add_field(
        name="📊 MMR System",
        value="Dynamic MMR: Changes based on games played",
        inline=False
    )

    # Footer with reporter info
    embed.set_footer(text=f"Reported by {interaction.user.display_name}")

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="adminreport", description="Admin command to report match results")
@app_commands.describe(
    match_id="Match ID",
    team_number="The team number that won (1 or 2)",
    result="Must be 'win'"
)
@app_commands.choices(result=[
    app_commands.Choice(name="Win", value="win"),
])
async def adminreport_slash(interaction: discord.Interaction, match_id: str, team_number: int, result: str = "win"):
    # Check if command is used in an allowed channel
    if not is_command_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
            ephemeral=True
        )
        return

    # Check if user has admin permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need administrator permissions to use this command.",
                                                ephemeral=True)
        return

    # Validate team number
    if team_number not in [1, 2]:
        await interaction.response.send_message("Invalid team number. Please use 1 or 2.", ephemeral=True)
        return

    # Validate result argument
    if result.lower() != "win":
        await interaction.response.send_message("Invalid result. Please use 'win' to indicate the winning team.",
                                                ephemeral=True)
        return

    # Get the match by ID directly from active matches or completed matches
    active_match = system_coordinator.queue_manager.get_match_by_id(match_id)

    if not active_match:
        # Check in completed matches
        active_match = system_coordinator.match_system.matches.find_one({"match_id": match_id, "status": "in_progress"})
        if not active_match:
            await interaction.response.send_message(f"No active match found with ID `{match_id}`.", ephemeral=True)
            return

    match_id = active_match.get("match_id")

    # Determine winner and scores based on admin input
    if team_number == 1:
        team1_score = 1
        team2_score = 0
    else:
        team1_score = 0
        team2_score = 1

    # Update match data in the database
    system_coordinator.match_system.matches.update_one(
        {"match_id": match_id},
        {"$set": {
            "status": "completed",
            "winner": team_number,
            "score": {"team1": team1_score, "team2": team2_score},
            "completed_at": datetime.datetime.now(datetime.UTC),
            "reported_by": str(interaction.user.id)
        }}
    )

    # Remove from active matches
    if system_coordinator.queue_manager:
        system_coordinator.queue_manager.remove_match(match_id)

    # Determine winning and losing teams
    if team_number == 1:
        winning_team = active_match.get("team1", [])
        losing_team = active_match.get("team2", [])
    else:
        winning_team = active_match.get("team2", [])
        losing_team = active_match.get("team1", [])

    # Update MMR
    system_coordinator.match_system.update_player_mmr(winning_team, losing_team, match_id)

    # Format team members - using display_name instead of mentions
    winning_members = []
    for player in winning_team:
        try:
            player_id = player.get("id")
            if player_id and player_id.isdigit():
                member = await interaction.guild.fetch_member(int(player_id))
                winning_members.append(member.display_name if member else player.get("name", "Unknown"))
            else:
                winning_members.append(player.get("name", "Unknown"))
        except:
            winning_members.append(player.get("name", "Unknown"))

    losing_members = []
    for player in losing_team:
        try:
            player_id = player.get("id")
            if player_id and player_id.isdigit():
                member = await interaction.guild.fetch_member(int(player_id))
                losing_members.append(member.display_name if member else player.get("name", "Unknown"))
            else:
                losing_members.append(player.get("name", "Unknown"))
        except:
            losing_members.append(player.get("name", "Unknown"))

    # Create results embed
    embed = discord.Embed(
        title="Match Results (Admin Report)",
        description=f"Match completed",
        color=0x00ff00
    )

    embed.add_field(name="Match ID", value=f"`{match_id}`", inline=False)
    embed.add_field(name="Winners", value=", ".join(winning_members), inline=False)
    embed.add_field(name="Losers", value=", ".join(losing_members), inline=False)
    embed.add_field(name="MMR", value="+15 for winners, -12 for losers (approximate)", inline=False)
    embed.set_footer(text=f"Reported by admin: {interaction.user.display_name}")

    await interaction.response.send_message(embed=embed)

    # Also send a message encouraging people to check the leaderboard
    await interaction.channel.send("Check the updated leaderboard with `/leaderboard`!")


@bot.tree.command(name="leaderboard", description="Shows a link to the leaderboard website")
async def leaderboard_slash(interaction: discord.Interaction):
    # Check if command is used in an allowed channel
    if not is_command_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
            ephemeral=True
        )
        return

    # Replace this URL with your actual leaderboard website URL from Render
    leaderboard_url = "https://sixgentsbot-1.onrender.com"

    embed = discord.Embed(
        title="🏆 Rocket League 6 Mans Leaderboard 🏆",
        description="View the complete leaderboard with player rankings, MMR, and stats!",
        color=0x00aaff,
        url=leaderboard_url  # This makes the title clickable
    )

    embed.add_field(
        name="Click to View Leaderboard",
        value=f"[View Full Leaderboard]({leaderboard_url})",
        inline=False
    )

    embed.add_field(
        name="Features",
        value="• Player rankings\n• MMR tracking\n• Win/Loss records\n• Win percentages",
        inline=False
    )

    embed.set_footer(text="Updated after each match")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="rank", description="Check your rank and stats (or another member's)")
@app_commands.describe(member="The member whose rank you want to check (optional)")
async def rank_slash(interaction: discord.Interaction, member: discord.Member = None):
    # Check if command is used in an allowed channel
    if not is_command_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
            ephemeral=True
        )
        return

    if member is None:
        member = interaction.user

    player_id = str(member.id)
    player_data = system_coordinator.match_system.get_player_stats(player_id)

    # If no player data exists, create a placeholder profile based on their rank role
    if not player_data:
        # Check if player has a rank verification or role
        rank_record = db.get_collection('ranks').find_one({"discord_id": player_id})

        # Get rank roles
        rank_a_role = discord.utils.get(interaction.guild.roles, name="Rank A")
        rank_b_role = discord.utils.get(interaction.guild.roles, name="Rank B")
        rank_c_role = discord.utils.get(interaction.guild.roles, name="Rank C")

        # Determine tier and MMR based on roles if no rank record exists
        if not rank_record:
            if rank_a_role in member.roles:
                tier = "Rank A"
                mmr = 1850
            elif rank_b_role in member.roles:
                tier = "Rank B"
                mmr = 1350
            elif rank_c_role in member.roles:
                tier = "Rank C"
                mmr = 600
            else:
                # No role or verification found
                await interaction.response.send_message(
                    f"{member.mention} hasn't verified their rank yet. Please use the rank verification system to get started!",
                    ephemeral=True
                )
                return
        else:
            # Use data from rank record
            tier = rank_record.get("tier", "Rank C")
            mmr = rank_record.get("mmr", 600)

        # Create a temporary player_data object for display
        player_data = {
            "name": member.display_name,
            "mmr": mmr,
            "wins": 0,
            "losses": 0,
            "matches": 0,
            "tier": tier,
            "is_new": True  # Flag to indicate this is a new player
        }

    # Calculate stats - add global stats
    mmr = player_data.get("mmr", 0)
    global_mmr = player_data.get("global_mmr", 300)  # Default global MMR to 300
    wins = player_data.get("wins", 0)
    global_wins = player_data.get("global_wins", 0)
    losses = player_data.get("losses", 0)
    global_losses = player_data.get("global_losses", 0)
    matches = player_data.get("matches", 0)
    global_matches = player_data.get("global_matches", 0)
    is_new = player_data.get("is_new", False)

    # Calculate win rates
    win_rate = 0
    if matches > 0:
        win_rate = (wins / matches) * 100

    global_win_rate = 0
    if global_matches > 0:
        global_win_rate = (global_wins / global_matches) * 100

    # Get player's rank position only if they've played games
    rank_position = "Unranked"
    total_players = 0

    if not is_new and matches > 0:
        all_players = list(system_coordinator.match_system.players.find().sort("mmr", -1))
        total_players = len(all_players)

        # Get the position using the player's ID
        for i, p in enumerate(all_players):
            if p.get("id") == player_id:
                rank_position = i + 1
                break

    # Get global rank position if they've played global games
    global_rank_position = "Unranked"
    if global_matches > 0:
        global_players = list(
            system_coordinator.match_system.players.find({"global_matches": {"$gt": 0}}).sort("global_mmr", -1))
        # Get the position using the player's ID
        for i, p in enumerate(global_players):
            if p.get("id") == player_id:
                global_rank_position = i + 1
                break

    # Create embed
    embed = discord.Embed(
        title=f"Stats for {member.display_name}",
        color=member.color
    )

    # Determine player tier based on MMR
    tier = "Rank C"
    if mmr >= 1600:
        tier = "Rank A"
    elif mmr >= 1100:
        tier = "Rank B"

    tier_color = 0x12b51a  # Default color for Rank C (green)
    if tier == "Rank A":
        tier_color = 0xC41E3A  # Red color for Rank A
    elif tier == "Rank B":
        tier_color = 0x1E90FF  # Blue color for Rank B

    embed.color = tier_color

    # Add ranked section
    embed.add_field(name="__Ranked Stats__", value="", inline=False)
    embed.add_field(name="Rank", value=tier, inline=True)

    if matches > 0:
        embed.add_field(name="Leaderboard", value=f"#{rank_position} of {total_players}", inline=True)
    else:
        embed.add_field(name="Leaderboard", value="Unranked (0 games)", inline=True)

    embed.add_field(name="MMR", value=str(mmr), inline=True)
    embed.add_field(name="Win Rate", value=f"{win_rate:.1f}%" if matches > 0 else "N/A", inline=True)
    embed.add_field(name="Record", value=f"{wins}W - {losses}L", inline=True)
    embed.add_field(name="Matches", value=str(matches), inline=True)

    # Add global section
    embed.add_field(name="__Global Stats__", value="", inline=False)

    if global_matches > 0:
        embed.add_field(name="Global Rank", value=f"#{global_rank_position}", inline=True)
    else:
        embed.add_field(name="Global Rank", value="Unranked (0 games)", inline=True)

    embed.add_field(name="Global MMR", value=str(global_mmr), inline=True)
    embed.add_field(name="Win Rate", value=f"{global_win_rate:.1f}%" if global_matches > 0 else "N/A", inline=True)
    embed.add_field(name="Record", value=f"{global_wins}W - {global_losses}L", inline=True)
    embed.add_field(name="Matches", value=str(global_matches), inline=True)

    # Add note for new players
    if is_new:
        embed.set_footer(
            text="⭐ New player - this is your starting MMR based on rank verification. Play matches to earn your spot on the leaderboard!")
    else:
        embed.set_footer(text="Stats updated after each match")

    embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)

    # Send only once
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="clearqueue", description="Clear all players from the queue (Admin only)")
async def clearqueue_slash(interaction: discord.Interaction):
    # Check if command is used in an allowed channel
    if not is_queue_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, or global channels.",
            ephemeral=True
        )
        return

    # Check if user has admin permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need administrator permissions to use this command.",
                                                ephemeral=True)
        return

    # Get current queue status
    status_data = system_coordinator.queue_manager.get_queue_status(interaction.channel)
    queue_count = status_data['queue_count']

    # Clear players from this channel's queue
    channel_id = str(interaction.channel.id)
    system_coordinator.queue_manager.queue_collection.delete_many({"channel_id": channel_id})

    # Send confirmation
    if queue_count == 0:
        await interaction.response.send_message("Queue was already empty!")
    else:
        await interaction.response.send_message(f"✅ Queue cleared! Removed {queue_count} player(s) from the queue.")


@bot.tree.command(name="forcestart", description="Force start the team selection process (Admin only)")
async def forcestart_slash(interaction: discord.Interaction):
    # Check if command is used in an allowed channel
    if not is_queue_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, or global channels.",
            ephemeral=True
        )
        return

    # Check if user has admin permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need administrator permissions to use this command.",
                                                ephemeral=True)
        return

    channel_id = str(interaction.channel.id)

    # Check if there's already an active match in selection phase
    active_match = system_coordinator.queue_manager.get_match_by_channel(channel_id, status="voting") or \
                   system_coordinator.queue_manager.get_match_by_channel(channel_id, status="selection")

    if active_match:
        await interaction.response.send_message("A team selection is already in progress in this channel!")
        return

    # Get players from queue
    status_data = system_coordinator.queue_manager.get_queue_status(interaction.channel)
    queue_count = status_data['queue_count']

    if queue_count == 0:
        await interaction.response.send_message("Can't force start: Queue is empty!")
        return

    # If fewer than 6 players, prompt to add dummy players
    if queue_count < 6:
        await interaction.response.send_message(
            f"There are only {queue_count}/6 players in the queue. Would you like to add {6 - queue_count} dummy players to fill it?",
            ephemeral=True
        )
        return

    # Force start by creating a match and starting vote
    match_id = await system_coordinator.queue_manager.create_match(interaction.channel, interaction.user.mention)

    # Start the voting process for this match
    channel_name = interaction.channel.name.lower()
    if channel_name in system_coordinator.vote_systems:
        await system_coordinator.vote_systems[channel_name].start_vote(interaction.channel)
        await interaction.response.send_message("Force starting team selection!")
    else:
        await interaction.response.send_message("Error: No vote system found for this channel.")


@bot.tree.command(name="forcestop",
                  description="Force stop any active votes or selections and clear the queue (Admin only)")
async def forcestop_slash(interaction: discord.Interaction):
    # Check if command is used in an allowed channel
    if not is_command_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
            ephemeral=True
        )
        return

    # Check if user has admin permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need administrator permissions to use this command.",
                                                ephemeral=True)
        return

    # Get current players in queue
    channel_id = str(interaction.channel.id)
    queue_count = system_coordinator.queue_manager.get_queue_status(interaction.channel)['queue_count']

    # Cancel any active votes
    vote_active = system_coordinator.is_voting_active(channel_id)
    if vote_active:
        system_coordinator.cancel_voting(channel_id)

    # Cancel any active selections
    selection_active = system_coordinator.is_selection_active(channel_id)
    if selection_active:
        system_coordinator.cancel_selection(channel_id)

    # Clear the queue collection
    system_coordinator.queue_manager.queue_collection.delete_many({"channel_id": channel_id})

    # Create a response message
    embed = discord.Embed(
        title="⚠️ Force Stop Executed",
        color=0xff9900
    )

    # Add appropriate fields based on what was stopped
    if vote_active:
        embed.add_field(name="Vote Canceled", value="Team selection voting has been canceled.", inline=False)

    if selection_active:
        embed.add_field(name="Team Selection Canceled", value="Captain selection process has been canceled.",
                        inline=False)

    embed.add_field(name="Queue Cleared", value=f"Removed {queue_count} player(s) from the queue.", inline=False)
    embed.set_footer(text=f"Executed by {interaction.user.display_name}")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="purgechat", description="Clear chat messages")
@app_commands.describe(amount_to_delete="Number of messages to delete (1-100)")
async def purgechat_slash(interaction: discord.Interaction, amount_to_delete: int = 10):
    # Check if command is used in an allowed channel
    if not is_command_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
            ephemeral=True
        )
        return

    if interaction.user.guild_permissions.manage_messages:
        if 1 <= amount_to_delete <= 100:
            await interaction.response.defer(ephemeral=True)
            await interaction.channel.purge(limit=amount_to_delete)
            await interaction.followup.send(f"Cleared {amount_to_delete} messages.", ephemeral=True)
        else:
            await interaction.response.send_message("Please enter a number between 1 and 100", ephemeral=True)
    else:
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)


@bot.tree.command(name="ping", description="Check if the bot is connected")
async def ping_slash(interaction: discord.Interaction):
    # Check if command is used in an allowed channel
    if not is_command_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
            ephemeral=True
        )
        return

    await interaction.response.send_message("Pong! Bot is connected to Discord.")


@bot.tree.command(name="help", description="Shows command information")
@app_commands.describe(command_name="Get details about a specific command")
async def help_slash(interaction: discord.Interaction, command_name: str = None):
    # Check if command is used in an allowed channel
    if not is_command_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
            ephemeral=True
        )
        return

    # If a specific command is requested, show details for that command
    if command_name:
        # Look for the command in the slash commands
        for cmd in bot.tree.get_commands():
            if cmd.name == command_name:
                embed = discord.Embed(
                    title=f"Command: /{cmd.name}",
                    description=cmd.description or "No description available",
                    color=0x00ff00
                )

                # Add parameter info if available
                if hasattr(cmd, 'parameters') and cmd.parameters:
                    params_str = "\n".join([f"{p.name}: {p.description}" for p in cmd.parameters])
                    embed.add_field(name="Parameters", value=params_str, inline=False)

                await interaction.response.send_message(embed=embed)
                return

        await interaction.response.send_message(f"Command `{command_name}` not found.", ephemeral=True)
        return

    # Create an embed for the command list
    embed = discord.Embed(
        title="Rocket League 6 Mans Bot Commands",
        description="Use `/help <command>` for more details on a specific command",
        color=0x00ff00
    )

    # Define commands and descriptions
    commands_dict = {
        'adjustmmr': 'Admin command to adjust a player\'s MMR',
        'adminreport': 'Admin command to report match results',
        'clearqueue': 'Clear all players from the queue (Admin only)',
        'forcestart': 'Force start the team selection process (Admin only)',
        'forcestop': 'Force stop active votes/selections and clear the queue',
        'help': 'Display help information',
        'leaderboard': 'Shows a link to the leaderboard website',
        'queue': 'Join the queue for 6 mans',
        'leave': 'Leave the queue',
        'purgechat': 'Clear chat messages',
        'rank': 'Check your rank and stats (or another member\'s)',
        'removematch': 'Remove the results of a match (Admin only)',
        'report': 'Report match results',
        'resetleaderboard': 'Reset the leaderboard (Admin only)',
        'status': 'Shows the current queue status',
        'sub': 'Substitute players in an active match',
        'ping': 'Check if the bot is connected'
    }

    # Group commands by category
    queue_commands = ['queue', 'leave', 'status']
    match_commands = ['report', 'leaderboard', 'rank', 'sub']
    admin_commands = ['adjustmmr', 'adminreport', 'clearqueue', 'forcestart', 'forcestop', 'removematch',
                      'resetleaderboard', 'purgechat']
    utility_commands = ['help', 'ping']

    # Add command fields grouped by category
    embed.add_field(
        name="📋 Queue Commands",
        value="\n".join([f"`/{cmd}` - {commands_dict[cmd]}" for cmd in queue_commands]),
        inline=False
    )

    embed.add_field(
        name="🎮 Match Commands",
        value="\n".join([f"`/{cmd}` - {commands_dict[cmd]}" for cmd in match_commands]),
        inline=False
    )

    embed.add_field(
        name="🛠️ Admin Commands",
        value="\n".join([f"`/{cmd}` - {commands_dict[cmd]}" for cmd in admin_commands]),
        inline=False
    )

    embed.add_field(
        name="🔧 Utility Commands",
        value="\n".join([f"`/{cmd}` - {commands_dict[cmd]}" for cmd in utility_commands]),
        inline=False
    )

    # Add "How It Works" section
    embed.add_field(
        name="How 6 Mans Works:",
        value=(
            "1. Join the queue with `/queue` in a rank channel\n"
            "2. When 6 players join, voting starts automatically\n"
            "3. Vote by clicking on the team selection buttons\n"
            "4. Teams will be created based on the vote results\n"
            "5. After the match, report the results with `/report <match_id> win` or `/report <match_id> loss`\n"
            "6. Check the leaderboard with `/leaderboard`"
        ),
        inline=False
    )

    embed.set_footer(text="Type /help <command> for more info on a specific command")
    await interaction.response.send_message(embed=embed)


# Error handlers
@bot.event
async def on_error(event, *args, **kwargs):
    print(f"Error in event {event}: {args} {kwargs}")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.errors.CommandNotFound):
        await interaction.response.send_message("Command not found. Use `/help` to see available commands.",
                                                ephemeral=True)
    elif isinstance(error, app_commands.errors.MissingPermissions):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
    elif isinstance(error, app_commands.errors.MissingRequiredArgument):
        await interaction.response.send_message("Missing required argument. Use `/help` to see command usage.",
                                                ephemeral=True)
    else:
        print(f"Command error: {error}")
        try:
            await interaction.response.send_message(f"An error occurred: {error}", ephemeral=True)
        except:
            # If interaction was already responded to
            try:
                await interaction.followup.send(f"An error occurred: {error}", ephemeral=True)
            except:
                # Last resort
                print(f"Could not respond to interaction with error: {error}")


# Run the bot with the keepalive server
if __name__ == "__main__":
    # Start the keepalive server first
    start_keepalive_server()

    # Then run the bot
    bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)