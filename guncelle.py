import os
import re
import sqlite3
import logging
import time
import unicodedata
import html
from difflib import SequenceMatcher
from functools import lru_cache
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
MAX_TELEGRAM_MESSAGE_LENGTH = 4000
bekleyen_arama_sonuclari = []
BENZERLIK_ESIGI = 0.84
KITAP_BENZERLIK_ESIGI = 0.78
DOSYA_UZANTILARI = ('.pdf', '.epub', '.mobi')
TEKILLESTIRME_ATLANACAK_TOKENLAR = {
    'ayt',
    'bankasi',
    'deneme',
    'denemesi',
    'kitap',
    'matematik',
    'model',
    'pdf',
    'sinif',
    'soru',
    'tyt',
    'yayin',
    'yayinlari',
    'yks',
}

TURKCE_KARAKTER_CEVIRISI = str.maketrans({
    'ç': 'c',
    'ğ': 'g',
    'ı': 'i',
    'ö': 'o',
    'ş': 's',
    'ü': 'u',
    'Ç': 'c',
    'Ğ': 'g',
    'İ': 'i',
    'I': 'i',
    'Ö': 'o',
    'Ş': 's',
    'Ü': 'u',
})

YAZIM_ESDEGERLERI = {
    'orjinal': ('orijinal',),
    'orijinal': ('orjinal',),
}

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

# Veritabanından o gruba/başlığa ait en son taranan mesaj ID'sini getiren fonksiyon
def en_son_mesaj_id_getir(grup_id, topic_id=None):
    if topic_id:
        cursor.execute("SELECT MAX(mesaj_id) FROM kitaplar WHERE grup_id = ? AND topic_id = ?", (grup_id, topic_id))
    else:
        cursor.execute("SELECT MAX(mesaj_id) FROM kitaplar WHERE grup_id = ? AND topic_id IS NULL", (grup_id,))
    res = cursor.fetchone()
    return res[0] if res and res[0] else 0

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


@lru_cache(maxsize=120000)
def arama_metnini_normalize_et(metin):
    metin = metin.translate(TURKCE_KARAKTER_CEVIRISI).casefold()
    metin = unicodedata.normalize('NFKD', metin)
    metin = ''.join(karakter for karakter in metin if not unicodedata.combining(karakter))
    metin = re.sub(r'[^a-z0-9]+', ' ', metin)
    return re.sub(r'\s+', ' ', metin).strip()


def arama_tokenlarini_getir(metin):
    return tuple(token for token in arama_metnini_normalize_et(metin).split() if token)


def dosya_uzantilarini_temizle(dosya_adi):
    dosya_adi = dosya_adi.strip()
    while True:
        kok, uzanti = os.path.splitext(dosya_adi)
        if uzanti.casefold() not in DOSYA_UZANTILARI:
            return dosya_adi
        dosya_adi = kok


@lru_cache(maxsize=120000)
def kitap_tekillestirme_tokenlarini_getir(dosya_adi):
    kitap_adi = dosya_uzantilarini_temizle(dosya_adi)
    kitap_adi = re.sub(r'^@[A-Za-z0-9]+(?=[_\s.-])', ' ', kitap_adi)
    kitap_adi = re.sub(r'@[A-Za-z0-9_]+$', ' ', kitap_adi)
    kitap_adi = re.sub(r'\s@[A-Za-z0-9_]+\b', ' ', kitap_adi)
    kitap_adi = re.sub(r'\b20\d{10,}\b', ' ', kitap_adi)
    kitap_adi = re.sub(r'\b\d{8,}\b', ' ', kitap_adi)
    kitap_adi = re.sub(r'\((?:\d+|copy)\)\s*$', ' ', kitap_adi, flags=re.IGNORECASE)

    normalize_kitap_adi = arama_metnini_normalize_et(kitap_adi)
    normalize_kitap_adi = re.sub(r'(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)', ' ', normalize_kitap_adi)
    tokenlar = tuple(
        token for token in normalize_kitap_adi.split()
        if token
        and token != 'pdf'
        and not (token.isdigit() and len(token) >= 8)
    )
    return tokenlar


def kitap_tekillestirme_anahtari(dosya_adi):
    return ' '.join(kitap_tekillestirme_tokenlarini_getir(dosya_adi))


def tekillestirme_indeks_tokenlarini_getir(tokenlar):
    token_adaylari = [
        token for token in tokenlar
        if len(token) >= 4
        and not token.isdigit()
        and token not in TEKILLESTIRME_ATLANACAK_TOKENLAR
    ]

    if token_adaylari:
        return token_adaylari[:8]

    return [
        token for token in tokenlar
        if len(token) >= 4
        and not token.isdigit()
    ][:8]


def token_eslesme_skoru(arama_tokeni, dosya_tokenlari, normalize_dosya_adi):
    token_adaylari = (arama_tokeni, *YAZIM_ESDEGERLERI.get(arama_tokeni, ()))

    for token_adayi in token_adaylari:
        if token_adayi in dosya_tokenlari:
            return 25

    for token_adayi in token_adaylari:
        if token_adayi in normalize_dosya_adi:
            return 18

    for token_adayi in token_adaylari:
        if len(token_adayi) < 4:
            continue

        for dosya_tokeni in dosya_tokenlari:
            if len(dosya_tokeni) < 4:
                continue

            if token_adayi in dosya_tokeni or dosya_tokeni in token_adayi:
                return 14

            benzerlik = SequenceMatcher(None, token_adayi, dosya_tokeni).ratio()
            if benzerlik >= BENZERLIK_ESIGI:
                return int(benzerlik * 12)

    return 0


def tekillestirme_token_eslesir_mi(token_a, token_b):
    if token_a == token_b:
        return True

    adaylar_a = (token_a, *YAZIM_ESDEGERLERI.get(token_a, ()))
    adaylar_b = (token_b, *YAZIM_ESDEGERLERI.get(token_b, ()))
    if set(adaylar_a) & set(adaylar_b):
        return True

    if len(token_a) >= 4 and len(token_b) >= 4:
        if token_a in token_b or token_b in token_a:
            return True
        return SequenceMatcher(None, token_a, token_b).ratio() >= BENZERLIK_ESIGI

    return False


def anlamli_sayi_tokenlari(tokenlar):
    return {
        token for token in tokenlar
        if token.isdigit()
        and len(token) < 8
        and not re.fullmatch(r'20\d{2}', token)
    }


def sayi_tokenlari_uyumlu_mu(tokenlar_a, tokenlar_b):
    sayilar_a = anlamli_sayi_tokenlari(tokenlar_a)
    sayilar_b = anlamli_sayi_tokenlari(tokenlar_b)

    if not sayilar_a or not sayilar_b:
        return True

    return sayilar_a == sayilar_b


def kitaplar_benzer_mi(tokenlar_a, tokenlar_b):
    if not tokenlar_a or not tokenlar_b:
        return False

    if not sayi_tokenlari_uyumlu_mu(tokenlar_a, tokenlar_b):
        return False

    eslesen_a = set()
    eslesen_b = set()

    for index_a, token_a in enumerate(tokenlar_a):
        for index_b, token_b in enumerate(tokenlar_b):
            if index_b in eslesen_b:
                continue

            if tekillestirme_token_eslesir_mi(token_a, token_b):
                eslesen_a.add(index_a)
                eslesen_b.add(index_b)
                break

    kisa_kapsam = len(eslesen_a) / min(len(tokenlar_a), len(tokenlar_b))
    uzun_kapsam = len(eslesen_a) / max(len(tokenlar_a), len(tokenlar_b))
    return kisa_kapsam >= 0.88 and uzun_kapsam >= KITAP_BENZERLIK_ESIGI


def sonuc_arama_skoru(metin, arama_tokenlari, dosya_adi):
    normalize_dosya_adi = arama_metnini_normalize_et(dosya_adi)
    dosya_tokenlari = normalize_dosya_adi.split()
    skor = 0

    for arama_tokeni in arama_tokenlari:
        token_skoru = token_eslesme_skoru(arama_tokeni, dosya_tokenlari, normalize_dosya_adi)
        if token_skoru == 0:
            return 0
        skor += token_skoru

    normalize_arama_metni = arama_metnini_normalize_et(metin)
    if normalize_arama_metni and normalize_arama_metni in normalize_dosya_adi:
        skor += 50

    return skor


def sonuc_tercih_puani(skor, dosya_adi):
    puan = skor * 100
    if dosya_adi.startswith('@'):
        puan -= 8
    if re.search(r'\b\d{8,}\b', dosya_adi):
        puan -= 5
    puan -= len(dosya_adi) / 100
    return puan


def kaynaklari_sirala(kaynaklar):
    sirali_kaynaklar = sorted(
        kaynaklar,
        key=lambda kaynak: (
            -kaynak['tercih_puani'],
            -kaynak['skor'],
            arama_metnini_normalize_et(kaynak['sonuc'][1]),
        )
    )
    return [kaynak['sonuc'] for kaynak in sirali_kaynaklar]


def tekil_sonuclari_getir(eslesen_sonuclar):
    gruplar = []
    gruplar_by_anahtar = {}
    gruplar_by_indeks_token = {}

    for skor, sonuc in eslesen_sonuclar:
        _, dosya_adi, _, _ = sonuc
        anahtar = kitap_tekillestirme_anahtari(dosya_adi)
        tokenlar = kitap_tekillestirme_tokenlarini_getir(dosya_adi)
        indeks_tokenlari = set(tekillestirme_indeks_tokenlarini_getir(tokenlar))
        hedef_grup = gruplar_by_anahtar.get(anahtar)

        if hedef_grup is None:
            aday_gruplar = []
            gorulen_gruplar = set()

            for indeks_tokeni in indeks_tokenlari:
                for grup in gruplar_by_indeks_token.get(indeks_tokeni, []):
                    grup_id = id(grup)
                    if grup_id in gorulen_gruplar:
                        continue
                    gorulen_gruplar.add(grup_id)
                    aday_gruplar.append(grup)

            for grup in aday_gruplar:
                if kitaplar_benzer_mi(tokenlar, grup['tokenlar']):
                    hedef_grup = grup
                    break

        tercih_puani = sonuc_tercih_puani(skor, dosya_adi)
        if hedef_grup is None:
            hedef_grup = {
                'anahtar': anahtar,
                'tokenlar': tokenlar,
                'en_iyi_skor': skor,
                'en_iyi_tercih_puani': tercih_puani,
                'en_iyi_sonuc': sonuc,
                'kopya_sayisi': 1,
                'indeks_tokenlari': indeks_tokenlari,
                'kaynaklar': [{'tercih_puani': tercih_puani, 'skor': skor, 'sonuc': sonuc}],
            }
            gruplar.append(hedef_grup)
            gruplar_by_anahtar[anahtar] = hedef_grup
            for indeks_tokeni in indeks_tokenlari:
                gruplar_by_indeks_token.setdefault(indeks_tokeni, []).append(hedef_grup)
            continue

        hedef_grup['kopya_sayisi'] += 1
        hedef_grup['en_iyi_skor'] = max(hedef_grup['en_iyi_skor'], skor)
        hedef_grup['kaynaklar'].append({'tercih_puani': tercih_puani, 'skor': skor, 'sonuc': sonuc})

        if tercih_puani > hedef_grup['en_iyi_tercih_puani']:
            hedef_grup['en_iyi_tercih_puani'] = tercih_puani
            hedef_grup['en_iyi_sonuc'] = sonuc

        if anahtar:
            gruplar_by_anahtar[anahtar] = hedef_grup

        yeni_indeks_tokenlari = indeks_tokenlari - hedef_grup['indeks_tokenlari']
        for indeks_tokeni in yeni_indeks_tokenlari:
            gruplar_by_indeks_token.setdefault(indeks_tokeni, []).append(hedef_grup)
        hedef_grup['indeks_tokenlari'].update(yeni_indeks_tokenlari)

    gruplar.sort(key=lambda grup: (-grup['en_iyi_skor'], arama_metnini_normalize_et(grup['en_iyi_sonuc'][1])))
    return [(*grup['en_iyi_sonuc'], grup['kopya_sayisi'], kaynaklari_sirala(grup['kaynaklar'])) for grup in gruplar]


def kitaplari_ara(metin):
    arama_tokenlari = arama_tokenlarini_getir(metin)
    if not arama_tokenlari:
        return []

    cursor.execute("SELECT grup_adi, dosya_adi, grup_id, mesaj_id FROM kitaplar")
    eslesen_sonuclar = []

    for sonuc in cursor.fetchall():
        _, dosya_adi, _, _ = sonuc
        skor = sonuc_arama_skoru(metin, arama_tokenlari, dosya_adi)
        if skor > 0:
            eslesen_sonuclar.append((skor, sonuc))

    eslesen_sonuclar.sort(key=lambda item: (-item[0], arama_metnini_normalize_et(item[1][1])))
    return tekil_sonuclari_getir(eslesen_sonuclar)


def html_kacir(metin):
    return html.escape(str(metin), quote=False)


def tasarimli_durum_mesaji(baslik, satirlar):
    govde = '\n'.join(satir for satir in satirlar if satir)
    return f"{baslik}\n\n{govde}" if govde else baslik


def sonuc_listesini_metne_dok(sonuclar):
    satirlar = []
    for index, sonuc in enumerate(sonuclar, start=1):
        grup_adi, dosya_adi, _, _, *ek_bilgiler = sonuc
        kopya_sayisi = ek_bilgiler[0] if ek_bilgiler else 1
        kopya_bilgisi = (
            f"\n   🔁 <b>Kopya:</b> {kopya_sayisi} kaynak birleştirildi"
            if kopya_sayisi > 1
            else ""
        )
        satirlar.append(
            f"\n<b>{index}. {html_kacir(dosya_adi)}</b>\n"
            f"   📁 <i>{html_kacir(grup_adi)}</i>"
            f"{kopya_bilgisi}"
        )
    return satirlar


async def uzun_mesaj_gonder(hedef, baslik, satirlar, parse_mode=None):
    parcalar = []
    mevcut_parca = baslik

    for satir in satirlar:
        eklenecek = f"{mevcut_parca}\n{satir}" if mevcut_parca else satir
        if len(eklenecek) > MAX_TELEGRAM_MESSAGE_LENGTH:
            parcalar.append(mevcut_parca)
            mevcut_parca = satir
        else:
            mevcut_parca = eklenecek

    if mevcut_parca:
        parcalar.append(mevcut_parca)

    for parca in parcalar:
        await client.send_message(hedef, parca, parse_mode=parse_mode)


def secim_numaralarini_ayikla(metin):
    if not re.fullmatch(r'\d+(?:\s*,\s*\d+)*', metin):
        return None

    secimler = []
    gorulen_secimler = set()
    for secim_metni in metin.split(','):
        secim = int(secim_metni.strip())
        if secim in gorulen_secimler:
            continue

        gorulen_secimler.add(secim)
        secimler.append(secim)

    return secimler


def secim_metni_gibi_gorunuyor(metin):
    return bool(re.fullmatch(r'[\d\s,]+', metin))


async def secilen_kitabi_gonder(secim, secilen_sonuc, toplam_secim_sayisi=1):
    grup_adi, dosya_adi, grup_id, mesaj_id, *ek_bilgiler = secilen_sonuc
    kopya_sayisi = ek_bilgiler[0] if ek_bilgiler else 1
    yedek_kaynaklar = ek_bilgiler[1] if len(ek_bilgiler) > 1 else [(grup_adi, dosya_adi, grup_id, mesaj_id)]
    logger.info(f"📥 [SEÇİM] {secim}. sıradaki dosya isteniyor: '{dosya_adi}'")

    son_hata = None
    for kaynak_index, kaynak in enumerate(yedek_kaynaklar, start=1):
        kaynak_grup_adi, kaynak_dosya_adi, kaynak_grup_id, kaynak_mesaj_id = kaynak
        try:
            await client.forward_messages('me', messages=kaynak_mesaj_id, from_peer=kaynak_grup_id)
            liste_bilgisi = f"🔢 <b>Liste no:</b> <code>{secim}</code>" if toplam_secim_sayisi > 1 else ""
            kopya_bilgisi = f"🔁 <b>Birleştirilen kopya:</b> {kopya_sayisi}" if kopya_sayisi > 1 else ""
            kaynak_bilgisi = (
                f"✅ <b>Kullanılan kaynak:</b> {kaynak_index}/{len(yedek_kaynaklar)}"
                if len(yedek_kaynaklar) > 1
                else ""
            )
            await client.send_message(
                'me',
                tasarimli_durum_mesaji(
                    "✅ <b>Dosya gönderildi</b>",
                    [
                        liste_bilgisi,
                        f"📄 <b>Kitap:</b>\n<code>{html_kacir(kaynak_dosya_adi)}</code>",
                        f"📁 <b>Kanal:</b> {html_kacir(kaynak_grup_adi)}",
                        kopya_bilgisi,
                        kaynak_bilgisi,
                    ]
                ),
                parse_mode='html'
            )
            logger.info(f"✨ [İŞLEM TAMAM] Seçilen dosya gönderildi. Kaynak: {kaynak_index}/{len(yedek_kaynaklar)}")
            return True
        except Exception as e:
            son_hata = e
            logger.warning(
                f"⚠️ [YEDEK KAYNAK DENENİYOR] '{kaynak_dosya_adi}' gönderilemedi "
                f"({kaynak_index}/{len(yedek_kaynaklar)}): {str(e)}"
            )

    logger.error(f"🚨 [GÖNDERİM HATASI] '{dosya_adi}' için tüm kopyalar denendi: {str(son_hata)}")
    return False


@client.on(events.NewMessage(chats='me'))
async def komut_ve_arama_motoru(event):
    global bekleyen_arama_sonuclari
    metin = event.text.strip()
    
    # .indeksle (Sıfırdan tam tarama) veya .guncelle (Sadece yeniler)
    if metin == ".indeksle" or metin == ".guncelle":
        sadece_yeniler = (metin == ".guncelle")
        mod_adi = "Akıllı Güncelleme" if sadece_yeniler else "Sıfırdan Komple Tarama"
        
        logger.info(f"🚀 [KOMUT] {mod_adi} başlatılıyor...")
        await event.reply(
            tasarimli_durum_mesaji(
                "🔄 <b>Tarama başladı</b>",
                [
                    f"🧭 <b>Mod:</b> {html_kacir(mod_adi)}",
                    "Terminalden ilerlemeyi takip edebilirsin.",
                ]
            ),
            parse_mode='html'
        )
        
        toplam_eklenen = [0]
        grup_sayisi = 0
        
        async for dialog in client.iter_dialogs(limit=None):
            if dialog.is_group or dialog.is_channel:
                grup_sayisi += 1
                grup_kitap_sayisi = [0]
                
                try:
                    # SOHBET BİR TOPLULUK (FORUM) MU?
                    if getattr(dialog.entity, 'forum', False):
                        logger.info(f"📂 [{grup_sayisi}] Topluluk Taranıyor: '{dialog.name}'")
                        if FORUM_TOPICS_SUPPORTED:
                            offset_id = 0
                            while True:
                                topics = await client(functions.channels.GetForumTopicsRequest(
                                    channel=dialog.id, offset_date=0, offset_id=offset_id, offset_topic=0, limit=100
                                ))
                                if not topics.topics:
                                    break

                                for topic in topics.topics:
                                    min_msg_id = en_son_mesaj_id_getir(dialog.id, topic.id) if sadece_yeniler else 0

                                    if min_msg_id > 0:
                                        logger.info(f"     ➔ 🏷️ '{topic.title}' (ID {min_msg_id} sonrasındaki yeni mesajlar kontrol ediliyor...)")
                                        async for message in client.iter_messages(dialog.id, reply_to=topic.id, min_id=min_msg_id):
                                            dosya_kontrol_ve_kaydet(dialog, message, grup_kitap_sayisi, toplam_eklenen, topic_id=topic.id)
                                    else:
                                        async for message in client.iter_messages(dialog.id, reply_to=topic.id, limit=1000):
                                            dosya_kontrol_ve_kaydet(dialog, message, grup_kitap_sayisi, toplam_eklenen, topic_id=topic.id)

                                offset_id = topics.topics[-1].id
                                if len(topics.topics) < 100:
                                    break
                        else:
                            min_msg_id = en_son_mesaj_id_getir(dialog.id) if sadece_yeniler else 0
                            logger.warning(
                                f"   ⚠️ '{dialog.name}' forum başlıkları Telethon {functions.__package__.split('.')[0]} sürümünde desteklenmiyor. "
                                "Genel sohbet akışı taranacak."
                            )
                            if min_msg_id > 0:
                                async for message in client.iter_messages(dialog.id, min_id=min_msg_id):
                                    dosya_kontrol_ve_kaydet(dialog, message, grup_kitap_sayisi, toplam_eklenen)
                            else:
                                async for message in client.iter_messages(dialog.id, limit=1500):
                                    dosya_kontrol_ve_kaydet(dialog, message, grup_kitap_sayisi, toplam_eklenen)
                                
                    # NORMAL GRUP VEYA KANALSA
                    else:
                        min_msg_id = en_son_mesaj_id_getir(dialog.id) if sadece_yeniler else 0
                        
                        if min_msg_id > 0:
                            logger.info(f"📂 [{grup_sayisi}] '{dialog.name}' (ID {min_msg_id} sonrasındaki yeni mesajlar kontrol ediliyor...)")
                            async for message in client.iter_messages(dialog.id, min_id=min_msg_id):
                                dosya_kontrol_ve_kaydet(dialog, message, grup_kitap_sayisi, toplam_eklenen)
                        else:
                            logger.info(f"📂 [{grup_sayisi}] '{dialog.name}' (Geçmişe dönük taranıyor...)")
                            async for message in client.iter_messages(dialog.id, limit=1500):
                                dosya_kontrol_ve_kaydet(dialog, message, grup_kitap_sayisi, toplam_eklenen)
                    
                    conn.commit()
                    if grup_kitap_sayisi[0] > 0:
                        logger.info(f"✅ '{dialog.name}' grubuna {grup_kitap_sayisi[0]} adet YENİ kitap eklendi.")
                    
                except Exception as e:
                    logger.error(f"🚨 [ERİŞİM HATASI] '{dialog.name}' taranamadı: {str(e)}")
                    continue
                    
        logger.info(f"🏁 [{mod_adi} BİTTİ] Toplam {toplam_eklenen[0]} YENİ kitap hafızaya eklendi.")
        await event.reply(
            tasarimli_durum_mesaji(
                "✅ <b>Tarama tamamlandı</b>",
                [
                    f"🧭 <b>Mod:</b> {html_kacir(mod_adi)}",
                    f"➕ <b>Eklenen yeni kitap:</b> {toplam_eklenen[0]}",
                ]
            ),
            parse_mode='html'
        )
        return

    # ARAMA BÖLÜMÜ
    if not metin or metin.startswith('.'):
        return

    secim_numaralari = secim_numaralarini_ayikla(metin)
    if bekleyen_arama_sonuclari and (secim_numaralari is not None or secim_metni_gibi_gorunuyor(metin)):
        if not secim_numaralari:
            await event.reply(
                tasarimli_durum_mesaji(
                    "❌ <b>Geçersiz seçim formatı</b>",
                    [
                        "Tek seçim için <code>1</code>",
                        "Çoklu seçim için <code>1,2,5</code> yaz.",
                    ]
                ),
                parse_mode='html'
            )
            return

        gecersiz_secimler = [
            secim for secim in secim_numaralari
            if secim < 1 or secim > len(bekleyen_arama_sonuclari)
        ]
        if gecersiz_secimler:
            gecersiz_metin = ', '.join(str(secim) for secim in gecersiz_secimler)
            await event.reply(
                tasarimli_durum_mesaji(
                    "❌ <b>Geçersiz seçim</b>",
                    [
                        f"Seçim: <code>{html_kacir(gecersiz_metin)}</code>",
                        f"Geçerli aralık: <code>1-{len(bekleyen_arama_sonuclari)}</code>",
                    ]
                ),
                parse_mode='html'
            )
            return

        logger.info(f"📦 [ÇOKLU SEÇİM] İstenen numaralar: {', '.join(str(secim) for secim in secim_numaralari)}")
        basarili_secimler = []
        basarisiz_secimler = []
        toplam_secim_sayisi = len(secim_numaralari)

        for secim in secim_numaralari:
            secilen_sonuc = bekleyen_arama_sonuclari[secim - 1]
            if await secilen_kitabi_gonder(secim, secilen_sonuc, toplam_secim_sayisi):
                basarili_secimler.append(secim)
            else:
                basarisiz_secimler.append(secim)

        bekleyen_arama_sonuclari = []

        if basarisiz_secimler:
            basarisiz_metin = ', '.join(str(secim) for secim in basarisiz_secimler)
            await event.reply(
                tasarimli_durum_mesaji(
                    "⚠️ <b>Gönderim tamamlandı, bazıları başarısız</b>",
                    [
                        f"✅ <b>Gönderilen:</b> {len(basarili_secimler)}",
                        f"❌ <b>Gönderilemeyen:</b> <code>{html_kacir(basarisiz_metin)}</code>",
                        "<code>.guncelle</code> çalıştırıp tekrar deneyebilirsin.",
                    ]
                ),
                parse_mode='html'
            )
        elif toplam_secim_sayisi > 1:
            await event.reply(
                tasarimli_durum_mesaji(
                    "✅ <b>Çoklu gönderim tamamlandı</b>",
                    [f"📦 <b>Gönderilen kitap:</b> {len(basarili_secimler)}"]
                ),
                parse_mode='html'
            )

        return

    logger.info(f"🔍 [ARAMA TALEBİ] Aranan: '{metin}'")
    sonuclar = kitaplari_ara(metin)
    
    if not sonuclar:
        logger.warning(f"❌ '{metin}' için hafızada eşleşme yok.")
        await event.reply(
            tasarimli_durum_mesaji(
                "❌ <b>Sonuç bulunamadı</b>",
                [f"🔎 <b>Arama:</b> <code>{html_kacir(metin)}</code>"]
            ),
            parse_mode='html'
        )
        return

    bekleyen_arama_sonuclari = sonuclar
    toplam_kopya_sayisi = sum(sonuc[4] if len(sonuc) > 4 else 1 for sonuc in sonuclar)
    birlestirme_bilgisi = (
        f'🔁 <b>Birleştirilen tekrar:</b> {toplam_kopya_sayisi - len(sonuclar)}\n'
        if toplam_kopya_sayisi > len(sonuclar)
        else ''
    )
    baslik = (
        '📚 <b>Arama Sonuçları</b>\n'
        f'🔎 <b>Arama:</b> <code>{html_kacir(metin)}</code>\n'
        f'📌 <b>Tekil sonuç:</b> {len(sonuclar)}\n'
        f'{birlestirme_bilgisi}'
        '\n<b>Seçim</b>\n'
        'Tek kitap: <code>1</code>\n'
        'Çoklu seçim: <code>1,2,5</code>\n'
    )
    await uzun_mesaj_gonder('me', baslik, sonuc_listesini_metne_dok(sonuclar), parse_mode='html')
    logger.info(f"📝 [LİSTE GÖNDERİLDİ] {len(sonuclar)} sonuç numaralı liste halinde gönderildi.")

if __name__ == "__main__":
    print("⚡ Akıllı Güncelleme Destekli Kitap Tarayıcı Aktif!")
    client.start()
    client.run_until_disconnected()
