import discord
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
import random
from rate_limiter import DiscordRateLimiter
from bulk_role_manager import BulkRoleManager
from render_config import (
    configure_for_render,
    render_startup_sequence,
    render_safe_sync,
    RenderErrorHandler,
    cloud_safe_defer,
    cloud_safe_followup,
    is_render_platform,
    is_cloud_platform,
    get_platform_info
)


# Rate limiting configuration
EMERGENCY_MODE = False
DISCORD_RATE_LIMIT_ENABLED = True
MAX_CONCURRENT_ROLE_OPERATIONS = 1  # Reduced from 3 to 1 for maximum safety
DELAY_BETWEEN_ROLE_OPERATIONS = 2.0  # Increased from 0.5 to 2.0 seconds
GUILD_SYNC_DELAY = 15.0  # Increased from 1.0 to 15.0 seconds
DM_RETRY_DELAY = 5.0  # Increased from 2.0 to 5.0 seconds
MEMBER_FETCH_DELAY = 1.0  # Increased from 0.1 to 1.0 second

print("🚀 ENHANCED rate limiting system loaded with ULTRA-CONSERVATIVE settings")
print("📊 Configuration:")
print(f"  • Rate limiting enabled: {DISCORD_RATE_LIMIT_ENABLED}")
print(f"  • Max concurrent role ops: {MAX_CONCURRENT_ROLE_OPERATIONS}")
print(f"  • Role operation delay: {DELAY_BETWEEN_ROLE_OPERATIONS}s")
print(f"  • Guild sync delay: {GUILD_SYNC_DELAY}s")
print(f"  • DM retry delay: {DM_RETRY_DELAY}s")
print(f"  • Member fetch delay: {MEMBER_FETCH_DELAY}s")


# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')

RESET_IN_PROGRESS = False
RESET_START_TIME = None

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

        # ADDED: Additional attributes that the role system might expect
        self.bot = interaction.client if hasattr(interaction, 'client') else None
        self.user = interaction.user  # Alias for author

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


async def safe_fetch_member(guild, user_id, fallback_name="Unknown"):
    """Safely fetch a Discord member with emergency mode protection"""
    global EMERGENCY_MODE

    # Skip all fetches during emergency mode
    if EMERGENCY_MODE:
        print(f"🚨 Emergency mode: Skipping member fetch for {user_id}")
        return None

    try:
        # CRITICAL: Skip dummy players immediately - don't try to fetch them
        if str(user_id).startswith('9000'):
            print(f"ℹ️ Skipping dummy player fetch: {user_id}")
            return None

        # Skip invalid IDs
        if not str(user_id).isdigit():
            print(f"⚠️ Invalid user ID format: {user_id}")
            return None

        user_id = int(user_id)  # Convert to int after validation

        if rate_limiter:
            # Add random delay before fetching
            await asyncio.sleep(random.uniform(2.0, 4.0))  # Increased delay
            return await rate_limiter.fetch_member_with_limit(guild, user_id, max_retries=1)
        else:
            # Manual rate limiting fallback with much longer delays
            await asyncio.sleep(random.uniform(3.0, 6.0))  # Much longer delay
            return await guild.fetch_member(user_id)

    except discord.HTTPException as e:
        if e.status == 429:
            print(f"⚠️ Rate limited fetching member {user_id} - backing off")
            # Don't retry immediately on rate limit
            return None
        elif e.status == 404:
            print(f"ℹ️ Member {user_id} not found (404)")
            return None
        else:
            print(f"❌ HTTP error fetching member {user_id}: {e}")
            return None
    except ValueError:
        print(f"❌ Invalid user ID: {user_id}")
        return None
    except Exception as e:
        print(f"❌ Unexpected error fetching member {user_id}: {e}")
        return None


async def emergency_rate_limit_recovery():
    """Emergency function to handle severe rate limiting"""
    print("🆘 EMERGENCY: Severe rate limiting detected!")
    print("🚨 Implementing emergency recovery measures...")

    # Disable all background tasks temporarily
    global EMERGENCY_MODE
    EMERGENCY_MODE = True

    # Long cooling-off period
    print("❄️ Entering 5-minute cooling-off period...")
    await asyncio.sleep(300)  # 5 minutes

    print("✅ Emergency recovery completed - resuming limited operations")
    EMERGENCY_MODE = False


def check_rate_limit_health():
    """Check if rate limiter is in healthy state"""
    if rate_limiter:
        status = rate_limiter.get_rate_limit_status()

        total_failures = sum(
            status.get(op_type, {}).get('failure_count', 0)
            for op_type in status.keys()
        )

        if total_failures > 10:
            print(f"⚠️ Rate limiter health warning: {total_failures} total failures")
            return False

        return True

    return False


async def startup_health_check():
    """Perform health checks after bot startup"""
    await asyncio.sleep(30)  # Wait 30 seconds after startup

    try:
        # Check rate limiter health
        if not check_rate_limit_health():
            print("⚠️ Rate limiter health check failed")
            await emergency_rate_limit_recovery()

        # Check if we can make basic API calls
        for guild in bot.guilds:
            try:
                await guild.fetch_member(bot.user.id)  # Try to fetch self
                print(f"✅ Health check passed for guild: {guild.name}")
                break  # Only test one guild
            except discord.HTTPException as e:
                if e.status == 429:
                    print(f"⚠️ Rate limited during health check - initiating recovery")
                    await emergency_rate_limit_recovery()
                    break
                else:
                    print(f"⚠️ API error during health check: {e}")
            except Exception as e:
                print(f"⚠️ Unexpected error during health check: {e}")

        print("✅ Startup health check completed")

    except Exception as e:
        print(f"❌ Health check failed: {e}")

async def safe_role_operation(member, operation, *roles, reason=None):
    """Safely perform role operations with rate limiting"""
    try:
        if rate_limiter:
            if operation == "add":
                await rate_limiter.add_role_with_limit(member, *roles, reason=reason)
            elif operation == "remove":
                await rate_limiter.remove_role_with_limit(member, *roles, reason=reason)
        else:
            # Manual rate limiting fallback
            await asyncio.sleep(0.5)
            if operation == "add":
                await member.add_roles(*roles, reason=reason)
            elif operation == "remove":
                await member.remove_roles(*roles, reason=reason)
        return True
    except discord.HTTPException as e:
        if e.status == 429:
            retry_after = getattr(e, 'retry_after', 2)
            print(f"Rate limited on role operation, waiting {retry_after}s")
            await asyncio.sleep(retry_after)
            try:
                if operation == "add":
                    await member.add_roles(*roles, reason=f"{reason} - retry")
                elif operation == "remove":
                    await member.remove_roles(*roles, reason=f"{reason} - retry")
                return True
            except Exception as retry_error:
                print(f"Role operation retry failed: {retry_error}")
                return False
        else:
            print(f"Role operation failed: {e}")
            return False
    except Exception as e:
        print(f"Unexpected error in role operation: {e}")
        return False


async def safe_send_followup(interaction, content=None, embed=None, ephemeral=False, max_retries=2):
    """Safely send followup messages with enhanced rate limiting protection"""
    for attempt in range(max_retries):
        try:
            if rate_limiter:
                # Add delay before sending
                await asyncio.sleep(random.uniform(0.5, 1.0))
                return await rate_limiter.send_message_with_limit(
                    interaction.followup, content=content, embed=embed, ephemeral=ephemeral
                )
            else:
                # Manual delay fallback with longer delays
                if attempt > 0:
                    await asyncio.sleep(random.uniform(2.0, 5.0))  # Longer delay on retry
                return await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
        except discord.HTTPException as e:
            if e.status == 429 and attempt < max_retries - 1:
                retry_after = max(getattr(e, 'retry_after', 5), 5)  # Minimum 5s wait
                jitter = random.uniform(0, retry_after * 0.3)  # Add jitter
                total_wait = retry_after + jitter
                print(f"⚠️ Followup rate limited, waiting {total_wait:.1f}s (attempt {attempt + 1})")
                await asyncio.sleep(total_wait)
                continue
            else:
                raise
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"⚠️ Followup error, retrying: {e}")
                await asyncio.sleep(random.uniform(2.0, 4.0))
                continue
            else:
                raise

    # If we get here, all retries failed
    raise Exception(f"Failed to send followup after {max_retries} attempts")


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

# Initialize rate limiter only
rate_limiter = DiscordRateLimiter()


@bot.event
async def on_ready():
    print(f"{bot.user.name} is now online with ID: {bot.user.id}")
    print(f"Connected to {len(bot.guilds)} guilds")

    # Print platform information
    platform_info = get_platform_info()
    print(f"Platform Info: {platform_info}")

    try:
        # Configure for cloud platforms (Render, Heroku, etc.)
        is_cloud = configure_for_render(bot, rate_limiter)

        if is_cloud:
            print("🌐 Cloud platform configuration applied")
            await render_startup_sequence(bot)
        else:
            print("💻 Local development mode")

        # ENHANCED rate limiter initialization with cloud awareness
        rate_limiter.bot = bot

        try:
            rate_limiter.start_bulk_processor()
            print("✅ Rate limiting system initialized successfully")
        except Exception as rl_error:
            print(f"⚠️ Rate limiter initialization warning: {rl_error}")
            print("Bot will continue with reduced rate limiting capability")

        # Connect rate limiter to systems
        try:
            system_coordinator.match_system.set_rate_limiter(rate_limiter)
            print("✅ Rate limiter connected to match system")
        except Exception as ms_error:
            print(f"⚠️ Error connecting rate limiter to match system: {ms_error}")

        try:
            system_coordinator.set_bot(bot)
            system_coordinator.set_rate_limiter(rate_limiter)
            print("✅ System coordinator initialized")
        except Exception as sc_error:
            print(f"⚠️ Error initializing system coordinator: {sc_error}")

        global bulk_role_manager
        bulk_role_manager = BulkRoleManager(db, bot, rate_limiter)

        # Connect it to match system
        system_coordinator.match_system.set_bulk_role_manager(bulk_role_manager)

        # Start the daily 3am task
        bulk_role_manager.start_daily_role_update_task()
        print("✅ Bulk role update system initialized - will process at 3:00 AM daily")

        # Start background tasks with error handling
        try:
            bot.loop.create_task(system_coordinator.check_for_ready_matches())
            print(f"✅ Background tasks started - {datetime.datetime.now(datetime.UTC)}")
        except Exception as task_error:
            print(f"⚠️ Background task warning: {task_error}")

        # Command synchronization - use cloud-safe version if on cloud platform
        if is_cloud:
            print("🌐 Using cloud-safe command synchronization...")
            success = await render_safe_sync(bot)
            if not success:
                print("⚠️ Cloud-safe sync had issues, but bot will continue to operate")
        else:
            print("💻 Using standard command synchronization...")
            print("Syncing global commands...")
            try:
                await asyncio.sleep(5.0)  # 5 second delay
                await bot.tree.sync()
                print("✅ Global commands synced")
            except Exception as sync_error:
                print(f"⚠️ Error syncing global commands: {sync_error}")

            print("Syncing guild-specific commands...")
            for i, guild in enumerate(bot.guilds):
                try:
                    if i > 0:  # Don't delay before the first guild
                        delay = random.uniform(15.0, 25.0)  # 15-25 second random delay
                        print(f"⏳ Waiting {delay:.1f}s before syncing to next guild...")
                        await asyncio.sleep(delay)

                    print(f"🔄 Syncing commands to guild: {guild.name} (ID: {guild.id})")
                    await bot.tree.sync(guild=guild)
                    print(f"✅ Synced commands to guild: {guild.name}")

                except discord.HTTPException as guild_error:
                    if guild_error.status == 429:
                        retry_after = max(getattr(guild_error, 'retry_after', 30), 30)
                        print(f"⚠️ Rate limited syncing to {guild.name}, waiting {retry_after}s")
                        await asyncio.sleep(retry_after)
                        try:
                            await bot.tree.sync(guild=guild)
                            print(f"✅ Retry successful for guild: {guild.name}")
                        except Exception as retry_error:
                            print(f"❌ Retry failed for guild {guild.name}: {retry_error}")
                    else:
                        print(f"❌ Error syncing to guild {guild.name}: {guild_error}")
                except Exception as guild_error:
                    print(f"❌ Unexpected error syncing to guild {guild.name}: {guild_error}")

        commands = bot.tree.get_commands()
        print(f"✅ Registered {len(commands)} global application commands")
        for cmd in commands:
            print(f"  - /{cmd.name}")

        print("✅ Command synchronization complete.")

        if not is_cloud:
            bot.loop.create_task(startup_health_check())

    except Exception as e:
        print(f"❌ Critical error during bot initialization: {e}")
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
    global RESET_IN_PROGRESS, RESET_START_TIME
    if RESET_IN_PROGRESS:
        duration = ""
        if RESET_START_TIME:
            elapsed = datetime.datetime.now() - RESET_START_TIME
            duration = f" (Running for {elapsed.seconds // 60}m {elapsed.seconds % 60}s)"

        embed = discord.Embed(
            title="⛔ Queuing Disabled",
            description=f"A leaderboard reset is currently in progress{duration}",
            color=0xff9900
        )
        embed.add_field(
            name="Please Wait",
            value="• Reset operations are running\n• Queuing will be re-enabled automatically\n• Check back in a few minutes",
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

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
    if RESET_IN_PROGRESS:
        duration = ""
        if RESET_START_TIME:
            elapsed = datetime.datetime.now() - RESET_START_TIME
            duration = f" (Running for {elapsed.seconds // 60}m {elapsed.seconds % 60}s)"

        embed = discord.Embed(
            title="⛔ Queuing Disabled",
            description=f"A leaderboard reset is currently in progress{duration}",
            color=0xff9900
        )
        embed.add_field(
            name="Please Wait",
            value="• Reset operations are running\n• Queuing will be re-enabled automatically\n• Check back in a few minutes",
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

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
        # Enhanced error handling for rate limiting issues
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
            elif isinstance(error.original, discord.HTTPException):
                if error.original.status == 429:
                    print(f"Command rate limited: {error.original}")
                    if not interaction.response.is_done():
                        try:
                            # ENHANCED: Add delay before responding to rate limit error
                            await asyncio.sleep(random.uniform(1.0, 3.0))
                            await interaction.response.send_message(
                                "⚠️ The bot is currently rate limited. Please wait a moment and try again.",
                                ephemeral=True
                            )
                        except:
                            pass  # If we can't even send this, just log it
                    return
                else:
                    print(f"HTTP error in command: {error.original}")
                    if not interaction.response.is_done():
                        await interaction.response.send_message(
                            "A network error occurred. Please try again in a moment.",
                            ephemeral=True
                        )
            else:
                print(f"Command invoke error: {error.original}")
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "An error occurred. Please try again.",
                        ephemeral=True
                    )
        else:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "An unexpected error occurred. Please try again.",
                    ephemeral=True
                )
    except Exception as e:
        print(f"Error in error handler: {e}")
        # Last resort - try to log the issue
        try:
            print(f"Original error that caused handler failure: {error}")
            print(f"Handler error: {e}")
        except:
            pass


@bot.tree.command(name="status", description="Shows the current queue status")
async def status_slash(interaction: discord.Interaction):
    if RESET_IN_PROGRESS:
        duration = ""
        if RESET_START_TIME:
            elapsed = datetime.datetime.now() - RESET_START_TIME
            duration = f" (Running for {elapsed.seconds // 60}m {elapsed.seconds % 60}s)"

        embed = discord.Embed(
            title="⛔ Queue Status Unavailable",
            description=f"A leaderboard reset is currently in progress{duration}",
            color=0xff9900
        )
        embed.add_field(
            name="Please Wait",
            value="• Reset operations are running\n• Queue status will be available after reset\n• All queues are currently disabled",
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

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
async def report_slash_cloud_enhanced(interaction: discord.Interaction, match_id: str, result: str):
    if RESET_IN_PROGRESS:
        duration = ""
        if RESET_START_TIME:
            elapsed = datetime.datetime.now() - RESET_START_TIME
            duration = f" (Running for {elapsed.seconds // 60}m {elapsed.seconds % 60}s)"

        embed = discord.Embed(
            title="⛔ Match Reporting Disabled",
            description=f"A leaderboard reset is currently in progress{duration}",
            color=0xff9900
        )
        embed.add_field(
            name="Please Wait",
            value="• Reset operations are running\n• Match reporting will be re-enabled automatically\n• Your match results will be preserved",
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Create context for backward compatibility
    ctx = SimpleContext(interaction)

    # Normalize the match ID
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

    # Check if the match was created in this specific channel
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

    # CLOUD-SAFE defer with enhanced error handling
    try:
        defer_success = await cloud_safe_defer(interaction)
        if not defer_success:
            # If defer fails, try to send error message
            await RenderErrorHandler.handle_rate_limit(interaction, "match report")
            return
    except Exception as defer_error:
        print(f"Critical defer error: {defer_error}")
        await RenderErrorHandler.handle_general_error(interaction, defer_error, "match report")
        return

    # Add cloud platform delay before processing
    if is_cloud_platform():
        await asyncio.sleep(random.uniform(1.0, 3.0))

    try:
        # CRITICAL: Process match result with error handling
        print(f"🔄 Processing match report for {match_id} by {interaction.user.display_name}")

        # Get match result with enhanced error handling
        match_result, error = await system_coordinator.match_system.report_match_by_id(match_id, reporter_id, result,
                                                                                       ctx)

        if error:
            print(f"❌ Match report error: {error}")
            await cloud_safe_followup(interaction, f"Error: {error}")
            return

        if not match_result:
            print(f"❌ Match report failed: No result returned")
            await cloud_safe_followup(interaction, "Failed to process match report.")
            return

        print(f"✅ Match report processed successfully for {match_id}")

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

        # Extract MMR changes and streaks from match result properly
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

        # Initialize arrays for MMR changes and streaks
        winning_team_mmr_changes = []
        losing_team_mmr_changes = []
        winning_team_streaks = []
        losing_team_streaks = []

        # Extract MMR changes for winning team with proper global/ranked filtering
        for player in winning_team:
            player_id = player.get("id")

            if player_id and player_id in mmr_changes_by_player:
                change_data = mmr_changes_by_player[player_id]

                # Only show MMR changes that match the current match type
                change_is_global = change_data.get("is_global", False)
                if change_is_global == is_global:
                    mmr_change = change_data["mmr_change"]
                    streak = change_data["streak"]

                    winning_team_mmr_changes.append(f"+{mmr_change} MMR")

                    # Format streak display with emojis
                    if streak >= 3:
                        winning_team_streaks.append(f"🔥 {streak}W")
                    elif streak == 2:
                        winning_team_streaks.append(f"↗️ {streak}W")
                    elif streak == 1:
                        winning_team_streaks.append(f"↗️ {streak}W")
                    else:
                        winning_team_streaks.append("—")
                else:
                    winning_team_mmr_changes.append("—")
                    winning_team_streaks.append("—")
            elif player_id and player_id.startswith('9000'):  # Dummy player
                winning_team_mmr_changes.append("+0 MMR")
                winning_team_streaks.append("—")
            else:
                winning_team_mmr_changes.append("—")
                winning_team_streaks.append("—")

        # Extract MMR changes for losing team with proper global/ranked filtering
        for player in losing_team:
            player_id = player.get("id")

            if player_id and player_id in mmr_changes_by_player:
                change_data = mmr_changes_by_player[player_id]

                # Only show MMR changes that match the current match type
                change_is_global = change_data.get("is_global", False)
                if change_is_global == is_global:
                    mmr_change = change_data["mmr_change"]
                    streak = change_data["streak"]

                    losing_team_mmr_changes.append(f"{mmr_change} MMR")  # Already negative

                    # Format streak display for losses
                    if streak <= -3:
                        losing_team_streaks.append(f"❄️ {abs(streak)}L")
                    elif streak == -2:
                        losing_team_streaks.append(f"↘️ {abs(streak)}L")
                    elif streak == -1:
                        losing_team_streaks.append(f"↘️ {abs(streak)}L")
                    else:
                        losing_team_streaks.append("—")
                else:
                    losing_team_mmr_changes.append("—")
                    losing_team_streaks.append("—")
            elif player_id and player_id.startswith('9000'):  # Dummy player
                losing_team_mmr_changes.append("-0 MMR")
                losing_team_streaks.append("—")
            else:
                losing_team_mmr_changes.append("—")
                losing_team_streaks.append("—")

        # Create the embed with enhanced formatting
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

        # Create individual fields for each winning player with SAFE member fetching
        for i, player in enumerate(winning_team):
            try:
                # CLOUD-SAFE member fetching with fallback to stored name
                if is_cloud_platform():
                    # On cloud platforms, skip member fetching to avoid rate limits
                    name = player.get('name', 'Unknown')
                else:
                    # Only fetch members locally
                    member = await safe_fetch_member(interaction.guild, player.get("id", 0))
                    name = member.display_name if member else player.get('name', 'Unknown')
            except:
                name = player.get("name", "Unknown")

            # Enhanced display with simplified MMR format
            mmr_display = winning_team_mmr_changes[i] if i < len(winning_team_mmr_changes) else "—"
            streak_display = winning_team_streaks[i] if i < len(winning_team_streaks) else "—"

            embed.add_field(
                name=f"**{name}**",
                value=f"{mmr_display}\n{streak_display}",
                inline=True
            )

        # Spacer field if needed for proper alignment (for 3-column layout)
        if len(winning_team) % 3 == 1:
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)
        elif len(winning_team) % 3 == 2:
            embed.add_field(name="\u200b", value="\u200b", inline=True)

        # Add Losers header
        embed.add_field(name="😔 Losers", value="\u200b", inline=False)

        # Create individual fields for each losing player with SAFE member fetching
        for i, player in enumerate(losing_team):
            try:
                # CLOUD-SAFE member fetching with fallback to stored name
                if is_cloud_platform():
                    # On cloud platforms, skip member fetching to avoid rate limits
                    name = player.get('name', 'Unknown')
                else:
                    # Only fetch members locally
                    member = await safe_fetch_member(interaction.guild, player.get("id", 0))
                    name = member.display_name if member else player.get('name', 'Unknown')
            except:
                name = player.get("name", "Unknown")

            # Enhanced display with simplified MMR format
            mmr_display = losing_team_mmr_changes[i] if i < len(losing_team_mmr_changes) else "—"
            streak_display = losing_team_streaks[i] if i < len(losing_team_streaks) else "—"

            embed.add_field(
                name=f"**{name}**",
                value=f"{mmr_display}\n{streak_display}",
                inline=True
            )

        # Spacer field if needed for proper alignment (for 3-column layout)
        if len(losing_team) % 3 == 1:
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)
        elif len(losing_team) % 3 == 2:
            embed.add_field(name="\u200b", value="\u200b", inline=True)

        # Enhanced MMR System explanation with streak info
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

        # Send the embed using cloud-safe followup
        print(f"📤 Sending match results embed for {match_id}")
        await cloud_safe_followup(interaction, embed=embed)
        print(f"✅ Match report completed successfully for {match_id}")

    except discord.HTTPException as e:
        print(f"❌ Discord HTTP error in match report: {e}")
        await RenderErrorHandler.handle_general_error(interaction, e, "match report")
    except asyncio.TimeoutError:
        print(f"❌ Timeout error in match report")
        await RenderErrorHandler.handle_timeout(interaction, "match report")
    except Exception as e:
        print(f"❌ Unexpected error in match report: {e}")
        import traceback
        traceback.print_exc()
        await RenderErrorHandler.handle_general_error(interaction, e, "match report")


def get_rank_from_mmr(mmr):
    """Helper function to determine rank from MMR"""
    if mmr >= 1600:
        return "Rank A"
    elif mmr >= 1100:
        return "Rank B"
    else:
        return "Rank C"


@bot.tree.command(name="checkpending", description="Check pending role updates (Admin only)")
async def checkpending_slash(interaction: discord.Interaction):
    if not has_admin_or_mod_permissions(interaction.user, interaction.guild):
        await interaction.response.send_message("Admin only", ephemeral=True)
        return

    pending_count = bulk_role_manager.get_pending_updates_count()

    embed = discord.Embed(
        title="📋 Pending Role Updates",
        description=f"There are **{pending_count}** pending role updates",
        color=0x3498db
    )

    if pending_count > 0:
        embed.add_field(
            name="Next Processing",
            value="3:00 AM (daily automatic processing)",
            inline=False
        )
        embed.add_field(
            name="Manual Processing",
            value="Use `/forceprocess @member` to process a specific player immediately",
            inline=False
        )
    else:
        embed.add_field(
            name="Status",
            value="✅ No pending updates",
            inline=False
        )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="forceprocess", description="Force process a player's role update (Admin only)")
@app_commands.describe(member="Member to process role update for")
async def forceprocess_slash(interaction: discord.Interaction, member: discord.Member):
    if not has_admin_or_mod_permissions(interaction.user, interaction.guild):
        await interaction.response.send_message("Admin only", ephemeral=True)
        return

    await interaction.response.defer()

    player_id = str(member.id)
    guild_id = str(interaction.guild.id)

    # Check if there's a pending update
    pending = bulk_role_manager.get_player_pending_update(player_id, guild_id)

    if not pending:
        await interaction.followup.send(f"{member.mention} has no pending role updates.")
        return

    # Process the update
    success = await bulk_role_manager.force_process_player_update(player_id, guild_id)

    if success:
        embed = discord.Embed(
            title="✅ Role Update Processed",
            description=f"Successfully processed role update for {member.mention}",
            color=0x00ff00
        )
        embed.add_field(
            name="New MMR",
            value=str(pending.get("new_mmr", "Unknown")),
            inline=True
        )
        embed.add_field(
            name="New Rank",
            value=pending.get("new_rank", "Unknown"),
            inline=True
        )
    else:
        embed = discord.Embed(
            title="❌ Processing Failed",
            description=f"Failed to process role update for {member.mention}",
            color=0xff0000
        )
        embed.add_field(
            name="Note",
            value="Check logs for details. Update will still be processed at 3 AM.",
            inline=False
        )

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
    leaderboard_url = os.environ.get('PUBLIC_URL', 'http://localhost:5000')

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
async def rank_slash_enhanced(interaction: discord.Interaction, member: discord.Member = None):
    if RESET_IN_PROGRESS:
        duration = ""
        if RESET_START_TIME:
            elapsed = datetime.datetime.now() - RESET_START_TIME
            duration = f" (Running for {elapsed.seconds // 60}m {elapsed.seconds % 60}s)"

        embed = discord.Embed(
            title="⛔ Rank Stats Unavailable",
            description=f"A leaderboard reset is currently in progress{duration}",
            color=0xff9900
        )
        embed.add_field(
            name="Please Wait",
            value="• Reset operations are running\n• Player stats are being updated\n• Rank information will be available after reset",
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Check if command is used in an allowed channel
    if not is_command_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
            ephemeral=True
        )
        return

    # ENHANCED: Defer response as this might involve fetching member data
    await interaction.response.defer()

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

        # ENHANCED: Get rank roles with rate limiting protection
        try:
            if rate_limiter:
                # Use rate limiter to get member data if needed
                await asyncio.sleep(0.1)  # Small delay to prevent rapid fetches

            rank_a_role = discord.utils.get(interaction.guild.roles, name="Rank A")
            rank_b_role = discord.utils.get(interaction.guild.roles, name="Rank B")
            rank_c_role = discord.utils.get(interaction.guild.roles, name="Rank C")
        except Exception as e:
            print(f"Error getting rank roles for /rank command: {e}")
            rank_a_role = rank_b_role = rank_c_role = None

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
                if is_self_check:
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
                    # FIX: Use followup instead of response
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    embed = discord.Embed(
                        title="No Rank Data",
                        description=f"{member.mention} hasn't verified their rank yet.",
                        color=0x95a5a6
                    )
                    # FIX: Use followup instead of response
                    await interaction.followup.send(embed=embed, ephemeral=True)
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
            "current_streak": 0,
            "longest_win_streak": 0,
            "longest_loss_streak": 0,
            "global_current_streak": 0,
            "global_longest_win_streak": 0,
            "global_longest_loss_streak": 0,
            "last_promotion": None
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

    # Get ALL streak information
    current_streak = player_data.get("current_streak", 0)
    longest_win_streak = player_data.get("longest_win_streak", 0)
    longest_loss_streak = player_data.get("longest_loss_streak", 0)
    global_current_streak = player_data.get("global_current_streak", 0)
    global_longest_win_streak = player_data.get("global_longest_win_streak", 0)
    global_longest_loss_streak = player_data.get("global_longest_loss_streak", 0)

    # Get rank protection information
    last_promotion = player_data.get("last_promotion")
    has_promotion_protection = False
    games_since_promotion = 0

    if last_promotion and not is_new:
        current_matches = player_data.get("matches", 0)
        matches_at_promotion = last_promotion.get("matches_at_promotion", 0)
        games_since_promotion = current_matches - matches_at_promotion
        has_promotion_protection = games_since_promotion < 3

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

    # Enhanced rank display with protection status
    rank_display = tier
    if has_promotion_protection:
        rank_display += f" 🛡️ (Protected: {3 - games_since_promotion} games left)"
    elif last_promotion and games_since_promotion < 10:
        rank_display += f" ⭐ (Promoted {games_since_promotion} games ago)"

    embed.add_field(name="Rank", value=rank_display, inline=True)

    if matches > 0:
        embed.add_field(name="Leaderboard", value=f"#{rank_position} of {total_players}", inline=True)
    else:
        embed.add_field(name="Leaderboard", value="Unranked (0 games)", inline=True)

    embed.add_field(name="MMR", value=str(mmr), inline=True)
    embed.add_field(name="Win Rate", value=f"{win_rate:.1f}%" if matches > 0 else "N/A", inline=True)
    embed.add_field(name="Record", value=f"{wins}W - {losses}L", inline=True)
    embed.add_field(name="Matches", value=str(matches), inline=True)

    # Add ranked streak information
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

    # Add global streak information
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

    # Enhanced MMR System section
    if not is_new and matches > 0:
        embed.add_field(name="__🎯 Enhanced MMR System__", value="", inline=False)

        system_info = []

        # Streak bonuses
        if abs(current_streak) >= 2 or abs(global_current_streak) >= 2:
            system_info.append("🔥 **Active Streak Bonuses**: 2x multiplier at 2+ streak")

        # Momentum system
        if matches >= 10:
            system_info.append("⚡ **Momentum Tracking**: Recent performance affects MMR")

        # Rank protection
        if has_promotion_protection:
            system_info.append(f"🛡️ **Promotion Protection**: {3 - games_since_promotion} games of 50% loss reduction")
        elif mmr >= 1100:  # Check for demotion protection
            if mmr < 1150:  # Close to Rank B/C boundary
                system_info.append("🛡️ **Demotion Protection**: Reduced losses near rank boundary")
            elif mmr >= 1600 and mmr < 1650:  # Close to Rank A/B boundary
                system_info.append("🛡️ **Demotion Protection**: Reduced losses near rank boundary")
        elif mmr >= 1050 and mmr < 1100:  # Close to promotion to Rank B
            system_info.append("🚀 **Promotion Assistance**: Bonus MMR gains near rank up")
        elif mmr >= 1550 and mmr < 1600:  # Close to promotion to Rank A
            system_info.append("🚀 **Promotion Assistance**: Bonus MMR gains near rank up")

        if system_info:
            embed.add_field(name="Active Bonuses", value="\n".join(system_info), inline=False)
        else:
            embed.add_field(name="Available Features",
                            value="• 2x streak multipliers (2+ wins/losses)\n• Momentum bonuses (10+ games)\n• Rank boundary protection",
                            inline=False)

    # Add note for new players
    if is_new:
        embed.set_footer(
            text="⭐ New player - this is your starting MMR based on rank verification. Play matches to earn your spot on the leaderboard!")
    else:
        # Add comprehensive streak info in footer
        footer_info = []
        if longest_win_streak >= 3:
            footer_info.append(f"Best ranked streak: {longest_win_streak} wins")
        if abs(longest_loss_streak) >= 3:
            footer_info.append(f"Worst ranked streak: {abs(longest_loss_streak)} losses")
        if global_longest_win_streak >= 3:
            footer_info.append(f"Best global streak: {global_longest_win_streak} wins")
        if abs(global_longest_loss_streak) >= 3:
            footer_info.append(f"Worst global streak: {abs(global_longest_loss_streak)} losses")

        if footer_info:
            embed.set_footer(text=" | ".join(footer_info))
        else:
            embed.set_footer(text="Enhanced MMR system with streak bonuses, momentum tracking, and rank protection")

    embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)

    # FIX: Use followup instead of response since we already deferred
    await interaction.followup.send(embed=embed)

    # Add final delay to prevent rapid successive /rank commands
    await asyncio.sleep(0.5)


@bot.tree.command(name="addplayer", description="Add a player to the queue (Admin/Mod only)")
@app_commands.describe(member="The member to add to the queue")
async def addplayer_slash(interaction: discord.Interaction, member: discord.Member):
    if RESET_IN_PROGRESS:
        duration = ""
        if RESET_START_TIME:
            elapsed = datetime.datetime.now() - RESET_START_TIME
            duration = f" (Running for {elapsed.seconds // 60}m {elapsed.seconds % 60}s)"

        embed = discord.Embed(
            title="⛔ Admin Queue Commands Disabled",
            description=f"A leaderboard reset is currently in progress{duration}",
            color=0xff9900
        )
        embed.add_field(
            name="Please Wait",
            value="• Reset operations are running\n• Admin queue commands will be re-enabled automatically\n• Check back in a few minutes",
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

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
    if RESET_IN_PROGRESS:
        duration = ""
        if RESET_START_TIME:
            elapsed = datetime.datetime.now() - RESET_START_TIME
            duration = f" (Running for {elapsed.seconds // 60}m {elapsed.seconds % 60}s)"

        embed = discord.Embed(
            title="⛔ Admin Queue Commands Disabled",
            description=f"A leaderboard reset is currently in progress{duration}",
            color=0xff9900
        )
        embed.add_field(
            name="Please Wait",
            value="• Reset operations are running\n• Admin queue commands will be re-enabled automatically\n• Check back in a few minutes",
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

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
async def on_app_command_error_cloud_enhanced(interaction: discord.Interaction, error):
    print(f"Command error: {error}")

    try:
        # Use cloud-aware error handling
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
            elif isinstance(error.original, discord.HTTPException):
                await RenderErrorHandler.handle_general_error(interaction, error.original, "command")
                return
            elif isinstance(error.original, asyncio.TimeoutError):
                await RenderErrorHandler.handle_timeout(interaction, "command")
                return
            else:
                print(f"Command invoke error: {error.original}")
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "An error occurred. Please try again.",
                        ephemeral=True
                    )
        else:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "An unexpected error occurred. Please try again.",
                    ephemeral=True
                )
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
async def adjustmmr_slash_rate_limited(interaction: discord.Interaction, player: discord.Member, amount: int,
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

    # ENHANCED: Check for duplicate command prevention
    if await is_duplicate_command_for_adjustmmr(interaction, player.id):
        await interaction.response.send_message(
            "⚠️ An MMR adjustment for this player is already in progress. Please wait.",
            ephemeral=True
        )
        return

    # CLOUD-SAFE defer with enhanced error handling
    try:
        defer_success = await cloud_safe_defer(interaction)
        if not defer_success:
            await RenderErrorHandler.handle_rate_limit(interaction, "MMR adjustment")
            return
    except Exception as defer_error:
        print(f"Critical defer error in adjustmmr: {defer_error}")
        await RenderErrorHandler.handle_general_error(interaction, defer_error, "MMR adjustment")
        return

    # Add cloud platform delay before processing
    if is_cloud_platform():
        await asyncio.sleep(random.uniform(2.0, 4.0))
    else:
        await asyncio.sleep(random.uniform(1.0, 2.0))

    # Determine which MMR to adjust
    is_global = global_mmr.lower() == "true"
    mmr_type = "Global" if is_global else "Ranked"

    # Get player data with rate limiting protection
    player_id = str(player.id)

    try:
        # Add delay before database operation
        await asyncio.sleep(random.uniform(0.5, 1.0))
        player_data = system_coordinator.match_system.players.find_one({"id": player_id})
    except Exception as db_error:
        print(f"Database error in adjustmmr: {db_error}")
        await cloud_safe_followup(interaction, "❌ Database error occurred. Please try again.", ephemeral=True)
        return

    # Handle player not found
    if not player_data:
        # Check for rank record as fallback with rate limiting
        try:
            await asyncio.sleep(random.uniform(0.3, 0.7))
            rank_record = db.get_collection('ranks').find_one({"discord_id": player_id})
        except Exception as rank_error:
            print(f"Error checking rank record: {rank_error}")
            await cloud_safe_followup(interaction, "❌ Error accessing player data. Please try again.", ephemeral=True)
            return

        if rank_record:
            # Create player entry with initial values
            try:
                await create_new_player_entry_rate_limited(
                    interaction, player, player_id, rank_record, is_global, amount, mmr_type
                )
                return
            except Exception as create_error:
                print(f"Error creating new player entry: {create_error}")
                await cloud_safe_followup(interaction, "❌ Error creating player entry. Please try again.",
                                          ephemeral=True)
                return
        else:
            await cloud_safe_followup(interaction,
                                      f"Player {player.mention} not found in the database and has no rank verification. They need to verify their rank first.",
                                      ephemeral=True
                                      )
            return

    # Update existing player with rate limiting protection
    try:
        await update_existing_player_mmr_rate_limited(
            interaction, player, player_id, player_data, is_global, amount, mmr_type
        )
    except Exception as update_error:
        print(f"Error updating existing player: {update_error}")
        await cloud_safe_followup(interaction, "❌ Error updating player MMR. Please try again.", ephemeral=True)


async def is_duplicate_command_for_adjustmmr(interaction, target_player_id):
    """Enhanced duplicate prevention specifically for adjustmmr commands"""
    admin_id = interaction.user.id
    command_name = "adjustmmr"

    # Create a unique key for this admin adjusting this specific player
    key = f"{admin_id}:{command_name}:{target_player_id}"

    async with command_lock:
        now = datetime.datetime.now(datetime.UTC).timestamp()

        # Check if this exact command combination was run very recently (within 5 seconds)
        if key in recent_commands:
            last_time = recent_commands[key]
            if now - last_time < 5.0:  # 5 second cooldown for MMR adjustments
                print(
                    f"DUPLICATE ADJUSTMMR BLOCKED: {command_name} from {interaction.user.name} for player {target_player_id}")
                return True

        # Update the timestamp
        recent_commands[key] = now

        # Clean old entries
        old_keys = [k for k, v in recent_commands.items() if now - v > 15.0]
        for old_key in old_keys:
            del recent_commands[old_key]

    return False


async def create_new_player_entry_rate_limited(interaction, player, player_id, rank_record, is_global, amount,
                                               mmr_type):
    """Create new player entry with rate limiting protection"""

    if is_global:
        starting_mmr = rank_record.get("global_mmr", 300)
        new_mmr = starting_mmr + amount

        # Add delay before database insert
        await asyncio.sleep(random.uniform(0.5, 1.0))

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
            "current_streak": 0,
            "longest_win_streak": 0,
            "longest_loss_streak": 0,
            "global_current_streak": 0,
            "global_longest_win_streak": 0,
            "global_longest_loss_streak": 0,
            "created_at": datetime.datetime.utcnow(),
            "last_updated": datetime.datetime.utcnow()
        })

        await cloud_safe_followup(interaction,
                                  f"Created new player entry for {player.mention}. Adjusted {mmr_type} MMR from {starting_mmr} to {new_mmr} ({'+' if amount >= 0 else ''}{amount})."
                                  )
    else:
        # For ranked MMR, use tier-based MMR
        tier = rank_record.get("tier", "Rank C")
        starting_mmr = system_coordinator.match_system.TIER_MMR.get(tier, 600)
        new_mmr = starting_mmr + amount

        # Add delay before database insert
        await asyncio.sleep(random.uniform(0.5, 1.0))

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
            "current_streak": 0,
            "longest_win_streak": 0,
            "longest_loss_streak": 0,
            "global_current_streak": 0,
            "global_longest_win_streak": 0,
            "global_longest_loss_streak": 0,
            "created_at": datetime.datetime.utcnow(),
            "last_updated": datetime.datetime.utcnow()
        })

        # ENHANCED: Try to update Discord role with ULTRA-SAFE rate limiting protection
        try:
            # Add delay before role update
            await asyncio.sleep(random.uniform(2.0, 4.0))

            await system_coordinator.match_system.update_discord_role_ultra_safe(
                interaction, player_id, new_mmr
            )

            await cloud_safe_followup(interaction,
                                      f"✅ Created new player entry for {player.mention}. Adjusted {mmr_type} MMR from {starting_mmr} to {new_mmr} ({'+' if amount >= 0 else ''}{amount}). Discord role updated."
                                      )
        except Exception as role_error:
            print(f"Warning: Could not update Discord role for {player.display_name}: {role_error}")
            await cloud_safe_followup(interaction,
                                      f"⚠️ Created new player entry for {player.mention}. Adjusted {mmr_type} MMR from {starting_mmr} to {new_mmr} ({'+' if amount >= 0 else ''}{amount}). Role update failed - may need manual update."
                                      )


async def update_existing_player_mmr_rate_limited(interaction, player, player_id, player_data, is_global, amount,
                                                  mmr_type):
    """Update existing player MMR with comprehensive rate limiting"""

    # Determine old and new MMR values
    if is_global:
        old_mmr = player_data.get("global_mmr", 300)
        new_mmr = old_mmr + amount

        # Add delay before database update
        await asyncio.sleep(random.uniform(0.5, 1.0))

        system_coordinator.match_system.players.update_one(
            {"id": player_id},
            {"$set": {
                "global_mmr": new_mmr,
                "last_updated": datetime.datetime.utcnow()
            }}
        )

        # Create response embed for global MMR (no role update needed)
        await send_mmr_adjustment_embed_rate_limited(
            interaction, player, mmr_type, old_mmr, new_mmr, amount,
            tier_changed=False, role_updated=False
        )

    else:
        old_mmr = player_data.get("mmr", 600)
        new_mmr = old_mmr + amount

        # Determine rank changes
        old_tier = get_rank_from_mmr(old_mmr)
        new_tier = get_rank_from_mmr(new_mmr)
        tier_changed = old_tier != new_tier

        # Add delay before database update
        await asyncio.sleep(random.uniform(0.5, 1.0))

        system_coordinator.match_system.players.update_one(
            {"id": player_id},
            {"$set": {
                "mmr": new_mmr,
                "last_updated": datetime.datetime.utcnow()
            }}
        )

        # ENHANCED: Try to update Discord role for ranked MMR changes with ULTRA-SAFE rate limiting protection
        role_updated = False

        if tier_changed:
            print(f"🚨 RANK CHANGE DETECTED: {player.display_name} {old_tier} → {new_tier}")

            try:
                # PRIORITY: Immediate role update for rank changes
                await asyncio.sleep(random.uniform(1.0, 2.0))  # Shorter delay for rank changes

                await system_coordinator.match_system.update_discord_role_ultra_safe(
                    interaction, player_id, new_mmr
                )
                role_updated = True
                print(f"✅ PRIORITY ROLE UPDATE: {player.display_name} role updated to {new_tier}")

            except Exception as role_error:
                print(f"❌ Priority role update failed for {player.display_name}: {role_error}")
        else:
            # Same rank - try normal role update with longer delay
            try:
                await asyncio.sleep(random.uniform(3.0, 6.0))  # Longer delay for same rank

                await system_coordinator.match_system.update_discord_role_ultra_safe(
                    interaction, player_id, new_mmr
                )
                role_updated = True

            except Exception as role_error:
                print(f"Warning: Normal role update failed for {player.display_name}: {role_error}")

        # Send response with comprehensive information
        await send_mmr_adjustment_embed_rate_limited(
            interaction, player, mmr_type, old_mmr, new_mmr, amount,
            tier_changed, role_updated, old_tier, new_tier
        )


async def send_mmr_adjustment_embed_rate_limited(interaction, player, mmr_type, old_mmr, new_mmr, amount,
                                                 tier_changed=False, role_updated=False, old_tier=None, new_tier=None):
    """Send MMR adjustment embed with rate limiting protection"""

    try:
        # Add delay before sending embed
        await asyncio.sleep(random.uniform(0.3, 0.7))

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
        if not mmr_type.startswith("Global"):
            if tier_changed and old_tier and new_tier:
                embed.add_field(
                    name="🎯 Rank Change",
                    value=f"**Old Tier:** {old_tier}\n**New Tier:** {new_tier}\n**Promotion/Demotion:** {'✅ Yes' if tier_changed else 'No'}",
                    inline=False
                )

                if role_updated:
                    embed.add_field(
                        name="🔄 Discord Role",
                        value="✅ Discord role updated successfully",
                        inline=False
                    )
                else:
                    embed.add_field(
                        name="⚠️ Discord Role",
                        value="❌ Role update failed - may need manual update",
                        inline=False
                    )
            else:
                # Same tier
                embed.add_field(
                    name="Rank Tier",
                    value=f"**Tier:** {new_tier or get_rank_from_mmr(new_mmr)} (unchanged)",
                    inline=False
                )

                if role_updated:
                    embed.add_field(
                        name="Discord Role",
                        value="✅ Role information updated",
                        inline=False
                    )

        embed.set_footer(
            text=f"Adjusted by {interaction.user.display_name} | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        # Use cloud-safe followup with additional rate limiting
        await cloud_safe_followup(interaction, embed=embed)

    except Exception as embed_error:
        print(f"Error sending MMR adjustment embed: {embed_error}")
        # Fallback to simple text message
        try:
            await cloud_safe_followup(interaction,
                                      f"✅ MMR adjustment completed for {player.mention}: {old_mmr} → {new_mmr} ({'+' if amount >= 0 else ''}{amount})"
                                      )
        except:
            pass  # If even the fallback fails, we've already updated the database


async def set_reset_status(status: bool, interaction=None):
    """Set the reset status and notify channels"""
    global RESET_IN_PROGRESS, RESET_START_TIME

    RESET_IN_PROGRESS = status

    if status:
        RESET_START_TIME = datetime.datetime.now()
        if interaction:
            # Notify all queue channels that reset is starting
            for guild in bot.guilds:
                for channel in guild.text_channels:
                    if channel.name.lower() in ["rank-a", "rank-b", "rank-c", "global"]:
                        try:
                            embed = discord.Embed(
                                title="🚨 RESET IN PROGRESS",
                                description="⛔ **Queuing is temporarily disabled**\n\nA leaderboard reset is currently running. Please wait for completion.",
                                color=0xff0000
                            )
                            embed.add_field(
                                name="What's Happening",
                                value="• Database is being reset\n• Discord roles may be updated\n• This may take several minutes",
                                inline=False
                            )
                            embed.set_footer(text=f"Reset started by {interaction.user.display_name}")
                            await channel.send(embed=embed)
                        except Exception as e:
                            print(f"Error notifying channel {channel.name}: {e}")
    else:
        RESET_START_TIME = None
        if interaction:
            # Notify that reset is complete
            for guild in bot.guilds:
                for channel in guild.text_channels:
                    if channel.name.lower() in ["rank-a", "rank-b", "rank-c", "global"]:
                        try:
                            embed = discord.Embed(
                                title="✅ RESET COMPLETE",
                                description="🎉 **Queuing is now re-enabled!**\n\nThe leaderboard reset has finished successfully.",
                                color=0x00ff00
                            )
                            embed.add_field(
                                name="Ready to Play",
                                value="• Use `/queue` to join matches\n• Check `/rank` for your stats\n• Visit the website for leaderboards",
                                inline=False
                            )
                            await channel.send(embed=embed)
                        except Exception as e:
                            print(f"Error notifying channel {channel.name}: {e}")

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

    global RESET_IN_PROGRESS
    if RESET_IN_PROGRESS:
        await interaction.response.send_message(
            "❌ A reset is already in progress! Please wait for it to complete.",
            ephemeral=True
        )
        return

    # Check confirmation
    if confirmation != "CONFIRM":
        await interaction.response.send_message(
            "❌ Leaderboard reset canceled. You must type 'CONFIRM' (all caps) to confirm this action.",
            ephemeral=True
        )
        return

    await set_reset_status(True, interaction)

    # IMMEDIATE response to prevent timeout
    await interaction.response.send_message(
        f"🔄 Starting {reset_type} leaderboard reset... This will take several minutes. Please wait.",
        ephemeral=True
    )

    # Run the actual reset in a background task to avoid interaction timeouts
    asyncio.create_task(perform_reset_background_enhanced(interaction, reset_type))


async def perform_reset_background_enhanced(interaction: discord.Interaction, reset_type: str):
    """ENHANCED reset with ULTRA-SAFE rate limiting for Discord roles"""
    try:
        channel = interaction.channel
        user = interaction.user
        guild = interaction.guild

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
        matches_removed = 0

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
            # Send initial progress message
            await safe_send_message(channel, "🔄 Starting complete reset... (1/5) Creating backups")

            # 1. Make backup of rank verification data
            ranks_collection = db.get_collection('ranks')
            all_ranks = list(ranks_collection.find())
            backup_ranks_collection = db.get_collection(f"ranks_backup_{timestamp}")
            if all_ranks:
                backup_ranks_collection.insert_many(all_ranks)

            # 2. ULTRA-SAFE DISCORD ROLE REMOVAL with extensive rate limiting protection
            await safe_send_message(channel, "🔄 (2/5) Removing Discord roles... This may take 10+ minutes")

            # Get rank roles
            rank_role_names = ["Rank A", "Rank B", "Rank C"]
            rank_roles = [discord.utils.get(guild.roles, name=name) for name in rank_role_names]
            rank_roles = [role for role in rank_roles if role is not None]

            if rank_roles:
                try:
                    # ENHANCED: Use the ULTRA-SAFE approach with extreme delays
                    removal_result = await ultra_safe_bulk_role_removal(guild, rank_roles, channel)
                    roles_removed_count = removal_result['success_count']
                    role_removal_errors = removal_result['errors']

                except Exception as role_error:
                    print(f"Error in role removal: {role_error}")
                    role_removal_errors.append(f"Role removal failed: {str(role_error)}")
                    await safe_send_message(channel, f"⚠️ Role removal encountered errors: {str(role_error)}")

            await safe_send_message(channel, "🔄 (3/5) Clearing player records...")

            # 3. DELETE all player records
            players_removed = system_coordinator.match_system.players.delete_many({}).deleted_count
            reset_count = players_removed

            await safe_send_message(channel, "🔄 (4/5) Clearing rank verifications...")

            # 4. Delete all rank verification records
            ranks_removed = ranks_collection.delete_many({}).deleted_count

            await safe_send_message(channel, "🔄 (5/5) Clearing match history...")

            # 5. Delete all matches
            matches_result = system_coordinator.match_system.matches.delete_many({})
            matches_removed = matches_result.deleted_count

        # Record the reset in the resets collection
        resets_collection = db.get_collection('resets')
        resets_collection.insert_one({
            "type": "leaderboard_reset",
            "reset_type": reset_type,
            "timestamp": datetime.datetime.utcnow(),
            "admin_id": str(user.id),
            "admin_name": user.display_name,
            "backup_collection": backup_collection_name,
            "roles_removed_count": roles_removed_count,
            "role_removal_errors_count": len(role_removal_errors) if reset_type == "all" else 0
        })

        # Send completion message
        embed = discord.Embed(
            title="🔄 Leaderboard Reset Complete",
            description=f"Reset type: **{reset_type.upper()}**",
            color=0x00ff00  # Green for success
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
                value=f"✅ Success: **{roles_removed_count}** members\n❌ Errors: **{len(role_removal_errors)}** members",
                inline=False
            )

            embed.add_field(
                name="Important",
                value="**All players must re-verify their ranks** before joining queues again.",
                inline=False
            )

        embed.set_footer(
            text=f"Reset by {user.display_name} | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        await safe_send_message(channel, embed=embed)

        # Final announcement for complete reset
        if reset_type == "all":
            announcement = discord.Embed(
                title="🚨 Complete Season Reset Performed",
                description=f"A complete leaderboard reset has been performed by {user.mention}",
                color=0xff0000
            )

            announcement.add_field(
                name="What This Means",
                value=(
                    f"• **{roles_removed_count}** members had their Discord rank roles removed\n"
                    f"• All match history and MMR has been cleared\n"
                    f"• All rank verifications have been reset"
                ),
                inline=False
            )

            announcement.add_field(
                name="To Play Again",
                value=(
                    "1. Visit the rank verification page on the website\n"
                    "2. Re-verify your Rocket League rank\n"
                    "3. Get your Discord role and starting MMR back\n"
                    "4. Use `/queue` to join matches again"
                ),
                inline=False
            )

            await safe_send_message(channel, embed=announcement)

        await set_reset_status(False, interaction)

    except Exception as e:
        print(f"Error in background reset: {e}")
        import traceback
        traceback.print_exc()

        await set_reset_status(False, interaction)

        error_embed = discord.Embed(
            title="❌ Reset Error",
            description=f"An error occurred during the reset: {str(e)}",
            color=0xff0000
        )
        await safe_send_message(interaction.channel, embed=error_embed)


async def safe_send_message(channel, content=None, embed=None, max_retries=3):
    """Safely send a message with rate limiting protection"""
    for attempt in range(max_retries):
        try:
            if embed:
                return await channel.send(embed=embed)
            else:
                return await channel.send(content)
        except discord.HTTPException as e:
            if e.status == 429:  # Rate limited
                retry_after = getattr(e, 'retry_after', 2 ** attempt)
                print(f"Rate limited sending message, waiting {retry_after}s")
                await asyncio.sleep(retry_after)
                continue
            else:
                print(f"HTTP error sending message: {e}")
                break
        except Exception as e:
            print(f"Error sending message: {e}")
            if attempt == max_retries - 1:
                break
            await asyncio.sleep(1)

    print(f"Failed to send message after {max_retries} attempts")
    return None


async def ultra_safe_bulk_role_removal(guild, roles_to_remove, progress_channel=None):
    """ULTRA-SAFE role removal with EXTREME rate limiting protection"""
    print(f"🚀 Starting ULTRA-SAFE bulk role removal for {len(roles_to_remove)} roles")

    # Find members with these roles
    members_with_roles = []
    for member in guild.members:
        if member.bot:
            continue
        member_roles = [role for role in member.roles if role in roles_to_remove]
        if member_roles:
            members_with_roles.append((member, member_roles))

    if not members_with_roles:
        return {"success_count": 0, "errors": [], "message": "No members with roles found"}

    print(f"Found {len(members_with_roles)} members with roles to remove")

    success_count = 0
    errors = []

    # ENHANCED: Use the batch processing system for maximum safety
    from rate_limiter import batch_role_operations_with_extreme_safety

    # Convert to batch operations format
    operations = []
    for member, member_roles in members_with_roles:
        operations.append({
            'member': member,
            'operation': 'remove',
            'roles': member_roles,
            'reason': 'Complete leaderboard reset'
        })

    # Progress callback
    async def progress_callback(message):
        if progress_channel:
            try:
                await rate_limiter.send_message_with_limit(
                    progress_channel, f"🔄 Bulk Role Removal: {message}", max_retries=1
                )
            except:
                pass  # Ignore progress message errors

    print(f"🚀 Starting batch role removal with extreme safety...")

    # Execute with extreme safety
    results = await batch_role_operations_with_extreme_safety(
        rate_limiter, operations, progress_callback
    )

    return {
        "success_count": results['successful'],
        "errors": results['errors'],
        "message": f"Completed: {results['successful']} successful, {results['failed']} errors"
    }


async def remove_discord_role_rate_limited(member, *roles, reason=None):
    """
    Rate-limited role removal using the existing rate limiter
    """
    try:
        # Use the rate limiter from your main.py
        await rate_limiter.remove_role_with_limit(member, *roles, reason=reason)
        return True
    except Exception as e:
        print(f"Error removing roles from {member.display_name}: {e}")
        raise  # Re-raise to be caught by the calling code


@bot.tree.command(name="resetplayer", description="Reset all data for a specific player (Admin only)")
@app_commands.describe(
    member="The member whose data you want to reset",
    confirmation="Type 'CONFIRM' to confirm the reset"
)
async def resetplayer_slash(interaction: discord.Interaction, member: discord.Member, confirmation: str):
    # Check permissions and confirmation
    if not is_command_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
            ephemeral=True
        )
        return

    if not has_admin_or_mod_permissions(interaction.user, interaction.guild):
        await interaction.response.send_message(
            "You need administrator permissions or the 6mod role to use this command.",
            ephemeral=True)
        return

    if confirmation != "CONFIRM":
        await interaction.response.send_message(
            "❌ Player reset canceled. You must type 'CONFIRM' (all caps) to confirm this action.",
            ephemeral=True
        )
        return

    # ENHANCED: Defer with longer timeout consideration
    await interaction.response.defer()

    # Add initial delay to prevent rapid successive commands
    await asyncio.sleep(random.uniform(1.0, 3.0))

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
        # Get current player data before deletion (for summary)
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

        # Delete player data from players collection
        try:
            result = system_coordinator.match_system.players.delete_one({"id": player_id})
            if result.deleted_count > 0:
                reset_summary["player_data"] = True
                print(f"Deleted player data for {player_name} (ID: {player_id})")
            else:
                print(f"No player data found for {player_name} (ID: {player_id})")
        except Exception as e:
            reset_summary["errors"].append(f"Failed to delete player data: {str(e)}")

        # Delete rank verification from ranks collection
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

        # ENHANCED: Remove Discord rank roles with ULTRA-SAFE operations and better error handling
        try:
            # Get rank roles
            rank_a_role = discord.utils.get(interaction.guild.roles, name="Rank A")
            rank_b_role = discord.utils.get(interaction.guild.roles, name="Rank B")
            rank_c_role = discord.utils.get(interaction.guild.roles, name="Rank C")
            rank_roles = [role for role in [rank_a_role, rank_b_role, rank_c_role] if role]

            # Check if member has any rank roles
            member_rank_roles = [role for role in member.roles if role in rank_roles]

            if member_rank_roles:
                print(f"🔄 Removing {len(member_rank_roles)} rank roles from {player_name}")

                # ENHANCED: Use safe role removal with comprehensive error handling
                role_removal_success = False

                try:
                    # Add pre-operation delay
                    await asyncio.sleep(random.uniform(2.0, 5.0))

                    # Use safe role operation method
                    success = await safe_role_operation(
                        member, 'remove', *member_rank_roles,
                        reason=f"Player reset by {interaction.user.display_name}"
                    )

                    if success:
                        role_removal_success = True
                        print(f"✅ Successfully removed {len(member_rank_roles)} rank role(s) from {player_name}")
                    else:
                        print(f"❌ Failed to remove roles from {player_name}")

                except Exception as role_error:
                    print(f"❌ Critical error removing roles from {player_name}: {role_error}")

                reset_summary["discord_roles"] = role_removal_success

            else:
                print(f"ℹ️ No rank roles found for {player_name}")
                reset_summary["discord_roles"] = True  # No roles to remove

        except Exception as e:
            reset_summary["discord_roles"] = False
            reset_summary["errors"].append(f"Unexpected error removing roles: {str(e)}")

        # Create response embed
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
        else:
            reset_items.append("⚠️ Discord roles (may need manual removal)")

        embed.add_field(name="Data Reset", value="\n".join(reset_items), inline=False)

        # Add errors if any
        if reset_summary["errors"]:
            error_text = "\n".join([f"❌ {error}" for error in reset_summary["errors"]])
            embed.add_field(name="Issues Encountered", value=error_text, inline=False)

        embed.set_footer(
            text=f"Reset by {interaction.user.display_name} | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        # Send response with enhanced safety
        try:
            await safe_send_followup(interaction, embed=embed)
        except Exception as e:
            print(f"Error sending reset completion message: {e}")
            # Fallback simple message
            try:
                await interaction.followup.send(f"Player reset completed for {member.mention}", ephemeral=True)
            except:
                pass

        # ENHANCED: Send DM to player with ultra-safe messaging
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
                    "• Your Discord rank role may have been removed"
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

            # ENHANCED: Use rate limiter for DM with multiple fallbacks
            dm_success = False

            # Method 1: Rate limiter approach
            if rate_limiter:
                try:
                    await asyncio.sleep(random.uniform(2.0, 5.0))  # Delay before DM
                    await rate_limiter.send_message_with_limit(member, embed=dm_embed, max_retries=1)
                    dm_success = True
                    print(f"✅ DM sent to {player_name} via rate limiter")
                except discord.Forbidden:
                    print(f"⚠️ Cannot DM {player_name} - DMs disabled")
                except Exception as e:
                    print(f"⚠️ Rate limiter DM failed for {player_name}: {e}")

            # Method 2: Manual approach with longer delay if rate limiter failed
            if not dm_success:
                try:
                    await asyncio.sleep(random.uniform(5.0, 8.0))  # Longer delay for manual method
                    await member.send(embed=dm_embed)
                    dm_success = True
                    print(f"✅ DM sent to {player_name} via manual method")
                except discord.Forbidden:
                    print(f"⚠️ Cannot DM {player_name} - DMs disabled")
                except Exception as e:
                    print(f"⚠️ Manual DM failed for {player_name}: {e}")

            if not dm_success:
                print(f"❌ Could not send DM to {player_name} via any method")

        except Exception as dm_error:
            print(f"❌ Critical error in DM handling for {player_name}: {str(dm_error)}")

        print(f"✅ Player reset completed for {player_name} by {interaction.user.display_name}")

        # FINAL: Add a delay at the end to prevent rapid successive commands
        await asyncio.sleep(random.uniform(2.0, 5.0))

    except Exception as e:
        await interaction.followup.send(f"❌ Error resetting player: {str(e)}")
        print(f"Error in resetplayer command: {e}")
        import traceback
        traceback.print_exc()


# 4. Sub Command
@bot.tree.command(name="sub", description="Substitute or swap players in an active match")
@app_commands.describe(
    match_id="The ID of the match",
    action="Choose to substitute a player or swap two players",
    player_out="The player to remove (for substitute) or first player (for swap)",
    player_in="The player to add (for substitute) or second player (for swap)"
)
@app_commands.choices(action=[
    app_commands.Choice(name="Substitute Player", value="substitute"),
    app_commands.Choice(name="Swap Players", value="swap")
])
async def sub_slash(interaction: discord.Interaction, match_id: str, action: str, player_out: discord.Member,
                    player_in: discord.Member):
    # Check if command is used in an allowed channel
    if not is_command_channel(interaction.channel):
        await interaction.response.send_message(
            f"{interaction.user.mention}, this command can only be used in the rank-a, rank-b, rank-c, global, or sixgents channels.",
            ephemeral=True
        )
        return

    # Check if user has permissions
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
            "Substitutions and swaps are only available for in-progress matches.",
            ephemeral=True
        )
        return

    # Route to appropriate function based on action
    if action == "substitute":
        await handle_substitute(interaction, match_id, match, player_out, player_in)
    elif action == "swap":
        await handle_swap(interaction, match_id, match, player_out, player_in)


async def handle_substitute(interaction, match_id, match, player_out, player_in):
    """Handle player substitution (replace player_out with player_in)"""

    # Get player data
    player_out_id = str(player_out.id)
    player_in_id = str(player_in.id)
    player_out_name = player_out.display_name
    player_in_name = player_in.display_name
    player_out_mention = player_out.mention
    player_in_mention = player_in.mention

    # Get teams
    team1 = match.get("team1", [])
    team2 = match.get("team2", [])

    # Check which team player_out is on
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
            f"{player_in_mention} is already part of match `{match_id}`. Use the 'Swap Players' action instead.",
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
        color=0x00aaff  # Blue for substitutions
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

    embed.add_field(
        name="Updated Team",
        value=", ".join([p.get('mention', p.get('name')) for p in (team1 if team_num == 1 else team2)]),
        inline=False
    )

    embed.set_footer(
        text=f"Substitution by {interaction.user.display_name} | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    await interaction.response.send_message(embed=embed)


async def handle_swap(interaction, match_id, match, player1, player2):
    """Handle player swap between teams"""

    # Get player data
    player1_id = str(player1.id)
    player2_id = str(player2.id)
    player1_mention = player1.mention
    player2_mention = player2.mention

    # Get teams
    team1 = match.get("team1", [])
    team2 = match.get("team2", [])

    # Find both players in the match
    player1_team = None
    player1_index = -1
    player2_team = None
    player2_index = -1

    # Check team1
    for i, player in enumerate(team1):
        if player.get("id") == player1_id:
            player1_team = 1
            player1_index = i
        elif player.get("id") == player2_id:
            player2_team = 1
            player2_index = i

    # Check team2
    for i, player in enumerate(team2):
        if player.get("id") == player1_id:
            player1_team = 2
            player1_index = i
        elif player.get("id") == player2_id:
            player2_team = 2
            player2_index = i

    # Validate both players are in the match
    if player1_team is None:
        await interaction.response.send_message(f"{player1_mention} is not in match `{match_id}`.", ephemeral=True)
        return

    if player2_team is None:
        await interaction.response.send_message(f"{player2_mention} is not in match `{match_id}`.", ephemeral=True)
        return

    # Check they're on different teams
    if player1_team == player2_team:
        await interaction.response.send_message(
            f"Both {player1_mention} and {player2_mention} are on Team {player1_team}. Can only swap players between different teams.",
            ephemeral=True
        )
        return

    # Perform the swap
    if player1_team == 1:  # player1 on team1, player2 on team2
        player1_data = team1[player1_index]
        player2_data = team2[player2_index]

        team1[player1_index] = player2_data
        team2[player2_index] = player1_data

        swap_description = f"{player1_mention} (Team 1 → Team 2) ↔ {player2_mention} (Team 2 → Team 1)"
    else:  # player1 on team2, player2 on team1
        player1_data = team2[player1_index]
        player2_data = team1[player2_index]

        team2[player1_index] = player2_data
        team1[player2_index] = player1_data

        swap_description = f"{player1_mention} (Team 2 → Team 1) ↔ {player2_mention} (Team 1 → Team 2)"

    # Update database
    system_coordinator.match_system.matches.update_one(
        {"match_id": match_id},
        {"$set": {"team1": team1, "team2": team2}}
    )

    # Update in memory
    if match_id in system_coordinator.queue_manager.active_matches:
        system_coordinator.queue_manager.active_matches[match_id]["team1"] = team1
        system_coordinator.queue_manager.active_matches[match_id]["team2"] = team2

    # Create response embed
    embed = discord.Embed(
        title="Players Swapped Between Teams",
        description=f"Match ID: `{match_id}`",
        color=0xff9500  # Orange for swaps
    )

    embed.add_field(
        name="Team Swap",
        value=swap_description,
        inline=False
    )

    embed.add_field(
        name="Updated Teams",
        value=(
            f"**Team 1:** {', '.join([p.get('mention', p.get('name')) for p in team1])}\n"
            f"**Team 2:** {', '.join([p.get('mention', p.get('name')) for p in team2])}"
        ),
        inline=False
    )

    embed.set_footer(
        text=f"Swap by {interaction.user.display_name} | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="streak", description="Check your current streak or another player's streak")
@app_commands.describe(member="The member whose streak you want to check (optional)")
async def streak_slash(interaction: discord.Interaction, member: discord.Member = None):
    if RESET_IN_PROGRESS:
        duration = ""
        if RESET_START_TIME:
            elapsed = datetime.datetime.now() - RESET_START_TIME
            duration = f" (Running for {elapsed.seconds // 60}m {elapsed.seconds % 60}s)"

        embed = discord.Embed(
            title="⛔ Streak Stats Unavailable",
            description=f"A leaderboard reset is currently in progress{duration}",
            color=0xff9900
        )
        embed.add_field(
            name="Please Wait",
            value="• Reset operations are running\n• Streak data is being updated\n• Streak information will be available after reset",
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

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

    # SAFE: Add player avatar with error handling
    try:
        embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
    except:
        pass  # Skip thumbnail if there's an error

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
    try:
        # Start the keepalive server first
        start_keepalive_server()

        print("🚀 Starting Discord bot with cloud platform support...")

        # Print platform detection info
        platform_info = get_platform_info()
        print(f"Platform Detection: {platform_info}")

        if is_cloud_platform():
            print("🌐 Cloud platform detected - using enhanced rate limiting")
        else:
            print("💻 Local development detected - using standard settings")

        # Add startup delay for cloud platforms
        if is_cloud_platform():
            import time

            time.sleep(10)  # 10 second delay before connecting
            print("⏳ Cloud startup delay completed")

        # Then run the bot
        bot.run(TOKEN, log_handler=handler, log_level=logging.WARNING)

    except Exception as startup_error:
        print(f"❌ Critical startup error: {startup_error}")
        import traceback

        traceback.print_exc()

    except Exception as startup_error:
        print(f"❌ Critical startup error: {startup_error}")
        import traceback

        traceback.print_exc()
