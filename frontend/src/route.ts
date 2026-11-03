/** The page and the tab live in the address, for example #/settings/models or #/dashboard/agent. A reload, a
 * bookmark or the Back button then opens the same page; the selected case is saved for the user on the server. */
export type Route = { page: string; tab: string };

export function readRoute(): Route {
  const [page = "", tab = ""] = window.location.hash.replace(/^#\/?/, "").split("/");
  return { page, tab };
}

/** A new page adds a history entry, so Back works; a tab change replaces it. */
export function writeRoute(page: string, tab = "", replace = false): void {
  const hash = `#/${page}${tab ? `/${tab}` : ""}`;
  if (window.location.hash === hash) return;
  if (replace) window.history.replaceState(null, "", hash);
  else window.history.pushState(null, "", hash);
}
