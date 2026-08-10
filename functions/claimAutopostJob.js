/**
 * claimAutopostJob — قفل idempotency لبوت النشر التلقائي (تلغرام → فيسبوك).
 * يتأكد كل منشور/ألبوم تلغرام ينشر مرة وحدة بس، حتى لو البوت انعاد تشغيله
 * أو انقطع اتصاله وأعاد الاتصال. يستخدم معاملة Firestore (transaction) حتى
 * ما يصير سباق (race) لو نفس المعرف وصل مرتين بنفس اللحظة.
 */

const { onRequest } = require("firebase-functions/v2/https");
const { defineSecret } = require("firebase-functions/params");
const { initializeApp, getApps } = require("firebase-admin/app");
const { getFirestore, FieldValue } = require("firebase-admin/firestore");

if (!getApps().length) initializeApp();
const db = getFirestore();

const AUTOPOST_SECRET = defineSecret("AUTOPOST_BOT_SECRET");

exports.claimAutopostJob = onRequest(
  { region: "europe-west1", secrets: [AUTOPOST_SECRET], cors: true },
  async (req, res) => {
    if (req.method !== "POST") {
      return res.status(405).json({ success: false, error: "method_not_allowed" });
    }
    if (req.body?.secret !== AUTOPOST_SECRET.value()) {
      return res.status(401).json({ success: false, error: "unauthorized" });
    }

    const jobId = String(req.body?.jobId || "").trim();
    if (!jobId) {
      return res.status(400).json({ success: false, error: "missing_job_id" });
    }

    const ref = db.collection("fb_autopost_log").doc(jobId);
    try {
      const claimed = await db.runTransaction(async (tx) => {
        const snap = await tx.get(ref);
        if (snap.exists) return false;
        tx.set(ref, { claimedAt: FieldValue.serverTimestamp(), results: null });
        return true;
      });
      return res.status(200).json({ success: true, claimed });
    } catch (e) {
      return res.status(500).json({ success: false, error: "server_error" });
    }
  }
);

/**
 * reportAutopostResult — يسجل نتيجة النشر (نجح/فشل) لكل صفحة، بعد claimAutopostJob.
 * اختياري بالتنفيذ (البوت يشتغل حتى لو ما ناداها)، بس مفيد للمراجعة لاحقاً.
 */
exports.reportAutopostResult = onRequest(
  { region: "europe-west1", secrets: [AUTOPOST_SECRET], cors: true },
  async (req, res) => {
    if (req.method !== "POST") {
      return res.status(405).json({ success: false, error: "method_not_allowed" });
    }
    if (req.body?.secret !== AUTOPOST_SECRET.value()) {
      return res.status(401).json({ success: false, error: "unauthorized" });
    }

    const jobId = String(req.body?.jobId || "").trim();
    const results = req.body?.results;
    if (!jobId || typeof results !== "object") {
      return res.status(400).json({ success: false, error: "missing_fields" });
    }

    await db.collection("fb_autopost_log").doc(jobId).set(
      { results, reportedAt: FieldValue.serverTimestamp() },
      { merge: true }
    );
    return res.status(200).json({ success: true });
  }
);
