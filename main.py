import discord
import requests
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
            now = datetime.datetime.now().timestamp()
            # Only keep commands from last 5 minutes
            old_size = len(recent_commands)
            current_records = recent_commands.copy()
            recent_commands.clear()
            recent_commands.update({k: v for k, v in current_records.items() if now - v < 300})
            print(f"Cleaned command cache: {old_size} → {len(recent_commands)} entries")

    return False


# Helper function to check if command is used in a queue-specific channel
def is_queue_channel(ctx):
    """Check if the command is being used in a queue-allowed channel"""
    allowed_channels = ["rank-a", "rank-b", "rank-c", "global"]
    return ctx.channel.name.lower() in allowed_channels


# Helper function to check if command is used in a general command channel
def is_command_channel(ctx):
    """Check if the command is being used in a general command channel"""
    allowed_channels = ["rank-a", "rank-b", "rank-c", "global", "sixgents"]
    return ctx.channel.name.lower() in allowed_channels


def get_queue_status(self, channel_id):
    """Get the current status of a specific channel's queue"""
    channel_id = str(channel_id)

    # Get all players currently in this channel's queue
    players = list(self.queue_collection.find({"channel_id": channel_id}))
    count = len(players)

    # Create an embed instead of plain text
    embed = discord.Embed(
        title="Queue Status",
        description=f"**Current Queue: {count}/6 players**",
        color=0x3498db
    )

    if count == 0:
        embed.add_field(name="Status", value="Queue is empty! Use `/join` to join the queue.", inline=False)
        return embed

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
        # Check vote and captain status
        embed.add_field(name="Status", value="**Queue is FULL!** Use `/status` to check status.", inline=False)

    return embed


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

    print(f"BOT INSTANCE ACTIVE - {datetime.datetime.now()}")


@bot.event
async def on_reaction_add(reaction, user):
    """Handle reactions for voting"""
    if user.bot:
        return  # Ignore bot reactions

    # Pass to vote system to handle
    await vote_system.handle_reaction(reaction, user)


# Queue commands
@bot.command()
async def join(ctx):
    """Join the queue for 6 mans"""
    # Check if this is a duplicate command
    if await is_duplicate_command(ctx):
        return

    # Check if command is used in an allowed channel
    if not is_queue_channel(ctx):
        await ctx.send(
            f"{ctx.author.mention}, this command can only be used in the rank-a, rank-b, rank-c, or global channels.")
        return

    # Check if the player has completed rank verification
    player = ctx.author
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
        await ctx.send(embed=embed)
        return

    # Check if the player has the right role for this channel
    channel_name = ctx.channel.name.lower()
    if channel_name in ["rank-a", "rank-b", "rank-c"]:
        required_role_name = f"Rank {channel_name[-1].upper()}"
        required_role = discord.utils.get(ctx.guild.roles, name=required_role_name)

        if required_role and required_role not in player.roles:
            await ctx.send(
                f"{player.mention}, you need the {required_role.name} role to join this queue. Please join the appropriate queue for your rank.")
            return

    # Continue with regular join process
    channel_id = ctx.channel.id
    response = queue_handler.add_player(player, channel_id)
    await ctx.send(response)

    # Check if queue is full and start voting
    players = queue_handler.get_players_for_match(channel_id)
    if len(players) >= 6:
        # Check if voting is already active for this channel
        if not vote_system.is_voting_active(channel_id):
            await vote_system.start_vote(ctx.channel)


@bot.command()
async def leave(ctx):
    """Leave the queue"""
    # Check if this is a duplicate command
    if await is_duplicate_command(ctx):
        return

    # Check if command is used in an allowed channel
    if not is_queue_channel(ctx):
        await ctx.send(
            f"{ctx.author.mention}, this command can only be used in the rank-a, rank-b, rank-c, or global channels.")
        return

    # Check if the player has completed rank verification
    player = ctx.author
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
        await ctx.send(embed=embed)
        return

    channel_id = str(ctx.channel.id)
    player_id = str(ctx.author.id)
    player_mention = ctx.author.mention

    # DEBUG - Before leaving
    print(f"DEBUG - LEAVE ATTEMPT: {ctx.author.name} trying to leave queue in channel {channel_id}")

    # Find if the player is in THIS specific channel's queue
    existing_queue = queue_handler.queue_collection.find_one({"id": player_id, "channel_id": channel_id})

    if not existing_queue:
        # Check if they're in any other channel's queue
        other_queue = queue_handler.queue_collection.find_one({"id": player_id})
        if other_queue:
            other_channel_id = other_queue.get("channel_id")
            if other_channel_id and other_channel_id.isdigit():
                await ctx.send(
                    f"{player_mention} is not in this channel's queue. You are in <#{other_channel_id}>'s queue.")
            else:
                await ctx.send(f"{player_mention} is in another channel's queue, not this one.")
        else:
            await ctx.send(f"{player_mention} is not in any queue!")
        return

    # Delete the player from THIS channel's queue
    result = queue_handler.queue_collection.delete_one({"id": player_id, "channel_id": channel_id})

    # Check if voting is active in this channel
    if vote_system.is_voting_active(channel_id):
        vote_system.cancel_voting(channel_id)

    # Check if captain selection is active in this channel
    if captains_system.is_selection_active(channel_id):
        captains_system.cancel_selection(channel_id)

    if result.deleted_count > 0:
        await ctx.send(f"{player_mention} has left the queue!")
    else:
        await ctx.send(f"Error removing {player_mention} from the queue. Please try again.")

    # DEBUG - After leaving
    print("DEBUG - AFTER LEAVE: Current queue state:")
    all_queued = list(queue_handler.queue_collection.find())
    for p in all_queued:
        print(f"Player: {p.get('name')}, Channel: {p.get('channel_id')}")


@bot.command()
async def status(ctx):
    """Shows the current queue status"""
    # Check if this is a duplicate command
    if await is_duplicate_command(ctx):
        return

    # Check if command is used in an allowed channel
    if not is_queue_channel(ctx):
        await ctx.send(
            f"{ctx.author.mention}, this command can only be used in the rank-a, rank-b, rank-c, or global channels.")
        return

    # Check if the player has completed rank verification
    player = ctx.author
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
        await ctx.send(embed=embed)
        return

    channel_id = str(ctx.channel.id)

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
        await ctx.send(embed=embed)
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

    await ctx.send(embed=embed)

    # If queue is full, check if we should start voting
    if count >= 6:
        channel_name = ctx.channel.name.lower()
        if channel_name in ["rank-a", "rank-b", "rank-c", "global"]:
            # Check if voting is already active for this channel
            if not vote_system.is_voting_active(channel_id):
                await vote_system.start_vote(ctx.channel)


@bot.command()
async def adjustmmr(ctx, member_input=None, amount: int = 0):
    """Admin command to adjust a player's MMR (format: /adjustmmr @user +/-amount or /adjustmmr userID +/-amount)"""
    # Check if user has admin permissions
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("You need administrator permissions to use this command.")
        return

    if member_input is None:
        await ctx.send(
            "Please specify a member to adjust MMR for. Usage: `/adjustmmr @user +/-amount` or `/adjustmmr userID +/-amount`")
        return

    if amount == 0:
        await ctx.send("Please specify a non-zero amount to adjust. Usage: `/adjustmmr @user +/-amount`")
        return

    # Try to resolve the member
    member = None

    # Check if it's a mention
    if ctx.message.mentions:
        member = ctx.message.mentions[0]
    else:
        # Try to convert to ID
        try:
            user_id = int(member_input.strip("<@!>"))  # Remove mention formatting if present
            member = await ctx.guild.fetch_member(user_id)
        except (ValueError, discord.errors.NotFound):
            # Try to find by name
            try:
                members = await ctx.guild.fetch_members().flatten()
                member = discord.utils.get(members, display_name=member_input)
                if not member:
                    member = discord.utils.get(members, name=member_input)
            except:
                pass

    if not member:
        await ctx.send(f"Could not find member '{member_input}'. Please use a mention, ID, or exact username.")
        return

    player_id = str(member.id)

    # Debug output
    print(f"Adjusting MMR for player: {member.display_name} (ID: {player_id}) by {amount}")

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
            "created_at": datetime.datetime.utcnow(),
            "last_updated": datetime.datetime.utcnow()
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
                "last_updated": datetime.datetime.utcnow()
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

    embed.set_footer(text=f"Adjusted by {ctx.author.display_name}")

    await ctx.send(embed=embed)

    # Update their Discord role based on new MMR
    try:
        await match_system.update_discord_role(ctx, player_id, new_mmr)
    except Exception as e:
        print(f"Error updating Discord role: {str(e)}")
        await ctx.send(f"Note: Could not update Discord role. Error: {str(e)}")


# Match commands
@bot.command()
async def report(ctx, match_id: str, result: str):
    """Report match results (format: /report <match_id> <win/loss>)"""
    # Check if this is a duplicate command
    if await is_duplicate_command(ctx):
        return

    # Check if command is used in an allowed channel
    if not is_command_channel(ctx):
        await ctx.send(
            f"{ctx.author.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.")
        return

    reporter_id = str(ctx.author.id)

    # Validate result argument
    if result.lower() not in ["win", "loss"]:
        await ctx.send("Invalid result. Please use 'win' or 'loss'.")
        return

    # Get match result
    match_result, error = await match_system.report_match_by_id(match_id, reporter_id, result, ctx)

    if error:
        await ctx.send(f"Error: {error}")
        return

    if not match_result:
        await ctx.send("Failed to process match report.")
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

    # Format player info for display
    winning_members = []
    for i, player in enumerate(winning_team):
        try:
            member = await ctx.guild.fetch_member(int(player["id"]))
            name = member.display_name if member else player['name']
            # Bold the names and add some color with emoji
            winning_members.append(f"**{name}** {winning_team_mmr_changes[i]}")
        except:
            winning_members.append(f"**{player['name']}** {winning_team_mmr_changes[i]}")

    losing_members = []
    for i, player in enumerate(losing_team):
        try:
            member = await ctx.guild.fetch_member(int(player["id"]))
            name = member.display_name if member else player['name']
            # Bold the names and add some color with emoji
            losing_members.append(f"**{name}** {losing_team_mmr_changes[i]}")
        except:
            losing_members.append(f"**{player['name']}** {losing_team_mmr_changes[i]}")

    # Create an enhanced embed for results
    embed = discord.Embed(
        title="Match Results",
        description=f"Match completed",
        color=0x00ff00  # Green color
    )

    # Match ID field
    embed.add_field(name="Match ID", value=f"`{match_id}`", inline=False)

    # Winners with trophy emoji
    embed.add_field(
        name="🏆 Winners",
        value=", ".join(winning_members),
        inline=False
    )

    # Losers
    embed.add_field(
        name="😔 Losers",
        value=", ".join(losing_members),
        inline=False
    )

    # MMR System explanation
    embed.add_field(
        name="📊 MMR System",
        value="Dynamic MMR: Changes based on games played",
        inline=False
    )

    # Footer with reporter info
    embed.set_footer(text=f"Reported by {ctx.author.display_name}")

    await ctx.send(embed=embed)


@bot.command()
async def adminreport(ctx, team_number: int, result: str, match_id: str = None):
    """Admin command to report match results (format: /adminreport <team_number> <win> [match_id])"""
    # Check if this is a duplicate command
    if await is_duplicate_command(ctx):
        return

    # Check if command is used in an allowed channel
    if not is_command_channel(ctx):
        await ctx.send(f"{ctx.author.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.")
        return

    # Check if user has admin permissions
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("You need administrator permissions to use this command.")
        return

    # Validate team number
    if team_number not in [1, 2]:
        await ctx.send("Invalid team number. Please use 1 or 2.")
        return

    # Validate result argument
    if result.lower() != "win":
        await ctx.send("Invalid result. Please use 'win' to indicate the winning team.")
        return

    channel_id = str(ctx.channel.id)

    # Find the active match either by ID or channel
    if match_id:
        active_match = match_system.matches.find_one({"match_id": match_id, "status": "in_progress"})
        if not active_match:
            await ctx.send(f"No active match found with ID `{match_id}`.")
            return
    else:
        # Otherwise try to find match in current channel
        active_match = match_system.get_active_match_by_channel(channel_id)
        if not active_match:
            await ctx.send(
                "No active match found in this channel. Please report in the channel where the match was created or provide a match ID.")
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
            "completed_at": datetime.datetime.utcnow(),
            "reported_by": str(ctx.author.id)
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
            member = await ctx.guild.fetch_member(int(player["id"]) if player["id"].isdigit() else 0)
            winning_members.append(member.display_name if member else player["name"])
        except:
            winning_members.append(player["name"])

    losing_members = []
    for player in losing_team:
        try:
            member = await ctx.guild.fetch_member(int(player["id"]) if player["id"].isdigit() else 0)
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
    embed.set_footer(text=f"Reported by admin: {ctx.author.display_name}")

    await ctx.send(embed=embed)

    # Also send a message encouraging people to check the leaderboard
    await ctx.send("Check the updated leaderboard with `/leaderboard`!")


@bot.command()
async def leaderboard(ctx):
    """Shows a link to the leaderboard website"""
    # Check if this is a duplicate command
    if await is_duplicate_command(ctx):
        return

    # Check if command is used in an allowed channel
    if not is_command_channel(ctx):
        await ctx.send(f"{ctx.author.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.")
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

    await ctx.send(embed=embed)


@bot.command()
async def rank(ctx, member: discord.Member = None):
    """Check your rank and stats (or another member's)"""
    # Check if this is a duplicate command
    if await is_duplicate_command(ctx):
        return

    # Check if command is used in an allowed channel
    if not is_command_channel(ctx):
        await ctx.send(f"{ctx.author.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.")
        return

    if member is None:
        member = ctx.author

    player_id = str(member.id)
    player_data = match_system.get_player_stats(player_id)

    if not player_data:
        await ctx.send(f"{member.mention} hasn't played any matches yet.")
        return

    # Calculate stats
    mmr = player_data.get("mmr", 1000)
    wins = player_data.get("wins", 0)
    losses = player_data.get("losses", 0)
    matches = player_data.get("matches", 0)

    win_rate = 0
    if matches > 0:
        win_rate = (wins / matches) * 100

    # Get player's rank position - fixed to use player_data's mmr for comparison
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

    embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
    embed.set_footer(text="Stats updated after each match")

    # Send only once
    await ctx.send(embed=embed)


# Admin commands
@bot.command()
async def clearqueue(ctx):
    """Clear all players from the queue (Admin only)"""
    # Check if this is a duplicate command
    if await is_duplicate_command(ctx):
        return

    # Check if command is used in an allowed channel
    if not is_command_channel(ctx):
        await ctx.send(f"{ctx.author.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.")
        return

    # Check if user has admin permissions
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("You need administrator permissions to use this command.")
        return

    # Get current players in queue
    channel_id = str(ctx.channel.id)
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
        await ctx.send("Queue was already empty!")
    else:
        await ctx.send(f"✅ Queue cleared! Removed {count} player(s) from the queue.")


@bot.command()
async def resetleaderboard(ctx, confirmation: str = None):
    """Reset the leaderboard (Admin only)"""
    # Check if this is a duplicate command
    if await is_duplicate_command(ctx):
        return

    # Check if command is used in an allowed channel
    if not is_command_channel(ctx):
        await ctx.send(f"{ctx.author.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.")
        return

    # Check if user has admin permissions
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("You need administrator permissions to use this command.")
        return

    # Require confirmation
    if confirmation is None or confirmation.lower() != "confirm":
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
        await ctx.send(embed=embed)
        return

    # Check multiple collections to determine if truly empty
    db = match_system.players.database
    player_count = match_system.players.count_documents({})
    match_count = db['matches'].count_documents({})
    rank_count = db['ranks'].count_documents({})

    total_documents = player_count + match_count + rank_count

    try:
        # Create backup collections with timestamp
        timestamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")

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

        # Call the web API to reset the leaderboard
        web_reset = "⚠️ Web reset not attempted"
        try:
            webapp_url = os.getenv('WEBAPP_URL', 'https://sixgentsbot-1.onrender.com')
            admin_token = os.getenv('ADMIN_TOKEN', 'admin-secret-token')

            headers = {
                'Authorization': admin_token,
                'Content-Type': 'application/json'
            }

            data = {
                'admin_id': str(ctx.author.id),
                'reason': 'Season reset via Discord command'
            }

            response = requests.post(f"{webapp_url}/api/reset-leaderboard",
                                     headers=headers,
                                     json=data)

            if response.status_code == 200:
                web_reset = "✅ Web leaderboard reset successfully."
            else:
                web_reset = f"❌ Failed to reset web leaderboard (Status: {response.status_code})."
        except Exception as e:
            web_reset = f"❌ Error connecting to web leaderboard: {str(e)}"

        # Remove rank roles from all members
        role_reset = await remove_all_rank_roles(ctx.guild)

        # Record the reset event locally
        resets_collection = db['resets']
        resets_collection.insert_one({
            "type": "leaderboard_reset",
            "timestamp": datetime.datetime.utcnow(),
            "performed_by": str(ctx.author.id),
            "performed_by_name": ctx.author.display_name,
            "reason": "Season reset via Discord command"
        })

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
            name="Discord Roles",
            value=role_reset,
            inline=False
        )

        embed.set_footer(text=f"Reset by {ctx.author.display_name}")

        await ctx.send(embed=embed)

    except Exception as e:
        await ctx.send(f"Error resetting leaderboard: {str(e)}")


# Add this new helper function to handle role removal
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


@bot.command()
async def forcestart(ctx):
    """Force start the team selection process (Admin only)"""
    # Check if this is a duplicate command
    if await is_duplicate_command(ctx):
        return

    # Check if command is used in an allowed channel
    if not is_queue_channel(ctx):
        await ctx.send(f"{ctx.author.mention}, this command can only be used in the rank-a, rank-b, rank-c, or global channels.")
        return

    # Check if user has admin permissions
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("You need administrator permissions to use this command.")
        return

    # Cancel any existing votes to ensure we can start a new one
    if vote_system.is_voting_active():
        vote_system.cancel_voting()

    if captains_system.is_selection_active():
        captains_system.cancel_selection()

    # Get current players in queue
    channel_id = str(ctx.channel.id)
    players = queue_handler.get_players_for_match(channel_id)
    player_count = len(players)

    if player_count == 0:
        await ctx.send("Can't force start: Queue is empty!")
        return

    # If fewer than 6 players, add dummy players to reach 6
    if player_count < 6:
        # Create dummy players to fill the queue
        needed = 6 - player_count
        await ctx.send(f"Adding {needed} dummy players to fill the queue for testing...")

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
    await ctx.send("**Force starting team selection!**")
    await vote_system.start_vote(ctx.channel)


@bot.command()
async def forcestop(ctx):
    """Force stop any active votes or selections and clear the queue (Admin only)"""
    # Check if this is a duplicate command
    if await is_duplicate_command(ctx):
        return

    # Check if command is used in an allowed channel
    if not is_command_channel(ctx):
        await ctx.send(f"{ctx.author.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.")
        return

    # Check if user has admin permissions
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("You need administrator permissions to use this command.")
        return

    # Get current players in queue
    channel_id = str(ctx.channel.id)
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
    embed.set_footer(text=f"Executed by {ctx.author.display_name}")

    await ctx.send(embed=embed)


@bot.command()
async def sub(ctx, action: str = None, player1: discord.Member = None, player2: discord.Member = None):
    """
    Substitute players in an active match
    Usage:
    - /sub swap @player1 @player2 (swap players between teams)
    - /sub in @player_in @player_out (sub a new player in for an existing player)
    """
    # Check if this is a duplicate command
    if await is_duplicate_command(ctx):
        return

    # Check if command is used in an allowed channel
    if not is_command_channel(ctx):
        await ctx.send(
            f"{ctx.author.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.")
        return

    # Check if user has permission (match participant or admin)
    is_admin = ctx.author.guild_permissions.administrator
    channel_id = str(ctx.channel.id)

    # Get the active match in this channel
    active_match = match_system.get_active_match_by_channel(channel_id)

    if not active_match:
        await ctx.send("No active match found in this channel.")
        return

    # Check if the command user is in the match (or is an admin)
    user_id = str(ctx.author.id)
    team1_ids = [p["id"] for p in active_match["team1"]]
    team2_ids = [p["id"] for p in active_match["team2"]]

    is_participant = user_id in team1_ids or user_id in team2_ids

    if not (is_participant or is_admin):
        await ctx.send("You must be a participant in this match or an admin to use this command.")
        return

    # Check proper command usage
    if not action or not player1:
        await ctx.send("Invalid command usage. Examples:\n"
                       "- `/sub swap @player1 @player2` (swap players between teams)\n"
                       "- `/sub in @new_player @player_out` (sub in a new player)")
        return

    # Handle the "swap" action - swapping players between teams
    if action.lower() == "swap":
        if not player1 or not player2:
            await ctx.send("You need to specify two players to swap.")
            return

        player1_id = str(player1.id)
        player2_id = str(player2.id)

        # Check if both players are in the match but on different teams
        player1_in_team1 = player1_id in team1_ids
        player1_in_team2 = player1_id in team2_ids
        player2_in_team1 = player2_id in team1_ids
        player2_in_team2 = player2_id in team2_ids

        if not ((player1_in_team1 and player2_in_team2) or (player1_in_team2 and player2_in_team1)):
            await ctx.send("Both players must be in the match and on different teams.")
            return

        # Execute the swap
        await swap_players(ctx, active_match, player1, player2)

    # Handle the "in" action - subbing a new player in
    elif action.lower() == "in":
        if not player1 or not player2:
            await ctx.send("You need to specify both the incoming player and the player to replace.")
            return

        new_player_id = str(player1.id)
        out_player_id = str(player2.id)

        # Check if player_out is in the match
        out_in_team1 = out_player_id in team1_ids
        out_in_team2 = out_player_id in team2_ids

        if not (out_in_team1 or out_in_team2):
            await ctx.send(f"{player2.mention} is not in this match.")
            return

        # Check if new_player is already in the match
        if new_player_id in team1_ids or new_player_id in team2_ids:
            await ctx.send(f"{player1.mention} is already in this match.")
            return

        # Check if new player is eligible for this channel
        channel_name = ctx.channel.name.lower()
        if channel_name in ["rank-a", "rank-b", "rank-c"]:
            # For rank-specific channels, check if the new player has the appropriate role
            required_role = None
            if channel_name == "rank-a":
                required_role = discord.utils.get(ctx.guild.roles, name="Rank A")
            elif channel_name == "rank-b":
                required_role = discord.utils.get(ctx.guild.roles, name="Rank B")
            elif channel_name == "rank-c":
                required_role = discord.utils.get(ctx.guild.roles, name="Rank C")

            if required_role and required_role not in player1.roles:
                await ctx.send(
                    f"{player1.mention} doesn't have the {required_role.name} role required for this channel.")
                return

        # Execute the substitution
        await sub_in_player(ctx, active_match, player1, player2)

    else:
        await ctx.send("Invalid action. Use 'swap' or 'in'.")


async def swap_players(ctx, match, player1, player2):
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

    await ctx.send(embed=embed)


async def sub_in_player(ctx, match, new_player, out_player):
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

    await ctx.send(embed=embed)


@bot.command()
async def purgechat(ctx, amount_to_delete: int = 10):
    """Clear chat messages"""
    # Check if this is a duplicate command
    if await is_duplicate_command(ctx):
        return

    # Check if command is used in an allowed channel
    if not is_command_channel(ctx):
        await ctx.send(f"{ctx.author.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.")
        return

    if ctx.author.guild_permissions.manage_messages:
        if 1 <= amount_to_delete <= 100:
            await ctx.channel.purge(limit=amount_to_delete + 1)
            await ctx.send(f"Cleared {amount_to_delete} messages.", delete_after=5)
        else:
            await ctx.send("Please enter a number between 1 and 100")
    else:
        await ctx.send("You don't have permission to use this command.")


@bot.command()
async def ping(ctx):
    """Simple ping command that doesn't use MongoDB"""
    # Check if this is a duplicate command
    if await is_duplicate_command(ctx):
        return

    # Check if command is used in an allowed channel
    if not is_command_channel(ctx):
        await ctx.send(f"{ctx.author.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.")
        return

    await ctx.send("Pong! Bot is connected to Discord.")


# Help command
@bot.command()
async def helpme(ctx):
    """Display help information"""
    # Check if this is a duplicate command
    if await is_duplicate_command(ctx):
        return

    # Check if command is used in an allowed channel
    if not is_command_channel(ctx):
        await ctx.send(f"{ctx.author.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.")
        return

    embed = discord.Embed(
        title="Rocket League 6 Mans Bot",
        description="Commands for the 6 mans queue system:",
        color=0x00ff00
    )

    embed.add_field(name="/join", value="Join the queue (rank-a, rank-b, rank-c, global channels only)", inline=False)
    embed.add_field(name="/leave", value="Leave the queue (rank-a, rank-b, rank-c, global channels only)", inline=False)
    embed.add_field(name="/status", value="Show the current queue status (rank-a, rank-b, rank-c, global channels only)", inline=False)
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

    await ctx.send(embed=embed)


# Error handler
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        # Get the command that was attempted
        attempted_command = ctx.message.content.split()[0][1:]  # Remove the / prefix
        await ctx.send(f"Command not found. Use `/helpme` to see available commands.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Missing required argument. Use `/helpme` to see command usage.")
    elif isinstance(error, (discord.errors.HTTPException, discord.errors.GatewayNotFound,
                            discord.errors.ConnectionClosed)):
        print(f"Discord connection error: {error}")
        # Don't reply, as this might create duplicates
    else:
        print(f"Error: {error}")


# Run the bot with the keepalive server
if __name__ == "__main__":
    # Start the keepalive server first
    start_keepalive_server()

    # Then run the bot
    bot.run(token, log_handler=handler, log_level=logging.DEBUG)