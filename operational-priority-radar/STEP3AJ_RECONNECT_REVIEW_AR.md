# Step 3AJ — اختبار إعادة الاتصال المنفصل

أظهر تشغيل 24 سبتمبر 2026 اتصالًا واحدًا فقط عبر `run_once()`. انتهى قبل اكتمال جلسة السوق بخطأ WebSocket 1011 (مهلة Pong/Ping)، ولم يُختبر فيه `reconnect_loop()`؛ نجاح حفظ ملف الدليل لا يعني نجاح الجلسة.

يضيف هذا التغيير خيار `--reconnect-probe` إلى `live_sip_soak.py`، ويستدعي `WebSocketRuntime.reconnect_loop()` الحقيقي. يحتفظ بإثبات ACK مستقل لكل حقبة، وعداد تصريف مستقل، والسجل النهائي قبل إبطال الحقبة. يوقف التجربة عند overflow. يصنف 407 الحقيقي فقط إذا جاء من إطار SIP من النوع `error`، ولا يصنف استثناء يذكر النص 407 على أنه إطار.

التجربة للقراءة فقط. أي انقطاع يفتح فجوة تاريخية لم تثبت تسويتها؛ لذلك تبقى `continuity_proven` و`full_session_coverage_proven` و`direct_handoff_authorized` و`retroactive_entries_allowed` و`production_leadership_proven` جميعًا false، حتى بعد ACK جديد. لا تصل الأداة بـRedis أو إنتاج E/B. نجاح الاختبار الاصطناعي لا يثبت التعافي في جلسة Alpaca الفعلية، ولا سبب مهلة Ping.

التشغيل الميداني يحتاج اعتمادًا محفوظًا في بيئة التنفيذ، والبوابة الموجودة `OPR_LIVE_SIP_SOAK=I_UNDERSTAND_READ_ONLY_SIP`. مثال: `python live_sip_soak.py --reconnect-probe --duration-sec 1800 --max-symbols 12000 --output /tmp/OPR_STEP3AJ_RECONNECT_EVIDENCE.json`. لا تستخدم خيارات session-start/end في هذا الوضع؛ لا يدعي شمول جلسة التداول. يلزم اختبار REST وتسوية فجوة الانقطاع بشكل مستقل قبل الادعاء بالاستمرارية أو تفعيل Shadow.
