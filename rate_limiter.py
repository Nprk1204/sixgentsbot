"""
Advanced Discord rate limiter for the 6 Mans bot.
Handles both individual operations and efficient bulk operations 
while respecting Discord's rate limits more aggressively.
"""

import asyncio
import time
from typing import List, Dict, Optional, Callable, Any
import discord
from discord.ext import commands
import logging
import random


class DiscordRateLimiter:
    """
    Enhanced Discord rate limiter with aggressive rate limiting protection
    """

    def __init__(self, bot: commands.Bot = None):
        self.bot = bot

        # ENHANCED: More conservative rate limits
        self.rate_limits = {
            'role_modification': {
                'requests_per_second': 2,  # Reduced from 5 to 2
                'burst_limit': 5,  # Reduced from 10 to 5
                'delay_between_requests': 1.0,  # Increased from 0.2 to 1.0
                'backoff_multiplier': 2.0,
                'max_backoff': 30.0
            },
            'member_fetch': {
                'requests_per_second': 10,  # Reduced from 50 to 10
                'burst_limit': 20,  # Reduced from 100 to 20
                'delay_between_requests': 0.2,  # Increased from 0.02 to 0.2
                'backoff_multiplier': 1.5,
                'max_backoff': 15.0
            },
            'message_send': {
                'requests_per_second': 3,  # Reduced from 10 to 3
                'burst_limit': 6,  # Reduced from 20 to 6
                'delay_between_requests': 0.5,  # Increased from 0.1 to 0.5
                'backoff_multiplier': 2.0,
                'max_backoff': 20.0
            },
            'guild_operations': {
                'requests_per_second': 2,  # Reduced from 10 to 2
                'burst_limit': 4,  # Reduced from 20 to 4
                'delay_between_requests': 1.0,  # Increased from 0.1 to 1.0
                'backoff_multiplier': 2.0,
                'max_backoff': 25.0
            }
        }

        # Track rate limit state per operation type
        self.rate_limit_state = {}

        # ENHANCED: Track consecutive failures for backoff
        self.failure_counts = {}
        self.last_failure_times = {}

        # Add jitter to prevent thundering herd
        self.use_jitter = True

    async def remove_role_with_limit(self, member: discord.Member, *roles, reason: str = None, max_retries: int = 5):
        """Remove roles with enhanced rate limiting and retry logic"""
        return await self._enhanced_rate_limited_operation(
            'role_modification',
            member.remove_roles,
            max_retries,
            *roles,
            reason=reason
        )

    async def add_role_with_limit(self, member: discord.Member, *roles, reason: str = None, max_retries: int = 5):
        """Add roles with enhanced rate limiting and retry logic"""
        return await self._enhanced_rate_limited_operation(
            'role_modification',
            member.add_roles,
            max_retries,
            *roles,
            reason=reason
        )

    async def fetch_member_with_limit(self, guild: discord.Guild, user_id: int, max_retries: int = 3):
        """Fetch member with enhanced rate limiting"""
        return await self._enhanced_rate_limited_operation(
            'member_fetch',
            guild.fetch_member,
            max_retries,
            user_id
        )

    async def send_message_with_limit(self, channel, *args, max_retries: int = 3, **kwargs):
        """Send message with enhanced rate limiting"""
        return await self._enhanced_rate_limited_operation(
            'message_send',
            channel.send,
            max_retries,
            *args,
            **kwargs
        )

    async def _enhanced_rate_limited_operation(self, operation_type: str, func: Callable, max_retries: int, *args,
                                               **kwargs):
        """Execute an operation with enhanced rate limiting and exponential backoff"""

        # Initialize tracking if needed
        if operation_type not in self.rate_limit_state:
            self.rate_limit_state[operation_type] = {
                'last_request': 0,
                'request_count': 0,
                'reset_time': time.time()
            }
            self.failure_counts[operation_type] = 0
            self.last_failure_times[operation_type] = 0

        config = self.rate_limits[operation_type]

        # Check if we're in backoff period due to recent failures
        current_time = time.time()
        if self.failure_counts[operation_type] > 0:
            time_since_failure = current_time - self.last_failure_times[operation_type]
            required_backoff = min(
                config['delay_between_requests'] * (
                            config['backoff_multiplier'] ** self.failure_counts[operation_type]),
                config['max_backoff']
            )

            if time_since_failure < required_backoff:
                wait_time = required_backoff - time_since_failure
                if self.use_jitter:
                    wait_time += random.uniform(0, wait_time * 0.1)  # Add up to 10% jitter

                print(
                    f"⏳ Backoff wait for {operation_type}: {wait_time:.2f}s (failure count: {self.failure_counts[operation_type]})")
                await asyncio.sleep(wait_time)

        # Attempt the operation with retries
        for attempt in range(max_retries):
            try:
                # Apply rate limiting before each attempt
                await self._apply_rate_limiting(operation_type)

                # Execute the operation
                result = await func(*args, **kwargs)

                # Success - reset failure count
                self.failure_counts[operation_type] = 0
                self.rate_limit_state[operation_type]['request_count'] += 1
                self.rate_limit_state[operation_type]['last_request'] = time.time()

                return result

            except discord.HTTPException as e:
                if e.status == 429:  # Rate limited
                    self.failure_counts[operation_type] += 1
                    self.last_failure_times[operation_type] = time.time()

                    # Get retry_after from Discord or use exponential backoff
                    retry_after = getattr(e, 'retry_after', None)
                    if retry_after is None:
                        retry_after = min(
                            config['delay_between_requests'] * (2 ** attempt),
                            config['max_backoff']
                        )

                    # Add jitter to prevent synchronized retries
                    if self.use_jitter:
                        jitter = random.uniform(0, retry_after * 0.2)  # Up to 20% jitter
                        retry_after += jitter

                    print(
                        f"🚫 Rate limited ({operation_type}, attempt {attempt + 1}/{max_retries}): waiting {retry_after:.2f}s")

                    if attempt < max_retries - 1:  # Don't wait on last attempt
                        await asyncio.sleep(retry_after)
                        continue
                    else:
                        print(f"❌ {operation_type} failed after {max_retries} attempts due to rate limiting")
                        raise

                elif e.status == 403:  # Forbidden
                    print(f"❌ Permission denied for {operation_type}: {e}")
                    raise

                elif e.status == 404:  # Not found
                    print(f"❌ Resource not found for {operation_type}: {e}")
                    raise

                else:  # Other HTTP errors
                    print(f"❌ HTTP {e.status} error for {operation_type}: {e}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)  # Exponential backoff for other errors
                        continue
                    else:
                        raise

            except Exception as e:
                print(f"❌ Unexpected error in {operation_type} (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 + attempt)  # Linear backoff for unexpected errors
                    continue
                else:
                    raise

        # Should not reach here, but just in case
        raise Exception(f"Failed to complete {operation_type} after {max_retries} attempts")

    async def _apply_rate_limiting(self, operation_type: str):
        """Apply rate limiting before making a request"""
        state = self.rate_limit_state[operation_type]
        config = self.rate_limits[operation_type]
        current_time = time.time()

        # Reset request count if enough time has passed
        if current_time - state['reset_time'] >= 1.0:
            state['request_count'] = 0
            state['reset_time'] = current_time

        # Check if we've hit the rate limit
        if state['request_count'] >= config['requests_per_second']:
            wait_time = 1.0 - (current_time - state['reset_time'])
            if wait_time > 0:
                if self.use_jitter:
                    wait_time += random.uniform(0, 0.1)  # Small jitter
                await asyncio.sleep(wait_time)
                # Reset after waiting
                state['request_count'] = 0
                state['reset_time'] = time.time()

        # Ensure minimum delay between requests
        time_since_last = current_time - state['last_request']
        min_delay = config['delay_between_requests']

        # Increase delay if we've had recent failures
        if self.failure_counts[operation_type] > 0:
            min_delay *= (1 + self.failure_counts[operation_type] * 0.5)

        if time_since_last < min_delay:
            wait_time = min_delay - time_since_last
            if self.use_jitter:
                wait_time += random.uniform(0, wait_time * 0.1)
            await asyncio.sleep(wait_time)

    def get_rate_limit_status(self) -> Dict:
        """Get current rate limiting status for debugging"""
        status = {}
        current_time = time.time()

        for op_type, state in self.rate_limit_state.items():
            config = self.rate_limits[op_type]
            time_since_reset = current_time - state['reset_time']
            time_since_last = current_time - state['last_request']

            status[op_type] = {
                'current_requests': state['request_count'],
                'max_requests_per_second': config['requests_per_second'],
                'time_since_reset': time_since_reset,
                'time_since_last_request': time_since_last,
                'failure_count': self.failure_counts.get(op_type, 0),
                'in_backoff': self.failure_counts.get(op_type, 0) > 0 and
                              (current_time - self.last_failure_times.get(op_type, 0)) < config[
                                  'delay_between_requests']
            }

        return status

    async def health_check(self) -> bool:
        """Check if the rate limiter is in a healthy state"""
        total_failures = sum(self.failure_counts.values())
        return total_failures < 10  # Consider unhealthy if more than 10 total failures

    def reset_failure_counts(self):
        """Reset all failure counts (for manual recovery)"""
        self.failure_counts = {op_type: 0 for op_type in self.rate_limits.keys()}
        self.last_failure_times = {op_type: 0 for op_type in self.rate_limits.keys()}
        print("🔄 Rate limiter failure counts reset")


# Enhanced safe operation functions
async def ultra_safe_role_operation(rate_limiter: DiscordRateLimiter, member: discord.Member,
                                    operation: str, *roles, reason: str = None, max_wait: float = 60.0):
    """
    Ultra-safe role operation with maximum protection against rate limiting

    Args:
        rate_limiter: The enhanced rate limiter instance
        member: Discord member
        operation: 'add' or 'remove'
        roles: Roles to add/remove
        reason: Reason for the operation
        max_wait: Maximum time to wait for completion

    Returns:
        Tuple of (success: bool, error_message: str or None)
    """
    start_time = time.time()

    try:
        # Add extra safety delay before starting
        await asyncio.sleep(random.uniform(0.5, 1.5))

        if operation == 'add':
            await rate_limiter.add_role_with_limit(member, *roles, reason=reason, max_retries=3)
        elif operation == 'remove':
            await rate_limiter.remove_role_with_limit(member, *roles, reason=reason, max_retries=3)
        else:
            return False, f"Invalid operation: {operation}"

        # Add extra safety delay after completion
        await asyncio.sleep(random.uniform(0.5, 1.0))

        elapsed = time.time() - start_time
        print(f"✅ Ultra-safe {operation} operation completed in {elapsed:.2f}s for {member.display_name}")

        return True, None

    except asyncio.TimeoutError:
        return False, f"Operation timed out after {max_wait}s"
    except discord.HTTPException as e:
        if e.status == 429:
            return False, f"Rate limited (this should be rare with enhanced protection)"
        elif e.status == 403:
            return False, f"Permission denied"
        elif e.status == 404:
            return False, f"Member or role not found"
        else:
            return False, f"Discord API error {e.status}: {str(e)}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"


async def batch_role_operations_with_extreme_safety(rate_limiter: DiscordRateLimiter,
                                                    operations: List[Dict],
                                                    progress_callback=None):
    """
    Process role operations in batches with extreme safety measures

    Args:
        rate_limiter: Enhanced rate limiter
        operations: List of dicts with 'member', 'operation', 'roles', 'reason'
        progress_callback: Optional async function for progress updates

    Returns:
        Dict with results
    """
    results = {
        'total': len(operations),
        'successful': 0,
        'failed': 0,
        'errors': []
    }

    print(f"🚀 Starting batch role operations for {len(operations)} members with extreme safety")

    # Process one at a time with long delays for maximum safety
    for i, op in enumerate(operations):
        try:
            member = op['member']
            operation = op['operation']  # 'add' or 'remove'
            roles = op['roles']
            reason = op.get('reason', 'Batch operation')

            # Progress update
            if progress_callback and i % 5 == 0:
                try:
                    await progress_callback(f"Processing {i + 1}/{len(operations)} members...")
                except:
                    pass  # Ignore progress callback errors

            # Ultra-safe operation
            success, error = await ultra_safe_role_operation(
                rate_limiter, member, operation, *roles, reason=reason
            )

            if success:
                results['successful'] += 1
                print(f"✅ {i + 1}/{len(operations)}: {operation} roles for {member.display_name}")
            else:
                results['failed'] += 1
                results['errors'].append(f"{member.display_name}: {error}")
                print(f"❌ {i + 1}/{len(operations)}: Failed for {member.display_name} - {error}")

            # CRITICAL: Long delay between each member (5-10 seconds)
            if i < len(operations) - 1:  # Don't wait after the last operation
                delay = random.uniform(5.0, 10.0)  # 5-10 second random delay
                print(f"⏳ Waiting {delay:.1f}s before next operation...")
                await asyncio.sleep(delay)

                # Extra long delay every 10 operations
                if (i + 1) % 10 == 0:
                    extra_delay = random.uniform(30.0, 60.0)  # 30-60 second break
                    print(f"🛑 Taking extended break: {extra_delay:.1f}s (processed {i + 1} members)")
                    await asyncio.sleep(extra_delay)

        except Exception as e:
            results['failed'] += 1
            error_msg = f"Critical error processing {op.get('member', 'unknown')}: {str(e)}"
            results['errors'].append(error_msg)
            print(f"❌ {error_msg}")

            # Still wait on error to prevent rapid fire
            await asyncio.sleep(random.uniform(3.0, 5.0))

    print(f"🏁 Batch operations completed: {results['successful']} successful, {results['failed']} failed")

    return results