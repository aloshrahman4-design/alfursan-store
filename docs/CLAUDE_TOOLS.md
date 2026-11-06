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

### ترتيب مقترح

1. `claude-code-setup` أولاً (رسمية وآمنة) ← خله يقترح الباقي.
2. `claude-mem` للذاكرة.
3. Strix لفحص دوال الطلبات ولوحة الأدمن قبل أي إطلاق.
4. Agent Reach وقائمة الـ APIs المجانية حسب الحاجة.
