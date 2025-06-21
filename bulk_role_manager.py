import asyncio
import datetime
import discord
from typing import Dict, List, Set
import random


class BulkRoleManager:
    def __init__(self, db, bot, rate_limiter=None):
        self.db = db
        self.bot = bot
        self.rate_limiter = rate_limiter

        # Database collection for pending role updates
        self.pending_roles = db.get_collection('pending_role_updates')

        # Track if daily task is running
        self.daily_task = None

    def start_daily_role_update_task(self):
        """Start the daily 3am role update task"""
        if self.daily_task and not self.daily_task.done():
            self.daily_task.cancel()

        self.daily_task = self.bot.loop.create_task(self._daily_role_update_loop())
        print("✅ Daily role update task started - will run at 3:00 AM daily")

    async def _daily_role_update_loop(self):
        """Main loop that runs daily at 3am"""
        while True:
            try:
                # Calculate time until next 3am
                now = datetime.datetime.now()
                target_time = now.replace(hour=3, minute=0, second=0, microsecond=0)

                # If it's already past 3am today, target tomorrow's 3am
                if now >= target_time:
                    target_time += datetime.timedelta(days=1)

                sleep_seconds = (target_time - now).total_seconds()
                print(
                    f"💤 Next bulk role update scheduled for {target_time.strftime('%Y-%m-%d %H:%M:%S')} (in {sleep_seconds / 3600:.1f} hours)")

                # Sleep until 3am
                await asyncio.sleep(sleep_seconds)

                # Run the bulk update
                await self.process_all_pending_role_updates()

            except asyncio.CancelledError:
                print("🛑 Daily role update task cancelled")
                break
            except Exception as e:
                print(f"❌ Error in daily role update loop: {e}")
                # Wait 1 hour before trying again if there's an error
                await asyncio.sleep(3600)

    def queue_role_update(self, player_id: str, guild_id: str, new_mmr: int, old_rank: str = None, new_rank: str = None,
                          promotion: bool = False):
        """Queue a role update to be processed at 3am"""
        try:
            # Check if there's already a pending update for this player
            existing = self.pending_roles.find_one({"player_id": player_id, "guild_id": guild_id})

            update_data = {
                "player_id": player_id,
                "guild_id": guild_id,
                "new_mmr": new_mmr,
                "old_rank": old_rank,
                "new_rank": new_rank,
                "promotion": promotion,
                "queued_at": datetime.datetime.utcnow(),
                "processed": False
            }

            if existing:
                # Update existing record with latest MMR/rank info
                self.pending_roles.update_one(
                    {"player_id": player_id, "guild_id": guild_id},
                    {"$set": update_data}
                )
                print(f"📝 Updated pending role update for player {player_id}: {new_mmr} MMR")
            else:
                # Create new pending update
                self.pending_roles.insert_one(update_data)
                print(f"📋 Queued role update for player {player_id}: {new_mmr} MMR")

            return True

        except Exception as e:
            print(f"❌ Error queuing role update: {e}")
            return False

    async def process_all_pending_role_updates(self):
        """Process all pending role updates (runs at 3am)"""
        try:
            print("🚀 Starting bulk role update process at 3:00 AM...")

            # Get all unprocessed role updates
            pending_updates = list(self.pending_roles.find({"processed": False}))

            if not pending_updates:
                print("✅ No pending role updates to process")
                return

            print(f"📊 Processing {len(pending_updates)} pending role updates...")

            # Group by guild for efficiency
            updates_by_guild = {}
            for update in pending_updates:
                guild_id = update["guild_id"]
                if guild_id not in updates_by_guild:
                    updates_by_guild[guild_id] = []
                updates_by_guild[guild_id].append(update)

            total_processed = 0
            total_errors = 0

            # Process each guild's updates
            for guild_id, guild_updates in updates_by_guild.items():
                guild_processed, guild_errors = await self._process_guild_role_updates(guild_id, guild_updates)
                total_processed += guild_processed
                total_errors += guild_errors

                # Long delay between guilds to be extra safe
                if len(updates_by_guild) > 1:
                    await asyncio.sleep(random.uniform(30.0, 60.0))

            print(f"✅ Bulk role update completed: {total_processed} successful, {total_errors} errors")

            # Send completion summary to admin channel if configured
            await self._send_completion_summary(total_processed, total_errors)

        except Exception as e:
            print(f"❌ Critical error in bulk role update: {e}")
            import traceback
            traceback.print_exc()

    async def _process_guild_role_updates(self, guild_id: str, updates: List[Dict]) -> tuple:
        """Process role updates for a specific guild"""
        try:
            guild = self.bot.get_guild(int(guild_id))
            if not guild:
                print(f"❌ Guild {guild_id} not found")
                # Mark all updates as processed with error
                for update in updates:
                    self.pending_roles.update_one(
                        {"_id": update["_id"]},
                        {"$set": {"processed": True, "error": "Guild not found",
                                  "processed_at": datetime.datetime.utcnow()}}
                    )
                return 0, len(updates)

            print(f"🔄 Processing {len(updates)} role updates for guild: {guild.name}")

            # Get rank roles
            rank_roles = {
                "Rank A": discord.utils.get(guild.roles, name="Rank A"),
                "Rank B": discord.utils.get(guild.roles, name="Rank B"),
                "Rank C": discord.utils.get(guild.roles, name="Rank C")
            }

            # Verify all roles exist
            missing_roles = [name for name, role in rank_roles.items() if role is None]
            if missing_roles:
                print(f"❌ Missing roles in {guild.name}: {missing_roles}")
                # Mark updates as processed with error
                for update in updates:
                    self.pending_roles.update_one(
                        {"_id": update["_id"]},
                        {"$set": {"processed": True, "error": f"Missing roles: {missing_roles}",
                                  "processed_at": datetime.datetime.utcnow()}}
                    )
                return 0, len(updates)

            successful = 0
            errors = 0

            # Process each player's role update
            for i, update in enumerate(updates):
                try:
                    player_id = update["player_id"]
                    new_mmr = update["new_mmr"]
                    old_rank = update.get("old_rank")
                    new_rank = update.get("new_rank")
                    promotion = update.get("promotion", False)

                    # Fetch member
                    try:
                        if self.rate_limiter:
                            await asyncio.sleep(random.uniform(3.0, 6.0))
                            member = await self.rate_limiter.fetch_member_with_limit(guild, int(player_id))
                        else:
                            await asyncio.sleep(random.uniform(5.0, 8.0))
                            member = await guild.fetch_member(int(player_id))
                    except discord.NotFound:
                        print(f"⚠️ Member {player_id} not found in {guild.name} - may have left server")
                        self.pending_roles.update_one(
                            {"_id": update["_id"]},
                            {"$set": {"processed": True, "error": "Member not found",
                                      "processed_at": datetime.datetime.utcnow()}}
                        )
                        errors += 1
                        continue
                    except Exception as e:
                        print(f"❌ Error fetching member {player_id}: {e}")
                        self.pending_roles.update_one(
                            {"_id": update["_id"]},
                            {"$set": {"processed": True, "error": str(e), "processed_at": datetime.datetime.utcnow()}}
                        )
                        errors += 1
                        continue

                    # Determine target role based on MMR
                    if new_mmr >= 1600:
                        target_role = rank_roles["Rank A"]
                        target_rank_name = "Rank A"
                    elif new_mmr >= 1100:
                        target_role = rank_roles["Rank B"]
                        target_rank_name = "Rank B"
                    else:
                        target_role = rank_roles["Rank C"]
                        target_rank_name = "Rank C"

                    # Check current rank roles
                    current_rank_roles = [role for role in member.roles if role in rank_roles.values()]

                    # Skip if already has correct role
                    if len(current_rank_roles) == 1 and current_rank_roles[0] == target_role:
                        print(f"✅ {member.display_name} already has correct role ({target_rank_name})")
                        self.pending_roles.update_one(
                            {"_id": update["_id"]},
                            {"$set": {"processed": True, "result": "No change needed",
                                      "processed_at": datetime.datetime.utcnow()}}
                        )
                        successful += 1
                        continue

                    # Remove old rank roles
                    if current_rank_roles:
                        try:
                            if self.rate_limiter:
                                await self.rate_limiter.remove_role_with_limit(
                                    member, *current_rank_roles,
                                    reason="Bulk role update - removing old rank"
                                )
                            else:
                                await asyncio.sleep(random.uniform(3.0, 6.0))
                                await member.remove_roles(*current_rank_roles,
                                                          reason="Bulk role update - removing old rank")

                            await asyncio.sleep(random.uniform(5.0, 8.0))  # Delay between remove and add

                        except Exception as e:
                            print(f"❌ Error removing old roles from {member.display_name}: {e}")
                            self.pending_roles.update_one(
                                {"_id": update["_id"]},
                                {"$set": {"processed": True, "error": f"Failed to remove old roles: {str(e)}",
                                          "processed_at": datetime.datetime.utcnow()}}
                            )
                            errors += 1
                            continue

                    # Add new role
                    try:
                        if self.rate_limiter:
                            await self.rate_limiter.add_role_with_limit(
                                member, target_role,
                                reason=f"Bulk role update - MMR: {new_mmr}"
                            )
                        else:
                            await asyncio.sleep(random.uniform(3.0, 6.0))
                            await member.add_roles(target_role, reason=f"Bulk role update - MMR: {new_mmr}")

                        # Mark as successfully processed
                        result_msg = f"Updated to {target_rank_name}"
                        if promotion:
                            result_msg += " (Promotion)"

                        self.pending_roles.update_one(
                            {"_id": update["_id"]},
                            {"$set": {
                                "processed": True,
                                "result": result_msg,
                                "final_role": target_rank_name,
                                "processed_at": datetime.datetime.utcnow()
                            }}
                        )

                        print(
                            f"✅ Updated {member.display_name}: {old_rank or 'Unknown'} → {target_rank_name} (MMR: {new_mmr})")
                        successful += 1

                    except Exception as e:
                        print(f"❌ Error adding new role to {member.display_name}: {e}")
                        self.pending_roles.update_one(
                            {"_id": update["_id"]},
                            {"$set": {"processed": True, "error": f"Failed to add new role: {str(e)}",
                                      "processed_at": datetime.datetime.utcnow()}}
                        )
                        errors += 1
                        continue

                    # Delay between each player to prevent rate limiting
                    if i < len(updates) - 1:  # Don't delay after the last update
                        await asyncio.sleep(random.uniform(8.0, 15.0))

                except Exception as e:
                    print(f"❌ Unexpected error processing update for player {update.get('player_id', 'unknown')}: {e}")
                    self.pending_roles.update_one(
                        {"_id": update["_id"]},
                        {"$set": {"processed": True, "error": f"Unexpected error: {str(e)}",
                                  "processed_at": datetime.datetime.utcnow()}}
                    )
                    errors += 1

            print(f"📊 Guild {guild.name} completed: {successful} successful, {errors} errors")
            return successful, errors

        except Exception as e:
            print(f"❌ Critical error processing guild {guild_id}: {e}")
            # Mark all updates as processed with error
            for update in updates:
                self.pending_roles.update_one(
                    {"_id": update["_id"]},
                    {"$set": {"processed": True, "error": f"Guild processing error: {str(e)}",
                              "processed_at": datetime.datetime.utcnow()}}
                )
            return 0, len(updates)

    async def _send_completion_summary(self, successful: int, errors: int):
        """Send completion summary to rl-admin channel instead of admin channels"""
        try:
            summary_msg = (
                f"🌅 **Daily Role Update Complete**\n"
                f"✅ **Successful:** {successful}\n"
                f"❌ **Errors:** {errors}\n"
                f"📅 **Completed:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )

            print(summary_msg)

            # Send to rl-admin channel in all guilds
            for guild in self.bot.guilds:
                # Look for rl-admin channel specifically
                role_updates_channel = discord.utils.get(guild.text_channels, name="rl-admin")

                if role_updates_channel:
                    try:
                        embed = discord.Embed(
                            title="🌅 Daily Role Update Complete",
                            description="The automated role update process has finished.",
                            color=0x00ff00 if errors == 0 else 0xffa500,
                            timestamp=datetime.datetime.utcnow()
                        )

                        # Main stats
                        embed.add_field(name="✅ Successful Updates", value=str(successful), inline=True)
                        embed.add_field(name="❌ Failed Updates", value=str(errors), inline=True)
                        embed.add_field(name="📊 Total Processed", value=str(successful + errors), inline=True)

                        # Status indicator
                        if errors == 0:
                            embed.add_field(
                                name="🎉 Status",
                                value="All role updates completed successfully!",
                                inline=False
                            )
                        else:
                            embed.add_field(
                                name="⚠️ Status",
                                value=f"Completed with {errors} errors. Check logs for details.",
                                inline=False
                            )

                            # Add error rate
                            error_rate = (errors / (successful + errors)) * 100 if (successful + errors) > 0 else 0
                            embed.add_field(
                                name="📈 Error Rate",
                                value=f"{error_rate:.1f}%",
                                inline=True
                            )

                        # Add next update info
                        embed.add_field(
                            name="⏰ Next Update",
                            value="Tomorrow at 3:00 AM",
                            inline=True
                        )

                        # Add helpful info
                        embed.add_field(
                            name="ℹ️ Information",
                            value=(
                                "• Role updates happen automatically every day at 3:00 AM\n"
                                "• Only players with MMR changes get role updates\n"
                                "• Use `/checkpending` to see queued updates\n"
                                "• Use `/forceprocess @player` for immediate updates"
                            ),
                            inline=False
                        )

                        embed.set_footer(text="6 Mans Role Update System")

                        await role_updates_channel.send(embed=embed)
                        print(f"✅ Sent role update summary to #{role_updates_channel.name} in {guild.name}")

                    except Exception as e:
                        print(f"❌ Could not send summary to #{role_updates_channel.name} in {guild.name}: {e}")
                else:
                    print(f"⚠️ No #rl-admin channel found in {guild.name}")

                    # Fallback to other admin channels if rl-admin doesn't exist
                    fallback_channels = [
                        discord.utils.get(guild.text_channels, name="admin-logs"),
                        discord.utils.get(guild.text_channels, name="bot-logs"),
                        discord.utils.get(guild.text_channels, name="sixgents")
                    ]

                    for channel in fallback_channels:
                        if channel:
                            try:
                                fallback_embed = discord.Embed(
                                    title="🌅 Daily Role Update Complete",
                                    description=(
                                        f"**Note:** This message should be in #rl-admin channel\n\n"
                                        f"✅ **Successful:** {successful}\n"
                                        f"❌ **Errors:** {errors}\n"
                                        f"📊 **Total:** {successful + errors}"
                                    ),
                                    color=0x00ff00 if errors == 0 else 0xffa500,
                                    timestamp=datetime.datetime.utcnow()
                                )

                                fallback_embed.add_field(
                                    name="💡 Recommendation",
                                    value="Create a #rl-admin channel for these notifications",
                                    inline=False
                                )

                                await channel.send(embed=fallback_embed)
                                print(f"✅ Sent fallback summary to #{channel.name} in {guild.name}")
                                break  # Only send to first available fallback channel
                            except Exception as e:
                                print(f"⚠️ Could not send fallback summary to {channel.name}: {e}")
                                continue

        except Exception as e:
            print(f"❌ Error sending completion summary: {e}")

    def get_pending_updates_count(self) -> int:
        """Get count of pending role updates"""
        try:
            return self.pending_roles.count_documents({"processed": False})
        except Exception as e:
            print(f"❌ Error getting pending updates count: {e}")
            return 0

    def get_player_pending_update(self, player_id: str, guild_id: str) -> dict:
        """Check if a player has a pending role update"""
        try:
            return self.pending_roles.find_one({
                "player_id": player_id,
                "guild_id": guild_id,
                "processed": False
            })
        except Exception as e:
            print(f"❌ Error checking pending update: {e}")
            return None

    async def force_process_player_update(self, player_id: str, guild_id: str) -> bool:
        """Force process a specific player's role update immediately (admin command)"""
        try:
            pending = self.get_player_pending_update(player_id, guild_id)
            if not pending:
                return False

            print(f"🔧 Force processing role update for player {player_id}")

            # Process just this one update
            successful, errors = await self._process_guild_role_updates(guild_id, [pending])

            return successful > 0

        except Exception as e:
            print(f"❌ Error force processing player update: {e}")
            return False