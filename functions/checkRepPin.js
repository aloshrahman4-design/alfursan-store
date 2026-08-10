/**
 * checkRepPin — يتحقق من رمز دخول المندوبة لصفحة تحديث حالة الطلبات.
 * نفس منطق checkAdminPin بالجملة بالضبط (سر بـ Secret Manager + قفل مؤقت
 * بعد محاولات فاشلة)، بس برمز وسر منفصلين تماماً — هذا وصول محدود
 * (تحديث حالة طلب بس)، مو نفس صلاحية إدارة المنتجات.
 */

const { onRequest } = require("firebase-functions/v2/https");
const { defineSecret } = require("firebase-functions/params");
const { initializeApp, getApps } = require("firebase-admin/app");
const { getFirestore, FieldValue } = require("firebase-admin/firestore");

if (!getApps().length) initializeApp();
const db = getFirestore();

const REP_PIN = defineSecret("STORE_REP_PIN");
const ORDER_STATUS_SECRET = defineSecret("ORDER_STATUS_SECRET");

const MAX_ATTEMPTS = 5;
const LOCK_MS = 5 * 60 * 1000;

exports.checkRepPin = onRequest(
  { region: "europe-west1", secrets: [REP_PIN, ORDER_STATUS_SECRET], cors: true },
  async (req, res) => {
    if (req.method !== "POST") {
      return res.status(405).json({ success: false, error: "method_not_allowed" });
    }

    const pin = String(req.body?.pin || "").trim();
    const ip = (req.headers["x-forwarded-for"] || "").split(",")[0].trim() || req.ip || "unknown";
    const attemptRef = db.collection("rep_login_attempts").doc(ip);

    const snap = await attemptRef.get();
    const data = snap.exists ? snap.data() : { count: 0, lockedUntil: 0 };
    const now = Date.now();

    if (data.lockedUntil && now < data.lockedUntil) {
      return res.status(429).json({
        success: false,
        error: "locked",
        retryAfter: Math.ceil((data.lockedUntil - now) / 1000),
      });
    }

    if (!pin || pin !== REP_PIN.value()) {
      const prevCount = data.lockedUntil && now >= data.lockedUntil ? 0 : (data.count || 0);
      const count = prevCount + 1;
      const locked = count >= MAX_ATTEMPTS;
      await attemptRef.set({
        count: locked ? 0 : count,
        lockedUntil: locked ? now + LOCK_MS : 0,
        updatedAt: FieldValue.serverTimestamp(),
      });
      if (locked) {
        return res.status(429).json({ success: false, error: "locked", retryAfter: LOCK_MS / 1000 });
      }
      return res.status(401).json({
        success: false,
        error: "wrong_pin",
        attemptsLeft: MAX_ATTEMPTS - count,
      });
    }

    await attemptRef.delete().catch(() => {});
    return res.status(200).json({ success: true, secret: ORDER_STATUS_SECRET.value() });
  }
);
