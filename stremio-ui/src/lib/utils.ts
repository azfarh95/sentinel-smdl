import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn-svelte's canonical class-name merger. Combines `clsx` (conditional
 *  classes) with `tailwind-merge` (dedupes conflicting Tailwind utilities,
 *  so `cn("px-4", "px-6")` resolves to "px-6" not "px-4 px-6"). */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Pretty-print a byte count. Returns "2.4 GB", "180 MB", "—" for null. */
export function fmtSize(n: number | null | undefined): string {
  if (!n || n <= 0) return "—";
  let val = n;
  for (const unit of ["B", "KB", "MB", "GB", "TB"]) {
    if (val < 1024) return `${val.toFixed(unit === "B" ? 0 : 1)} ${unit}`;
    val /= 1024;
  }
  return `${val.toFixed(1)} PB`;
}
