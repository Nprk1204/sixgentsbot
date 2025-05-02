from pymongo import MongoClient
class Database:
    def __init__(self, MONGO_URI):
        self.client = MongoClient(MONGO_URI)
        self.db = self.client['sixgents_db']

    def get_collection(self, name):
        return self.db[name]
