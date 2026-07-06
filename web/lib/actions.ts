"use server";

import { revalidatePath } from "next/cache";
import { confirmEntityDb } from "./data";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// P1-6: action này ghi field HUMAN-OWNED (display_name, resolution_status) —
// thứ duy nhất replay không dựng lại được. Validate chặt, không bao giờ để
// lỗi Postgres thô văng qua RSC boundary thành trang 500.
export async function confirmEntity(formData: FormData) {
  const id = String(formData.get("id") ?? "");
  if (!UUID_RE.test(id)) return; // id thiếu/hỏng ('null') → no-op, không phải 500
  const action = String(formData.get("action")) === "reject" ? "reject" : "confirm";
  const raw = formData.get("display_name");
  const displayName = raw ? String(raw).trim().slice(0, 200) : undefined;
  try {
    await confirmEntityDb(id, action, displayName || undefined);
  } catch (e) {
    console.error("[actions] confirmEntity failed:", e instanceof Error ? e.message : String(e));
    return;
  }
  revalidatePath("/entities");
}
