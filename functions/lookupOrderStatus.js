/**
 * lookupOrderStatus — يرجع حالة الطلب برمزه القصير (4 رموز) حتى الزبونة
 * تتابع طلبها. عامة (بدون secret) مثل createOrder، فترجع الحالة بس —
 * بدون المنتجات أو المجموع — حتى ما تنكشف تفاصيل طلبات الناس لو أحد خمّن رمز.
 * تقبل GET ?orderNumber=XXXX أو POST {orderNumber}.
 */

const { onRequest } = require("firebase-functions/v2/https");
const { initializeApp, getApps } = require("firebase-admin/app");
const { getFirestore } = require("firebase-admin/firestore");

if (!getApps().length) initializeApp();
const db = getFirestore();

const CODE_RE = /^[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{4}$/;

exports.lookupOrderStatus = onRequest({ region: "europe-west1", cors: true }, async (req, res) => {
  if (req.method !== "GET" && req.method !== "POST") {
    return res.status(405).json({ success: false, error: "method_not_allowed" });
  }

  const raw = req.method === "GET" ? req.query.orderNumber : req.body?.orderNumber;
  const code = String(raw || "").trim().toUpperCase();
  if (!CODE_RE.test(code)) {
    return res.status(400).json({ success: false, error: "invalid_order_number" });
  }

  try {
    const snap = await db.collection("orders").where("orderNumber", "==", code).limit(1).get();
    if (snap.empty) {
      return res.status(404).json({ success: false, error: "not_found" });
    }
    const order = snap.docs[0].data();
    return res.status(200).json({
      success: true,
      orderNumber: code,
      status: order.status,
      updatedAt: order.updatedAt?.toMillis?.() || null,
    });
  } catch (e) {
    return res.status(500).json({ success: false, error: "server_error" });
  }
});
