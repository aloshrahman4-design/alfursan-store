/**
 * safeEqual — مقارنة أسرار بوقت ثابت حتى ما يگدر أحد يخمّن السر حرف حرف
 * من فرق زمن الاستجابة (timing attack).
 */
const crypto = require("crypto");

exports.safeEqual = (a, b) => {
  const ha = crypto.createHash("sha256").update(String(a ?? "")).digest();
  const hb = crypto.createHash("sha256").update(String(b ?? "")).digest();
  return crypto.timingSafeEqual(ha, hb) && String(a ?? "") !== "";
};
