# QueuePilot — Independent Contact Center Platform

**الإصدار الحالي: v1.0 (Native SIP + Single WAV + Local Voice IVR Lab) — مرحلة تطوير واختبار، غير جاهز للإنتاج.**

> **الرؤية الأساسية:** Avaya Aura/Communication Manager تستقبل المكالمات من شبكة الاتصالات وتُسلّمها إلى QueuePilot عبر **SIP Trunk** فقط. بعد التسليم، **QueuePilot هو المسؤول الوحيد عن منطق مركز الاتصال**: استقبال جلسات SIP، الـIVR، اختيار الـQueue، توزيع المكالمات (ACD)، الاتصال بتليفونات الموظفين، مرور الصوت (RTP)، التسجيل، متابعة المشرفين والتقارير.
>
> **لا يستخدم المشروع Asterisk أو FreeSWITCH أو Avaya AES/WTI/TSAPI/DMCC أو أي PBX/ACD خارجي.** محرك SIP/RTP الجاري تطويره جزء من كود QueuePilot نفسه، وليس نظامًا وسيطًا. هذا وصف **للهدف المعماري**؛ وليس ادعاءً بأن كل هذه الوظائف اكتملت الآن.

**English summary:** QueuePilot is intended to be a self-contained, native SIP contact center. Avaya Aura provides inbound SIP handoff only. QueuePilot owns its ACD, phone registration, two-leg call control, anchored media, IVR, recording, monitoring and reporting. Current code is a **restricted lab prototype**, not a deployable production switch; the features below are explicitly distinguished as tested, partial or pending.

## 1. الفكرة: ما الذي تديره Avaya وما الذي يديره QueuePilot؟

| Avaya Aura — قبل تسليم المكالمة | QueuePilot — بعد التسليم |
|---|---|
| استقبال المكالمة من PSTN / خطوط الشركة | استقبال `INVITE` على SIP listener مستقل |
| اختيار SIP route المخصص لتسليم المكالمة | تحديد القسم من DID ثم IVR صوتي محلي واختيارات DTMF أو تحويل مباشر للطابور |
| تسليم SIP signaling والصوت وفق إعدادات الـTrunk | إنشاء Queue، تشغيل ACD، اختيار الموظف، إرسال `INVITE` إلى تليفونه |
| توفير مسار احتياطي عند تعطل QueuePilot حسب تصميم الشبكة | تمرير RTP عبر QueuePilot، تشغيل التسجيل والمراقبة عند اكتمال تطويرهما |
| **لا تدير موظفي QueuePilot أو مهاراتهم أو التسجيلات أو التقارير** | إدارة بيانات المكالمات والموظفين والصلاحيات والتقارير من داخل المنصة |

التليفون **لا يشترط أن يكون من علامة تجارية معينة**؛ لكنه يجب أن يدعم SIP ويقبل التسجيل على QueuePilot أو الاتصال المباشر به وفق نظام تسجيل/تهيئة متوافق. **Avaya IP Phone لا يعني تلقائيًا SIP Phone**: أجهزة H.323-only أو التي تتطلب خوادم تسجيل/تهيئة Avaya لا يمكن افتراض توافقها مع المحرك الحالي. يلزم اختبار كل موديل وFirmware فعليًا قبل أي تشغيل حقيقي.

## 2. الصورة الكاملة للنظام (Target Architecture)

```mermaid
flowchart TD
    Caller[المتصل / PSTN] --> Aura[Avaya Aura / Communication Manager]
    Aura -->|SIP INVITE + SDP / RTP handoff| Ingress[QueuePilot Native SIP Ingress]
    subgraph QP[QueuePilot - نظام مستقل]
        Ingress --> Call[Native SIP B2BUA / Call Controller]
        Call --> IVR[IVR صوتي / RFC4733 DTMF (معملي) / Business Hours مستهدف]
        IVR --> Queue[ACD / Skills / Priority / Queues]
        Queue --> Registrar[SIP Registrar / Agent Location]
        Call <--> Media[RTP Media Anchor / Recording / Monitor]
        Media --> Events[Call Events / CDR / Analytics]
        Events --> DB[(QueuePilot Database)]
        DB --> Dashboard[Supervisor Dashboard / Reports]
    end
    Registrar -->|SIP INVITE| Phone[IP Phone SIP متوافق]
    Media <-->|RTP anchored through QueuePilot| Phone
```

- **SIP / B2BUA:** إنشاء جلسة مكالمة منفصلة مع Avaya، وجلسة أخرى مع تليفون الموظف، وإدارة حالات الاتصال. بعض أوامر SIP الأساسية موجودة **معمليًا**؛ لا توجد بعد تغطية كاملة لـRFC 3261.
- **ACD:** اختيار الموظف المؤهل بناءً على المهارات، حالة الجاهزية، الأولوية، وأطول مدة انتظار للموظف؛ ثم معالجة عدم الرد وإعادة المحاولة.
- **RTP Media Anchor:** الصوت يمر عبر QueuePilot في الاتجاهين، وليس مباشرة من Avaya إلى التليفون؛ ده شرط أساسي للتسجيل والاستماع المباشر والـIVR داخل QueuePilot.
- **Management & Reports:** لوحة الإدارة وقاعدة البيانات جزء من نفس المشروع. حاليًا تطبيق الويب ومحرك SIP يعملان في **عمليتين محليتين منفصلتين تابعتين لـQueuePilot**؛ وليس في أي PBX مستقل.

## 3. رحلة المكالمة المطلوبة — خطوة بخطوة

1. **وصول المكالمة:** العميل يتصل برقم الشركة؛ Avaya تستقبلها وتوجهها إلى عنوان QueuePilot المسموح به عبر SIP Trunk، مع رقم المتصل والرقم المطلوب (DID) حسب إعدادات التسليم.
2. **التحقق:** QueuePilot يتحقق من عنوان مصدر الـSIP والرسالة والـSDP وسياسات الدخول. في النسخة المعملية توجد قوائم سماح للمصادر؛ مصادقة الإنتاج عبر النقل الآمن والمواصفات الكاملة ما زالت مطلوبة.
3. **تحديد الخدمة:** يربط DID بالـQueue (متاح معمليًا). يمكن اختيار Queue مباشرة من DID أو تشغيل رسالة WAV صوتية (8kHz PCM16) واستقبال DTMF برقم واحد عبر RFC4733 ثم توجيه المكالمة للـQueue المناسبة **معمليًا**؛ مواعيد العمل والقوائم المتعددة والتكامل مع محرر IVR لم تكتمل.
4. **الدخول للطابور:** ACD يسجّل المكالمة ويبحث عن موظف جاهز تتطابق مهاراته مع متطلبات المكالمة، مع مراعاة الأولوية وأطول مدة توافر؛ وإذا لم يرد الموظف يجرّب غيره حسب السياسة.
5. **الاتصال بالموظف:** QueuePilot يحدد مكان تليفون SIP المسجل لديه ويُرسل له `INVITE` مباشرة. **التسجيل والاتصال مجرّبان في بيئة SIP محاكاة فقط**.
6. **الرد ونقل الصوت:** بعد قبول الموظف، يربط QueuePilot طرفَي المكالمة ويمرّر G.711 RTP عبر محرك الصوت الداخلي. **ده مُختبَر معمليًا، وليس على Avaya فعلية**.
7. **التسجيل والمراقبة:** في التصميم النهائي يتم التسجيل والاستماع بإذن وصلاحيات وتدقيق، مع احتفاظ QueuePilot بالوسائط. الموجود حاليًا: تسجيل WAV واحد ستيريو تشخيصي اختياري لكل مكالمة (مغلق افتراضيًا)، ومكتبة لاستيراد التسجيلات وواجهة طلبات المراقبة، **مع ربط محلي اختياري: **ملف WAV ستيريو واحد للمكالمة** (العميل يسار والموظف يمين، مع محاذاة زمنية تقريبية وحشو صمت)؛ ليس نظام تسجيل إنتاجيًا معتمدًا**.
8. **الإغلاق والتقارير:** تُحدَّث حالة المكالمة والموظف (ومنها After-Call Work) وتُسجَّل الأحداث. التكامل المحلي الاختياري يكتب أحداث دورة المكالمة في قاعدة البيانات لعرضها بالداشبورد؛ **الاستمرارية وإعادة تشغيل الأحداث المفقودة غير مكتملتين**.

### مثال توضيحي

```text
المتصل يطلب 6000
  -> Avaya Aura: SIP handoff إلى QueuePilot
  -> QueuePilot: DID 6000 -> support
  -> ACD: agent-1 جاهز + مهارة english
  -> QueuePilot: SIP INVITE إلى امتداد 1001 المسجل عليه
  -> الموظف يرد -> QueuePilot يربط G.711 RTP
  -> انتهاء المكالمة -> Call Event -> Dashboard / Reports
```

الأرقام والأسماء أعلاه **عينة اختبار فقط**، وليست إعدادات شبكة العميل الحقيقية.

## 4. الوظائف: المنفّذ حاليًا مقابل الهدف

| الوظيفة | الحالة في v1.0 | ما ينقص للإنتاج |
|---|---|---|
| SIP REGISTER / مصادقة التليفون | ✅ اختبار محلي: Digest SHA-256 + nonce | توافق التليفونات الفعلية، TLS، تهيئة ومراقبة الأجهزة |
| استقبال SIP من مصدر مسموح + DID mapping | ✅ اختبار معملي | تكامل Avaya Trunk الحقيقي وتوافق SIP/SDP |
| ACD Skills / Priority / Longest Idle / retry / ACW | ✅ قواعد واختبارات محلية | حالة دائمة، مزامنة الموظفين ومعالجة إعادة التشغيل |
| اتصال الموظف SIP ثنائي الطرف (INVITE/ACK/BYE/CANCEL) | ✅ نطاق معملي محدود | معاملات SIP كاملة، timers/retransmissions، transfer/hold/re-INVITE |
| نقل الصوت G.711 RTP في الاتجاهين | ✅ اختبار محلي + صوت رسالة IVR وRFC4733 | SRTP/RTCP/jitter/codec interoperability ومراقبة الجودة |
| Dashboard / إدارة Queues وAgents | ✅ واجهة وAPI محليًا | صلاحيات مؤسسية، مزامنة حية كاملة، نشر آمن |
| حفظ أحداث Native SIP في قاعدة البيانات | 🟡 اختياري بـ`--local-events` | Event journal دائم وإعادة إرسال ومطابقة CDR |
| IVR Flow Editor / إنشاء مسودات | 🟡 موجودة، منفصلة عن التشغيل | ربط المسودات المعتمدة بالمحرك وإدارة الإصدارات |
| Voice IVR RTP + RFC4733 DTMF + queue choice | ✅ معمل: WAV محلي، رد SIP مبكر، اختيار رقم، إعادة المحاولة/الـFallback | قوائم متداخلة وTTS ومواعيد العمل، Avaya interop وTLS/SRTP |
| Recording Library: استيراد/بحث/تشغيل | 🟡 استيراد وتسجيل تلقائي معملي اختياري من Native RTP بعد انتهاء المكالمة | استكمال التخزين الآمن والتشفير والاحتفاظ واختبار Avaya |
| Single Call WAV | ✅ Lab: ملف واحد ستيريو (العميل يسار/الموظف يمين)، وربطه بالمكتبة بعد انتهاء المكالمة | دقة التوقيت/جودة الإنتاج، والموافقة والتوافق مع أجهزة Avaya |
| Live Listen / Approval / Browser Audio Gateway | 🟡 API وWorkflow فقط | ربط RTP mixer/observer حي وآمن بالمكالمة |
| Whisper / Barge / Conference | ❌ مخطط | Mix/Inject Media وSIP Conference Controller |
| Hold / Transfer / Music on Hold | ❌ غير مكتمل | تطوير كامل وإجراء اختبارات تداخل المكالمات |
| Reports / CSV / JSON / ZIP | 🟡 10 مجموعات بيانات | اكتمال الأحداث، مقاييس الإنتاج، استرجاع الأعطال وموثوقية الأرقام |
| High Availability / Failover / PostgreSQL / SSO | ❌ غير منفذ | تنفيذ واعتماد تشغيلي |

**لا يجوز تفسير ✅ على أنها اعتماد إنتاجي**: المقصود نجاح اختبارات محلية بمحاكاة SIP، ولم يُختبر بعد جهاز Avaya IP Phone حقيقي أو SIP Trunk تابع لعميل.

## 5. إدارة الكول سنتر والتقارير

اللوحة الحالية توفر إدارة الطوابير والموظفين، سجل المكالمات، عرض مؤشرات SLA أساسية، مكتبة تسجيلات مستوردة، مسودات IVR، وطلبات موافقة للمراقبة. التقارير المتاحة حاليًا عبر `/api/cc/reports`:

| Report | المحتوى |
|---|---|
| `calls` | تفاصيل المكالمات وحالاتها |
| `queues` | أداء الطوابير |
| `agents` | أداء الموظفين ومؤشرات الرد |
| `intervals` | تقسيمات زمنية نصف ساعة |
| `sla` | مستوى الخدمة المحسوب من الأحداث المسجلة |
| `recordings` | بيانات التسجيلات المستوردة والتسجيلات المعملية المستقلة بصيغة WAV ستيريو للمكالمة الواحدة |
| `monitoring` | طلبات/حالات المراقبة وتدقيقها |
| `ivr` | بيانات مسودات وتعديلات تدفقات IVR |
| `audit` | سجل العمليات الإدارية |
| `events` | الأحداث المستلمة من محرك المكالمات |

- صيغ التصدير الموجودة: **JSON، CSV، وZIP مجمّع يضم ملفات CSV**. **XLSX/PDF ليست صيغ تصدير مدمجة حاليًا**.
- التقارير الحالية **مشتقة من الأحداث الموجودة بقاعدة البيانات**، ولذلك لا تُعد تقارير تشغيلية نهائية قبل وجود مصدر أحداث موثوق ومتصل بكل دورة مكالمة وتسجيل حالات الموظفين بدقة.
- من مقاييس الإنتاج المستهدفة مستقبلًا: Offered/Answered/Abandoned، ASA، AHT، Service Level، Occupancy، Agent States/AUX/ACW، Transfers، Callback، Recording Coverage، Quality، Interval/Team/Skill reports.

## 6. بنية المستودع

```text
.
├── app/
│   ├── main.py                 # FastAPI routes, admin dashboard, native event API
│   ├── service.py              # Call lifecycle and reporting domain
│   ├── contact_center.py       # Recordings library, monitoring workflow, IVR drafts, report exports
│   ├── db.py                   # SQLite schema and migration support
│   ├── audit.py, security.py, config.py, models.py
│   └── static/                 # Web dashboard (HTML, CSS, JS)
├── telephony/
│   ├── native_switch.py        # Embedded SIP call controller + native IVR; LAB ONLY
│   ├── ivr.py                  # Local WAV validation and PCM16 -> G.711 encoding
│   ├── sip_stack.py            # SIP message parsing/auth/SDP primitives
│   ├── native_sip.py           # Ingress rules, phone directory, call planning
│   ├── acd.py                  # Skills-based queue scheduling
│   ├── rtp_media.py            # Embedded G.711 RTP relay and native recorder hookup
│   ├── stereo_recording.py     # One aligned two-channel WAV per call (lab)
│   ├── local_events.py        # Optional direct local event writer into QueuePilot DB
│   ├── recording_archive.py   # Consent-gated single stereo WAV archival into QueuePilot recording library
│   └── native_switch.example.json
├── tests/                      # Backend, ACD, signaling/media and event tests
├── docs/
│   ├── NATIVE_SIP_CALLCENTER_ARCHITECTURE.md
│   └── CODEBASE_CLEANUP_AUDIT.md
├── .github/workflows/ci.yml   # Automated lab checks (when GitHub Actions runs)
├── .env.example                # Placeholders; never commit real secrets
├── setup_demo.py               # One-time local demo configuration
├── run.py                      # Management web API
└── requirements.txt
```

**ما أزيل عمدًا:** كود تحكم Avaya AES/WTI، Bridge الخاص به، وصفحة إدارة Avaya، الأمثلة والوثائق والاختبارات المرتبطة بالبنية القديمة، وأمثلة Asterisk. ملفات البيانات القديمة الموجودة مسبقًا في قاعدة بيانات مطوَّرة **لا تُحذف تلقائيًا**.

## 7. التشغيل المحلي — تجربة التطوير فقط

**المتطلبات:** Python 3.11+، شبكة محلية/Loopback معزولة لاختبارات SIP، ونظام تشغيل مناسب. لا تشغّل المحرك الحالي على SIP Trunk إنتاجي.

### 7.1 تشغيل لوحة QueuePilot

```bash
# من جذر المستودع
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell:
# .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python setup_demo.py
python run.py
```

افتح `http://127.0.0.1:8000` من جهاز الاختبار. يُنشئ `setup_demo.py` ملف `.env` وكلمة مرور مدير عشوائية يعرضها **مرة واحدة**، ويرفض الكتابة فوق `.env` موجود. وضع Demo مخصص للاختبار فقط.

بديلًا عن Demo، انسخ `.env.example` إلى `.env` واضبط **كل** المتغيرات والأسرار المطلوبة بصورة صحيحة. لا ترفع `.env` إلى GitHub.

### 7.2 تشغيل محرك SIP الداخلي في معمل معزول

```bash
cp telephony/native_switch.example.json telephony/native_switch.local.json
# Windows: انسخ الملف يدويًا أو استخدم Copy-Item.
# بدّل جميع REPLACE_ placeholders، واترك listen_ip وpeer networks على loopback للاختبارات.
python -m telephony.native_switch \
  --config telephony/native_switch.local.json \
  --lab
```

الملف المثال مُعدّ للاستماع على `127.0.0.1:15060` فقط. يرفض البرنامج البدء من غير `--lab`، وهو **ليس SIP gateway إنتاجيًا**.

### 7.3 إظهار أحداث SIP في لوحة الإدارة (اختياري/معملي)

1. شغّل لوحة الإدارة، وأنشئ Queue وAgent باستخدام الواجهة/API.
2. افتح `telephony/native_switch.local.json` واستبدل `routes[].queue` ومفاتيح `agents` بمعرّفات قاعدة بيانات QueuePilot الفعلية، وليس الأسماء الظاهرة فقط.
3. تأكد من استخدام **نفس** `APP_DB_PATH` في عمليتَي الـDashboard والـSIP.
4. أعد تشغيل محرك SIP مع الخيار `--local-events`:

```bash
python -m telephony.native_switch \
  --config telephony/native_switch.local.json \
  --lab --local-events
```

يتحقق المحرك من معرّفات Queue/Agent الموجودة؛ ويفشل بدء الربط إذا كانت غير صالحة. مسار حفظ الأحداث هذا **مزامن، أحادي المضيف، وغير مضمون الاستعادة بعد انقطاع مفاجئ**؛ فلا تستخدمه كمصدر تقارير إنتاجية.

### 7.4 الاختبارات

```bash
python -m pytest -q
python -m compileall -q app telephony tests
node --check app/static/app.js
```

**التحقق الأخير لهذه الوثيقة:** 45 اختبار Python محلي ناجح على v1.0. لا يعني ذلك أن GitHub Actions نجح أو أن Avaya الحقيقية اختُبرت.

## 8. واجهات الـAPI الرئيسية

| المسار | الغرض |
|---|---|
| `POST /api/login` | الدخول إلى لوحة الإدارة |
| `GET /api/health`, `/api/dashboard` | حالة الخدمة وإحصائيات اللوحة |
| `POST /api/queues`, `POST /api/agents` | إدارة بيانات الطوابير والموظفين |
| `POST /api/engine/events`, `POST /api/engine/heartbeat` | أحداث/نبض محرك QueuePilot الداخلية، بمفتاح `X-Engine-Key` |
| `/api/cc/recordings` | مكتبة التسجيلات المستوردة والبيانات الوصفية والتشغيل المصرّح |
| `/api/cc/monitor` | موافقات المراقبة وبوابة بث معملية، غير متصلة بـRTP الحقيقي |
| `/api/cc/ivr` | إدارة مسودات IVR؛ ليس محرك تشغيل IVR |
| `/api/cc/reports` | قائمة تقارير JSON/CSV/ZIP والتصدير |

توثيق FastAPI التفاعلي متاح محليًا على `/docs` لأغراض التطوير. لا تنشره على الإنترنت من دون حمايات مناسبة. واجهات Native Engine **داخلية**، وليست Avaya CTI APIs.


## 8.1 تطوير v0.9 — تسجيل المكالمة بالكامل في ملف واحد (معمل فقط)

تم استبدال تسجيل الاتجاهين في ملفين منفصلين بـ **ملف WAV ستيريو واحد لكل مكالمة**، يُنتجه محرك QueuePilot بنفسه أثناء مرور RTP عبره. **القناة اليسرى صوت العميل، واليمنى صوت الموظف**؛ عند التشغيل في المتصفح تسمع الطرفين من نفس الملف، وتبقى كل قناة قابلة للفحص بشكل مستقل. تعتمد المحاذاة على توقيت وصول أول حزمة من كل اتجاه مع طوابع RTP وحشو الصمت، بنطاق تأخير معمل 500 مللي ثانية. هذا **تقريب** وليس تزامنًا إنتاجيًا مضمونًا عبر شبكات مختلفة.

التسجيل **مغلق افتراضيًا**، ولا يُفعّل إلا بمعمل معزول وبموافقة سياسة الإخطار والتسجيل:

1. اضبط `RECORDINGS_ENABLED=true` و`RECORDING_DIR` في `.env` المحلي للوحة، مع حماية الملفات وتجنب رفع المفاتيح إلى GitHub.
2. في `telephony/native_switch.local.json` اضبط `record_calls=true` و`recording_notice_confirmed=true`، واستخدم مجلد `record_directory` مختلفًا عن مجلد المكتبة. إقرار السياسة ليس إثباتًا أن المتصل تلقّى إخطارًا فعليًا.
3. أنشئ الطوابير والموظفين بمعرّفات متوافقة وشغّل محرك SIP بـ`--lab --local-events` مع نفس قاعدة البيانات المحلية.
4. عند إنهاء المكالمة، يُغلق ملف WAV الوحيد، وتُحفظ بياناته في مكتبة المشرف كسجل واحد من المصدر `queuepilot-native-conversation`. بعد نجاح الأرشفة يُحذف ملف الالتقاط المؤقت وتبقى نسخة واحدة في المكتبة. لو الأرشفة فشلت يُحتفظ بالمصدر لاسترداده يدويًا، ولا يُعرض كتسجيل مكتمل.
5. يفتح المشرف الملف من `/api/cc/recordings/{id}/audio` بعد تسجيل الدخول، وتظهر بياناته في تقرير `recordings`، مع منع إنشاء سجل ثاني لنفس مكالمة Native.

الحد المعملي 30 دقيقة للالتقاط، وحد الملف المؤرشف 64 MiB. تظل الملفات التاريخية المكوّنة من اتجاهين في الإصدارات السابقة **محفوظة بدون حذف أو تحويل تلقائي**. لم يُنفّذ بعد التشفير الكامل أو التسجيل الإنتاجي أو الاستماع المباشر على RTP أو استعادة التسجيل بعد الأعطال. راجع [توثيق التسجيل](docs/NATIVE_RECORDING_LAB.md).

## 9. الأمان والخصوصية ومبادئ الاستقلال

1. **Network isolation:** ضع SIP/RTP والتليفونات على Voice VLAN وشبكات موثوقة مع Allowlist محدد؛ الوجود على نفس الشبكة لا يغني عن المصادقة.
2. **Secrets:** كلمات مرور الموظفين، مفاتيح API، وملفات `.env` و`native_switch.local.json` لا تدخل Git مطلقًا.
3. **SIP/Media protection:** UDP/G.711 المعملي ليس بديلًا عن TLS/SRTP، حماية المعاملات، مقاومة إساءة الاستخدام وحماية التسجيلات.
4. **Recording:** يلزم سياسة حفظ وتشفير وصلاحيات وإشعار/موافقة مناسبة حسب القوانين المطبقة. التسجيل التشخيصي والأرشفة داخل المكتبة معطلان افتراضيًا، والتسجيل الإنتاجي غير منفّذ.
5. **Supervisor monitoring:** كل طلب استماع يحتاج هوية مستقلة وصلاحية محددة وموافقة وتدقيق وإيقاف مضمون؛ الاستماع/Whisper/Barge على المكالمات الحية لم يُوصل بعد.
6. **Data safety:** لا تحذف قواعد البيانات أو التسجيلات القديمة أثناء الترقية؛ النسخة v0.9 تحتفظ بالجداول التاريخية وتنقل Audit القديم إلى سجل عام بدون إسقاط المصدر.
7. **Production operations:** ما زال مطلوبًا RBAC/SSO، قاعدة بيانات إنتاجية، event journal، نسخ احتياطية، High Availability، مراقبة سعة RTP/SIP، واختبارات اختراق وتحمل.

## 10. خطة التنفيذ إلى نظام مستقل متكامل

- **المرحلة A — نواة الاتصال:** استكمال SIP transactions/dialogs وSDP وTLS/SRTP والـDTMF واختبار التسجيل على موديلات Avaya SIP الحقيقية.
- **المرحلة B — IVR/ACD:** تشغيل رسائل صوتية فعلية، Multi-level IVR، ساعات العمل، Queue Music، مهارات الموظفين، حالات AUX/Ready/ACW وHold/Transfer.
- **المرحلة C — Media Operations:** تعزيز التسجيل الواحد لكل مكالمة لبيئة الإنتاج، Live Listen وWhisper وBarge بصلاحيات وتدقيق قانوني.
- **المرحلة D — Reports:** تخزين أحداث موثوق وقابل لإعادة التشغيل، سجلات CDR وAgent State دقيقة، مؤشرات SLA/ASA/AHT/Occupancy وصيغ تصدير إضافية عند تنفيذها.
- **المرحلة E — Production Readiness:** PostgreSQL، SSO/RBAC، نسخ احتياطي، failover، اختبارات ضغط، استعادة بعد الأعطال، ومصفوفة توافق التليفونات وAvaya SIP Trunk.

### شروط القبول قبل توصيل مكالمات عملاء حقيقيين

- Avaya تُسلّم مكالمة SIP إلى QueuePilot، وتختفي أي حاجة لتدخل AES/WTI في إدارة المكالمة بعدها.
- تليفون Avaya **SIP متوافق** يسجّل على QueuePilot ويرد على مكالمة حقيقية بصوت ثابت في الاتجاهين.
- IVR يستقبل DTMF ويحوّل إلى Queue صحيحة؛ ACD يتعامل مع عدم الرد والانشغال والإلغاء.
- Recording وLive Listening المصرّح بهما يعملان من نفس مسار RTP، مع مؤشرات واضحة وسياسات الموافقة.
- كل مكالمة لها سجلات كاملة، وتقارير متطابقة، ومعالجة لأحداث التكرار والفقد والتوقف.
- نتائج Security/Performance/Recovery/Interoperability موثقة قبل السماح بالتشغيل الفعلي.

## 11. وثائق المشروع والحالة

- [Native SIP architecture and technical requirements](docs/NATIVE_SIP_CALLCENTER_ARCHITECTURE.md)
- [Codebase cleanup audit / release risks](docs/CODEBASE_CLEANUP_AUDIT.md)
- [GitHub development repository](https://github.com/SamoTech/SamoQueue)

**الخلاصة:** QueuePilot ليس إضافة على Avaya، بل نواة مركز اتصال مستقل. **دور Avaya يقتصر على تسليم مكالمات SIP الواردة**؛ لكن **النسخة الحالية v0.9 لا تزال أساسًا معمليًا**. التوثيق يفرق بين ما نجح محليًا وما نعمل للوصول إليه، حتى لا تُعرَّض مكالمات أو بيانات حقيقية للخطر.


## 10. Voice IVR الفعلي داخل QueuePilot — v1.0 (معملي)

إذا تم تعريف `ivr_menus` في ملف `telephony/native_switch.local.json`، فإن QueuePilot نفسه يرد على المكالمة الواردة عبر SIP، ويشغّل ملف صوت WAV محليًا على RTP، ويستقبل مفتاح العميل من RFC4733 DTMF ويحدد الـQueue المناسبة، قبل الاتصال بتليفون الموظف. الاختيار متاح من الأرقام `0-9` و`*` و`#`، مع عدد محاولات ومهلة وQueue احتياطي اختياري.

**إعداد سريع:** أضف `ivr_menus` لقائمة ملف الإعداد المحلي كما في [دليل Native IVR](docs/NATIVE_IVR_LAB.md)، وجهّز رسالة WAV mono PCM16 بمعدل 8000 Hz ومدتها لا تتجاوز 30 ثانية. محرك الـIVR لا يعتمد على Avaya IVR أو Asterisk.

**القيود:** محرر IVR في الداشبورد لا ينشر القوائم إلى المحرك بعد؛ التحكم حاليًا يتم من JSON محلي خاص، والرسائل لا تُولَّد بالـTTS. يظل **تسجيل المكالمة ملف WAV واحدًا ستيريو** بعد اتصال الموظف (لا يشمل رسالة الـIVR)، مع شرط تفعيل سياسة التسجيل؛ ولا يُسمح باستخدام الإصدار على مكالمات العملاء حتى اجتياز اختبارات Avaya والأمان والاعتمادية.
