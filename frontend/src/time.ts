/** Times on the website, in the zone of the team , else the browser's zone.
 * Thus the times are the same on each computer that opens the app. */
let zone: string | undefined;

export function setTimeZone(name: string | undefined): void {
  zone = name || undefined;
}

const fmt = (value: string | number | Date, options: Intl.DateTimeFormatOptions) =>
  new Date(value).toLocaleString(undefined, { ...options, timeZone: zone });

/** 10/11/2026, 12:47:37 PM */
export const dateTime = (value: string | number | Date) => fmt(value, {});
/** Oct 11, 12:47 PM */
export const shortDateTime = (value: string | number | Date) => fmt(value, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
/** 12:47 PM */
export const clockTime = (value: string | number | Date) => fmt(value, { hour: "numeric", minute: "2-digit" });
/** Oct 11, 2026 */
export const dayLabel = (value: string | number | Date) => fmt(value, { month: "short", day: "numeric", year: "numeric" });
/** 10/11/2026 */
export const shortDate = (value: string | number | Date) => fmt(value, { year: "numeric", month: "numeric", day: "numeric" });
/** EDT or EST: the short name of the zone now */
export function zoneLabel(): string {
  const part = new Intl.DateTimeFormat("en-US", { timeZone: zone, timeZoneName: "short" }).formatToParts(new Date())
    .find((p) => p.type === "timeZoneName");
  return part?.value ?? "local time";
}
