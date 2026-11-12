# Step 3T — بوابة حفظ E/B المسترجعة

أضيفت بوابة إنتاجية ضيقة لحفظ سجلات Early Core E وBase Ready B المسترجعة. لا تقبل البوابة أي كتابة إلا بوجود permit كامل الجلسة مربوط ببصمة الدليل، الجلسة، نطاق الرموز، epoch، هوية العامل والجيل القيادي.

## الضمانات

- يجب أن يثبت الـpermit: subscription ACK، وepoch واحدًا غير منقطع، وتطابق `received=handled=metrics_acked`، وعدم overflow، واكتمال pagination وتسوية native session والتداولات والحالات.
- يجب أن يتطابق التسلسل `1..received` وأن يكون capture فارغًا ومؤكدًا حتى آخر رسالة.
- تُفحص القيادة قبل Lua وبعد commit؛ وتنفذ Lua فحص المالك والجيل داخل المعاملة نفسها.
- الكتابة `SET NX` ذرية ومحدودة إلى 80 سجلًا، وإعادة المحاولة المتطابقة idempotent، وأي تعارض يمنع الدفعة كلها.
- لا تنشئ البوابة Opportunity أو Trade أو Outbox، وتبقى `retroactive_entries_allowed=false` و`direct_handoff_authorized=false` و`shadow_deploy_authorized=false`.

## الحد الحالي

البوابة آمنة ومختبرة، لكنها غير موصولة بعد بمنسق SIP الإنتاجي، ولا يوجد حتى الآن permit جلسة كاملة حقيقي. لذلك لا تسمح Step 3T وحدها بالنشر أو Shadow أو DIRECT.
