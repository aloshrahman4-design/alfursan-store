/**
 * getOrdersSummary — استعلام قراءة فقط (لا تعديل) يستخدمه المساعد الرئيسي
 * عبر n8n/Make بدل اختراع أرقام مبيعات. يرجّع عدد الطلبات حسب الحالة،
 * وآخر N طلب (رقم الطلب/الحالة/المجموع/الوقت فقط، بدون أي بيانات زبون
 * حساسة). محمي بسر منفصل (ASSISTANT_QUERY_SECRET) عن أسرار المندوبة/
 * الإدارة، لأن هذا وصول قراءة بحتة ولا يجوز أن يشارك صلاحية التعديل.
 */

const { onRequest } = require("firebase-functions/v2/https");
const { defineSecret } = require("firebase-functions/params");
const { initializeApp, getApps } = require("firebase-admin/app");
const { getFirestore } = require("firebase-admin/firestore");

if (!getApps().length) initializeApp();
const db = getFirestore();

const ASSISTANT_QUERY_SECRET = defineSecret("ASSISTANT_QUERY_SECRET");
const STATUSES = ["تجهيز", "بالشحن", "تم التسليم"];
const MAX_RECENT = 50;
const DEFAULT_RECENT = 10;

exports.getOrdersSummary = onRequest(
  { region: "europe-west1", secrets: [ASSISTANT_QUERY_SECRET], cors: true },
  async (req, res) => {
    if (req.method !== "POST") {
      return res.status(405).json({ success: false, error: "method_not_allowed" });
    }
    if (req.body?.secret !== ASSISTANT_QUERY_SECRET.value()) {
      return res.status(401).json({ success: false, error: "unauthorized" });
    }

    const limit = Math.min(MAX_RECENT, Math.max(1, Number(req.body?.limit) || DEFAULT_RECENT));

    try {
      const countsByStatus = {};
      await Promise.all(
        STATUSES.map(async (status) => {
          const snap = await db.collection("orders").where("status", "==", status).count().get();
          countsByStatus[status] = snap.data().count;
        })
      );

      const recentSnap = await db
        .collection("orders")
        .orderBy("createdAt", "desc")
        .limit(limit)
        .get();

      const recentOrders = recentSnap.docs.map((doc) => {
        const d = doc.data();
        return {
          orderNumber: d.orderNumber,
          status: d.status,
          total: d.total,
          page: d.page || null,
          createdAt: d.createdAt ? d.createdAt.toDate().toISOString() : null,
        };
      });

      const totalOrders = Object.values(countsByStatus).reduce((a, b) => a + b, 0);

      return res.status(200).json({
        success: true,
        totalOrders,
        countsByStatus,
        recentOrders,
      });
    } catch (e) {
      return res.status(500).json({ success: false, error: "server_error" });
    }
  }
);
