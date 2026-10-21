# خدمة تحقق البريد عبر Gmail لتطبيق APK

هذا المشروع يوفر واجهتي API لإرسال رمز تحقق إلى البريد وإتمام التحقق منه. لا تحتاج وظيفة OTP إلى توكن تيليجرام؛ يمكن تفعيل بوت تيليجرام الإداري بشكل اختياري.

## مهم قبل التشغيل

- كلمة مرور تطبيق Google التي ظهرت في لقطة شاشة سابقة يجب إلغاؤها وإنشاء كلمة مرور جديدة.
- إذا كان توكن Telegram قد نُشر أو شاركته في مكان غير خاص، قم بتجديده من BotFather.
- لا تضع كلمة مرور Gmail أو توكن Telegram أو `APP_API_KEY` في ملف APK ولا ترفعها إلى GitHub.
- هذا المشروع بداية قابلة للتجربة. قاعدة SQLite في إعداد Render الافتراضي قد تكون على قرص مؤقت؛ إعادة نشر الخدمة أو إعادة تشغيلها قد تفقد الرموز وحدود الإرسال. للإنتاج استخدم قاعدة بيانات دائمة (مثل PostgreSQL)، وأضف حماية ضد إساءة الاستخدام مثل App Check أو CAPTCHA، ومراقبة وحدودًا إضافية.
- نقاط API الخاصة بالتطبيق عامة عمدًا كي لا نحتاج إلى وضع سرّ داخل APK. لذلك توجد حدود إرسال لكل بريد ولكل IP؛ هذه الحدود ليست بديلًا عن الحماية الإضافية عند إطلاق التطبيق للعامة.

## الإعداد على Render

1. فك ضغط الملف على الهاتف، ثم ارفع **الملفات التي بداخله مباشرة إلى جذر مستودع GitHub**: `bot.py`, `requirements.txt`, `render.yaml`, `README_AR.md`, `.env.example`, `.gitignore`. لا تضعها داخل مجلد فرعي؛ يجب أن يكون `bot.py` في الصفحة الرئيسية للمستودع.
2. لا ترفع ملف `.env` حقيقيًا أو أي أسرار.
3. في Render أنشئ Web Service من المستودع، أو استخدم Blueprint إذا أردت استعمال `render.yaml`.
4. إذا أنشأت الخدمة يدويًا، استخدم:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python bot.py`
   - Health Check Path: `/health`
5. داخل Environment أضف:
   - `SMTP_EMAIL` = عنوان Gmail المخصص للإرسال.
   - `SMTP_APP_PASSWORD` = كلمة مرور تطبيق Google الجديدة، عادة 16 حرفًا؛ اكتبها دون مسافات.
   - `APP_NAME` = اسم تطبيقك (اختياري، الافتراضي My App).
   - `APP_API_KEY` = سر عشوائي طويل، لا يوضع داخل APK. إذا استخدمت Blueprint، يمكن أن ينشأ تلقائيًا.
   - `DATABASE_PATH` = `mailbot.sqlite3` (افتراضي).
6. اضغط Save Changes / Deploy latest commit وانتظر نجاح النشر.
7. افتح `https://YOUR-SERVICE.onrender.com/health` وتأكد أن النتيجة JSON تتضمن `"ok": true`.

### تفعيل بوت تيليجرام الإداري (اختياري)

داخل Environment أضف كلا المتغيرين:
- `BOT_TOKEN` = التوكن الحالي من BotFather.
- `ADMIN_TELEGRAM_ID` = رقم حساب المسؤول في تيليجرام.

إذا لم تضف `BOT_TOKEN`، تعمل خدمة API للبريد وحدها. التوكن ليس مطلوبًا كي يرسل التطبيق رموز التحقق.

## ربط التطبيق

بعد أن يعطيك Render رابطًا حقيقيًا، استبدل `https://YOUR-SERVICE.onrender.com` بعنوان خدمتك.

### عند ضغط المستخدم «إرسال رمز التحقق»

أرسل طلب HTTPS:

`POST https://YOUR-SERVICE.onrender.com/send-otp`

Headers:

`Content-Type: application/json`

Body JSON:

```json
{"email":"user@example.com"}
```

عند نجاح الإرسال، تكون الاستجابة مثل:

```json
{"ok":true,"message":"otp_sent","expires_in_seconds":600}
```

### عند إدخال المستخدم الرمز

أرسل:

`POST https://YOUR-SERVICE.onrender.com/verify-otp`

Headers:

`Content-Type: application/json`

Body JSON:

```json
{"email":"user@example.com","code":"123456"}
```

استبدل الرمز التجريبي بالرمز الذي كتبه المستخدم. إذا كانت الاستجابة `ok: true` و`verified: true`، أكمل خطوة التسجيل داخل التطبيق.

الأكواد تنتهي بعد 10 دقائق، وتُحذف بعد نجاح التحقق، وهناك حد أقصى لخمس محاولات، وفاصل دقيقة بين طلبات الرمز، وثلاثة طلبات لكل بريد في الساعة وعشرة لكل IP في الساعة.

## اختبار سريع دون APK

بعد النشر افتح `/health` أولًا. يمكن اختبار الواجهتين باستخدام أداة API تدعم POST وJSON، مع بريد تملكه. لا تستخدم عناوين أشخاص دون إذن.

## إذا فشل النشر

افتح Render → الخدمة → Logs. اقرأ أول رسالة خطأ حقيقية فوق `Exited with status 1`. تأكد أن `SMTP_EMAIL` و`SMTP_APP_PASSWORD` موجودان في Environment، وأن أوامر البناء والتشغيل صحيحة. لا ترسل السجلات إذا كانت تعرض أسرارًا.
