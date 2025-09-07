import os
import re
import asyncio
import zipfile
import tempfile
import shutil
from datetime import datetime
from typing import Dict, List, Optional
import logging

from telethon import TelegramClient, events, Button
from telethon.errors import SessionPasswordNeededError, PhoneNumberInvalidError
from telethon.tl.functions.account import GetPasswordRequest
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
        
    def is_admin(self, user_id: int) -> bool:
        """Cek apakah user adalah admin"""
        return user_id in self.admin_ids
    
    async def check_admin_access(self, event):
        """Cek akses admin dan kirim pesan jika bukan admin"""
        if not self.is_admin(event.sender_id):
            await event.respond(
                "❌ **Akses Ditolak**\n\n"
                "Bot ini hanya dapat digunakan oleh admin.\n"
                "Hubungi administrator untuk mendapatkan akses.",
                buttons=[]
            )
            return False
        return True
        
    async def start_bot(self):
        """Memulai bot"""
        await self.bot.start(bot_token=self.bot_token)
        
        @self.bot.on(events.NewMessage(pattern='/start'))
        async def start_handler(event):
            if not await self.check_admin_access(event):
                return
                
            await event.respond(
                "🤖 **Bot Session Manager**\n\n"
                "Kirimkan file ZIP yang berisi folder sessions/users/ dengan file .session\n"
                "Atau gunakan /akun untuk melihat daftar akun yang tersedia",
                buttons=[
                    [Button.inline("📱 Kelola Akun", b"show_accounts")]
                ]
            )
        
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
                await event.answer("❌ Akses ditolak - Hanya admin", alert=True)
                return
            data = event.data.decode('utf-8')
            
            if data == "show_accounts":
                await self.show_accounts(event)
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
        
        logger.info("Bot started successfully!")
        
    async def process_zip_file(self, event):
        """Memproses file ZIP yang dikirim"""
        await event.respond("🔄 Memproses file ZIP...")
        
        try:
            # Download file
            file_path = await event.download_media(self.temp_dir)
            
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
                await event.respond("❌ Folder sessions/users tidak ditemukan dalam ZIP")
                return
            
            # Proses session files
            session_files = [f for f in os.listdir(sessions_path) if f.endswith('.session')]
            
            if not session_files:
                await event.respond("❌ Tidak ada file .session ditemukan")
                return
            
            await event.respond(f"🔍 Ditemukan {len(session_files)} file session. Memvalidasi...")
            
            valid_count = 0
            for session_file in session_files:
                session_path = os.path.join(sessions_path, session_file)
                result = await self.validate_session(session_path)
                
                if result['valid'] and not result['has_2fa']:
                    # Copy session ke directory kerja
                    work_session_path = os.path.join(self.temp_dir, session_file)
                    shutil.copy2(session_path, work_session_path)
                    
                    self.valid_sessions[result['user_id']] = {
                        'session_path': work_session_path,
                        'phone': result['phone'],
                        'username': result['username'],
                        'user_id': result['user_id']
                    }
                    valid_count += 1
            
            await event.respond(
                f"✅ **Validasi selesai!**\n\n"
                f"📊 **Hasil:**\n"
                f"• Total file: {len(session_files)}\n"
                f"• Session valid (tanpa 2FA): {valid_count}\n\n"
                f"Gunakan /akun untuk melihat daftar akun",
                buttons=[
                    [Button.inline("📱 Lihat Akun", b"show_accounts")]
                ]
            )
            
        except Exception as e:
            await event.respond(f"❌ Error memproses ZIP: {str(e)}")
        finally:
            # Cleanup
            if 'file_path' in locals():
                try:
                    os.remove(file_path)
                except:
                    pass
            if 'extract_dir' in locals():
                try:
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
                await client.get_me()
            except SessionPasswordNeededError:
                has_2fa = True
            
            if has_2fa:
                await client.disconnect()
                return {'valid': True, 'has_2fa': True, 'user_id': None, 'phone': None, 'username': None}
            
            # Get user info
            me = await client.get_me()
            
            await client.disconnect()
            
            return {
                'valid': True,
                'has_2fa': False,
                'user_id': str(me.id),
                'phone': me.phone,
                'username': me.username or 'Tidak ada'
            }
            
        except Exception as e:
            logger.error(f"Error validating session {session_path}: {e}")
            return {'valid': False, 'has_2fa': False, 'user_id': None, 'phone': None, 'username': None}
    
    async def show_accounts(self, event):
        """Menampilkan daftar akun"""
        if not self.valid_sessions:
            text = "❌ Belum ada akun yang tersedia\n\nKirimkan file ZIP dengan session untuk memulai"
            buttons = []
        else:
            # Urutkan berdasarkan user_id (terendah ke tertinggi)
            sorted_accounts = sorted(self.valid_sessions.items(), key=lambda x: int(x[0]))
            
            text = f"📱 **Daftar Akun** ({len(sorted_accounts)} akun)\n\n"
            buttons = []
            
            for user_id, data in sorted_accounts:
                phone = data['phone'] or 'Unknown'
                username = data['username'] or 'Tidak ada'
                
                text += f"• **{phone}** (@{username})\n"
                buttons.append([Button.inline(f"📞 {phone}", f"acc_{user_id}".encode())])
        
        if hasattr(event, 'edit'):
            await event.edit(text, buttons=buttons)
        else:
            await event.respond(text, buttons=buttons)
    
    async def show_account_info(self, event, user_id: str):
        """Menampilkan informasi detail akun"""
        if user_id not in self.valid_sessions:
            await event.answer("❌ Akun tidak ditemukan")
            return
        
        try:
            session_data = self.valid_sessions[user_id]
            session_path = session_data['session_path'].replace('.session', '')
            
            client = TelegramClient(session_path, self.api_id, self.api_hash)
            await client.connect()
            
            if not await client.is_user_authorized():
                await event.answer("❌ Session tidak valid")
                await client.disconnect()
                return
            
            me = await client.get_me()
            
            # Hitung grup yang dimiliki/admin
            admin_groups = 0
            async for dialog in client.iter_dialogs():
                if dialog.is_group or dialog.is_channel:
                    try:
                        permissions = await client.get_permissions(dialog.entity, me)
                        if permissions.is_admin or permissions.is_creator:
                            admin_groups += 1
                    except:
                        continue
            
            await client.disconnect()
            
            text = (
                f"👤 **Info Akun**\n\n"
                f"📞 **Phone:** {me.phone or 'Unknown'}\n"
                f"🆔 **ID:** `{me.id}`\n"
                f"👤 **Username:** @{me.username or 'Tidak ada'}\n"
                f"👑 **Admin/Owner Grup:** {admin_groups}\n\n"
                f"⚡ **Aksi Tersedia:**"
            )
            
            buttons = [
                [Button.inline("📨 Get OTP", f"getotp_{user_id}".encode())],
                [Button.inline("🗑️ Clear Chat", f"clear_{user_id}".encode())],
                [Button.inline("🚪 Keluar Grup", f"leavegroups_{user_id}".encode())],
                [Button.inline("📱 Cek Session", f"sessions_{user_id}".encode())],
                [Button.inline("⬅️ Kembali", b"back_accounts")]
            ]
            
            await event.edit(text, buttons=buttons)
            
        except Exception as e:
            await event.answer(f"❌ Error: {str(e)}")
    
    async def get_telegram_messages(self, client, get_latest_only=False):
        """Get messages from Telegram service number +42777 with OTP extraction"""
        messages = []
        
        try:
            # Look specifically for the Telegram service entity
            try:
                # First try directly getting the entity
                try:
                    telegram_service = await client.get_entity("+42777")
                except:
                    # If that fails, try finding it in dialogs
                    dialogs = await client.get_dialogs()
                    telegram_service = None
                    
                    for dialog in dialogs:
                        if dialog.name == "Telegram" or (hasattr(dialog.entity, 'phone') and dialog.entity.phone == "+42777"):
                            telegram_service = dialog.entity
                            break
                    
                    if not telegram_service:
                        return ["📭 Tidak dapat menemukan chat dengan layanan Telegram (+42777)"]
                
                # Get recent messages from Telegram service
                limit = 5 if get_latest_only else 10
                service_messages = await client.get_messages(telegram_service, limit=limit)
                
                # Pola untuk berbagai bahasa
                otp_patterns = [
                    # Bahasa Indonesia
                    r'Kode masuk Anda: (\d+)',
                    r'Kode masuk: (\d+)',
                    r'Kode verifikasi: (\d+)',
                    # English
                    r'Your login code:?\s*(\d+)',
                    r'Your code:?\s*(\d+)',
                    r'Login code:?\s*(\d+)',
                    r'Verification code:?\s*(\d+)',
                    # General patterns
                    r'code:?\s*(\d+)',
                    r'OTP:?\s*(\d+)',
                    # Last resort: any 5-6 digit number (common OTP length)
                    r'\b(\d{5,6})\b'
                ]
                
                # Cari OTP dalam pesan
                for msg in service_messages:
                    if msg and msg.message:
                        message_content = msg.message
                        
                        # Format waktu
                        msg_time = msg.date.strftime('%Y-%m-%d %H:%M:%S')
                        
                        # Coba semua pola untuk menemukan OTP
                        otp_found = False
                        for pattern in otp_patterns:
                            otp_match = re.search(pattern, message_content, re.IGNORECASE)
                            if otp_match:
                                otp_code = otp_match.group(1)
                                
                                # Jika pattern adalah pola umum 5-6 digit, pastikan ini pesan OTP
                                if pattern == r'\b(\d{5,6})\b':
                                    # Verifikasi pesan ini kemungkinan besar adalah pesan OTP
                                    common_terms = ['code', 'telegram', 'login', 'verification', 'otp',
                                                   'kode', 'masuk', 'verifikasi']
                                    
                                    is_likely_otp = False
                                    for term in common_terms:
                                        if term.lower() in message_content.lower():
                                            is_likely_otp = True
                                            break
                                    
                                    if not is_likely_otp:
                                        continue  # Skip jika tidak seperti pesan OTP
                                
                                # Format tampilan sederhana sesuai permintaan
                                msg_text = f"🔐 OTP: `{otp_code}`\n"
                                msg_text += f"⏰ Waktu: {msg_time}"
                                
                                messages.append(msg_text)
                                otp_found = True
                                break  # Keluar dari loop pola setelah menemukan OTP
                        
                        # Jika OTP ditemukan dan hanya ingin yang terbaru, langsung keluar
                        if otp_found and get_latest_only:
                            break
                
                if not messages:
                    messages.append("📭 Tidak ada pesan OTP dari layanan Telegram (+42777)")
            except Exception as e:
                logger.error(f"Error getting Telegram service: {e}")
                messages.append(f"❌ Tidak dapat menemukan chat dengan layanan Telegram: {str(e)}")
        except Exception as e:
            logger.error(f"Error in get_telegram_messages: {e}")
            messages.append(f"❌ Error: {str(e)}")
        
        return messages
    
    async def get_otp(self, event, user_id: str):
        """Mendapatkan OTP dari +42777 dengan parsing yang akurat"""
        try:
            session_data = self.valid_sessions[user_id]
            session_path = session_data['session_path'].replace('.session', '')
            
            client = TelegramClient(session_path, self.api_id, self.api_hash)
            await client.connect()
            
            # Gunakan fungsi parsing OTP yang sudah dibuat
            otp_messages = await self.get_telegram_messages(client, get_latest_only=True)
            
            await client.disconnect()
            
            if otp_messages:
                text = f"📨 **OTP Terbaru:**\n\n{otp_messages[0]}"
            else:
                text = "📭 Tidak ada pesan OTP dari +42777"
            
            buttons = [[Button.inline("⬅️ Kembali", f"acc_{user_id}".encode())]]
            await event.edit(text, buttons=buttons)
            
        except Exception as e:
            await event.answer(f"❌ Error mendapatkan OTP: {str(e)}")
    
    async def clear_chats(self, event, user_id: str):
        """Menghapus semua chat"""
        try:
            session_data = self.valid_sessions[user_id]
            session_path = session_data['session_path'].replace('.session', '')
            
            client = TelegramClient(session_path, self.api_id, self.api_hash)
            await client.connect()
            
            cleared_count = 0
            async for dialog in client.iter_dialogs():
                if dialog.is_user:  # Hanya clear chat pribadi
                    try:
                        await client.delete_dialog(dialog.entity)
                        cleared_count += 1
                    except:
                        continue
            
            await client.disconnect()
            
            text = f"🗑️ **Chat Cleared**\n\nBerhasil menghapus {cleared_count} chat pribadi"
            buttons = [[Button.inline("⬅️ Kembali", f"acc_{user_id}".encode())]]
            await event.edit(text, buttons=buttons)
            
        except Exception as e:
            await event.answer(f"❌ Error clearing chat: {str(e)}")
    
    async def leave_groups(self, event, user_id: str):
        """Keluar dari semua grup kecuali yang dia admin/owner"""
        try:
            session_data = self.valid_sessions[user_id]
            session_path = session_data['session_path'].replace('.session', '')
            
            client = TelegramClient(session_path, self.api_id, self.api_hash)
            await client.connect()
            
            me = await client.get_me()
            left_count = 0
            admin_count = 0
            
            async for dialog in client.iter_dialogs():
                if dialog.is_group or dialog.is_channel:
                    try:
                        # Cek apakah user adalah admin atau owner
                        permissions = await client.get_permissions(dialog.entity, me)
                        
                        if permissions.is_admin or permissions.is_creator:
                            admin_count += 1
                            continue  # Skip jika admin/owner
                        
                        # Keluar dari grup
                        await client.delete_dialog(dialog.entity)
                        left_count += 1
                        
                        # Delay kecil untuk menghindari flood
                        await asyncio.sleep(0.5)
                        
                    except Exception as e:
                        logger.error(f"Error leaving group {dialog.name}: {e}")
                        continue
            
            await client.disconnect()
            
            text = (
                f"🚪 **Keluar Grup Selesai**\n\n"
                f"✅ Keluar dari: {left_count} grup\n"
                f"👑 Tetap admin di: {admin_count} grup"
            )
            buttons = [[Button.inline("⬅️ Kembali", f"acc_{user_id}".encode())]]
            await event.edit(text, buttons=buttons)
            
        except Exception as e:
            await event.answer(f"❌ Error keluar grup: {str(e)}")
    
    async def check_sessions(self, event, user_id: str):
        """Cek session aktif dengan opsi hapus semua"""
        try:
            session_data = self.valid_sessions[user_id]
            session_path = session_data['session_path'].replace('.session', '')
            
            client = TelegramClient(session_path, self.api_id, self.api_hash)
            await client.connect()
            
            # Get active sessions
            from telethon.tl.functions.account import GetAuthorizationsRequest
            result = await client(GetAuthorizationsRequest())
            
            current_sessions = [auth for auth in result.authorizations if not auth.current]
            
            text = f"📱 **Session Aktif** ({len(result.authorizations)} total)\n\n"
            text += f"🔄 Session saat ini: 1\n"
            text += f"📱 Session lain: {len(current_sessions)}\n\n"
            
            if current_sessions:
                text += "**Perangkat Lain:**\n"
                for i, auth in enumerate(current_sessions[:5], 1):  # Limit 5 untuk tampilan
                    device_info = f"{auth.device_model} - {auth.platform}"
                    if len(device_info) > 25:
                        device_info = device_info[:25] + "..."
                    
                    text += f"**{i}.** {device_info}\n"
                    text += f"   📍 {auth.country} - {auth.region}\n"
                    text += f"   🕒 {datetime.fromtimestamp(auth.date_active).strftime('%d/%m %H:%M')}\n\n"
            
            buttons = []
            if current_sessions:
                buttons.append([Button.inline("❌ Hapus Semua Session Lain", f"killall_{user_id}".encode())])
            
            buttons.append([Button.inline("⬅️ Kembali", f"acc_{user_id}".encode())])
            
            await client.disconnect()
            await event.edit(text, buttons=buttons)
            
        except Exception as e:
            await event.answer(f"❌ Error cek session: {str(e)}")
    
    async def kill_all_sessions(self, event, user_id: str):
        """Hapus semua session aktif kecuali session saat ini"""
        try:
            session_data = self.valid_sessions[user_id]
            session_path = session_data['session_path'].replace('.session', '')
            
            client = TelegramClient(session_path, self.api_id, self.api_hash)
            await client.connect()
            
            from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationsRequest
            
            # Hapus semua session kecuali yang saat ini
            await client(ResetAuthorizationsRequest())
            
            await client.disconnect()
            
            text = "✅ **Semua Session Berhasil Dihapus**\n\nSemua perangkat lain telah dikeluarkan dari akun ini"
            buttons = [[Button.inline("⬅️ Kembali", f"acc_{user_id}".encode())]]
            await event.edit(text, buttons=buttons)
            
        except Exception as e:
            await event.answer(f"❌ Error hapus session: {str(e)}")

# Konfigurasi
API_ID = 23316210  # Ganti dengan API ID Anda
API_HASH = "efbb21f5b0e4693f769929e64c3e8c30"  # Ganti dengan API Hash Anda  
BOT_TOKEN = "8233834199:AAFUEQiLI7QsHVoNfUI06Q"  # Ganti dengan Bot Token Anda

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
        
        print("Bot berjalan... Tekan Ctrl+C untuk stop")
        await manager.bot.run_until_disconnected()
        
    except KeyboardInterrupt:
        print("Bot dihentikan oleh user")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        # Cleanup temp directory
        try:
            shutil.rmtree(manager.temp_dir)
        except:
            pass

if __name__ == "__main__":
    asyncio.run(main())
