# Independent Priority Radar

بوت مستقل للمراقبة والتنبيه يستفيد من نموذج الجودة في Phase 2، ويحفظ كل مرشحي الأولوية، ولا يرسل إلى تيليجرام إلا بعد تأكيد بسيط على شمعة دقيقة مكتملة.

## ما يفعله

- يبني نموذج الجودة المجمد مرة واحدة من سجلات Development القديمة في Redis ويحفظه تحت namespace جديد.
- يبني قائمة الأسهم بنفسه من Alpaca ويجري مسحاً مستقلاً.
- يحفظ كل مرشح Top 5% في Redis ويطبعه في اللوق.
- يرسل تيليجرام فقط عند إغلاق دقيقة فوق المقاومة دون ظل علوي رافض.
- يحفظ لقطة الفلوت من الكاش اليومي ولقطة الخبر من `market_radar:news` لحظة الترشيح.
- يتابع كل حالة 60 دقيقة ويحفظ MFE/MAE والوصول إلى +2% و+5% و+10%.
- يرسل ملخصاً أسبوعياً مختصراً الجمعة 17:15 بتوقيت نيويورك، بعد اكتمال متابعة آخر مرشح.
- لا يملك أي كود لإرسال أوامر شراء أو بيع.

## ملفات الرفع

- `independent_priority_radar.py`
- `requirements.txt`
- `runtime.txt`

Start Command:

```text
python independent_priority_radar.py
```

## متغيرات Render المطلوبة

- `UPSTASH_REDIS_REST_URL`
- `UPSTASH_REDIS_REST_TOKEN`
- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `IPR_ADMIN_TOKEN`، أو يواصل استخدام `NDR_BT_ADMIN_TOKEN` الموجود.

متغيرات اختيارية:

- `IPR_REDIS_PREFIX=independent_priority_radar:v1`
- `NDR_BT_REDIS_PREFIX=next_day_radar_backtest_v3`
- `IPR_FLOAT_KEYS=market_radar:float,elite_catalyst:float`
- `IPR_NEWS_KEY=market_radar:news`
- `IPR_SCAN_INTERVAL_SEC=90`
- `IPR_SNAPSHOT_REFRESH_SEC=300`
- `IPR_UNIVERSE_REFRESH_SEC=14400`
- `IPR_CONFIRMATION_WINDOW_MIN=15`
- `IPR_PRICE_MIN=0.50`
- `IPR_PRICE_MAX=40.00`
- `IPR_MIN_DAY_VOLUME=150000`
- `IPR_MIN_DOLLAR_VOLUME=500000`
- `IPR_MAX_DEEP_SYMBOLS=1200`

## أول تشغيل

قد يستغرق أول تشغيل عدة دقائق لأن البوت يقرأ حالات البحث القديمة، يركب مجموعة Development، يدرب نموذج L2=1.0 مرة واحدة، ويقفل بصمته. بعد ذلك يحمل النموذج المحفوظ مباشرة في كل إعادة تشغيل.

يمكن تشغيله على خدمة `next-day-radar-backtest` الحالية بعد انتهاء البحث، دون إنشاء خدمة Render إضافية: ارفع ملفات التشغيل إلى مجلد `ndr_backtest` وغيّر Start Command فقط إلى `python independent_priority_radar.py`. اترك ملفات البحث القديمة في GitHub وRedis ولا تحذفها.

راقب:

- `/health`: الخدمة حية.
- `/ready`: النموذج والاتصالات جاهزة.
- `/status`: حالة المسح وتفاصيل النموذج المقفل.
- `/api/candidates/recent`: آخر العينات المحفوظة.
- `/api/candidate/<candidate_id>`: العينة الكاملة.
- `/api/weekly/latest`: آخر ملخص أسبوعي محفوظ.
- `/protocol`: البروتوكول وبصمته.

## ضوابط مهمة

- احتمال النموذج المعروض هو درجة ترتيب، وليس احتمال ربح مضموناً.
- التأكيد اللحظي لم يُثبت كسياسة تداول؛ لذلك تحفظ الحالات المؤكدة وغير المؤكدة للمقارنة.
- لا تُغير حدود التأكيد قبل خمس جلسات لفحص التشغيل، ولا تعتمد فلتر جديد قبل عشر جلسات و100 مرشح على الأقل.
- لا تحذف مفاتيح الباك تيست القديمة؛ يحتاجها البوت في أول بناء للنموذج فقط.
