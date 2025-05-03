# test_discord_role.py
import os
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
DISCORD_BOT_TOKEN = os.getenv('DISCORD_TOKEN')
DISCORD_GUILD_ID = os.getenv('DISCORD_GUILD_ID')


def test_role_assignment(username, role_name=None, role_id=None):
    """Test Discord role assignment with detailed logging"""
    print("\n===== DISCORD ROLE ASSIGNMENT TEST =====")
    print(f"Testing role assignment for user: {username}")
    print(f"Role name: {role_name}")
    print(f"Role ID: {role_id}")

    if not username or not DISCORD_BOT_TOKEN or not DISCORD_GUILD_ID:
        print("❌ Missing required information:")
        print(f"- Username provided: {'Yes' if username else 'No'}")
        print(f"- Bot token provided: {'Yes' if DISCORD_BOT_TOKEN else 'No'}")
        print(f"- Guild ID provided: {'Yes' if DISCORD_GUILD_ID else 'No'}")
        return False

    # Headers for all API requests
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        # STEP 1: Verify bot authentication
        print("\n1. Verifying bot authentication...")
        auth_url = "https://discord.com/api/v10/users/@me"
        auth_response = requests.get(auth_url, headers=headers)

        if auth_response.status_code != 200:
            print(f"❌ Authentication failed: {auth_response.status_code}")
            print(f"Response: {auth_response.text[:200]}")
            return False

        bot_user = auth_response.json()
        bot_id = bot_user.get('id')
        bot_name = bot_user.get('username')
        print(f"✅ Bot authenticated as: {bot_name} (ID: {bot_id})")

        # STEP 2: Get server information
        print("\n2. Getting server information...")
        guild_url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}"
        guild_response = requests.get(guild_url, headers=headers)

        if guild_response.status_code != 200:
            print(f"❌ Failed to get server info: {guild_response.status_code}")
            print(f"Response: {guild_response.text[:200]}")
            return False

        guild_data = guild_response.json()
        print(f"✅ Connected to server: {guild_data.get('name')}")

        # STEP 3: Get all server members
        print("\n3. Getting server members...")
        members_url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/members?limit=1000"
        members_response = requests.get(members_url, headers=headers)

        if members_response.status_code != 200:
            print(f"❌ Failed to get members: {members_response.status_code}")
            print(f"Response: {members_response.text[:200]}")
            return False

        members = members_response.json()
        print(f"✅ Found {len(members)} members in the server")

        # STEP 4: Find target user
        print(f"\n4. Looking for user matching '{username}'...")
        user_id = None
        matched_name = None
        search_name = username.lower().strip()

        for member in members:
            member_user = member.get('user', {})
            member_username = (member_user.get('username') or '').lower().strip()
            member_global_name = (member_user.get('global_name') or '').lower().strip()
            member_nickname = (member.get('nick') or '').lower().strip()
            member_id = member_user.get('id')

            print(
                f"  Checking member: id={member_id}, username={member_username}, global_name={member_global_name}, nickname={member_nickname}")

            if (search_name == member_username or
                    search_name == member_global_name or
                    search_name == member_nickname or
                    search_name in member_username or
                    search_name in member_global_name or
                    search_name in member_nickname):
                user_id = member_id
                matched_name = member_user.get('username') or member_global_name
                print(f"✅ Found matching user: {matched_name} (ID: {user_id})")
                break

        if not user_id:
            print(f"❌ No matching user found for '{username}'")
            return False

        # STEP 5: Get all server roles and bot's highest role
        print("\n5. Getting server roles...")
        roles_url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/roles"
        roles_response = requests.get(roles_url, headers=headers)

        if roles_response.status_code != 200:
            print(f"❌ Failed to get roles: {roles_response.status_code}")
            print(f"Response: {roles_response.text[:200]}")
            return False

        roles = roles_response.json()
        print(f"✅ Found {len(roles)} roles in the server")

        # Print all roles for reference
        print("\nAvailable roles:")
        for i, role in enumerate(roles):
            role_id_value = role.get('id')
            role_name_value = role.get('name')
            role_position = role.get('position')
            print(f"  {i + 1}. '{role_name_value}' (ID: {role_id_value}, Position: {role_position})")

        # Find bot's highest role position
        bot_highest_role = 0
        for member in members:
            member_user = member.get('user', {})
            if member_user.get('id') == bot_id:
                member_roles = member.get('roles', [])
                for role in roles:
                    if role.get('id') in member_roles and role.get('position', 0) > bot_highest_role:
                        bot_highest_role = role.get('position', 0)
                break

        print(f"\nBot's highest role position: {bot_highest_role}")

        # STEP 6: Find target role (by name or ID)
        print("\n6. Finding target role...")
        target_role_id = role_id
        target_role_position = 0

        if not target_role_id and role_name:
            # Find by name if ID not provided
            for role in roles:
                if role.get('name', '').lower() == role_name.lower():
                    target_role_id = role.get('id')
                    target_role_position = role.get('position', 0)
                    print(
                        f"✅ Found role by name: '{role.get('name')}' (ID: {target_role_id}, Position: {target_role_position})")
                    break

            if not target_role_id:
                print(f"❌ No role found with name: '{role_name}'")
                return False
        elif target_role_id:
            # Verify the ID exists
            role_found = False
            for role in roles:
                if role.get('id') == target_role_id:
                    target_role_position = role.get('position', 0)
                    print(
                        f"✅ Found role by ID: '{role.get('name')}' (ID: {target_role_id}, Position: {target_role_position})")
                    role_found = True
                    break

            if not role_found:
                print(f"❌ No role found with ID: '{target_role_id}'")
                return False
        else:
            print("❌ No role name or ID provided")
            return False

        # STEP 7: Check role hierarchy
        print("\n7. Checking role hierarchy...")
        if target_role_position >= bot_highest_role:
            print(
                f"❌ Role hierarchy issue: Bot's highest role ({bot_highest_role}) must be higher than the role to assign ({target_role_position})")
            return False

        print("✅ Bot's role position is higher than target role - hierarchy check passed")

        # STEP 8: Assign the role
        print("\n8. Attempting to assign role...")
        assign_url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/members/{user_id}/roles/{target_role_id}"
        assign_response = requests.put(assign_url, headers=headers)

        if assign_response.status_code in [204, 200]:
            print(f"✅ Role assignment successful! Status code: {assign_response.status_code}")
            return True
        else:
            print(f"❌ Role assignment failed: {assign_response.status_code}")
            print(f"Response: {assign_response.text[:200]}")

            if assign_response.status_code == 403:
                print("This is likely a permissions issue. Check that your bot has 'Manage Roles' permission.")

            return False

    except Exception as e:
        import traceback
        print(f"❌ Exception occurred: {str(e)}")
        traceback.print_exc()
        return False
    finally:
        print("\n===== TEST COMPLETED =====")


if __name__ == "__main__":
    # Example usage by name
    print("\n\n---- TESTING ROLE ASSIGNMENT BY NAME ----")
    discord_username = input("Enter Discord username to test: ")
    role_name = input("Enter role name to assign (e.g., 'Rank A'): ")
    test_role_assignment(discord_username, role_name=role_name)

    # Example usage by ID
    print("\n\n---- TESTING ROLE ASSIGNMENT BY ID ----")
    discord_username = input("Enter Discord username to test: ")
    role_id = input("Enter role ID to assign: ")
    test_role_assignment(discord_username, role_id=role_id)