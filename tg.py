import os
import re
import json
import asyncio
import zipfile
import tempfile
import shutil
from datetime import datetime, timezone
from typing import Dict, List, Optional
import logging

from telethon import TelegramClient, events, Button
from telethon.errors import SessionPasswordNeededError, PhoneNumberInvalidError, ReplyMarkupInvalidError
from telethon.tl.functions.account import GetPasswordRequest, GetAuthorizationsRequest, ResetAuthorizationRequest
from telethon.tl.functions.messages import GetHistoryRequest
from telethon.tl.functions.auth import SendCodeRequest
from telethon.tl.types import User

# Konfigurasi logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class SessionManager:
    def __init__(self, bot_token: str, api_id: int, api_hash: str, admin_ids: List[int]):
        self.bot_token = bot_token
        self.api_id = api_id
        self.api_hash = api_hash
        self.admin_ids = admin_ids
        self.bot = TelegramClient('bot', api_id, api_hash)
        self.valid_sessions: Dict[str, dict] = {}
        self.temp_dir = tempfile.mkdtemp()
        self.sessions_dir = os.path.join(os.getcwd(), 'validated_sessions')
        self.accounts_file = os.path.join(self.sessions_dir, 'accounts.json')
        
        # Buat folder untuk session yang sudah divalidasi
        os.makedirs(self.sessions_dir, exist_ok=True)
        
        # Load session yang sudah tersimpan
        self.load_saved_sessions()
        
    def is_admin(self, user_id: int) -> bool:
        """Cek apakah user adalah admin"""
        return user_id in self.admin_ids
    
    async def check_admin_access(self, event):
        """Cek akses admin dan kirim pesan jika bukan admin"""
        if not self.is_admin(event.sender_id):
            await event.respond(
                "🚫 **ACCESS DENIED**\n\n"
                "┌─────────────────────────┐\n"
                "│  ⚠️  ADMIN ONLY BOT  ⚠️  │\n"
                "└─────────────────────────┘\n\n"
                "🔒 Bot ini hanya dapat digunakan oleh administrator yang berwenang.\n"
                "📞 Hubungi admin untuk mendapatkan akses."
            )
            return False
        return True
    
    def load_saved_sessions(self):
        """Load session yang sudah disimpan"""
        try:
            if os.path.exists(self.accounts_file):
                with open(self.accounts_file, 'r') as f:
                    accounts_data = json.load(f)
                
                for user_id, data in accounts_data.items():
                    session_path = os.path.join(self.sessions_dir, f"{user_id}.session")
                    if os.path.exists(session_path):
                        self.valid_sessions[user_id] = {
                            'session_path': session_path,
                            'phone': data['phone'],
                            'username': data['username'],
                            'user_id': user_id,
                            'first_name': data.get('first_name', 'Unknown'),
                            'last_name': data.get('last_name', ''),
                            'validated_at': data.get('validated_at', '')
                        }
                
                logger.info(f"Loaded {len(self.valid_sessions)} saved sessions")
        except Exception as e:
            logger.error(f"Error loading saved sessions: {e}")
    
    def save_sessions(self):
        """Simpan session yang sudah divalidasi"""
        try:
            accounts_data = {}
            for user_id, data in self.valid_sessions.items():
                accounts_data[user_id] = {
                    'phone': data['phone'],
                    'username': data['username'],
                    'first_name': data.get('first_name', 'Unknown'),
                    'last_name': data.get('last_name', ''),
                    'validated_at': data.get('validated_at', datetime.now().isoformat())
                }
            
            with open(self.accounts_file, 'w') as f:
                json.dump(accounts_data, f, indent=2)
            
            logger.info(f"Saved {len(accounts_data)} sessions")
        except Exception as e:
            logger.error(f"Error saving sessions: {e}")
        
    async def start_bot(self):
        """Memulai bot"""
        await self.bot.start(bot_token=self.bot_token)
        
        @self.bot.on(events.NewMessage(pattern='/start'))
        async def start_handler(event):
            if not await self.check_admin_access(event):
                return
                
            welcome_text = (
                "🤖 **TELEGRAM SESSION MANAGER**\n\n"
                "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                "┃  🔧  **FITUR TERSEDIA**  🔧  ┃\n"
                "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                "📁 **Upload ZIP** → Auto validasi sessions\n"
                "📱 **Kelola Akun** → Manajemen lengkap\n"
                "🔐 **Get OTP** → Dari Telegram service\n"
                "🗑️ **Clear Chat** → Hapus semua chat\n"
                "🚪 **Leave Groups** → Keluar grup (kecuali admin)\n"
                "📱 **Manage Sessions** → Kontrol perangkat\n\n"
                f"💾 **Sessions Tersimpan:** `{len(self.valid_sessions)}`"
            )
            
            buttons = [
                [Button.inline("📱 KELOLA AKUN", b"show_accounts")],
                [Button.inline("ℹ️ INFO BOT", b"bot_info")]
            ]
            
            await event.respond(welcome_text, buttons=buttons)
        
        @self.bot.on(events.NewMessage(pattern='/akun'))
        async def accounts_handler(event):
            if not await self.check_admin_access(event):
                return
            await self.show_accounts(event)
        
        @self.bot.on(events.NewMessage)
        async def file_handler(event):
            if not await self.check_admin_access(event):
                return
            if event.document and event.document.mime_type == 'application/zip':
                await self.process_zip_file(event)
        
        @self.bot.on(events.CallbackQuery)
        async def callback_handler(event):
            if not self.is_admin(event.sender_id):
                await event.answer("🚫 ACCESS DENIED - Admin Only!", alert=True)
                return
            
            try:
                data = event.data.decode('utf-8')
                
                if data == "show_accounts":
                    await self.show_accounts(event)
                elif data == "bot_info":
                    await self.show_bot_info(event)
                elif data.startswith("acc_"):
                    user_id = data.split("_")[1]
                    await self.show_account_info(event, user_id)
                elif data.startswith("getotp_"):
                    user_id = data.split("_")[1]
                    await self.get_otp(event, user_id)
                elif data.startswith("clear_"):
                    user_id = data.split("_")[1]
                    await self.clear_chats(event, user_id)
                elif data.startswith("sessions_"):
                    user_id = data.split("_")[1]
                    await self.check_sessions(event, user_id)
                elif data.startswith("killall_"):
                    user_id = data.split("_")[1]
                    await self.kill_all_sessions(event, user_id)
                elif data.startswith("leavegroups_"):
                    user_id = data.split("_")[1]
                    await self.leave_groups(event, user_id)
                elif data == "back_accounts":
                    await self.show_accounts(event)
                elif data == "back_main":
                    await start_handler(event)
            except Exception as e:
                logger.error(f"Error in callback handler: {e}")
                try:
                    await event.answer(f"❌ Error: {str(e)}", alert=True)
                except:
                    pass
        
        logger.info("Bot started successfully!")
        
    async def show_bot_info(self, event):
        """Menampilkan info bot"""
        info_text = (
            "ℹ️ **BOT INFORMATION**\n\n"
            "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            "┃     📊  **STATISTIK**  📊     ┃\n"
            "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
            f"💾 **Total Sessions:** `{len(self.valid_sessions)}`\n"
            f"🔐 **Admin Users:** `{len(self.admin_ids)}`\n"
            f"📁 **Storage Path:** `{self.sessions_dir}`\n\n"
            "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            "┃      🛠️  **FEATURES**  🛠️      ┃\n"
            "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
            "✅ Auto Session Validation\n"
            "✅ 2FA Detection & Skip\n"
            "✅ Persistent Storage\n"
            "✅ OTP Extraction\n"
            "✅ Bulk Group Management\n"
            "✅ Device Session Control\n"
            "✅ Admin-Only Access"
        )
        
        buttons = [[Button.inline("⬅️ KEMBALI", b"show_accounts")]]
        
        try:
            await event.edit(info_text, buttons=buttons)
        except ReplyMarkupInvalidError:
            await event.respond(info_text, buttons=buttons)
        
    async def process_zip_file(self, event):
        """Memproses file ZIP yang dikirim"""
        status_msg = await event.respond(
            "🔄 **PROCESSING ZIP FILE**\n\n"
            "⏳ Downloading and extracting..."
        )
        
        try:
            # Download file
            file_path = await event.download_media(self.temp_dir)
            
            await status_msg.edit(
                "🔄 **PROCESSING ZIP FILE**\n\n"
                "📂 Searching for sessions folder..."
            )
            
            # Extract ZIP
            extract_dir = os.path.join(self.temp_dir, "extracted")
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
            
            # Cari folder sessions/users
            sessions_path = None
            for root, dirs, files in os.walk(extract_dir):
                if root.endswith('sessions/users') or root.endswith('sessions\\users'):
                    sessions_path = root
                    break
            
            if not sessions_path:
                await status_msg.edit(
                    "❌ **EXTRACTION FAILED**\n\n"
                    "📁 Folder `sessions/users/` tidak ditemukan dalam ZIP\n\n"
                    "🔍 **Structure Required:**\n"
                    "```\n"
                    "your_zip.zip\n"
                    "└── sessions/\n"
                    "    └── users/\n"
                    "        ├── file1.session\n"
                    "        ├── file2.session\n"
                    "        └── ...\n"
                    "```"
                )
                return
            
            # Proses session files
            session_files = [f for f in os.listdir(sessions_path) if f.endswith('.session')]
            
            if not session_files:
                await status_msg.edit(
                    "❌ **NO SESSIONS FOUND**\n\n"
                    "📁 Tidak ada file `.session` ditemukan di folder users"
                )
                return
            
            await status_msg.edit(
                f"🔍 **VALIDATING SESSIONS**\n\n"
                f"📁 Found: `{len(session_files)}` session files\n"
                f"⏳ Validating... (0/{len(session_files)})"
            )
            
            valid_count = 0
            skipped_2fa = 0
            invalid_count = 0
            
            for i, session_file in enumerate(session_files, 1):
                session_path = os.path.join(sessions_path, session_file)
                result = await self.validate_session(session_path)
                
                # Update progress
                if i % 5 == 0 or i == len(session_files):
                    try:
                        await status_msg.edit(
                            f"🔍 **VALIDATING SESSIONS**\n\n"
                            f"📁 Total: `{len(session_files)}` files\n"
                            f"⏳ Progress: `{i}/{len(session_files)}`\n\n"
                            f"✅ Valid: `{valid_count}`\n"
                            f"🔒 2FA Skipped: `{skipped_2fa}`\n"
                            f"❌ Invalid: `{invalid_count}`"
                        )
                    except:
                        pass
                
                if result['valid'] and not result['has_2fa']:
                    # Copy session ke directory kerja
                    work_session_path = os.path.join(self.sessions_dir, f"{result['user_id']}.session")
                    shutil.copy2(session_path, work_session_path)
                    
                    self.valid_sessions[result['user_id']] = {
                        'session_path': work_session_path,
                        'phone': result['phone'],
                        'username': result['username'],
                        'user_id': result['user_id'],
                        'first_name': result.get('first_name', 'Unknown'),
                        'last_name': result.get('last_name', ''),
                        'validated_at': datetime.now().isoformat()
                    }
                    valid_count += 1
                elif result['valid'] and result['has_2fa']:
                    skipped_2fa += 1
                else:
                    invalid_count += 1
            
            # Simpan data session
            self.save_sessions()
            
            final_text = (
                "✅ **VALIDATION COMPLETE**\n\n"
                "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                "┃        📊  **RESULTS**  📊       ┃\n"
                "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                f"📁 **Total Files:** `{len(session_files)}`\n"
                f"✅ **Valid Sessions:** `{valid_count}`\n"
                f"🔒 **2FA Skipped:** `{skipped_2fa}`\n"
                f"❌ **Invalid:** `{invalid_count}`\n\n"
                f"💾 **Saved to:** `{self.sessions_dir}`\n\n"
                "🎉 Sessions berhasil disimpan dan siap digunakan!"
            )
            
            buttons = [
                [Button.inline("📱 LIHAT AKUN", b"show_accounts")],
                [Button.inline("⬅️ MAIN MENU", b"back_main")]
            ]
            
            await status_msg.edit(final_text, buttons=buttons)
            
        except Exception as e:
            error_text = (
                "❌ **PROCESSING ERROR**\n\n"
                f"🚨 **Error:** `{str(e)}`\n\n"
                "💡 **Troubleshooting:**\n"
                "• Pastikan ZIP tidak corrupt\n"
                "• Periksa struktur folder\n"
                "• Coba upload ulang"
            )
            await status_msg.edit(error_text)
            logger.error(f"Error processing ZIP: {e}")
        finally:
            # Cleanup
            try:
                if 'file_path' in locals():
                    os.remove(file_path)
                if 'extract_dir' in locals():
                    shutil.rmtree(extract_dir)
            except:
                pass
    
    async def validate_session(self, session_path: str) -> dict:
        """Validasi session file"""
        try:
            session_name = os.path.splitext(os.path.basename(session_path))[0]
            client = TelegramClient(session_path.replace('.session', ''), self.api_id, self.api_hash)
            
            await client.connect()
            
            if not await client.is_user_authorized():
                await client.disconnect()
                return {'valid': False, 'has_2fa': False, 'user_id': None, 'phone': None, 'username': None}
            
            # Cek 2FA
            has_2fa = False
            try:
                me = await client.get_me()
            except SessionPasswordNeededError:
                has_2fa = True
                await client.disconnect()
                return {'valid': True, 'has_2fa': True, 'user_id': None, 'phone': None, 'username': None}
            
            await client.disconnect()
            
            return {
                'valid': True,
                'has_2fa': False,
                'user_id': str(me.id),
                'phone': me.phone,
                'username': me.username or 'None',
                'first_name': me.first_name or 'Unknown',
                'last_name': me.last_name or ''
            }
            
        except Exception as e:
            logger.error(f"Error validating session {session_path}: {e}")
            return {'valid': False, 'has_2fa': False, 'user_id': None, 'phone': None, 'username': None}
    
    async def show_accounts(self, event):
        """Menampilkan daftar akun"""
        if not self.valid_sessions:
            text = (
                "📱 **ACCOUNT MANAGER**\n\n"
                "❌ **No Sessions Available**\n\n"
                "🔄 Upload ZIP file containing sessions to get started\n\n"
                "📁 **Required Structure:**\n"
                "```\n"
                "your_file.zip\n"
                "└── sessions/\n"
                "    └── users/\n"
                "        └── *.session\n"
                "```"
            )
            buttons = [[Button.inline("⬅️ MAIN MENU", b"back_main")]]
        else:
            # Urutkan berdasarkan user_id (terendah ke tertinggi)
            sorted_accounts = sorted(self.valid_sessions.items(), key=lambda x: int(x[0]))
            
            text = (
                "📱 **ACCOUNT MANAGER**\n\n"
                "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                f"┃     📊  {len(sorted_accounts)} ACCOUNTS READY  📊     ┃\n"
                "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
            )
            
            buttons = []
            
            for i, (user_id, data) in enumerate(sorted_accounts[:20], 1):  # Limit 20 untuk button
                phone = data['phone'] or 'Unknown'
                username = data['username'] or 'None'
                first_name = data.get('first_name', 'Unknown')
                
                # Format display name
                display_name = f"{first_name}"
                if len(display_name) > 15:
                    display_name = display_name[:15] + "..."
                
                text += f"**{i:02d}.** `{phone}` • @{username}\n"
                text += f"     👤 {display_name} • ID: `{user_id}`\n\n"
                
                buttons.append([Button.inline(f"📞 {phone}", f"acc_{user_id}".encode())])
            
            if len(sorted_accounts) > 20:
                text += f"... dan {len(sorted_accounts) - 20} akun lainnya"
            
            buttons.append([Button.inline("⬅️ MAIN MENU", b"back_main")])
        
        try:
            if hasattr(event, 'edit'):
                await event.edit(text, buttons=buttons)
            else:
                await event.respond(text, buttons=buttons)
        except ReplyMarkupInvalidError:
            # Fallback jika button error
            await event.respond(text)
    
    async def show_account_info(self, event, user_id: str):
        """Menampilkan informasi detail akun"""
        if user_id not in self.valid_sessions:
            await event.answer("❌ Akun tidak ditemukan", alert=True)
            return
        
        loading_text = (
            "🔄 **LOADING ACCOUNT INFO**\n\n"
            "⏳ Connecting to Telegram...\n"
            "📊 Fetching account details..."
        )
        
        try:
            await event.edit(loading_text)
        except:
            await event.respond(loading_text)
        
        try:
            session_data = self.valid_sessions[user_id]
            session_path = session_data['session_path'].replace('.session', '')
            
            client = TelegramClient(session_path, self.api_id, self.api_hash)
            await client.connect()
            
            if not await client.is_user_authorized():
                await event.edit("❌ **Session Invalid**\n\nSession sudah tidak aktif")
                await client.disconnect()
                return
            
            me = await client.get_me()
            
            # Hitung grup yang dimiliki/admin
            admin_groups = 0
            total_groups = 0
            
            async for dialog in client.iter_dialogs():
                if dialog.is_group or dialog.is_channel:
                    total_groups += 1
                    try:
                        permissions = await client.get_permissions(dialog.entity, me)
                        if permissions.is_admin or permissions.is_creator:
                            admin_groups += 1
                    except:
                        continue
            
            await client.disconnect()
            
            # Format tanggal validasi
            validated_at = session_data.get('validated_at', '')
            if validated_at:
                try:
                    date_obj = datetime.fromisoformat(validated_at.replace('Z', '+00:00'))
                    validated_str = date_obj.strftime('%d/%m/%Y %H:%M')
                except:
                    validated_str = 'Unknown'
            else:
                validated_str = 'Unknown'
            
            full_name = f"{me.first_name or ''} {me.last_name or ''}".strip()
            
            text = (
                f"👤 **ACCOUNT DETAILS**\n\n"
                "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                "┃       📋  **INFO**  📋        ┃\n"
                "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                f"📞 **Phone:** `{me.phone or 'Unknown'}`\n"
                f"🆔 **Telegram ID:** `{me.id}`\n"
                f"👤 **Name:** `{full_name or 'Unknown'}`\n"
                f"🔗 **Username:** @{me.username or 'None'}\n\n"
                "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                "┃      📊  **STATS**  📊       ┃\n"
                "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                f"👑 **Admin Groups:** `{admin_groups}`\n"
                f"💬 **Total Groups:** `{total_groups}`\n"
                f"✅ **Validated:** `{validated_str}`\n\n"
                "🔧 **Available Actions:**"
            )
            
            buttons = [
                [Button.inline("📨 GET OTP", f"getotp_{user_id}".encode()),
                 Button.inline("🗑️ CLEAR CHAT", f"clear_{user_id}".encode())],
                [Button.inline("🚪 LEAVE GROUPS", f"leavegroups_{user_id}".encode()),
                 Button.inline("📱 SESSIONS", f"sessions_{user_id}".encode())],
                [Button.inline("⬅️ BACK TO LIST", b"back_accounts")]
            ]
            
            await event.edit(text, buttons=buttons)
            
        except Exception as e:
            error_text = (
                "❌ **ERROR LOADING ACCOUNT**\n\n"
                f"🚨 **Error:** `{str(e)}`\n\n"
                "💡 Session mungkin sudah expired atau invalid"
            )
            try:
                await event.edit(error_text)
            except:
                await event.respond(error_text)
    
    async def get_telegram_messages(self, client, get_latest_only=False):
        """Get messages from Telegram service number +42777 with OTP extraction"""
        try:
            # Cari Telegram service
            telegram_service = None
            try:
                telegram_service = await client.get_entity("+42777")
            except:
                try:
                    telegram_service = await client.get_entity("Telegram")
                except:
                    dialogs = await client.get_dialogs()
                    for dialog in dialogs:
                        if (hasattr(dialog.entity, 'phone') and dialog.entity.phone == "+42777") or \
                           dialog.name == "Telegram":
                            telegram_service = dialog.entity
                            break
            
            if not telegram_service:
                return ["📭 Chat dengan layanan Telegram (+42777) tidak ditemukan"]
            
            # Get messages
            limit = 3 if get_latest_only else 10
            service_messages = await client.get_messages(telegram_service, limit=limit)
            
            # Pattern untuk extract OTP
            otp_patterns = [
                r'Your login code:?\s*(\d{5,6})',
                r'Kode masuk Anda:?\s*(\d{5,6})',
                r'Your code:?\s*(\d{5,6})',
                r'Kode:?\s*(\d{5,6})',
                r'code:?\s*(\d{5,6})',
                r'(\d{5,6})'
            ]
            
            results = []
            
            for msg in service_messages:
                if msg and msg.message:
                    message_content = msg.message
                    
                    # Format waktu dengan timezone handling
                    try:
                        if msg.date.tzinfo is None:
                            msg_time = msg.date.replace(tzinfo=timezone.utc)
                        else:
                            msg_time = msg.date
                        
                        time_str = msg_time.strftime('%d/%m/%Y %H:%M UTC')
                    except:
                        time_str = 'Unknown time'
                    
                    # Cari OTP
                    otp_found = False
                    for pattern in otp_patterns:
                        otp_match = re.search(pattern, message_content, re.IGNORECASE)
                        if otp_match:
                            otp_code = otp_match.group(1)
                            
                            # Verifikasi ini pesan OTP
                            if pattern == r'(\d{5,6})':
                                otp_terms = ['code', 'telegram', 'login', 'verification', 
                                           'kode', 'masuk', 'verifikasi']
                                if not any(term.lower() in message_content.lower() for term in otp_terms):
                                    continue
                            
                            result_text = (
                                f"🔐 **OTP CODE:** `{otp_code}`\n"
                                f"⏰ **Time:** {time_str}"
                            )
                            
                            results.append(result_text)
                            otp_found = True
                            break
                    
                    if otp_found and get_latest_only:
                        break
            
            return results if results else ["📭 Tidak ada pesan OTP ditemukan"]
            
        except Exception as e:
            logger.error(f"Error getting Telegram messages: {e}")
            return [f"❌ Error: {str(e)}"]
    
    async def get_otp(self, event, user_id: str):
        """Mendapatkan OTP dari +42777"""
        loading_text = (
            "🔍 **SEARCHING OTP**\n\n"
            "⏳ Connecting to account...\n"
            "📨 Fetching messages from Telegram service..."
        )
        
        try:
            await event.edit(loading_text)
        except:
            await event.respond(loading_text)
        
        try:
            session_data = self.valid_sessions[user_id]
            session_path = session_data['session_path'].replace('.session', '')
            
            client = TelegramClient(session_path, self.api_id, self.api_hash)
            await client.connect()
            
            otp_messages = await self.get_telegram_messages(client, get_latest_only=True)
            
            await client.disconnect()
            
            if otp_messages and otp_messages[0] != "📭 Chat dengan layanan Telegram (+42777) tidak ditemukan":
                text = (
                    "📨 **OTP RETRIEVED**\n\n"
                    f"{otp_messages[0]}\n\n"
                    "💡 **Quick Copy:** Tap the code to copy"
                )
            else:
                text = (
                    "📭 **NO OTP FOUND**\n\n"
                    "❌ Tidak ada pesan OTP dari layanan Telegram (+42777)\n\n"
                    "💡 **Tips:**\n"
                    "• Pastikan ada pesan dari +42777\n"
                    "• Coba login ke Telegram untuk dapat OTP\n"
                    "• Periksa folder chat lainnya"
                )
            
            buttons = [[Button.inline("⬅️ BACK", f"acc_{user_id}".encode())]]
            await event.edit(text, buttons=buttons)
            
        except Exception as e:
            error_text = (
                "❌ **OTP FETCH ERROR**\n\n"
                f"🚨 **Error:** `{str(e)}`\n\n"
                "💡 Kemungkinan session expired atau tidak ada akses ke chat +42777"
            )
            buttons = [[Button.inline("⬅️ BACK", f"acc_{user_id}".encode())]]
            try:
                await event.edit(error_text, buttons=buttons)
            except:
                await event.respond(error_text, buttons=buttons)
    
    async def clear_chats(self, event, user_id: str):
        """Menghapus semua chat"""
        loading_text = (
            "🗑️ **CLEARING CHATS**\n\n"
            "⏳ Connecting to account...\n"
            "📊 Scanning private chats..."
        )
        
        try:
            await event.edit(loading_text)
        except:
            await event.respond(loading_text)
        
        try:
            session_data = self.valid_sessions[user_id]
            session_path = session_data['session_path'].replace('.session', '')
            
            client = TelegramClient(session_path, self.api_id, self.api_hash)
            await client.connect()
            
            cleared_count = 0
            total_chats = 0
            
            # Count total private chats first
            async for dialog in client.iter_dialogs():
                if dialog.is_user:
                    total_chats += 1
            
            # Update status
            try:
                await event.edit(
                    "🗑️ **CLEARING CHATS**\n\n"
                    f"📊 Found {total_chats} private chats\n"
                    "🔄 Deleting conversations..."
                )
            except:
                pass
            
            # Clear chats
            async for dialog in client.iter_dialogs():
                if dialog.is_user:
                    try:
                        await client.delete_dialog(dialog.entity)
                        cleared_count += 1
                        
                        # Update progress every 10 deletions
                        if cleared_count % 10 == 0:
                            try:
                                await event.edit(
                                    "🗑️ **CLEARING CHATS**\n\n"
                                    f"📊 Total: {total_chats} chats\n"
                                    f"✅ Cleared: {cleared_count}\n"
                                    f"⏳ Remaining: {total_chats - cleared_count}"
                                )
                            except:
                                pass
                        
                        # Small delay to avoid flood limits
                        await asyncio.sleep(0.1)
                    except Exception as e:
                        logger.error(f"Error deleting chat: {e}")
                        continue
            
            await client.disconnect()
            
            result_text = (
                "✅ **CHAT CLEARING COMPLETE**\n\n"
                "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                "┃       📊  **RESULTS**  📊       ┃\n"
                "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                f"🗑️ **Chats Cleared:** `{cleared_count}`\n"
                f"📊 **Total Found:** `{total_chats}`\n"
                f"📱 **Success Rate:** `{round((cleared_count/total_chats)*100 if total_chats > 0 else 0)}%`\n\n"
                "🎉 Private chats berhasil dibersihkan!"
            )
            
            buttons = [[Button.inline("⬅️ BACK", f"acc_{user_id}".encode())]]
            await event.edit(result_text, buttons=buttons)
            
        except Exception as e:
            error_text = (
                "❌ **CHAT CLEARING ERROR**\n\n"
                f"🚨 **Error:** `{str(e)}`\n\n"
                f"✅ **Cleared:** `{cleared_count}` chats before error\n\n"
                "💡 Some chats may have been cleared successfully"
            )
            buttons = [[Button.inline("⬅️ BACK", f"acc_{user_id}".encode())]]
            try:
                await event.edit(error_text, buttons=buttons)
            except:
                await event.respond(error_text, buttons=buttons)
    
    async def leave_groups(self, event, user_id: str):
        """Keluar dari semua grup kecuali yang dia admin/owner"""
        loading_text = (
            "🚪 **LEAVING GROUPS**\n\n"
            "⏳ Connecting to account...\n"
            "📊 Analyzing group memberships..."
        )
        
        try:
            await event.edit(loading_text)
        except:
            await event.respond(loading_text)
        
        try:
            session_data = self.valid_sessions[user_id]
            session_path = session_data['session_path'].replace('.session', '')
            
            client = TelegramClient(session_path, self.api_id, self.api_hash)
            await client.connect()
            
            me = await client.get_me()
            left_count = 0
            admin_count = 0
            total_groups = 0
            error_count = 0
            
            # Count and categorize groups
            groups_to_leave = []
            admin_groups = []
            
            async for dialog in client.iter_dialogs():
                if dialog.is_group or dialog.is_channel:
                    total_groups += 1
                    try:
                        permissions = await client.get_permissions(dialog.entity, me)
                        
                        if permissions.is_admin or permissions.is_creator:
                            admin_groups.append(dialog)
                            admin_count += 1
                        else:
                            groups_to_leave.append(dialog)
                    except Exception as e:
                        logger.error(f"Error checking permissions for {dialog.name}: {e}")
                        error_count += 1
            
            # Update status
            try:
                await event.edit(
                    "🚪 **LEAVING GROUPS**\n\n"
                    f"📊 **Analysis Complete:**\n"
                    f"• Total Groups: `{total_groups}`\n"
                    f"• Admin/Owner: `{admin_count}`\n"
                    f"• To Leave: `{len(groups_to_leave)}`\n\n"
                    "🔄 Starting group exit process..."
                )
            except:
                pass
            
            # Leave non-admin groups
            for i, dialog in enumerate(groups_to_leave, 1):
                try:
                    await client.delete_dialog(dialog.entity)
                    left_count += 1
                    
                    # Update progress every 5 groups
                    if i % 5 == 0:
                        try:
                            await event.edit(
                                "🚪 **LEAVING GROUPS**\n\n"
                                f"📊 Progress: `{i}/{len(groups_to_leave)}`\n"
                                f"✅ Left: `{left_count}`\n"
                                f"👑 Staying Admin: `{admin_count}`\n"
                                f"⏳ Remaining: `{len(groups_to_leave) - i}`"
                            )
                        except:
                            pass
                    
                    # Delay to avoid flood limits
                    await asyncio.sleep(0.5)
                    
                except Exception as e:
                    logger.error(f"Error leaving group {dialog.name}: {e}")
                    error_count += 1
                    continue
            
            await client.disconnect()
            
            result_text = (
                "✅ **GROUP EXIT COMPLETE**\n\n"
                "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                "┃       📊  **RESULTS**  📊       ┃\n"
                "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                f"🚪 **Groups Left:** `{left_count}`\n"
                f"👑 **Still Admin In:** `{admin_count}`\n"
                f"📊 **Total Processed:** `{total_groups}`\n"
                f"❌ **Errors:** `{error_count}`\n\n"
                "🎉 Group cleanup berhasil diselesaikan!"
            )
            
            buttons = [[Button.inline("⬅️ BACK", f"acc_{user_id}".encode())]]
            await event.edit(result_text, buttons=buttons)
            
        except Exception as e:
            error_text = (
                "❌ **GROUP EXIT ERROR**\n\n"
                f"🚨 **Error:** `{str(e)}`\n\n"
                f"✅ **Left:** `{left_count}` groups before error\n"
                f"👑 **Admin:** `{admin_count}` groups preserved\n\n"
                "💡 Partial completion may have occurred"
            )
            buttons = [[Button.inline("⬅️ BACK", f"acc_{user_id}".encode())]]
            try:
                await event.edit(error_text, buttons=buttons)
            except:
                await event.respond(error_text, buttons=buttons)
    
    async def check_sessions(self, event, user_id: str):
        """Cek session aktif dengan opsi hapus semua"""
        loading_text = (
            "📱 **CHECKING SESSIONS**\n\n"
            "⏳ Connecting to account...\n"
            "🔍 Fetching active devices..."
        )
        
        try:
            await event.edit(loading_text)
        except:
            await event.respond(loading_text)
        
        try:
            session_data = self.valid_sessions[user_id]
            session_path = session_data['session_path'].replace('.session', '')
            
            client = TelegramClient(session_path, self.api_id, self.api_hash)
            await client.connect()
            
            # Get active sessions
            result = await client(GetAuthorizationsRequest())
            
            current_sessions = [auth for auth in result.authorizations if not auth.current]
            current_session = next((auth for auth in result.authorizations if auth.current), None)
            
            await client.disconnect()
            
            text = (
                "📱 **ACTIVE SESSIONS**\n\n"
                "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                "┃      📊  **OVERVIEW**  📊      ┃\n"
                "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                f"🔄 **Current Session:** `1 device`\n"
                f"📱 **Other Sessions:** `{len(current_sessions)} devices`\n"
                f"📊 **Total Active:** `{len(result.authorizations)} devices`\n\n"
            )
            
            if current_session:
                text += "🔄 **CURRENT DEVICE:**\n"
                device_info = f"{current_session.device_model} - {current_session.platform}"
                if len(device_info) > 30:
                    device_info = device_info[:30] + "..."
                
                text += f"• `{device_info}`\n"
                text += f"• 📍 {current_session.country}, {current_session.region}\n"
                
                try:
                    if current_session.date_active:
                        date_active = datetime.fromtimestamp(current_session.date_active)
                        text += f"• 🕒 {date_active.strftime('%d/%m/%Y %H:%M')}\n"
                except:
                    text += f"• 🕒 Active now\n"
                text += "\n"
            
            if current_sessions:
                text += f"📱 **OTHER DEVICES ({len(current_sessions)}):**\n"
                for i, auth in enumerate(current_sessions[:5], 1):  # Show max 5
                    device_info = f"{auth.device_model} - {auth.platform}"
                    if len(device_info) > 25:
                        device_info = device_info[:25] + "..."
                    
                    text += f"**{i}.** `{device_info}`\n"
                    text += f"     📍 {auth.country}, {auth.region}\n"
                    
                    try:
                        if auth.date_active:
                            date_active = datetime.fromtimestamp(auth.date_active)
                            text += f"     🕒 {date_active.strftime('%d/%m/%Y %H:%M')}\n"
                    except:
                        text += f"     🕒 Recently active\n"
                    text += "\n"
                
                if len(current_sessions) > 5:
                    text += f"... and {len(current_sessions) - 5} more devices\n\n"
            else:
                text += "✅ **No other active sessions found**\n\n"
            
            buttons = []
            if current_sessions:
                buttons.append([Button.inline("❌ KILL ALL OTHER SESSIONS", f"killall_{user_id}".encode())])
            
            buttons.append([Button.inline("⬅️ BACK", f"acc_{user_id}".encode())])
            
            await event.edit(text, buttons=buttons)
            
        except Exception as e:
            error_text = (
                "❌ **SESSION CHECK ERROR**\n\n"
                f"🚨 **Error:** `{str(e)}`\n\n"
                "💡 Kemungkinan session expired atau tidak ada akses"
            )
            buttons = [[Button.inline("⬅️ BACK", f"acc_{user_id}".encode())]]
            try:
                await event.edit(error_text, buttons=buttons)
            except:
                await event.respond(error_text, buttons=buttons)
    
    async def kill_all_sessions(self, event, user_id: str):
        """Hapus semua session aktif kecuali session saat ini"""
        loading_text = (
            "⚠️ **TERMINATING SESSIONS**\n\n"
            "🔄 Connecting to account...\n"
            "❌ Killing all other devices..."
        )
        
        try:
            await event.edit(loading_text)
        except:
            await event.respond(loading_text)
        
        try:
            session_data = self.valid_sessions[user_id]
            session_path = session_data['session_path'].replace('.session', '')
            
            client = TelegramClient(session_path, self.api_id, self.api_hash)
            await client.connect()
            
            # Get current session count
            result_before = await client(GetAuthorizationsRequest())
            other_sessions_count = len([auth for auth in result_before.authorizations if not auth.current])
            
            # Reset all authorizations except current
            await client(ResetAuthorizationsRequest())
            
            # Wait a moment for the operation to complete
            await asyncio.sleep(2)
            
            # Verify the result
            result_after = await client(GetAuthorizationsRequest())
            remaining_sessions = len([auth for auth in result_after.authorizations if not auth.current])
            
            await client.disconnect()
            
            text = (
                "✅ **SESSION TERMINATION COMPLETE**\n\n"
                "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                "┃       📊  **RESULTS**  📊       ┃\n"
                "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                f"❌ **Sessions Killed:** `{other_sessions_count}`\n"
                f"✅ **Current Session:** `Active & Protected`\n"
                f"📱 **Remaining Others:** `{remaining_sessions}`\n\n"
                "🔐 **Security Status:** All other devices have been logged out\n"
                "🎉 **Account secured successfully!**"
            )
            
            buttons = [
                [Button.inline("🔄 CHECK SESSIONS", f"sessions_{user_id}".encode())],
                [Button.inline("⬅️ BACK", f"acc_{user_id}".encode())]
            ]
            
            await event.edit(text, buttons=buttons)
            
        except Exception as e:
            error_text = (
                "❌ **SESSION TERMINATION ERROR**\n\n"
                f"🚨 **Error:** `{str(e)}`\n\n"
                "💡 **Possible Causes:**\n"
                "• Network connection issues\n"
                "• Session already expired\n"
                "• API rate limiting\n\n"
                "🔄 Try again in a few moments"
            )
            buttons = [[Button.inline("⬅️ BACK", f"acc_{user_id}".encode())]]
            try:
                await event.edit(error_text, buttons=buttons)
            except:
                await event.respond(error_text, buttons=buttons)

# Konfigurasi
API_ID = 23316210  # Ganti dengan API ID Anda
API_HASH = "efbb21f5b0e4693f769929e64c3e8c30"  # Ganti dengan API Hash Anda  
BOT_TOKEN = "8233834199:AAEP4u18S-2Qn8-7M6NdSsN_I6lBdkf9cco"  # Ganti dengan Bot Token Anda

# ADMIN IDs - Hanya user dengan ID ini yang bisa menggunakan bot
ADMIN_IDS = [
    5988451717,   # Ganti dengan Telegram user ID admin 1
    987654321,   # Ganti dengan Telegram user ID admin 2
    # Tambahkan ID admin lainnya di sini
]

async def main():
    """Fungsi utama"""
    manager = SessionManager(BOT_TOKEN, API_ID, API_HASH, ADMIN_IDS)
    
    try:
        await manager.start_bot()
        
        print("🤖 Bot started successfully!")
        print(f"📁 Sessions stored in: {manager.sessions_dir}")
        print(f"🔐 Admin users: {len(ADMIN_IDS)}")
        print("=" * 50)
        print("Bot is running... Press Ctrl+C to stop")
        
        await manager.bot.run_until_disconnected()
        
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped by user")
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        logger.error(f"Fatal error: {e}")
    finally:
        # Cleanup temp directory
        try:
            shutil.rmtree(manager.temp_dir)
            print("🧹 Temporary files cleaned up")
        except Exception as e:
            logger.error(f"Error cleaning up temp dir: {e}")

if __name__ == "__main__":
    # Tampilkan info startup
    print("=" * 50)
    print("🤖 TELEGRAM SESSION MANAGER BOT")
    print("=" * 50)
    print("🔧 Initializing...")
    
    # Jalankan bot
    asyncio.run(main())
