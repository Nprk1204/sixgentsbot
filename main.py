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
from collections import defaultdict, deque
import time
from discord_rate_limiter import discord_rate_limiter, RateLimitedDiscordOps, rate_limiter_maintenance, rate_limited

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')

# Set up logging
handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix='/', intents=intents)
bot.remove_command('help')

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
    server_thread.daemon = True
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


# Track recent commands to prevent duplicates - ENHANCED
recent_commands = {}
command_lock = asyncio.Lock()


async def is_duplicate_command(ctx):
    """Enhanced duplicate prevention"""
    user_id = ctx.author.id
    command_name = ctx.command.name if ctx.command else "unknown"
    channel_id = ctx.channel.id
    message_id = ctx.message.id
    timestamp = ctx.message.created_at.timestamp()

    # Create a unique key based on user, command, and channel
    key = f"{user_id}:{command_name}:{channel_id}"

    async with command_lock:
        now = datetime.datetime.now(datetime.UTC).timestamp()

        # Check if this exact command was run very recently (within 2 seconds)
        if key in recent_commands:
            last_time = recent_commands[key]
            if now - last_time < 2.0:  # 2 second cooldown
                print(f"DUPLICATE BLOCKED: {command_name} from {ctx.author.name} (too recent)")
                return True

        # Update the timestamp
        recent_commands[key] = now

        # Clean old entries (older than 10 seconds)
        old_keys = [k for k, v in recent_commands.items() if now - v > 10.0]
        for old_key in old_keys:
            del recent_commands[old_key]

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


def has_admin_or_mod_permissions(user, guild):
    """Check if user has admin permissions OR the "6mod" role"""
    if user.guild_permissions.administrator:
        return True
    mod_role = discord.utils.get(guild.roles, name="6mod")
    if mod_role and mod_role in user.roles:
        return True
    return False


async def safe_interaction_response(interaction, *args, **kwargs):
    """Safely respond to interactions with rate limiting"""
    try:
        await discord_rate_limiter.wait_for_rate_limit(
            route="POST/interactions/{id}/{token}/callback",
            guild_id=str(interaction.guild.id) if interaction.guild else None,
            channel_id=str(interaction.channel.id) if interaction.channel else None
        )

        if not interaction.response.is_done():
            await interaction.response.send_message(*args, **kwargs)
        else:
            await interaction.followup.send(*args, **kwargs)

    except discord.HTTPException as e:
        if e.status == 429:
            discord_rate_limiter.handle_429_response(getattr(e, 'retry_after', None))
            await asyncio.sleep(getattr(e, 'retry_after', 1))
            # Retry once
            if not interaction.response.is_done():
                await interaction.response.send_message(*args, **kwargs)
            else:
                await interaction.followup.send(*args, **kwargs)
        else:
            raise


async def safe_interaction_defer(interaction, ephemeral=False):
    """Safely defer interactions with rate limiting"""
    try:
        await discord_rate_limiter.wait_for_rate_limit(
            route="POST/interactions/{id}/{token}/callback",
            guild_id=str(interaction.guild.id) if interaction.guild else None,
            channel_id=str(interaction.channel.id) if interaction.channel else None
        )

        await interaction.response.defer(ephemeral=ephemeral)

    except discord.HTTPException as e:
        if e.status == 429:
            discord_rate_limiter.handle_429_response(getattr(e, 'retry_after', None))
            await asyncio.sleep(getattr(e, 'retry_after', 1))
            await interaction.response.defer(ephemeral=ephemeral)
        else:
            raise


async def safe_followup_send(interaction, *args, **kwargs):
    """Safely send followup messages with rate limiting"""
    try:
        await discord_rate_limiter.wait_for_rate_limit(
            route="POST/webhooks/{application.id}/{interaction.token}",
            guild_id=str(interaction.guild.id) if interaction.guild else None,
            channel_id=str(interaction.channel.id) if interaction.channel else None
        )

        return await interaction.followup.send(*args, **kwargs)

    except discord.HTTPException as e:
        if e.status == 429:
            discord_rate_limiter.handle_429_response(getattr(e, 'retry_after', None))
            await asyncio.sleep(getattr(e, 'retry_after', 1))
            return await interaction.followup.send(*args, **kwargs)
        else:
            raise


@rate_limited("PUT/guilds/{id}/members/{id}/roles/{id}", is_role_operation=True)
async def assign_discord_role_rate_limited(member, role, reason=None):
    """Rate-limited Discord role assignment"""
    return await member.add_roles(role, reason=reason)


@rate_limited("DELETE/guilds/{id}/members/{id}/roles/{id}", is_role_operation=True)
async def remove_discord_role_rate_limited(member, role, reason=None):
    """Rate-limited Discord role removal"""
    return await member.remove_roles(role, reason=reason)


# Database setup
client = MongoClient(MONGO_URI, server_api=ServerApi('1'))
try:
    client.admin.command('ping')
    print("MongoDB connection successful!")
except Exception as e:
    print(f"MongoDB connection error: {e}")

# Initialize components
db = Database(MONGO_URI)
system_coordinator = SystemCoordinator(db)


@bot.event
async def on_ready():
    print(f"{bot.user.name} is now online with ID: {bot.user.id}")
    print(f"Connected to {len(bot.guilds)} guilds")

    try:
        system_coordinator.set_bot(bot)
        bot.loop.create_task(system_coordinator.check_for_ready_matches())
        bot.loop.create_task(rate_limiter_maintenance())
        print(f"BOT INSTANCE ACTIVE - {datetime.datetime.now(datetime.UTC)}")

        print("Syncing global commands...")
        await bot.tree.sync()

        print("Syncing guild-specific commands...")
        for guild in bot.guilds:
            try:
                await bot.tree.sync(guild=guild)
                print(f"Synced commands to guild: {guild.name} (ID: {guild.id})")
            except Exception as guild_error:
                print(f"Error syncing to guild {guild.name}: {guild_error}")

        commands = bot.tree.get_commands()
        print(f"Registered {len(commands)} global application commands:")
        for cmd in commands:
            print(f"- /{cmd.name}")

        print("Command synchronization complete.")

    except Exception as e:
        print(f"Error during bot initialization: {e}")
        import traceback
        traceback.print_exc()


@bot.event
async def on_reaction_add(reaction, user):
    """Handle reactions for voting"""
    if user.bot:
        return
    await system_coordinator.handle_reaction(reaction, user)


# Queue commands
# Updated queue command - optimized for speed, no defer
@bot.tree.command(name="queue", description="Join the queue for 6 mans")
async def queue_slash(interaction: discord.Interaction):
    # Check channel first
    if not is_queue_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, or global channels.",
            ephemeral=True
        )
        return

    # IMMEDIATE response to prevent timeout
    await interaction.response.defer()

    # Fast rank verification check
    player = interaction.user
    player_id = str(player.id)

    rank_record = db.get_collection('ranks').find_one({"discord_id": player_id})
    rank_a_role = discord.utils.get(interaction.guild.roles, name="Rank A")
    rank_b_role = discord.utils.get(interaction.guild.roles, name="Rank B")
    rank_c_role = discord.utils.get(interaction.guild.roles, name="Rank C")
    has_rank_role = any(role in player.roles for role in [rank_a_role, rank_b_role, rank_c_role])

    if not (rank_record or has_rank_role):
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
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # Create missing rank record if needed
    if has_rank_role and not rank_record:
        tier = "Rank C"
        mmr = 600
        if rank_a_role in player.roles:
            tier = "Rank A"
            mmr = 1600
        elif rank_b_role in player.roles:
            tier = "Rank B"
            mmr = 1100

        try:
            db.get_collection('ranks').insert_one({
                "discord_id": player_id,
                "discord_username": player.display_name,
                "tier": tier,
                "mmr": mmr,
                "timestamp": datetime.datetime.utcnow()
            })
        except Exception as e:
            print(f"Error creating rank record: {e}")

    # Get queue manager response
    try:
        response_message = await system_coordinator.queue_manager.add_player(player, interaction.channel)
    except Exception as e:
        print(f"Error adding player to queue: {e}")
        await interaction.followup.send("An error occurred while joining the queue. Please try again.", ephemeral=True)
        return

    # Handle response - SINGLE followup message only
    if "QUEUE_ERROR:" in response_message:
        error_msg = response_message.replace("QUEUE_ERROR:", "").strip()

        if "already in the queue for" in error_msg:
            embed = discord.Embed(
                title="Queue Error",
                description=error_msg,
                color=0xe74c3c
            )
            embed.add_field(name="What to do",
                            value="Use `/leave` in that channel first, then try joining this queue again.",
                            inline=False)
        elif "already in this queue" in error_msg:
            status_data = system_coordinator.queue_manager.get_queue_status(interaction.channel)
            queue_count = status_data['queue_count']
            embed = discord.Embed(
                title="Queue Status",
                description=f"{player.mention}, you're already in this queue!",
                color=0xffa500
            )
            embed.add_field(name="Current Queue", value=f"**{queue_count}/6** players waiting", inline=False)
            embed.add_field(name="Queue Progress",
                            value=f"{'▰' * queue_count}{'▱' * (6 - queue_count)} ({queue_count}/6)", inline=False)
        else:
            embed = discord.Embed(title="Queue Error", description=error_msg, color=0xff0000)

        embed.timestamp = datetime.datetime.now()
        embed.set_footer(text=f"Channel: #{interaction.channel.name}")
        await interaction.followup.send(embed=embed)

    elif "SUCCESS:" in response_message:
        success_msg = response_message.replace("SUCCESS:", "").strip()
        status_data = system_coordinator.queue_manager.get_queue_status(interaction.channel)
        queue_count = status_data['queue_count']

        embed = discord.Embed(
            title="Queue Status",
            description=success_msg,
            color=0x00ff00
        )
        embed.add_field(name="Queue Progress", value=f"{'▰' * queue_count}{'▱' * (6 - queue_count)} ({queue_count}/6)",
                        inline=False)

        if queue_count < 6:
            embed.add_field(name="Status", value=f"Waiting for **{6 - queue_count}** more player(s)", inline=False)
        else:
            embed.add_field(name="Status", value="🎉 **Queue is FULL!** Match starting soon...", inline=False)

        embed.timestamp = datetime.datetime.now()
        embed.set_footer(text=f"Channel: #{interaction.channel.name}")
        await interaction.followup.send(embed=embed)

    else:
        # Match creation
        match_id = response_message
        embed = discord.Embed(
            title="Match Starting!",
            description=f"Queue is full! Starting team selection for match `{match_id}`",
            color=0x00ff00
        )
        embed.add_field(name="Match ID", value=f"`{match_id}`", inline=False)
        embed.add_field(name="Next Step", value="Team selection voting will begin shortly. Make sure to vote!",
                        inline=False)
        embed.timestamp = datetime.datetime.now()
        embed.set_footer(text=f"Channel: #{interaction.channel.name}")
        await interaction.followup.send(embed=embed)

        # FIXED: Start voting AFTER sending the match creation message
        # Check if voting system exists for this channel
        channel_name = interaction.channel.name.lower()
        if channel_name in system_coordinator.vote_systems:
            # Small delay to ensure proper order
            await asyncio.sleep(1.0)
            try:
                await system_coordinator.vote_systems[channel_name].start_vote(interaction.channel)
            except Exception as vote_error:
                print(f"Error starting vote: {vote_error}")


# FIXED: Leave command with proper interaction handling
@bot.tree.command(name="leave", description="Leave the queue")
async def leave_slash(interaction: discord.Interaction):
    if not is_queue_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, or global channels.",
            ephemeral=True
        )
        return

    # IMMEDIATE response to prevent timeout
    await interaction.response.defer()

    # Fast rank verification check
    player = interaction.user
    player_id = str(player.id)

    rank_record = db.get_collection('ranks').find_one({"discord_id": player_id})
    rank_a_role = discord.utils.get(interaction.guild.roles, name="Rank A")
    rank_b_role = discord.utils.get(interaction.guild.roles, name="Rank B")
    rank_c_role = discord.utils.get(interaction.guild.roles, name="Rank C")
    has_rank_role = any(role in player.roles for role in [rank_a_role, rank_b_role, rank_c_role])

    if not (rank_record or has_rank_role):
        embed = discord.Embed(
            title="Rank Verification Required",
            description="You need to verify your Rocket League rank before joining the queue.",
            color=0xf1c40f
        )
        embed.add_field(name="How to Verify",
                        value="Visit the rank check page on the website to complete verification.", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # Get queue manager response
    try:
        response_message = await system_coordinator.queue_manager.remove_player(interaction.user, interaction.channel)
    except Exception as e:
        print(f"Error removing player from queue: {e}")
        await interaction.followup.send("An error occurred while leaving the queue. Please try again.", ephemeral=True)
        return

    # Handle response - SINGLE followup message only
    if "MATCH_ERROR:" in response_message:
        error_msg = response_message.replace("MATCH_ERROR:", "").strip()

        # Extract match ID if present
        match_id = None
        if "Match ID:" in error_msg:
            import re
            match = re.search(r'Match ID: `([^`]+)`', error_msg)
            if match:
                match_id = match.group(1)

        embed = discord.Embed(title="Cannot Leave Queue", description=error_msg, color=0xe74c3c)

        if "voting" in error_msg:
            embed.add_field(name="Reason", value="Team selection voting is in progress", inline=False)
            embed.add_field(name="What to do", value="Wait for the vote to complete, then you'll be in a match",
                            inline=False)
        elif "selection" in error_msg:
            embed.add_field(name="Reason", value="Captain selection is in progress", inline=False)
            embed.add_field(name="What to do", value="Wait for team selection to complete, then you'll be in a match",
                            inline=False)
        elif "active match" in error_msg:
            embed.add_field(name="Reason", value="You're currently in an active match", inline=False)
            if match_id:
                embed.add_field(name="What to do",
                                value=f"Complete your match using `/report {match_id} win` or `/report {match_id} loss`",
                                inline=False)

        embed.timestamp = datetime.datetime.now()
        embed.set_footer(text=f"Channel: #{interaction.channel.name}")
        await interaction.followup.send(embed=embed)

    elif "QUEUE_ERROR:" in response_message:
        error_msg = response_message.replace("QUEUE_ERROR:", "").strip()
        status_data = system_coordinator.queue_manager.get_queue_status(interaction.channel)
        queue_count = status_data['queue_count']

        if "not in this channel's queue" in error_msg:
            embed = discord.Embed(title="Queue Error", description=error_msg, color=0xf1c40f)
            embed.add_field(name="Current Queue", value=f"**{queue_count}/6** players waiting in this channel",
                            inline=False)
            embed.add_field(name="Your Queue",
                            value="You're queued in a different channel. Use `/leave` in that channel to leave your queue.",
                            inline=False)
        elif "not in any queue" in error_msg:
            embed = discord.Embed(title="Queue Status", description=error_msg, color=0xf1c40f)
            embed.add_field(name="Current Queue", value=f"**{queue_count}/6** players waiting", inline=False)
            if queue_count > 0:
                embed.add_field(name="Join Queue", value="Use `/queue` to join the current queue", inline=False)
            else:
                embed.add_field(name="Join Queue", value="Use `/queue` to start a new queue", inline=False)
        else:
            embed = discord.Embed(title="Queue Error", description=error_msg, color=0xe74c3c)

        embed.timestamp = datetime.datetime.now()
        embed.set_footer(text=f"Channel: #{interaction.channel.name}")
        await interaction.followup.send(embed=embed)

    elif "SUCCESS:" in response_message:
        success_msg = response_message.replace("SUCCESS:", "").strip()
        status_data = system_coordinator.queue_manager.get_queue_status(interaction.channel)
        queue_count = status_data['queue_count']

        embed = discord.Embed(title="Queue Status", description=success_msg, color=0xff9900)
        embed.add_field(name="Updated Queue", value=f"**{queue_count}/6** players remaining", inline=False)

        if queue_count > 0:
            embed.add_field(name="Queue Progress",
                            value=f"{'▰' * queue_count}{'▱' * (6 - queue_count)} ({queue_count}/6)", inline=False)
            embed.add_field(name="Status", value=f"Waiting for **{6 - queue_count}** more player(s)", inline=False)
        else:
            embed.add_field(name="Status", value="Queue is now empty", inline=False)

        embed.timestamp = datetime.datetime.now()
        embed.set_footer(text=f"Channel: #{interaction.channel.name}")
        await interaction.followup.send(embed=embed)

    else:
        # Fallback
        embed = discord.Embed(title="Queue Status", description=response_message, color=0x95a5a6)
        status_data = system_coordinator.queue_manager.get_queue_status(interaction.channel)
        queue_count = status_data['queue_count']
        embed.add_field(name="Current Queue", value=f"**{queue_count}/6** players waiting", inline=False)
        embed.timestamp = datetime.datetime.now()
        embed.set_footer(text=f"Channel: #{interaction.channel.name}")
        await interaction.followup.send(embed=embed)


# FIXED: Error handler - simplified
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    print(f"Command error: {error}")

    try:
        if isinstance(error, app_commands.errors.CommandNotFound):
            if not interaction.response.is_done():
                await interaction.response.send_message("Command not found. Use `/help` to see available commands.",
                                                        ephemeral=True)
        elif isinstance(error, app_commands.errors.MissingPermissions):
            if not interaction.response.is_done():
                await interaction.response.send_message("You don't have permission to use this command.",
                                                        ephemeral=True)
        elif isinstance(error, app_commands.errors.CommandInvokeError):
            if isinstance(error.original, discord.errors.NotFound):
                print(f"Interaction timed out: {error.original}")
                return
            else:
                print(f"Command invoke error: {error.original}")
                if not interaction.response.is_done():
                    await interaction.response.send_message("An error occurred. Please try again.", ephemeral=True)
        else:
            if not interaction.response.is_done():
                await interaction.response.send_message("An error occurred. Please try again.", ephemeral=True)
    except Exception as e:
        print(f"Error in error handler: {e}")


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

    # Normalize the match ID (take just the first 6 characters if longer)
    match_id = match_id.strip()
    if len(match_id) > 8:
        match_id = match_id[:6]

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

    # ADDED: Check if the match was created in this specific channel
    current_channel_id = str(interaction.channel.id)

    # Find the match first to check which channel it belongs to
    match = None

    # Check active matches first
    if system_coordinator.queue_manager:
        match = system_coordinator.queue_manager.get_match_by_id(match_id)

    # If not in active matches, check completed matches in database
    if not match:
        match = system_coordinator.match_system.matches.find_one({"match_id": match_id})

    if not match:
        await interaction.response.send_message(f"No match found with ID `{match_id}`.", ephemeral=True)
        return

    # Check if the match belongs to this channel
    match_channel_id = str(match.get('channel_id', ''))
    if match_channel_id != current_channel_id:
        # Get the correct channel name for the error message
        try:
            correct_channel = bot.get_channel(int(match_channel_id))
            if correct_channel:
                await interaction.response.send_message(
                    f"❌ This match was created in {correct_channel.mention}. Please report it there instead.",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"❌ This match was not created in this channel. Please report it in the correct channel.",
                    ephemeral=True
                )
        except:
            await interaction.response.send_message(
                f"❌ This match was not created in this channel. Please report it in the correct channel.",
                ephemeral=True
            )
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
    is_global = match_result.get("is_global", False)
    mmr_type = "Global" if is_global else "Ranked"

    if winner == 1:
        winning_team = match_result["team1"]
        losing_team = match_result["team2"]
    else:
        winning_team = match_result["team2"]
        losing_team = match_result["team1"]

    print(f"Processing match report display for match {match_id}")
    print(f"Match type: {mmr_type}")
    print(f"MMR changes available: {len(match_result.get('mmr_changes', []))}")

    # FIXED: Extract MMR changes and streaks from match result properly
    mmr_changes_by_player = {}
    for change in match_result.get("mmr_changes", []):
        player_id = change.get("player_id")
        if player_id:
            mmr_changes_by_player[player_id] = {
                "mmr_change": change.get("mmr_change", 0),
                "streak": change.get("streak", 0),
                "is_win": change.get("is_win", False),
                "is_global": change.get("is_global", False),
                "old_mmr": change.get("old_mmr", 0),
                "new_mmr": change.get("new_mmr", 0)
            }
            print(
                f"Player {player_id}: MMR change {change.get('mmr_change', 0)}, streak {change.get('streak', 0)}, is_global: {change.get('is_global', False)}")

    # Initialize arrays for MMR changes and streaks
    winning_team_mmr_changes = []
    losing_team_mmr_changes = []
    winning_team_streaks = []
    losing_team_streaks = []

    # FIXED: Extract MMR changes for winning team with proper global/ranked filtering
    for player in winning_team:
        player_id = player.get("id")

        if player_id and player_id in mmr_changes_by_player:
            change_data = mmr_changes_by_player[player_id]

            # FIXED: Only show MMR changes that match the current match type
            change_is_global = change_data.get("is_global", False)
            if change_is_global == is_global:  # Only show if match types align
                mmr_change = change_data["mmr_change"]
                streak = change_data["streak"]

                # UPDATED: Show just the MMR value without "Global" or "Ranked" prefix
                winning_team_mmr_changes.append(f"+{mmr_change} MMR")

                # FIXED: Format streak display with emojis based on the new streak value
                if streak >= 3:
                    winning_team_streaks.append(f"🔥 {streak}W")
                elif streak == 2:
                    winning_team_streaks.append(f"↗️ {streak}W")
                elif streak == 1:
                    winning_team_streaks.append(f"↗️ {streak}W")
                else:
                    winning_team_streaks.append("—")

                print(f"Winner {player.get('name', 'Unknown')}: +{mmr_change} MMR, streak {streak}")
            else:
                print(
                    f"Skipping MMR display for {player.get('name', 'Unknown')} - wrong match type (change_is_global: {change_is_global}, match_is_global: {is_global})")
                winning_team_mmr_changes.append("—")
                winning_team_streaks.append("—")
        elif player_id and player_id.startswith('9000'):  # Dummy player
            winning_team_mmr_changes.append("+0 MMR")
            winning_team_streaks.append("—")
        else:
            print(f"No MMR change found for winner {player.get('name', 'Unknown')} (ID: {player_id})")
            winning_team_mmr_changes.append("—")
            winning_team_streaks.append("—")

    # FIXED: Extract MMR changes for losing team with proper global/ranked filtering
    for player in losing_team:
        player_id = player.get("id")

        if player_id and player_id in mmr_changes_by_player:
            change_data = mmr_changes_by_player[player_id]

            # FIXED: Only show MMR changes that match the current match type
            change_is_global = change_data.get("is_global", False)
            if change_is_global == is_global:  # Only show if match types align
                mmr_change = change_data["mmr_change"]
                streak = change_data["streak"]

                # UPDATED: Show just the MMR value without "Global" or "Ranked" prefix
                losing_team_mmr_changes.append(f"{mmr_change} MMR")  # Already negative

                # FIXED: Format streak display for losses
                if streak <= -3:
                    losing_team_streaks.append(f"❄️ {abs(streak)}L")
                elif streak == -2:
                    losing_team_streaks.append(f"↘️ {abs(streak)}L")
                elif streak == -1:
                    losing_team_streaks.append(f"↘️ {abs(streak)}L")
                else:
                    losing_team_streaks.append("—")

                print(f"Loser {player.get('name', 'Unknown')}: {mmr_change} MMR, streak {streak}")
            else:
                print(
                    f"Skipping MMR display for {player.get('name', 'Unknown')} - wrong match type (change_is_global: {change_is_global}, match_is_global: {is_global})")
                losing_team_mmr_changes.append("—")
                losing_team_streaks.append("—")
        elif player_id and player_id.startswith('9000'):  # Dummy player
            losing_team_mmr_changes.append("-0 MMR")
            losing_team_streaks.append("—")
        else:
            print(f"No MMR change found for loser {player.get('name', 'Unknown')} (ID: {player_id})")
            losing_team_mmr_changes.append("—")
            losing_team_streaks.append("—")

    # FIXED: Create the embed with enhanced formatting to include match type and streaks
    embed = discord.Embed(
        title=f"{mmr_type} Match Results",
        description=f"Match completed",
        color=0x00ff00  # Green color
    )

    # Match ID and type field
    embed.add_field(
        name="Match Info",
        value=f"**Match ID:** `{match_id}`\n**Type:** {mmr_type} Match",
        inline=False
    )

    # Add Winners header
    embed.add_field(name="🏆 Winners", value="\u200b", inline=False)

    # Create individual fields for each winning player with better formatting
    for i, player in enumerate(winning_team):
        try:
            member = await interaction.guild.fetch_member(int(player.get("id", 0)))
            name = member.display_name if member else player.get('name', 'Unknown')
        except:
            name = player.get("name", "Unknown")

        # FIXED: Enhanced display with simplified MMR format
        mmr_display = winning_team_mmr_changes[i] if i < len(winning_team_mmr_changes) else "—"
        streak_display = winning_team_streaks[i] if i < len(winning_team_streaks) else "—"

        embed.add_field(
            name=f"**{name}**",
            value=f"{mmr_display}\n{streak_display}",
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

    # Create individual fields for each losing player with better formatting
    for i, player in enumerate(losing_team):
        try:
            member = await interaction.guild.fetch_member(int(player.get("id", 0)))
            name = member.display_name if member else player.get('name', 'Unknown')
        except:
            name = player.get("name", "Unknown")

        # FIXED: Enhanced display with simplified MMR format
        mmr_display = losing_team_mmr_changes[i] if i < len(losing_team_mmr_changes) else "—"
        streak_display = losing_team_streaks[i] if i < len(losing_team_streaks) else "—"

        embed.add_field(
            name=f"**{name}**",
            value=f"{mmr_display}\n{streak_display}",
            inline=True
        )

    # Spacer field if needed to ensure proper alignment (for 3-column layout)
    if len(losing_team) % 3 == 1:
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
    elif len(losing_team) % 3 == 2:
        embed.add_field(name="\u200b", value="\u200b", inline=True)

    # FIXED: Enhanced MMR System explanation with streak info
    embed.add_field(
        name="📊 MMR & Streak System",
        value=(
            f"**{mmr_type} MMR:** Dynamic changes based on team balance and streaks\n"
            f"**Streaks:** 🔥 3+ wins = bonus MMR | ❄️ 3+ losses = extra penalty\n"
            f"**Icons:** ↗️ Recent win | ↘️ Recent loss | — No streak"
        ),
        inline=False
    )

    # Footer with reporter info and timestamp
    embed.set_footer(
        text=f"Reported by {interaction.user.display_name} | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    # FIXED: Add debug info if no MMR changes were found
    if not mmr_changes_by_player:
        embed.add_field(
            name="⚠️ Debug Info",
            value="No MMR changes recorded for this match. This may indicate a system error.",
            inline=False
        )
        print(f"WARNING: No MMR changes found in match result for match {match_id}")

    await interaction.followup.send(embed=embed)

    print(f"Match report display completed for {match_id}")


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
    if not has_admin_or_mod_permissions(interaction.user, interaction.guild):
        await interaction.response.send_message(
            "You need administrator permissions or the 6mod role to use this command.",
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

    # Get player data
    player_data = system_coordinator.match_system.players.find_one({"id": player_id})

    # Check if this is the user checking their own rank
    is_self_check = member.id == interaction.user.id

    # If no player data exists, check for rank verification
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
                # No role or verification found - show rank verification required embed
                if is_self_check:
                    # Show rank verification required embed for self
                    embed = discord.Embed(
                        title="Rank Verification Required",
                        description="You need to verify your Rocket League rank to see your stats.",
                        color=0xf1c40f
                    )
                    embed.add_field(
                        name="How to Verify",
                        value="Visit the rank check page on the website to complete verification.",
                        inline=False
                    )
                    embed.add_field(
                        name="What You Get",
                        value="• Your starting MMR based on your Rocket League rank\n• Access to all queues\n• Stat tracking\n• Leaderboard placement",
                        inline=False
                    )
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    # Show simple message for checking other unverified users
                    embed = discord.Embed(
                        title="No Rank Data",
                        description=f"{member.mention} hasn't verified their rank yet.",
                        color=0x95a5a6
                    )
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                return
        else:
            # Use data from rank record
            tier = rank_record.get("tier", "Rank C")
            mmr = rank_record.get("mmr", 600)

        # Create a temporary player_data object for display with ALL streak fields
        player_data = {
            "name": member.display_name,
            "mmr": mmr,
            "wins": 0,
            "losses": 0,
            "matches": 0,
            "tier": tier,
            "is_new": True,
            "global_mmr": 300,
            "global_wins": 0,
            "global_losses": 0,
            "global_matches": 0,
            # FIXED: Add ALL streak fields with defaults
            "current_streak": 0,
            "longest_win_streak": 0,
            "longest_loss_streak": 0,
            "global_current_streak": 0,
            "global_longest_win_streak": 0,
            "global_longest_loss_streak": 0
        }

    # Calculate stats - add global stats
    mmr = player_data.get("mmr", 0)
    global_mmr = player_data.get("global_mmr", 300)
    wins = player_data.get("wins", 0)
    global_wins = player_data.get("global_wins", 0)
    losses = player_data.get("losses", 0)
    global_losses = player_data.get("global_losses", 0)
    matches = player_data.get("matches", 0)
    global_matches = player_data.get("global_matches", 0)
    is_new = player_data.get("is_new", False)

    # FIXED: Get ALL streak information
    current_streak = player_data.get("current_streak", 0)
    longest_win_streak = player_data.get("longest_win_streak", 0)
    longest_loss_streak = player_data.get("longest_loss_streak", 0)
    global_current_streak = player_data.get("global_current_streak", 0)
    global_longest_win_streak = player_data.get("global_longest_win_streak", 0)
    global_longest_loss_streak = player_data.get("global_longest_loss_streak", 0)

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

        for i, p in enumerate(all_players):
            if p.get("id") == player_id:
                rank_position = i + 1
                break

    # Get global rank position if they've played global games
    global_rank_position = "Unranked"
    if global_matches > 0:
        global_players = list(
            system_coordinator.match_system.players.find({"global_matches": {"$gt": 0}}).sort("global_mmr", -1))
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

    # FIXED: Add ranked streak information
    if current_streak != 0:
        streak_display = ""
        if current_streak > 0:
            if current_streak >= 3:
                streak_display = f"🔥 {current_streak} Win Streak"
            else:
                streak_display = f"{current_streak} Win Streak"
        else:
            if current_streak <= -3:
                streak_display = f"❄️ {abs(current_streak)} Loss Streak"
            else:
                streak_display = f"{abs(current_streak)} Loss Streak"

        embed.add_field(name="Current Streak", value=streak_display, inline=True)

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

    # FIXED: Add global streak information
    if global_current_streak != 0:
        global_streak_display = ""
        if global_current_streak > 0:
            if global_current_streak >= 3:
                global_streak_display = f"🔥 {global_current_streak} Win Streak"
            else:
                global_streak_display = f"{global_current_streak} Win Streak"
        else:
            if global_current_streak <= -3:
                global_streak_display = f"❄️ {abs(global_current_streak)} Loss Streak"
            else:
                global_streak_display = f"{abs(global_current_streak)} Loss Streak"

        embed.add_field(name="Global Streak", value=global_streak_display, inline=True)

    # Add note for new players
    if is_new:
        embed.set_footer(
            text="⭐ New player - this is your starting MMR based on rank verification. Play matches to earn your spot on the leaderboard!")
    else:
        # FIXED: Add comprehensive streak info in footer
        streak_info = []
        if longest_win_streak >= 3:
            streak_info.append(f"Best ranked streak: {longest_win_streak} wins")
        if abs(longest_loss_streak) >= 3:
            streak_info.append(f"Worst ranked streak: {abs(longest_loss_streak)} losses")
        if global_longest_win_streak >= 3:
            streak_info.append(f"Best global streak: {global_longest_win_streak} wins")
        if abs(global_longest_loss_streak) >= 3:
            streak_info.append(f"Worst global streak: {abs(global_longest_loss_streak)} losses")

        if streak_info:
            embed.set_footer(text=" | ".join(streak_info))
        else:
            embed.set_footer(text="Stats updated after each match")

    embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="addplayer", description="Add a player to the queue (Admin/Mod only)")
@app_commands.describe(member="The member to add to the queue")
async def addplayer_slash(interaction: discord.Interaction, member: discord.Member):
    # Check if command is used in an allowed channel
    if not is_queue_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, or global channels.",
            ephemeral=True
        )
        return

    # Check if user has admin permissions
    if not has_admin_or_mod_permissions(interaction.user, interaction.guild):
        await interaction.response.send_message(
            "You need administrator permissions or the 6mod role to use this command.",
            ephemeral=True)
        return

    # Defer response since queue operations might take time
    await interaction.response.defer()

    # Check if the target member has rank verification
    player_id = str(member.id)
    rank_record = db.get_collection('ranks').find_one({"discord_id": player_id})
    rank_a_role = discord.utils.get(interaction.guild.roles, name="Rank A")
    rank_b_role = discord.utils.get(interaction.guild.roles, name="Rank B")
    rank_c_role = discord.utils.get(interaction.guild.roles, name="Rank C")
    has_rank_role = any(role in member.roles for role in [rank_a_role, rank_b_role, rank_c_role])

    if not (rank_record or has_rank_role):
        embed = discord.Embed(
            title="Cannot Add Player",
            description=f"{member.mention} needs to verify their Rocket League rank before being added to the queue.",
            color=0xf1c40f
        )
        embed.add_field(
            name="What they need to do",
            value="The player must visit the rank check page on the website to complete verification.",
            inline=False
        )
        await interaction.followup.send(embed=embed)
        return

    # Create missing rank record if needed
    if has_rank_role and not rank_record:
        tier = "Rank C"
        mmr = 600
        if rank_a_role in member.roles:
            tier = "Rank A"
            mmr = 1600
        elif rank_b_role in member.roles:
            tier = "Rank B"
            mmr = 1100

        try:
            db.get_collection('ranks').insert_one({
                "discord_id": player_id,
                "discord_username": member.display_name,
                "tier": tier,
                "mmr": mmr,
                "timestamp": datetime.datetime.utcnow()
            })
        except Exception as e:
            print(f"Error creating rank record: {e}")

    # Try to add the player to the queue using the existing queue manager
    try:
        response_message = await system_coordinator.queue_manager.add_player(member, interaction.channel)
    except Exception as e:
        print(f"Error adding player to queue: {e}")
        await interaction.followup.send(
            f"An error occurred while adding {member.mention} to the queue. Please try again.")
        return

    # Handle response
    if "QUEUE_ERROR:" in response_message:
        error_msg = response_message.replace("QUEUE_ERROR:", "").strip()

        embed = discord.Embed(
            title="Cannot Add Player",
            description=error_msg,
            color=0xe74c3c
        )
        embed.set_footer(text=f"Admin action by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

    elif "SUCCESS:" in response_message:
        success_msg = response_message.replace("SUCCESS:", "").strip()
        status_data = system_coordinator.queue_manager.get_queue_status(interaction.channel)
        queue_count = status_data['queue_count']

        embed = discord.Embed(
            title="Player Added to Queue",
            description=f"Successfully added {member.mention} to the queue!",
            color=0x00ff00
        )
        embed.add_field(name="Queue Progress", value=f"{'▰' * queue_count}{'▱' * (6 - queue_count)} ({queue_count}/6)",
                        inline=False)

        if queue_count < 6:
            embed.add_field(name="Status", value=f"Waiting for **{6 - queue_count}** more player(s)", inline=False)
        else:
            embed.add_field(name="Status", value="🎉 **Queue is FULL!** Match starting soon...", inline=False)

        embed.set_footer(text=f"Added by admin: {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

    else:
        # Match creation response
        match_id = response_message
        embed = discord.Embed(
            title="Match Starting!",
            description=f"Queue filled! Starting team selection for match `{match_id}`",
            color=0x00ff00
        )
        embed.add_field(name="Match ID", value=f"`{match_id}`", inline=False)
        embed.add_field(name="Added Player", value=f"{member.mention} was the final player needed!", inline=False)
        embed.set_footer(text=f"Queue completed by admin: {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

        # Start voting for the match
        channel_name = interaction.channel.name.lower()
        if channel_name in system_coordinator.vote_systems:
            await asyncio.sleep(1.0)  # Small delay to ensure proper order
            try:
                await system_coordinator.vote_systems[channel_name].start_vote(interaction.channel)
            except Exception as vote_error:
                print(f"Error starting vote after admin add: {vote_error}")


@bot.tree.command(name="removeplayer", description="Remove player(s) from the queue (Admin/Mod only)")
@app_commands.describe(
    member="The specific member to remove (leave empty to remove all players)",
    remove_all="Set to 'yes' to remove all players from the queue"
)
@app_commands.choices(remove_all=[
    app_commands.Choice(name="No - Remove specific player only", value="no"),
    app_commands.Choice(name="Yes - Remove ALL players", value="yes")
])
async def removeplayer_slash(interaction: discord.Interaction, member: discord.Member = None, remove_all: str = "no"):
    # Check if command is used in an allowed channel
    if not is_queue_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, or global channels.",
            ephemeral=True
        )
        return

    # Check if user has admin permissions
    if not has_admin_or_mod_permissions(interaction.user, interaction.guild):
        await interaction.response.send_message(
            "You need administrator permissions or the 6mod role to use this command.",
            ephemeral=True)
        return

    # Defer response first to avoid interaction timeout issues
    await interaction.response.defer()

    # Validate parameters
    if remove_all == "yes" and member is not None:
        await interaction.followup.send(
            "❌ Cannot specify both a member and 'remove all'. Choose one option.",
            ephemeral=True
        )
        return

    if remove_all == "no" and member is None:
        await interaction.followup.send(
            "❌ Please specify a member to remove, or set 'remove_all' to 'yes' to clear the entire queue.",
            ephemeral=True
        )
        return

    # Get current queue status
    status_data = system_coordinator.queue_manager.get_queue_status(interaction.channel)
    queue_count = status_data['queue_count']
    queue_players = status_data['queue_players']

    # If queue is empty
    if queue_count == 0:
        await interaction.followup.send("The queue is already empty!")
        return

    if remove_all == "yes":
        # Remove all players from queue
        player_mentions = [player['mention'] for player in queue_players]

        embed = discord.Embed(
            title="Queue Cleared",
            description=f"Removed **{queue_count}** player(s) from the queue:",
            color=0xff9900
        )

        if player_mentions:
            # Split into chunks if too many players (Discord embed field limit)
            if len(player_mentions) <= 10:
                embed.add_field(name="Players Removed", value=", ".join(player_mentions), inline=False)
            else:
                # Show first 10 and indicate there are more
                shown_mentions = player_mentions[:10]
                embed.add_field(
                    name="Players Removed",
                    value=", ".join(shown_mentions) + f"\n... and {len(player_mentions) - 10} more",
                    inline=False
                )

        embed.set_footer(text=f"Cleared by {interaction.user.display_name}")

        # Clear players from this channel's queue
        channel_id = str(interaction.channel.id)
        system_coordinator.queue_manager.queue_collection.delete_many({"channel_id": channel_id})

        # Update in-memory state
        if channel_id in system_coordinator.queue_manager.channel_queues:
            system_coordinator.queue_manager.channel_queues[channel_id] = []

        await interaction.followup.send(embed=embed)

    else:
        # Remove specific player
        player_id = str(member.id)

        # Check if player is in this channel's queue
        player_in_queue = False
        channel_id = str(interaction.channel.id)

        # Check in-memory queue first
        if channel_id in system_coordinator.queue_manager.channel_queues:
            for p in system_coordinator.queue_manager.channel_queues[channel_id]:
                if p.get('id') == player_id:
                    player_in_queue = True
                    break

        # Double-check in database if not found in memory
        if not player_in_queue:
            db_player = system_coordinator.queue_manager.queue_collection.find_one({
                "id": player_id,
                "channel_id": channel_id
            })
            if db_player:
                player_in_queue = True
                print(f"Found player {member.display_name} in database but not in memory - syncing issue")

        if not player_in_queue:
            # Check if they're in any other queue
            in_other_queue = False
            other_channel_name = None

            # Check in-memory queues
            for other_channel_id, players in system_coordinator.queue_manager.channel_queues.items():
                if other_channel_id == channel_id:  # Skip current channel since we already checked
                    continue
                for p in players:
                    if p.get('id') == player_id:
                        in_other_queue = True
                        try:
                            other_channel = bot.get_channel(int(other_channel_id))
                            if other_channel:
                                other_channel_name = other_channel.name
                        except:
                            pass
                        break
                if in_other_queue:
                    break

            # Also check database for other queues if not found in memory
            if not in_other_queue:
                db_other_queue = system_coordinator.queue_manager.queue_collection.find_one({
                    "id": player_id,
                    "channel_id": {"$ne": channel_id}
                })
                if db_other_queue:
                    in_other_queue = True
                    try:
                        other_channel = bot.get_channel(int(db_other_queue.get("channel_id")))
                        if other_channel:
                            other_channel_name = other_channel.name
                    except:
                        pass

            if in_other_queue:
                embed = discord.Embed(
                    title="Player Not in This Queue",
                    description=f"{member.mention} is not in this channel's queue.",
                    color=0xf1c40f
                )
                if other_channel_name:
                    embed.add_field(
                        name="Current Location",
                        value=f"They are in the **#{other_channel_name}** queue instead.",
                        inline=False
                    )
                await interaction.followup.send(embed=embed)
            else:
                embed = discord.Embed(
                    title="Player Not in Queue",
                    description=f"{member.mention} is not in any queue.",
                    color=0x95a5a6
                )
                await interaction.followup.send(embed=embed)
            return

        # Remove the specific player from database first
        channel_id = str(interaction.channel.id)
        result = system_coordinator.queue_manager.queue_collection.delete_one({
            "id": player_id,
            "channel_id": channel_id
        })

        # Update in-memory state regardless of database result (for sync)
        removed_from_memory = False
        if channel_id in system_coordinator.queue_manager.channel_queues:
            original_count = len(system_coordinator.queue_manager.channel_queues[channel_id])
            system_coordinator.queue_manager.channel_queues[channel_id] = [
                p for p in system_coordinator.queue_manager.channel_queues[channel_id]
                if p.get('id') != player_id
            ]
            new_count = len(system_coordinator.queue_manager.channel_queues[channel_id])
            removed_from_memory = (new_count < original_count)

        # Consider it successful if removed from either database OR memory
        removal_successful = (result.deleted_count > 0) or removed_from_memory

        if removal_successful:
            # Get updated queue status
            updated_status = system_coordinator.queue_manager.get_queue_status(interaction.channel)
            updated_count = updated_status['queue_count']

            embed = discord.Embed(
                title="Player Removed from Queue",
                description=f"Successfully removed {member.mention} from the queue.",
                color=0xff9900
            )
            embed.add_field(name="Updated Queue", value=f"**{updated_count}/6** players remaining", inline=False)

            if updated_count > 0:
                embed.add_field(name="Queue Progress",
                                value=f"{'▰' * updated_count}{'▱' * (6 - updated_count)} ({updated_count}/6)",
                                inline=False)
                embed.add_field(name="Status", value=f"Waiting for **{6 - updated_count}** more player(s)",
                                inline=False)
            else:
                embed.add_field(name="Status", value="Queue is now empty", inline=False)

            embed.set_footer(text=f"Removed by {interaction.user.display_name}")

            # Add debug info if there was a sync issue
            if removed_from_memory and result.deleted_count == 0:
                embed.add_field(name="⚠️ Note", value="Player was removed from memory (sync issue resolved)",
                                inline=False)
            elif result.deleted_count > 0 and not removed_from_memory:
                embed.add_field(name="⚠️ Note", value="Player was removed from database (memory already synced)",
                                inline=False)

            await interaction.followup.send(embed=embed)
        else:
            embed = discord.Embed(
                title="Removal Failed",
                description=f"Failed to remove {member.mention} from the queue. They may have already left or been removed.",
                color=0xe74c3c
            )
            # Add debug info
            embed.add_field(name="Debug Info",
                            value=f"DB result: {result.deleted_count}, Memory removal: {removed_from_memory}",
                            inline=False)
            await interaction.followup.send(embed=embed)


@bot.tree.command(name="removematch", description="Remove/reverse a completed match and its MMR changes (Admin only)")
@app_commands.describe(
    match_id="The ID of the match to remove",
    confirmation="Type 'CONFIRM' to confirm the removal"
)
async def removematch_slash(interaction: discord.Interaction, match_id: str, confirmation: str):
    # Check if command is used in an allowed channel
    if not is_command_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
            ephemeral=True
        )
        return

    # Check if user has admin permissions
    if not has_admin_or_mod_permissions(interaction.user, interaction.guild):
        await interaction.response.send_message(
            "You need administrator permissions or the 6mod role to use this command.",
            ephemeral=True)
        return

    # Check confirmation
    if confirmation != "CONFIRM":
        await interaction.response.send_message(
            "❌ Match removal canceled. You must type 'CONFIRM' (all caps) to confirm this action.",
            ephemeral=True
        )
        return

    # Clean and validate match ID
    match_id = match_id.strip()
    if len(match_id) > 8:
        match_id = match_id[:6]

    # Defer response as this operation could take time
    await interaction.response.defer()

    try:
        # Look for the match in completed matches
        match = system_coordinator.match_system.matches.find_one({
            "match_id": match_id,
            "status": "completed"
        })

        if not match:
            await interaction.followup.send(f"❌ No completed match found with ID `{match_id}`.")
            return

        # Get match details for display
        match_details = {
            "match_id": match_id,
            "team1": match.get("team1", []),
            "team2": match.get("team2", []),
            "winner": match.get("winner"),
            "completed_at": match.get("completed_at"),
            "mmr_changes": match.get("mmr_changes", []),
            "is_global": match.get("is_global", False)
        }

        # Validate we have the necessary data
        if not match_details["mmr_changes"]:
            await interaction.followup.send(f"❌ Match `{match_id}` has no MMR changes to reverse.")
            return

        # Store original player stats for rollback verification
        affected_players = []
        rollback_summary = []

        # Reverse MMR changes for each player
        for mmr_change in match_details["mmr_changes"]:
            player_id = mmr_change.get("player_id")
            if not player_id:
                continue

            # Skip dummy players
            if player_id.startswith('9000'):
                continue

            # Get current player data
            player_data = system_coordinator.match_system.players.find_one({"id": player_id})
            if not player_data:
                rollback_summary.append(f"⚠️ Player {player_id} not found in database")
                continue

            # Get the MMR change details
            mmr_change_amount = mmr_change.get("mmr_change", 0)
            was_win = mmr_change.get("is_win", False)
            was_global = mmr_change.get("is_global", False)
            streak_at_time = mmr_change.get("streak", 0)

            # Store current stats before changes
            if was_global:
                current_mmr = player_data.get("global_mmr", 300)
                current_wins = player_data.get("global_wins", 0)
                current_losses = player_data.get("global_losses", 0)
                current_matches = player_data.get("global_matches", 0)
                current_streak = player_data.get("global_current_streak", 0)
            else:
                current_mmr = player_data.get("mmr", 600)
                current_wins = player_data.get("wins", 0)
                current_losses = player_data.get("losses", 0)
                current_matches = player_data.get("matches", 0)
                current_streak = player_data.get("current_streak", 0)

            # Calculate new values (reverse the changes)
            new_mmr = current_mmr - mmr_change_amount  # Subtract the MMR change
            new_matches = max(0, current_matches - 1)  # Decrease match count

            if was_win:
                new_wins = max(0, current_wins - 1)
                new_losses = current_losses
            else:
                new_wins = current_wins
                new_losses = max(0, current_losses - 1)

            # ROBUST STREAK REVERSAL:
            # The streak stored in MMR changes is what the player had AFTER the match
            # We need to calculate what they had BEFORE by reversing the match outcome
            streak_after_match = streak_at_time

            if was_win:
                # Player won this match
                if streak_after_match > 0:
                    # After winning, they have a positive streak
                    if streak_after_match == 1:
                        # This win started a new streak (they either had 0 or were on a loss streak)
                        # Since we can't know if they had a loss streak before, we'll set to 0
                        new_streak = 0
                    else:
                        # They already had a win streak, so before this win it was 1 less
                        new_streak = streak_after_match - 1
                else:
                    # After winning they don't have a positive streak? This shouldn't happen
                    # But if it does, assume they had no streak before
                    new_streak = 0
            else:
                # Player lost this match
                if streak_after_match < 0:
                    # After losing, they have a negative streak
                    if streak_after_match == -1:
                        # This loss started a new streak (they either had 0 or were on a win streak)
                        # Since we can't know if they had a win streak before, we'll set to 0
                        new_streak = 0
                    else:
                        # They already had a loss streak, so before this loss it was 1 less negative
                        new_streak = streak_after_match + 1
                else:
                    # After losing they don't have a negative streak? This shouldn't happen
                    # But if it does, assume they had no streak before
                    new_streak = 0

            print(
                f"Streak reversal for {player_id}: {current_streak} (current) <- {streak_after_match} (after match) -> {new_streak} (before match, {'win' if was_win else 'loss'} reversed)")

            # Prepare update document
            if was_global:
                update_doc = {
                    "$set": {
                        "global_mmr": max(0, new_mmr),  # Don't go below 0
                        "global_wins": new_wins,
                        "global_losses": new_losses,
                        "global_matches": new_matches,
                        "global_current_streak": new_streak,
                        "last_updated": datetime.datetime.utcnow()
                    }
                }
                mmr_type = "Global"
            else:
                update_doc = {
                    "$set": {
                        "mmr": max(0, new_mmr),  # Don't go below 0
                        "wins": new_wins,
                        "losses": new_losses,
                        "matches": new_matches,
                        "current_streak": new_streak,
                        "last_updated": datetime.datetime.utcnow()
                    }
                }
                mmr_type = "Ranked"

            # Apply the update
            result = system_coordinator.match_system.players.update_one(
                {"id": player_id},
                update_doc
            )

            if result.modified_count > 0:
                # Try to get player name from the match data
                player_name = "Unknown"
                for team in [match_details["team1"], match_details["team2"]]:
                    for p in team:
                        if p.get("id") == player_id:
                            player_name = p.get("name", "Unknown")
                            break
                    if player_name != "Unknown":
                        break

                rollback_summary.append(
                    f"✅ {player_name}: {mmr_type} MMR {current_mmr} → {max(0, new_mmr)} ({mmr_change_amount:+d} reversed), Streak {current_streak} → {new_streak}"
                )
                affected_players.append(player_name)
            else:
                rollback_summary.append(f"⚠️ Failed to update player {player_id}")

        # Delete the match from the database
        delete_result = system_coordinator.match_system.matches.delete_one({"match_id": match_id})

        if delete_result.deleted_count == 0:
            await interaction.followup.send(f"⚠️ Warning: Match `{match_id}` could not be deleted from database.")

        # Create detailed response embed
        embed = discord.Embed(
            title="🗑️ Match Removed Successfully",
            description=f"Match `{match_id}` has been removed and all MMR changes reversed.",
            color=0xff9900
        )

        # Add match details
        team1_names = [p.get("name", "Unknown") for p in match_details["team1"]]
        team2_names = [p.get("name", "Unknown") for p in match_details["team2"]]

        winner_team = "Team 1" if match_details["winner"] == 1 else "Team 2"
        match_type = "Global" if match_details["is_global"] else "Ranked"

        embed.add_field(
            name="Match Details",
            value=(
                f"**Type:** {match_type}\n"
                f"**Winner:** {winner_team}\n"
                f"**Completed:** {match_details['completed_at'].strftime('%Y-%m-%d %H:%M') if match_details['completed_at'] else 'Unknown'}"
            ),
            inline=False
        )

        embed.add_field(
            name="Team 1",
            value=", ".join(team1_names),
            inline=True
        )

        embed.add_field(
            name="Team 2",
            value=", ".join(team2_names),
            inline=True
        )

        # Add rollback summary
        if rollback_summary:
            # Split into chunks if too long
            rollback_text = "\n".join(rollback_summary)
            if len(rollback_text) > 1024:
                # Split into multiple fields
                chunks = []
                current_chunk = []
                current_length = 0

                for line in rollback_summary:
                    if current_length + len(line) + 1 > 1024:
                        chunks.append("\n".join(current_chunk))
                        current_chunk = [line]
                        current_length = len(line)
                    else:
                        current_chunk.append(line)
                        current_length += len(line) + 1

                if current_chunk:
                    chunks.append("\n".join(current_chunk))

                for i, chunk in enumerate(chunks):
                    field_name = f"MMR Changes Reversed {i + 1}/{len(chunks)}" if len(
                        chunks) > 1 else "MMR Changes Reversed"
                    embed.add_field(name=field_name, value=chunk, inline=False)
            else:
                embed.add_field(name="MMR Changes Reversed", value=rollback_text, inline=False)

        embed.add_field(
            name="Summary",
            value=f"**Players Affected:** {len(affected_players)}\n**Database Records:** Match deleted",
            inline=False
        )

        embed.set_footer(
            text=f"Removed by {interaction.user.display_name} | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        await interaction.followup.send(embed=embed)

        # Send notification to affected players (optional)
        if len(affected_players) <= 10:  # Only if reasonable number of players
            notification_embed = discord.Embed(
                title="Match Removed - MMR Restored",
                description=f"Match `{match_id}` has been removed by an administrator and your MMR has been restored.",
                color=0x00ff00
            )

            notification_embed.add_field(
                name="What This Means",
                value="• The match result has been reversed\n• Your MMR has been restored to pre-match values\n• Your win/loss record has been adjusted",
                inline=False
            )

            await interaction.channel.send(embed=notification_embed)

    except Exception as e:
        await interaction.followup.send(f"❌ Error removing match: {str(e)}")
        print(f"Error in removematch command: {e}")
        import traceback
        traceback.print_exc()

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
    if not has_admin_or_mod_permissions(interaction.user, interaction.guild):
        await interaction.response.send_message(
            "You need administrator permissions or the 6mod role to use this command.",
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
        # Just send one complete message instead of multiple
        # Create an embed showing the players in the match
        embed = discord.Embed(
            title="Match Players",
            description=f"Match ID: `{match_id}`\n\nForce starting team selection with the following players:",
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

        await interaction.followup.send(embed=embed)

        # Now start the vote system - without additional messages
        await system_coordinator.vote_systems[channel_name].start_vote(interaction.channel)
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
    if not has_admin_or_mod_permissions(interaction.user, interaction.guild):
        await interaction.response.send_message(
            "You need administrator permissions or the 6mod role to use this command.",
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

    # Create embed to display results - avoiding too many fields
    embed = discord.Embed(
        title="Active Matches Removed",
        description=f"Removed {len(removed_matches)} active match(es) from this channel.",
        color=0xff5555  # Red color
    )

    # Instead of adding each match as a separate field, combine them into chunks
    if removed_matches:
        match_text = []
        for i, match in enumerate(removed_matches, 1):
            match_text.append(
                f"Match {i}: ID `{match['match_id']}` (Status: {match['status']}, Players: {match['player_count']})")

        # Join all matches into a single chunked field (max 10 per field to avoid value length issues)
        chunks = []
        current_chunk = []
        for line in match_text:
            current_chunk.append(line)
            if len(current_chunk) >= 10:
                chunks.append("\n".join(current_chunk))
                current_chunk = []

        if current_chunk:  # Add any remaining items
            chunks.append("\n".join(current_chunk))

        # Add chunks as fields
        for i, chunk in enumerate(chunks):
            embed.add_field(
                name=f"Matches Removed {i + 1}/{len(chunks)}" if len(chunks) > 1 else "Matches Removed",
                value=chunk,
                inline=False
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
                    params_str = "\n".join([f"**{p.name}**: {p.description}" for p in cmd.parameters])
                    embed.add_field(name="Parameters", value=params_str, inline=False)

                await interaction.response.send_message(embed=embed)
                return

        await interaction.response.send_message(f"Command `{command_name}` not found.", ephemeral=True)
        return

    # Create an embed for the command list
    embed = discord.Embed(
        title="🚀 Rocket League 6 Mans Bot Commands",
        description="Your complete guide to 6 Mans commands. Use `/help <command>` for detailed information on any specific command.",
        color=0x00ff00
    )

    # Define commands and descriptions with all current commands
    commands_dict = {
        # Queue Commands
        'queue': 'Join the queue for 6 mans matches in your current channel',
        'leave': 'Leave the current queue if you\'re in one',
        'status': 'View current queue status, players waiting, and active matches',

        # Match Commands
        'report': 'Report your match results (win/loss) using your match ID',
        'leaderboard': 'View a link to the full leaderboard website with rankings and stats',
        'rank': 'Check your personal stats, MMR, and rank (or view another player\'s)',
        'streak': 'View your current win/loss streak and streak history',

        # Admin/Mod Queue Management
        'addplayer': 'Add a specific player to the current queue (Admin/Mod only)',
        'removeplayer': 'Remove a specific player or clear the entire queue (Admin/Mod only)',

        # Admin/Mod Match Management
        'adminreport': 'Force report match results by specifying the winning team (Admin/Mod only)',
        'sub': 'Substitute a player in an active match (Admin/Mod only)',
        'forcestart': 'Force start a match with dummy players if needed (Admin/Mod only)',
        'removeactivematches': 'Cancel all active matches in the current channel (Admin/Mod only)',
        'removematch': 'Remove/reverse the results of a completed match (Admin/Mod only)',

        # Admin/Mod Player Management
        'adjustmmr': 'Manually adjust a player\'s MMR (ranked or global) (Admin/Mod only)',
        'resetplayer': 'Completely reset all data for a specific player (Admin/Mod only)',
        'resetstreak': 'Reset a player\'s current streak or all streak records (Admin/Mod only)',

        # Admin/Mod System Management
        'resetleaderboard': 'Reset leaderboard data (global, ranked, or complete reset) (Admin/Mod only)',
        'topstreaks': 'View leaderboards for highest win/loss streaks (Admin/Mod only)',
        'streakstats': 'View server-wide streak statistics and analytics (Admin/Mod only)',

        # Debug Commands (Admin/Mod only)
        'debugmmr': 'Debug MMR storage issues for a specific match (Admin/Mod only)',
        'testmmr': 'Test MMR calculation manually for a match (Admin/Mod only)',

        # Utility
        'help': 'Display this help menu or get details about specific commands'
    }

    # Group commands by category
    queue_commands = ['queue', 'leave', 'status']
    match_commands = ['report', 'leaderboard', 'rank', 'streak']
    queue_admin_commands = ['addplayer', 'removeplayer']
    match_admin_commands = ['adminreport', 'sub', 'forcestart', 'removeactivematches', 'removematch']
    player_admin_commands = ['adjustmmr', 'resetplayer', 'resetstreak']
    system_admin_commands = ['resetleaderboard', 'topstreaks', 'streakstats']
    debug_commands = ['debugmmr', 'testmmr']
    utility_commands = ['help']

    # Add command fields grouped by category with improved formatting
    embed.add_field(
        name="📋 Queue Management",
        value="\n".join([f"`/{cmd}` - {commands_dict[cmd]}" for cmd in queue_commands]),
        inline=False
    )

    embed.add_field(
        name="🎮 Match & Player Commands",
        value="\n".join([f"`/{cmd}` - {commands_dict[cmd]}" for cmd in match_commands]),
        inline=False
    )

    embed.add_field(
        name="👥 Admin: Queue Management",
        value="\n".join([f"`/{cmd}` - {commands_dict[cmd]}" for cmd in queue_admin_commands]),
        inline=False
    )

    embed.add_field(
        name="⚔️ Admin: Match Management",
        value="\n".join([f"`/{cmd}` - {commands_dict[cmd]}" for cmd in match_admin_commands]),
        inline=False
    )

    embed.add_field(
        name="🎯 Admin: Player Management",
        value="\n".join([f"`/{cmd}` - {commands_dict[cmd]}" for cmd in player_admin_commands]),
        inline=False
    )

    embed.add_field(
        name="🔧 Admin: System Management",
        value="\n".join([f"`/{cmd}` - {commands_dict[cmd]}" for cmd in system_admin_commands]),
        inline=False
    )

    embed.add_field(
        name="🐛 Admin: Debug Tools",
        value="\n".join([f"`/{cmd}` - {commands_dict[cmd]}" for cmd in debug_commands]),
        inline=False
    )

    embed.add_field(
        name="🛠️ Utility Commands",
        value="\n".join([f"`/{cmd}` - {commands_dict[cmd]}" for cmd in utility_commands]),
        inline=False
    )

    # Add "How 6 Mans Works" section with improved formatting
    embed.add_field(
        name="📖 How 6 Mans Works:",
        value=(
            "**1.** Use `/queue` in a rank channel (rank-a, rank-b, rank-c, or global)\n"
            "**2.** When 6 players join, automated team voting begins\n"
            "**3.** Vote for team setup: ⚖️ Balanced, 🎲 Random, or 👑 Captains\n"
            "**4.** Teams are finalized based on community votes\n"
            "**5.** Play your match and report results with `/report <match_id> win/loss`\n"
            "**6.** Check updated rankings with `/leaderboard` or personal stats with `/rank`\n"
            "**7.** Track your performance streaks with `/streak`"
        ),
        inline=False
    )

    # Enhanced streak system section
    embed.add_field(
        name="🔥 Advanced Streak System:",
        value=(
            "**Enhanced Streak Tracking**\n"
            "• **Win Streaks (3+)**: Bonus MMR with 🔥 indicator\n"
            "• **Loss Streaks (3+)**: MMR penalties with ❄️ indicator\n"
            "• **Streak Multipliers**: Longer streaks = bigger impact (up to +50%)\n"
            "• **Dual Tracking**: Separate streaks for ranked and global matches\n"
            "• **Live Monitoring**: Use `/streak` to check current status\n"
            "• **Admin Analytics**: `/topstreaks` and `/streakstats` for insights"
        ),
        inline=False
    )

    # Add admin tools section
    embed.add_field(
        name="👑 Admin/Moderator Tools:",
        value=(
            "**Queue Control**: Add/remove players, force start matches\n"
            "**Match Management**: Substitute players, remove matches, admin reports\n"
            "**Player Management**: Adjust MMR, reset player data, manage streaks\n"
            "**System Control**: Reset leaderboards, view analytics, debug tools\n"
            "**Requires**: Administrator permissions or '6mod' role"
        ),
        inline=False
    )

    # Add rank system explanation
    embed.add_field(
        name="🏆 Dual MMR System:",
        value=(
            "**Ranked Queues**:\n"
            "• **Rank A** (1600+ MMR) - Expert players\n"
            "• **Rank B** (1100-1599 MMR) - Intermediate players\n"
            "• **Rank C** (600-1099 MMR) - Developing players\n\n"
            "**Global Queue**: Mixed ranks, separate MMR system (300+ MMR)\n"
            "*Players must verify their Rocket League rank before joining*"
        ),
        inline=False
    )

    # Enhanced footer with command count
    total_commands = len(commands_dict)
    admin_commands = len(queue_admin_commands + match_admin_commands + player_admin_commands +
                        system_admin_commands + debug_commands + ['purgechat'])
    user_commands = total_commands - admin_commands

    embed.set_footer(
        text=f"💡 {total_commands} total commands • {user_commands} user commands • {admin_commands} admin commands • Use /help <command> for details"
    )

    await interaction.response.send_message(embed=embed)


# Error handlers
@bot.event
async def on_error(event, *args, **kwargs):
    print(f"Error in event {event}: {args} {kwargs}")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    print(f"Command error: {error}")

    try:
        if isinstance(error, app_commands.errors.CommandNotFound):
            if not interaction.response.is_done():
                await interaction.response.send_message("Command not found. Use `/help` to see available commands.",
                                                        ephemeral=True)
        elif isinstance(error, app_commands.errors.MissingPermissions):
            if not interaction.response.is_done():
                await interaction.response.send_message("You don't have permission to use this command.",
                                                        ephemeral=True)
        elif isinstance(error, app_commands.errors.CommandInvokeError):
            if isinstance(error.original, discord.errors.NotFound):
                print(f"Interaction timed out: {error.original}")
                return
            else:
                print(f"Command invoke error: {error.original}")
                if not interaction.response.is_done():
                    await interaction.response.send_message("An error occurred. Please try again.", ephemeral=True)
        else:
            if not interaction.response.is_done():
                await interaction.response.send_message("An error occurred. Please try again.", ephemeral=True)
    except Exception as e:
        print(f"Error in error handler: {e}")


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
    if not has_admin_or_mod_permissions(interaction.user, interaction.guild):
        await interaction.response.send_message(
            "You need administrator permissions or the 6mod role to use this command.",
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
    if not has_admin_or_mod_permissions(interaction.user, interaction.guild):
        await interaction.response.send_message(
            "You need administrator permissions or the 6mod role to use this command.",
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
    roles_removed_count = 0
    role_removal_errors = []

    if reset_type == "global":
        # Reset only global stats
        result = system_coordinator.match_system.players.update_many(
            {},
            {"$set": {
                "global_mmr": 300,
                "global_wins": 0,
                "global_losses": 0,
                "global_matches": 0,
                "global_current_streak": 0,
                "global_longest_win_streak": 0,
                "global_longest_loss_streak": 0
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
                    "matches": 0,
                    "current_streak": 0,
                    "longest_win_streak": 0,
                    "longest_loss_streak": 0
                }}
            )
            reset_count += 1

        # Reset ranked matches
        matches_result = system_coordinator.match_system.matches.delete_many({"is_global": {"$ne": True}})
        matches_removed = matches_result.deleted_count

    else:  # "all" - Complete reset including Discord roles
        # 1. Make backup of rank verification data
        ranks_collection = db.get_collection('ranks')
        all_ranks = list(ranks_collection.find())
        backup_ranks_collection = db.get_collection(f"ranks_backup_{timestamp}")
        if all_ranks:
            backup_ranks_collection.insert_many(all_ranks)

        # 2. IMPROVED DISCORD ROLE REMOVAL
        print("=== STARTING IMPROVED DISCORD ROLE REMOVAL ===")

        # Get rank roles
        rank_role_names = ["Rank A", "Rank B", "Rank C"]
        rank_roles = {}

        for role in interaction.guild.roles:
            if role.name in rank_role_names:
                rank_roles[role.name] = role
                print(f"Found rank role: {role.name} (ID: {role.id})")

        if not rank_roles:
            print("❌ NO RANK ROLES FOUND!")
        else:
            print(f"Found {len(rank_roles)} rank roles")

            # Get ALL guild members (not just from database)
            all_guild_members = []

            # Method 1: Try to get all members from guild cache first
            print(f"Guild member count from cache: {interaction.guild.member_count}")
            cached_members = list(interaction.guild.members)
            print(f"Actually cached members: {len(cached_members)}")

            if len(cached_members) < interaction.guild.member_count:
                # Cache might be incomplete, try to fetch more
                print("Cache appears incomplete, attempting to fetch more members...")
                try:
                    # Try to fetch all members with chunking
                    await interaction.guild.chunk(cache=True)
                    cached_members = list(interaction.guild.members)
                    print(f"After chunking: {len(cached_members)} members")
                except Exception as e:
                    print(f"Error during chunking: {e}")

            # Process each cached member
            processed_count = 0
            for member in cached_members:
                # Skip bots
                if member.bot:
                    continue

                try:
                    # Check if member has any rank roles
                    member_rank_roles = [role for role in member.roles if role.name in rank_role_names]

                    if member_rank_roles:
                        print(
                            f"Processing member: {member.display_name} (ID: {member.id}) with roles: {[r.name for r in member_rank_roles]}")

                        try:
                            # Remove all rank roles from this member
                            await member.remove_roles(*member_rank_roles, reason="Complete leaderboard reset")
                            roles_removed_count += 1
                            print(f"✅ Removed {len(member_rank_roles)} rank role(s) from {member.display_name}")

                            # Small delay to avoid rate limits
                            await asyncio.sleep(0.3)

                        except discord.Forbidden:
                            error_msg = f"No permission to remove roles from {member.display_name}"
                            print(f"❌ {error_msg}")
                            role_removal_errors.append(error_msg)
                        except discord.HTTPException as e:
                            error_msg = f"HTTP error removing roles from {member.display_name}: {e}"
                            print(f"❌ {error_msg}")
                            role_removal_errors.append(error_msg)
                        except Exception as e:
                            error_msg = f"Unexpected error removing roles from {member.display_name}: {e}"
                            print(f"❌ {error_msg}")
                            role_removal_errors.append(error_msg)

                    processed_count += 1

                except Exception as e:
                    error_msg = f"Error processing member {member.display_name}: {e}"
                    print(f"❌ {error_msg}")
                    role_removal_errors.append(error_msg)
                    continue

            print(
                f"=== ROLE REMOVAL COMPLETE: Processed {processed_count} members, removed roles from {roles_removed_count} members ===")

            # If we couldn't find many members with roles, also check database players
            if roles_removed_count == 0 and all_players:
                print("No members found with roles via guild cache, checking database players...")

                for player in all_players:
                    player_id = player.get("id")

                    # Skip dummy players
                    if not player_id or player_id.startswith('9000'):
                        continue

                    try:
                        member = interaction.guild.get_member(int(player_id))
                        if not member:
                            # Try to fetch if not in cache
                            try:
                                member = await interaction.guild.fetch_member(int(player_id))
                            except (discord.NotFound, discord.HTTPException):
                                print(f"Member not found: {player.get('name')} (ID: {player_id})")
                                continue

                        if member:
                            # Check if member has any rank roles
                            member_rank_roles = [role for role in member.roles if role.name in rank_role_names]

                            if member_rank_roles:
                                try:
                                    await member.remove_roles(*member_rank_roles, reason="Complete leaderboard reset")
                                    roles_removed_count += 1
                                    print(f"✅ Removed roles from database player: {member.display_name}")
                                    await asyncio.sleep(0.3)
                                except Exception as e:
                                    error_msg = f"Error removing roles from {member.display_name}: {e}"
                                    role_removal_errors.append(error_msg)

                    except (ValueError, TypeError):
                        print(f"Invalid player ID: {player_id}")
                        continue
                    except Exception as e:
                        error_msg = f"Error processing database player {player.get('name')}: {e}"
                        role_removal_errors.append(error_msg)
                        continue

        # 3. DELETE all player records
        players_removed = system_coordinator.match_system.players.delete_many({}).deleted_count
        print(f"Removed {players_removed} player records for complete reset")
        reset_count = players_removed

        # 4. Delete all rank verification records
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
        "backup_collection": backup_collection_name,
        "roles_removed_count": roles_removed_count,
        "role_removal_errors_count": len(role_removal_errors) if reset_type == "all" else 0
    })

    # Send detailed completion report
    embed = discord.Embed(
        title="🔄 Leaderboard Reset Complete",
        description=f"Reset type: **{reset_type.upper()}**",
        color=0xff9900 if reset_type == "all" else 0x00ff00
    )

    embed.add_field(
        name="Database Reset",
        value=f"Players affected: {reset_count}/{player_count}\nMatches removed: {matches_removed}",
        inline=False
    )

    embed.add_field(
        name="Backup Created",
        value=f"Collection: `{backup_collection_name}`",
        inline=False
    )

    if reset_type == "all":
        embed.add_field(
            name="Discord Role Removal",
            value=f"✅ Removed roles from: **{roles_removed_count}** members\n❌ Errors encountered: **{len(role_removal_errors)}** members",
            inline=False
        )

        embed.add_field(
            name="Rank Verification Reset",
            value=f"**{ranks_removed}** rank verifications removed. All players must re-verify.",
            inline=False
        )

        if role_removal_errors and len(role_removal_errors) <= 10:
            # Show errors if there are 10 or fewer
            error_sample = "\n".join(role_removal_errors)
            embed.add_field(
                name="Role Removal Issues",
                value=f"```{error_sample}```",
                inline=False
            )
        elif role_removal_errors:
            # Show sample of errors if there are many
            error_sample = "\n".join(role_removal_errors[:3])
            error_sample += f"\n... and {len(role_removal_errors) - 3} more"
            embed.add_field(
                name="Role Removal Issues (Sample)",
                value=f"```{error_sample}```",
                inline=False
            )

    embed.set_footer(
        text=f"Reset by {interaction.user.display_name} | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    await interaction.followup.send(embed=embed)

    # Final announcement for complete reset
    if reset_type == "all":
        announcement = discord.Embed(
            title="🔄 Complete Season Reset",
            description=f"A complete leaderboard reset has been performed by {interaction.user.mention}",
            color=0xff0000
        )

        announcement.add_field(
            name="🚨 IMPORTANT: Complete Reset Performed 🚨",
            value=(
                f"**{roles_removed_count}** members had their Discord rank roles removed.\n"
                f"**All players must re-verify their ranks** before joining queues again."
            ),
            inline=False
        )

        announcement.add_field(
            name="How to Re-verify",
            value=(
                "1. Visit the rank verification page on the website\n"
                "2. Select your current Rocket League rank\n"
                "3. Get your Discord role and starting MMR back"
            ),
            inline=False
        )

        if role_removal_errors:
            announcement.add_field(
                name="Manual Review Required",
                value=f"⚠️ {len(role_removal_errors)} members may need manual role removal by an admin.",
                inline=False
            )

        await interaction.channel.send(embed=announcement)


# Add this command to your main.py file, after the other slash commands

@bot.tree.command(name="resetplayer", description="Reset all data for a specific player (Admin only)")
@app_commands.describe(
    member="The member whose data you want to reset",
    confirmation="Type 'CONFIRM' to confirm the reset"
)
async def resetplayer_slash(interaction: discord.Interaction, member: discord.Member, confirmation: str):
    # Check if command is used in an allowed channel
    if not is_command_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
            ephemeral=True
        )
        return

    # Check if user has admin permissions
    if not has_admin_or_mod_permissions(interaction.user, interaction.guild):
        await interaction.response.send_message(
            "You need administrator permissions or the 6mod role to use this command.",
            ephemeral=True)
        return

    # Check confirmation
    if confirmation != "CONFIRM":
        await interaction.response.send_message(
            "❌ Player reset canceled. You must type 'CONFIRM' (all caps) to confirm this action.",
            ephemeral=True
        )
        return

    # Defer response as this operation could take time
    await interaction.response.defer()

    player_id = str(member.id)
    player_name = member.display_name

    # Check if player is currently in an active match
    if player_id in system_coordinator.queue_manager.player_matches:
        match_id = system_coordinator.queue_manager.player_matches[player_id]
        await interaction.followup.send(
            f"❌ Cannot reset {member.mention} - they are currently in an active match (ID: `{match_id}`). "
            "Please wait for the match to complete or use `/removeactivematches` first.",
            ephemeral=True
        )
        return

    # Check if player is in any queue
    player_in_queue = False
    queue_channel = None
    for channel_id, players in system_coordinator.queue_manager.channel_queues.items():
        for p in players:
            if p.get('id') == player_id:
                player_in_queue = True
                try:
                    queue_channel = bot.get_channel(int(channel_id))
                except:
                    queue_channel = None
                break
        if player_in_queue:
            break

    if player_in_queue:
        # Remove player from queue first
        try:
            result = system_coordinator.queue_manager.queue_collection.delete_one({
                "id": player_id
            })

            # Update in-memory state
            for channel_id, players in system_coordinator.queue_manager.channel_queues.items():
                system_coordinator.queue_manager.channel_queues[channel_id] = [
                    p for p in players if p.get('id') != player_id
                ]

            queue_info = f" (removed from queue in {queue_channel.mention if queue_channel else 'unknown channel'})"
        except Exception as e:
            queue_info = f" (warning: could not remove from queue - {str(e)})"
    else:
        queue_info = ""

    # Initialize counters for what we're resetting
    reset_summary = {
        "player_data": False,
        "rank_verification": False,
        "discord_roles": False,
        "queue_removal": player_in_queue,
        "errors": []
    }

    try:
        # 1. Get current player data before deletion (for summary)
        player_data = system_coordinator.match_system.players.find_one({"id": player_id})

        current_stats = {}
        if player_data:
            current_stats = {
                "ranked_mmr": player_data.get("mmr", 0),
                "global_mmr": player_data.get("global_mmr", 300),
                "ranked_matches": player_data.get("matches", 0),
                "global_matches": player_data.get("global_matches", 0),
                "ranked_wins": player_data.get("wins", 0),
                "global_wins": player_data.get("global_wins", 0),
                "ranked_losses": player_data.get("losses", 0),
                "global_losses": player_data.get("global_losses", 0),
                "current_streak": player_data.get("current_streak", 0),
                "global_current_streak": player_data.get("global_current_streak", 0)
            }

        # 2. Delete player data from players collection
        try:
            result = system_coordinator.match_system.players.delete_one({"id": player_id})
            if result.deleted_count > 0:
                reset_summary["player_data"] = True
                print(f"Deleted player data for {player_name} (ID: {player_id})")
            else:
                print(f"No player data found for {player_name} (ID: {player_id})")
        except Exception as e:
            reset_summary["errors"].append(f"Failed to delete player data: {str(e)}")

        # 3. Delete rank verification from ranks collection
        try:
            ranks_collection = db.get_collection('ranks')
            result = ranks_collection.delete_one({"discord_id": player_id})
            if result.deleted_count > 0:
                reset_summary["rank_verification"] = True
                print(f"Deleted rank verification for {player_name} (ID: {player_id})")
            else:
                print(f"No rank verification found for {player_name} (ID: {player_id})")
        except Exception as e:
            reset_summary["errors"].append(f"Failed to delete rank verification: {str(e)}")

        # 4. Remove Discord rank roles
        try:
            # Get rank roles
            rank_a_role = discord.utils.get(interaction.guild.roles, name="Rank A")
            rank_b_role = discord.utils.get(interaction.guild.roles, name="Rank B")
            rank_c_role = discord.utils.get(interaction.guild.roles, name="Rank C")
            rank_roles = [role for role in [rank_a_role, rank_b_role, rank_c_role] if role]

            # Check if member has any rank roles
            member_rank_roles = [role for role in member.roles if role in rank_roles]

            if member_rank_roles:
                await member.remove_roles(*member_rank_roles, reason=f"Player reset by {interaction.user.display_name}")
                reset_summary["discord_roles"] = True
                print(f"Removed {len(member_rank_roles)} rank role(s) from {player_name}")
            else:
                print(f"No rank roles found for {player_name}")

        except discord.Forbidden:
            reset_summary["errors"].append("No permission to remove Discord roles")
        except discord.HTTPException as e:
            reset_summary["errors"].append(f"Discord error removing roles: {str(e)}")
        except Exception as e:
            reset_summary["errors"].append(f"Unexpected error removing roles: {str(e)}")

        # 5. Remove player from player_matches tracking (if somehow still there)
        if player_id in system_coordinator.queue_manager.player_matches:
            del system_coordinator.queue_manager.player_matches[player_id]

    except Exception as e:
        reset_summary["errors"].append(f"Unexpected error during reset: {str(e)}")

    # Create detailed response embed
    embed = discord.Embed(
        title=f"🔄 Player Reset Complete",
        description=f"Reset data for {member.mention} ({member.display_name})",
        color=0xff9900 if reset_summary["errors"] else 0x00ff00
    )

    # Add what was reset
    reset_items = []
    if reset_summary["player_data"]:
        reset_items.append("✅ Player statistics and MMR data")
    if reset_summary["rank_verification"]:
        reset_items.append("✅ Rank verification record")
    if reset_summary["discord_roles"]:
        reset_items.append("✅ Discord rank roles")
    if reset_summary["queue_removal"]:
        reset_items.append("✅ Removed from queue")

    if not reset_items:
        reset_items.append("ℹ️ No data found to reset")

    embed.add_field(
        name="Data Reset",
        value="\n".join(reset_items),
        inline=False
    )

    # Add previous stats if we had any
    if current_stats and (current_stats["ranked_matches"] > 0 or current_stats["global_matches"] > 0):
        stats_text = []
        if current_stats["ranked_matches"] > 0:
            stats_text.append(f"**Ranked:** {current_stats['ranked_wins']}W-{current_stats['ranked_losses']}L "
                              f"({current_stats['ranked_matches']} matches, {current_stats['ranked_mmr']} MMR)")
        if current_stats["global_matches"] > 0:
            stats_text.append(f"**Global:** {current_stats['global_wins']}W-{current_stats['global_losses']}L "
                              f"({current_stats['global_matches']} matches, {current_stats['global_mmr']} MMR)")

        embed.add_field(
            name="Previous Stats",
            value="\n".join(stats_text) if stats_text else "No previous match data",
            inline=False
        )

    # Add queue removal info
    if queue_info:
        embed.add_field(
            name="Queue Status",
            value=f"Player was{queue_info}",
            inline=False
        )

    # Add errors if any
    if reset_summary["errors"]:
        error_text = "\n".join([f"❌ {error}" for error in reset_summary["errors"]])
        embed.add_field(
            name="Errors Encountered",
            value=error_text,
            inline=False
        )

    # Add instructions for the player
    embed.add_field(
        name="Next Steps",
        value=(
            f"{member.mention} will need to:\n"
            "1. Visit the rank verification page on the website\n"
            "2. Re-verify their Rocket League rank\n"
            "3. Get their Discord role and starting MMR back\n"
            "4. Join queues again to start playing"
        ),
        inline=False
    )

    embed.set_footer(
        text=f"Reset by {interaction.user.display_name} | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    await interaction.followup.send(embed=embed)

    # Send a DM to the player (optional, with error handling)
    try:
        dm_embed = discord.Embed(
            title="Your 6 Mans Data Has Been Reset",
            description="An administrator has reset your 6 Mans player data.",
            color=0xffa500
        )

        dm_embed.add_field(
            name="What This Means",
            value=(
                "• All your match history and MMR have been cleared\n"
                "• Your rank verification has been removed\n"
                "• Your Discord rank role has been removed"
            ),
            inline=False
        )

        dm_embed.add_field(
            name="To Play Again",
            value=(
                "1. Visit the rank verification page on our website\n"
                "2. Re-verify your Rocket League rank\n"
                "3. You'll get your Discord role and starting MMR back\n"
                "4. You can then join queues again"
            ),
            inline=False
        )

        dm_embed.set_footer(text="If you have questions, contact a server administrator")

        await member.send(embed=dm_embed)
        print(f"Sent reset notification DM to {player_name}")

    except discord.Forbidden:
        print(f"Could not send DM to {player_name} - DMs disabled")
    except Exception as dm_error:
        print(f"Error sending DM to {player_name}: {str(dm_error)}")

    print(f"Player reset completed for {player_name} by {interaction.user.display_name}")


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

    # Check if user has permissions - FIXED: Use the correct function name
    if not has_admin_or_mod_permissions(interaction.user, interaction.guild):
        await interaction.response.send_message(
            "You need administrator permissions or the 6mod role to use this command.",
            ephemeral=True)
        return

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

    # REMOVED: The problematic is_admin check that was causing the error
    # The permission check above is sufficient

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


@bot.tree.command(name="streak", description="Check your current streak or another player's streak")
@app_commands.describe(member="The member whose streak you want to check (optional)")
async def streak_slash(interaction: discord.Interaction, member: discord.Member = None):
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
    # Fix: Use players.find_one directly instead of get_player_stats
    player_data = system_coordinator.match_system.players.find_one({"id": player_id})

    if not player_data:
        await interaction.response.send_message(
            f"{member.mention} hasn't played any matches yet. No streak information available.",
            ephemeral=True
        )
        return

    # Extract streak information
    # Ranked Streaks
    current_streak = player_data.get("current_streak", 0)
    longest_win_streak = player_data.get("longest_win_streak", 0)
    longest_loss_streak = player_data.get("longest_loss_streak", 0)

    # Global Streaks
    global_current_streak = player_data.get("global_current_streak", 0)
    global_longest_win_streak = player_data.get("global_longest_win_streak", 0)
    global_longest_loss_streak = player_data.get("global_longest_loss_streak", 0)

    # Get general player stats
    mmr = player_data.get("mmr", 0)
    global_mmr = player_data.get("global_mmr", 300)
    matches = player_data.get("matches", 0)
    global_matches = player_data.get("global_matches", 0)
    wins = player_data.get("wins", 0)
    losses = player_data.get("losses", 0)
    global_wins = player_data.get("global_wins", 0)
    global_losses = player_data.get("global_losses", 0)

    # Calculate win rates
    win_rate = round((wins / matches) * 100, 1) if matches > 0 else 0
    global_win_rate = round((global_wins / global_matches) * 100, 1) if global_matches > 0 else 0

    # Create embed for streaks
    embed = discord.Embed(
        title=f"{member.display_name}'s Streak Status",
        description="View both ranked and global streak information below",
        color=0x7289da  # Discord Blurple as default
    )

    # Add player avatar
    embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)

    # RANKED STREAKS SECTION
    embed.add_field(
        name="📊 RANKED STREAKS",
        value="",
        inline=False
    )

    # Format ranked streak info
    if current_streak > 0:
        streak_color = 0x43b581  # Green
        streak_icon = "🔥" if current_streak >= 3 else "↗️"
        streak_text = f"{streak_icon} **{current_streak}** Win Streak"
        streak_desc = "On fire! Each win gives bonus MMR."
    elif current_streak < 0:
        streak_color = 0xf04747  # Red
        streak_icon = "❄️" if current_streak <= -3 else "↘️"
        streak_text = f"{streak_icon} **{abs(current_streak)}** Loss Streak"
        streak_desc = "In a slump. Each loss costs extra MMR."
    else:
        streak_color = 0x7289da  # Discord Blurple
        streak_text = "No active streak"
        streak_desc = "No active win or loss streak."

    # Main ranked streak info
    embed.add_field(
        name="Current Ranked Streak",
        value=f"{streak_text}\n{streak_desc}",
        inline=False
    )

    # Ranked MMR impact info
    if abs(current_streak) >= 3:
        bonus_percent = min((abs(current_streak) - 3 + 1) * 10, 50)
        embed.add_field(
            name="Ranked MMR Impact",
            value=f"**+{bonus_percent}%** {'bonus' if current_streak > 0 else 'penalty'} to MMR changes",
            inline=False
        )

    # Ranked personal bests
    embed.add_field(
        name="Best Ranked Win Streak",
        value=f"🏆 **{longest_win_streak}** wins" if longest_win_streak > 0 else "None yet",
        inline=True
    )

    embed.add_field(
        name="Worst Ranked Loss Streak",
        value=f"📉 **{abs(longest_loss_streak)}** losses" if longest_loss_streak < 0 else "None yet",
        inline=True
    )

    # Ranked stats
    embed.add_field(
        name="Ranked Stats",
        value=f"MMR: **{mmr}**\nRecord: **{wins}W-{losses}L**\nWin Rate: **{win_rate}%**",
        inline=False
    )

    # Add a separator
    embed.add_field(
        name="⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯",
        value="",
        inline=False
    )

    # GLOBAL STREAKS SECTION
    embed.add_field(
        name="🌐 GLOBAL STREAKS",
        value="",
        inline=False
    )

    # Format global streak info
    if global_current_streak > 0:
        global_streak_icon = "🔥" if global_current_streak >= 3 else "↗️"
        global_streak_text = f"{global_streak_icon} **{global_current_streak}** Win Streak"
        global_streak_desc = "On fire in global matches!"
    elif global_current_streak < 0:
        global_streak_icon = "❄️" if global_current_streak <= -3 else "↘️"
        global_streak_text = f"{global_streak_icon} **{abs(global_current_streak)}** Loss Streak"
        global_streak_desc = "In a global slump."
    else:
        global_streak_text = "No active global streak"
        global_streak_desc = "No active global win or loss streak."

    # Main global streak info
    embed.add_field(
        name="Current Global Streak",
        value=f"{global_streak_text}\n{global_streak_desc}",
        inline=False
    )

    # Global MMR impact info
    if abs(global_current_streak) >= 3:
        global_bonus_percent = min((abs(global_current_streak) - 3 + 1) * 10, 50)
        embed.add_field(
            name="Global MMR Impact",
            value=f"**+{global_bonus_percent}%** {'bonus' if global_current_streak > 0 else 'penalty'} to Global MMR changes",
            inline=False
        )

    # Global personal bests
    embed.add_field(
        name="Best Global Win Streak",
        value=f"🏆 **{global_longest_win_streak}** wins" if global_longest_win_streak > 0 else "None yet",
        inline=True
    )

    embed.add_field(
        name="Worst Global Loss Streak",
        value=f"📉 **{abs(global_longest_loss_streak)}** losses" if global_longest_loss_streak < 0 else "None yet",
        inline=True
    )

    # Global stats
    embed.add_field(
        name="Global Stats",
        value=f"Global MMR: **{global_mmr}**\nRecord: **{global_wins}W-{global_losses}L**\nWin Rate: **{global_win_rate}%**",
        inline=False
    )

    # Tips based on streak
    if current_streak >= 3 or global_current_streak >= 3:
        embed.add_field(
            name="💡 Tip",
            value="Keep playing while you're hot! You're earning bonus MMR on each win.",
            inline=False
        )
    elif current_streak <= -3 or global_current_streak <= -3:
        embed.add_field(
            name="💡 Tip",
            value="Consider taking a short break or changing up your strategy. Each additional loss is costing you extra MMR.",
            inline=False
        )

    # Add footer with explanation
    embed.set_footer(
        text="Streaks of 3+ affect MMR gains and losses. The longer the streak, the bigger the impact!")

    # Set embed color based on most significant streak
    if abs(current_streak) >= abs(global_current_streak):
        # Ranked streak is more significant
        if current_streak > 0:
            embed.color = 0x43b581  # Green for win streak
        elif current_streak < 0:
            embed.color = 0xf04747  # Red for loss streak
    else:
        # Global streak is more significant
        if global_current_streak > 0:
            embed.color = 0x43b581  # Green for win streak
        elif global_current_streak < 0:
            embed.color = 0xf04747  # Red for loss streak

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="topstreaks", description="Show players with the highest win or loss streaks (Admin only)")
@app_commands.describe(
    streak_type="Type of streak to view",
    mode="Ranked or Global mode",
    limit="Number of players to show (1-25)"
)
@app_commands.choices(streak_type=[
    app_commands.Choice(name="Win Streaks", value="win"),
    app_commands.Choice(name="Loss Streaks", value="loss"),
    app_commands.Choice(name="Current Streaks", value="current")
])
@app_commands.choices(mode=[
    app_commands.Choice(name="Ranked", value="ranked"),
    app_commands.Choice(name="Global", value="global")
])
async def topstreaks_slash(
        interaction: discord.Interaction,
        streak_type: str,
        mode: str = "ranked",
        limit: int = 10
):
    # Check if command is used in an allowed channel
    if not is_command_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
            ephemeral=True
        )
        return

    # Check if user has admin permissions
    if not has_admin_or_mod_permissions(interaction.user, interaction.guild):
        await interaction.response.send_message(
            "You need administrator permissions or the 6mod role to use this command.",
            ephemeral=True)
        return

    # Validate limit
    limit = max(1, min(25, limit))

    # Start deferred response since this might take time
    await interaction.response.defer()

    try:
        # Determine if we're looking at ranked or global mode
        is_global = mode == "global"
        mode_prefix = "Global " if is_global else "Ranked "

        # Set up query based on streak type and mode
        if streak_type == "win":
            # Get players with highest win streaks
            field_name = "global_longest_win_streak" if is_global else "longest_win_streak"
            query = {field_name: {"$gt": 0}}
            sort_field = field_name
            sort_dir = -1  # Descending
            title = f"Top {limit} {mode_prefix}Win Streaks"
            embed_color = 0x43b581  # Green
        elif streak_type == "loss":
            # Get players with worst loss streaks
            field_name = "global_longest_loss_streak" if is_global else "longest_loss_streak"
            query = {field_name: {"$lt": 0}}
            sort_field = field_name
            sort_dir = 1  # Ascending (for negative values)
            title = f"Top {limit} {mode_prefix}Loss Streaks"
            embed_color = 0xf04747  # Red
        else:  # current
            # Get players with highest absolute current streaks
            field_name = "global_current_streak" if is_global else "current_streak"
            query = {field_name: {"$ne": 0}}
            sort_field = field_name
            sort_dir = -1
            title = f"Top {limit} Current {mode_prefix}Streaks"
            embed_color = 0x7289da  # Discord Blurple

        # Get players from database
        if streak_type == "current":
            # For current streaks, we need to sort by absolute value
            pipeline = [
                {"$match": query},
                {"$addFields": {
                    "abs_streak": {"$abs": f"${field_name}"}
                }},
                {"$sort": {"abs_streak": -1}},
                {"$limit": limit}
            ]
            players = list(system_coordinator.match_system.players.aggregate(pipeline))
        else:
            players = list(system_coordinator.match_system.players.find(query)
                           .sort(sort_field, sort_dir)
                           .limit(limit))

        if not players:
            await interaction.followup.send(f"No players found with {mode_prefix.lower()}{streak_type} streaks.")
            return

        # Create embed
        embed = discord.Embed(
            title=title,
            color=embed_color
        )

        # Add fields for each player (limit to prevent embed size issues)
        displayed_count = 0
        for i, player in enumerate(players):
            if displayed_count >= 10:  # Discord embed field limit
                break

            player_id = player.get("id")
            if not player_id:
                continue

            player_name = player.get("name", "Unknown")

            # Try to get Discord member name
            try:
                member = await interaction.guild.fetch_member(int(player_id))
                if member:
                    player_name = member.display_name
            except:
                pass  # Use name from database if member not found

            # Format streak value safely
            try:
                if streak_type == "win":
                    streak_value = player.get(field_name, 0)
                    if streak_value is None:
                        streak_value = 0
                    streak_display = f"🏆 {streak_value} Wins"
                elif streak_type == "loss":
                    streak_value = player.get(field_name, 0)
                    if streak_value is None:
                        streak_value = 0
                    streak_display = f"📉 {abs(streak_value)} Losses"
                else:  # current
                    streak_value = player.get(field_name, 0)
                    if streak_value is None:
                        streak_value = 0
                    if streak_value > 0:
                        icon = "🔥" if streak_value >= 3 else "↗️"
                        streak_display = f"{icon} {streak_value} Win Streak"
                    elif streak_value < 0:
                        icon = "❄️" if streak_value <= -3 else "↘️"
                        streak_display = f"{icon} {abs(streak_value)} Loss Streak"
                    else:
                        streak_display = "No Streak"

                # Get player's MMR based on mode
                if is_global:
                    mmr = player.get("global_mmr", 300)
                    if mmr is None:
                        mmr = 300
                    mmr_label = "Global MMR"
                else:
                    mmr = player.get("mmr", 0)
                    if mmr is None:
                        mmr = 0
                    # Determine rank based on MMR
                    if mmr >= 1600:
                        rank = "Rank A"
                    elif mmr >= 1100:
                        rank = "Rank B"
                    else:
                        rank = "Rank C"
                    mmr_label = f"MMR ({rank})"

                # Add player field
                embed.add_field(
                    name=f"#{i + 1}: {player_name}",
                    value=f"{streak_display}\n{mmr_label}: {mmr}",
                    inline=False
                )
                displayed_count += 1

            except Exception as field_error:
                print(f"Error processing player {player_name}: {field_error}")
                continue

        # Add footer
        embed.set_footer(text=f"Streak data as of {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")

        # If we have more players than displayed, add a note
        if len(players) > displayed_count:
            embed.add_field(
                name="Note",
                value=f"Showing top {displayed_count} of {len(players)} players with streaks.",
                inline=False
            )

        # Send response
        await interaction.followup.send(embed=embed)

    except Exception as e:
        print(f"Error in topstreaks command: {e}")
        import traceback
        traceback.print_exc()
        await interaction.followup.send(f"Error retrieving streak data: {str(e)}")

@bot.tree.command(name="resetstreak", description="Reset a player's streak (Admin only)")
@app_commands.describe(
            member="The member whose streak to reset",
            reset_type="Type of streak to reset"
        )
@app_commands.choices(reset_type=[
            app_commands.Choice(name="Current Streak Only", value="current"),
            app_commands.Choice(name="All Streak Records", value="all")
        ])
async def resetstreak_slash(interaction: discord.Interaction, member: discord.Member, reset_type: str):
            # Check if command is used in an allowed channel
            if not is_command_channel(interaction.channel):
                await interaction.response.send_message(
                    f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
                    ephemeral=True
                )
                return

            # Check if user has admin permissions
            if not has_admin_or_mod_permissions(interaction.user, interaction.guild):
                await interaction.response.send_message(
                    "You need administrator permissions or the 6mod role to use this command.",
                    ephemeral=True)
                return

            player_id = str(member.id)
            player_data = system_coordinator.match_system.players.find_one({"id": player_id})

            if not player_data:
                await interaction.response.send_message(
                    f"{member.mention} hasn't played any matches. No streak information to reset.",
                    ephemeral=True
                )
                return

            # Get current streak values
            current_streak = player_data.get("current_streak", 0)

            # Create update document based on reset type
            if reset_type == "current":
                update_doc = {
                    "$set": {
                        "current_streak": 0
                    }
                }
                success_message = f"Reset current streak for {member.mention}. Previous streak: "
                if current_streak > 0:
                    success_message += f"**{current_streak}** Win Streak"
                elif current_streak < 0:
                    success_message += f"**{abs(current_streak)}** Loss Streak"
                else:
                    success_message += "No streak"
            else:  # "all"
                update_doc = {
                    "$set": {
                        "current_streak": 0,
                        "longest_win_streak": 0,
                        "longest_loss_streak": 0
                    }
                }
                success_message = f"Reset all streak records for {member.mention}."

            # Update player record
            result = system_coordinator.match_system.players.update_one(
                {"id": player_id},
                update_doc
            )

            if result.modified_count > 0:
                await interaction.response.send_message(success_message)
            else:
                await interaction.response.send_message(
                    f"Failed to reset streak for {member.mention}. No changes were made.")


@bot.tree.command(name="debugmmr", description="Debug MMR storage issue (Admin only)")
async def debug_mmr_issue(interaction: discord.Interaction, match_id: str):
    if not has_admin_or_mod_permissions(interaction.user, interaction.guild):
        await interaction.response.send_message("Admin only", ephemeral=True)
        return

    await interaction.response.defer()

    # Check if match exists in database
    match = system_coordinator.match_system.matches.find_one({"match_id": match_id})

    if not match:
        await interaction.followup.send(f"❌ Match `{match_id}` not found in database!")
        return

    # Get match details
    status = match.get("status", "unknown")
    is_global = match.get("is_global", False)
    team1 = match.get("team1", [])
    team2 = match.get("team2", [])
    mmr_changes = match.get("mmr_changes", [])

    # Build debug report
    debug_text = f"**Match Debug Report: `{match_id}`**\n\n"
    debug_text += f"📊 **Basic Info:**\n"
    debug_text += f"• Status: {status}\n"
    debug_text += f"• Is Global: {is_global}\n"
    debug_text += f"• Team 1 size: {len(team1)}\n"
    debug_text += f"• Team 2 size: {len(team2)}\n"
    debug_text += f"• MMR changes recorded: {len(mmr_changes)}\n\n"

    # Show team compositions
    debug_text += f"👥 **Team 1:**\n"
    for i, player in enumerate(team1):
        player_id = player.get("id", "unknown")
        player_name = player.get("name", "unknown")
        is_dummy = player_id.startswith('9000')
        debug_text += f"  {i + 1}. {player_name} (ID: {player_id}) {'[DUMMY]' if is_dummy else '[REAL]'}\n"

    debug_text += f"\n👥 **Team 2:**\n"
    for i, player in enumerate(team2):
        player_id = player.get("id", "unknown")
        player_name = player.get("name", "unknown")
        is_dummy = player_id.startswith('9000')
        debug_text += f"  {i + 1}. {player_name} (ID: {player_id}) {'[DUMMY]' if is_dummy else '[REAL]'}\n"

    # Show MMR changes in detail
    debug_text += f"\n💰 **MMR Changes ({len(mmr_changes)} total):**\n"
    if mmr_changes:
        for i, change in enumerate(mmr_changes):
            player_id = change.get("player_id", "unknown")
            mmr_change = change.get("mmr_change", 0)
            old_mmr = change.get("old_mmr", 0)
            new_mmr = change.get("new_mmr", 0)
            streak = change.get("streak", 0)
            is_win = change.get("is_win", False)
            change_is_global = change.get("is_global", False)

            # Find player name
            player_name = "Unknown"
            for team in [team1, team2]:
                for p in team:
                    if p.get("id") == player_id:
                        player_name = p.get("name", "Unknown")
                        break

            result_icon = "🏆" if is_win else "😔"
            debug_text += f"  {i + 1}. {result_icon} {player_name}: {old_mmr} → {new_mmr} ({mmr_change:+d})\n"
            debug_text += f"     Streak: {streak}, Global: {change_is_global}\n"
    else:
        debug_text += "  ❌ No MMR changes found!\n"

    # Check if YOUR player ID is in the match
    your_id = str(interaction.user.id)
    your_in_match = False
    your_team = None

    for player in team1 + team2:
        if player.get("id") == your_id:
            your_in_match = True
            your_team = "Team 1" if player in team1 else "Team 2"
            break

    debug_text += f"\n🫵 **Your Participation:**\n"
    debug_text += f"• Your ID: {your_id}\n"
    debug_text += f"• You in match: {your_in_match}\n"
    if your_in_match:
        debug_text += f"• Your team: {your_team}\n"

        # Check if you have MMR change recorded
        your_mmr_change = None
        for change in mmr_changes:
            if change.get("player_id") == your_id:
                your_mmr_change = change
                break

        if your_mmr_change:
            debug_text += f"• Your MMR change: {your_mmr_change.get('mmr_change', 0):+d}\n"
            debug_text += f"• Your new streak: {your_mmr_change.get('streak', 0)}\n"
        else:
            debug_text += f"• ❌ No MMR change recorded for you!\n"

    # Split message if too long
    if len(debug_text) > 2000:
        # Send in chunks
        chunks = []
        current_chunk = ""
        for line in debug_text.split('\n'):
            if len(current_chunk + line + '\n') > 1900:
                chunks.append(current_chunk)
                current_chunk = line + '\n'
            else:
                current_chunk += line + '\n'
        if current_chunk:
            chunks.append(current_chunk)

        for i, chunk in enumerate(chunks):
            if i == 0:
                await interaction.followup.send(chunk)
            else:
                await interaction.followup.send(chunk)
    else:
        await interaction.followup.send(debug_text)


# Also add this command to manually test MMR calculation
@bot.tree.command(name="testmmr", description="Test MMR calculation manually (Admin only)")
async def test_mmr_calculation(interaction: discord.Interaction, match_id: str):
    if not has_admin_or_mod_permissions(interaction.user, interaction.guild):
        await interaction.response.send_message("Admin only", ephemeral=True)
        return

    await interaction.response.defer()

    # Find the match
    match = system_coordinator.match_system.matches.find_one({"match_id": match_id})
    if not match:
        await interaction.followup.send(f"Match `{match_id}` not found!")
        return

    # Check if match is completed
    if match.get("status") != "completed":
        await interaction.followup.send(f"Match `{match_id}` is not completed yet (status: {match.get('status')})")
        return

    # Get your player data
    your_id = str(interaction.user.id)
    your_player_data = system_coordinator.match_system.players.find_one({"id": your_id})

    result_text = f"**MMR Test for Match `{match_id}`**\n\n"
    result_text += f"Your Player ID: {your_id}\n"

    if your_player_data:
        result_text += f"Your current ranked MMR: {your_player_data.get('mmr', 'Not found')}\n"
        result_text += f"Your current global MMR: {your_player_data.get('global_mmr', 'Not found')}\n"
        result_text += f"Your ranked matches: {your_player_data.get('matches', 0)}\n"
        result_text += f"Your global matches: {your_player_data.get('global_matches', 0)}\n"
        result_text += f"Your ranked streak: {your_player_data.get('current_streak', 0)}\n"
        result_text += f"Your global streak: {your_player_data.get('global_current_streak', 0)}\n"
    else:
        result_text += "❌ No player data found for you in the database!\n"
        result_text += "This means you haven't played any matches yet or there's a database issue.\n"

    await interaction.followup.send(result_text)


@bot.tree.command(name="streakstats", description="Show server-wide streak statistics (Admin only)")
async def streakstats_slash(interaction: discord.Interaction):
    # Check if command is used in an allowed channel
    if not is_command_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
            ephemeral=True
        )
        return

    # Check if user has admin permissions
    if not has_admin_or_mod_permissions(interaction.user, interaction.guild):
        await interaction.response.send_message(
            "You need administrator permissions or the 6mod role to use this command.",
            ephemeral=True)
        return

    # Start deferred response since this might take time
    await interaction.response.defer()

    try:
        # Get overall stats with aggregation pipeline
        pipeline = [
            {
                "$group": {
                    "_id": None,
                    "total_players": {"$sum": 1},
                    "players_with_win_streaks": {"$sum": {"$cond": [{"$gt": ["$current_streak", 0]}, 1, 0]}},
                    "players_with_loss_streaks": {"$sum": {"$cond": [{"$lt": ["$current_streak", 0]}, 1, 0]}},
                    "avg_win_streak": {
                        "$avg": {"$cond": [{"$gt": ["$current_streak", 0]}, "$current_streak", None]}},
                    "avg_loss_streak": {"$avg": {
                        "$cond": [{"$lt": ["$current_streak", 0]}, {"$abs": "$current_streak"}, None]}},
                    "max_win_streak": {"$max": "$longest_win_streak"},
                    "max_loss_streak": {"$min": "$longest_loss_streak"},
                    "avg_longest_win": {"$avg": "$longest_win_streak"},
                    "avg_longest_loss": {"$avg": {"$abs": "$longest_loss_streak"}}
                }
            }
        ]

        stats = list(system_coordinator.match_system.players.aggregate(pipeline))

        if not stats or not stats[0]:
            await interaction.followup.send("No streak statistics available - no players found.")
            return

        stats = stats[0]  # Get the first (and only) result

        # Calculate percentages safely
        total_players = stats.get("total_players", 0)
        if total_players == 0:
            await interaction.followup.send("No players found to generate statistics.")
            return

        players_with_win_streaks = stats.get("players_with_win_streaks", 0) or 0
        players_with_loss_streaks = stats.get("players_with_loss_streaks", 0) or 0

        win_streak_percent = (players_with_win_streaks / total_players * 100) if total_players > 0 else 0
        loss_streak_percent = (players_with_loss_streaks / total_players * 100) if total_players > 0 else 0

        # Format values safely
        avg_win_streak = stats.get("avg_win_streak") or 0
        avg_loss_streak = stats.get("avg_loss_streak") or 0
        max_win_streak = stats.get("max_win_streak") or 0
        max_loss_streak = stats.get("max_loss_streak") or 0
        avg_longest_win = stats.get("avg_longest_win") or 0
        avg_longest_loss = stats.get("avg_longest_loss") or 0

        # Round safely
        avg_win_streak = round(float(avg_win_streak), 1) if avg_win_streak else 0
        avg_loss_streak = round(float(avg_loss_streak), 1) if avg_loss_streak else 0
        avg_longest_win = round(float(avg_longest_win), 1) if avg_longest_win else 0
        avg_longest_loss = round(float(avg_longest_loss), 1) if avg_longest_loss else 0
        max_loss_streak = abs(int(max_loss_streak)) if max_loss_streak else 0

        # Create embed
        embed = discord.Embed(
            title="Server-wide Streak Statistics",
            description=f"Statistics based on {total_players} players",
            color=0x7289da
        )

        # Current streaks summary
        no_streak_count = total_players - players_with_win_streaks - players_with_loss_streaks
        embed.add_field(
            name="Current Streaks",
            value=(
                f"**Win Streaks**: {players_with_win_streaks} players ({win_streak_percent:.1f}%)\n"
                f"**Loss Streaks**: {players_with_loss_streaks} players ({loss_streak_percent:.1f}%)\n"
                f"**No Streak**: {no_streak_count} players"
            ),
            inline=False
        )

        # Streak length stats
        embed.add_field(
            name="Win Streak Stats",
            value=(
                f"**Average Win Streak**: {avg_win_streak} wins\n"
                f"**Longest Win Streak**: {max_win_streak} wins\n"
                f"**Avg Longest Win Streak**: {avg_longest_win} wins"
            ),
            inline=True
        )

        embed.add_field(
            name="Loss Streak Stats",
            value=(
                f"**Average Loss Streak**: {avg_loss_streak} losses\n"
                f"**Longest Loss Streak**: {max_loss_streak} losses\n"
                f"**Avg Longest Loss Streak**: {avg_longest_loss} losses"
            ),
            inline=True
        )

        # Get Rank A, B, C breakdowns
        try:
            rank_a_stats = await get_rank_stats_safe(system_coordinator.match_system.players, 1600)
            rank_b_stats = await get_rank_stats_safe(system_coordinator.match_system.players, 1100, 1599)
            rank_c_stats = await get_rank_stats_safe(system_coordinator.match_system.players, 0, 1099)

            embed.add_field(
                name="Rank A Streaks",
                value=(
                    f"**Win Streak %**: {rank_a_stats.get('win_streak_percent', 0):.1f}%\n"
                    f"**Loss Streak %**: {rank_a_stats.get('loss_streak_percent', 0):.1f}%\n"
                    f"**Avg Win Streak**: {rank_a_stats.get('avg_win_streak', 0):.1f}\n"
                    f"**Avg Loss Streak**: {rank_a_stats.get('avg_loss_streak', 0):.1f}"
                ),
                inline=True
            )

            embed.add_field(
                name="Rank B Streaks",
                value=(
                    f"**Win Streak %**: {rank_b_stats.get('win_streak_percent', 0):.1f}%\n"
                    f"**Loss Streak %**: {rank_b_stats.get('loss_streak_percent', 0):.1f}%\n"
                    f"**Avg Win Streak**: {rank_b_stats.get('avg_win_streak', 0):.1f}\n"
                    f"**Avg Loss Streak**: {rank_b_stats.get('avg_loss_streak', 0):.1f}"
                ),
                inline=True
            )

            embed.add_field(
                name="Rank C Streaks",
                value=(
                    f"**Win Streak %**: {rank_c_stats.get('win_streak_percent', 0):.1f}%\n"
                    f"**Loss Streak %**: {rank_c_stats.get('loss_streak_percent', 0):.1f}%\n"
                    f"**Avg Win Streak**: {rank_c_stats.get('avg_win_streak', 0):.1f}\n"
                    f"**Avg Loss Streak**: {rank_c_stats.get('avg_loss_streak', 0):.1f}"
                ),
                inline=True
            )
        except Exception as rank_error:
            print(f"Error getting rank stats: {rank_error}")
            embed.add_field(
                name="Rank Breakdown",
                value="Error calculating rank-specific statistics",
                inline=False
            )

        # Add footer
        embed.set_footer(text=f"Data as of {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")

        # Send response
        await interaction.followup.send(embed=embed)

    except Exception as e:
        print(f"Error in streakstats command: {e}")
        import traceback
        traceback.print_exc()
        await interaction.followup.send(f"Error retrieving streak statistics: {str(e)}")

# Helper function for streak stats by rank (with safe None handling)
async def get_rank_stats_safe(players_collection, min_mmr, max_mmr=None):
    """Get streak statistics for a specific MMR range with safe None handling"""
    try:
        # Build query based on MMR range
        if max_mmr:
            query = {"mmr": {"$gte": min_mmr, "$lt": max_mmr}}
        else:
            query = {"mmr": {"$gte": min_mmr}}

        # Run aggregation pipeline
        pipeline = [
            {"$match": query},
            {
                "$group": {
                    "_id": None,
                    "total_players": {"$sum": 1},
                    "players_with_win_streaks": {"$sum": {"$cond": [{"$gt": ["$current_streak", 0]}, 1, 0]}},
                    "players_with_loss_streaks": {"$sum": {"$cond": [{"$lt": ["$current_streak", 0]}, 1, 0]}},
                    "avg_win_streak": {
                        "$avg": {"$cond": [{"$gt": ["$current_streak", 0]}, "$current_streak", None]}},
                    "avg_loss_streak": {
                        "$avg": {"$cond": [{"$lt": ["$current_streak", 0]}, {"$abs": "$current_streak"}, None]}}
                }
            }
        ]

        stats = list(players_collection.aggregate(pipeline))

        if not stats or not stats[0]:
            return {
                "total_players": 0,
                "win_streak_percent": 0,
                "loss_streak_percent": 0,
                "avg_win_streak": 0,
                "avg_loss_streak": 0
            }

        stats = stats[0]  # Get the first (and only) result

        # Calculate percentages safely
        total_players = stats.get("total_players", 0) or 0
        players_with_win_streaks = stats.get("players_with_win_streaks", 0) or 0
        players_with_loss_streaks = stats.get("players_with_loss_streaks", 0) or 0

        win_streak_percent = (players_with_win_streaks / total_players * 100) if total_players > 0 else 0
        loss_streak_percent = (players_with_loss_streaks / total_players * 100) if total_players > 0 else 0

        # Safe value extraction
        avg_win_streak = stats.get("avg_win_streak") or 0
        avg_loss_streak = stats.get("avg_loss_streak") or 0

        return {
            "total_players": total_players,
            "win_streak_percent": win_streak_percent,
            "loss_streak_percent": loss_streak_percent,
            "avg_win_streak": float(avg_win_streak) if avg_win_streak else 0,
            "avg_loss_streak": float(avg_loss_streak) if avg_loss_streak else 0
        }
    except Exception as e:
        print(f"Error in get_rank_stats_safe: {e}")
        return {
            "total_players": 0,
            "win_streak_percent": 0,
            "loss_streak_percent": 0,
            "avg_win_streak": 0,
            "avg_loss_streak": 0
        }

# Run the bot with the keepalive server
if __name__ == "__main__":
    # Start the keepalive server first
    start_keepalive_server()

    # Then run the bot
    bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)