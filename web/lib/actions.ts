"use server";

import { revalidatePath } from "next/cache";
import { confirmEntityDb } from "./data";

export async function confirmEntity(formData: FormData) {
  const id = String(formData.get("id"));
  const action = String(formData.get("action")) === "reject" ? "reject" : "confirm";
  const displayName = formData.get("display_name");
  await confirmEntityDb(id, action, displayName ? String(displayName) : undefined);
  revalidatePath("/entities");
}
