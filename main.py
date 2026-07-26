import os
import json
import hashlib
import asyncio
import aiohttp
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
import logging
from collections import defaultdict
import time
import sqlite3
import re
import threading
import nest_asyncio

# Apply nest_asyncio to allow nested event loops
nest_asyncio.apply()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuration
API_ID = int(os.environ.get('API_ID', '33032511'))
API_HASH = os.environ.get('API_HASH', '58d0bd6b23f6da7bde206f79866dbc4b')
BOT_USERNAME = '@THE_UNKNOWN_OSINT_BOT'
DEFAULT_COUNTRY_CODE = '91'

# Database setup
def init_db():
    conn = sqlite3.connect('sessions.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sessions
                 (phone TEXT PRIMARY KEY, 
                  session_string TEXT, 
                  created_at TIMESTAMP,
                  last_used TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS pending_otp
                 (phone TEXT PRIMARY KEY,
                  phone_code_hash TEXT,
                  timestamp TIMESTAMP)''')
    conn.commit()
    conn.close()

init_db()

# Temporary storage
pending_otp_cache = {}

# Timeouts
REQUEST_TIMEOUT = 30
OTP_TIMEOUT = 120

# Global bot client
bot_client = None
user_clients = {}

def format_phone_for_telegram(phone):
    """Format phone number for Telegram API"""
    phone = re.sub(r'\D', '', phone)
    if phone.startswith('0'):
        phone = phone[1:]
    if not phone.startswith('91') and len(phone) == 10:
        phone = DEFAULT_COUNTRY_CODE + phone
    if not phone.startswith('+'):
        phone = '+' + phone
    return phone

def save_session(phone, session_string):
    """Save session to database"""
    try:
        conn = sqlite3.connect('sessions.db')
        c = conn.cursor()
        current_time = datetime.now().isoformat()
        c.execute('''INSERT OR REPLACE INTO sessions 
                     (phone, session_string, created_at, last_used) 
                     VALUES (?, ?, ?, ?)''',
                  (phone, session_string, current_time, current_time))
        conn.commit()
        conn.close()
        logger.info(f"Session saved for {phone}")
        return True
    except Exception as e:
        logger.error(f"Error saving session: {e}")
        return False

def get_session(phone):
    """Get session from database"""
    try:
        conn = sqlite3.connect('sessions.db')
        c = conn.cursor()
        c.execute('SELECT session_string FROM sessions WHERE phone = ?', (phone,))
        result = c.fetchone()
        conn.close()
        if result:
            update_session_usage(phone)
            return result[0]
        return None
    except Exception as e:
        logger.error(f"Error getting session: {e}")
        return None

def update_session_usage(phone):
    """Update last_used timestamp"""
    try:
        conn = sqlite3.connect('sessions.db')
        c = conn.cursor()
        c.execute('UPDATE sessions SET last_used = ? WHERE phone = ?',
                 (datetime.now().isoformat(), phone))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error updating session usage: {e}")

def delete_session(phone):
    """Delete session from database"""
    try:
        conn = sqlite3.connect('sessions.db')
        c = conn.cursor()
        c.execute('DELETE FROM sessions WHERE phone = ?', (phone,))
        conn.commit()
        conn.close()
        logger.info(f"Session deleted for {phone}")
        return True
    except Exception as e:
        logger.error(f"Error deleting session: {e}")
        return False

def save_pending_otp(phone, phone_code_hash):
    """Save pending OTP to database"""
    try:
        conn = sqlite3.connect('sessions.db')
        c = conn.cursor()
        current_time = datetime.now().isoformat()
        c.execute('''INSERT OR REPLACE INTO pending_otp 
                     (phone, phone_code_hash, timestamp) 
                     VALUES (?, ?, ?)''',
                  (phone, phone_code_hash, current_time))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error saving pending OTP: {e}")
        return False

def get_pending_otp(phone):
    """Get pending OTP from database"""
    try:
        conn = sqlite3.connect('sessions.db')
        c = conn.cursor()
        c.execute('SELECT phone_code_hash, timestamp FROM pending_otp WHERE phone = ?', (phone,))
        result = c.fetchone()
        conn.close()
        return result
    except Exception as e:
        logger.error(f"Error getting pending OTP: {e}")
        return None

def delete_pending_otp(phone):
    """Delete pending OTP from database"""
    try:
        conn = sqlite3.connect('sessions.db')
        c = conn.cursor()
        c.execute('DELETE FROM pending_otp WHERE phone = ?', (phone,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error deleting pending OTP: {e}")
        return False

async def initialize_bot():
    """Initialize the Telegram bot client"""
    global bot_client
    try:
        bot_client = TelegramClient('bot_session', API_ID, API_HASH)
        await bot_client.start()
        logger.info("Bot client initialized successfully")
        return True
    except Exception as e:
        logger.error(f"Failed to initialize bot: {e}")
        return False

async def get_user_client(phone):
    """Get or create user client with permanent session"""
    try:
        formatted_phone = format_phone_for_telegram(phone)
        
        if formatted_phone in user_clients:
            client = user_clients[formatted_phone]
            try:
                await client.get_me()
                return client, True
            except:
                del user_clients[formatted_phone]
        
        session_string = get_session(formatted_phone)
        if session_string:
            try:
                client = TelegramClient(formatted_phone, API_ID, API_HASH)
                await client.connect()
                try:
                    await client.start()
                    user_clients[formatted_phone] = client
                    logger.info(f"Reconnected to existing session for {formatted_phone}")
                    return client, True
                except Exception as e:
                    logger.warning(f"Failed to reconnect session for {formatted_phone}: {e}")
                    delete_session(formatted_phone)
                    return None, False
            except Exception as e:
                logger.error(f"Error connecting with session: {e}")
                delete_session(formatted_phone)
                return None, False
        return None, False
    except Exception as e:
        logger.error(f"Error getting user client: {e}")
        return None, False

async def send_command_to_bot(phone, client, command):
    """Send command to bot using user's client"""
    try:
        bot_entity = await client.get_entity(BOT_USERNAME)
        await client.send_message(bot_entity, command)
        logger.info(f"Sent command {command} to bot for {phone}")
        
        start_time = time.time()
        response_json = None
        response_messages = []
        
        @client.on(events.NewMessage(from_users=bot_entity))
        async def handler(event):
            nonlocal response_json
            if event.message.text:
                full_text = event.message.text
                response_messages.append(full_text)
                logger.info(f"Received response from bot: {full_text[:100]}...")
                
                if "JSON" in full_text or "{" in full_text:
                    try:
                        json_start = full_text.find("{")
                        json_end = full_text.rfind("}") + 1
                        if json_start != -1 and json_end != -1:
                            json_content = full_text[json_start:json_end]
                            response_json = json.loads(json_content)
                            logger.info(f"Parsed JSON response: {response_json}")
                    except Exception as e:
                        logger.error(f"Error parsing JSON: {e}")
        
        while time.time() - start_time < REQUEST_TIMEOUT:
            await asyncio.sleep(0.5)
            if response_json:
                return response_json
        
        if response_messages and not response_json:
            for msg in response_messages:
                if "{" in msg:
                    try:
                        json_start = msg.find("{")
                        json_end = msg.rfind("}") + 1
                        if json_start != -1 and json_end != -1:
                            json_content = msg[json_start:json_end]
                            return json.loads(json_content)
                    except:
                        pass
                if "not found" in msg.lower() or "no result" in msg.lower():
                    return {"status": "error", "message": "Number not found"}
        
        return {"status": "error", "message": "No valid response received"}
        
    except FloodWaitError as e:
        logger.error(f"Flood wait error for {phone}: {e}")
        return {"status": "error", "message": f"Please wait {e.seconds} seconds"}
    except Exception as e:
        logger.error(f"Error sending command for {phone}: {e}")
        return {"status": "error", "message": str(e)}

async def handle_login_request(phone):
    """Handle login request and send OTP"""
    try:
        if not bot_client:
            await initialize_bot()
        
        formatted_phone = format_phone_for_telegram(phone)
        
        session_string = get_session(formatted_phone)
        if session_string:
            client = TelegramClient(formatted_phone, API_ID, API_HASH)
            await client.connect()
            try:
                await client.start()
                user_clients[formatted_phone] = client
                return {"status": "success", "message": "Already logged in", "session_active": True}
            except Exception as e:
                logger.warning(f"Invalid session for {formatted_phone}, requesting new login")
                delete_session(formatted_phone)
        
        client = TelegramClient(formatted_phone, API_ID, API_HASH)
        await client.connect()
        
        try:
            result = await client.send_code_request(formatted_phone)
            
            user_clients[formatted_phone] = client
            
            phone_code_hash = str(result.phone_code_hash) if result.phone_code_hash else None
            
            save_pending_otp(formatted_phone, phone_code_hash)
            
            pending_otp_cache[formatted_phone] = {
                'client': client,
                'phone_code_hash': phone_code_hash,
                'timestamp': time.time()
            }
            
            return {
                "status": "success", 
                "message": "OTP sent successfully to your Telegram",
                "formatted_phone": formatted_phone
            }
            
        except Exception as e:
            await client.disconnect()
            if formatted_phone in user_clients:
                del user_clients[formatted_phone]
            logger.error(f"Error sending OTP for {formatted_phone}: {e}")
            return {"status": "error", "message": str(e)}
            
    except Exception as e:
        logger.error(f"Login error for {phone}: {e}")
        return {"status": "error", "message": str(e)}

async def verify_otp_and_save_session(phone, otp_code):
    """Verify OTP and save session permanently"""
    try:
        formatted_phone = format_phone_for_telegram(phone)
        
        otp_data = pending_otp_cache.get(formatted_phone)
        
        if not otp_data:
            db_data = get_pending_otp(formatted_phone)
            if not db_data:
                return {"status": "error", "message": "No pending OTP request. Please login first."}
            
            phone_code_hash, timestamp_str = db_data
            timestamp = datetime.fromisoformat(timestamp_str).timestamp()
            
            client = user_clients.get(formatted_phone)
            if not client:
                return {"status": "error", "message": "Client not found. Please login again."}
            
            otp_data = {
                'client': client,
                'phone_code_hash': phone_code_hash,
                'timestamp': timestamp
            }
        else:
            client = otp_data['client']
        
        if time.time() - otp_data['timestamp'] > OTP_TIMEOUT:
            await client.disconnect()
            if formatted_phone in user_clients:
                del user_clients[formatted_phone]
            if formatted_phone in pending_otp_cache:
                del pending_otp_cache[formatted_phone]
            delete_pending_otp(formatted_phone)
            return {"status": "error", "message": "OTP expired. Please request new OTP."}
        
        try:
            await client.sign_in(formatted_phone, otp_code, phone_code_hash=otp_data.get('phone_code_hash'))
            
            session_string = client.session.save()
            
            if save_session(formatted_phone, session_string):
                if formatted_phone in pending_otp_cache:
                    del pending_otp_cache[formatted_phone]
                delete_pending_otp(formatted_phone)
                return {"status": "success", "message": "Login successful. Session saved permanently."}
            else:
                return {"status": "error", "message": "Failed to save session"}
                
        except Exception as e:
            logger.error(f"Error verifying OTP for {formatted_phone}: {e}")
            
            if "password" in str(e).lower():
                return {"status": "error", "message": "2FA enabled. Need password support."}
            elif "code" in str(e).lower():
                return {"status": "error", "message": f"Invalid OTP: {str(e)}"}
            else:
                return {"status": "error", "message": str(e)}
            
    except Exception as e:
        logger.error(f"OTP verification error for {phone}: {e}")
        return {"status": "error", "message": str(e)}

def run_async_task(coro):
    """Run async task in a new event loop"""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(coro)
        loop.close()
        return result
    except Exception as e:
        logger.error(f"Error in async task: {e}")
        raise

@app.route('/num', methods=['GET'])
def get_number_info():
    """Main endpoint to get number information"""
    try:
        phone_number = request.args.get('num', '').strip()
        
        if not phone_number:
            return jsonify({
                "status": "error",
                "message": "Please input valid number (10 digits required)"
            }), 400
        
        phone_number = re.sub(r'\D', '', phone_number)
        
        if len(phone_number) != 10:
            return jsonify({
                "status": "error",
                "message": "Please input valid number (exactly 10 digits required)"
            }), 400
        
        formatted_phone = format_phone_for_telegram(phone_number)
        
        session_string = get_session(formatted_phone)
        if not session_string:
            return jsonify({
                "status": "error",
                "message": "No active session. Please login first with /login?num=7724809103"
            }), 403
        
        # Run async task
        result = run_async_task(get_user_client(phone_number))
        client, is_valid = result
        
        if not is_valid or not client:
            return jsonify({
                "status": "error",
                "message": "Session expired. Please login again with /login?num=7724809103"
            }), 403
        
        command = f"/num {phone_number}"
        response = run_async_task(send_command_to_bot(phone_number, client, command))
        
        if not response:
            return jsonify({
                "status": "error",
                "message": "No response from bot"
            }), 404
        
        if response.get('status') == 'error':
            return jsonify(response), 404
        
        if response.get('count', 0) == 0:
            return jsonify({
                "status": "error",
                "message": "No information found for this number",
                "query": phone_number
            }), 404
        
        return jsonify(response)
        
    except Exception as e:
        logger.error(f"Error in get_number_info: {e}")
        return jsonify({
            "status": "error",
            "message": f"Internal server error: {str(e)}"
        }), 500

@app.route('/login', methods=['GET'])
def login():
    """Login endpoint to start OTP process"""
    try:
        phone_number = request.args.get('num', '').strip()
        
        if not phone_number:
            return jsonify({
                "status": "error",
                "message": "Please input valid number (10 digits required)"
            }), 400
        
        phone_number = re.sub(r'\D', '', phone_number)
        
        if len(phone_number) != 10:
            return jsonify({
                "status": "error",
                "message": "Please input valid number (exactly 10 digits required)"
            }), 400
        
        formatted_phone = format_phone_for_telegram(phone_number)
        
        session_string = get_session(formatted_phone)
        if session_string:
            result = run_async_task(get_user_client(phone_number))
            client, is_valid = result
            
            if is_valid:
                return jsonify({
                    "status": "success",
                    "message": "Already logged in with valid session",
                    "session_active": True,
                    "phone": formatted_phone
                })
        
        result = run_async_task(handle_login_request(phone_number))
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"Error in login: {e}")
        return jsonify({
            "status": "error",
            "message": f"Internal server error: {str(e)}"
        }), 500

@app.route('/login/otp', methods=['GET'])
def verify_otp_endpoint():
    """Verify OTP endpoint"""
    try:
        phone_number = request.args.get('num', '').strip()
        otp_code = request.args.get('otp', '').strip()
        
        if not phone_number:
            return jsonify({
                "status": "error",
                "message": "Please input valid number (10 digits required)"
            }), 400
        
        phone_number = re.sub(r'\D', '', phone_number)
        
        if len(phone_number) != 10:
            return jsonify({
                "status": "error",
                "message": "Please input valid number (exactly 10 digits required)"
            }), 400
        
        if not otp_code:
            return jsonify({
                "status": "error",
                "message": "Please input valid OTP (4-6 digits)"
            }), 400
        
        otp_code = re.sub(r'\D', '', otp_code)
        
        if len(otp_code) < 4:
            return jsonify({
                "status": "error",
                "message": "Please input valid OTP (4-6 digits)"
            }), 400
        
        result = run_async_task(verify_otp_and_save_session(phone_number, otp_code))
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"Error in OTP verification: {e}")
        return jsonify({
            "status": "error",
            "message": f"Internal server error: {str(e)}"
        }), 500

@app.route('/logout', methods=['GET'])
def logout():
    """Logout endpoint"""
    try:
        phone_number = request.args.get('num', '').strip()
        
        if not phone_number:
            return jsonify({
                "status": "error",
                "message": "Phone number required"
            }), 400
        
        phone_number = re.sub(r'\D', '', phone_number)
        
        if len(phone_number) != 10:
            return jsonify({
                "status": "error",
                "message": "Please input valid number (10 digits required)"
            }), 400
        
        formatted_phone = format_phone_for_telegram(phone_number)
        
        if formatted_phone in user_clients:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(user_clients[formatted_phone].disconnect())
                loop.close()
            except:
                pass
            del user_clients[formatted_phone]
        
        if delete_session(formatted_phone):
            return jsonify({
                "status": "success",
                "message": "Logged out successfully. Session deleted."
            })
        else:
            return jsonify({
                "status": "error",
                "message": "No active session found"
            }), 404
            
    except Exception as e:
        logger.error(f"Error in logout: {e}")
        return jsonify({
            "status": "error",
            "message": f"Internal server error: {str(e)}"
        }), 500

@app.route('/check_session', methods=['GET'])
def check_session():
    """Check if session is valid"""
    try:
        phone_number = request.args.get('num', '').strip()
        
        if not phone_number:
            return jsonify({
                "status": "error",
                "message": "Phone number required"
            }), 400
        
        phone_number = re.sub(r'\D', '', phone_number)
        
        if len(phone_number) != 10:
            return jsonify({
                "status": "error",
                "message": "Please input valid number (10 digits required)"
            }), 400
        
        formatted_phone = format_phone_for_telegram(phone_number)
        
        session_string = get_session(formatted_phone)
        if not session_string:
            return jsonify({
                "status": "error",
                "message": "No session found",
                "session_active": False
            })
        
        result = run_async_task(get_user_client(phone_number))
        client, is_valid = result
        
        if is_valid:
            return jsonify({
                "status": "success",
                "message": "Session is valid",
                "session_active": True,
                "phone": formatted_phone
            })
        else:
            delete_session(formatted_phone)
            return jsonify({
                "status": "error",
                "message": "Session expired",
                "session_active": False
            })
            
    except Exception as e:
        logger.error(f"Error in check_session: {e}")
        return jsonify({
            "status": "error",
            "message": f"Internal server error: {str(e)}"
        }), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "ok",
        "timestamp": datetime.now().isoformat()
    })

@app.route('/', methods=['GET'])
def index():
    """Home page with API documentation"""
    return jsonify({
        "status": "ok",
        "message": "Telegram OSINT API",
        "base_url": "http://192.0.0.2:8080",
        "endpoints": {
            "login": "/login?num=7724809103",
            "verify_otp": "/login/otp?num=7724809103&otp=123456",
            "get_info": "/num?num=8815743146",
            "check_session": "/check_session?num=7724809103",
            "logout": "/logout?num=7724809103",
            "health": "/health"
        },
        "example": {
            "login": "http://192.0.0.2:8080/login?num=7724809103",
            "verify": "http://192.0.0.2:8080/login/otp?num=7724809103&otp=123456",
            "get_info": "http://192.0.0.2:8080/num?num=8815743146"
        }
    })

@app.errorhandler(404)
def not_found(error):
    return jsonify({
        "status": "error",
        "message": "Endpoint not found"
    }), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({
        "status": "error",
        "message": "Internal server error"
    }), 500

if __name__ == '__main__':
    # Initialize bot
    asyncio.run(initialize_bot())
    
    # Run Flask app
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)