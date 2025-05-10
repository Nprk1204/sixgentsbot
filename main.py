import discord
from discord import app_commands
from discord.ext import commands
import logging
import datetime
import os
import asyncio
from threading import Thread
from flask import Flask
import requests
from dotenv import load_dotenv
from database import Database
from queue_handler import QueueHandler
from votesystem import VoteSystem
from captainssystem import CaptainsSystem
from matchsystem import MatchSystem
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
from system_coordinators import VoteSystemCoordinator, CaptainSystemCoordinator

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

# Use commands.Bot for registering slash commands
bot = commands.Bot(command_prefix='/', intents=intents, help_command=None)

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


# Helper functions for the slash commands
async def is_duplicate_command(ctx_or_interaction):
    """Thread-safe check if a command is a duplicate - works with both ctx and interaction"""
    if isinstance(ctx_or_interaction, discord.Interaction):
        user_id = ctx_or_interaction.user.id
        command_name = ctx_or_interaction.command.name if ctx_or_interaction.command else "unknown"
        channel_id = ctx_or_interaction.channel_id
        message_id = ctx_or_interaction.id
        timestamp = ctx_or_interaction.created_at.timestamp()
    else:
        user_id = ctx_or_interaction.author.id
        command_name = ctx_or_interaction.command.name if ctx_or_interaction.command else "unknown"
        channel_id = ctx_or_interaction.channel.id
        message_id = ctx_or_interaction.message.id
        timestamp = ctx_or_interaction.message.created_at.timestamp()

    # Add more detailed logging
    print(f"Command received: {command_name} from user ID: {user_id} (ID: {message_id})")

    # Use a more unique key that includes the message ID
    key = f"{user_id}:{command_name}:{channel_id}:{message_id}"

    # Use lock to prevent race conditions
    async with command_lock:
        # Check if we've seen this exact message ID before
        # This ensures we only detect true duplicates, not repeat attempts
        if key in recent_commands:
            print(f"DUPLICATE FOUND: {command_name} from user ID: {user_id} (ID: {message_id})")
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


# Helper function to check if command is used in an allowed channel
def is_command_channel(channel_name):
    """Check if the command is being used in a general command channel"""
    allowed_channels = ["rank-a", "rank-b", "rank-c", "global", "sixgents"]
    return channel_name.lower() in allowed_channels


def is_queue_channel(channel_name):
    """Check if the command is being used in a queue-allowed channel"""
    allowed_channels = ["rank-a", "rank-b", "rank-c", "global"]
    return channel_name.lower() in allowed_channels


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

    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

    print(f"BOT INSTANCE ACTIVE - {datetime.datetime.now(datetime.UTC)}")


@bot.event
async def on_reaction_add(reaction, user):
    """Handle reactions for voting"""
    if user.bot:
        return  # Ignore bot reactions

    # Pass to vote system to handle
    await vote_system.handle_reaction(reaction, user)


# Convert all commands to slash commands
# Queue commands
@bot.tree.command(name="join", description="Join the queue for 6 mans")
async def join(interaction: discord.Interaction):
    """Join the queue for 6 mans"""
    # Check if this is a duplicate command
    if await is_duplicate_command(interaction):
        await interaction.response.send_message(
            "This command was already processed. Please avoid duplicate submissions.", ephemeral=True)
        return

    # Check if command is used in an allowed channel
    if not is_queue_channel(interaction.channel.name):
        await interaction.response.send_message(
            f"This command can only be used in the rank-a, rank-b, rank-c, or global channels.",
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

    # Continue with regular join process
    channel_id = interaction.channel_id
    response = queue_handler.add_player(player, channel_id)
    await interaction.response.send_message(response)

    # Check if queue is full and start voting
    players = queue_handler.get_players_for_match(channel_id)
    if len(players) >= 6:
        # Check if voting is already active for this channel
        if not vote_system.is_voting_active(channel_id):
            await vote_system.start_vote(interaction.channel)


@bot.tree.command(name="leave", description="Leave the queue")
async def leave(interaction: discord.Interaction):
    """Leave the queue"""
    # Check if this is a duplicate command
    if await is_duplicate_command(interaction):
        await interaction.response.send_message(
            "This command was already processed. Please avoid duplicate submissions.", ephemeral=True)
        return

    # Check if command is used in an allowed channel
    if not is_queue_channel(interaction.channel.name):
        await interaction.response.send_message(
            f"This command can only be used in the rank-a, rank-b, rank-c, or global channels.",
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

    channel_id = str(interaction.channel_id)

    # DEBUG - Before leaving
    print(f"DEBUG - LEAVE ATTEMPT: {interaction.user.name} trying to leave queue in channel {channel_id}")

    # Find if the player is in ANY queue first
    any_queue = queue_handler.queue_collection.find_one({"id": player_id})
    if not any_queue:
        await interaction.response.send_message(f"{player_mention} is not in any queue!")
        return

    # Now check if the player is in THIS specific channel's queue
    channel_queue = queue_handler.queue_collection.find_one({"id": player_id, "channel_id": channel_id})
    if not channel_queue:
        other_channel_id = any_queue.get("channel_id")
        if other_channel_id and other_channel_id.isdigit():
            await interaction.response.send_message(
                f"{player_mention} is not in this channel's queue. You are in <#{other_channel_id}>'s queue."
            )
        else:
            await interaction.response.send_message(f"{player_mention} is in another channel's queue, not this one.")
        return

    # Check if voting is active in this channel
    if vote_system.is_voting_active(channel_id):
        await interaction.response.send_message(f"{player_mention} cannot leave the queue while voting is in progress!")
        return

    # Delete the player from THIS channel's queue
    result = queue_handler.queue_collection.delete_one({"id": player_id, "channel_id": channel_id})

    # Check if captain selection is active in this channel
    if captains_system.is_selection_active(channel_id):
        captains_system.cancel_selection(channel_id)

    if result.deleted_count > 0:
        await interaction.response.send_message(f"{player_mention} has left the queue!")
    else:
        await interaction.response.send_message(f"Error removing {player_mention} from the queue. Please try again.")

    # DEBUG - After leaving
    print("DEBUG - AFTER LEAVE: Current queue state:")
    all_queued = list(queue_handler.queue_collection.find())
    for p in all_queued:
        print(f"Player: {p.get('name')}, Channel: {p.get('channel_id')}")


@bot.tree.command(name="status", description="Shows the current queue status")
async def status(interaction: discord.Interaction):
    """Shows the current queue status"""
    # Check if this is a duplicate command
    if await is_duplicate_command(interaction):
        await interaction.response.send_message(
            "This command was already processed. Please avoid duplicate submissions.", ephemeral=True)
        return

    # Check if command is used in an allowed channel
    if not is_queue_channel(interaction.channel.name):
        await interaction.response.send_message(
            f"This command can only be used in the rank-a, rank-b, rank-c, or global channels.",
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

    channel_id = str(interaction.channel_id)

    # Get all players in this channel's queue directly from the database
    # Use queue_collection instead of queue
    players = list(queue_handler.queue_collection.find({"channel_id": channel_id}))
    count = len(players)

    # Create an embed
    embed = discord.Embed(
        title="Queue Status",
        description=f"**Current Queue: {count}/6 players**",
        color=0x3498db
    )

    if count == 0:
        embed.add_field(name="Status", value="Queue is empty! Use `/join` to join the queue.", inline=False)
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


@bot.tree.command(name="adjustmmr", description="Admin command to adjust a player's MMR")
@app_commands.describe(
    member="The player to adjust MMR for",
    amount="The amount of MMR to add or subtract"
)
async def adjustmmr(interaction: discord.Interaction, member: discord.Member, amount: int):
    """Admin command to adjust a player's MMR"""
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

    # Defer response since this might take a moment
    await interaction.response.defer()

    # Get player data from database
    player_data = match_system.players.find_one({"id": player_id})

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

    await interaction.followup.send(embed=embed)

    # Update their Discord role based on new MMR
    try:
        await match_system.update_discord_role(interaction, player_id, new_mmr)
    except Exception as e:
        print(f"Error updating Discord role: {str(e)}")
        await interaction.followup.send(f"Note: Could not update Discord role. Error: {str(e)}")


# Match commands
@bot.tree.command(name="report", description="Report match results")
@app_commands.describe(
    match_id="The ID of the match to report",
    result="Whether you won or lost the match"
)
@app_commands.choices(result=[
    app_commands.Choice(name="Win", value="win"),
    app_commands.Choice(name="Loss", value="loss")
])
async def report(interaction: discord.Interaction, match_id: str, result: str):
    """Report match results"""
    # Check if this is a duplicate command
    if await is_duplicate_command(interaction):
        await interaction.response.send_message(
            "This command was already processed. Please avoid duplicate submissions.", ephemeral=True)
        return

    # Check if command is used in an allowed channel
    if not is_command_channel(interaction.channel.name):
        await interaction.response.send_message(
            f"This command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
            ephemeral=True
        )
        return

    reporter_id = str(interaction.user.id)

    # Defer response since this might take a moment
    await interaction.response.defer()

    # Get match result
    match_result, error = await match_system.report_match_by_id(match_id, reporter_id, result, interaction)

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

    # Get MMR changes for display
    winning_team_mmr_changes = []
    for player in winning_team:
        player_id = player["id"]
        if player_id.startswith('9000'):  # Skip dummy players
            winning_team_mmr_changes.append("+0")
            continue

        player_data = match_system.players.find_one({"id": player_id})
        if player_data:
            matches_played = player_data.get("matches", 0)
            mmr_change = match_system.calculate_dynamic_mmr(matches_played, is_win=True)
            winning_team_mmr_changes.append(f"+{mmr_change}")
        else:
            winning_team_mmr_changes.append("+??")

    losing_team_mmr_changes = []
    for player in losing_team:
        player_id = player["id"]
        if player_id.startswith('9000'):  # Skip dummy players
            losing_team_mmr_changes.append("-0")
            continue

        player_data = match_system.players.find_one({"id": player_id})
        if player_data:
            matches_played = player_data.get("matches", 0)
            mmr_change = match_system.calculate_dynamic_mmr(matches_played, is_win=False)
            losing_team_mmr_changes.append(f"-{mmr_change}")
        else:
            losing_team_mmr_changes.append("-??")

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
    match_id="Optional: The ID of the match to report"
)
@app_commands.choices(
    team_number=[
        app_commands.Choice(name="Team 1", value=1),
        app_commands.Choice(name="Team 2", value=2)
    ],
    result=[
        app_commands.Choice(name="Win", value="win")
    ]
)
async def adminreport(interaction: discord.Interaction, team_number: int, result: str, match_id: str = None):
    """Admin command to report match results"""
    # Check if this is a duplicate command
    if await is_duplicate_command(interaction):
        await interaction.response.send_message(
            "This command was already processed. Please avoid duplicate submissions.", ephemeral=True)
        return

    # Check if command is used in an allowed channel
    if not is_command_channel(interaction.channel.name):
        await interaction.response.send_message(
            f"This command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
            ephemeral=True
        )
        return

    # Check if user has admin permissions
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need administrator permissions to use this command.",
                                                ephemeral=True)
        return

    # Validate result argument
    if result.lower() != "win":
        await interaction.response.send_message("Invalid result. Please use 'win' to indicate the winning team.",
                                                ephemeral=True)
        return

    # Defer response since this might take a moment
    await interaction.response.defer()

    channel_id = str(interaction.channel_id)

    # Find the active match either by ID or channel
    if match_id:
        active_match = match_system.matches.find_one({"match_id": match_id, "status": "in_progress"})
        if not active_match:
            await interaction.followup.send(f"No active match found with ID `{match_id}`.")
            return
    else:
        # Otherwise try to find match in current channel
        active_match = match_system.get_active_match_by_channel(channel_id)
        if not active_match:
            await interaction.followup.send(
                "No active match found in this channel. Please report in the channel where the match was created or provide a match ID."
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

        await interaction.followup.send(embed=embed)

        # Also send a message encouraging people to check the leaderboard
        await interaction.followup.send("Check the updated leaderboard with `/leaderboard`!")

    @bot.tree.command(name="leaderboard", description="Shows a link to the leaderboard website")
    async def leaderboard(interaction: discord.Interaction):
        """Shows a link to the leaderboard website"""
        # Check if this is a duplicate command
        if await is_duplicate_command(interaction):
            await interaction.response.send_message(
                "This command was already processed. Please avoid duplicate submissions.", ephemeral=True)
            return

        # Check if command is used in an allowed channel
        if not is_command_channel(interaction.channel.name):
            await interaction.response.send_message(
                f"This command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
                ephemeral=True
            )
            return

        # Replace this URL with your actual leaderboard website URL from Render
        leaderboard_url = "https://sixgentsbot-1.onrender.com"

        embed = discord.Embed(
            title="🏆 Rocket League 6 Gents Leaderboard 🏆",
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
    @app_commands.describe(member="The member to check stats for (default: yourself)")
    async def rank(interaction: discord.Interaction, member: discord.Member = None):
        """Check your rank and stats (or another member's)"""
        # Check if this is a duplicate command
        if await is_duplicate_command(interaction):
            await interaction.response.send_message(
                "This command was already processed. Please avoid duplicate submissions.", ephemeral=True)
            return

        # Check if command is used in an allowed channel
        if not is_command_channel(interaction.channel.name):
            await interaction.response.send_message(
                f"This command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
                ephemeral=True
            )
            return

        if member is None:
            member = interaction.user

        player_id = str(member.id)
        player_data = match_system.get_player_stats(player_id)

        if not player_data:
            await interaction.response.send_message(f"{member.mention} hasn't played any matches yet.")
            return

        # Calculate stats
        mmr = player_data.get("mmr", 1000)
        wins = player_data.get("wins", 0)
        losses = player_data.get("losses", 0)
        matches = player_data.get("matches", 0)

        win_rate = 0
        if matches > 0:
            win_rate = (wins / matches) * 100

        # Get player's rank position
        all_players = list(match_system.players.find().sort("mmr", -1))

        # Get the position using the player's ID
        rank_position = None
        for i, p in enumerate(all_players):
            if p["id"] == player_id:
                rank_position = i + 1
                break

        if rank_position is None:
            rank_position = "Unknown"

        # Create embed
        embed = discord.Embed(
            title=f"Stats for {member.display_name}",
            color=member.color
        )

        embed.add_field(name="Rank", value=f"#{rank_position} of {len(all_players)}", inline=True)
        embed.add_field(name="MMR", value=str(mmr), inline=True)
        embed.add_field(name="Win Rate", value=f"{win_rate:.1f}%", inline=True)
        embed.add_field(name="Record", value=f"{wins}W - {losses}L", inline=True)
        embed.add_field(name="Matches", value=str(matches), inline=True)

        if member.avatar:
            embed.set_thumbnail(url=member.avatar.url)
        elif member.default_avatar:
            embed.set_thumbnail(url=member.default_avatar.url)

        embed.set_footer(text="Stats updated after each match")

        await interaction.response.send_message(embed=embed)

    # Admin commands
    @bot.tree.command(name="clearqueue", description="Clear all players from the queue (Admin only)")
    async def clearqueue(interaction: discord.Interaction):
        """Clear all players from the queue (Admin only)"""
        # Check if this is a duplicate command
        if await is_duplicate_command(interaction):
            await interaction.response.send_message(
                "This command was already processed. Please avoid duplicate submissions.", ephemeral=True)
            return

        # Check if command is used in an allowed channel
        if not is_command_channel(interaction.channel.name):
            await interaction.response.send_message(
                f"This command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
                ephemeral=True
            )
            return

        # Check if user has admin permissions
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You need administrator permissions to use this command.",
                                                    ephemeral=True)
            return

        # Get current players in queue
        channel_id = str(interaction.channel_id)
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

    @bot.tree.command(name="resetleaderboard", description="Reset the leaderboard (Admin only)")
    @app_commands.describe(
        confirmation="Type 'confirm' to execute the reset after seeing the warning"
    )
    async def resetleaderboard(interaction: discord.Interaction, confirmation: str = None):
        """Reset the leaderboard (Admin only)"""
        # Check if command is used in an allowed channel
        if not is_command_channel(interaction.channel.name):
            await interaction.response.send_message(
                f"This command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
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
                description="This will reset MMR, stats, rank data, match history and **remove all rank roles**. This action cannot be undone!",
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
                await interaction.response.send_message(
                    "Please run `/resetleaderboard` first to see the warning before confirming.")
                return

            # Remove the confirmation once used
            if user_id in bot.reset_confirmations:
                del bot.reset_confirmations[user_id]

            # Defer the response since this will take a while
            await interaction.response.defer()

            try:
                # Initialize variables that will be used throughout the function
                web_reset = "⚠️ Web reset not attempted"
                verification_reset = "⚠️ Verification reset not attempted"

                # Check multiple collections to determine if truly empty
                db = match_system.players.database
                player_count = match_system.players.count_documents({})
                match_count = db['matches'].count_documents({})
                rank_count = db['ranks'].count_documents({})

                total_documents = player_count + match_count + rank_count

                # Create backup collections with timestamp
                timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d_%H%M%S")

                # Track what was backed up for reporting
                backed_up = []

                # Backup and reset players
                if player_count > 0:
                    backup_collection_name = f"players_backup_{timestamp}"
                    db.create_collection(backup_collection_name)
                    backup_collection = db[backup_collection_name]
                    for player in match_system.players.find():
                        backup_collection.insert_one(player)
                    match_system.players.delete_many({})
                    backed_up.append(f"Players ({player_count})")

                # Backup and reset matches
                matches_collection = db['matches']
                if match_count > 0:
                    backup_collection_name = f"matches_backup_{timestamp}"
                    db.create_collection(backup_collection_name)
                    backup_collection = db[backup_collection_name]
                    for match in matches_collection.find():
                        backup_collection.insert_one(match)
                    matches_collection.delete_many({})
                    backed_up.append(f"Matches ({match_count})")

                # Backup and reset ranks
                ranks_collection = db['ranks']
                if rank_count > 0:
                    backup_collection_name = f"ranks_backup_{timestamp}"
                    db.create_collection(backup_collection_name)
                    backup_collection = db[backup_collection_name]
                    for rank in ranks_collection.find():
                        backup_collection.insert_one(rank)
                    ranks_collection.delete_many({})
                    backed_up.append(f"Ranks ({rank_count})")

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
                resets_collection = db['resets']
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

            except Exception as e:
                await interaction.followup.send(f"Error resetting leaderboard: {str(e)}")
        else:
            await interaction.response.send_message(
                "Invalid confirmation. Use `/resetleaderboard` to see instructions.")
            return

    # Add this helper function for the resetleaderboard command
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

    @bot.tree.command(name="forcestart", description="Force start the team selection process (Admin only)")
    async def forcestart(interaction: discord.Interaction):
        """Force start the team selection process (Admin only)"""
        # Check if this is a duplicate command
        if await is_duplicate_command(interaction):
            await interaction.response.send_message(
                "This command was already processed. Please avoid duplicate submissions.", ephemeral=True)
            return

        # Check if command is used in an allowed channel
        if not is_queue_channel(interaction.channel.name):
            await interaction.response.send_message(
                f"This command can only be used in the rank-a, rank-b, rank-c, or global channels.",
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
        channel_id = str(interaction.channel_id)
        players = queue_handler.get_players_for_match(channel_id)
        player_count = len(players)

        if player_count == 0:
            await interaction.response.send_message("Can't force start: Queue is empty!")
            return

        # If fewer than 6 players, add dummy players to reach 6
        if player_count < 6:
            # Create dummy players to fill the queue
            needed = 6 - player_count
            await interaction.response.send_message(f"Adding {needed} dummy players to fill the queue for testing...")

            for i in range(needed):
                # Use numeric IDs starting from 9000 to prevent parsing issues
                dummy_id = f"9000{i + 1}"  # 90001, 90002, etc
                dummy_name = f"TestPlayer{i + 1}"
                dummy_mention = f"@TestPlayer{i + 1}"

                # Add dummy player to queue
                queue_handler.queue_collection.insert_one({
                    "id": dummy_id,
                    "name": dummy_name,
                    "mention": dummy_mention,
                    "channel_id": channel_id
                })

        # Force start the vote
        await interaction.response.send_message("**Force starting team selection!**")
        await vote_system.start_vote(interaction.channel)

    @bot.tree.command(name="forcestop",
                      description="Force stop any active votes or selections and clear the queue (Admin only)")
    async def forcestop(interaction: discord.Interaction):
        """Force stop any active votes or selections and clear the queue (Admin only)"""
        # Check if this is a duplicate command
        if await is_duplicate_command(interaction):
            await interaction.response.send_message(
                "This command was already processed. Please avoid duplicate submissions.", ephemeral=True)
            return

        # Check if command is used in an allowed channel
        if not is_command_channel(interaction.channel.name):
            await interaction.response.send_message(
                f"This command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
                ephemeral=True
            )
            return

        # Check if user has admin permissions
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You need administrator permissions to use this command.",
                                                    ephemeral=True)
            return

        # Get current players in queue
        channel_id = str(interaction.channel_id)
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

    @bot.tree.command(name="helpme", description="Display help information")
    async def helpme(interaction: discord.Interaction):
        """Display help information"""
        # Check if this is a duplicate command
        if await is_duplicate_command(interaction):
            await interaction.response.send_message(
                "This command was already processed. Please avoid duplicate submissions.", ephemeral=True)
            return

        # Check if command is used in an allowed channel
        if not is_command_channel(interaction.channel.name):
            await interaction.response.send_message(
                f"This command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="Rocket League 6 Mans Bot",
            description="Commands for the 6 mans queue system:",
            color=0x00ff00
        )

        embed.add_field(name="/join", value="Join the queue (rank-a, rank-b, rank-c, global channels only)",
                        inline=False)
        embed.add_field(name="/leave", value="Leave the queue (rank-a, rank-b, rank-c, global channels only)",
                        inline=False)
        embed.add_field(name="/status",
                        value="Show the current queue status (rank-a, rank-b, rank-c, global channels only)",
                        inline=False)
        embed.add_field(name="/report <match_id> <win/loss>", value="Report match results", inline=False)
        embed.add_field(name="/leaderboard", value="View the leaderboard website", inline=False)
        embed.add_field(name="/rank [member]", value="Show your rank or another member's rank", inline=False)

        embed.add_field(
            name="How it works:",
            value=(
                "1. Join the queue with `/join` in a rank channel\n"
                "2. When 6 players join, voting starts automatically\n"
                "3. Vote by reacting to the vote message\n"
                "4. Teams will be created based on the vote results\n"
                "5. After the match, report the results with `/report <match_id> win` or `/report <match_id> loss`\n"
                "6. Check the leaderboard with `/leaderboard`"
            ),
            inline=False
        )

        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="ping", description="Simple ping command to check bot connection")
    async def ping(interaction: discord.Interaction):
        """Simple ping command that doesn't use MongoDB"""
        # Check if this is a duplicate command
        if await is_duplicate_command(interaction):
            await interaction.response.send_message(
                "This command was already processed. Please avoid duplicate submissions.", ephemeral=True)
            return

        # Check if command is used in an allowed channel
        if not is_command_channel(interaction.channel.name):
            await interaction.response.send_message(
                f"This command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
                ephemeral=True
            )
            return

        # Calculate ping
        latency = round(bot.latency * 1000)  # Convert to ms

        embed = discord.Embed(
            title="🏓 Pong!",
            description=f"Bot is connected to Discord.\nLatency: {latency}ms",
            color=0x00ff00
        )

        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="purgechat", description="Clear chat messages (Admin only)")
    @app_commands.describe(amount_to_delete="Number of messages to delete (1-100)")
    async def purgechat(interaction: discord.Interaction, amount_to_delete: int = 10):
        """Clear chat messages"""
        # Check if this is a duplicate command
        if await is_duplicate_command(interaction):
            await interaction.response.send_message(
                "This command was already processed. Please avoid duplicate submissions.", ephemeral=True)
            return

        # Check if command is used in an allowed channel
        if not is_command_channel(interaction.channel.name):
            await interaction.response.send_message(
                f"This command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
                ephemeral=True
            )
            return

        if interaction.user.guild_permissions.manage_messages:
            if 1 <= amount_to_delete <= 100:
                await interaction.response.defer(ephemeral=True)

                # Delete messages
                deleted = await interaction.channel.purge(limit=amount_to_delete)

                await interaction.followup.send(f"Cleared {len(deleted)} messages.", ephemeral=True)
            else:
                await interaction.response.send_message("Please enter a number between 1 and 100", ephemeral=True)
        else:
            await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)

    @bot.tree.command(name="removelastmatch", description="Remove the results of the last match or a specific match")
    @app_commands.describe(
        match_id="The ID of the match to remove (optional, will find the latest match if not provided)",
        confirm="Type 'confirm' to execute the removal after checking match details"
    )
    async def removelastmatch(interaction: discord.Interaction, match_id: str = None, confirm: str = None):
        """Remove the results of a match (Admin only)"""
        # Check if this is a duplicate command
        if await is_duplicate_command(interaction):
            await interaction.response.send_message(
                "This command was already processed. Please avoid duplicate submissions.", ephemeral=True)
            return

        # Check if command is used in an allowed channel
        if not is_command_channel(interaction.channel.name):
            await interaction.response.send_message(
                f"This command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
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

        # Defer response since this might take a bit
        await interaction.response.defer()

        # CASE 1: No match_id provided - Find the most recent match
        if match_id is None:
            # Find the most recent completed match
            match = match_system.matches.find_one(
                {"status": "completed"},
                sort=[("completed_at", -1)]  # Sort by completion time, most recent first
            )

            if not match:
                await interaction.followup.send("No completed matches found to remove.")
                return

            # Display match information
            match_id = match['match_id']

            # Create confirmation message
            embed = create_match_confirmation_embed(match, match_id)

            # Track that we showed this match information
            current_time = datetime.datetime.now(datetime.UTC).timestamp()
            bot.remove_confirmations[match_id] = current_time

            await interaction.followup.send(embed=embed)
            return

        # CASE 2: Match ID provided but no confirmation - Show match details
        elif confirm is None:
            # Find the match
            match = match_system.matches.find_one({"match_id": match_id, "status": "completed"})
            if not match:
                await interaction.followup.send(f"No completed match found with ID `{match_id}`.")
                return

            # Create confirmation message
            embed = create_match_confirmation_embed(match, match_id)

            # Track that we showed this match information
            current_time = datetime.datetime.now(datetime.UTC).timestamp()
            bot.remove_confirmations[match_id] = current_time

            await interaction.followup.send(embed=embed)
            return

            # CASE 3: Match ID and confirmation provided - Process the removal
        elif confirm.lower() == "confirm":
            # Check if the match ID has been confirmed within the last 5 minutes
            current_time = datetime.datetime.now(datetime.UTC).timestamp()
            confirmation_time = bot.remove_confirmations.get(match_id, 0)

            if current_time - confirmation_time > 300 or confirmation_time == 0:  # 5 minutes expiration
                await interaction.followup.send(
                    f"Please run `/removelastmatch {match_id}` first to see the warning before confirming.")
                return

            # Remove the confirmation once used
            if match_id in bot.remove_confirmations:
                del bot.remove_confirmations[match_id]

            # Find the match
            match = match_system.matches.find_one({"match_id": match_id, "status": "completed"})
            if not match:
                await interaction.followup.send(f"No completed match found with ID `{match_id}`.")
                return

            # Execute the actual removal
            removal_result = await remove_match_results(interaction, match)
            await interaction.followup.send(embed=removal_result)

        # CASE 4: Invalid confirmation text
        else:
            await interaction.followup.send(
                "Invalid confirmation. Use `/removelastmatch {match_id}` to see instructions.")

    # Helper functions for removelastmatch command
    def create_match_confirmation_embed(match, match_id):
        """Create an embed for match removal confirmation"""
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
            reporter_name = f"Unknown (ID: {reporter_id})"

            # This is safe since it doesn't rely on bot.get_guild which might not be available
            if 'guild_id' in match:
                try:
                    guild = bot.get_guild(int(match.get('guild_id', 0)))
                    if guild:
                        member = guild.get_member(int(reporter_id))
                        if member:
                            reporter_name = member.display_name
                except:
                    pass

            embed.add_field(
                name="Reported By",
                value=reporter_name,
                inline=False
            )

        embed.add_field(
            name="To confirm:",
            value=f"Type `/removelastmatch {match_id} confirm`",
            inline=False
        )

        embed.add_field(
            name="Warning",
            value="This will revert MMR changes and could affect player rankings. This action cannot be undone!",
            inline=False
        )

        return embed

    async def remove_match_results(interaction, match):
        """Process the actual removal of match results using stored MMR values"""
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

                # These values are already stored in the match document
                old_mmr = change_data.get('old_mmr', 0)
                new_mmr = change_data.get('new_mmr', 0)
                mmr_change = change_data.get('mmr_change', 0)
                is_win = change_data.get('is_win', False)

                # Update player's MMR back to the old value
                current_mmr = player_data.get('mmr', 0)

                # Update wins/losses and matches count
                current_matches = player_data.get('matches', 0)
                new_matches = max(0, current_matches - 1)

                if is_win:
                    current_wins = player_data.get('wins', 0)
                    new_wins = max(0, current_wins - 1)
                    new_losses = player_data.get('losses', 0)
                else:
                    current_losses = player_data.get('losses', 0)
                    new_losses = max(0, current_losses - 1)
                    new_wins = player_data.get('wins', 0)

                # Update database - restore MMR to original value
                match_system.players.update_one(
                    {"id": player_id},
                    {"$set": {
                        "mmr": old_mmr,
                        "wins": new_wins,
                        "losses": new_losses,
                        "matches": new_matches,
                        "last_updated": datetime.datetime.now(datetime.UTC)
                    }}
                )

                # Track change for reporting
                mmr_changes.append({
                    "player": player['name'],
                    "old_mmr": current_mmr,
                    "new_mmr": old_mmr,
                    "change": f"{mmr_change}" if mmr_change > 0 else f"{mmr_change}"  # Already includes sign
                })

                # Update Discord role based on new MMR
                try:
                    await match_system.update_discord_role(interaction, player_id, old_mmr)
                except Exception as e:
                    print(f"Error updating Discord role for {player['name']}: {str(e)}")
            else:
                # Fall back to approximation if we don't have stored data
                # (This should rarely happen if your report_match_by_id is storing MMR changes)
                is_winner = player in winning_team

                if is_winner:
                    # Use the dynamic calculation for winners
                    matches_played = player_data.get("matches", 0)
                    if matches_played > 0:
                        matches_played_before = matches_played - 1
                        mmr_change = match_system.calculate_dynamic_mmr(matches_played_before, is_win=True)

                        # Revert MMR
                        current_mmr = player_data.get("mmr", 0)
                        new_mmr = max(0, current_mmr - mmr_change)  # Don't go below 0

                        # Update wins and matches count
                        current_wins = player_data.get("wins", 0)
                        new_wins = max(0, current_wins - 1)  # Don't go below 0
                        new_matches = max(0, matches_played - 1)  # Don't go below 0

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

                        # Update Discord role based on new MMR
                        try:
                            await match_system.update_discord_role(interaction, player_id, new_mmr)
                        except Exception as e:
                            print(f"Error updating Discord role for {player['name']}: {str(e)}")
                else:
                    # Use the dynamic calculation for losers
                    matches_played = player_data.get("matches", 0)
                    if matches_played > 0:
                        matches_played_before = matches_played - 1
                        mmr_change = match_system.calculate_dynamic_mmr(matches_played_before, is_win=False)

                        # Revert MMR
                        current_mmr = player_data.get("mmr", 0)
                        new_mmr = current_mmr + mmr_change

                        # Update losses and matches count
                        current_losses = player_data.get("losses", 0)
                        new_losses = max(0, current_losses - 1)  # Don't go below 0
                        new_matches = max(0, matches_played - 1)  # Don't go below 0

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

                        # Update Discord role based on new MMR
                        try:
                            await match_system.update_discord_role(interaction, player_id, new_mmr)
                        except Exception as e:
                            print(f"Error updating Discord role for {player['name']}: {str(e)}")

        # Update match status
        match_system.matches.update_one(
            {"match_id": match_id},
            {"$set": {
                "status": "removed",
                "removed_at": datetime.datetime.now(datetime.UTC),
                "removed_by": str(interaction.user.id)
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

        embed.set_footer(text=f"Removed by {interaction.user.display_name}")

        return embed

    # Then run the bot
    bot.run(token, log_handler=handler, log_level=logging.DEBUG)