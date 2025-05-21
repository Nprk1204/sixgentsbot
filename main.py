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

    # Get queue count and players
    queue_count = status_data['queue_count']
    queue_players = status_data['queue_players']

    # Create a simple embed that just shows queue status
    embed = discord.Embed(
        title="Queue Status",
        description=f"**Current Queue: {queue_count}/6 players**",
        color=0x3498db
    )

    if queue_count == 0:
        embed.add_field(name="Status", value="Queue is empty! Use `/queue` to join the queue.", inline=False)
    else:
        # Create a list of player mentions
        player_mentions = [player['mention'] for player in queue_players]

        # Add player list to embed
        embed.add_field(name="Players in Queue", value=", ".join(player_mentions), inline=False)

        # Add info about how many more players are needed
        if queue_count < 6:
            more_needed = 6 - queue_count
            embed.add_field(name="Info", value=f"{more_needed} more player(s) needed for a match.", inline=False)
        else:
            # Queue is full
            embed.add_field(name="Status", value="**Queue is FULL!** Ready to start match.", inline=False)

    # Simply note the number of active matches without details
    active_matches = status_data['active_matches']
    if active_matches:
        embed.add_field(
            name="Active Matches",
            value=f"There are currently {len(active_matches)} active match(es) in progress.",
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
    queue_players = status_data['queue_players']

    # If there are no players in the queue
    if queue_count == 0:
        await interaction.response.send_message("Queue is already empty!")
        return

    # Create an embed with the players being removed
    player_mentions = [player['mention'] for player in queue_players]

    embed = discord.Embed(
        title="Queue Cleared",
        description=f"Removed {queue_count} player(s) from the queue:",
        color=0xff9900  # Orange color
    )

    if player_mentions:
        embed.add_field(name="Players Removed", value=", ".join(player_mentions), inline=False)

    embed.set_footer(text=f"Cleared by {interaction.user.display_name}")

    # Clear players from this channel's queue
    channel_id = str(interaction.channel.id)
    system_coordinator.queue_manager.queue_collection.delete_many({"channel_id": channel_id})

    # Make sure to update the in-memory state too
    if channel_id in system_coordinator.queue_manager.channel_queues:
        system_coordinator.queue_manager.channel_queues[channel_id] = []

    # Send confirmation
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="forcestart",
                  description="Force start the team selection process with dummy players if needed (Admin only)")
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

    # Defer response as this might take some time
    await interaction.response.defer()

    # Get players from queue
    status_data = system_coordinator.queue_manager.get_queue_status(interaction.channel)
    queue_count = status_data['queue_count']
    queue_players = status_data['queue_players'].copy()  # Make a copy of the list to avoid modifying original

    # If queue is empty, we need to add 6 dummy players
    if queue_count == 0:
        await interaction.followup.send("Queue is empty. Adding 6 dummy players...")
        await add_dummy_players(interaction.channel, 6)
        # Update queue status after adding dummies
        status_data = system_coordinator.queue_manager.get_queue_status(interaction.channel)
        queue_count = status_data['queue_count']
        queue_players = status_data['queue_players'].copy()
    # If fewer than 6 players, add dummies to fill
    elif queue_count < 6:
        dummies_needed = 6 - queue_count
        await interaction.followup.send(
            f"Only {queue_count}/6 players in queue. Adding {dummies_needed} dummy players...")
        await add_dummy_players(interaction.channel, dummies_needed)
        # Update queue status after adding dummies
        status_data = system_coordinator.queue_manager.get_queue_status(interaction.channel)
        queue_count = status_data['queue_count']
        queue_players = status_data['queue_players'].copy()

    # Force start by creating a match and starting vote
    match_id = await system_coordinator.queue_manager.create_match(interaction.channel, interaction.user.mention)

    # Start the voting process for this match
    channel_name = interaction.channel.name.lower()
    if channel_name in system_coordinator.vote_systems:
        await system_coordinator.vote_systems[channel_name].start_vote(interaction.channel)
        await interaction.followup.send("Force starting team selection with the following players:")

        # Create an embed showing the players in the match
        embed = discord.Embed(
            title="Match Players",
            description=f"Match ID: `{match_id}`",
            color=0x3498db
        )

        # List players, indicating which ones are dummies
        player_list = []
        for player in queue_players[:6]:  # Take the first 6 players
            player_id = player.get('id', '')
            player_mention = player.get('mention', player.get('name', 'Unknown'))
            is_dummy = player_id.startswith('9000')
            player_list.append(f"{player_mention}{' [BOT]' if is_dummy else ''}")

        embed.add_field(
            name="Players",
            value="\n".join(player_list),
            inline=False
        )

        await interaction.channel.send(embed=embed)
    else:
        await interaction.followup.send("Error: No vote system found for this channel.")


async def add_dummy_players(channel, count):
    """Add dummy players to the queue"""
    channel_id = str(channel.id)
    is_global = channel.name.lower() == "global"

    # Determine MMR range based on channel
    if channel.name.lower() == "rank-a":
        mmr_range = (1600, 2100)
    elif channel.name.lower() == "rank-b":
        mmr_range = (1100, 1599)
    else:  # rank-c or global
        mmr_range = (600, 1099)

    # Create dummy players and add to queue
    for i in range(count):
        # Generate a unique dummy ID
        dummy_id = f"9000{i + 1}"

        # Generate a random MMR within the range for this channel
        dummy_mmr = random.randint(mmr_range[0], mmr_range[1])

        # Create dummy player data
        dummy_data = {
            "id": dummy_id,
            "name": f"TestDummy{i + 1}",
            "mention": f"TestDummy{i + 1}",
            "channel_id": channel_id,
            "is_global": is_global,
            "joined_at": datetime.datetime.utcnow(),
            "dummy_mmr": dummy_mmr  # Store MMR directly in player data
        }

        # Add to database
        system_coordinator.queue_manager.queue_collection.insert_one(dummy_data)

        # Add to in-memory queue
        if channel_id not in system_coordinator.queue_manager.channel_queues:
            system_coordinator.queue_manager.channel_queues[channel_id] = []

        system_coordinator.queue_manager.channel_queues[channel_id].append(dummy_data)


@bot.tree.command(name="removeactivematches", description="Remove all active matches in this channel (Admin only)")
async def removeactivematches_slash(interaction: discord.Interaction):
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

    channel_id = str(interaction.channel.id)

    # Find all active matches in this channel
    active_matches = []
    for match_id, match in system_coordinator.queue_manager.active_matches.items():
        if match.get('channel_id') == channel_id:
            active_matches.append(match)

    # If no active matches, inform the user
    if not active_matches:
        await interaction.response.send_message("No active matches found in this channel.")
        return

    # First, cancel any active votings or selections
    vote_active = system_coordinator.is_voting_active(channel_id)
    if vote_active:
        system_coordinator.cancel_voting(channel_id)

    selection_active = system_coordinator.is_selection_active(channel_id)
    if selection_active:
        system_coordinator.cancel_selection(channel_id)

    # Store match details for the embed
    removed_matches = []
    for match in active_matches:
        match_id = match.get('match_id')
        player_count = 0

        # Count players in teams if available
        team1 = match.get('team1', [])
        team2 = match.get('team2', [])
        if team1 or team2:
            player_count = len(team1) + len(team2)
        # Otherwise count players directly
        elif 'players' in match:
            player_count = len(match.get('players', []))

        removed_matches.append({
            'match_id': match_id,
            'player_count': player_count,
            'status': match.get('status', 'unknown')
        })

        # Remove the match
        system_coordinator.queue_manager.remove_match(match_id)

    # Create embed to display results
    embed = discord.Embed(
        title="Active Matches Removed",
        description=f"Removed {len(removed_matches)} active match(es) from this channel.",
        color=0xff5555  # Red color
    )

    for i, match in enumerate(removed_matches, 1):
        embed.add_field(
            name=f"Match {i}",
            value=f"ID: `{match['match_id']}`\nStatus: {match['status']}\nPlayers: {match['player_count']}",
            inline=True
        )

    embed.set_footer(text=f"Executed by {interaction.user.display_name}")

    # Send confirmation
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
        'clearqueue': 'Clear all players from the current queue (Admin only)',
        'forcestart': 'Force start a match with dummy players if needed (Admin only)',
        'removeactivematches': 'Remove all active matches in the current channel (Admin only)',
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
    admin_commands = ['adjustmmr', 'adminreport', 'clearqueue', 'forcestart', 'removeactivematches',
                     'removematch', 'resetleaderboard', 'purgechat']
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


# 1. Adjust MMR Command
@bot.tree.command(name="adjustmmr", description="Admin command to adjust a player's MMR")
@app_commands.describe(
    player="The player whose MMR you want to adjust",
    amount="The amount to adjust (positive or negative)",
    global_mmr="Whether to adjust global MMR instead of ranked MMR"
)
@app_commands.choices(global_mmr=[
    app_commands.Choice(name="Ranked MMR", value="false"),
    app_commands.Choice(name="Global MMR", value="true")
])
async def adjustmmr_slash(interaction: discord.Interaction, player: discord.Member, amount: int,
                          global_mmr: str = "false"):
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

    # Determine which MMR to adjust
    is_global = global_mmr.lower() == "true"
    mmr_type = "Global" if is_global else "Ranked"

    # Get player data
    player_id = str(player.id)
    player_data = system_coordinator.match_system.players.find_one({"id": player_id})

    # Handle player not found
    if not player_data:
        # Check for rank record as fallback
        rank_record = db.get_collection('ranks').find_one({"discord_id": player_id})

        if rank_record:
            # Create player entry with initial values
            if is_global:
                starting_mmr = rank_record.get("global_mmr", 300)
                new_mmr = starting_mmr + amount

                system_coordinator.match_system.players.insert_one({
                    "id": player_id,
                    "name": player.display_name,
                    "mmr": 600,  # Default ranked MMR
                    "global_mmr": new_mmr,
                    "wins": 0,
                    "global_wins": 0,
                    "losses": 0,
                    "global_losses": 0,
                    "matches": 0,
                    "global_matches": 0,
                    "created_at": datetime.datetime.utcnow(),
                    "last_updated": datetime.datetime.utcnow()
                })

                await interaction.response.send_message(
                    f"Created new player entry for {player.mention}. Adjusted {mmr_type} MMR from {starting_mmr} to {new_mmr} ({'+' if amount >= 0 else ''}{amount})."
                )
                return
            else:
                # For ranked MMR, use tier-based MMR
                tier = rank_record.get("tier", "Rank C")
                starting_mmr = system_coordinator.match_system.TIER_MMR.get(tier, 600)
                new_mmr = starting_mmr + amount

                system_coordinator.match_system.players.insert_one({
                    "id": player_id,
                    "name": player.display_name,
                    "mmr": new_mmr,
                    "global_mmr": 300,  # Default global MMR
                    "wins": 0,
                    "global_wins": 0,
                    "losses": 0,
                    "global_losses": 0,
                    "matches": 0,
                    "global_matches": 0,
                    "created_at": datetime.datetime.utcnow(),
                    "last_updated": datetime.datetime.utcnow()
                })

                await interaction.response.send_message(
                    f"Created new player entry for {player.mention}. Adjusted {mmr_type} MMR from {starting_mmr} to {new_mmr} ({'+' if amount >= 0 else ''}{amount})."
                )
                return
        else:
            await interaction.response.send_message(
                f"Player {player.mention} not found in the database and has no rank verification. They need to verify their rank first.",
                ephemeral=True
            )
            return

    # Update existing player
    if is_global:
        old_mmr = player_data.get("global_mmr", 300)
        new_mmr = old_mmr + amount

        system_coordinator.match_system.players.update_one(
            {"id": player_id},
            {"$set": {
                "global_mmr": new_mmr,
                "last_updated": datetime.datetime.utcnow()
            }}
        )
    else:
        old_mmr = player_data.get("mmr", 600)
        new_mmr = old_mmr + amount

        system_coordinator.match_system.players.update_one(
            {"id": player_id},
            {"$set": {
                "mmr": new_mmr,
                "last_updated": datetime.datetime.utcnow()
            }}
        )

    # Create embed response
    embed = discord.Embed(
        title=f"MMR Adjustment for {player.display_name}",
        color=0x00ff00 if amount >= 0 else 0xff0000
    )

    embed.add_field(
        name=f"{mmr_type} MMR Adjustment",
        value=f"**Old MMR:** {old_mmr}\n**New MMR:** {new_mmr}\n**Change:** {'+' if amount >= 0 else ''}{amount}",
        inline=False
    )

    # Add tier information if it's ranked MMR
    if not is_global:
        # Determine new tier based on MMR
        old_tier = "Rank C"
        if old_mmr >= 1600:
            old_tier = "Rank A"
        elif old_mmr >= 1100:
            old_tier = "Rank B"

        new_tier = "Rank C"
        if new_mmr >= 1600:
            new_tier = "Rank A"
        elif new_mmr >= 1100:
            new_tier = "Rank B"

        tier_changed = old_tier != new_tier

        embed.add_field(
            name="Rank Tier",
            value=f"**Old Tier:** {old_tier}\n**New Tier:** {new_tier}\n**Changed:** {'Yes' if tier_changed else 'No'}",
            inline=False
        )

        # Update Discord role if tier changed
        if tier_changed:
            embed.add_field(
                name="Discord Role",
                value="Discord role will be updated on the player's next match.",
                inline=False
            )

    embed.set_footer(
        text=f"Adjusted by {interaction.user.display_name} | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    await interaction.response.send_message(embed=embed)


# 2. Remove Match Command
@bot.tree.command(name="removematch", description="Remove the results of a match (Admin only)")
@app_commands.describe(
    match_id="The ID of the match to remove"
)
async def removematch_slash(interaction: discord.Interaction, match_id: str):
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

    # Defer response since this might take some time
    await interaction.response.defer()

    # Look up the match
    match = system_coordinator.match_system.matches.find_one({"match_id": match_id})

    if not match:
        await interaction.followup.send(f"Match with ID `{match_id}` not found.")
        return

    # Check if the match is completed
    if match.get("status") != "completed":
        await interaction.followup.send(f"Match with ID `{match_id}` is not completed and cannot be removed.")
        return

    # Store match details for confirmation
    winner = match.get("winner")
    team1 = match.get("team1", [])
    team2 = match.get("team2", [])
    is_global = match.get("is_global", False)
    mmr_changes = match.get("mmr_changes", [])

    if not mmr_changes:
        await interaction.followup.send(
            f"Match with ID `{match_id}` has no MMR changes recorded. Cannot safely remove it.")
        return

    # Reverse MMR changes for each player
    for change in mmr_changes:
        player_id = change.get("player_id")
        old_mmr = change.get("old_mmr")
        is_win = change.get("is_win")
        is_global_match = change.get("is_global", is_global)

        if not player_id or old_mmr is None:
            continue

        # Update player record
        player = system_coordinator.match_system.players.find_one({"id": player_id})

        if player:
            if is_global_match:
                # Update global stats
                system_coordinator.match_system.players.update_one(
                    {"id": player_id},
                    {"$set": {"global_mmr": old_mmr},
                     "$inc": {
                         "global_matches": -1,
                         "global_wins": -1 if is_win else 0,
                         "global_losses": 0 if is_win else -1
                     }}
                )
            else:
                # Update ranked stats
                system_coordinator.match_system.players.update_one(
                    {"id": player_id},
                    {"$set": {"mmr": old_mmr},
                     "$inc": {
                         "matches": -1,
                         "wins": -1 if is_win else 0,
                         "losses": 0 if is_win else -1
                     }}
                )

    # Remove the match from the database
    system_coordinator.match_system.matches.delete_one({"match_id": match_id})

    # Create embed response
    embed = discord.Embed(
        title="Match Removed",
        description=f"Match ID: `{match_id}`",
        color=0xff0000
    )

    # Format team names
    team1_names = [p.get("name", "Unknown") for p in team1]
    team2_names = [p.get("name", "Unknown") for p in team2]

    embed.add_field(
        name="Team 1" + (" (Winner)" if winner == 1 else ""),
        value=", ".join(team1_names) if team1_names else "Unknown",
        inline=False
    )

    embed.add_field(
        name="Team 2" + (" (Winner)" if winner == 2 else ""),
        value=", ".join(team2_names) if team2_names else "Unknown",
        inline=False
    )

    embed.add_field(
        name="Match Type",
        value="Global" if is_global else "Ranked",
        inline=True
    )

    embed.add_field(
        name="MMR Changes",
        value="All MMR changes have been reversed",
        inline=True
    )

    embed.set_footer(
        text=f"Removed by {interaction.user.display_name} | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    await interaction.followup.send(embed=embed)


# 3. Reset Leaderboard Command
@bot.tree.command(name="resetleaderboard", description="Reset the leaderboard (Admin only)")
@app_commands.describe(
    confirmation="Type 'CONFIRM' to confirm the reset",
    reset_type="Type of reset to perform"
)
@app_commands.choices(reset_type=[
    app_commands.Choice(name="Global Only", value="global"),
    app_commands.Choice(name="Ranked Only", value="ranked"),
    app_commands.Choice(name="Complete Reset", value="all")
])
async def resetleaderboard_slash(interaction: discord.Interaction, confirmation: str, reset_type: str = "all"):
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

    # Check confirmation
    if confirmation != "CONFIRM":
        await interaction.response.send_message(
            "❌ Leaderboard reset canceled. You must type 'CONFIRM' (all caps) to confirm this action.",
            ephemeral=True
        )
        return

    # Defer response as this operation could take time
    await interaction.response.defer()

    # Create backup collection name with timestamp
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_collection_name = f"players_backup_{timestamp}"

    # Create a backup of the current players collection
    backup_collection = db.get_collection(backup_collection_name)
    all_players = list(system_coordinator.match_system.players.find())

    if all_players:
        backup_collection.insert_many(all_players)

    # Initialize counters
    player_count = len(all_players)
    reset_count = 0

    if reset_type == "global":
        # Reset only global stats
        result = system_coordinator.match_system.players.update_many(
            {},
            {"$set": {
                "global_mmr": 300,
                "global_wins": 0,
                "global_losses": 0,
                "global_matches": 0
            }}
        )
        reset_count = result.modified_count

        # Reset global matches
        matches_result = system_coordinator.match_system.matches.delete_many({"is_global": True})
        matches_removed = matches_result.deleted_count

    elif reset_type == "ranked":
        # Reset only ranked stats
        for player in all_players:
            player_id = player.get("id")

            # Look up rank record for default MMR based on tier
            rank_record = db.get_collection('ranks').find_one({"discord_id": player_id})

            if rank_record:
                tier = rank_record.get("tier", "Rank C")
                starting_mmr = system_coordinator.match_system.TIER_MMR.get(tier, 600)
            else:
                starting_mmr = 600  # Default

            # Update with tier-based starting MMR
            system_coordinator.match_system.players.update_one(
                {"id": player_id},
                {"$set": {
                    "mmr": starting_mmr,
                    "wins": 0,
                    "losses": 0,
                    "matches": 0
                }}
            )
            reset_count += 1

        # Reset ranked matches
        matches_result = system_coordinator.match_system.matches.delete_many({"is_global": {"$ne": True}})
        matches_removed = matches_result.deleted_count

    else:  # "all" - Complete reset
        # COMPLETE RESET: Clear and reset everything including rank verifications

        # 1. Make backup of rank verification data
        ranks_collection = db.get_collection('ranks')
        all_ranks = list(ranks_collection.find())
        backup_ranks_collection = db.get_collection(f"ranks_backup_{timestamp}")
        if all_ranks:
            backup_ranks_collection.insert_many(all_ranks)

        # 2. Reset player stats - Set MMR to 0 for unverified players
        for player in all_players:
            player_id = player.get("id")

            # For complete resets, we reset to zero MMR until re-verification
            system_coordinator.match_system.players.update_one(
                {"id": player_id},
                {"$set": {
                    "mmr": 0,  # Reset to 0 until re-verification
                    "global_mmr": 0,
                    "wins": 0,
                    "global_wins": 0,
                    "losses": 0,
                    "global_losses": 0,
                    "matches": 0,
                    "global_matches": 0
                }}
            )
            reset_count += 1

            # 3. Remove Discord roles for all players
            try:
                member = await interaction.guild.fetch_member(int(player_id))
                if member:
                    # Get all rank roles
                    rank_a_role = discord.utils.get(interaction.guild.roles, name="Rank A")
                    rank_b_role = discord.utils.get(interaction.guild.roles, name="Rank B")
                    rank_c_role = discord.utils.get(interaction.guild.roles, name="Rank C")

                    # Remove all rank roles
                    roles_to_remove = []
                    if rank_a_role and rank_a_role in member.roles:
                        roles_to_remove.append(rank_a_role)
                    if rank_b_role and rank_b_role in member.roles:
                        roles_to_remove.append(rank_b_role)
                    if rank_c_role and rank_c_role in member.roles:
                        roles_to_remove.append(rank_c_role)

                    if roles_to_remove:
                        await member.remove_roles(*roles_to_remove, reason="Leaderboard reset")
            except Exception as e:
                print(f"Error removing roles for {player_id}: {e}")

        # 4. Delete all rank verification records to force re-verification
        ranks_removed = ranks_collection.delete_many({}).deleted_count

        # 5. Delete all matches
        matches_result = system_coordinator.match_system.matches.delete_many({})
        matches_removed = matches_result.deleted_count

    # Record the reset in the resets collection
    resets_collection = db.get_collection('resets')
    resets_collection.insert_one({
        "type": "leaderboard_reset",
        "reset_type": reset_type,
        "timestamp": datetime.datetime.utcnow(),
        "admin_id": str(interaction.user.id),
        "admin_name": interaction.user.display_name,
        "backup_collection": backup_collection_name
    })

    # Send confirmation
    embed = discord.Embed(
        title="🔄 Leaderboard Reset Complete",
        description=f"Reset type: **{reset_type.upper()}**",
        color=0xff9900  # Orange color
    )

    embed.add_field(
        name="Stats",
        value=f"Players affected: {reset_count}/{player_count}\nMatches removed: {matches_removed}",
        inline=False
    )

    embed.add_field(
        name="Backup Created",
        value=f"Collection: `{backup_collection_name}`",
        inline=False
    )

    if reset_type == "all":
        # For complete reset, mention rank verification
        embed.add_field(
            name="Rank Verification Reset",
            value=f"**{ranks_removed}** rank verifications have been removed. All players must re-verify their ranks.",
            inline=False
        )

        embed.add_field(
            name="Discord Roles",
            value="All Rank A, B, and C roles have been removed from members. Roles will be reassigned during re-verification.",
            inline=False
        )
    elif reset_type in ["ranked", "all"]:
        embed.add_field(
            name="MMR Reset",
            value="All players have been reset to their rank-based starting MMR values.",
            inline=False
        )

    embed.set_footer(
        text=f"Reset performed by {interaction.user.display_name} | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    await interaction.followup.send(embed=embed)

    # Announce the reset in the channel
    announcement = discord.Embed(
        title="🔄 Season Reset",
        description=f"A leaderboard reset has been performed by {interaction.user.mention}",
        color=0xff9900
    )

    announcement.add_field(
        name="Reset Type",
        value=f"{reset_type.title()} stats have been reset.",
        inline=False
    )

    if reset_type == "all":
        announcement.add_field(
            name="🚨 IMPORTANT: Re-verification Required 🚨",
            value="This was a complete reset. **All Discord roles have been removed and MMR has been set to 0**. " +
                  "**All players must re-verify their ranks** before joining the queue again.",
            inline=False
        )

        announcement.add_field(
            name="How to Verify",
            value="Use the rank check page on the website to verify your rank. " +
                  "This will assign your appropriate Discord role and starting MMR.",
            inline=False
        )
    else:
        announcement.add_field(
            name="What This Means",
            value="Your MMR has been reset to the starting value based on your verified rank.",
            inline=False
        )

    # Send announcement to the channel
    await interaction.channel.send(embed=announcement)


# 4. Sub Command
@bot.tree.command(name="sub", description="Substitute players in an active match")
@app_commands.describe(
    match_id="The ID of the match",
    player_out="The player to remove from the match",
    player_in="The player to add to the match"
)
async def sub_slash(interaction: discord.Interaction, match_id: str, player_out: discord.Member,
                    player_in: discord.Member):
    # Check if command is used in an allowed channel
    if not is_command_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
            ephemeral=True
        )
        return

    # Check if user has permissions (admin or match participant)
    is_admin = interaction.user.guild_permissions.administrator

    # Look up the match
    match = system_coordinator.match_system.matches.find_one({"match_id": match_id})

    if not match:
        await interaction.response.send_message(f"Match with ID `{match_id}` not found.", ephemeral=True)
        return

    # Check if match is in progress
    if match.get("status") != "in_progress":
        await interaction.response.send_message(
            f"Match with ID `{match_id}` is not in progress (status: {match.get('status')}). " +
            "Substitutions are only available for in-progress matches.",
            ephemeral=True
        )
        return

    # Get player data
    player_out_id = str(player_out.id)
    player_in_id = str(player_in.id)
    player_out_name = player_out.display_name
    player_in_name = player_in.display_name
    player_out_mention = player_out.mention
    player_in_mention = player_in.mention

    # If not admin, check if user is part of the match
    if not is_admin:
        # Get teams
        team1 = match.get("team1", [])
        team2 = match.get("team2", [])
        all_players = team1 + team2

        # Check if interaction user is part of the match
        user_in_match = any(p.get("id") == str(interaction.user.id) for p in all_players)

        if not user_in_match:
            await interaction.response.send_message(
                "You must be part of the match or an administrator to make substitutions.",
                ephemeral=True
            )
            return

    # Check which team player_out is on
    team1 = match.get("team1", [])
    team2 = match.get("team2", [])

    player_found = False
    team_num = 0
    team_index = -1

    # Check team1
    for i, player in enumerate(team1):
        if player.get("id") == player_out_id:
            team_num = 1
            team_index = i
            player_found = True
            break

    # Check team2 if not found in team1
    if not player_found:
        for i, player in enumerate(team2):
            if player.get("id") == player_out_id:
                team_num = 2
                team_index = i
                player_found = True
                break

    if not player_found:
        await interaction.response.send_message(
            f"{player_out_mention} is not part of match `{match_id}`.",
            ephemeral=True
        )
        return

    # Check if player_in is already in a match
    player_in_match = system_coordinator.queue_manager.get_player_match(player_in_id)

    if player_in_match and player_in_match.get("match_id") != match_id:
        await interaction.response.send_message(
            f"{player_in_mention} is already in another active match and cannot be substituted.",
            ephemeral=True
        )
        return

    # Check if player_in is already in this match
    player_in_this_match = any(p.get("id") == player_in_id for p in team1 + team2)

    if player_in_this_match:
        await interaction.response.send_message(
            f"{player_in_mention} is already part of match `{match_id}`.",
            ephemeral=True
        )
        return

    # Create new player data
    new_player_data = {
        "id": player_in_id,
        "name": player_in_name,
        "mention": player_in_mention
    }

    # Update the match in the database
    if team_num == 1:
        # Replace in team1
        team1[team_index] = new_player_data

        system_coordinator.match_system.matches.update_one(
            {"match_id": match_id},
            {"$set": {"team1": team1}}
        )

        # Update in the queue manager's active matches
        if match_id in system_coordinator.queue_manager.active_matches:
            system_coordinator.queue_manager.active_matches[match_id]["team1"] = team1

            # Update player-match mapping
            system_coordinator.queue_manager.player_matches.pop(player_out_id, None)
            system_coordinator.queue_manager.player_matches[player_in_id] = match_id
    else:
        # Replace in team2
        team2[team_index] = new_player_data

        system_coordinator.match_system.matches.update_one(
            {"match_id": match_id},
            {"$set": {"team2": team2}}
        )

        # Update in the queue manager's active matches
        if match_id in system_coordinator.queue_manager.active_matches:
            system_coordinator.queue_manager.active_matches[match_id]["team2"] = team2

            # Update player-match mapping
            system_coordinator.queue_manager.player_matches.pop(player_out_id, None)
            system_coordinator.queue_manager.player_matches[player_in_id] = match_id

    # Create embed response
    embed = discord.Embed(
        title="Player Substitution",
        description=f"Match ID: `{match_id}`",
        color=0x00aaff
    )

    embed.add_field(
        name="Team",
        value=f"Team {team_num}",
        inline=False
    )

    embed.add_field(
        name="Substitution",
        value=f"OUT: {player_out_mention}\nIN: {player_in_mention}",
        inline=False
    )

    embed.set_footer(
        text=f"Requested by {interaction.user.display_name} | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    await interaction.response.send_message(embed=embed)

# Run the bot with the keepalive server
if __name__ == "__main__":
    # Start the keepalive server first
    start_keepalive_server()

    # Then run the bot
    bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)