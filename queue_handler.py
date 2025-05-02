import discord

class QueueHandler:
    def __init__(self, db):
        self.queue = db.get_collection('queue')
        self.vote_system = None
        self.captains_system = None

    def set_vote_system(self, vote_system):
        """Set the vote system reference"""
        self.vote_system = vote_system

    def set_captains_system(self, captains_system):
        """Set the captains system reference"""
        self.captains_system = captains_system

    def add_player(self, player):
        """Add a player to the queue"""
        player_id = str(player.id)
        player_mention = player.mention
        player_name = player.display_name

        # Check if player is already in queue
        if self.queue.find_one({"id": player_id}):
            return f"{player_mention} is already in queue!"

        # Store player in queue
        self.queue.insert_one({
            "id": player_id,
            "name": player_name,
            "mention": player_mention
        })

        count = self.queue.count_documents({})

        # Start vote if queue is full
        if count == 6 and self.vote_system:
            return f"{player_mention} has joined the queue! Queue is now full!\n\nStarting team selection vote..."

        return f"{player_mention} has joined the queue! There are {count}/6 players"

    def remove_player(self, player):
        """Remove a player from the queue"""
        player_id = str(player.id)
        result = self.queue.delete_one({"id": player_id})

        # Cancel any active votes or selections
        if self.vote_system:
            self.vote_system.cancel_voting()

        if self.captains_system:
            self.captains_system.cancel_selection()

        if result.deleted_count > 0:
            return f"{player.mention} has left the queue!"
        else:
            return f"{player.mention} was not in the queue!"

    def get_queue_status(self):
        """Get the current status of the queue"""
        # Get all players currently in the queue
        players = list(self.queue.find())
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
        elif self.vote_system and self.vote_system.is_voting_active():
            embed.add_field(name="Status", value="**Voting in progress!** Use `/vote random` or `/vote captains`",
                            inline=False)
        elif self.captains_system and self.captains_system.is_selection_active():
            embed.add_field(name="Status", value="**Captain selection in progress!**", inline=False)

        return embed

    def get_players_for_match(self):
        """Get players in the queue for a match"""
        return list(self.queue.find().limit(6))

    def remove_players_from_queue(self, players):
        """Remove players from the queue"""
        for player in players:
            self.queue.delete_one({"id": player['id']})

    def is_player_in_queue(self, player_id):
        """Check if a player is in the queue"""
        return self.queue.find_one({"id": player_id}) is not None