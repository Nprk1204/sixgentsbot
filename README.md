# Rocket League 6 Mans Discord Bot

A Discord bot for organizing Rocket League 6 mans games with team selection voting, captain selection, match reporting, MMR tracking, and a leaderboard website.

## Features

- Queue system for players to join/leave
- Team selection voting (Random or Captains)
- Captain selection via DMs
- Match reporting with MMR adjustments
- Leaderboard tracking
- Web interface for viewing rankings
- Admin commands for managing matches and queue

## Setup

### Prerequisites

- Python 3.8+
- MongoDB Atlas account
- Discord Bot Token
- Discord Server with Administrator permissions

### Installation

1. Clone this repository:
```bash
git clone https://github.com/yourusername/rocket-league-6mans-bot.git
cd rocket-league-6mans-bot
```

2. Create a virtual environment and install dependencies:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

3. Create a `.env` file with your credentials:
```
DISCORD_TOKEN=your_discord_bot_token
MONGO_URI=your_mongodb_connection_string
```

4. Create a directory for templates:
```bash
mkdir templates
```

5. Save the index.html file in the templates directory.

### Running the Bot

```bash
python main.py
```

### Running the Web Leaderboard

The leaderboard website runs separately from the Discord bot:

```bash
python leaderboard_app.py
```

By default, the website runs on http://localhost:5000. For production deployment, consider using a WSGI server like Gunicorn with Nginx.

## Commands

### Queue Commands
- `/join` - Join the queue
- `/leave` - Leave the queue
- `/status` - Show the current queue status

### Match Commands
- `/report <team1_score> <team2_score>` - Report match results
- `/leaderboard [limit]` - Show the leaderboard (default: top 10)
- `/rank [member]` - Show your rank or another member's rank

### Admin Commands
- `/clearqueue` - Clear the queue (Admin only)
- `/forcestart` - Force start the team selection process (Admin only)
- `/purgechat <amount>` - Clear chat messages (Admin only)

## How it Works

1. **Queue System:**
   - Players join the queue using `/join`
   - When the queue reaches 6 players, voting starts automatically

2. **Team Selection:**
   - Players vote using reactions (🎲 for Random, 👑 for Captains)
   - After 60 seconds or when all votes are in, teams are created

3. **Captains Mode:**
   - If Captains mode wins, two random captains are selected
   - Captain 1 picks one player, Captain 2 picks two players
   - Remaining player goes to Team 1

4. **Match Reporting:**
   - After the match, any player can report the result with `/report`
   - MMR is updated based on the result (+15 for winners, -12 for losers)

5. **Leaderboard:**
   - View rankings in Discord with `/leaderboard`
   - Check detailed stats with `/rank`
   - Visit the web interface for a more detailed view

## Customization

### MMR Settings
You can customize the MMR gain/loss values in the `match_system.py` file:

```python
# MMR gain/loss values
MMR_GAIN = 15  # Default: +15 for winners
MMR_LOSS = 12  # Default: -12 for losers
```

### Styling the Leaderboard
The web leaderboard uses Bootstrap and custom CSS. You can modify the styling in the `templates/index.html` file.

## Deployment

### Discord Bot
For 24/7 operation, consider hosting the bot on a VPS or using a service like Heroku. Make sure to update the `.env` file with your production credentials.

### Web Leaderboard
To deploy the web leaderboard, you can use:
- Heroku
- AWS Elastic Beanstalk
- DigitalOcean App Platform
- Any VPS with Nginx + Gunicorn

## MongoDB Schema

The application uses three main collections:

1. **queue** - Stores currently queued players
2. **matches** - Stores match information
3. **players** - Stores player stats and MMR

## Dependencies

- discord.py - Discord API wrapper
- pymongo - MongoDB driver
- flask - Web framework for leaderboard
- python-dotenv - Environment variable management
- uuid - For generating unique IDs

## License

MIT License

## Credits

Created by [Nathan]