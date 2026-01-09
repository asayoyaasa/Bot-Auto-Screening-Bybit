import json
import os
from dotenv import load_dotenv

load_dotenv()

def load_config():
    if not os.path.exists('config.json'): return {}
    with open('config.json', 'r') as f:
        config = json.load(f)
    
    if os.getenv('BOT_ENV') == 'testing':
        print("⚠️ RUNNING IN TEST MODE")
        config['database']['database'] = 'bybit_bot_test'
        
    return config

CONFIG = load_config()