# أدوات تطوّر شغل Claude Code في مشروع الفرسان

هذي الأدوات الخمس من الفيديو، مع أوامر التثبيت الصحيحة وكيف تفيد مشروعنا
(المتجر + لوحة الأدمن + Firebase Functions + بوت التليغرام).

> شغّل الأوامر على جهازك داخل Claude Code (في الطرفية). أوامر `/plugin` تُكتب داخل جلسة Claude Code نفسها.

---

## 1) Claude Code Setup — إضافة رسمية من Anthropic

تفحص كود المشروع وتقترح أنسب Skills وسيرفرات MCP وhooks له.

```
/plugin install claude-code-setup@claude-plugins-official
```

بعد التثبيت اطلب منها: «افحص المشروع واقترح لي أدوات». متوقع تقترح مثلاً
أدوات لـ Firebase وPython (البوت) وPlaywright لفحص صفحات `index.html` و`admin.html`.

## 2) claude-mem — ذاكرة دائمة بين الجلسات

يحفظ ملخص كل جلسة ويرجّعه تلقائياً بالجلسة الجاية، فما تحتاج تعيد شرح
المشروع (منطق تسعير الدرزن/الكارتون، أسعار الموردين، إلخ) كل مرة.

```
/plugin marketplace add thedotmack/claude-mem
/plugin install claude-mem@thedotmack
```

ثم أعد تشغيل Claude Code. المصدر: https://github.com/thedotmack/claude-mem

## 3) Strix — فحص الثغرات الأمنية

وكلاء ذكاء اصطناعي يعملون اختبار اختراق حقيقي على الكود والموقع ويقترحون الإصلاح.
مهم لنا لأن عندنا دوال حساسة: `createOrder`، `updateOrderStatus`، `checkRepPin`،
`fbOAuthCallback`، ولوحة الأدمن.

إضافة مهارات Strix لـ Claude Code:

```
npx skills add usestrix/strix
```

أو الأداة نفسها (تحتاج Docker ومفتاح نموذج ذكاء اصطناعي):

```
curl -sSL https://strix.ai/install | bash
strix --target ./
```

⚠️ افحص فقط مواقع وأكواد تملكها. المصدر: https://github.com/usestrix/strix

## 4) Agent Reach — جمع البيانات من الإنترنت

يخلّي Claude يقرأ من Instagram وLinkedIn وX وReddit وغيرها، حتى المنصات اللي
تحتاج تسجيل دخول، بدون اشتراك API مدفوع. مفيد لمتابعة أسعار المنافسين
والموردين وتعليقات الزبائن.

التثبيت: اكتب لـ Claude Code:

```
ثبّت لي Agent Reach: https://raw.githubusercontent.com/Panniantong/Agent-Reach/main/docs/install.md
```

⚠️ تنبيهات:
- لا تثبّته من PyPI (الحزمة `agent-reach` هناك مو هذا المشروع) — فقط من رابط GitHub أعلاه.
- ينسخ جلسات تسجيل الدخول من متصفح Chrome عندك؛ استخدم حسابات ثانوية مو حساباتك الرئيسية.
- التزم بشروط استخدام كل منصة.

المصدر: https://github.com/Panniantong/Agent-Reach

## 5) Awesome Free LLM APIs — نماذج ذكاء اصطناعي مجانية

قائمة بكل واجهات النماذج المجانية (Gemini، Groq، OpenRouter...) مع الحدود اليومية،
عشان تكمل شغلك لما يخلص حد الاستخدام. يفيد بوت التليغرام مباشرة لأنه يستخدم Gemini
(`telegram-bot/gemini_extractor.py`) — نقدر نضيف نموذج احتياطي مجاني لو تعطل Gemini.

المصدر: https://github.com/mnfst/awesome-free-llm-apis

---

---

# الفيديو الثاني: 5 إضافات مجانية (تم التحقق منها من صفحاتها الرسمية)

## 6) Agent Skills — يبرمج مثل مبرمج senior
حزمة 25 مهارة من Addy Osmani (مدير سابق بجوجل)، تغطي كل مراحل المشروع: تحديد المتطلبات، التخطيط، البرمجة، الاختبار (TDD)، المراجعة، والنشر.

```
/plugin marketplace add addyosmani/agent-skills
/plugin install agent-skills@addy-agent-skills
```
أو `npx skills add addyosmani/agent-skills --list` لتشوف المهارات قبل التثبيت وتختار منها.
أنفع شي لنا: مراجعة الكود قبل الدمج، وTDD لملف `pricing.py`.

## 7) Context7 — توثيق المكتبات بأحدث إصدار
يعطي Claude توثيق حديث لأي مكتبة، فما يكتب كود بدوال قديمة. مفيد لنا مع `firebase-functions` و`python-telegram-bot` و`google-genai` (اللي تتغير واجهاتها كثير).

```
npx ctx7 setup
```
للحذف: `npx ctx7 remove`. المصدر: https://github.com/upstash/context7

## 8) Graphify — يحوّل الكود لخريطة معرفة
يقرأ ملفات المشروع ويبني خريطة تربط الأجزاء ببعض، فClaude يفهم المشروع بدون ما يقرأ كل الملفات (يوفر استهلاك).

```
pip install graphifyy && graphify install
```
(الاسم بحرفين y بالنهاية. إذا طلع خطأ externally-managed استخدم `pipx install graphifyy`.) ثم اكتب `/graphify` داخل Claude Code.
المصدر: https://github.com/safishamsi/graphify

## 9) 21st MCP — واجهات وتصاميم جاهزة
مكتبة آلاف مكونات الواجهات (React) يستدعيها Claude بطلب واحد، مثل «سوي صفحة أسعار احترافية». تنفع لتحسين تصميم `index.html`.

```
npx @21st-dev/cli@latest init --client claude
```
يحتاج مفتاح API من https://21st.dev/mcp (لا تكتبه بالمحادثة أو بملفات المشروع). ملاحظة: المكونات بصيغة React وصفحاتنا HTML عادي، فتحتاج تحويل.

## 10) Strix
نفس الأداة الموجودة بالأعلى (رقم 3): تهاجم تطبيقك مثل هاكر محترف وتلقى الثغرات وتصلحها. الفيديو يعتبرها أهم إضافة، وفعلاً أنصح تشغلها على دوال الطلبات قبل أي إطلاق.

---

# الفيديو الثالث: 7 سكيلات (تم التحقق منها من صفحاتها الرسمية)

| # | السكيل | شنو يسوي | التثبيت (داخل Claude Code) |
|---|---|---|---|
| 1 | **frontend-design** (رسمي من Anthropic) | يصمم واجهات ما تبين "مصنوعة بالذكاء الاصطناعي" | `/plugin install frontend-design@claude-plugins-official` |
| 2 | **Humanizer** | يعيد كتابة النص حتى ما ينكشف أنه AI، ويقلد أسلوبك | `/plugin marketplace add blader/humanizer` ثم `/plugin install humanizer@humanizer` |
| 3 | **أوامر موجودة أصلاً** | `/simplify` و`/security-review` و`/loop` جاهزة بدون تثبيت | لا يحتاج تثبيت |
| 4 | **HyperFrames** (HeyGen) | يسوي فيديوهات وأنيميشن احترافي من الكود | `/plugin marketplace add heygen-com/hyperframes` ثم `/plugin install hyperframes@hyperframes` |
| 5 | **Marketing Skills** | فريق تسويق كامل: SEO، كتابة إعلانات، تحسين التحويل، إيميلات | `/plugin marketplace add coreyhaines31/marketingskills` |
| 6 | **Claude in Chrome** (رسمي) | يتحكم بمتصفحك: يتصفح ويعبي النماذج ويبحث بدالك | ملحق Chrome الرسمي من متجر Chrome، ثم `claude --chrome` |
| 7 | **Superpowers** | يفكر ويخطط قبل ما يبني (الهدف، المستخدم، الأولويات) ثم يبني خطوة خطوة بالاختبارات | `/plugin install superpowers@claude-plugins-official` |

**ملاحظة على 3:** الفيديو يذكر `/deep-research` كأمر موجود، لكن ما قدرت أتأكد منه بنسختي الحالية، فجربه عندك. `/simplify` و`/security-review` و`/loop` أكيد موجودين.
**أنفع لمتجرنا:** frontend-design لتحسين `index.html`، وMarketing Skills لنصوص المنتجات والإعلانات، وHumanizer لمنشورات الفيسبوك، وSuperpowers قبل أي ميزة كبيرة.

## نسخة وحدة تلصقها كلها (داخل Claude Code)

```
/plugin install claude-code-setup@claude-plugins-official
/plugin install frontend-design@claude-plugins-official
/plugin install superpowers@claude-plugins-official
/plugin marketplace add thedotmack/claude-mem
/plugin install claude-mem@thedotmack
/plugin marketplace add addyosmani/agent-skills
/plugin install agent-skills@addy-agent-skills
/plugin marketplace add blader/humanizer
/plugin install humanizer@humanizer
/plugin marketplace add heygen-com/hyperframes
/plugin install hyperframes@hyperframes
/plugin marketplace add coreyhaines31/marketingskills
```
ثم من الطرفية: `npx ctx7 setup` و`npx skills add usestrix/strix`.

⚠️ لا تثبّت الكل مرة وحدة: كل سكيل ياخذ جزء من سياق Claude ويقلل تركيزه. ابدأ بـ 4 أو 5 تحتاجها فعلاً (أنصح: superpowers وfrontend-design وContext7 وStrix وclaude-mem) وزيد حسب الشغل.

### ترتيب مقترح

1. `claude-code-setup` أولاً (رسمية وآمنة) ← خله يقترح الباقي.
2. `claude-mem` للذاكرة.
3. Strix لفحص دوال الطلبات ولوحة الأدمن قبل أي إطلاق.
4. Agent Reach وقائمة الـ APIs المجانية حسب الحاجة.
