# rate_limiter.py
"""
Advanced Discord rate limiter for the 6 Mans bot.
Handles both individual operations and efficient bulk operations 
while respecting Discord's rate limits.
"""

import asyncio
import time
from typing import List, Dict, Optional, Callable, Any
import discord
from discord.ext import commands
import logging


class DiscordRateLimiter:
    """
    Advanced Discord rate limiter that handles both individual operations
    and efficient bulk operations while respecting Discord's rate limits.
    """

    def __init__(self, bot: commands.Bot = None):
        self.bot = bot

        # Rate limit configurations
        self.rate_limits = {
            'role_modification': {
                'requests_per_second': 5,
                'burst_limit': 10,
                'delay_between_requests': 0.2
            },
            'member_fetch': {
                'requests_per_second': 50,  # Discord allows up to 50/second
                'burst_limit': 100,
                'delay_between_requests': 0.02
            },
            'message_send': {
                'requests_per_second': 10,
                'burst_limit': 20,
                'delay_between_requests': 0.1
            },
            'guild_operations': {
                'requests_per_second': 10,
                'burst_limit': 20,
                'delay_between_requests': 0.1
            }
        }

        # Track rate limit state per operation type
        self.rate_limit_state = {}

        # Bulk operation queue
        self.bulk_queues = {
            'role_removals': [],
            'role_additions': [],
            'member_fetches': []
        }

        # Background task for processing bulk operations
        self.bulk_processor_task = None

    def start_bulk_processor(self):
        """Start the background bulk processor"""
        if self.bulk_processor_task is None or self.bulk_processor_task.done():
            self.bulk_processor_task = asyncio.create_task(self._bulk_processor())

    def stop_bulk_processor(self):
        """Stop the background bulk processor"""
        if self.bulk_processor_task and not self.bulk_processor_task.done():
            self.bulk_processor_task.cancel()

    async def _bulk_processor(self):
        """Background task that processes bulk operations efficiently"""
        while True:
            try:
                # Process role removals in batches
                if self.bulk_queues['role_removals']:
                    await self._process_role_removal_batch()

                # Process role additions in batches
                if self.bulk_queues['role_additions']:
                    await self._process_role_addition_batch()

                # Process member fetches in batches
                if self.bulk_queues['member_fetches']:
                    await self._process_member_fetch_batch()

                # Small delay between batch processing cycles
                await asyncio.sleep(0.1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Error in bulk processor: {e}")
                await asyncio.sleep(1)  # Longer delay on error

    async def _process_role_removal_batch(self):
        """Process a batch of role removals efficiently"""
        batch_size = 5  # Process 5 at a time to respect rate limits
        batch = self.bulk_queues['role_removals'][:batch_size]
        self.bulk_queues['role_removals'] = self.bulk_queues['role_removals'][batch_size:]

        for operation in batch:
            try:
                member = operation['member']
                roles = operation['roles']
                reason = operation.get('reason', 'Bulk role removal')
                callback = operation.get('callback')

                await member.remove_roles(*roles, reason=reason)

                if callback:
                    await callback(member, roles, True, None)

            except Exception as e:
                logging.error(f"Error removing roles from {operation['member']}: {e}")
                if operation.get('callback'):
                    await operation['callback'](operation['member'], operation['roles'], False, e)

            # Rate limit delay
            await asyncio.sleep(self.rate_limits['role_modification']['delay_between_requests'])

    async def _process_role_addition_batch(self):
        """Process a batch of role additions efficiently"""
        batch_size = 5
        batch = self.bulk_queues['role_additions'][:batch_size]
        self.bulk_queues['role_additions'] = self.bulk_queues['role_additions'][batch_size:]

        for operation in batch:
            try:
                member = operation['member']
                roles = operation['roles']
                reason = operation.get('reason', 'Bulk role addition')
                callback = operation.get('callback')

                await member.add_roles(*roles, reason=reason)

                if callback:
                    await callback(member, roles, True, None)

            except Exception as e:
                logging.error(f"Error adding roles to {operation['member']}: {e}")
                if operation.get('callback'):
                    await operation['callback'](operation['member'], operation['roles'], False, e)

            await asyncio.sleep(self.rate_limits['role_modification']['delay_between_requests'])

    async def _process_member_fetch_batch(self):
        """Process a batch of member fetches efficiently"""
        batch_size = 20  # Higher batch size for fetches
        batch = self.bulk_queues['member_fetches'][:batch_size]
        self.bulk_queues['member_fetches'] = self.bulk_queues['member_fetches'][batch_size:]

        for operation in batch:
            try:
                guild = operation['guild']
                user_id = operation['user_id']
                callback = operation.get('callback')

                member = await guild.fetch_member(user_id)

                if callback:
                    await callback(member, True, None)

            except Exception as e:
                logging.error(f"Error fetching member {operation['user_id']}: {e}")
                if operation.get('callback'):
                    await operation['callback'](None, False, e)

            await asyncio.sleep(self.rate_limits['member_fetch']['delay_between_requests'])

    # PUBLIC API METHODS

    async def remove_roles_bulk(self, operations: List[Dict], progress_callback: Optional[Callable] = None):
        """
        Queue bulk role removals for efficient processing

        Args:
            operations: List of dicts with 'member', 'roles', 'reason', 'callback'
            progress_callback: Optional callback for progress updates
        """
        self.bulk_queues['role_removals'].extend(operations)
        self.start_bulk_processor()

        if progress_callback:
            await progress_callback(f"Queued {len(operations)} role removal operations")

    async def add_roles_bulk(self, operations: List[Dict], progress_callback: Optional[Callable] = None):
        """Queue bulk role additions for efficient processing"""
        self.bulk_queues['role_additions'].extend(operations)
        self.start_bulk_processor()

        if progress_callback:
            await progress_callback(f"Queued {len(operations)} role addition operations")

    async def fetch_members_bulk(self, operations: List[Dict], progress_callback: Optional[Callable] = None):
        """Queue bulk member fetches for efficient processing"""
        self.bulk_queues['member_fetches'].extend(operations)
        self.start_bulk_processor()

        if progress_callback:
            await progress_callback(f"Queued {len(operations)} member fetch operations")

    async def remove_role_with_limit(self, member: discord.Member, *roles, reason: str = None):
        """Remove roles with individual rate limiting"""
        await self._rate_limited_operation('role_modification',
                                           member.remove_roles, *roles, reason=reason)

    async def add_role_with_limit(self, member: discord.Member, *roles, reason: str = None):
        """Add roles with individual rate limiting"""
        await self._rate_limited_operation('role_modification',
                                           member.add_roles, *roles, reason=reason)

    async def fetch_member_with_limit(self, guild: discord.Guild, user_id: int):
        """Fetch member with rate limiting"""
        return await self._rate_limited_operation('member_fetch',
                                                  guild.fetch_member, user_id)

    async def send_message_with_limit(self, channel, *args, **kwargs):
        """Send message with rate limiting"""
        return await self._rate_limited_operation('message_send',
                                                  channel.send, *args, **kwargs)

    async def _rate_limited_operation(self, operation_type: str, func: Callable, *args, **kwargs):
        """Execute an operation with rate limiting"""
        # Initialize rate limit state if not exists
        if operation_type not in self.rate_limit_state:
            self.rate_limit_state[operation_type] = {
                'last_request': 0,
                'request_count': 0,
                'reset_time': time.time()
            }

        state = self.rate_limit_state[operation_type]
        config = self.rate_limits[operation_type]

        current_time = time.time()

        # Reset counter if enough time has passed
        if current_time - state['reset_time'] >= 1.0:
            state['request_count'] = 0
            state['reset_time'] = current_time

        # Check if we need to wait
        if state['request_count'] >= config['requests_per_second']:
            wait_time = 1.0 - (current_time - state['reset_time'])
            if wait_time > 0:
                await asyncio.sleep(wait_time)
                # Reset after waiting
                state['request_count'] = 0
                state['reset_time'] = time.time()

        # Ensure minimum delay between requests
        time_since_last = current_time - state['last_request']
        if time_since_last < config['delay_between_requests']:
            await asyncio.sleep(config['delay_between_requests'] - time_since_last)

        # Execute the operation
        try:
            result = await func(*args, **kwargs)
            state['request_count'] += 1
            state['last_request'] = time.time()
            return result
        except discord.HTTPException as e:
            if e.status == 429:  # Rate limited
                retry_after = e.retry_after if hasattr(e, 'retry_after') else 1.0
                logging.warning(f"Rate limited for {operation_type}, waiting {retry_after}s")
                await asyncio.sleep(retry_after)
                # Retry once
                result = await func(*args, **kwargs)
                state['request_count'] += 1
                state['last_request'] = time.time()
                return result
            else:
                raise


class BulkOperationHelper:
    """Helper class for efficient bulk operations in Discord commands"""

    def __init__(self, rate_limiter: DiscordRateLimiter):
        self.rate_limiter = rate_limiter

    async def bulk_role_removal_with_progress(self,
                                              guild: discord.Guild,
                                              role_names: List[str],
                                              interaction: discord.Interaction = None,
                                              progress_messages: bool = True):
        """
        Efficiently remove roles from all members with progress tracking

        Returns:
            Dict with results: {
                'success_count': int,
                'error_count': int, 
                'errors': List[str],
                'members_processed': int
            }
        """
        results = {
            'success_count': 0,
            'error_count': 0,
            'errors': [],
            'members_processed': 0
        }

        # Get all roles to remove
        roles_to_remove = []
        for role_name in role_names:
            role = discord.utils.get(guild.roles, name=role_name)
            if role:
                roles_to_remove.append(role)

        if not roles_to_remove:
            return results

        # Progress tracking
        last_progress_time = time.time()
        progress_interval = 5.0  # Update every 5 seconds

        async def progress_callback(message: str):
            nonlocal last_progress_time
            current_time = time.time()
            if progress_messages and interaction and (current_time - last_progress_time > progress_interval):
                try:
                    await interaction.followup.send(f"📊 Progress: {message}", ephemeral=True)
                    last_progress_time = current_time
                except:
                    pass  # Ignore if we can't send progress updates

        # Collect members who have these roles
        members_with_roles = []

        if progress_messages and interaction:
            await interaction.followup.send("🔍 Scanning server members...", ephemeral=True)

        # Use bulk member fetching if we need to fetch members
        member_count = 0
        for member in guild.members:
            member_roles = [role for role in member.roles if role in roles_to_remove]
            if member_roles:
                members_with_roles.append({
                    'member': member,
                    'roles': member_roles,
                    'reason': 'Bulk role removal operation'
                })
                member_count += 1

            # Periodic progress updates during scanning
            if member_count % 100 == 0 and progress_messages:
                await progress_callback(f"Found {member_count} members with roles to remove...")

        if not members_with_roles:
            if progress_messages and interaction:
                await interaction.followup.send("✅ No members found with specified roles.", ephemeral=True)
            return results

        if progress_messages and interaction:
            await interaction.followup.send(
                f"🚀 Starting bulk role removal for {len(members_with_roles)} members...",
                ephemeral=True
            )

        # Add callback to track results
        async def result_callback(member, roles, success, error):
            if success:
                results['success_count'] += 1
            else:
                results['error_count'] += 1
                results['errors'].append(f"{member.display_name}: {str(error)}")

            results['members_processed'] += 1

            # Progress updates
            if results['members_processed'] % 10 == 0:
                await progress_callback(
                    f"Processed {results['members_processed']}/{len(members_with_roles)} members "
                    f"(✅ {results['success_count']} ❌ {results['error_count']})"
                )

        # Add callbacks to operations
        for operation in members_with_roles:
            operation['callback'] = result_callback

        # Queue the bulk operations
        await self.rate_limiter.remove_roles_bulk(members_with_roles, progress_callback)

        # Wait for completion
        start_time = time.time()
        while results['members_processed'] < len(members_with_roles):
            await asyncio.sleep(0.5)

            # Timeout after 10 minutes
            if time.time() - start_time > 600:
                break

        return results

    async def smart_member_search(self,
                                  guild: discord.Guild,
                                  search_criteria: Dict,
                                  interaction: discord.Interaction = None) -> List[discord.Member]:
        """
        Efficiently search for members based on various criteria

        Args:
            guild: Discord guild
            search_criteria: Dict with keys like 'role_names', 'name_contains', etc.
            interaction: For progress updates

        Returns:
            List of matching members
        """
        matching_members = []

        # Check if we need to use member cache or fetch from API
        if len(guild.members) < guild.member_count:
            # Need to fetch more members
            if interaction:
                await interaction.followup.send("🔄 Loading complete member list...", ephemeral=True)

            try:
                await guild.chunk(cache=True)
            except Exception as e:
                logging.error(f"Error chunking guild: {e}")

        # Apply search criteria
        for member in guild.members:
            matches = True

            # Check role criteria
            if 'role_names' in search_criteria:
                required_roles = search_criteria['role_names']
                member_role_names = [role.name for role in member.roles]
                if not any(role_name in member_role_names for role_name in required_roles):
                    matches = False

            # Check name criteria
            if 'name_contains' in search_criteria and matches:
                name_search = search_criteria['name_contains'].lower()
                if (name_search not in member.display_name.lower() and
                        name_search not in member.name.lower()):
                    matches = False

            # Check bot criteria
            if 'exclude_bots' in search_criteria and search_criteria['exclude_bots'] and matches:
                if member.bot:
                    matches = False

            if matches:
                matching_members.append(member)

        return matching_members


# Helper functions for easy integration
async def safe_role_operation(rate_limiter: DiscordRateLimiter, member: discord.Member, operation: str, *roles,
                              reason: str = None):
    """
    Safely perform role operations with rate limiting

    Args:
        rate_limiter: The rate limiter instance
        member: Discord member
        operation: 'add' or 'remove'
        roles: Roles to add/remove
        reason: Reason for the operation
    """
    try:
        if operation == 'add':
            await rate_limiter.add_role_with_limit(member, *roles, reason=reason)
        elif operation == 'remove':
            await rate_limiter.remove_role_with_limit(member, *roles, reason=reason)
        return True, None
    except Exception as e:
        return False, str(e)


async def send_progress_update(rate_limiter: DiscordRateLimiter, channel, message: str, delay: float = 1.0):
    """Send progress updates with rate limiting to avoid spam"""
    try:
        await rate_limiter.send_message_with_limit(channel, message)
        await asyncio.sleep(delay)  # Additional delay for readability
    except Exception as e:
        logging.error(f"Failed to send progress update: {e}")

    async def handle_rate_limit_with_backoff(self, func, *args, max_retries=3, **kwargs):
        """Handle rate limits with exponential backoff"""
        for attempt in range(max_retries):
            try:
                return await func(*args, **kwargs)
            except discord.HTTPException as e:
                if e.status == 429:  # Rate limited
                    retry_after = getattr(e, 'retry_after', 2 ** attempt)
                    print(f"Rate limited, waiting {retry_after}s (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(retry_after)
                    continue
                elif e.status == 403:  # Forbidden - might be permissions issue
                    print(f"Permission denied for operation: {e}")
                    raise
                else:
                    print(f"HTTP error {e.status}: {e}")
                    raise
            except Exception as e:
                print(f"Unexpected error in rate limit handler: {e}")
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(2 ** attempt)

        raise Exception(f"Failed after {max_retries} attempts")


# 2. Improved role removal function with better error handling
async def safe_bulk_role_removal(guild, role_names, max_concurrent=5, delay_between_batches=2.0):
    """
    Safely remove roles from all members with strict rate limiting
    """
    # Find all roles to remove
    roles_to_remove = [discord.utils.get(guild.roles, name=name) for name in role_names]
    roles_to_remove = [role for role in roles_to_remove if role is not None]

    if not roles_to_remove:
        return {"success": 0, "errors": [], "message": "No roles found to remove"}

    # Find members with these roles
    members_with_roles = []
    for member in guild.members:
        if member.bot:
            continue
        member_roles = [role for role in member.roles if role in roles_to_remove]
        if member_roles:
            members_with_roles.append((member, member_roles))

    if not members_with_roles:
        return {"success": 0, "errors": [], "message": "No members found with specified roles"}

    print(f"Found {len(members_with_roles)} members with roles to remove")

    success_count = 0
    errors = []

    # Process in small batches with delays
    for i in range(0, len(members_with_roles), max_concurrent):
        batch = members_with_roles[i:i + max_concurrent]

        # Process each member in the batch
        batch_tasks = []
        for member, member_roles in batch:
            async def remove_roles_for_member(m, roles):
                try:
                    await m.remove_roles(*roles, reason="Leaderboard reset")
                    return True, None
                except discord.Forbidden:
                    return False, f"No permission to remove roles from {m.display_name}"
                except discord.HTTPException as e:
                    if e.status == 429:
                        # Wait and retry once
                        await asyncio.sleep(getattr(e, 'retry_after', 5))
                        try:
                            await m.remove_roles(*roles, reason="Leaderboard reset - retry")
                            return True, None
                        except Exception as retry_error:
                            return False, f"Rate limited retry failed for {m.display_name}: {str(retry_error)}"
                    else:
                        return False, f"HTTP error for {m.display_name}: {str(e)}"
                except Exception as e:
                    return False, f"Unexpected error for {m.display_name}: {str(e)}"

            batch_tasks.append(remove_roles_for_member(member, member_roles))

        # Wait for all tasks in this batch to complete
        batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

        # Process results
        for result in batch_results:
            if isinstance(result, Exception):
                errors.append(f"Task exception: {str(result)}")
            elif result[0]:  # Success
                success_count += 1
            else:  # Error
                errors.append(result[1])

        # Progress update
        progress = min(i + max_concurrent, len(members_with_roles))
        print(f"Processed {progress}/{len(members_with_roles)} members (✅ {success_count} ❌ {len(errors)})")

        # Delay between batches to respect rate limits
        if i + max_concurrent < len(members_with_roles):
            await asyncio.sleep(delay_between_batches)

    return {
        "success": success_count,
        "errors": errors,
        "message": f"Completed: {success_count} successful, {len(errors)} errors"
    }
