"""
Simple test to check if basic discord.py works
"""

# Test basic imports first
try:
    print("Testing basic imports...")

    # Test Flask
    from flask import Flask

    print("✅ Flask works")

    # Test MongoDB
    from pymongo import MongoClient

    print("✅ PyMongo works")

    # Test dotenv
    from dotenv import load_dotenv

    print("✅ python-dotenv works")

    # Test discord - this might fail
    import discord

    print("✅ Discord.py works!")

    print("\n🎉 All imports successful!")
    print("Your Python environment is working correctly.")

except ImportError as e:
    print(f"❌ Import error: {e}")
    print("\n💡 Solutions:")
    print("1. Downgrade to Python 3.11 or 3.12")
    print("2. Update discord.py: pip install --upgrade discord.py")
    print("3. Check if you're in the virtual environment")

except Exception as e:
    print(f"❌ Unexpected error: {e}")

# Check Python version
import sys

print(f"\n🐍 Python version: {sys.version}")

if sys.version_info >= (3, 13):
    print("⚠️ Warning: Python 3.13+ has compatibility issues with discord.py")
    print("💡 Recommended: Use Python 3.11 or 3.12")