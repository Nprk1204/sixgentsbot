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

    # Use a more unique key
    key = f"{user_id}:{command_name}:{channel_id}:{message_id}"

    # Use lock to prevent race conditions
    async with command_lock:
        # Check if we've seen this command recently
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
            recent_commands.clear()
            recent_commands.update({k: v for k, v in recent_commands.items() if now - v < 300})
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

    player = ctx.author
    channel_id = ctx.channel.id
    response = queue_handler.add_player(player, channel_id)
    await ctx.send(response)

    # Check if queue is full and start voting
    players = queue_handler.get_players_for_match(channel_id)
    if len(players) >= 6 and channel_id in vote_system.vote_systems and not vote_system.is_voting_active(channel_id):
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

    channel_id = ctx.channel.id

    # Check if voting is active in this channel
    if vote_system.is_voting_active(channel_id):
        await ctx.send(f"{ctx.author.mention}, you cannot leave the queue while voting is in progress!")
        return

    # Check if captain selection is active in this channel
    if captains_system.is_selection_active(channel_id):
        await ctx.send(f"{ctx.author.mention}, you cannot leave the queue while team selection is in progress!")
        return

    player = ctx.author
    response = queue_handler.remove_player(player, channel_id)
    await ctx.send(response)


@bot.command()
async def status(ctx):
    """Shows the current queue status"""
    # Check if this is a duplicate command
    if await is_duplicate_command(ctx):
        return

    # Check if command is used in an allowed channel
    if not is_queue_channel(ctx):
        await ctx.send(f"{ctx.author.mention}, this command can only be used in the rank-a, rank-b, rank-c, or global channels.")
        return

    channel_id = str(ctx.channel.id)
    response = queue_handler.get_queue_status(channel_id)
    await ctx.send(embed=response)

    # If queue is full but vote not started, check if we should start voting
    players = queue_handler.get_players_for_match(channel_id)
    if len(players) >= 6:
        # Check if voting is already active for this channel
        if not vote_system.is_voting_active(channel_id):
            await vote_system.start_vote(ctx.channel)


# Match commands
@bot.command()
async def report(ctx, match_id: str, result: str):
    """Report match results (format: /report <match_id> <win/loss>)"""
    # Check if this is a duplicate command
    if await is_duplicate_command(ctx):
        return

    # Check if command is used in an allowed channel
    if not is_command_channel(ctx):
        await ctx.send(f"{ctx.author.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.")
        return

    reporter_id = str(ctx.author.id)

    # Validate result argument
    if result.lower() not in ["win", "loss"]:
        await ctx.send("Invalid result. Please use 'win' or 'loss'.")
        return

    # Print debug info
    print(f"Processing report command for match {match_id} by {reporter_id}")

    # Find match by ID with explicit query
    active_match = match_system.matches.find_one({"match_id": match_id})

    # Debug print match data if found
    if active_match:
        print(f"Found match {match_id} with status: {active_match.get('status')}")
    else:
        print(f"No match found with ID {match_id}")
        await ctx.send(f"No match found with ID `{match_id}`.")
        return

    # Check if match is still in progress
    if active_match.get("status") != "in_progress":
        print(f"Match {match_id} is not in progress, status: {active_match.get('status')}")
        await ctx.send(f"Error: This match has already been reported.")
        return

    # Now proceed with reporting - ONLY ONCE
    print(f"Calling report_match_by_id for match {match_id}")
    match, error = await match_system.report_match_by_id(match_id, reporter_id, result, ctx)
    print(f"Report result: match={match is not None}, error={error}")

    if error:
        await ctx.send(f"Error: {error}")
        return

    if not match:
        await ctx.send("Failed to process match report.")
        return

    # Determine winning team
    winner = match["winner"]

    if winner == 1:
        winning_team = match["team1"]
        losing_team = match["team2"]
    else:
        winning_team = match["team2"]
        losing_team = match["team1"]

    # Format team members - using display_name instead of mentions
    winning_members = []
    for player in winning_team:
        try:
            member = await ctx.guild.fetch_member(int(player["id"]))
            winning_members.append(member.display_name)
        except:
            winning_members.append(player["name"])

    losing_members = []
    for player in losing_team:
        try:
            member = await ctx.guild.fetch_member(int(player["id"]))
            losing_members.append(member.display_name)
        except:
            losing_members.append(player["name"])

    # Create results embed
    embed = discord.Embed(
        title="Match Results",
        description=f"Match completed",
        color=0x00ff00
    )

    embed.add_field(name="Winners", value=", ".join(winning_members), inline=False)
    embed.add_field(name="Losers", value=", ".join(losing_members), inline=False)
    embed.add_field(name="MMR", value="+15 for winners, -12 for losers", inline=False)
    embed.set_footer(text=f"Reported by {ctx.author.display_name}")

    await ctx.send(embed=embed)

    # Also send a message encouraging people to check the leaderboard
    await ctx.send("Check the updated leaderboard with `/leaderboard`!")


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
    embed.set_thumbnail(url="https://i.imgur.com/pKd5Sdk.png")  # Replace with a Rocket League icon URL

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
    players = queue_handler.get_players_for_match()
    count = len(players)

    # Clear the queue collection
    queue_handler.queue.delete_many({})

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
    players = queue_handler.get_players_for_match()
    current_count = len(players)

    if current_count == 0:
        await ctx.send("Can't force start: Queue is empty!")
        return

    # If fewer than 6 players, add dummy players to reach 6
    if current_count < 6:
        # Create dummy players to fill the queue
        needed = 6 - current_count
        await ctx.send(f"Adding {needed} dummy players to fill the queue for testing...")

        for i in range(needed):
            # Use numeric IDs starting from 9000 to prevent parsing issues
            dummy_id = f"9000{i + 1}"  # 90001, 90002, etc
            dummy_name = f"TestPlayer{i + 1}"
            dummy_mention = f"@TestPlayer{i + 1}"

            # Add dummy player to queue
            queue_handler.queue.insert_one({
                "id": dummy_id,
                "name": dummy_name,
                "mention": dummy_mention
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
    players = queue_handler.get_players_for_match()
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
    queue_handler.queue.delete_many({})

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
    # Check for duplicate command
    if await is_duplicate_command(ctx):
        print(f"Duplicate command detected in error handler: {ctx.command}")
        return

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