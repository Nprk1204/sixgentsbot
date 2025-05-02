import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os
from database import Database
from queue_handler import QueueHandler
from votesystem import VoteSystem
from captainssystem import CaptainsSystem
from matchsystem import MatchSystem  # Changed from match_system to matchsystem
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
import uuid
import datetime

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
captains_system = CaptainsSystem(db, queue_handler)
vote_system = VoteSystem(db, queue_handler, captains_system)
match_system = MatchSystem(db)

# Connect components
queue_handler.set_vote_system(vote_system)
queue_handler.set_captains_system(captains_system)
captains_system.set_match_system(match_system)  # Set match_system in captains_system
vote_system.set_match_system(match_system)      # Set match_system in vote_system

@bot.event
async def on_ready():
    print(f"{bot.user.name} is now online")
    vote_system.set_bot(bot)


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
    player = ctx.author
    response = queue_handler.add_player(player)
    await ctx.send(response)

    # Check if queue is full and start voting
    players = queue_handler.get_players_for_match()
    if len(players) >= 6 and not vote_system.is_voting_active():
        await vote_system.start_vote(ctx.channel)


@bot.command()
async def leave(ctx):
    """Leave the queue"""
    player = ctx.author
    response = queue_handler.remove_player(player)
    await ctx.send(response)


@bot.command()
async def status(ctx):
    """Shows the current queue status"""
    response = queue_handler.get_queue_status()
    await ctx.send(embed=response)  # Send as embed

    # If queue is full but vote not started, start it
    players = queue_handler.get_players_for_match()
    if len(players) >= 6 and not vote_system.is_voting_active():
        await vote_system.start_vote(ctx.channel)


# Match commands

@bot.command()
async def report(ctx, result: str):
    """Report match results (format: /report <win/loss>)"""
    reporter_id = str(ctx.author.id)
    channel_id = str(ctx.channel.id)

    # Validate result argument
    if result.lower() not in ["win", "loss"]:
        await ctx.send("Invalid result. Please use 'win' or 'loss'.")
        return

    # Find active match in this channel
    active_match = match_system.get_active_match_by_channel(channel_id)

    if not active_match:
        await ctx.send(
            "No active match found in this channel. Please report in the channel where the match was created.")
        return

    match_id = active_match["match_id"]
    match, error = match_system.report_match_by_id(match_id, reporter_id, result)

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
async def leaderboard(ctx, limit: int = 10):
    """Show the top players by MMR"""
    top_players = match_system.get_leaderboard(limit)

    if not top_players:
        await ctx.send("No players found in the leaderboard yet.")
        return

    embed = discord.Embed(
        title="🏆 Leaderboard 🏆",
        description=f"Top {len(top_players)} players by MMR",
        color=0x00aaff
    )

    for i, player in enumerate(top_players):
        medal = ""
        if i == 0:
            medal = "🥇 "
        elif i == 1:
            medal = "🥈 "
        elif i == 2:
            medal = "🥉 "
        else:
            medal = f"{i + 1}. "

        # Try to get member object for mention
        try:
            member = await ctx.guild.fetch_member(int(player["id"]))
            name = member.display_name  # Use display_name instead of mention
        except:
            name = player["name"]  # Fallback to stored name

        # Calculate win rate
        matches = player.get("matches", 0)
        wins = player.get("wins", 0)
        win_rate = 0
        if matches > 0:
            win_rate = (wins / matches) * 100

        value = f"MMR: **{player['mmr']}** | W-L: {wins}-{player.get('losses', 0)} | Win Rate: {win_rate:.1f}%"
        embed.add_field(name=f"{medal}{name}", value=value, inline=False)

    embed.set_footer(text="Updated after each match")
    await ctx.send(embed=embed)


@bot.command()
async def rank(ctx, member: discord.Member = None):
    """Check your rank and stats (or another member's)"""
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

    await ctx.send(embed=embed)


# Admin commands
@bot.command()
async def clearqueue(ctx):
    """Clear all players from the queue (Admin only)"""
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
async def forcestart(ctx):
    """Force start the team selection process (Admin only)"""
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
async def purgechat(ctx, amount_to_delete: int = 10):
    """Clear chat messages"""
    if ctx.author.guild_permissions.manage_messages:
        if 1 <= amount_to_delete <= 100:
            await ctx.channel.purge(limit=amount_to_delete + 1)
            await ctx.send(f"Cleared {amount_to_delete} messages.", delete_after=5)
        else:
            await ctx.send("Please enter a number between 1 and 100")
    else:
        await ctx.send("You don't have permission to use this command.")


@bot.command()
async def resetleaderboard(ctx, confirmation: str = None):
    """Reset the leaderboard (Admin only)"""
    # Check if user has admin permissions
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("You need administrator permissions to use this command.")
        return

    # Require confirmation
    if confirmation is None or confirmation.lower() != "confirm":
        embed = discord.Embed(
            title="⚠️ Reset Leaderboard Confirmation",
            description="This will reset MMR and stats for ALL players. This action cannot be undone!",
            color=0xff9900
        )
        embed.add_field(
            name="To confirm:",
            value="Type `/resetleaderboard confirm`",
            inline=False
        )
        await ctx.send(embed=embed)
        return

    # Get current player count
    player_count = match_system.players.count_documents({})

    if player_count == 0:
        await ctx.send("Leaderboard is already empty!")
        return

    # Create backup (optional)
    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_collection_name = f"players_backup_{timestamp}"

    try:
        # Create backup
        db = match_system.players.database
        db.create_collection(backup_collection_name)
        backup_collection = db[backup_collection_name]

        # Copy data to backup
        for player in match_system.players.find():
            backup_collection.insert_one(player)

        # Delete all player records
        match_system.players.delete_many({})

        # Send confirmation
        embed = discord.Embed(
            title="✅ Leaderboard Reset Complete",
            description=f"Reset {player_count} player records.",
            color=0x00ff00
        )
        embed.add_field(
            name="Backup Created",
            value=f"Backup collection: `{backup_collection_name}`",
            inline=False
        )
        embed.set_footer(text=f"Reset by {ctx.author.display_name}")

        await ctx.send(embed=embed)

    except Exception as e:
        await ctx.send(f"Error resetting leaderboard: {str(e)}")


# Help command
@bot.command()
async def helpme(ctx):
    """Display help information"""
    embed = discord.Embed(
        title="Rocket League 6 Mans Bot",
        description="Commands for the 6 mans queue system:",
        color=0x00ff00
    )

    embed.add_field(name="/join", value="Join the queue", inline=False)
    embed.add_field(name="/leave", value="Leave the queue", inline=False)
    embed.add_field(name="/status", value="Show the current queue status", inline=False)
    embed.add_field(name="/report <team1_score> <team2_score>", value="Report match results", inline=False)
    embed.add_field(name="/leaderboard [limit]", value="Show the leaderboard (default: top 10)", inline=False)
    embed.add_field(name="/rank [member]", value="Show your rank or another member's rank", inline=False)
    embed.add_field(name="/purgechat [number]", value="Clear messages (mod only)", inline=False)

    embed.add_field(
        name="How it works:",
        value=(
            "1. Join the queue with `/join`\n"
            "2. When 6 players join, voting starts automatically\n"
            "3. Vote by reacting to the vote message\n"
            "4. Teams will be created based on the vote results\n"
            "5. After the match, report the results with `/report`\n"
            "6. Check the leaderboard with `/leaderboard`"
        ),
        inline=False
    )

    await ctx.send(embed=embed)


# Error handler
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send("Command not found. Use `/helpme` to see available commands.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Missing required argument. Use `/helpme` to see command usage.")
    else:
        print(f"Error: {error}")


# Run the bot
bot.run(token, log_handler=handler, log_level=logging.DEBUG)