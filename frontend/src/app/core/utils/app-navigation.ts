const OPEN_SESSION_QUERY_PARAM = 'openSession';

/** Reserve a browser tab while still inside the user's click event. */
export function reserveNewTab(): Window | null {
  const tab = window.open('about:blank', '_blank');
  if (tab) {
    tab.opener = null;
    tab.document.title = 'CARIBOU — creating fork…';
  }
  return tab;
}

/**
 * Load the app root, then let Angular perform the deep navigation. Loading a
 * session route directly would lose a reverse-proxy path prefix when the app is
 * hosted through Open OnDemand.
 */
export function navigateTabToSession(tab: Window | null, sessionId: string): boolean {
  if (!tab || tab.closed) return false;
  const url = new URL(document.baseURI);
  url.searchParams.set(OPEN_SESSION_QUERY_PARAM, sessionId);
  tab.location.replace(url.toString());
  return true;
}

export function requestedSessionId(): string | null {
  return new URL(window.location.href).searchParams.get(OPEN_SESSION_QUERY_PARAM);
}
