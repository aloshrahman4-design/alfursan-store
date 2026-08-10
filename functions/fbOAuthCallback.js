/**
 * fbOAuthCallback — بديل أبسط لتوليد Page Access Tokens يدوياً عبر
 * Graph API Explorer. المسار:
 *   1. تفتح رابط تسجيل الدخول (بيه client_id + الصلاحيات المطلوبة).
 *   2. توافقين مرة وحدة بمتصفح فيسبوك.
 *   3. فيسبوك يرجعك لهذا الرابط مع "code".
 *   4. الفنكشن تبادل الكود بتوكن مستخدم قصير الأمد، وبعدين توكن طويل
 *      الأمد (60 يوم)، وبعدين تجيب توكنات الصفحات الأربع منه —
 *      توكنات الصفحات المتولدة هيچي ما تنتهي.
 *   5. تعرض التوكنات الأربعة جاهزة تنسخينها لمتغيرات بيئة Render.
 *
 * التوكنات ما تنخزن بأي مكان — تظهر مرة وحدة بالمتصفح بس.
 */

const { onRequest } = require("firebase-functions/v2/https");
const { defineSecret } = require("firebase-functions/params");

const FB_APP_ID = defineSecret("FB_APP_ID");
const FB_APP_SECRET = defineSecret("FB_APP_SECRET");

const GRAPH_VERSION = "v19.0";
const TARGET_PAGES = {
  "112226891773568": "كناري",
  "607377785783244": "الشيخة",
  "277922022078817": "الأميرات",
  "1174723282380393": "دجلة",
};

const REDIRECT_URI = "https://europe-west1-alfursan-market.cloudfunctions.net/fbOAuthCallback";

function htmlPage(body) {
  return `<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">
  <title>توكنات صفحات فيسبوك</title>
  <style>
    body{font-family:system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 20px;background:#FAF3F5;color:#2B1B22}
    h1{font-size:1.3rem}
    .token-box{background:#fff;border:1.5px solid #E7D5DC;border-radius:12px;padding:14px;margin:12px 0}
    .token-box b{color:#5C1A33}
    textarea{width:100%;margin-top:8px;padding:8px;border-radius:8px;border:1px solid #ddd;font-family:monospace;font-size:.8rem}
    .err{background:#FBEAEA;color:#B3261E;padding:14px;border-radius:12px}
  </style></head><body>${body}</body></html>`;
}

exports.startFbLogin = onRequest({ region: "europe-west1", secrets: [FB_APP_ID] }, async (req, res) => {
  const redirectUri = REDIRECT_URI;
  const scope = "pages_show_list,pages_read_engagement,pages_manage_posts";
  const url =
    `https://www.facebook.com/${GRAPH_VERSION}/dialog/oauth` +
    `?client_id=${encodeURIComponent(FB_APP_ID.value())}` +
    `&redirect_uri=${encodeURIComponent(redirectUri)}` +
    `&scope=${encodeURIComponent(scope)}` +
    `&response_type=code`;
  res.redirect(url);
});

exports.fbOAuthCallback = onRequest(
  { region: "europe-west1", secrets: [FB_APP_ID, FB_APP_SECRET] },
  async (req, res) => {
    const code = req.query.code;
    const oauthError = req.query.error_description || req.query.error;
    if (oauthError) {
      return res.status(400).send(htmlPage(`<div class="err">رفضتِ الطلب أو صار خطأ: ${oauthError}</div>`));
    }
    if (!code) {
      return res.status(400).send(htmlPage(`<div class="err">ماكو code بالرابط — افتحي رابط تسجيل الدخول من جديد.</div>`));
    }

    const redirectUri = REDIRECT_URI;

    try {
      // ١) code → توكن مستخدم قصير الأمد
      const shortRes = await fetch(
        `https://graph.facebook.com/${GRAPH_VERSION}/oauth/access_token` +
          `?client_id=${FB_APP_ID.value()}&client_secret=${FB_APP_SECRET.value()}` +
          `&redirect_uri=${encodeURIComponent(redirectUri)}&code=${code}`
      );
      const shortData = await shortRes.json();
      if (!shortRes.ok || !shortData.access_token) {
        throw new Error(shortData?.error?.message || "فشل الحصول على توكن قصير الأمد");
      }

      // ٢) توكن قصير → توكن مستخدم طويل الأمد (٦٠ يوم)
      const longRes = await fetch(
        `https://graph.facebook.com/${GRAPH_VERSION}/oauth/access_token` +
          `?grant_type=fb_exchange_token&client_id=${FB_APP_ID.value()}&client_secret=${FB_APP_SECRET.value()}` +
          `&fb_exchange_token=${shortData.access_token}`
      );
      const longData = await longRes.json();
      if (!longRes.ok || !longData.access_token) {
        throw new Error(longData?.error?.message || "فشل تحويل التوكن لطويل الأمد");
      }

      // ٣) توكنات الصفحات (دائمة، ما تنتهي، طالما متولدة من توكن طويل الأمد)
      const pagesRes = await fetch(
        `https://graph.facebook.com/${GRAPH_VERSION}/me/accounts?fields=id,name,access_token&access_token=${longData.access_token}`
      );
      const pagesData = await pagesRes.json();
      if (!pagesRes.ok || !pagesData.data) {
        throw new Error(pagesData?.error?.message || "فشل جلب صفحاتك");
      }

      const found = {};
      for (const p of pagesData.data) {
        if (TARGET_PAGES[p.id]) found[p.id] = { name: TARGET_PAGES[p.id], token: p.access_token };
      }

      const missing = Object.entries(TARGET_PAGES).filter(([id]) => !found[id]);
      let body = `<h1>توكنات الصفحات ✅</h1><p>انسخي كل توكن لمتغير البيئة المطابق بـ Render، وسكري هذي الصفحة بعدها.</p>`;

      for (const [id, info] of Object.entries(found)) {
        const envKey = { "112226891773568": "FB_TOKEN_KANARI", "607377785783244": "FB_TOKEN_ALSHEKHA", "277922022078817": "FB_TOKEN_ALAMEERAT", "1174723282380393": "FB_TOKEN_DIJLA" }[id];
        body += `<div class="token-box"><b>${info.name}</b> — <code>${envKey}</code><textarea rows="2" readonly onclick="this.select()">${info.token}</textarea></div>`;
      }
      if (missing.length) {
        body += `<div class="err">ماكو توكن لهاي الصفحات: ${missing.map(([, n]) => n).join("، ")} — تأكدي انتي أدمن بيهم بفيسبوك، وإنك وافقتِ على كل الصلاحيات بصفحة تسجيل الدخول.</div>`;
      }

      return res.status(200).send(htmlPage(body));
    } catch (err) {
      return res.status(500).send(htmlPage(`<div class="err">صار خطأ: ${err.message}</div>`));
    }
  }
);
