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
from queue_handler import QueueHandler
from votesystem import VoteSystem
from captainssystem import CaptainsSystem
from matchsystem import MatchSystem
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
from system_coordinators import VoteSystemCoordinator, CaptainSystemCoordinator
import uuid
import random

# Load environment variables
load_dotenv()
token = os.getenv('DISCORD_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')

# Set up logging
handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.reactions = True  # Make sure reaction intents are enabled

bot = commands.Bot(command_prefix='/', intents=intents)
bot.remove_command('help')

# Track recent commands to prevent duplicates
recent_commands = {}
command_lock = asyncio.Lock()

# Create a minimal Flask app just for keepalive purposes
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


# Simple context class to help with the transition to slash commands
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


async def is_duplicate_command(ctx):
    """Thread-safe check if a command is a duplicate"""
    user_id = ctx.author.id
    command_name = ctx.command.name if ctx.command else "unknown"
    channel_id = ctx.channel.id
    message_id = ctx.message.id
    timestamp = ctx.message.created_at.timestamp()

    # Add more detailed logging
    print(f"Command received: {command_name} from {ctx.author.name} (ID: {message_id})")

    # Use a more unique key that includes the message ID
    key = f"{user_id}:{command_name}:{channel_id}:{message_id}"

    # Use lock to prevent race conditions
    async with command_lock:
        # Check if we've seen this exact message ID before
        # This ensures we only detect true duplicates, not repeat attempts
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
queue_handler = QueueHandler(db)
match_system = MatchSystem(db)

# Create channel-specific vote and captain systems
channel_names = ["rank-a", "rank-b", "rank-c", "global"]
vote_systems = {}
captains_systems = {}

for channel_name in channel_names:
    captains_systems[channel_name] = CaptainsSystem(db, queue_handler)
    captains_systems[channel_name].set_match_system(match_system)

    vote_systems[channel_name] = VoteSystem(db, queue_handler, captains_systems[channel_name])
    vote_systems[channel_name].set_match_system(match_system)

# Main vote system coordinator
vote_system = VoteSystemCoordinator(vote_systems)
captains_system = CaptainSystemCoordinator(captains_systems)


@bot.event
async def on_ready():
    print(f"{bot.user.name} is now online with ID: {bot.user.id}")
    print(f"Connected to {len(bot.guilds)} guilds")

    # Set bot for all systems
    vote_system.set_bot(bot)
    captains_system.set_bot(bot)
    queue_handler.set_bot(bot)

    # Get all queue channels and initialize systems
    for guild in bot.guilds:
        for channel in guild.text_channels:
            channel_name = channel.name.lower()
            if channel_name in ["rank-a", "rank-b", "rank-c", "global"]:
                channel_id = str(channel.id)

                # Initialize queue handler with channel-specific systems
                if channel_name in vote_systems:
                    queue_handler.set_vote_system(channel_id, vote_systems[channel_name])

                if channel_name in captains_systems:
                    queue_handler.set_captains_system(channel_id, captains_systems[channel_name])

    print(f"BOT INSTANCE ACTIVE - {datetime.datetime.now(datetime.UTC)}")

    # Sync command tree with Discord
    await bot.tree.sync()
    print("Successfully synced application commands")


@bot.event
async def on_reaction_add(reaction, user):
    """Handle reactions for voting"""
    if user.bot:
        return  # Ignore bot reactions

    # Pass to vote system to handle
    await vote_system.handle_reaction(reaction, user)


# Queue commands
@bot.tree.command(name="queue", description="Join the queue for 6 mans")
async def queue_slash(interaction: discord.Interaction):
    # Create context for backward compatibility
    ctx = SimpleContext(interaction)

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

    # Fix: Allow joining if either database record exists OR player has a rank role
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

    # Continue with regular join process
    channel_id = interaction.channel.id
    response = queue_handler.add_player(player, channel_id)
    await interaction.response.send_message(response)

    # Check if queue is full and start voting
    players = queue_handler.get_players_for_match(channel_id)
    if len(players) >= 6:
        # Check if voting is already active for this channel
        if not vote_system.is_voting_active(channel_id):
            await vote_system.start_vote(interaction.channel)


@bot.tree.command(name="leave", description="Leave the queue")
async def leave_slash(interaction: discord.Interaction):
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
    player_mention = player.mention

    # Get all rank roles
    rank_a_role = discord.utils.get(interaction.guild.roles, name="Rank A")
    rank_b_role = discord.utils.get(interaction.guild.roles, name="Rank B")
    rank_c_role = discord.utils.get(interaction.guild.roles, name="Rank C")
    has_rank_role = any(role in player.roles for role in [rank_a_role, rank_b_role, rank_c_role])

    # Check if player has a rank entry in the database OR has a rank role
    rank_record = db.get_collection('ranks').find_one({"discord_id": player_id})

    # If no rank verification, show the same message as join command
    if not (rank_record or has_rank_role):
        # Player hasn't completed rank verification
        embed = discord.Embed(
            title="Rank Verification Required",
            description="You need to verify your Rocket League rank to use queue commands.",
            color=0xf1c40f
        )
        embed.add_field(
            name="How to Verify",
            value="Visit the rank check page on the website to complete verification.",
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    channel_id = str(interaction.channel.id)

    # DEBUG - Before leaving
    print(f"DEBUG - LEAVE ATTEMPT: {interaction.user.name} trying to leave queue in channel {channel_id}")

    # Find if the player is in ANY queue first
    any_queue = queue_handler.queue_collection.find_one({"id": player_id})
    if not any_queue:
        await interaction.response.send_message(f"{player_mention} is not in any queue!", ephemeral=True)
        return

    # Now check if the player is in THIS specific channel's queue
    channel_queue = queue_handler.queue_collection.find_one({"id": player_id, "channel_id": channel_id})
    if not channel_queue:
        other_channel_id = any_queue.get("channel_id")
        if other_channel_id and other_channel_id.isdigit():
            await interaction.response.send_message(
                f"{player_mention} is not in this channel's queue. You are in <#{other_channel_id}>'s queue.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"{player_mention} is in another channel's queue, not this one.",
                ephemeral=True
            )
        return

    # Check if voting is active in this channel
    if vote_system.is_voting_active(channel_id):
        await interaction.response.send_message(
            f"{player_mention} cannot leave the queue while voting is in progress!",
            ephemeral=True
        )
        return

    # Delete the player from THIS channel's queue
    result = queue_handler.queue_collection.delete_one({"id": player_id, "channel_id": channel_id})

    # Check if captain selection is active in this channel
    if captains_system.is_selection_active(channel_id):
        captains_system.cancel_selection(channel_id)

    if result.deleted_count > 0:
        await interaction.response.send_message(f"{player_mention} has left the queue!")
    else:
        await interaction.response.send_message(
            f"Error removing {player_mention} from the queue. Please try again.",
            ephemeral=True
        )

    # DEBUG - After leaving
    print("DEBUG - AFTER LEAVE: Current queue state:")
    all_queued = list(queue_handler.queue_collection.find())
    for p in all_queued:
        print(f"Player: {p.get('name')}, Channel: {p.get('channel_id')}")


@bot.tree.command(name="status", description="Shows the current queue status")
async def status_slash(interaction: discord.Interaction):
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

    # Check if player has a rank entry in the ranks collection
    rank_record = db.get_collection('ranks').find_one({"discord_id": player_id})

    if not rank_record:
        # Player hasn't completed rank verification
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

    channel_id = str(interaction.channel.id)

    # Get all players in this channel's queue directly from the database
    players = list(queue_handler.queue_collection.find({"channel_id": channel_id}))
    count = len(players)

    # Create an embed
    embed = discord.Embed(
        title="Queue Status",
        description=f"**Current Queue: {count}/6 players**",
        color=0x3498db
    )

    if count == 0:
        embed.add_field(name="Status", value="Queue is empty! Use `/queue` to join the queue.", inline=False)
        await interaction.response.send_message(embed=embed)
        return

    # Create a list of player mentions
    player_mentions = [player['mention'] for player in players]

    # Add player list to embed
    embed.add_field(name="Players", value=", ".join(player_mentions), inline=False)

    # Add info about how many more players are needed
    if count < 6:
        more_needed = 6 - count
        embed.add_field(name="Info", value=f"{more_needed} more player(s) needed for a match.", inline=False)
    else:
        # Queue is full
        embed.add_field(name="Status", value="**Queue is FULL!** Ready to start match.", inline=False)

    await interaction.response.send_message(embed=embed)

    # If queue is full, check if we should start voting
    if count >= 6:
        channel_name = interaction.channel.name.lower()
        if channel_name in ["rank-a", "rank-b", "rank-c", "global"]:
            # Check if voting is already active for this channel
            if not vote_system.is_voting_active(channel_id):
                await vote_system.start_vote(interaction.channel)


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
    match_result, error = await match_system.report_match_by_id(match_id, reporter_id, result, ctx)

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
                if player["id"] == player_id:
                    winning_team_mmr_changes[i] = f"+{mmr_change}"
                    break
        else:
            # This is a loser
            for i, player in enumerate(losing_team):
                if player["id"] == player_id:
                    # MMR change is already negative for losers
                    losing_team_mmr_changes[i] = f"{mmr_change}"
                    break

    # Handle dummy players (they don't have MMR changes in the database)
    for i, player in enumerate(winning_team):
        if player["id"].startswith('9000'):  # Dummy player
            winning_team_mmr_changes[i] = "+0"

    for i, player in enumerate(losing_team):
        if player["id"].startswith('9000'):  # Dummy player
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
            member = await interaction.guild.fetch_member(int(player["id"]))
            name = member.display_name if member else player['name']
        except:
            name = player["name"]

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
            member = await interaction.guild.fetch_member(int(player["id"]))
            name = member.display_name if member else player['name']
        except:
            name = player["name"]

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
    team_number="The team number that won (1 or 2)",
    result="Must be 'win'",
    match_id="Match ID (optional - uses current channel's match if omitted)"
)
@app_commands.choices(result=[
    app_commands.Choice(name="Win", value="win"),
])
async def adminreport_slash(interaction: discord.Interaction, team_number: int, result: str, match_id: str = None):
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

    channel_id = str(interaction.channel.id)

    # Find the active match either by ID or channel
    if match_id:
        active_match = match_system.matches.find_one({"match_id": match_id, "status": "in_progress"})
        if not active_match:
            await interaction.response.send_message(f"No active match found with ID `{match_id}`.", ephemeral=True)
            return
    else:
        # Otherwise try to find match in current channel
        active_match = match_system.get_active_match_by_channel(channel_id)
        if not active_match:
            await interaction.response.send_message(
                "No active match found in this channel. Please report in the channel where the match was created or provide a match ID.",
                ephemeral=True
            )
            return
        match_id = active_match["match_id"]

    # Determine winner and scores based on admin input
    if team_number == 1:
        team1_score = 1
        team2_score = 0
    else:
        team1_score = 0
        team2_score = 1

    # Update match data
    match_system.matches.update_one(
        {"match_id": match_id},
        {"$set": {
            "status": "completed",
            "winner": team_number,
            "score": {"team1": team1_score, "team2": team2_score},
            "completed_at": datetime.datetime.now(datetime.UTC),
            "reported_by": str(interaction.user.id)
        }}
    )

    # Determine winning and losing teams
    if team_number == 1:
        winning_team = active_match["team1"]
        losing_team = active_match["team2"]
    else:
        winning_team = active_match["team2"]
        losing_team = active_match["team1"]

    # Update MMR
    match_system.update_player_mmr(winning_team, losing_team)

    # Format team members - using display_name instead of mentions
    winning_members = []
    for player in winning_team:
        try:
            member = await interaction.guild.fetch_member(int(player["id"]) if player["id"].isdigit() else 0)
            winning_members.append(member.display_name if member else player["name"])
        except:
            winning_members.append(player["name"])

    losing_members = []
    for player in losing_team:
        try:
            member = await interaction.guild.fetch_member(int(player["id"]) if player["id"].isdigit() else 0)
            losing_members.append(member.display_name if member else player["name"])
        except:
            losing_members.append(player["name"])

    # Create results embed
    embed = discord.Embed(
        title="Match Results (Admin Report)",
        description=f"Match completed",
        color=0x00ff00
    )

    embed.add_field(name="Winners", value=", ".join(winning_members), inline=False)
    embed.add_field(name="Losers", value=", ".join(losing_members), inline=False)
    embed.add_field(name="MMR", value="+15 for winners, -12 for losers", inline=False)
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

    # Optionally add a thumbnail
    embed.set_thumbnail(url="")  # Replace with a Rocket League icon URL

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
    player_data = match_system.get_player_stats(player_id)

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
        all_players = list(match_system.players.find().sort("mmr", -1))
        total_players = len(all_players)

        # Get the position using the player's ID
        for i, p in enumerate(all_players):
            if p["id"] == player_id:
                rank_position = i + 1
                break

    # Get global rank position if they've played global games
    global_rank_position = "Unranked"
    if global_matches > 0:
        global_players = list(match_system.players.find({"global_matches": {"$gt": 0}}).sort("global_mmr", -1))
        # Get the position using the player's ID
        for i, p in enumerate(global_players):
            if p["id"] == player_id:
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

    tier_color = 0x1287438  # Default color for Rank C (green)
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


@bot.tree.command(name="adjustmmr", description="Admin command to adjust a player's MMR")
@app_commands.describe(
    member="The member whose MMR you want to adjust",
    amount="The amount to adjust (positive or negative)"
)
async def adjustmmr_slash(interaction: discord.Interaction, member: discord.Member, amount: int):
    # Check if user has admin permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need administrator permissions to use this command.",
                                                ephemeral=True)
        return

    if amount == 0:
        await interaction.response.send_message("Please specify a non-zero amount to adjust.", ephemeral=True)
        return

    player_id = str(member.id)

    # Debug output
    print(f"Adjusting MMR for player: {member.display_name} (ID: {player_id}) by {amount}")

    # Get player data from database
    player_data = match_system.players.find_one({"id": player_id})

    # Creating context for compatibility with match_system functions
    ctx = SimpleContext(interaction)

    if not player_data:
        # Player doesn't exist in database, create a new entry
        match_system.players.insert_one({
            "id": player_id,
            "name": member.display_name,
            "mmr": 1000 + amount,  # Start with default 1000 + adjustment
            "wins": 0,
            "losses": 0,
            "matches": 0,
            "created_at": datetime.datetime.now(datetime.UTC),
            "last_updated": datetime.datetime.now(datetime.UTC)
        })

        old_mmr = 1000
        new_mmr = 1000 + amount
    else:
        # Update existing player
        old_mmr = player_data.get("mmr", 1000)
        new_mmr = max(0, old_mmr + amount)  # Ensure MMR doesn't go below 0

        match_system.players.update_one(
            {"id": player_id},
            {"$set": {
                "mmr": new_mmr,
                "last_updated": datetime.datetime.now(datetime.UTC)
            }}
        )

    # Create an embed to show the MMR change
    direction = "increased" if amount > 0 else "decreased"
    color = 0x00ff00 if amount > 0 else 0xff0000  # Green for increase, red for decrease

    embed = discord.Embed(
        title=f"MMR Adjustment for {member.display_name}",
        description=f"MMR has been {direction} by {abs(amount)} points.",
        color=color
    )

    embed.add_field(name="Previous MMR", value=str(old_mmr), inline=True)
    embed.add_field(name="New MMR", value=str(new_mmr), inline=True)
    embed.add_field(name="Change", value=f"{'+' if amount > 0 else ''}{amount}", inline=True)

    embed.set_footer(text=f"Adjusted by {interaction.user.display_name}")

    await interaction.response.send_message(embed=embed)

    # Update their Discord role based on new MMR
    try:
        await match_system.update_discord_role(ctx, player_id, new_mmr)
    except Exception as e:
        print(f"Error updating Discord role: {str(e)}")
        await interaction.followup.send(f"Note: Could not update Discord role. Error: {str(e)}")


@bot.tree.command(name="clearqueue", description="Clear all players from the queue (Admin only)")
async def clearqueue_slash(interaction: discord.Interaction):
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
    players = queue_handler.get_players_for_match(channel_id)
    count = len(players)

    # Clear the queue collection
    queue_handler.queue_collection.delete_many({})

    # Cancel any active votes or selections
    if vote_system.is_voting_active():
        vote_system.cancel_voting()

    if captains_system.is_selection_active():
        captains_system.cancel_selection()

    # Send confirmation
    if count == 0:
        await interaction.response.send_message("Queue was already empty!")
    else:
        await interaction.response.send_message(f"✅ Queue cleared! Removed {count} player(s) from the queue.")


@bot.tree.command(name="resetleaderboard", description="Reset the leaderboard and clear all queues (Admin only)")
@app_commands.describe(confirmation="Type 'confirm' to reset the leaderboard")
async def resetleaderboard_slash(interaction: discord.Interaction, confirmation: str = None):
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

    # Track reset confirmations with a dict that maps user IDs to timestamps
    if not hasattr(bot, 'reset_confirmations'):
        bot.reset_confirmations = {}

    user_id = str(interaction.user.id)
    current_time = datetime.datetime.now(datetime.UTC).timestamp()

    # Require confirmation
    if confirmation is None:
        # First step: Show warning
        embed = discord.Embed(
            title="⚠️ Reset Leaderboard Confirmation",
            description="This will reset MMR, stats, rank data, match history, clear all active queues, cancel any ongoing votes/selections, and **remove all rank roles**. This action cannot be undone!",
            color=0xff9900
        )
        embed.add_field(
            name="To confirm:",
            value="Type `/resetleaderboard confirm`",
            inline=False
        )
        await interaction.response.send_message(embed=embed)

        # Store that this user has seen the warning
        bot.reset_confirmations[user_id] = current_time
        return
    elif confirmation.lower() == "confirm":
        # Check if user has seen the warning (within the last 5 minutes)
        confirmation_time = bot.reset_confirmations.get(user_id, 0)
        if current_time - confirmation_time > 300 or confirmation_time == 0:  # 5 minutes expiration
            await interaction.response.send_message("Please use /resetleaderboard first!", ephemeral=True)
            return

        # Remove the confirmation once used
        if user_id in bot.reset_confirmations:
            del bot.reset_confirmations[user_id]

        # Begin actual reset process
        await interaction.response.defer()

        try:
            # Initialize variables that will be used throughout the function
            web_reset = "⚠️ Web reset not attempted"
            verification_reset = "⚠️ Verification reset not attempted"

            # Check multiple collections to determine if truly empty
            db_obj = match_system.players.database
            player_count = match_system.players.count_documents({})
            match_count = db_obj['matches'].count_documents({})
            rank_count = db_obj['ranks'].count_documents({})

            total_documents = player_count + match_count + rank_count

            # Create backup collections with timestamp
            timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d_%H%M%S")

            # Track what was backed up for reporting
            backed_up = []

            # Backup and reset players
            if player_count > 0:
                backup_collection_name = f"players_backup_{timestamp}"
                db_obj.create_collection(backup_collection_name)
                backup_collection = db_obj[backup_collection_name]
                for player in match_system.players.find():
                    backup_collection.insert_one(player)
                match_system.players.delete_many({})
                backed_up.append(f"Players ({player_count})")

            # Backup and reset matches
            matches_collection = db_obj['matches']
            if match_count > 0:
                backup_collection_name = f"matches_backup_{timestamp}"
                db_obj.create_collection(backup_collection_name)
                backup_collection = db_obj[backup_collection_name]
                for match in matches_collection.find():
                    backup_collection.insert_one(match)
                matches_collection.delete_many({})
                backed_up.append(f"Matches ({match_count})")

            # Backup and reset ranks
            ranks_collection = db_obj['ranks']
            if rank_count > 0:
                backup_collection_name = f"ranks_backup_{timestamp}"
                db_obj.create_collection(backup_collection_name)
                backup_collection = db_obj[backup_collection_name]
                for rank in ranks_collection.find():
                    backup_collection.insert_one(rank)
                ranks_collection.delete_many({})
                backed_up.append(f"Ranks ({rank_count})")

            # Clear all queues and cancel active votes/selections
            queue_count = queue_handler.queue_collection.count_documents({})
            if queue_count > 0:
                # Backup queue first
                backup_collection_name = f"queue_backup_{timestamp}"
                db_obj.create_collection(backup_collection_name)
                backup_collection = db_obj[backup_collection_name]
                for queue_item in queue_handler.queue_collection.find():
                    backup_collection.insert_one(queue_item)

                # Clear all queues
                queue_handler.queue_collection.delete_many({})
                backed_up.append(f"Queues ({queue_count})")

            # Cancel any active votes
            active_vote_channels = []
            if vote_system.is_voting_active():
                for channel_id in vote_system.active_votes.keys():
                    active_vote_channels.append(channel_id)
                vote_system.cancel_voting()

            # Cancel any active captain selections
            active_selection_channels = []
            if captains_system.is_selection_active():
                for channel_id in captains_system.active_selections.keys():
                    active_selection_channels.append(channel_id)
                captains_system.cancel_selection()

            # Call the web API to reset the leaderboard and verification status
            try:
                webapp_url = os.getenv('WEBAPP_URL', 'https://sixgentsbot-1.onrender.com')
                admin_token = os.getenv('ADMIN_TOKEN', 'admin-secret-token')

                headers = {
                    'Authorization': admin_token,
                    'Content-Type': 'application/json'
                }

                data = {
                    'admin_id': str(interaction.user.id),
                    'reason': 'Season reset via Discord command'
                }

                # Reset leaderboard
                leaderboard_response = requests.post(
                    f"{webapp_url}/api/reset-leaderboard",
                    headers=headers,
                    json=data
                )

                # Also reset verification
                verification_response = requests.post(
                    f"{webapp_url}/api/reset-verification",
                    headers=headers,
                    json=data
                )

                if leaderboard_response.status_code == 200:
                    web_reset = "✅ Web leaderboard reset successfully."
                else:
                    web_reset = f"❌ Failed to reset web leaderboard (Status: {leaderboard_response.status_code})."

                if verification_response.status_code == 200:
                    verification_reset = "✅ Rank verification reset successfully."
                else:
                    verification_reset = f"❌ Failed to reset rank verification (Status: {verification_response.status_code})."

            except Exception as e:
                web_reset = f"❌ Error connecting to web services: {str(e)}"
                # Keep verification_reset with its default value

            # Remove rank roles from all members
            role_reset = await remove_all_rank_roles(interaction.guild)

            # Record the reset event locally
            resets_collection = db_obj['resets']
            resets_collection.insert_one({
                "type": "leaderboard_reset",
                "timestamp": datetime.datetime.now(datetime.UTC),
                "performed_by": str(interaction.user.id),
                "performed_by_name": interaction.user.display_name,
                "reason": "Season reset via Discord command"
            })

            # Debug - print variable values before creating embed
            print(f"Debug - web_reset: {web_reset}, verification_reset: {verification_reset}")

            # Send confirmation
            embed = discord.Embed(
                title="✅ Leaderboard Reset Complete",
                description=f"Reset {total_documents} documents across {len(backed_up) if backed_up else 0} collections.",
                color=0x00ff00
            )

            if backed_up:
                embed.add_field(
                    name="Collections Reset",
                    value="\n".join(backed_up),
                    inline=False
                )

            # Add info about cleared queues and canceled activities
            if queue_count > 0:
                embed.add_field(
                    name="Queues Cleared",
                    value=f"Cleared {queue_count} players from active queues",
                    inline=False
                )

            if active_vote_channels or active_selection_channels:
                cancel_message = ""
                if active_vote_channels:
                    cancel_message += f"Canceled team voting in {len(active_vote_channels)} channels\n"
                if active_selection_channels:
                    cancel_message += f"Canceled captain selection in {len(active_selection_channels)} channels"

                embed.add_field(
                    name="Active Processes Canceled",
                    value=cancel_message.strip(),
                    inline=False
                )

            embed.add_field(
                name="Backup Created",
                value=f"Backup timestamp: `{timestamp}`",
                inline=False
            )

            embed.add_field(
                name="Web Leaderboard Status",
                value=web_reset,
                inline=False
            )

            embed.add_field(
                name="Verification Reset Status",
                value=verification_reset,
                inline=False
            )

            embed.add_field(
                name="Discord Roles",
                value=role_reset,
                inline=False
            )

            embed.set_footer(text=f"Reset by {interaction.user.display_name}")

            await interaction.followup.send(embed=embed)

            # Send a global announcement to all rank channels
            for channel_name in ["rank-a", "rank-b", "rank-c", "global"]:
                for channel in interaction.guild.text_channels:
                    if channel.name.lower() == channel_name:
                        try:
                            announcement = discord.Embed(
                                title="🔄 Season Reset Announcement",
                                description="The leaderboard and all ranks have been reset for a new season!",
                                color=0x9932CC  # Purple color for announcements
                            )
                            announcement.add_field(
                                name="What was reset",
                                value="• All MMR and stats\n• All rank roles\n• All active queues\n• All verification status",
                                inline=False
                            )
                            announcement.add_field(
                                name="Next Steps",
                                value="Please verify your rank again to join the new season's queues!",
                                inline=False
                            )
                            announcement.set_footer(text=f"Reset performed by {interaction.user.display_name}")

                            await channel.send(embed=announcement)
                        except Exception as e:
                            print(f"Error sending reset announcement to {channel.name}: {str(e)}")

        except Exception as e:
            await interaction.followup.send(f"Error resetting leaderboard: {str(e)}")
    else:
        await interaction.response.send_message("Invalid confirmation. Use `/resetleaderboard` to see instructions.",
                                                ephemeral=True)
        return


# Helper function to handle role removal
async def remove_all_rank_roles(guild):
    """Remove all rank roles from members"""
    try:
        # Get the rank roles by name
        rank_role_names = ["Rank A", "Rank B", "Rank C"]
        rank_roles = []

        for role_name in rank_role_names:
            role = discord.utils.get(guild.roles, name=role_name)
            if role:
                rank_roles.append(role)

        if not rank_roles:
            return "⚠️ No rank roles found in server"

        # Count how many members had roles removed
        member_count = 0
        role_count = 0

        # Remove roles from all members
        for member in guild.members:
            member_updated = False
            for role in rank_roles:
                if role in member.roles:
                    await member.remove_roles(role)
                    role_count += 1
                    member_updated = True
            if member_updated:
                member_count += 1

        return f"✅ Removed {role_count} rank roles from {member_count} members"
    except Exception as e:
        return f"❌ Error removing roles: {str(e)}"


@bot.tree.command(name="removematch", description="Remove the results of a match by ID (Admin only)")
@app_commands.describe(
    match_id="The ID of the match to remove (optional - will show recent matches if omitted)",
    confirmation="Type 'confirm' to remove the match"
)
async def removematch_slash(interaction: discord.Interaction, match_id: str = None, confirmation: str = None):
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

    # Track match removals with a dict
    if not hasattr(bot, 'remove_confirmations'):
        bot.remove_confirmations = {}

    # CASE 1: No match ID provided - inform about usage
    if match_id is None:
        await show_recent_matches(interaction)
        return

    # CASE 2: Match ID provided but no confirmation - Show match details
    elif confirmation is None:
        # Find the match
        match = match_system.matches.find_one({"match_id": match_id})
        if not match:
            await interaction.response.send_message(f"No match found with ID `{match_id}`.", ephemeral=True)
            return

        # Create confirmation message with match details
        embed = create_match_confirmation_embed(match, match_id)

        # Track that we showed this match information
        current_time = datetime.datetime.now(datetime.UTC).timestamp()
        bot.remove_confirmations[match_id] = current_time

        await interaction.response.send_message(embed=embed)
        return

    # CASE 3: Match ID and confirmation provided - Process the removal
    elif confirmation.lower() == "confirm":
        # Check if the match ID has been confirmed within the last 5 minutes
        current_time = datetime.datetime.now(datetime.UTC).timestamp()
        confirmation_time = bot.remove_confirmations.get(match_id, 0)

        if current_time - confirmation_time > 300 or confirmation_time == 0:  # 5 minutes expiration
            await interaction.response.send_message(f"Please use `/removematch {match_id}` first!", ephemeral=True)
            return

        # Remove the confirmation once used
        if match_id in bot.remove_confirmations:
            del bot.remove_confirmations[match_id]

        # Find the match
        match = match_system.matches.find_one({"match_id": match_id})
        if not match:
            await interaction.response.send_message(f"No match found with ID `{match_id}`.", ephemeral=True)
            return

        # Defer the response as this could take some time
        await interaction.response.defer()

        # Creating context for compatibility with functions
        ctx = SimpleContext(interaction)

        # Execute the actual removal
        removal_result = await remove_match_results(ctx, match)
        await interaction.followup.send(embed=removal_result)

    # CASE 4: Invalid confirmation text
    else:
        await interaction.response.send_message(
            "Invalid confirmation. Use `/removematch {match_id}` to see instructions.", ephemeral=True)


async def show_recent_matches(interaction):
    """Shows a list of recent matches to help admins find match IDs"""
    recent_matches = list(match_system.matches.find(
        {"status": "completed"},
        {"match_id": 1, "team1": 1, "team2": 1, "winner": 1, "completed_at": 1}
    ).sort("completed_at", -1).limit(5))

    if not recent_matches:
        await interaction.response.send_message("No completed matches found.")
        return

    embed = discord.Embed(
        title="Recent Completed Matches",
        description="Here are the 5 most recent completed matches. Use `/removematch <match_id>` to remove one.",
        color=0x3498db
    )

    for match in recent_matches:
        match_id = match["match_id"]
        team1_summary = ", ".join([p.get("name", "Unknown") for p in match["team1"]])
        team2_summary = ", ".join([p.get("name", "Unknown") for p in match["team2"]])
        winner = match.get("winner", 0)

        completed_time = match.get('completed_at', datetime.datetime.now()).strftime("%Y-%m-%d %H:%M")

        value = f"`{match_id}` | {completed_time}\n" \
                f"Team 1: {team1_summary}\n" \
                f"Team 2: {team2_summary}\n" \
                f"Winner: Team {winner}"

        embed.add_field(
            name=f"Match {match_id}",
            value=value,
            inline=False
        )

    await interaction.response.send_message(embed=embed)


# Helper function to create a confirmation embed for the match
def create_match_confirmation_embed(match, match_id):
    """Create an embed for match removal confirmation"""
    # Check match status
    status = match.get('status', 'unknown')

    # Different display for completed vs. other status matches
    if status != 'completed':
        embed = discord.Embed(
            title="⚠️ Match Not Completed",
            description=f"Match `{match_id}` has status `{status}`, not `completed`.",
            color=0xff9900
        )
        embed.add_field(
            name="Warning",
            value="This match does not appear to be completed. Removing non-completed matches may have unexpected results.",
            inline=False
        )
    else:
        embed = discord.Embed(
            title="⚠️ Remove Match Confirmation",
            description=f"You are about to remove the results for match `{match_id}`.",
            color=0xff9900
        )

        # Format team information
        team1_names = [p['name'] for p in match['team1']]
        team2_names = [p['name'] for p in match['team2']]
        winner = match.get('winner', 0)

        embed.add_field(
            name=f"Team 1 {' (Winner)' if winner == 1 else ''}",
            value=", ".join(team1_names),
            inline=False
        )

        embed.add_field(
            name=f"Team 2 {' (Winner)' if winner == 2 else ''}",
            value=", ".join(team2_names),
            inline=False
        )

        if match.get('completed_at'):
            completed_time = match['completed_at'].strftime("%Y-%m-%d %H:%M:%S")
            embed.add_field(
                name="Completed At",
                value=completed_time,
                inline=False
            )

        if match.get('reported_by'):
            reporter_id = match['reported_by']
            try:
                member = bot.get_guild(int(match.get('guild_id', 0))).get_member(int(reporter_id))
                reporter_name = member.display_name if member else f"Unknown (ID: {reporter_id})"
            except:
                reporter_name = f"Unknown (ID: {reporter_id})"

            embed.add_field(
                name="Reported By",
                value=reporter_name,
                inline=False
            )

        embed.add_field(
            name="To confirm:",
            value=f"Type `/removematch {match_id} confirm`",
            inline=False
        )

        embed.add_field(
            name="Warning",
            value="This will revert MMR changes and could affect player rankings. This action cannot be undone!",
            inline=False
        )

        return embed

async def remove_match_results(ctx, match):
        """Process the actual removal of match results by negating MMR changes"""
        match_id = match['match_id']

        # Get the teams and determine who won
        team1 = match['team1']
        team2 = match['team2']
        winner = match.get('winner', 0)

        if winner == 1:
            winning_team = team1
            losing_team = team2
        elif winner == 2:
            winning_team = team2
            losing_team = team1
        else:
            # Create error embed
            error_embed = discord.Embed(
                title="❌ Error Removing Match",
                description=f"Match `{match_id}` does not have a valid winner assigned.",
                color=0xff0000
            )
            return error_embed

        # Start tracking MMR changes for reporting
        mmr_changes = []

        # Check if the match has stored MMR changes
        stored_mmr_changes = match.get('mmr_changes', [])
        has_stored_changes = len(stored_mmr_changes) > 0

        # Create a map of player_id to their MMR change for easy lookup
        mmr_change_map = {}
        if has_stored_changes:
            for change in stored_mmr_changes:
                player_id = change.get('player_id')
                if player_id:
                    mmr_change_map[player_id] = change

        # Process all players from both teams
        all_players = winning_team + losing_team

        for player in all_players:
            player_id = player['id']
            # Skip dummy players
            if player_id.startswith('9000'):
                continue

            player_data = match_system.players.find_one({"id": player_id})
            if not player_data:
                continue  # Skip if player no longer exists

            # Check if we have stored MMR change for this player
            if player_id in mmr_change_map:
                change_data = mmr_change_map[player_id]

                # Get the original MMR change value
                mmr_change = change_data.get('mmr_change', 0)
                is_win = change_data.get('is_win', False)

                # Current MMR and stats
                current_mmr = player_data.get('mmr', 0)
                current_matches = player_data.get('matches', 0)
                current_wins = player_data.get('wins', 0)
                current_losses = player_data.get('losses', 0)

                # Calculate new MMR by negating the original change
                # For winners: subtract the MMR they gained
                # For losers: add back the MMR they lost
                new_mmr = current_mmr - mmr_change  # For winners, mmr_change is positive. For losers, it's negative.

                # Decrement match count
                new_matches = max(0, current_matches - 1)

                # Update wins/losses counters
                if is_win:
                    new_wins = max(0, current_wins - 1)  # Decrement wins
                    new_losses = current_losses  # Losses stay the same
                else:
                    new_wins = current_wins  # Wins stay the same
                    new_losses = max(0, current_losses - 1)  # Decrement losses

                # Update database - apply the negated MMR change
                match_system.players.update_one(
                    {"id": player_id},
                    {"$set": {
                        "mmr": new_mmr,
                        "wins": new_wins,
                        "losses": new_losses,
                        "matches": new_matches,
                        "last_updated": datetime.datetime.now(datetime.UTC)
                    }}
                )

                # Format the change for display (reverse the original direction)
                display_change = f"-{mmr_change}" if mmr_change > 0 else f"+{abs(mmr_change)}"

                # Track change for reporting
                mmr_changes.append({
                    "player": player['name'],
                    "old_mmr": current_mmr,
                    "new_mmr": new_mmr,
                    "change": display_change
                })

                # Update Discord role based on new MMR
                try:
                    await match_system.update_discord_role(ctx, player_id, new_mmr)
                except Exception as e:
                    print(f"Error updating Discord role for {player['name']}: {str(e)}")
            else:
                # Fall back to approximation if we don't have stored data
                # (This should rarely happen if your report_match_by_id is storing MMR changes)
                is_winner = player in winning_team

                if is_winner:
                    # Approximate calculation for winners
                    matches_played = player_data.get("matches", 0)
                    if matches_played > 0:
                        matches_played_before = matches_played - 1
                        # Get player's MMR and call calculate_dynamic_mmr with all required parameters
                        current_mmr = player_data.get("mmr", 0)

                        # For approximation, use the player's current MMR for all parameters
                        mmr_change = match_system.calculate_dynamic_mmr(
                            current_mmr,
                            current_mmr,  # Approximation
                            current_mmr,  # Approximation
                            matches_played_before,
                            is_win=True
                        )

                        # Subtract the approximate MMR gain
                        new_mmr = max(0, current_mmr - mmr_change)

                        # Update wins and matches count
                        current_wins = player_data.get("wins", 0)
                        new_wins = max(0, current_wins - 1)
                        new_matches = max(0, matches_played - 1)

                        # Update database
                        match_system.players.update_one(
                            {"id": player_id},
                            {"$set": {
                                "mmr": new_mmr,
                                "wins": new_wins,
                                "matches": new_matches,
                                "last_updated": datetime.datetime.now(datetime.UTC)
                            }}
                        )

                        # Track change for reporting
                        mmr_changes.append({
                            "player": player['name'],
                            "old_mmr": current_mmr,
                            "new_mmr": new_mmr,
                            "change": f"-{mmr_change}"
                        })

                        # Update Discord role
                        try:
                            await match_system.update_discord_role(ctx, player_id, new_mmr)
                        except Exception as e:
                            print(f"Error updating Discord role: {str(e)}")
                else:
                    # Approximate calculation for losers
                    matches_played = player_data.get("matches", 0)
                    if matches_played > 0:
                        matches_played_before = matches_played - 1
                        current_mmr = player_data.get("mmr", 0)

                        # For approximation, use the player's current MMR for all parameters
                        mmr_change = match_system.calculate_dynamic_mmr(
                            current_mmr,
                            current_mmr,  # Approximation
                            current_mmr,  # Approximation
                            matches_played_before,
                            is_win=False
                        )

                        # Add back the approximate MMR loss
                        new_mmr = current_mmr + mmr_change

                        # Update losses and matches count
                        current_losses = player_data.get("losses", 0)
                        new_losses = max(0, current_losses - 1)
                        new_matches = max(0, matches_played - 1)

                        # Update database
                        match_system.players.update_one(
                            {"id": player_id},
                            {"$set": {
                                "mmr": new_mmr,
                                "losses": new_losses,
                                "matches": new_matches,
                                "last_updated": datetime.datetime.now(datetime.UTC)
                            }}
                        )

                        # Track change for reporting
                        mmr_changes.append({
                            "player": player['name'],
                            "old_mmr": current_mmr,
                            "new_mmr": new_mmr,
                            "change": f"+{mmr_change}"
                        })

                        # Update Discord role
                        try:
                            await match_system.update_discord_role(ctx, player_id, new_mmr)
                        except Exception as e:
                            print(f"Error updating Discord role: {str(e)}")

        # Update match status
        match_system.matches.update_one(
            {"match_id": match_id},
            {"$set": {
                "status": "removed",
                "removed_at": datetime.datetime.now(datetime.UTC),
                "removed_by": str(ctx.author.id)
            }}
        )

        # Create embed to display results
        embed = discord.Embed(
            title="✅ Match Results Removed",
            description=f"Successfully removed results for match `{match_id}`.",
            color=0x00ff00
        )

        # Add MMR changes
        for i, change in enumerate(mmr_changes):
            embed.add_field(
                name=change['player'],
                value=f"{change['old_mmr']} → {change['new_mmr']} ({change['change']})",
                inline=True
            )

            # Add spacer after every 3 players for formatting
            if (i + 1) % 3 == 0 and i < len(mmr_changes) - 1:
                embed.add_field(name="\u200b", value="\u200b", inline=True)

        embed.set_footer(text=f"Removed by {ctx.author.display_name}")

        return embed

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

        # Cancel any existing votes to ensure we can start a new one
        if vote_system.is_voting_active():
            vote_system.cancel_voting()

        if captains_system.is_selection_active():
            captains_system.cancel_selection()

        # Get current players in queue
        channel_id = str(interaction.channel.id)
        players = queue_handler.get_players_for_match(channel_id)
        player_count = len(players)

        if player_count == 0:
            await interaction.response.send_message("Can't force start: Queue is empty!")
            return

        # Before adding dummy players, determine the MMR range based on the channel
        # This ensures dummy players have appropriate MMR for each rank channel
        channel_name = interaction.channel.name.lower()
        if channel_name == "rank-a":
            min_mmr = 1600
            max_mmr = 2100
        elif channel_name == "rank-b":
            min_mmr = 1100
            max_mmr = 1599
        else:  # rank-c or global
            min_mmr = 600
            max_mmr = 1099

        # If fewer than 6 players, add dummy players to fill the queue
        if player_count < 6:
            # Create dummy players to fill the queue
            needed = 6 - player_count
            await interaction.response.send_message(f"Adding {needed} dummy players to fill the queue for testing...")

            for i in range(needed):
                # Use numeric IDs starting from 9000 to prevent parsing issues
                dummy_id = f"9000{i + 1}"  # 90001, 90002, etc
                dummy_name = f"TestPlayer{i + 1}"
                dummy_mention = f"@TestPlayer{i + 1}"

                # Generate a random MMR value appropriate for the channel
                dummy_mmr = random.randint(min_mmr, max_mmr)

                # Store MMR in a special field we'll check later
                dummy_player = {
                    "id": dummy_id,
                    "name": dummy_name,
                    "mention": dummy_mention,
                    "channel_id": channel_id,
                    "dummy_mmr": dummy_mmr  # Add MMR for the dummy player
                }

                # Add dummy player to queue
                queue_handler.queue_collection.insert_one(dummy_player)

        # Force start the vote
        await interaction.channel.send("**Force starting team selection!**")
        await vote_system.start_vote(interaction.channel)

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
        players = queue_handler.get_players_for_match(channel_id)
        count = len(players)

        # Cancel any active votes
        vote_active = vote_system.is_voting_active()
        if vote_active:
            vote_system.cancel_voting()

        # Cancel any active selections
        selection_active = captains_system.is_selection_active()
        if selection_active:
            captains_system.cancel_selection()

        # Clear the queue collection
        queue_handler.queue_collection.delete_many({})

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

        embed.add_field(name="Queue Cleared", value=f"Removed {count} player(s) from the queue.", inline=False)
        embed.set_footer(text=f"Executed by {interaction.user.display_name}")

        await interaction.response.send_message(embed=embed)

@bot.tree.command(name="sub", description="Substitute players in an active match")
@app_commands.describe(
        action="The type of substitution (swap or in)",
        player1="First player to substitute",
        player2="Second player to substitute"
    )
@app_commands.choices(action=[
        app_commands.Choice(name="Swap players between teams", value="swap"),
        app_commands.Choice(name="Sub a new player in", value="in")
    ])
async def sub_slash(interaction: discord.Interaction, action: str, player1: discord.Member,
                        player2: discord.Member):
        # Check if command is used in an allowed channel
        if not is_command_channel(interaction.channel):
            await interaction.response.send_message(
                f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
                ephemeral=True
            )
            return

        # Check if user has permission (match participant or admin)
        is_admin = interaction.user.guild_permissions.administrator
        channel_id = str(interaction.channel.id)

        # Get the active match in this channel
        active_match = match_system.get_active_match_by_channel(channel_id)

        if not active_match:
            await interaction.response.send_message("No active match found in this channel.", ephemeral=True)
            return

        # Check if the command user is in the match (or is an admin)
        user_id = str(interaction.user.id)
        team1_ids = [p["id"] for p in active_match["team1"]]
        team2_ids = [p["id"] for p in active_match["team2"]]

        is_participant = user_id in team1_ids or user_id in team2_ids

        if not (is_participant or is_admin):
            await interaction.response.send_message(
                "You must be a participant in this match or an admin to use this command.", ephemeral=True)
            return

        # Handle the "swap" action - swapping players between teams
        if action.lower() == "swap":
            player1_id = str(player1.id)
            player2_id = str(player2.id)

            # Check if both players are in the match but on different teams
            player1_in_team1 = player1_id in team1_ids
            player1_in_team2 = player1_id in team2_ids
            player2_in_team1 = player2_id in team1_ids
            player2_in_team2 = player2_id in team2_ids

            if not ((player1_in_team1 and player2_in_team2) or (player1_in_team2 and player2_in_team1)):
                await interaction.response.send_message("Both players must be in the match and on different teams.",
                                                        ephemeral=True)
                return

            # Execute the swap
            await interaction.response.defer()
            await swap_players(interaction, active_match, player1, player2)

        # Handle the "in" action - subbing a new player in
        elif action.lower() == "in":
            new_player_id = str(player1.id)
            out_player_id = str(player2.id)

            # Check if player_out is in the match
            out_in_team1 = out_player_id in team1_ids
            out_in_team2 = out_player_id in team2_ids

            if not (out_in_team1 or out_in_team2):
                await interaction.response.send_message(f"{player2.mention} is not in this match.", ephemeral=True)
                return

            # Check if new_player is already in the match
            if new_player_id in team1_ids or new_player_id in team2_ids:
                await interaction.response.send_message(f"{player1.mention} is already in this match.", ephemeral=True)
                return

            # Check if new player is eligible for this channel
            channel_name = interaction.channel.name.lower()
            if channel_name in ["rank-a", "rank-b", "rank-c"]:
                # For rank-specific channels, check if the new player has the appropriate role
                required_role = None
                if channel_name == "rank-a":
                    required_role = discord.utils.get(interaction.guild.roles, name="Rank A")
                elif channel_name == "rank-b":
                    required_role = discord.utils.get(interaction.guild.roles, name="Rank B")
                elif channel_name == "rank-c":
                    required_role = discord.utils.get(interaction.guild.roles, name="Rank C")

                if required_role and required_role not in player1.roles:
                    await interaction.response.send_message(
                        f"{player1.mention} doesn't have the {required_role.name} role required for this channel.",
                        ephemeral=True
                    )
                    return

            # Execute the substitution
            await interaction.response.defer()
            await sub_in_player(interaction, active_match, player1, player2)

        else:
            await interaction.response.send_message("Invalid action. Use 'swap' or 'in'.", ephemeral=True)

async def swap_players(interaction, match, player1, player2):
        """Swap two players between teams"""
        player1_id = str(player1.id)
        player2_id = str(player2.id)
        match_id = match["match_id"]

        # Get player indices
        team1_ids = [p["id"] for p in match["team1"]]
        team2_ids = [p["id"] for p in match["team2"]]

        player1_in_team1 = player1_id in team1_ids
        player1_index = team1_ids.index(player1_id) if player1_in_team1 else team2_ids.index(player1_id)
        player2_in_team1 = player2_id in team1_ids
        player2_index = team1_ids.index(player2_id) if player2_in_team1 else team2_ids.index(player2_id)

        # Get player data
        player1_data = match["team1"][player1_index] if player1_in_team1 else match["team2"][player1_index]
        player2_data = match["team1"][player2_index] if player2_in_team1 else match["team2"][player2_index]

        # Execute the swap in the database
        if player1_in_team1 and not player2_in_team1:  # player1 in team1, player2 in team2
            # Update team1 - replace player1 with player2
            match_system.matches.update_one(
                {"match_id": match_id},
                {"$set": {f"team1.{player1_index}": player2_data}}
            )
            # Update team2 - replace player2 with player1
            match_system.matches.update_one(
                {"match_id": match_id},
                {"$set": {f"team2.{player2_index}": player1_data}}
            )
        else:  # player1 in team2, player2 in team1
            # Update team2 - replace player1 with player2
            match_system.matches.update_one(
                {"match_id": match_id},
                {"$set": {f"team2.{player1_index}": player2_data}}
            )
            # Update team1 - replace player2 with player1
            match_system.matches.update_one(
                {"match_id": match_id},
                {"$set": {f"team1.{player2_index}": player1_data}}
            )

        # Create embed to announce the swap
        embed = discord.Embed(
            title="Player Swap",
            description=f"Players have been swapped between teams!",
            color=0x3498db
        )

        embed.add_field(name="Swapped Players",
                        value=f"{player1.mention} ⇄ {player2.mention}",
                        inline=False)

        # Get the updated match
        updated_match = match_system.matches.find_one({"match_id": match_id})

        # Format team mentions for display
        team1_mentions = [player['mention'] for player in updated_match["team1"]]
        team2_mentions = [player['mention'] for player in updated_match["team2"]]

        embed.add_field(name="Team 1", value=", ".join(team1_mentions), inline=False)
        embed.add_field(name="Team 2", value=", ".join(team2_mentions), inline=False)

        await interaction.followup.send(embed=embed)

async def sub_in_player(interaction, match, new_player, out_player):
        """Sub in a new player for an existing player"""
        new_player_id = str(new_player.id)
        out_player_id = str(out_player.id)
        match_id = match["match_id"]

        # Get player indices
        team1_ids = [p["id"] for p in match["team1"]]
        team2_ids = [p["id"] for p in match["team2"]]

        out_in_team1 = out_player_id in team1_ids
        out_index = team1_ids.index(out_player_id) if out_in_team1 else team2_ids.index(out_player_id)

        # Create new player data
        new_player_data = {
            "id": new_player_id,
            "name": new_player.display_name,
            "mention": new_player.mention
        }

        # Execute the substitution in the database
        if out_in_team1:  # player to replace is in team1
            match_system.matches.update_one(
                {"match_id": match_id},
                {"$set": {f"team1.{out_index}": new_player_data}}
            )
        else:  # player to replace is in team2
            match_system.matches.update_one(
                {"match_id": match_id},
                {"$set": {f"team2.{out_index}": new_player_data}}
            )

        # Create embed to announce the substitution
        embed = discord.Embed(
            title="Player Substitution",
            description=f"A player has been substituted!",
            color=0x3498db
        )

        embed.add_field(name="Substitution",
                        value=f"{new_player.mention} IN ↔ {out_player.mention} OUT",
                        inline=False)

        # Get the updated match
        updated_match = match_system.matches.find_one({"match_id": match_id})

        # Format team mentions for display
        team1_mentions = [player['mention'] for player in updated_match["team1"]]
        team2_mentions = [player['mention'] for player in updated_match["team2"]]

        embed.add_field(name="Team 1", value=", ".join(team1_mentions), inline=False)
        embed.add_field(name="Team 2", value=", ".join(team2_mentions), inline=False)

        await interaction.followup.send(embed=embed)

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
                "3. Vote by reacting to the vote message\n"
                "4. Teams will be created based on the vote results\n"
                "5. After the match, report the results with `/report <match_id> win` or `/report <match_id> loss`\n"
                "6. Check the leaderboard with `/leaderboard`"
            ),
            inline=False
        )

        embed.set_footer(text="Type /help <command> for more info on a specific command")

        await interaction.response.send_message(embed=embed)

    # Error handler
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
    bot.run(token, log_handler=handler, log_level=logging.DEBUG)