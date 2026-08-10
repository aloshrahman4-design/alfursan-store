/**
 * createOrder — يسجل طلب جديد بمجموعة orders ويرجع رمز قصير (4 رموز)
 * تكدر الزبونة تستخدمه بعدين لتتبع حالة طلبها عبر بوت واتساب.
 * فنكشن عامة (بدون secret) لأنها تنادى من أي زائر بالمتجر لحظة الطلب.
 */

const { onRequest } = require("firebase-functions/v2/https");
const { initializeApp, getApps } = require("firebase-admin/app");
const { getFirestore, FieldValue } = require("firebase-admin/firestore");

if (!getApps().length) initializeApp();
const db = getFirestore();

const CODE_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"; // بدون 0/O و1/I لتفادي اللبس
function randomCode() {
  let code = "";
  for (let i = 0; i < 4; i++) code += CODE_CHARS[Math.floor(Math.random() * CODE_CHARS.length)];
  return code;
}

async function uniqueOrderNumber() {
  for (let attempt = 0; attempt < 6; attempt++) {
    const code = randomCode();
    const existing = await db.collection("orders").where("orderNumber", "==", code).limit(1).get();
    if (existing.empty) return code;
  }
  throw new Error("could_not_generate_unique_code");
}

exports.createOrder = onRequest({ region: "europe-west1", cors: true }, async (req, res) => {
  if (req.method !== "POST") {
    return res.status(405).json({ success: false, error: "method_not_allowed" });
  }

  const { page, items, total } = req.body || {};
  if (!Array.isArray(items) || !items.length) {
    return res.status(400).json({ success: false, error: "missing_items" });
  }

  const cleanItems = items
    .map((it) => ({
      name: String(it?.name || "").slice(0, 200),
      qty: Math.max(1, Number(it?.qty) || 1),
    }))
    .filter((it) => it.name);

  if (!cleanItems.length) {
    return res.status(400).json({ success: false, error: "missing_items" });
  }

  try {
    const orderNumber = await uniqueOrderNumber();
    await db.collection("orders").add({
      orderNumber,
      page: String(page || "").slice(0, 50),
      items: cleanItems,
      total: Number(total) || 0,
      status: "تجهيز",
      createdAt: FieldValue.serverTimestamp(),
      updatedAt: FieldValue.serverTimestamp(),
    });
    return res.status(200).json({ success: true, orderNumber });
  } catch (e) {
    return res.status(500).json({ success: false, error: "server_error" });
  }
});
