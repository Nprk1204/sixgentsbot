import math
import discord
import datetime
import uuid
import asyncio
import random
from rate_limiter import DiscordRateLimiter, ultra_safe_role_operation


class MatchSystem:
    def __init__(self, db, queue_manager=None):
        self.db = db
        self.matches = db.get_collection('matches')
        self.players = db.get_collection('players')
        self.queue_manager = queue_manager
        self.bot = None
        self.rate_limiter = None
        self.bulk_role_manager = None

        # Tier-based MMR values
        self.TIER_MMR = {
            "Rank A": 1850,
            "Rank B": 1350,
            "Rank C": 600
        }

        # Rank boundaries for protection system
        self.RANK_BOUNDARIES = {
            "Rank C": {"min": 0, "max": 1099},
            "Rank B": {"min": 1100, "max": 1599},
            "Rank A": {"min": 1600, "max": 9999}
        }

    def set_bot(self, bot):
        """Set the bot instance"""
        self.bot = bot

    def set_rate_limiter(self, rate_limiter):
        """Set the rate limiter instance"""
        self.rate_limiter = rate_limiter

    def set_bulk_role_manager(self, bulk_role_manager):
        """Set the bulk role manager instance"""
        self.bulk_role_manager = bulk_role_manager

    def set_queue_manager(self, queue_manager):
        """Set the queue manager reference"""
        self.queue_manager = queue_manager

    def is_dummy_player(self, player_id):
        """Check if a player ID belongs to a dummy/test player"""
        return str(player_id).startswith('9000')

    def is_real_player(self, player_id):
        """Check if a player ID belongs to a real Discord user"""
        return not str(player_id).startswith('9000')

    async def update_discord_role_with_queue(self, ctx, player_id, new_mmr, old_mmr=None, immediate_announcement=True):
        """
        Queue a role update for 3am processing while sending immediate promotion feedback

        Args:
            ctx: Discord context (can be interaction or regular context)
            player_id: Player's Discord ID
            new_mmr: New MMR value
            old_mmr: Previous MMR value (for promotion detection)
            immediate_announcement: Whether to send rank change message immediately
        """
        try:
            # Skip dummy players
            if self.is_dummy_player(player_id):
                print(f"Skipping role update queue for dummy player {player_id}")
                return

            # SAFETY: Check if bulk_role_manager is available
            if not self.bulk_role_manager:
                print(f"⚠️ No bulk role manager available - skipping role queue for {player_id}")
                return

            # Get guild and channel from context with enhanced safety
            guild = None
            channel = None

            try:
                # Handle different context types
                if hasattr(ctx, 'guild') and ctx.guild:
                    guild = ctx.guild
                    channel = getattr(ctx, 'channel', None)
                elif hasattr(ctx, 'interaction'):
                    guild = ctx.interaction.guild
                    channel = ctx.interaction.channel
                else:
                    print(f"⚠️ Could not determine guild from context type: {type(ctx)}")
                    return

                if not guild:
                    print(f"⚠️ Guild is None in context")
                    return

                if not channel:
                    print(f"⚠️ Channel is None in context")
                    return

            except Exception as ctx_error:
                print(f"❌ Error extracting guild from context: {ctx_error}")
                return

            # Calculate rank change
            if old_mmr is None:
                print(f"⚠️ No old_mmr provided for player {player_id}, trying to get from database")
                # Try to get old MMR from player data
                player_data = self.players.find_one({"id": player_id})
                if player_data:
                    old_mmr = player_data.get("mmr", 600)
                else:
                    old_mmr = 600  # Default fallback

            old_rank = self.get_rank_tier_from_mmr(old_mmr)
            new_rank = self.get_rank_tier_from_mmr(new_mmr)

            print(f"🔍 Player {player_id}: {old_mmr} MMR ({old_rank}) → {new_mmr} MMR ({new_rank})")

            # Check if this is a promotion
            promotion = False
            demotion = False
            if old_rank and new_rank != old_rank:
                old_rank_value = {"Rank C": 1, "Rank B": 2, "Rank A": 3}.get(old_rank, 1)
                new_rank_value = {"Rank C": 1, "Rank B": 2, "Rank A": 3}.get(new_rank, 1)
                promotion = new_rank_value > old_rank_value
                demotion = new_rank_value < old_rank_value

                if promotion:
                    print(f"🎉 Promotion detected for player {player_id}: {old_rank} → {new_rank}")
                elif demotion:
                    print(f"📉 Demotion detected for player {player_id}: {old_rank} → {new_rank}")

            # Queue the role update for 3am
            success = self.bulk_role_manager.queue_role_update(
                player_id=player_id,
                guild_id=str(guild.id),
                new_mmr=new_mmr,
                old_rank=old_rank,
                new_rank=new_rank,
                promotion=promotion
            )

            if success:
                print(f"✅ Queued role update for {player_id}: {old_rank} → {new_rank} (MMR: {new_mmr})")
            else:
                print(f"❌ Failed to queue role update for {player_id}")

            # FIXED: Send immediate announcement for ANY rank change (promotion or demotion)
            if immediate_announcement and (promotion or demotion) and old_rank and new_rank and channel:
                try:
                    print(f"📢 Sending immediate rank change message for player {player_id}")

                    # Fetch member for mention with safety checks
                    member = None
                    try:
                        if self.rate_limiter:
                            await asyncio.sleep(random.uniform(1.0, 2.0))
                            member = await self.rate_limiter.fetch_member_with_limit(guild, int(player_id))
                        else:
                            await asyncio.sleep(random.uniform(2.0, 4.0))
                            member = await guild.fetch_member(int(player_id))
                    except Exception as member_error:
                        print(f"⚠️ Could not fetch member {player_id}: {member_error}")
                        return

                    if member:
                        # FIXED: Send the actual promotion/demotion message
                        await self.send_immediate_rank_change_message(
                            channel, member, old_rank, new_rank, new_mmr, old_mmr, promotion
                        )
                    else:
                        print(f"⚠️ Could not find member {player_id} to send rank change message")

                except Exception as e:
                    print(f"⚠️ Could not send immediate rank change announcement: {e}")

        except Exception as e:
            print(f"❌ Error in update_discord_role_with_queue: {e}")
            import traceback
            traceback.print_exc()
            # Don't let role update errors break the match reporting process

    async def send_immediate_rank_change_message(self, channel, member, old_rank, new_rank, new_mmr, old_mmr,
                                                 is_promotion):
        """Send immediate rank change message with enhanced formatting"""
        try:
            # Determine message type and color
            if is_promotion:
                title = "🎉 RANK PROMOTION!"
                description = f"Congratulations {member.mention}! You've been promoted!"
                color = 0x00ff00  # Green
            else:
                title = "📉 Rank Change"
                description = f"{member.mention}, your rank has changed."
                color = 0xff9900  # Orange

            # Create embed
            embed = discord.Embed(
                title=title,
                description=description,
                color=color
            )

            # Add rank change info
            embed.add_field(
                name="🔄 Rank Change",
                value=f"**{old_rank}** → **{new_rank}**",
                inline=True
            )

            embed.add_field(
                name="📊 MMR Change",
                value=f"{old_mmr} → **{new_mmr}** ({new_mmr - old_mmr:+d})",
                inline=True
            )

            embed.add_field(
                name="👑 Discord Role",
                value="Will be updated at 3:00 AM",
                inline=True
            )

            # Add motivational message based on change type and new rank
            if is_promotion:
                if new_rank == "Rank A":
                    embed.add_field(
                        name="🏆 Achievement Unlocked",
                        value="You've reached the highest rank! Elite tier achieved!",
                        inline=False
                    )
                elif new_rank == "Rank B":
                    embed.add_field(
                        name="📈 Great Progress",
                        value="You're climbing the ranks! Rank A is within reach!",
                        inline=False
                    )
                else:
                    embed.add_field(
                        name="🚀 Keep Going",
                        value="Great improvement! Keep playing to climb higher!",
                        inline=False
                    )
            else:
                # Demotion - be encouraging
                embed.add_field(
                    name="💪 Stay Strong",
                    value="Every setback is a setup for a comeback! You've got this!",
                    inline=False
                )

            # Add protection info if applicable
            if is_promotion:
                embed.add_field(
                    name="🛡️ Promotion Protection",
                    value="Next 3 games: 50% loss reduction",
                    inline=False
                )

            embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
            embed.set_footer(text="Role update scheduled for 3:00 AM daily")
            embed.timestamp = datetime.datetime.utcnow()

            # Send with rate limiting
            if self.rate_limiter:
                await self.rate_limiter.send_message_with_limit(channel, embed=embed)
                print(f"✅ Sent immediate rank change message for {member.display_name}: {old_rank} → {new_rank}")
            else:
                await asyncio.sleep(random.uniform(1.0, 2.0))
                await channel.send(embed=embed)
                print(f"✅ Sent rank change message for {member.display_name}: {old_rank} → {new_rank}")

        except Exception as e:
            print(f"❌ Error sending rank change message for {member.display_name}: {e}")
            # Don't let this error break the match reporting process

    async def update_discord_role_ultra_safe(self, ctx, player_id, new_mmr):
        """ULTRA-SAFE Discord role update method with extreme rate limiting protection"""
        try:
            # CRITICAL: Triple-check this is not a dummy player
            if self.is_dummy_player(player_id):
                print(f"🚨 SAFETY CHECK: Attempted to update role for dummy player {player_id} - BLOCKED")
                return

            # Skip if no rate limiter is available
            if not self.rate_limiter:
                print(f"⚠️ No rate limiter available - skipping role update for player {player_id}")
                return

            print(f"🔄 Starting ULTRA-SAFE role update for player {player_id} (MMR: {new_mmr})")

            # Define MMR thresholds for ranks
            RANK_A_THRESHOLD = 1600
            RANK_B_THRESHOLD = 1100

            # ULTRA-SAFE member fetching with extensive delays and retries
            member = None
            max_retries = 3  # Reduced retries to prevent hammering

            for attempt in range(max_retries):
                try:
                    print(f"🔍 Attempt {attempt + 1}/{max_retries}: Fetching member {player_id}")

                    # Pre-fetch delay that increases with each attempt
                    delay = random.uniform(3.0, 5.0) * (attempt + 1)
                    await asyncio.sleep(delay)

                    member = await self.rate_limiter.fetch_member_with_limit(ctx.guild, int(player_id))

                    if member:
                        print(f"✅ Successfully fetched member: {member.display_name}")
                        break

                except discord.HTTPException as e:
                    if e.status == 429:
                        wait_time = random.uniform(20.0, 30.0) * (attempt + 1)  # 20-30s, escalating
                        print(f"⚠️ Rate limited on attempt {attempt + 1}, waiting {wait_time:.1f}s")
                        await asyncio.sleep(wait_time)
                        continue
                    elif e.status == 404:
                        print(f"❌ Member {player_id} not found - user may have left server")
                        return
                    elif e.status == 403:
                        print(f"❌ No permission to fetch member {player_id}")
                        return
                    else:
                        print(f"❌ HTTP error fetching member {player_id}: {e}")
                        if attempt == max_retries - 1:
                            return
                        await asyncio.sleep(random.uniform(5.0, 10.0))
                except Exception as e:
                    print(f"❌ Unexpected error fetching member {player_id}: {e}")
                    if attempt == max_retries - 1:
                        return
                    await asyncio.sleep(random.uniform(5.0, 10.0))

            if not member:
                print(f"❌ Could not fetch member {player_id} after {max_retries} attempts")
                return

            # Get roles with error protection
            try:
                rank_a_role = discord.utils.get(ctx.guild.roles, name="Rank A")
                rank_b_role = discord.utils.get(ctx.guild.roles, name="Rank B")
                rank_c_role = discord.utils.get(ctx.guild.roles, name="Rank C")
            except Exception as e:
                print(f"❌ Error getting guild roles: {e}")
                return

            if not all([rank_a_role, rank_b_role, rank_c_role]):
                print(f"❌ One or more rank roles not found")
                return

            # Determine new role
            if new_mmr >= RANK_A_THRESHOLD:
                new_role = rank_a_role
            elif new_mmr >= RANK_B_THRESHOLD:
                new_role = rank_b_role
            else:
                new_role = rank_c_role

            # Check current role
            current_rank_role = None
            for role in member.roles:
                if role in [rank_a_role, rank_b_role, rank_c_role]:
                    current_rank_role = role
                    break

            # If no change needed, skip
            if current_rank_role == new_role:
                print(f"ℹ️ No role change needed for {member.display_name} (already has {new_role.name})")
                return

            print(
                f"🔄 Updating role for {member.display_name}: {current_rank_role.name if current_rank_role else 'None'} -> {new_role.name}")

            # ULTRA-SAFE role updates with EXTREME delays
            try:
                # Import the enhanced safe operation function
                from rate_limiter import ultra_safe_role_operation

                # Remove old role if exists
                if current_rank_role:
                    print(f"🗑️ Removing old role: {current_rank_role.name}")
                    success, error = await ultra_safe_role_operation(
                        self.rate_limiter, member, 'remove', current_rank_role,
                        reason="MMR rank update"
                    )

                    if not success:
                        print(f"❌ Failed to remove old role: {error}")
                        return

                    # Long delay between operations
                    await asyncio.sleep(random.uniform(8.0, 12.0))

                # Add new role
                print(f"➕ Adding new role: {new_role.name}")
                success, error = await ultra_safe_role_operation(
                    self.rate_limiter, member, 'add', new_role,
                    reason=f"MMR update: {new_mmr}"
                )

                if success:
                    print(f"✅ Successfully updated role for {member.display_name}")

                    # Handle promotion announcement (with additional safety and delay)
                    if not current_rank_role or (
                            (current_rank_role == rank_c_role and new_role in [rank_b_role, rank_a_role]) or
                            (current_rank_role == rank_b_role and new_role == rank_a_role)
                    ):
                        try:
                            print(f"🎉 Sending promotion message for {member.display_name}")
                            await asyncio.sleep(random.uniform(8.0, 12.0))  # Long delay before promotion message

                            await self.rate_limiter.send_message_with_limit(
                                ctx.channel,
                                f"🎉 Congratulations {member.mention}! You've been promoted to **{new_role.name}**!",
                                max_retries=2
                            )
                        except Exception as msg_error:
                            print(f"⚠️ Could not send promotion message: {msg_error}")
                else:
                    print(f"❌ Failed to add new role: {error}")

            except Exception as role_error:
                print(f"❌ Critical error during role update for {member.display_name}: {role_error}")

            # Final safety delay (longer than before)
            await asyncio.sleep(random.uniform(8.0, 15.0))

        except Exception as e:
            print(f"❌ Critical error in ultra safe role update for {player_id}: {e}")
            await asyncio.sleep(random.uniform(5.0, 10.0))

    async def update_discord_role_safe(self, ctx, player_id, new_mmr):
        """Safe Discord role update with enhanced error handling"""
        try:
            # Skip dummy players
            if self.is_dummy_player(player_id):
                print(f"Skipping role update for dummy player {player_id}")
                return

            # Skip if no rate limiter
            if not self.rate_limiter:
                print(f"No rate limiter available - skipping role update for {player_id}")
                return

            # Define thresholds
            RANK_A_THRESHOLD = 1600
            RANK_B_THRESHOLD = 1100

            # Fetch member safely
            try:
                await asyncio.sleep(random.uniform(1.0, 3.0))  # Random delay
                member = await self.rate_limiter.fetch_member_with_limit(ctx.guild, int(player_id))
            except Exception as e:
                print(f"Could not fetch member {player_id}: {e}")
                return

            if not member:
                print(f"Member {player_id} not found")
                return

            # Get roles
            rank_a_role = discord.utils.get(ctx.guild.roles, name="Rank A")
            rank_b_role = discord.utils.get(ctx.guild.roles, name="Rank B")
            rank_c_role = discord.utils.get(ctx.guild.roles, name="Rank C")

            if not all([rank_a_role, rank_b_role, rank_c_role]):
                print("Could not find all rank roles")
                return

            # Determine new role
            if new_mmr >= RANK_A_THRESHOLD:
                new_role = rank_a_role
            elif new_mmr >= RANK_B_THRESHOLD:
                new_role = rank_b_role
            else:
                new_role = rank_c_role

            # Check current role
            current_rank_role = None
            for role in member.roles:
                if role in [rank_a_role, rank_b_role, rank_c_role]:
                    current_rank_role = role
                    break

            # If no change needed, skip
            if current_rank_role == new_role:
                return

            print(
                f"Updating role for {member.display_name}: {current_rank_role.name if current_rank_role else 'None'} -> {new_role.name}")

            # Remove old role
            if current_rank_role:
                success, error = await ultra_safe_role_operation(
                    self.rate_limiter, member, 'remove', current_rank_role,
                    reason="MMR rank update"
                )
                if not success:
                    print(f"Failed to remove role: {error}")
                    return

                await asyncio.sleep(random.uniform(2.0, 5.0))

            # Add new role
            success, error = await ultra_safe_role_operation(
                self.rate_limiter, member, 'add', new_role,
                reason=f"MMR update: {new_mmr}"
            )

            if success:
                print(f"Successfully updated role for {member.display_name}")
            else:
                print(f"Failed to add role: {error}")

        except Exception as e:
            print(f"Error in safe role update: {e}")

    def create_match(self, match_id, team1, team2, channel_id, is_global=False):
        """Create a completed match entry in the database"""
        print(
            f"MatchSystem.create_match called with match_id: {match_id}, channel_id: {channel_id}, is_global: {is_global}")

        # Generate a shorter match ID if needed
        if not match_id or len(match_id) > 8:
            match_id = str(uuid.uuid4().hex)[:6]
            print(f"Generated new short match ID: {match_id}")

        # Create match data
        match_data = {
            "match_id": match_id,
            "team1": team1,
            "team2": team2,
            "status": "in_progress",
            "winner": None,
            "score": {"team1": 0, "team2": 0},
            "channel_id": channel_id,
            "created_at": datetime.datetime.utcnow(),
            "completed_at": None,
            "reported_by": None,
            "is_global": is_global
        }

        # Check if this match already exists in the database
        existing_match = self.matches.find_one({"match_id": match_id})
        if existing_match:
            print(f"Match {match_id} already exists in database. Updating it.")
            # Update the existing match
            self.matches.update_one(
                {"match_id": match_id},
                {"$set": {
                    "team1": team1,
                    "team2": team2,
                    "status": "in_progress",
                    "is_global": is_global
                }}
            )
        else:
            # Insert as a new match
            print(f"Creating new match in database: {match_id}")
            self.matches.insert_one(match_data)

        print(f"Match {match_id} successfully created/updated in database")
        return match_id

    async def update_discord_role_ultra_safe_fixed(self, guild, player_id, new_mmr):
        """FIXED version that takes guild directly instead of ctx"""
        try:
            # CRITICAL: Triple-check this is not a dummy player
            if self.is_dummy_player(player_id):
                print(f"🚨 SAFETY CHECK: Attempted to update role for dummy player {player_id} - BLOCKED")
                return

            # Skip if no rate limiter is available
            if not self.rate_limiter:
                print(f"⚠️ No rate limiter available - skipping role update for player {player_id}")
                return

            print(f"🔄 Starting ULTRA-SAFE role update for player {player_id} (MMR: {new_mmr})")

            # Define MMR thresholds for ranks
            RANK_A_THRESHOLD = 1600
            RANK_B_THRESHOLD = 1100

            # ULTRA-SAFE member fetching with extensive delays and retries
            member = None
            max_retries = 3  # Reduced retries to prevent hammering

            for attempt in range(max_retries):
                try:
                    print(f"🔍 Attempt {attempt + 1}/{max_retries}: Fetching member {player_id}")

                    # Pre-fetch delay that increases with each attempt
                    delay = random.uniform(3.0, 5.0) * (attempt + 1)
                    await asyncio.sleep(delay)

                    # FIX: Use guild directly instead of ctx.guild
                    member = await self.rate_limiter.fetch_member_with_limit(guild, int(player_id))

                    if member:
                        print(f"✅ Successfully fetched member: {member.display_name}")
                        break

                except discord.HTTPException as e:
                    if e.status == 429:
                        wait_time = random.uniform(20.0, 30.0) * (attempt + 1)  # 20-30s, escalating
                        print(f"⚠️ Rate limited on attempt {attempt + 1}, waiting {wait_time:.1f}s")
                        await asyncio.sleep(wait_time)
                        continue
                    elif e.status == 404:
                        print(f"❌ Member {player_id} not found - user may have left server")
                        return
                    elif e.status == 403:
                        print(f"❌ No permission to fetch member {player_id}")
                        return
                    else:
                        print(f"❌ HTTP error fetching member {player_id}: {e}")
                        if attempt == max_retries - 1:
                            return
                        await asyncio.sleep(random.uniform(5.0, 10.0))
                except Exception as e:
                    print(f"❌ Unexpected error fetching member {player_id}: {e}")
                    if attempt == max_retries - 1:
                        return
                    await asyncio.sleep(random.uniform(5.0, 10.0))

            if not member:
                print(f"❌ Could not fetch member {player_id} after {max_retries} attempts")
                return

            # Get roles with error protection
            try:
                rank_a_role = discord.utils.get(guild.roles, name="Rank A")
                rank_b_role = discord.utils.get(guild.roles, name="Rank B")
                rank_c_role = discord.utils.get(guild.roles, name="Rank C")
            except Exception as e:
                print(f"❌ Error getting guild roles: {e}")
                return

            if not all([rank_a_role, rank_b_role, rank_c_role]):
                print(f"❌ One or more rank roles not found")
                return

            # Determine new role
            if new_mmr >= RANK_A_THRESHOLD:
                new_role = rank_a_role
            elif new_mmr >= RANK_B_THRESHOLD:
                new_role = rank_b_role
            else:
                new_role = rank_c_role

            # Check current role
            current_rank_role = None
            for role in member.roles:
                if role in [rank_a_role, rank_b_role, rank_c_role]:
                    current_rank_role = role
                    break

            # If no change needed, skip
            if current_rank_role == new_role:
                print(f"ℹ️ No role change needed for {member.display_name} (already has {new_role.name})")
                return

            print(
                f"🔄 Updating role for {member.display_name}: {current_rank_role.name if current_rank_role else 'None'} -> {new_role.name}")

            # ULTRA-SAFE role updates with EXTREME delays
            try:
                # Import the enhanced safe operation function
                from rate_limiter import ultra_safe_role_operation

                # Remove old role if exists
                if current_rank_role:
                    print(f"🗑️ Removing old role: {current_rank_role.name}")
                    success, error = await ultra_safe_role_operation(
                        self.rate_limiter, member, 'remove', current_rank_role,
                        reason="MMR rank update"
                    )

                    if not success:
                        print(f"❌ Failed to remove old role: {error}")
                        return

                    # Long delay between operations
                    await asyncio.sleep(random.uniform(8.0, 12.0))

                # Add new role
                print(f"➕ Adding new role: {new_role.name}")
                success, error = await ultra_safe_role_operation(
                    self.rate_limiter, member, 'add', new_role,
                    reason=f"MMR update: {new_mmr}"
                )

                if success:
                    print(f"✅ Successfully updated role for {member.display_name}")
                else:
                    print(f"❌ Failed to add new role: {error}")

            except Exception as role_error:
                print(f"❌ Critical error during role update for {member.display_name}: {role_error}")

            # Final safety delay (longer than before)
            await asyncio.sleep(random.uniform(8.0, 15.0))

        except Exception as e:
            print(f"❌ Critical error in ultra safe role update for {player_id}: {e}")
            await asyncio.sleep(random.uniform(5.0, 10.0))

    def get_active_match_by_channel(self, channel_id):
        """Get active match by channel ID (delegates to queue_manager)"""
        if self.queue_manager:
            return self.queue_manager.get_match_by_channel(channel_id, status="in_progress")
        return None

    async def report_match_by_id(self, match_id, reporter_id, result, ctx=None):
        """Report a match result by match ID and win/loss"""
        # Clean the match ID first (remove any potential long format)
        match_id = match_id.strip()
        if len(match_id) > 8:  # If it's longer than our standard format
            match_id = match_id[:6]  # Take just the first 6 characters

        # Debug print match ID being searched
        print(f"Looking for match with ID: {match_id}")

        # Check if this is an active match in the queue manager
        active_match = None
        if self.queue_manager:
            active_match = self.queue_manager.get_match_by_id(match_id)
            if active_match:
                print(f"Found active match with ID {match_id}")
            else:
                print(f"No active match found with ID {match_id}")

        # If not found in active matches, check the completed matches
        if not active_match:
            completed_match = self.matches.find_one({"match_id": match_id})
            if completed_match:
                print(f"Found match in completed matches collection: {match_id}")

            if not completed_match:
                return None, "No match found with that ID."

            # If match exists but is already completed, return error
            if completed_match.get("status") != "in_progress":
                return None, "This match has already been reported."

            # Use the completed match data
            match = completed_match
        else:
            # Use the active match data
            match = active_match

        # Debug print to troubleshoot
        print(f"Reporting match {match_id}, current status: {match.get('status')}")
        print(f"Reporter ID: {reporter_id}")

        team1 = match.get("team1", [])
        team2 = match.get("team2", [])

        # Check if teams are empty and try to get them from the database
        if (not team1 or not team2) and self.matches is not None:
            print(f"Teams are empty or missing. Looking up match in database: {match_id}")
            db_match = self.matches.find_one({"match_id": match_id})
            if db_match:
                db_team1 = db_match.get("team1", [])
                db_team2 = db_match.get("team2", [])
                if db_team1 and db_team2:
                    print(f"Found match in database with teams. Using that data instead.")
                    team1 = db_team1
                    team2 = db_team2
                    match = db_match

        # Convert IDs to strings for consistent comparison
        team1_ids = [str(p.get("id", "")) for p in team1]
        team2_ids = [str(p.get("id", "")) for p in team2]

        # Debug print team members and their IDs
        print(f"Team 1 IDs: {team1_ids}")
        print(f"Team 2 IDs: {team2_ids}")
        print(f"Checking if reporter ID: {reporter_id} is in either team")

        # Fix: Convert reporter_id to string to ensure consistent comparison
        reporter_id = str(reporter_id)

        # Check both teams for reporter's ID
        reporter_in_team1 = reporter_id in team1_ids
        reporter_in_team2 = reporter_id in team2_ids

        if reporter_in_team1:
            reporter_team = 1
            print(f"Reporter found in team 1")
        elif reporter_in_team2:
            reporter_team = 2
            print(f"Reporter found in team 2")
        else:
            print(f"Reporter {reporter_id} not found in either team")

            # Check if reporter is in player_matches tracking
            if self.queue_manager and reporter_id in self.queue_manager.player_matches:
                player_match_id = self.queue_manager.player_matches[reporter_id]
                if player_match_id == match_id:
                    print(f"Reporter found in player_matches tracking for this match. Allowing report.")
                    # Determine team based on other evidence
                    if len(team1) > 0 and len(team2) > 0:
                        # If there are players in both teams, just assign to team 1 for now
                        reporter_team = 1
                    else:
                        return None, "Match teams are not properly set up. Please contact an admin."
                else:
                    return None, f"You are in a different match (ID: {player_match_id})."
            else:
                # If we got here, the reporter is not found anywhere
                return None, "You must be a player in this match to report results."

        # Determine winner based on reporter's team and their reported result
        if result.lower() == "win":
            winner = reporter_team
        elif result.lower() == "loss":
            winner = 2 if reporter_team == 1 else 1
        else:
            return None, "Invalid result. Please use 'win' or 'loss'."

        # Set scores (simplified to 1-0 or 0-1)
        if winner == 1:
            team1_score = 1
            team2_score = 0
        else:
            team1_score = 0
            team2_score = 1

        # Update match data with completion info
        now = datetime.datetime.utcnow()

        # Update match in the database
        result = self.matches.update_one(
            {"match_id": match_id, "status": "in_progress"},
            {"$set": {
                "status": "completed",
                "winner": winner,
                "score": {"team1": team1_score, "team2": team2_score},
                "completed_at": now,
                "reported_by": reporter_id
            }}
        )

        # If the match update was successful
        if result.modified_count == 0:
            # Double check if it exists but is already completed
            completed_match = self.matches.find_one({"match_id": match_id, "status": "completed"})
            if completed_match:
                return None, "This match has already been reported."
            else:
                return None, "Failed to update match. Please check the match ID."

        # Remove the match from active matches if it exists there
        if self.queue_manager:
            self.queue_manager.remove_match(match_id)

        # Check if this is a global match
        is_global_match = match.get("is_global", False)
        print(f"Match is global: {is_global_match}")

        # Determine winning and losing teams
        if winner == 1:
            winning_team = match.get("team1", [])
            losing_team = match.get("team2", [])
        else:
            winning_team = match.get("team2", [])
            losing_team = match.get("team1", [])

        print(f"Processing MMR updates for {len(winning_team)} winners and {len(losing_team)} losers")

        # Calculate team average MMRs for MMR adjustment calculation
        team1_mmrs = []
        team2_mmrs = []

        # Determine which MMR to use based on match type
        if is_global_match:
            # For global matches, use global MMR for calculations
            for player in match.get("team1", []):
                player_id = player.get("id")
                if player_id and not player_id.startswith('9000'):  # Skip dummy players
                    player_data = self.players.find_one({"id": player_id})
                    if player_data and "global_mmr" in player_data:
                        team1_mmrs.append(player_data.get("global_mmr", 300))
                    else:
                        team1_mmrs.append(300)  # Default global MMR

            for player in match.get("team2", []):
                player_id = player.get("id")
                if player_id and not player_id.startswith('9000'):  # Skip dummy players
                    player_data = self.players.find_one({"id": player_id})
                    if player_data and "global_mmr" in player_data:
                        team2_mmrs.append(player_data.get("global_mmr", 300))
                    else:
                        team2_mmrs.append(300)  # Default global MMR
        else:
            # For ranked matches, use regular MMR for calculations
            for player in match.get("team1", []):
                player_id = player.get("id")
                if player_id and not player_id.startswith('9000'):  # Skip dummy players
                    player_data = self.players.find_one({"id": player_id})
                    if player_data:
                        team1_mmrs.append(player_data.get("mmr", 600))
                    else:
                        # For new players, check rank record or use default
                        rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                        if rank_record:
                            tier = rank_record.get("tier", "Rank C")
                            team1_mmrs.append(self.TIER_MMR.get(tier, 600))
                        else:
                            team1_mmrs.append(600)  # Default MMR

            for player in match.get("team2", []):
                player_id = player.get("id")
                if player_id and not player_id.startswith('9000'):  # Skip dummy players
                    player_data = self.players.find_one({"id": player_id})
                    if player_data:
                        team2_mmrs.append(player_data.get("mmr", 600))
                    else:
                        # For new players, check rank record or use default
                        rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                        if rank_record:
                            tier = rank_record.get("tier", "Rank C")
                            team2_mmrs.append(self.TIER_MMR.get(tier, 600))
                        else:
                            team2_mmrs.append(600)  # Default MMR

        # Calculate average MMRs
        team1_avg_mmr = sum(team1_mmrs) / len(team1_mmrs) if team1_mmrs else 0
        team2_avg_mmr = sum(team2_mmrs) / len(team2_mmrs) if team2_mmrs else 0

        print(f"Team 1 avg MMR: {team1_avg_mmr}")
        print(f"Team 2 avg MMR: {team2_avg_mmr}")

        # Initialize MMR changes list to track all changes
        mmr_changes = []

        # Update MMR for winners
        for player in winning_team:
            player_id = player.get("id")

            # Skip dummy players
            if not player_id or player_id.startswith('9000'):
                continue

            # Determine which team this player is on for average MMR calculations
            is_team1 = player in match.get("team1", [])
            player_team_avg = team1_avg_mmr if is_team1 else team2_avg_mmr
            opponent_avg = team2_avg_mmr if is_team1 else team1_avg_mmr

            # Get player data or create new
            player_data = self.players.find_one({"id": player_id})

            if player_data:
                # Existing player logic
                if is_global_match:
                    # Global match win handling
                    global_matches = player_data.get("global_matches", 0) + 1
                    global_wins = player_data.get("global_wins", 0) + 1
                    old_mmr = player_data.get("global_mmr", 300)

                    # Get current global streak info and update for winner
                    global_current_streak = player_data.get("global_current_streak", 0)
                    new_global_streak = global_current_streak + 1 if global_current_streak >= 0 else 1
                    global_longest_win_streak = max(player_data.get("global_longest_win_streak", 0), new_global_streak)

                    # Calculate MMR gain with enhanced dynamic algorithm
                    mmr_gain = self.calculate_dynamic_mmr(
                        old_mmr,
                        player_team_avg,
                        opponent_avg,
                        global_matches,
                        is_win=True,
                        streak=new_global_streak,
                        player_data=player_data
                    )

                    new_mmr = old_mmr + mmr_gain
                    print(
                        f"Player {player.get('name', 'Unknown')} GLOBAL MMR update: {old_mmr} + {mmr_gain} = {new_mmr}")

                    # Update with ALL global streak fields
                    self.players.update_one(
                        {"id": player_id},
                        {"$set": {
                            "global_mmr": new_mmr,
                            "global_wins": global_wins,
                            "global_matches": global_matches,
                            "global_current_streak": new_global_streak,
                            "global_longest_win_streak": global_longest_win_streak,
                            "global_longest_loss_streak": player_data.get("global_longest_loss_streak", 0),
                            "last_updated": datetime.datetime.utcnow()
                        }}
                    )

                    # Track MMR change for global
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": old_mmr,
                        "new_mmr": new_mmr,
                        "mmr_change": mmr_gain,
                        "is_win": True,
                        "is_global": True,
                        "streak": new_global_streak
                    })
                    print(f"Added global MMR change for {player.get('name', 'Unknown')}: +{mmr_gain}")
                else:
                    # Regular ranked match win handling
                    matches_played = player_data.get("matches", 0) + 1
                    wins = player_data.get("wins", 0) + 1
                    old_mmr = player_data.get("mmr", 600)

                    # Get current streak info and update for winner
                    current_streak = player_data.get("current_streak", 0)
                    new_streak = current_streak + 1 if current_streak >= 0 else 1
                    longest_win_streak = max(player_data.get("longest_win_streak", 0), new_streak)

                    # Calculate MMR gain with enhanced dynamic algorithm
                    mmr_gain = self.calculate_dynamic_mmr(
                        old_mmr,
                        player_team_avg,
                        opponent_avg,
                        matches_played,
                        is_win=True,
                        streak=new_streak,
                        player_data=player_data
                    )

                    new_mmr = old_mmr + mmr_gain
                    print(
                        f"Player {player.get('name', 'Unknown')} RANKED MMR update: {old_mmr} + {mmr_gain} = {new_mmr}")

                    # Check for rank changes and track promotions
                    old_rank_tier = self.get_rank_tier_from_mmr(old_mmr)
                    new_rank_tier = self.get_rank_tier_from_mmr(new_mmr)

                    update_data = {
                        "mmr": new_mmr,
                        "wins": wins,
                        "matches": matches_played,
                        "current_streak": new_streak,
                        "longest_win_streak": longest_win_streak,
                        "longest_loss_streak": player_data.get("longest_loss_streak", 0),
                        "last_updated": datetime.datetime.utcnow()
                    }

                    # Track promotions for rank protection
                    if new_rank_tier != old_rank_tier and new_rank_tier > old_rank_tier:
                        update_data["last_promotion"] = {
                            "matches_at_promotion": matches_played,
                            "promoted_at": datetime.datetime.utcnow(),
                            "from_rank": old_rank_tier,
                            "to_rank": new_rank_tier,
                            "mmr_at_promotion": new_mmr
                        }
                        print(
                            f"🎉 Player {player.get('name', 'Unknown')} promoted from {old_rank_tier} to {new_rank_tier}!")

                    # Update with ALL ranked streak fields
                    self.players.update_one({"id": player_id}, {"$set": update_data})

                    # Track MMR change for ranked
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": old_mmr,
                        "new_mmr": new_mmr,
                        "mmr_change": mmr_gain,
                        "is_win": True,
                        "is_global": False,
                        "streak": new_streak
                    })
                    print(f"Added ranked MMR change for {player.get('name', 'Unknown')}: +{mmr_gain}")
            else:
                # New player logic
                if is_global_match:
                    # New player's first global match - win
                    rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                    starting_global_mmr = 300  # Default global MMR

                    if rank_record and "global_mmr" in rank_record:
                        starting_global_mmr = rank_record.get("global_mmr", 300)

                    # Calculate first win MMR with the enhanced algorithm
                    mmr_gain = self.calculate_dynamic_mmr(
                        starting_global_mmr,
                        player_team_avg,
                        opponent_avg,
                        1,  # First match
                        is_win=True,
                        streak=1,
                        player_data=None  # No existing data for new player
                    )

                    new_global_mmr = starting_global_mmr + mmr_gain
                    print(
                        f"NEW PLAYER {player.get('name', 'Unknown')} FIRST GLOBAL WIN: {starting_global_mmr} + {mmr_gain} = {new_global_mmr}")

                    # Get default ranked MMR from rank verification if available
                    starting_ranked_mmr = 600  # Default ranked MMR
                    if rank_record:
                        tier = rank_record.get("tier", "Rank C")
                        starting_ranked_mmr = self.TIER_MMR.get(tier, 600)

                    # Initialize new global player with ALL streak fields
                    self.players.insert_one({
                        "id": player_id,
                        "name": player.get("name", "Unknown"),
                        "mmr": starting_ranked_mmr,  # Default ranked MMR
                        "global_mmr": new_global_mmr,  # Updated global MMR
                        "wins": 0,
                        "global_wins": 1,
                        "losses": 0,
                        "global_losses": 0,
                        "matches": 0,
                        "global_matches": 1,
                        "current_streak": 0,
                        "longest_win_streak": 0,
                        "longest_loss_streak": 0,
                        "global_current_streak": 1,
                        "global_longest_win_streak": 1,
                        "global_longest_loss_streak": 0,
                        "last_promotion": None,  # Initialize promotion tracking
                        "created_at": datetime.datetime.utcnow(),
                        "last_updated": datetime.datetime.utcnow()
                    })

                    # Track MMR change for global
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": starting_global_mmr,
                        "new_mmr": new_global_mmr,
                        "mmr_change": mmr_gain,
                        "is_win": True,
                        "is_global": True,
                        "streak": 1
                    })
                    print(f"Added new player global MMR change for {player.get('name', 'Unknown')}: +{mmr_gain}")
                else:
                    # New player's first ranked match - win
                    rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                    starting_mmr = 600  # Default MMR

                    if rank_record:
                        tier = rank_record.get("tier", "Rank C")
                        starting_mmr = self.TIER_MMR.get(tier, 600)

                    # Calculate first win MMR with the enhanced algorithm
                    mmr_gain = self.calculate_dynamic_mmr(
                        starting_mmr,
                        player_team_avg,
                        opponent_avg,
                        1,  # First match
                        is_win=True,
                        streak=1,
                        player_data=None  # No existing data for new player
                    )

                    new_mmr = starting_mmr + mmr_gain
                    print(
                        f"NEW PLAYER {player.get('name', 'Unknown')} FIRST RANKED WIN: {starting_mmr} + {mmr_gain} = {new_mmr}")

                    # Initialize new ranked player with ALL streak fields
                    self.players.insert_one({
                        "id": player_id,
                        "name": player.get("name", "Unknown"),
                        "mmr": new_mmr,  # Updated ranked MMR
                        "global_mmr": 300,  # Default global MMR
                        "wins": 1,
                        "global_wins": 0,
                        "losses": 0,
                        "global_losses": 0,
                        "matches": 1,
                        "global_matches": 0,
                        "current_streak": 1,
                        "longest_win_streak": 1,
                        "longest_loss_streak": 0,
                        "global_current_streak": 0,
                        "global_longest_win_streak": 0,
                        "global_longest_loss_streak": 0,
                        "last_promotion": None,  # Initialize promotion tracking
                        "created_at": datetime.datetime.utcnow(),
                        "last_updated": datetime.datetime.utcnow()
                    })

                    # Track MMR change for ranked
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": starting_mmr,
                        "new_mmr": new_mmr,
                        "mmr_change": mmr_gain,
                        "is_win": True,
                        "is_global": False,
                        "streak": 1
                    })
                    print(f"Added new player ranked MMR change for {player.get('name', 'Unknown')}: +{mmr_gain}")

        # Update MMR for losers
        for player in losing_team:
            player_id = player.get("id")

            # Skip dummy players
            if not player_id or player_id.startswith('9000'):
                continue

            # Determine which team this player is on for average MMR calculations
            is_team1 = player in match.get("team1", [])
            player_team_avg = team1_avg_mmr if is_team1 else team2_avg_mmr
            opponent_avg = team2_avg_mmr if is_team1 else team1_avg_mmr

            # Get player data or create new
            player_data = self.players.find_one({"id": player_id})

            if player_data:
                # Existing player logic
                if is_global_match:
                    # Global match loss handling
                    global_matches = player_data.get("global_matches", 0) + 1
                    global_losses = player_data.get("global_losses", 0) + 1
                    old_mmr = player_data.get("global_mmr", 300)

                    # Get current global streak info and update for loser
                    global_current_streak = player_data.get("global_current_streak", 0)
                    new_global_streak = global_current_streak - 1 if global_current_streak <= 0 else -1
                    global_longest_loss_streak = min(player_data.get("global_longest_loss_streak", 0),
                                                     new_global_streak)

                    # Calculate MMR loss with enhanced dynamic algorithm
                    mmr_loss = self.calculate_dynamic_mmr(
                        old_mmr,
                        player_team_avg,
                        opponent_avg,
                        global_matches,
                        is_win=False,
                        streak=new_global_streak,
                        player_data=player_data
                    )

                    new_mmr = max(0, old_mmr - mmr_loss)  # Don't go below 0
                    print(
                        f"Player {player.get('name', 'Unknown')} GLOBAL MMR update: {old_mmr} - {mmr_loss} = {new_mmr}")

                    # Update with ALL global streak fields
                    self.players.update_one(
                        {"id": player_id},
                        {"$set": {
                            "global_mmr": new_mmr,
                            "global_losses": global_losses,
                            "global_matches": global_matches,
                            "global_current_streak": new_global_streak,
                            "global_longest_loss_streak": global_longest_loss_streak,
                            "global_longest_win_streak": player_data.get("global_longest_win_streak", 0),
                            "last_updated": datetime.datetime.utcnow()
                        }}
                    )

                    # Track MMR change for global loss
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": old_mmr,
                        "new_mmr": new_mmr,
                        "mmr_change": -mmr_loss,  # Negative for loss
                        "is_win": False,
                        "is_global": True,
                        "streak": new_global_streak
                    })
                    print(f"Added global MMR change for {player.get('name', 'Unknown')}: -{mmr_loss}")
                else:
                    # Regular ranked match loss handling
                    matches_played = player_data.get("matches", 0) + 1
                    losses = player_data.get("losses", 0) + 1
                    old_mmr = player_data.get("mmr", 600)

                    # Get current streak info and update for loser
                    current_streak = player_data.get("current_streak", 0)
                    new_streak = current_streak - 1 if current_streak <= 0 else -1
                    longest_loss_streak = min(player_data.get("longest_loss_streak", 0), new_streak)

                    # Calculate MMR loss with enhanced dynamic algorithm
                    mmr_loss = self.calculate_dynamic_mmr(
                        old_mmr,
                        player_team_avg,
                        opponent_avg,
                        matches_played,
                        is_win=False,
                        streak=new_streak,
                        player_data=player_data
                    )

                    new_mmr = max(0, old_mmr - mmr_loss)  # Don't go below 0
                    print(
                        f"Player {player.get('name', 'Unknown')} RANKED MMR update: {old_mmr} - {mmr_loss} = {new_mmr}")

                    # Check for rank changes (demotions)
                    old_rank_tier = self.get_rank_tier_from_mmr(old_mmr)
                    new_rank_tier = self.get_rank_tier_from_mmr(new_mmr)

                    update_data = {
                        "mmr": new_mmr,
                        "losses": losses,
                        "matches": matches_played,
                        "current_streak": new_streak,
                        "longest_loss_streak": longest_loss_streak,
                        "longest_win_streak": player_data.get("longest_win_streak", 0),
                        "last_updated": datetime.datetime.utcnow()
                    }

                    # Track demotions (though we don't give protection for demotions currently)
                    if new_rank_tier != old_rank_tier and new_rank_tier < old_rank_tier:
                        print(
                            f"📉 Player {player.get('name', 'Unknown')} demoted from {old_rank_tier} to {new_rank_tier}")

                    # Update with ALL ranked streak fields
                    self.players.update_one({"id": player_id}, {"$set": update_data})

                    # Track MMR change for ranked loss
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": old_mmr,
                        "new_mmr": new_mmr,
                        "mmr_change": -mmr_loss,  # Negative for loss
                        "is_win": False,
                        "is_global": False,
                        "streak": new_streak
                    })
                    print(f"Added ranked MMR change for {player.get('name', 'Unknown')}: -{mmr_loss}")
            else:
                # New player logic for losers
                if is_global_match:
                    # New player's first global match - loss
                    rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                    starting_global_mmr = 300  # Default global MMR

                    if rank_record and "global_mmr" in rank_record:
                        starting_global_mmr = rank_record.get("global_mmr", 300)

                    # Calculate first loss MMR with the enhanced algorithm
                    mmr_loss = self.calculate_dynamic_mmr(
                        starting_global_mmr,
                        player_team_avg,
                        opponent_avg,
                        1,  # First match
                        is_win=False,
                        streak=-1,
                        player_data=None  # No existing data for new player
                    )

                    new_global_mmr = max(0, starting_global_mmr - mmr_loss)
                    print(
                        f"NEW PLAYER {player.get('name', 'Unknown')} FIRST GLOBAL LOSS: {starting_global_mmr} - {mmr_loss} = {new_global_mmr}")

                    # Get default ranked MMR from rank verification if available
                    starting_ranked_mmr = 600  # Default ranked MMR
                    if rank_record:
                        tier = rank_record.get("tier", "Rank C")
                        starting_ranked_mmr = self.TIER_MMR.get(tier, 600)

                    # Initialize new global player with ALL streak fields
                    self.players.insert_one({
                        "id": player_id,
                        "name": player.get("name", "Unknown"),
                        "mmr": starting_ranked_mmr,  # Default ranked MMR
                        "global_mmr": new_global_mmr,  # Updated global MMR
                        "wins": 0,
                        "global_wins": 0,
                        "losses": 0,
                        "global_losses": 1,
                        "matches": 0,
                        "global_matches": 1,
                        "current_streak": 0,
                        "longest_win_streak": 0,
                        "longest_loss_streak": 0,
                        "global_current_streak": -1,
                        "global_longest_win_streak": 0,
                        "global_longest_loss_streak": -1,
                        "last_promotion": None,  # Initialize promotion tracking
                        "created_at": datetime.datetime.utcnow(),
                        "last_updated": datetime.datetime.utcnow()
                    })

                    # Track MMR change for global
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": starting_global_mmr,
                        "new_mmr": new_global_mmr,
                        "mmr_change": -mmr_loss,
                        "is_win": False,
                        "is_global": True,
                        "streak": -1
                    })
                    print(f"Added new player global MMR change for {player.get('name', 'Unknown')}: -{mmr_loss}")
                else:
                    # New player's first ranked match - loss
                    rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                    starting_mmr = 600  # Default MMR

                    if rank_record:
                        tier = rank_record.get("tier", "Rank C")
                        starting_mmr = self.TIER_MMR.get(tier, 600)

                    # Calculate first loss MMR with the enhanced algorithm
                    mmr_loss = self.calculate_dynamic_mmr(
                        starting_mmr,
                        player_team_avg,
                        opponent_avg,
                        1,  # First match
                        is_win=False,
                        streak=-1,
                        player_data=None  # No existing data for new player
                    )

                    new_mmr = max(0, starting_mmr - mmr_loss)
                    print(
                        f"NEW PLAYER {player.get('name', 'Unknown')} FIRST RANKED LOSS: {starting_mmr} - {mmr_loss} = {new_mmr}")

                    # Initialize new ranked player with ALL streak fields
                    self.players.insert_one({
                        "id": player_id,
                        "name": player.get("name", "Unknown"),
                        "mmr": new_mmr,  # Updated ranked MMR
                        "global_mmr": 300,  # Default global MMR
                        "wins": 0,
                        "global_wins": 0,
                        "losses": 1,
                        "global_losses": 0,
                        "matches": 1,
                        "global_matches": 0,
                        "current_streak": -1,
                        "longest_win_streak": 0,
                        "longest_loss_streak": -1,
                        "global_current_streak": 0,
                        "global_longest_win_streak": 0,
                        "global_longest_loss_streak": 0,
                        "last_promotion": None,  # Initialize promotion tracking
                        "created_at": datetime.datetime.utcnow(),
                        "last_updated": datetime.datetime.utcnow()
                    })

                    # Track MMR change for ranked
                    mmr_changes.append({
                        "player_id": player_id,
                        "old_mmr": starting_mmr,
                        "new_mmr": new_mmr,
                        "mmr_change": -mmr_loss,
                        "is_win": False,
                        "is_global": False,
                        "streak": -1
                    })
                    print(f"Added new player ranked MMR change for {player.get('name', 'Unknown')}: -{mmr_loss}")

        # Store the MMR changes in the match document
        print(f"Storing {len(mmr_changes)} MMR changes in match document")
        self.matches.update_one(
            {"match_id": match_id},
            {"$set": {
                "mmr_changes": mmr_changes,
                "team1_avg_mmr": team1_avg_mmr,
                "team2_avg_mmr": team2_avg_mmr
            }}
        )

        print(f"MMR changes stored successfully for match {match_id}")

        # Queue Discord role updates for 3am processing (immediate announcements, delayed role changes)
        if ctx:
            print("Queueing Discord role updates for 3am processing...")

            # Process all players - both winners and losers
            all_players = winning_team + losing_team

            for player in all_players:
                player_id = player.get("id")

                # Skip dummy players completely
                if not player_id or self.is_dummy_player(player_id):
                    print(f"Skipping dummy player role queue: {player.get('name', 'Unknown')} (ID: {player_id})")
                    continue

                # Only process real players for ranked matches (global matches don't affect Discord roles)
                if not is_global_match:
                    # Find the MMR change for this player from our tracked changes
                    old_mmr = None
                    new_mmr = None

                    for mmr_change in mmr_changes:
                        if mmr_change.get("player_id") == player_id:
                            old_mmr = mmr_change.get("old_mmr")
                            new_mmr = mmr_change.get("new_mmr")
                            break

                    if old_mmr is not None and new_mmr is not None:
                        try:
                            # FIXED: Safely get guild from context
                            guild = None
                            if hasattr(ctx, 'guild'):
                                guild = ctx.guild
                            elif hasattr(ctx, 'interaction') and hasattr(ctx.interaction, 'guild'):
                                guild = ctx.interaction.guild

                            if guild:
                                # Queue the role update with old and new MMR for proper promotion detection
                                await self.update_discord_role_with_queue(
                                    ctx, player_id, new_mmr, old_mmr, immediate_announcement=True
                                )
                                print(
                                    f"✅ Queued role update for {player.get('name', 'Unknown')} (MMR: {old_mmr} → {new_mmr})")
                            else:
                                print(
                                    f"⚠️ Could not get guild from context - skipping role queue for {player.get('name', 'Unknown')}")

                        except Exception as role_queue_error:
                            print(
                                f"❌ Error queueing role update for {player.get('name', 'Unknown')}: {role_queue_error}")
                            import traceback
                            traceback.print_exc()
                            # Continue processing other players even if one fails
                    else:
                        print(
                            f"⚠️ Could not find MMR change data for {player.get('name', 'Unknown')} (player_id: {player_id})")
                        # Debug: Print available MMR changes
                        print(f"Available MMR changes: {[change.get('player_id') for change in mmr_changes]}")

            print("✅ All role updates queued for 3am processing")
        else:
            print("ℹ️ No context provided - skipping role update queueing")

        # CRITICAL: Ensure match is removed from queue manager AFTER all processing
        if self.queue_manager:
            self.queue_manager.remove_match(match_id)
            print(f"✅ Match {match_id} removed from active matches")

        # Return a match result object that includes the MMR changes
        match_result = {
            "match_id": match_id,
            "team1": team1,
            "team2": team2,
            "winner": winner,
            "score": {"team1": team1_score, "team2": team2_score},
            "completed_at": now,
            "reported_by": reporter_id,
            "is_global": is_global_match,
            "mmr_changes": mmr_changes,
            "team1_avg_mmr": team1_avg_mmr,
            "team2_avg_mmr": team2_avg_mmr,
            "status": "completed"
        }

        return match_result, None

    async def update_discord_role(self, ctx, player_id, new_mmr):
        """Update a player's Discord role based on their new MMR - ENHANCED RATE LIMITING"""
        try:
            # Skip if no rate limiter is available
            if not self.rate_limiter:
                print("No rate limiter available - skipping Discord role update")
                return

            # Define MMR thresholds for ranks
            RANK_A_THRESHOLD = 1600
            RANK_B_THRESHOLD = 1100

            # ENHANCED: Get the player's Discord member object with comprehensive error handling
            try:
                # Add a delay before fetching member to space out API calls
                await asyncio.sleep(0.5)
                member = await self.rate_limiter.fetch_member_with_limit(ctx.guild, int(player_id))
            except discord.HTTPException as e:
                if e.status == 429:
                    print(f"Rate limited fetching member {player_id} for role update - waiting longer")
                    await asyncio.sleep(5.0)  # Wait 5 seconds for rate limit
                    try:
                        member = await self.rate_limiter.fetch_member_with_limit(ctx.guild, int(player_id))
                    except Exception as retry_error:
                        print(f"Retry failed for member {player_id}: {retry_error}")
                        return
                elif e.status == 404:
                    print(f"Member {player_id} not found - user may have left the server")
                    return
                elif e.status == 403:
                    print(f"No permission to fetch member {player_id}")
                    return
                else:
                    print(f"HTTP error fetching member {player_id}: {e}")
                    return
            except ValueError:
                print(f"Invalid player ID format: {player_id}")
                return
            except Exception as e:
                print(f"Unexpected error fetching member {player_id}: {e}")
                return

            if not member:
                print(f"Could not find Discord member with ID {player_id}")
                return

            # Get the rank roles with error handling
            try:
                rank_a_role = discord.utils.get(ctx.guild.roles, name="Rank A")
                rank_b_role = discord.utils.get(ctx.guild.roles, name="Rank B")
                rank_c_role = discord.utils.get(ctx.guild.roles, name="Rank C")
            except Exception as e:
                print(f"Error getting guild roles: {e}")
                return

            if not rank_a_role or not rank_b_role or not rank_c_role:
                print("Could not find one or more rank roles")
                return

            # Determine which role the player should have based on MMR
            new_role = None
            if new_mmr >= RANK_A_THRESHOLD:
                new_role = rank_a_role
            elif new_mmr >= RANK_B_THRESHOLD:
                new_role = rank_b_role
            else:
                new_role = rank_c_role

            # Check current roles
            current_rank_role = None
            for role in member.roles:
                if role in [rank_a_role, rank_b_role, rank_c_role]:
                    current_rank_role = role
                    break

            # If role hasn't changed, do nothing
            if current_rank_role == new_role:
                return

            # ENHANCED: Update roles using rate limiter with proper delays
            try:
                # Remove current rank role if they have one
                if current_rank_role:
                    await self.rate_limiter.remove_role_with_limit(
                        member, current_rank_role, reason="MMR rank update"
                    )
                    # IMPORTANT: Add delay between role operations
                    await asyncio.sleep(1.0)  # 1 second delay

                # Add the new role
                await self.rate_limiter.add_role_with_limit(
                    member, new_role, reason=f"MMR update: {new_mmr}"
                )

                # Log the role change
                print(
                    f"✅ Updated roles for {member.display_name}: {current_rank_role.name if current_rank_role else 'None'} -> {new_role.name}")

                # ENHANCED: Announce the rank change if it's a promotion (with rate limiting and delay)
                if not current_rank_role or (
                        (current_rank_role == rank_c_role and new_role in [rank_b_role, rank_a_role]) or
                        (current_rank_role == rank_b_role and new_role == rank_a_role)
                ):
                    try:
                        # Add delay before sending promotion message
                        await asyncio.sleep(1.0)
                        # Use rate limiter for message sending too
                        await self.rate_limiter.send_message_with_limit(
                            ctx.channel,
                            f"🎉 Congratulations {member.mention}! You've been promoted to **{new_role.name}**!"
                        )
                    except discord.HTTPException as msg_error:
                        if msg_error.status == 429:
                            print(f"Rate limited announcing promotion for {member.display_name}")
                        else:
                            print(f"Error announcing promotion for {member.display_name}: {msg_error}")
                    except Exception as msg_error:
                        print(f"Unexpected error announcing promotion for {member.display_name}: {msg_error}")

            except discord.HTTPException as role_error:
                if role_error.status == 429:
                    print(f"Rate limited updating roles for {member.display_name} - this operation will be skipped")
                    # Don't retry immediately to avoid further rate limiting
                elif role_error.status == 403:
                    print(f"No permission to update roles for {member.display_name}")
                else:
                    print(f"HTTP error updating roles for {member.display_name}: {role_error}")
            except Exception as role_error:
                print(f"Unexpected error updating roles for {member.display_name}: {role_error}")

            # IMPORTANT: Add delay at the end of each role update to prevent rapid successive calls
            await asyncio.sleep(1.0)

        except Exception as e:
            print(f"Critical error in update_discord_role: {str(e)}")
            # Add delay even on error to prevent rapid retries
            await asyncio.sleep(1.0)

    async def update_discord_role_ultra_safe(self, ctx, player_id, new_mmr):
        """ULTRA-SAFE Discord role update method with extreme rate limiting protection"""
        try:
            # CRITICAL: Triple-check this is not a dummy player
            if self.is_dummy_player(player_id):
                print(f"🚨 SAFETY CHECK: Attempted to update role for dummy player {player_id} - BLOCKED")
                return

            # Skip if no rate limiter is available
            if not self.rate_limiter:
                print(f"⚠️ No rate limiter available - skipping role update for player {player_id}")
                return

            print(f"🔄 Starting ULTRA-SAFE role update for player {player_id} (MMR: {new_mmr})")

            # Define MMR thresholds for ranks
            RANK_A_THRESHOLD = 1600
            RANK_B_THRESHOLD = 1100

            # ULTRA-SAFE member fetching with extensive delays and retries
            member = None
            max_retries = 5
            for attempt in range(max_retries):
                try:
                    print(f"🔍 Attempt {attempt + 1}/{max_retries}: Fetching member {player_id}")

                    # ENHANCED: Pre-fetch delay that increases with each attempt
                    delay = 3.0 * (attempt + 1)  # 3s, 6s, 9s, 12s, 15s
                    await asyncio.sleep(delay)

                    member = await self.rate_limiter.fetch_member_with_limit(ctx.guild, int(player_id))

                    if member:
                        print(f"✅ Successfully fetched member: {member.display_name}")
                        break

                except discord.HTTPException as e:
                    if e.status == 429:
                        wait_time = max(15.0 * (attempt + 1), getattr(e, 'retry_after', 15))  # Minimum 15s, escalating
                        print(f"⚠️ Rate limited on attempt {attempt + 1}, waiting {wait_time}s")
                        await asyncio.sleep(wait_time)
                        continue
                    elif e.status == 404:
                        print(f"❌ Member {player_id} not found - user may have left server")
                        return
                    elif e.status == 403:
                        print(f"❌ No permission to fetch member {player_id}")
                        return
                    else:
                        print(f"❌ HTTP error fetching member {player_id}: {e}")
                        if attempt == max_retries - 1:
                            return
                        await asyncio.sleep(5.0)
                except Exception as e:
                    print(f"❌ Unexpected error fetching member {player_id}: {e}")
                    if attempt == max_retries - 1:
                        return
                    await asyncio.sleep(5.0)

            if not member:
                print(f"❌ Could not fetch member {player_id} after {max_retries} attempts")
                return

            # Get roles with error protection
            try:
                rank_a_role = discord.utils.get(ctx.guild.roles, name="Rank A")
                rank_b_role = discord.utils.get(ctx.guild.roles, name="Rank B")
                rank_c_role = discord.utils.get(ctx.guild.roles, name="Rank C")
            except Exception as e:
                print(f"❌ Error getting guild roles: {e}")
                return

            if not all([rank_a_role, rank_b_role, rank_c_role]):
                print(f"❌ One or more rank roles not found")
                return

            # Determine new role
            if new_mmr >= RANK_A_THRESHOLD:
                new_role = rank_a_role
            elif new_mmr >= RANK_B_THRESHOLD:
                new_role = rank_b_role
            else:
                new_role = rank_c_role

            # Check current role
            current_rank_role = None
            for role in member.roles:
                if role in [rank_a_role, rank_b_role, rank_c_role]:
                    current_rank_role = role
                    break

            # If no change needed, skip
            if current_rank_role == new_role:
                print(f"ℹ️ No role change needed for {member.display_name} (already has {new_role.name})")
                return

            print(
                f"🔄 Updating role for {member.display_name}: {current_rank_role.name if current_rank_role else 'None'} -> {new_role.name}")

            # ULTRA-SAFE role updates with EXTREME delays and multiple approaches
            try:
                role_update_success = False

                # Method 1: Rate limiter approach
                try:
                    # Remove old role with delay
                    if current_rank_role:
                        print(f"🗑️ Removing old role: {current_rank_role.name}")
                        await self.rate_limiter.remove_role_with_limit(
                            member, current_rank_role, reason="MMR rank update"
                        )
                        await asyncio.sleep(5.0)  # 5 second delay after removal

                    # Add new role with delay
                    print(f"➕ Adding new role: {new_role.name}")
                    await self.rate_limiter.add_role_with_limit(
                        member, new_role, reason=f"MMR update: {new_mmr}"
                    )
                    await asyncio.sleep(3.0)  # 3 second delay after addition

                    role_update_success = True
                    print(f"✅ Rate limiter method successful for {member.display_name}")

                except Exception as rl_error:
                    print(f"⚠️ Rate limiter method failed for {member.display_name}: {rl_error}")

                # Method 2: Manual approach with EXTREME delays if rate limiter failed
                if not role_update_success:
                    try:
                        print(f"🔄 Trying manual method with extreme delays...")

                        # Remove old role manually
                        if current_rank_role:
                            await asyncio.sleep(8.0)  # 8 second pre-delay
                            await member.remove_roles(current_rank_role, reason="MMR rank update")
                            await asyncio.sleep(8.0)  # 8 second post-delay

                        # Add new role manually
                        await asyncio.sleep(5.0)  # 5 second between operations
                        await member.add_roles(new_role, reason=f"MMR update: {new_mmr}")
                        await asyncio.sleep(5.0)  # 5 second post-delay

                        role_update_success = True
                        print(f"✅ Manual method successful for {member.display_name}")

                    except discord.HTTPException as e:
                        if e.status == 429:
                            retry_after = max(getattr(e, 'retry_after', 20), 20)  # Minimum 20 second wait
                            print(
                                f"⚠️ Rate limited during manual method, waiting {retry_after}s for {member.display_name}")
                            await asyncio.sleep(retry_after)

                            # Single retry attempt with even longer delays
                            try:
                                if current_rank_role:
                                    await asyncio.sleep(10.0)
                                    await member.remove_roles(current_rank_role, reason="MMR rank update - retry")
                                    await asyncio.sleep(10.0)

                                await asyncio.sleep(10.0)
                                await member.add_roles(new_role, reason=f"MMR update: {new_mmr} - retry")
                                role_update_success = True
                                print(f"✅ Manual retry successful for {member.display_name}")
                            except Exception as retry_error:
                                print(f"❌ Manual retry failed for {member.display_name}: {retry_error}")
                        else:
                            print(f"❌ Manual HTTP error for {member.display_name}: {e}")
                    except Exception as e:
                        print(f"❌ Manual unexpected error for {member.display_name}: {e}")

                if role_update_success:
                    print(f"✅ Successfully updated role for {member.display_name}")

                    # Handle promotion announcement (with additional safety and delay)
                    if not current_rank_role or (
                            (current_rank_role == rank_c_role and new_role in [rank_b_role, rank_a_role]) or
                            (current_rank_role == rank_b_role and new_role == rank_a_role)
                    ):
                        try:
                            print(f"🎉 Sending promotion message for {member.display_name}")
                            await asyncio.sleep(5.0)  # 5 second delay before promotion message
                            await self.rate_limiter.send_message_with_limit(
                                ctx.channel,
                                f"🎉 Congratulations {member.mention}! You've been promoted to **{new_role.name}**!"
                            )
                        except Exception as msg_error:
                            print(f"⚠️ Could not send promotion message: {msg_error}")
                else:
                    print(f"❌ Failed to update role for {member.display_name} - all methods failed")

            except Exception as role_error:
                print(f"❌ Critical error during role update for {member.display_name}: {role_error}")

            # Final safety delay (longer than before)
            await asyncio.sleep(5.0)

        except Exception as e:
            print(f"❌ Critical error in ultra safe role update for {player_id}: {e}")
            await asyncio.sleep(3.0)

    def update_player_mmr(self, winning_team, losing_team, match_id=None):
        """Update MMR for all players in the match with enhanced dynamic MMR changes"""
        # Retrieve match data if match_id is provided
        match = None
        if match_id:
            match = self.matches.find_one({"match_id": match_id})

        # Calculate team average MMRs
        winning_team_mmrs = []
        losing_team_mmrs = []

        # Get MMRs for winning team
        for player in winning_team:
            player_id = player["id"]

            # Check for dummy players with stored MMR
            if player_id.startswith('9000') and "dummy_mmr" in player:
                winning_team_mmrs.append(player["dummy_mmr"])
                continue

            # Skip dummy players without MMR
            if player_id.startswith('9000'):
                continue

            # Get player MMR for real players
            player_data = self.players.find_one({"id": player_id})
            if player_data:
                winning_team_mmrs.append(player_data.get("mmr", 0))
            else:
                # For new players, get MMR from rank verification or use default
                rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                if rank_record:
                    tier = rank_record.get("tier", "Rank C")
                    winning_team_mmrs.append(self.TIER_MMR.get(tier, 600))
                else:
                    # Use tier-based default
                    winning_team_mmrs.append(600)  # Default to Rank C MMR

        # Get MMRs for losing team
        for player in losing_team:
            player_id = player["id"]

            # Check for dummy players with stored MMR
            if player_id.startswith('9000') and "dummy_mmr" in player:
                losing_team_mmrs.append(player["dummy_mmr"])
                continue

            # Skip dummy players without MMR
            if player_id.startswith('9000'):
                continue

            # Get player MMR for real players
            player_data = self.players.find_one({"id": player_id})
            if player_data:
                losing_team_mmrs.append(player_data.get("mmr", 0))
            else:
                # For new players, get MMR from rank verification or use default
                rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})
                if rank_record:
                    tier = rank_record.get("tier", "Rank C")
                    losing_team_mmrs.append(self.TIER_MMR.get(tier, 600))
                else:
                    # Use tier-based default
                    losing_team_mmrs.append(600)  # Default to Rank C MMR

        # Calculate average MMRs for each team
        winning_team_avg_mmr = sum(winning_team_mmrs) / len(winning_team_mmrs) if winning_team_mmrs else 0
        losing_team_avg_mmr = sum(losing_team_mmrs) / len(losing_team_mmrs) if losing_team_mmrs else 0

        print(f"Winning team avg MMR: {winning_team_avg_mmr}")
        print(f"Losing team avg MMR: {losing_team_avg_mmr}")

        # Add tracking for streak changes
        mmr_changes = []

        # Process winners
        for player in winning_team:
            player_id = player["id"]
            # Skip dummy players
            if player_id.startswith('9000'):
                continue

            # Get player data or create new
            player_data = self.players.find_one({"id": player_id})

            if player_data:
                # Existing player logic
                matches_played = player_data.get("matches", 0) + 1
                wins = player_data.get("wins", 0) + 1
                old_mmr = player_data.get("mmr", 600)

                # Get current streak info or initialize
                current_streak = player_data.get("current_streak", 0)
                # Positive number means win streak, negative means loss streak

                # Update streak - player won, so streak increases or resets from negative
                new_streak = current_streak + 1 if current_streak >= 0 else 1
                longest_win_streak = max(player_data.get("longest_win_streak", 0), new_streak)

                # Calculate MMR gain with enhanced algorithm
                mmr_gain = self.calculate_dynamic_mmr(
                    old_mmr,
                    winning_team_avg_mmr,
                    losing_team_avg_mmr,
                    matches_played,
                    is_win=True,
                    streak=new_streak,
                    player_data=player_data
                )

                new_mmr = old_mmr + mmr_gain
                print(f"Player {player['name']} MMR update: {old_mmr} + {mmr_gain} = {new_mmr} (Streak: {new_streak})")

                # Check for rank changes and track promotions
                old_rank_tier = self.get_rank_tier_from_mmr(old_mmr)
                new_rank_tier = self.get_rank_tier_from_mmr(new_mmr)

                update_data = {
                    "mmr": new_mmr,
                    "wins": wins,
                    "matches": matches_played,
                    "current_streak": new_streak,
                    "longest_win_streak": longest_win_streak,
                    "longest_loss_streak": player_data.get("longest_loss_streak", 0),
                    "last_updated": datetime.datetime.utcnow()
                }

                # Track promotions for rank protection
                if new_rank_tier != old_rank_tier and new_rank_tier > old_rank_tier:
                    update_data["last_promotion"] = {
                        "matches_at_promotion": matches_played,
                        "promoted_at": datetime.datetime.utcnow(),
                        "from_rank": old_rank_tier,
                        "to_rank": new_rank_tier,
                        "mmr_at_promotion": new_mmr
                    }
                    print(f"🎉 Player {player['name']} promoted from {old_rank_tier} to {new_rank_tier}!")

                # Update with ALL streak fields for winners
                self.players.update_one({"id": player_id}, {"$set": update_data})

                # Track MMR change
                mmr_changes.append({
                    "player_id": player_id,
                    "old_mmr": old_mmr,
                    "new_mmr": new_mmr,
                    "mmr_change": mmr_gain,
                    "is_win": True,
                    "streak": new_streak
                })
            else:
                # Look up player's rank in ranks collection
                print(f"New player {player['name']} (ID: {player_id}), determining starting MMR")

                # Try to find rank record
                rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})

                # Default values
                starting_mmr = 600  # Default MMR

                if rank_record:
                    print(f"Found rank record: {rank_record}")

                    # Simplified logic - just use tier-based MMR
                    tier = rank_record.get("tier", "Rank C")
                    starting_mmr = self.TIER_MMR.get(tier, 600)
                    print(f"Using tier-based MMR for {tier}: {starting_mmr}")
                else:
                    print(f"No rank record found, using default MMR: {starting_mmr}")

                # Calculate first win MMR with the enhanced algorithm
                mmr_gain = self.calculate_dynamic_mmr(
                    starting_mmr,
                    winning_team_avg_mmr,
                    losing_team_avg_mmr,
                    1,  # First match
                    is_win=True,
                    streak=1,
                    player_data=None  # No existing data for new player
                )

                new_mmr = starting_mmr + mmr_gain
                print(f"NEW PLAYER {player['name']} FIRST WIN: {starting_mmr} + {mmr_gain} = {new_mmr}")

                # Initialize player record with ALL streak information
                self.players.insert_one({
                    "id": player_id,
                    "name": player["name"],
                    "mmr": new_mmr,
                    "global_mmr": 300,  # Default global MMR
                    "wins": 1,
                    "global_wins": 0,
                    "losses": 0,
                    "global_losses": 0,
                    "matches": 1,
                    "global_matches": 0,
                    "current_streak": 1,  # Start with a win streak of 1
                    "longest_win_streak": 1,
                    "longest_loss_streak": 0,
                    "global_current_streak": 0,
                    "global_longest_win_streak": 0,
                    "global_longest_loss_streak": 0,
                    "last_promotion": None,  # Initialize promotion tracking
                    "created_at": datetime.datetime.utcnow(),
                    "last_updated": datetime.datetime.utcnow()
                })

                # Track MMR change for new player
                mmr_changes.append({
                    "player_id": player_id,
                    "old_mmr": starting_mmr,
                    "new_mmr": new_mmr,
                    "mmr_change": mmr_gain,
                    "is_win": True,
                    "streak": 1
                })

        # Process losers with enhanced logic
        for player in losing_team:
            player_id = player["id"]
            # Skip dummy players
            if player_id.startswith('9000'):
                continue

            # Get player data or create new
            player_data = self.players.find_one({"id": player_id})

            if player_data:
                # Update existing player
                matches_played = player_data.get("matches", 0) + 1
                losses = player_data.get("losses", 0) + 1
                old_mmr = player_data.get("mmr", 600)

                # Get current streak info
                current_streak = player_data.get("current_streak", 0)

                # Update streak - player lost, so streak decreases or resets from positive
                new_streak = current_streak - 1 if current_streak <= 0 else -1
                longest_loss_streak = min(player_data.get("longest_loss_streak", 0), new_streak)

                # Calculate MMR loss with enhanced algorithm
                mmr_loss = self.calculate_dynamic_mmr(
                    old_mmr,
                    losing_team_avg_mmr,
                    winning_team_avg_mmr,
                    matches_played,
                    is_win=False,
                    streak=new_streak,
                    player_data=player_data
                )

                new_mmr = max(0, old_mmr - mmr_loss)  # Don't go below 0
                print(f"Player {player['name']} MMR update: {old_mmr} - {mmr_loss} = {new_mmr} (Streak: {new_streak})")

                # Check for rank changes (demotions)
                old_rank_tier = self.get_rank_tier_from_mmr(old_mmr)
                new_rank_tier = self.get_rank_tier_from_mmr(new_mmr)

                update_data = {
                    "mmr": new_mmr,
                    "losses": losses,
                    "matches": matches_played,
                    "current_streak": new_streak,
                    "longest_win_streak": player_data.get("longest_win_streak", 0),
                    "longest_loss_streak": longest_loss_streak,
                    "last_updated": datetime.datetime.utcnow()
                }

                # Track demotions (though we don't give protection for demotions currently)
                if new_rank_tier != old_rank_tier and new_rank_tier < old_rank_tier:
                    print(f"📉 Player {player['name']} demoted from {old_rank_tier} to {new_rank_tier}")

                # Update with ALL streak fields for losers
                self.players.update_one({"id": player_id}, {"$set": update_data})

                # Track MMR change
                mmr_changes.append({
                    "player_id": player_id,
                    "old_mmr": old_mmr,
                    "new_mmr": new_mmr,
                    "mmr_change": -mmr_loss,  # Negative for loss
                    "is_win": False,
                    "streak": new_streak
                })
            else:
                # Look up player's rank in ranks collection
                print(f"New player {player['name']} (ID: {player_id}), determining starting MMR")

                # Try to find rank record
                rank_record = self.db.get_collection('ranks').find_one({"discord_id": player_id})

                # Default values
                starting_mmr = 600  # Default MMR

                if rank_record:
                    print(f"Found rank record: {rank_record}")

                    # Simplified logic - just use tier-based MMR
                    tier = rank_record.get("tier", "Rank C")
                    starting_mmr = self.TIER_MMR.get(tier, 600)
                    print(f"Using tier-based MMR for {tier}: {starting_mmr}")
                else:
                    print(f"No rank record found, using default MMR: {starting_mmr}")

                # Calculate first loss MMR with enhanced algorithm
                mmr_loss = self.calculate_dynamic_mmr(
                    starting_mmr,
                    losing_team_avg_mmr,
                    winning_team_avg_mmr,
                    1,  # First match
                    is_win=False,
                    streak=-1,
                    player_data=None  # No existing data for new player
                )

                new_mmr = max(0, starting_mmr - mmr_loss)  # Don't go below 0
                print(f"NEW PLAYER {player['name']} FIRST LOSS: {starting_mmr} - {mmr_loss} = {new_mmr}")

                # Initialize player record with ALL streak information
                self.players.insert_one({
                    "id": player_id,
                    "name": player["name"],
                    "mmr": new_mmr,
                    "global_mmr": 300,  # Default global MMR
                    "wins": 0,
                    "global_wins": 0,
                    "losses": 1,
                    "global_losses": 0,
                    "matches": 1,
                    "global_matches": 0,
                    "current_streak": -1,  # Start with a loss streak of -1
                    "longest_win_streak": 0,
                    "longest_loss_streak": -1,
                    "global_current_streak": 0,
                    "global_longest_win_streak": 0,
                    "global_longest_loss_streak": 0,
                    "last_promotion": None,  # Initialize promotion tracking
                    "created_at": datetime.datetime.utcnow(),
                    "last_updated": datetime.datetime.utcnow()
                })

                # Track MMR change for new player
                mmr_changes.append({
                    "player_id": player_id,
                    "old_mmr": starting_mmr,
                    "new_mmr": new_mmr,
                    "mmr_change": -mmr_loss,  # Negative for loss
                    "is_win": False,
                    "streak": -1
                })

        # Store the MMR changes and team average MMRs in the match document
        if match_id:
            self.matches.update_one(
                {"match_id": match_id},
                {"$set": {
                    "mmr_changes": mmr_changes,
                    "team1_avg_mmr": winning_team_avg_mmr,
                    "team2_avg_mmr": losing_team_avg_mmr
                }}
            )

            print(f"Stored MMR changes and team averages for match {match_id}")

    def calculate_dynamic_mmr(self, player_mmr, team_avg_mmr, opponent_avg_mmr, matches_played, is_win=True, streak=0,
                              player_data=None):
        """
        ENHANCED Calculate dynamic MMR change based on:
        1. MMR difference between teams
        2. Number of matches played (for decay)
        3. Win/loss streak with 2x multiplier
        4. Momentum system (recent performance)
        5. Rank boundary protection

        Parameters:
        - player_mmr: Current MMR of the player
        - team_avg_mmr: Average MMR of the player's team
        - opponent_avg_mmr: Average MMR of the opposing team
        - matches_played: Number of matches the player has played (including the current one)
        - is_win: True if calculating for a win, False for a loss
        - streak: Current streak value (positive for win streak, negative for loss streak)
        - player_data: Full player data object for momentum and rank protection calculations

        Returns:
        - MMR change amount
        """
        # Base values for MMR changes
        BASE_MMR_CHANGE = 25  # Standard MMR change for evenly matched teams for experienced players

        # First 15 games give higher MMR changes for placement
        FIRST_GAME_WIN = 110  # Base value for first win
        FIRST_GAME_LOSS = 80  # Base value for first loss

        MAX_MMR_CHANGE = 200  # Maximum for extreme cases with multipliers
        MIN_MMR_CHANGE = 15  # Minimum MMR change even after many games

        # Extended placement period to 15 games
        PLACEMENT_GAMES = 15
        DECAY_RATE = 0.1  # Reduced decay rate for longer placement period

        # 2x Streak multiplier settings
        MAX_STREAK_MULTIPLIER = 2.0  # Maximum multiplier for long streaks (100% bonus)
        STREAK_THRESHOLD = 3  # Kicks in after 2 wins/losses
        STREAK_SCALING = 0.1  # 10% per win/loss after threshold

        # Momentum system settings
        MOMENTUM_GAMES = 10  # Look at last 10 games for momentum
        MOMENTUM_THRESHOLD = 0.5  # 50% win rate for momentum bonus
        MOMENTUM_MULTIPLIER = 1.2  # 20% bonus for good momentum

        # Rank boundary protection settings - NOW PROPERLY USED
        RANK_BOUNDARIES = [1100, 1600]  # Rank B and Rank A thresholds
        PROMOTION_PROTECTION_GAMES = 3  # 3 games of protection after ranking up
        DEMOTION_PROTECTION_RANGE = 100  # 50 MMR buffer before demotion penalties kick in

        # Calculate the MMR difference between teams
        mmr_difference = opponent_avg_mmr - team_avg_mmr
        difference_factor = 1 + (mmr_difference / 400)  # More dramatic for underdog victories
        difference_factor = max(0.5, min(1.5, difference_factor))  # Constrain to reasonable range

        # Extended placement period (first 15 games)
        if matches_played <= PLACEMENT_GAMES:
            # Linearly interpolate between first game value and regular base value
            progress = (matches_played - 1) / (PLACEMENT_GAMES - 1)  # 0 for first match, 1 for 15th match

            if is_win:
                base_value = FIRST_GAME_WIN * (1 - progress) + BASE_MMR_CHANGE * progress
            else:
                base_value = FIRST_GAME_LOSS * (1 - progress) + BASE_MMR_CHANGE * progress

            # Apply difference factor
            if is_win:
                base_change = base_value * difference_factor
            else:
                base_change = base_value * (2 - difference_factor)
        else:
            # After placement, use the regular base value
            if is_win:
                base_change = BASE_MMR_CHANGE * difference_factor
            else:
                base_change = BASE_MMR_CHANGE * (2 - difference_factor)

        # Apply decay based on number of matches played after the initial placement games
        if matches_played <= PLACEMENT_GAMES:
            decay_multiplier = 1.0
        else:
            import math
            decay_multiplier = 1.0 * math.exp(-DECAY_RATE * (matches_played - PLACEMENT_GAMES))
            decay_multiplier = max(0.6, decay_multiplier)  # Don't decay below 60%

        # Calculate initial MMR change
        mmr_change = base_change * decay_multiplier

        # 2x Streak multiplier system
        streak_abs = abs(streak)
        if streak_abs >= STREAK_THRESHOLD:
            streak_bonus = min(
                (streak_abs - STREAK_THRESHOLD + 1) * STREAK_SCALING,
                MAX_STREAK_MULTIPLIER - 1.0
            )
            streak_multiplier = 1.0 + streak_bonus

            # Apply streak multiplier for continuing streaks
            if (is_win and streak > 0) or (not is_win and streak < 0):
                mmr_change *= streak_multiplier
                print(f"Streak multiplier applied: {streak_multiplier:.2f}x (Streak: {streak})")

        # Momentum system bonus - NOW PROPERLY IMPLEMENTED
        if player_data and matches_played > MOMENTUM_GAMES:
            momentum_bonus = self.calculate_momentum_bonus_enhanced(player_data, is_win, MOMENTUM_THRESHOLD,
                                                                    MOMENTUM_MULTIPLIER)
            if momentum_bonus > 1.0:
                mmr_change *= momentum_bonus
                print(f"Momentum bonus applied: {momentum_bonus:.2f}x")

        # Rank boundary protection - NOW PROPERLY IMPLEMENTED WITH ALL VARIABLES
        if player_data:
            protection_modifier = self.calculate_rank_protection_enhanced(
                player_data, player_mmr, is_win, matches_played,
                RANK_BOUNDARIES, PROMOTION_PROTECTION_GAMES, DEMOTION_PROTECTION_RANGE
            )
            mmr_change *= protection_modifier
            if protection_modifier != 1.0:
                protection_type = "promotion protection" if protection_modifier < 1.0 else "promotion assistance"
                print(f"Rank boundary {protection_type}: {protection_modifier:.2f}x modifier")

        # Ensure the change is within bounds
        mmr_change = max(MIN_MMR_CHANGE, min(MAX_MMR_CHANGE, mmr_change))

        return round(mmr_change)

    def calculate_momentum_bonus_enhanced(self, player_data, is_win, momentum_threshold, momentum_multiplier):
        """
        Enhanced momentum calculation using the defined constants
        """
        try:
            # Get recent matches for this player
            player_id = player_data.get('id')
            if not player_id:
                return 1.0

            # Look at last 10 completed matches
            recent_matches = list(self.matches.find(
                {"$or": [
                    {"team1.id": player_id},
                    {"team2.id": player_id}
                ], "status": "completed"}
            ).sort("completed_at", -1).limit(10))

            if len(recent_matches) < 5:  # Need at least 5 games for momentum
                return 1.0

            # Calculate win rate in recent matches
            wins = 0
            for match in recent_matches:
                player_won = self.did_player_win_match(match, player_id)
                if player_won:
                    wins += 1

            win_rate = wins / len(recent_matches)

            # Apply momentum bonus using the defined constants
            if win_rate >= (momentum_threshold + 0.2) and is_win:  # 70%+ win rate and currently winning
                return momentum_multiplier  # 20% bonus
            elif win_rate <= (momentum_threshold - 0.2) and not is_win:  # 30% or lower win rate and currently losing
                return 1.1  # 10% penalty reduction (mercy)

            return 1.0

        except Exception as e:
            print(f"Error calculating enhanced momentum bonus: {e}")
            return 1.0

    def calculate_rank_protection_enhanced(self, player_data, current_mmr, is_win, matches_played,
                                           rank_boundaries, promotion_protection_games, demotion_protection_range):
        """
        Enhanced rank protection calculation using all the defined constants
        """
        try:
            # Check if player recently got promoted
            recent_promotion = self.check_recent_promotion_enhanced(player_data, promotion_protection_games)
            if recent_promotion and not is_win:
                games_since_promotion = recent_promotion.get('games_since', 0)
                if games_since_promotion < promotion_protection_games:
                    return 0.5  # 50% loss reduction for recently promoted players

            # Check for demotion protection (close to rank boundary)
            for boundary in rank_boundaries:
                if current_mmr >= boundary:  # Player is above this boundary
                    distance_from_boundary = current_mmr - boundary
                    if distance_from_boundary <= demotion_protection_range and not is_win:
                        # Reduce loss when close to demotion
                        protection_factor = distance_from_boundary / demotion_protection_range
                        return 0.7 + (0.3 * protection_factor)  # 70-100% of normal loss

            # Check for promotion assistance (close to ranking up)
            for boundary in rank_boundaries:
                if current_mmr < boundary:  # Player is below this boundary
                    distance_to_boundary = boundary - current_mmr
                    if distance_to_boundary <= demotion_protection_range and is_win:
                        # Boost gains when close to promotion
                        assistance_factor = (
                                                        demotion_protection_range - distance_to_boundary) / demotion_protection_range
                        return 1.0 + (0.2 * assistance_factor)  # 100-120% of normal gain

            return 1.0

        except Exception as e:
            print(f"Error calculating enhanced rank protection: {e}")
            return 1.0

    def check_recent_promotion_enhanced(self, player_data, promotion_protection_games):
        """
        Enhanced promotion check using the defined constants
        """
        try:
            # Check if player has promotion data
            promotion_data = player_data.get('last_promotion')
            if promotion_data:
                current_matches = player_data.get('matches', 0)
                matches_at_promotion = promotion_data.get('matches_at_promotion', 0)
                games_since = current_matches - matches_at_promotion

                if games_since <= promotion_protection_games:  # Use the constant
                    return {'games_since': games_since}

            return None

        except Exception as e:
            print(f"Error checking enhanced recent promotion: {e}")
            return None

    def did_player_win_match(self, match, player_id):
        """
        Helper function to determine if a player won a specific match
        """
        try:
            # Check which team the player was on
            player_in_team1 = any(p.get("id") == player_id for p in match.get("team1", []))
            winner = match.get("winner")

            if player_in_team1:
                return winner == 1
            else:
                return winner == 2

        except Exception as e:
            print(f"Error checking if player won match: {e}")
            return False

    def get_rank_tier_from_mmr(self, mmr):
        """
        Helper function to determine rank tier from MMR
        """
        if mmr >= 1600:
            return "Rank A"
        elif mmr >= 1100:
            return "Rank B"
        else:
            return "Rank C"

    def get_player_protection_status(self, player_data):
        """
        Get comprehensive protection status for a player
        Returns dict with protection info for display in /rank command
        """
        try:
            status = {
                "has_protection": False,
                "games_left": 0,
                "protection_type": None,
                "momentum_bonus": False,
                "streak_bonus": False,
                "close_to_promotion": False,
                "close_to_demotion": False
            }

            if not player_data:
                return status

            # Check for recent promotion protection
            promotion_data = player_data.get('last_promotion')
            if promotion_data:
                current_matches = player_data.get('matches', 0)
                matches_at_promotion = promotion_data.get('matches_at_promotion', 0)
                games_since = current_matches - matches_at_promotion

                if games_since < 3:
                    status["has_protection"] = True
                    status["games_left"] = 3 - games_since
                    status["protection_type"] = "promotion"

            # Check for momentum bonus eligibility
            if player_data.get('matches', 0) > 10:
                # This would need to query recent matches, simplified for now
                status["momentum_bonus"] = True  # Could be enhanced with actual calculation

            # Check for streak bonus
            current_streak = abs(player_data.get('current_streak', 0))
            if current_streak >= 2:
                status["streak_bonus"] = True

            # Check proximity to rank boundaries
            current_mmr = player_data.get('mmr', 600)

            # Close to promotion
            if 1050 <= current_mmr < 1100 or 1550 <= current_mmr < 1600:
                status["close_to_promotion"] = True

            # Close to demotion
            if 1100 <= current_mmr <= 1150 or 1600 <= current_mmr <= 1650:
                status["close_to_demotion"] = True

            return status

        except Exception as e:
            print(f"Error getting protection status: {e}")
            return status