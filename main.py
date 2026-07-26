import os
import json
import asyncio
import threading
import time
import sqlite3
import re
import logging
from datetime import datetime
from flask import Flask, request, jsonify
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

# ========== CONFIGURATION ==========
API_ID = int(os.environ.get('API_ID', '33032511'))  # अपना API_ID डालें
API_HASH = os.environ.get('API_HASH', '58d0bd6b23f6da7bde206f79866dbc4b')  # अपना API_HASH डालें
BOT_USERNAME = '@THE_UNKNOWN_OSINT_BOT'
DEFAULT_COUNTRY_CODE = '91'
REQUEST_TIMEOUT = 30
OTP_TIMEOUT = 120

# ========== LOGGING ==========
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ========== FLASK APP ==========
app = Flask(__name__)

# ========== DATABASE SETUP ==========
def init_db():
    conn = sqlite3.connect('sessions.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sessions
                 (phone TEXT PRIMARY KEY, session_string TEXT, created_at TIMESTAMP, last_used TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS pending_otp
                 (phone TEXT PRIMARY KEY, phone_code_hash TEXT, timestamp TIMESTAMP)''')
    conn.commit()
    conn.close()
init_db()

# ========== HELPER FUNCTIONS ==========
def format_phone(phone):
    phone = re.sub(r'\D', '', phone)
    if phone.startswith('0'): phone = phone[1:]
    if not phone.startswith('91') and len(phone) == 10:
        phone = DEFAULT_COUNTRY_CODE + phone
    if not phone.startswith('+'):
        phone = '+' + phone
    return phone

def save_session(phone, session_string):
    try:
        conn = sqlite3.connect('sessions.db')
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?)',
                  (phone, session_string, datetime.now().isoformat(), datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Save session error: {e}")
        return False

def get_session(phone):
    try:
        conn = sqlite3.connect('sessions.db')
        c = conn.cursor()
        c.execute('SELECT session_string FROM sessions WHERE phone = ?', (phone,))
        result = c.fetchone()
        conn.close()
        if result:
            # Update last_used
            conn = sqlite3.connect('sessions.db')
            c = conn.cursor()
            c.execute('UPDATE sessions SET last_used = ? WHERE phone = ?', (datetime.now().isoformat(), phone))
            conn.commit()
            conn.close()
            return result[0]
        return None
    except Exception as e:
        logger.error(f"Get session error: {e}")
        return None

def delete_session(phone):
    try:
        conn = sqlite3.connect('sessions.db')
        c = conn.cursor()
        c.execute('DELETE FROM sessions WHERE phone = ?', (phone,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Delete session error: {e}")
        return False

def save_pending_otp(phone, phone_code_hash):
    try:
        conn = sqlite3.connect('sessions.db')
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO pending_otp VALUES (?, ?, ?)',
                  (phone, phone_code_hash, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Save pending OTP error: {e}")
        return False

def get_pending_otp(phone):
    try:
        conn = sqlite3.connect('sessions.db')
        c = conn.cursor()
        c.execute('SELECT phone_code_hash, timestamp FROM pending_otp WHERE phone = ?', (phone,))
        result = c.fetchone()
        conn.close()
        return result
    except Exception as e:
        logger.error(f"Get pending OTP error: {e}")
        return None

def delete_pending_otp(phone):
    try:
        conn = sqlite3.connect('sessions.db')
        c = conn.cursor()
        c.execute('DELETE FROM pending_otp WHERE phone = ?', (phone,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Delete pending OTP error: {e}")
        return False

# ========== ASYNC TELEGRAM FUNCTIONS ==========
class TelegramClientManager:
    def __init__(self):
        self.bot_client = None
        self.user_clients = {}
        self.pending_otp_cache = {}
        self.loop = None
        self.thread = None
        self.running = False

    def start(self):
        """Start the async loop in a separate thread"""
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        # Wait for loop to start
        while not self.loop:
            time.sleep(0.1)
        logger.info("Telegram client manager started")

    def _run_loop(self):
        """Run the asyncio event loop"""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._init_bot())
        self.loop.run_forever()

    async def _init_bot(self):
        """Initialize bot client"""
        try:
            self.bot_client = TelegramClient('bot_session', API_ID, API_HASH)
            await self.bot_client.start()
            logger.info("Bot client initialized")
        except Exception as e:
            logger.error(f"Bot init error: {e}")

    def run_async(self, coro):
        """Run async function in the event loop"""
        if not self.loop:
            raise Exception("Event loop not running")
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=REQUEST_TIMEOUT + 10)

    async def _get_user_client(self, phone):
        """Get or create user client"""
        formatted_phone = format_phone(phone)
        
        if formatted_phone in self.user_clients:
            client = self.user_clients[formatted_phone]
            try:
                await client.get_me()
                return client, True
            except:
                del self.user_clients[formatted_phone]
        
        session_string = get_session(formatted_phone)
        if session_string:
            try:
                client = TelegramClient(formatted_phone, API_ID, API_HASH)
                await client.connect()
                await client.start()
                self.user_clients[formatted_phone] = client
                return client, True
            except Exception as e:
                logger.warning(f"Session reconnect failed: {e}")
                delete_session(formatted_phone)
        return None, False

    async def _send_command(self, phone, client, command):
        """Send command to bot"""
        try:
            bot_entity = await client.get_entity(BOT_USERNAME)
            await client.send_message(bot_entity, command)
            logger.info(f"Command sent: {command}")
            
            start_time = time.time()
            response_json = None
            
            @client.on(events.NewMessage(from_users=bot_entity))
            async def handler(event):
                nonlocal response_json
                if event.message.text:
                    text = event.message.text
                    if "{" in text:
                        try:
                            json_start = text.find("{")
                            json_end = text.rfind("}") + 1
                            if json_start != -1 and json_end != -1:
                                response_json = json.loads(text[json_start:json_end])
                        except:
                            pass
            
            while time.time() - start_time < REQUEST_TIMEOUT:
                await asyncio.sleep(0.5)
                if response_json:
                    return response_json
            
            return {"status": "error", "message": "No valid response received"}
            
        except FloodWaitError as e:
            return {"status": "error", "message": f"Please wait {e.seconds} seconds"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def _handle_login(self, phone):
        """Handle login"""
        formatted_phone = format_phone(phone)
        
        # Check existing session
        session_string = get_session(formatted_phone)
        if session_string:
            client = TelegramClient(formatted_phone, API_ID, API_HASH)
            await client.connect()
            try:
                await client.start()
                self.user_clients[formatted_phone] = client
                return {"status": "success", "message": "Already logged in", "session_active": True}
            except:
                delete_session(formatted_phone)
        
        # New login
        client = TelegramClient(formatted_phone, API_ID, API_HASH)
        await client.connect()
        
        try:
            result = await client.send_code_request(formatted_phone)
            phone_code_hash = str(result.phone_code_hash) if result.phone_code_hash else None
            
            self.user_clients[formatted_phone] = client
            self.pending_otp_cache[formatted_phone] = {
                'client': client,
                'phone_code_hash': phone_code_hash,
                'timestamp': time.time()
            }
            save_pending_otp(formatted_phone, phone_code_hash)
            
            return {"status": "success", "message": "OTP sent successfully", "phone": formatted_phone}
            
        except Exception as e:
            await client.disconnect()
            if formatted_phone in self.user_clients:
                del self.user_clients[formatted_phone]
            return {"status": "error", "message": str(e)}

    async def _verify_otp(self, phone, otp_code):
        """Verify OTP"""
        formatted_phone = format_phone(phone)
        
        otp_data = self.pending_otp_cache.get(formatted_phone)
        if not otp_data:
            db_data = get_pending_otp(formatted_phone)
            if not db_data:
                return {"status": "error", "message": "No pending OTP. Please login first."}
            phone_code_hash, timestamp_str = db_data
            timestamp = datetime.fromisoformat(timestamp_str).timestamp()
            client = self.user_clients.get(formatted_phone)
            if not client:
                return {"status": "error", "message": "Client not found. Login again."}
            otp_data = {'client': client, 'phone_code_hash': phone_code_hash, 'timestamp': timestamp}
        else:
            client = otp_data['client']
        
        if time.time() - otp_data['timestamp'] > OTP_TIMEOUT:
            await client.disconnect()
            self.user_clients.pop(formatted_phone, None)
            self.pending_otp_cache.pop(formatted_phone, None)
            delete_pending_otp(formatted_phone)
            return {"status": "error", "message": "OTP expired. Request new OTP."}
        
        try:
            await client.sign_in(formatted_phone, otp_code, phone_code_hash=otp_data.get('phone_code_hash'))
            session_string = client.session.save()
            
            if save_session(formatted_phone, session_string):
                self.pending_otp_cache.pop(formatted_phone, None)
                delete_pending_otp(formatted_phone)
                return {"status": "success", "message": "Login successful. Session saved."}
            else:
                return {"status": "error", "message": "Failed to save session"}
                
        except Exception as e:
            if "password" in str(e).lower():
                return {"status": "error", "message": "2FA enabled. Need password support."}
            return {"status": "error", "message": f"Invalid OTP: {str(e)}"}

# ========== INITIALIZE TELEGRAM MANAGER ==========
telegram_manager = TelegramClientManager()
telegram_manager.start()

# ========== FLASK ROUTES ==========
@app.route('/num', methods=['GET'])
def get_number_info():
    phone = request.args.get('num', '').strip()
    if not phone or len(phone) != 10:
        return jsonify({"status": "error", "message": "Please input valid number (10 digits required)"}), 400
    
    phone = re.sub(r'\D', '', phone)
    formatted_phone = format_phone(phone)
    
    if not get_session(formatted_phone):
        return jsonify({"status": "error", "message": "No active session. Login first with /login?num=7724809103"}), 403
    
    try:
        client, is_valid = telegram_manager.run_async(telegram_manager._get_user_client(phone))
        if not is_valid:
            return jsonify({"status": "error", "message": "Session expired. Login again."}), 403
        
        response = telegram_manager.run_async(telegram_manager._send_command(phone, client, f"/num {phone}"))
        if not response or response.get('status') == 'error':
            return jsonify({"status": "error", "message": response.get('message', 'No information found')}), 404
        
        if response.get('count', 0) == 0:
            return jsonify({"status": "error", "message": "No information found for this number", "query": phone}), 404
        
        return jsonify(response)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/login', methods=['GET'])
def login():
    phone = request.args.get('num', '').strip()
    if not phone or len(phone) != 10:
        return jsonify({"status": "error", "message": "Please input valid number (10 digits required)"}), 400
    
    phone = re.sub(r'\D', '', phone)
    try:
        result = telegram_manager.run_async(telegram_manager._handle_login(phone))
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/login/otp', methods=['GET'])
def verify_otp():
    phone = request.args.get('num', '').strip()
    otp = request.args.get('otp', '').strip()
    
    if not phone or len(phone) != 10:
        return jsonify({"status": "error", "message": "Please input valid number (10 digits required)"}), 400
    
    if not otp or len(otp) < 4:
        return jsonify({"status": "error", "message": "Please input valid OTP (4-6 digits)"}), 400
    
    phone = re.sub(r'\D', '', phone)
    otp = re.sub(r'\D', '', otp)
    
    try:
        result = telegram_manager.run_async(telegram_manager._verify_otp(phone, otp))
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/logout', methods=['GET'])
def logout():
    phone = request.args.get('num', '').strip()
    if not phone or len(phone) != 10:
        return jsonify({"status": "error", "message": "Please input valid number (10 digits required)"}), 400
    
    phone = re.sub(r'\D', '', phone)
    formatted_phone = format_phone(phone)
    
    if formatted_phone in telegram_manager.user_clients:
        try:
            telegram_manager.run_async(telegram_manager.user_clients[formatted_phone].disconnect())
        except:
            pass
        del telegram_manager.user_clients[formatted_phone]
    
    if delete_session(formatted_phone):
        return jsonify({"status": "success", "message": "Logged out successfully"})
    return jsonify({"status": "error", "message": "No active session"}), 404

@app.route('/check_session', methods=['GET'])
def check_session():
    phone = request.args.get('num', '').strip()
    if not phone or len(phone) != 10:
        return jsonify({"status": "error", "message": "Please input valid number (10 digits required)"}), 400
    
    phone = re.sub(r'\D', '', phone)
    formatted_phone = format_phone(phone)
    
    if not get_session(formatted_phone):
        return jsonify({"status": "error", "message": "No session found", "session_active": False})
    
    try:
        client, is_valid = telegram_manager.run_async(telegram_manager._get_user_client(phone))
        if is_valid:
            return jsonify({"status": "success", "message": "Session is valid", "session_active": True})
        else:
            delete_session(formatted_phone)
            return jsonify({"status": "error", "message": "Session expired", "session_active": False})
    except:
        return jsonify({"status": "error", "message": "Session check failed", "session_active": False})

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

@app.route('/', methods=['GET'])
def index():
    return jsonify({
        "status": "ok",
        "message": "Telegram OSINT API",
        "endpoints": {
            "login": "/login?num=7724809103",
            "verify_otp": "/login/otp?num=7724809103&otp=123456",
            "get_info": "/num?num=8815743146",
            "check_session": "/check_session?num=7724809103",
            "logout": "/logout?num=7724809103"
        }
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)