import asyncio
import time
from collections import defaultdict, deque
from typing import Dict, Any, Optional
import discord
import logging


class DiscordRateLimiter:
    """
    Advanced rate limiter for Discord API calls to prevent 429 errors.

    Handles different rate limit buckets:
    - Global rate limits (50 requests per second)
    - Per-route rate limits (varies by endpoint)
    - Per-guild rate limits (for guild-specific operations)
    - Per-channel rate limits (for channel-specific operations)
    """

    def __init__(self):
        # Global rate limiting (Discord allows ~50 requests per second globally)
        self.global_limit = 45  # Stay slightly under the limit
        self.global_window = 1.0  # 1 second window
        self.global_requests = deque()

        # Per-route rate limiting
        self.route_limits: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            'limit': 5,  # Default limit per route
            'window': 1.0,  # Default window
            'requests': deque(),
            'reset_time': None
        })

        # Per-guild rate limiting
        self.guild_limits: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            'limit': 10,  # Requests per guild per second
            'window': 1.0,
            'requests': deque()
        })

        # Per-channel rate limiting
        self.channel_limits: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            'limit': 5,  # Messages per channel per 5 seconds
            'window': 5.0,
            'requests': deque()
        })

        # Role management rate limiting (very restrictive)
        self.role_limits: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            'limit': 1,  # 1 role change per 2 seconds per guild
            'window': 2.0,
            'requests': deque()
        })

        # Lock for thread safety
        self._lock = asyncio.Lock()

        # Track 429 responses for adaptive limiting
        self.recent_429s = deque()
        self.adaptive_factor = 1.0  # Multiplier to reduce limits when getting 429s

    async def wait_for_rate_limit(self,
                                  route: str = "default",
                                  guild_id: Optional[str] = None,
                                  channel_id: Optional[str] = None,
                                  is_role_operation: bool = False) -> None:
        """
        Wait if necessary to respect rate limits before making a request.

        Args:
            route: The API route being called (e.g., "POST/channels/{id}/messages")
            guild_id: Guild ID for guild-specific rate limiting
            channel_id: Channel ID for channel-specific rate limiting
            is_role_operation: True if this is a role add/remove operation
        """
        async with self._lock:
            now = time.time()

            # Clean old requests from all buckets
            self._clean_old_requests(now)

            # Check global rate limit first
            await self._wait_for_global_limit(now)

            # Check route-specific rate limit
            await self._wait_for_route_limit(route, now)

            # Check guild-specific rate limit
            if guild_id:
                await self._wait_for_guild_limit(guild_id, now)

            # Check channel-specific rate limit
            if channel_id:
                await self._wait_for_channel_limit(channel_id, now)

            # Check role operation rate limit (most restrictive)
            if is_role_operation and guild_id:
                await self._wait_for_role_limit(guild_id, now)

            # Record this request
            now = time.time()  # Update time after potential waits
            self.global_requests.append(now)
            self.route_limits[route]['requests'].append(now)

            if guild_id:
                self.guild_limits[guild_id]['requests'].append(now)
            if channel_id:
                self.channel_limits[channel_id]['requests'].append(now)
            if is_role_operation and guild_id:
                self.role_limits[guild_id]['requests'].append(now)

    def _clean_old_requests(self, now: float) -> None:
        """Remove old requests outside the time windows"""
        # Clean global requests
        while (self.global_requests and
               now - self.global_requests[0] > self.global_window):
            self.global_requests.popleft()

        # Clean route requests
        for route_data in self.route_limits.values():
            while (route_data['requests'] and
                   now - route_data['requests'][0] > route_data['window']):
                route_data['requests'].popleft()

        # Clean guild requests
        for guild_data in self.guild_limits.values():
            while (guild_data['requests'] and
                   now - guild_data['requests'][0] > guild_data['window']):
                guild_data['requests'].popleft()

        # Clean channel requests
        for channel_data in self.channel_limits.values():
            while (channel_data['requests'] and
                   now - channel_data['requests'][0] > channel_data['window']):
                channel_data['requests'].popleft()

        # Clean role requests
        for role_data in self.role_limits.values():
            while (role_data['requests'] and
                   now - role_data['requests'][0] > role_data['window']):
                role_data['requests'].popleft()

        # Clean old 429 responses (last 5 minutes)
        while (self.recent_429s and now - self.recent_429s[0] > 300):
            self.recent_429s.popleft()

    async def _wait_for_global_limit(self, now: float) -> None:
        """Wait if global rate limit would be exceeded"""
        effective_limit = int(self.global_limit * self.adaptive_factor)

        if len(self.global_requests) >= effective_limit:
            # Calculate wait time until oldest request expires
            oldest_request = self.global_requests[0]
            wait_time = self.global_window - (now - oldest_request)
            if wait_time > 0:
                print(f"Global rate limit hit, waiting {wait_time:.2f}s")
                await asyncio.sleep(wait_time)

    async def _wait_for_route_limit(self, route: str, now: float) -> None:
        """Wait if route-specific rate limit would be exceeded"""
        route_data = self.route_limits[route]
        effective_limit = int(route_data['limit'] * self.adaptive_factor)

        if len(route_data['requests']) >= effective_limit:
            oldest_request = route_data['requests'][0]
            wait_time = route_data['window'] - (now - oldest_request)
            if wait_time > 0:
                print(f"Route {route} rate limit hit, waiting {wait_time:.2f}s")
                await asyncio.sleep(wait_time)

    async def _wait_for_guild_limit(self, guild_id: str, now: float) -> None:
        """Wait if guild-specific rate limit would be exceeded"""
        guild_data = self.guild_limits[guild_id]
        effective_limit = int(guild_data['limit'] * self.adaptive_factor)

        if len(guild_data['requests']) >= effective_limit:
            oldest_request = guild_data['requests'][0]
            wait_time = guild_data['window'] - (now - oldest_request)
            if wait_time > 0:
                print(f"Guild {guild_id} rate limit hit, waiting {wait_time:.2f}s")
                await asyncio.sleep(wait_time)

    async def _wait_for_channel_limit(self, channel_id: str, now: float) -> None:
        """Wait if channel-specific rate limit would be exceeded"""
        channel_data = self.channel_limits[channel_id]
        effective_limit = int(channel_data['limit'] * self.adaptive_factor)

        if len(channel_data['requests']) >= effective_limit:
            oldest_request = channel_data['requests'][0]
            wait_time = channel_data['window'] - (now - oldest_request)
            if wait_time > 0:
                print(f"Channel {channel_id} rate limit hit, waiting {wait_time:.2f}s")
                await asyncio.sleep(wait_time)

    async def _wait_for_role_limit(self, guild_id: str, now: float) -> None:
        """Wait if role operation rate limit would be exceeded"""
        role_data = self.role_limits[guild_id]

        if len(role_data['requests']) >= role_data['limit']:
            oldest_request = role_data['requests'][0]
            wait_time = role_data['window'] - (now - oldest_request)
            if wait_time > 0:
                print(f"Role operation rate limit for guild {guild_id}, waiting {wait_time:.2f}s")
                await asyncio.sleep(wait_time)

    def handle_429_response(self, retry_after: Optional[float] = None) -> None:
        """
        Handle a 429 Too Many Requests response.

        Args:
            retry_after: Seconds to wait before retrying (from Discord's response)
        """
        now = time.time()
        self.recent_429s.append(now)

        # Implement adaptive limiting - reduce limits if getting 429s frequently
        recent_429_count = len(self.recent_429s)
        if recent_429_count >= 3:  # 3+ 429s in last 5 minutes
            self.adaptive_factor = max(0.5, self.adaptive_factor * 0.8)
            print(f"Reducing adaptive factor to {self.adaptive_factor:.2f} due to frequent 429s")

        # Log the 429 for monitoring
        print(f"Received 429 response. Retry after: {retry_after}s. Recent 429s: {recent_429_count}")

    def update_route_limit(self, route: str, limit: int, window: float = 1.0) -> None:
        """Update rate limit for a specific route based on Discord's headers"""
        self.route_limits[route]['limit'] = limit
        self.route_limits[route]['window'] = window
        print(f"Updated rate limit for {route}: {limit} requests per {window}s")

    def reset_adaptive_factor(self) -> None:
        """Reset adaptive factor when no recent 429s"""
        if len(self.recent_429s) == 0 and self.adaptive_factor < 1.0:
            self.adaptive_factor = min(1.0, self.adaptive_factor * 1.1)
            print(f"Increased adaptive factor to {self.adaptive_factor:.2f}")


# Create global rate limiter instance
discord_rate_limiter = DiscordRateLimiter()


# Decorator for rate-limited Discord API calls
def rate_limited(route: str = "default",
                 is_role_operation: bool = False):
    """
    Decorator to automatically apply rate limiting to Discord API calls.

    Args:
        route: The API route being called
        is_role_operation: True if this involves role add/remove operations
    """

    def decorator(func):
        async def wrapper(*args, **kwargs):
            # Extract context from function arguments
            guild_id = None
            channel_id = None

            # Try to extract IDs from common argument patterns
            for arg in args:
                if isinstance(arg, discord.Guild):
                    guild_id = str(arg.id)
                elif isinstance(arg, discord.Member):
                    guild_id = str(arg.guild.id)
                elif isinstance(arg, (discord.TextChannel, discord.VoiceChannel)):
                    channel_id = str(arg.id)
                    guild_id = str(arg.guild.id)
                elif isinstance(arg, discord.Interaction):
                    if arg.guild:
                        guild_id = str(arg.guild.id)
                    if arg.channel:
                        channel_id = str(arg.channel.id)

            # Wait for rate limits before making the call
            await discord_rate_limiter.wait_for_rate_limit(
                route=route,
                guild_id=guild_id,
                channel_id=channel_id,
                is_role_operation=is_role_operation
            )

            try:
                return await func(*args, **kwargs)
            except discord.HTTPException as e:
                if e.status == 429:  # Too Many Requests
                    retry_after = getattr(e, 'retry_after', None)
                    discord_rate_limiter.handle_429_response(retry_after)

                    if retry_after:
                        print(f"429 error, waiting {retry_after}s before retry")
                        await asyncio.sleep(retry_after)
                        return await func(*args, **kwargs)
                raise

        return wrapper

    return decorator


# Helper functions for common Discord operations
class RateLimitedDiscordOps:
    """Helper class with rate-limited Discord operations"""

    @staticmethod
    @rate_limited("POST/channels/{id}/messages")
    async def send_message(channel, *args, **kwargs):
        """Rate-limited message sending"""
        return await channel.send(*args, **kwargs)

    @staticmethod
    @rate_limited("PATCH/channels/{id}/messages/{id}", is_role_operation=False)
    async def edit_message(message, *args, **kwargs):
        """Rate-limited message editing"""
        return await message.edit(*args, **kwargs)

    @staticmethod
    @rate_limited("PUT/guilds/{id}/members/{id}/roles/{id}", is_role_operation=True)
    async def add_role(member, role, *args, **kwargs):
        """Rate-limited role addition"""
        return await member.add_roles(role, *args, **kwargs)

    @staticmethod
    @rate_limited("DELETE/guilds/{id}/members/{id}/roles/{id}", is_role_operation=True)
    async def remove_role(member, role, *args, **kwargs):
        """Rate-limited role removal"""
        return await member.remove_roles(role, *args, **kwargs)

    @staticmethod
    @rate_limited("PUT/channels/{id}/messages/{id}/reactions/{emoji}/@me")
    async def add_reaction(message, emoji):
        """Rate-limited reaction addition"""
        return await message.add_reaction(emoji)

    @staticmethod
    @rate_limited("DELETE/channels/{id}/messages/{id}/reactions/{emoji}/@me")
    async def remove_reaction(message, emoji, user):
        """Rate-limited reaction removal"""
        return await message.remove_reaction(emoji, user)

    @staticmethod
    @rate_limited("POST/channels/{id}/messages/{id}/crosspost")
    async def publish_message(message):
        """Rate-limited message publishing"""
        return await message.publish()

    @staticmethod
    @rate_limited("GET/guilds/{id}/members/{id}")
    async def fetch_member(guild, member_id):
        """Rate-limited member fetching"""
        return await guild.fetch_member(member_id)

    @staticmethod
    @rate_limited("PATCH/guilds/{id}/members/{id}")
    async def edit_member(member, *args, **kwargs):
        """Rate-limited member editing"""
        return await member.edit(*args, **kwargs)


# Background task to reset adaptive factor
async def rate_limiter_maintenance():
    """Background task to maintain the rate limiter"""
    while True:
        try:
            # Reset adaptive factor if no recent 429s
            discord_rate_limiter.reset_adaptive_factor()

            # Clean up old data
            now = time.time()
            discord_rate_limiter._clean_old_requests(now)

        except Exception as e:
            print(f"Error in rate limiter maintenance: {e}")

        # Run every 30 seconds
        await asyncio.sleep(30)