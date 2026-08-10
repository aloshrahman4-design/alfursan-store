/**
 * updateOrderStatus — تحدّث حالة طلب معيّن (تجهيز / بالشحن / تم التسليم).
 * تحتاج secret اللي يرجعه checkRepPin بعد نجاح رمز المندوبة.
 */

const { onRequest } = require("firebase-functions/v2/https");
const { defineSecret } = require("firebase-functions/params");
const { initializeApp, getApps } = require("firebase-admin/app");
const { getFirestore, FieldValue } = require("firebase-admin/firestore");

if (!getApps().length) initializeApp();
const db = getFirestore();

const ORDER_STATUS_SECRET = defineSecret("ORDER_STATUS_SECRET");
const VALID_STATUSES = ["تجهيز", "بالشحن", "تم التسليم"];

exports.updateOrderStatus = onRequest(
  { region: "europe-west1", secrets: [ORDER_STATUS_SECRET], cors: true },
  async (req, res) => {
    if (req.method !== "POST") {
      return res.status(405).json({ success: false, error: "method_not_allowed" });
    }

    const { secret, orderNumber, status } = req.body || {};
    if (secret !== ORDER_STATUS_SECRET.value()) {
      return res.status(401).json({ success: false, error: "unauthorized" });
    }
    if (!VALID_STATUSES.includes(status)) {
      return res.status(400).json({ success: false, error: "invalid_status" });
    }

    const code = String(orderNumber || "").trim().toUpperCase();
    if (!code) {
      return res.status(400).json({ success: false, error: "missing_order_number" });
    }

    const snap = await db.collection("orders").where("orderNumber", "==", code).limit(1).get();
    if (snap.empty) {
      return res.status(404).json({ success: false, error: "not_found" });
    }

    await snap.docs[0].ref.update({ status, updatedAt: FieldValue.serverTimestamp() });
    return res.status(200).json({ success: true });
  }
);
