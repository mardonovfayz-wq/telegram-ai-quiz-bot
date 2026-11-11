# Botni o‘z serveringizda ishga tushirish (faqat brauzer orqali)

Bu yo‘riqnoma botni Activepieces turgan serveringizda **alohida Docker xizmati**
sifatida ishga tushirish uchun. Replit va Railway ishlatilmaydi. Hamma qadamlar
serverning veb-paneli (Portainer, Coolify, Dokploy yoki shunga o‘xshash)
orqali bajariladi; kompyuteringizga hech narsa o‘rnatilmaydi va terminal kerak
emas.

> **Muhim:** bir xil Telegram token bilan ikkita bot (masalan, Replit va server)
> bir vaqtda ishlamasin. Ikkalasi ham kanalga post qiladi. Avval **alohida test
> bot va test kanal** bilan sinang; production tokenga o‘tishdan oldin Replit'dagi
> botni to‘xtating.

## 1. Kerakli narsalar

- Serverda Docker va veb-panel (Portainer / Coolify / Dokploy). Agar panel
  bo‘lmasa, uni bir marta server administratori o‘rnatishi kerak bo‘ladi —
  bu yagona terminal talab qiladigan qadam va sizdan talab qilinmaydi.
- GitHub'dagi shaxsiy repozitoriyga o‘qish huquqi bor token (fine-grained,
  faqat shu repozitoriy, faqat "Contents: Read"). Panel kodni shu token bilan
  oladi.

## 2. Stack yaratish (panel orqali)

1. Panelda **Stacks → Add stack → Repository (Git)** ni tanlang.
2. Repozitoriy: `https://github.com/mardonovfayz-wq/telegram-ai-quiz-bot`,
   branch: sinov uchun `phase-3-daily-automation`.
3. Compose fayl yo‘li: `deploy/docker-compose.yml`.
4. **Environment variables** bo‘limiga `deploy/quiz-bot.env.example` dagi
   nomlarni kiriting va qiymatlarni shu yerda to‘ldiring (kalitlar faqat panelda
   saqlanadi, GitHub'ga tushmaydi):
   - `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`, `ADMIN_ID`, `CHANNEL_ID`
   - `HOOK_TOKEN` — kamida 24 belgidan iborat tasodifiy maxfiy satr
     (Activepieces ishlatilsa). Bo‘sh qoldirilsa, HTTP hook o‘chiq.
   - `ACTIVEPIECES_NETWORK` — Activepieces konteynerlari ulangan Docker tarmog‘i
     nomi (panelning **Networks** sahifasida ko‘rinadi).
5. **Deploy** tugmasini bosing. Ma’lumotlar bazasi `quiz-bot-data` hajmida
   (volume) saqlanadi va qayta deploy qilinganda yo‘qolmaydi.

## 3. Tekshirish (Telegram orqali)

1. Test botga shaxsiy chatda `/start` yuboring.
2. `/autogen` — kunlik generatsiya **o‘chiq** ekanini ko‘rasiz.
3. `/upload` bilan test hujjat yuboring, keyin `/autogen run` — 10 tagacha
   qoralama keladi (hech narsa avtomatik e’lon qilinmaydi).
4. Bir nechtasini tasdiqlang, `/schedule 1 HH:MM` bilan yaqin vaqtni qo‘ying va
   test kanalda bitta post chiqishini tekshiring. `/audit` da yozuvlarni ko‘ring.
5. Hammasi to‘g‘ri bo‘lsa, `/autogen on` — kunlik 09:00 generatsiya yoqiladi.

## 4. Activepieces bilan ishlash (ixtiyoriy)

Bot o‘zi ham har kuni 09:00 da ishlay oladi (`/autogen trigger internal`,
standart). Activepieces orqali boshqarmoqchi bo‘lsangiz:

**Flow 1 — "Quiz bot: kunlik qoralamalar"**
1. Trigger: **Schedule → Every Day**, soat `9`, vaqt zonasi `Asia/Tashkent`,
   dam olish kunlari ham.
2. Qadam: **HTTP → Send HTTP request**
   - Method: `POST`
   - URL: `http://quiz-bot:8080/hooks/daily-generation` (bir Docker tarmog‘ida)
   - Authentication: Bearer, token = `HOOK_TOKEN`
   - Timeout: 30 soniya; xatoda flow davom etsin ("continue on failure").
   - Javoblar: `202` — boshlandi; `409` — o‘chiq / bugun bajarilgan / band;
     `401` — token noto‘g‘ri.
3. Shart: javob kodi `202` yoki `409` bo‘lmasa → **Telegram Bot → Send Text
   Message** orqali `ADMIN_ID` ga ogohlantirish.
   Telegram ulanishi uchun bot tokenidan foydalaning, lekin **Telegram "New
   Update" trigger'ini ishlatmang** — u webhook o‘rnatadi va botning o‘zi
   xabar ololmay qoladi.

**Flow 2 — "Quiz bot: holat nazorati"** (tavsiya etiladi)
1. Trigger: **Schedule → Every Hour**.
2. **HTTP GET** `http://quiz-bot:8080/healthz`.
3. Javob `200` bo‘lmasa → Telegram orqali adminga "Bot ishlamayapti" xabari.
   (Bot to‘xtab qolsa, o‘zi xabar yubora olmaydi — buni Activepieces qiladi.)

So‘ng botda: `/autogen trigger activepieces` va `/autogen on`. Kunlik ishga
tushirish bazada sana bo‘yicha qayd etiladi, shuning uchun bot jadvali va
Activepieces ikkalasi chaqirsa ham generatsiya kuniga **bir marta** bo‘ladi.
