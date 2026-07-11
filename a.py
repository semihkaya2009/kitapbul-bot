import os
import sqlite3
import logging
import time
from telethon import TelegramClient, events
from telethon import functions 

# --- MAKSİMUM DETAYLI LOG SİSTEMİ ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    handlers=[
        logging.FileHandler('bot_kayitlari.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Kendi API bilgilerini buraya gir
API_ID = 31228645       
API_HASH = 'ad0e85f4e5b310f9aa70c65166ea51fe' 

client = TelegramClient('kitap_tarayici_oturum', API_ID, API_HASH)

# --- VERİTABANI KURULUMU ---
DB_FILE = 'kitaplar.db'
conn = sqlite3.connect(DB_FILE)
cursor = conn.cursor()


def veritabani_hazirla():
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS kitaplar (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            grup_adi TEXT,
            dosya_adi TEXT,
            grup_id INTEGER,
            mesaj_id INTEGER,
            topic_id INTEGER DEFAULT NULL,
            UNIQUE(grup_id, mesaj_id)
        )
    ''')

    cursor.execute("PRAGMA table_info(kitaplar)")
    kolonlar = {row[1] for row in cursor.fetchall()}
    if 'topic_id' not in kolonlar:
        logger.info("🛠️ [VERİTABANI] 'topic_id' kolonu ekleniyor...")
        cursor.execute("ALTER TABLE kitaplar ADD COLUMN topic_id INTEGER DEFAULT NULL")

    conn.commit()


veritabani_hazirla()

FORUM_TOPICS_SUPPORTED = hasattr(functions.channels, 'GetForumTopicsRequest')

def dosya_kontrol_ve_kaydet(dialog, message, grup_kitap_sayisi, toplam_eklenen, topic_id=None):
    if message.document:
        dosya_adi = None
        for attr in message.document.attributes:
            if hasattr(attr, 'file_name'):
                dosya_adi = attr.file_name
                break
        
        if dosya_adi and dosya_adi.lower().endswith(('.pdf', '.epub', '.mobi')):
            try:
                cursor.execute(
                    "INSERT OR IGNORE INTO kitaplar (grup_adi, dosya_adi, grup_id, mesaj_id, topic_id) VALUES (?, ?, ?, ?, ?)",
                    (dialog.name, dosya_adi, dialog.id, message.id, topic_id)
                )
                if cursor.rowcount > 0:
                    grup_kitap_sayisi[0] += 1
                    toplam_eklenen[0] += 1
                    logger.info(f"   ➕ [YENİ DOSYA] Hafızaya Yazıldı: {dosya_adi}")
            except sqlite3.Error as db_error:
                logger.error(f"   ❌ [VERİTABANI HATASI] {dosya_adi} kaydedilemedi: {db_error}")

@client.on(events.NewMessage(chats='me'))
async def komut_ve_arama_motoru(event):
    metin = event.text.strip()
    
    if metin == ".indeksle":
        logger.info("🚀 [KOMUT] Sınırsız Komple Derin Tarama başlatılıyor...")
        await event.reply("🔄 Tüm topluluklar ve yüzlerce alt başlık dahil SINIRSIZ tarama başladı. Terminalden izleyebilirsiniz...")
        
        toplam_eklenen = [0]
        grup_sayisi = 0
        
        # CRITICAL: limit=None diyerek Telegram'daki istisnasız TÜM sohbetleri çekiyoruz
        async for dialog in client.iter_dialogs(limit=None):
            if dialog.is_group or dialog.is_channel:
                grup_sayisi += 1
                grup_kitap_sayisi = [0]
                logger.info(f"📂 [SOHBET: {grup_sayisi}] '{dialog.name}' (ID: {dialog.id}) taranıyor...")
                
                try:
                    # SOHBET BİR TOPLULUK (FORUM) MU?
                    if getattr(dialog.entity, 'forum', False):
                        logger.info(f"   🏛️ '{dialog.name}' bir TOPLULUK. Tüm alt başlıklar çekiliyor...")
                        if FORUM_TOPICS_SUPPORTED:
                            offset_id = 0
                            while True:
                                # limit=100 yaparak sayfa sayfa (pagination) tüm başlıkları çekiyoruz
                                topics = await client(functions.channels.GetForumTopicsRequest(
                                    channel=dialog.id, offset_date=0, offset_id=offset_id, offset_topic=0, limit=100
                                ))

                                if not topics.topics:
                                    break

                                for topic in topics.topics:
                                    logger.info(f"     ➔ 🏷️ Alt Başlık Taranıyor: '{topic.title}'")
                                    async for message in client.iter_messages(dialog.id, reply_to=topic.id, limit=1000):
                                        dosya_kontrol_ve_kaydet(dialog, message, grup_kitap_sayisi, toplam_eklenen, topic_id=topic.id)

                                offset_id = topics.topics[-1].id
                                if len(topics.topics) < 100:
                                    break
                        else:
                            logger.warning(
                                f"   ⚠️ '{dialog.name}' forum başlıkları bu Telethon sürümünde desteklenmiyor. "
                                "Genel sohbet akışı taranacak."
                            )
                            async for message in client.iter_messages(dialog.id, limit=1500):
                                dosya_kontrol_ve_kaydet(dialog, message, grup_kitap_sayisi, toplam_eklenen)
                                
                    # NORMAL GRUP VEYA KANALSA
                    else:
                        # Son 1500 mesajı geriye dönük tarar
                        async for message in client.iter_messages(dialog.id, limit=1500):
                            dosya_kontrol_ve_kaydet(dialog, message, grup_kitap_sayisi, toplam_eklenen)
                    
                    conn.commit()
                    logger.info(f"✅ [BİTTİ] '{dialog.name}' grubundan {grup_kitap_sayisi[0]} adet kitap eklendi.")
                    time.sleep(0.5) # FloodBan yememek için kısa es
                    
                except Exception as e:
                    logger.error(f"🚨 [ERİŞİM HATASI] '{dialog.name}' taranamadı. Detay: {str(e)}")
                    continue
                    
        logger.info(f"🏁 [TARAMA BİTTİ] Toplam {toplam_eklenen[0]} kitap başarıyla hafızaya alındı.")
        await event.reply(f"✅ Sınırsız derin indeksleme bitti! Toplam {toplam_eklenen[0]} yeni kitap hafızaya eklendi. Şimdi arama yapabilirsin.")
        return

    # ARAMA BÖLÜMÜ
    if not metin or metin.startswith('.'):
        return

    logger.info(f"🔍 [ARAMA TALEBİ] Aranan: '{metin}'")
    cursor.execute("SELECT grup_adi, dosya_adi, grup_id, mesaj_id FROM kitaplar WHERE dosya_adi LIKE ?", (f'%{metin}%',))
    sonuclar = cursor.fetchall()
    
    if not sonuclar:
        logger.warning(f"❌ '{metin}' için hafızada eşleşme yok.")
        await event.reply(f'❌ Hafızada "{metin}" kelimesine ait hiçbir kitap bulunamadı.')
        return
        
    await event.reply(f'📚 Toplam {len(sonuclar)} sonuç bulundu, ilk 15 dosya yönlendiriliyor...')
    
    gonderilen = 0
    for grup_adi, dosya_adi, grup_id, mesaj_id in sonuclar[:15]:
        try:
            await client.send_message('me', f'🔹 **Grup:** {grup_adi}\n📄 **Kitap:** {dosya_adi}')
            await client.forward_messages('me', messages=mesaj_id, from_peer=grup_id)
            gonderilen += 1
            time.sleep(0.5)
        except Exception as e:
            continue
    logger.info(f"✨ [İŞLEM TAMAM] {gonderilen} dosya gönderildi.")

print("⚡ Sınırsız Komple Derin Kitap Tarayıcı Aktif!")
client.start()
client.run_until_disconnected()
