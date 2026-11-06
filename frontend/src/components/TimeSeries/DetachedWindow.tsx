import { useEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

interface Props {
  title: string;
  width?: number;
  height?: number;
  onClosed: () => void;
  children: ReactNode;
}

const WINDOW_NAME = "insar-timeseries";
const OWNED = "data-insar-owned";
// Closing is deferred a moment so React StrictMode's mount, unmount, mount in
// development reuses the same window instead of closing and reopening it.
let closeTimer: ReturnType<typeof setTimeout> | undefined;

/**
 * Renders its children in a separate browser window (so it can be moved to
 * another screen). The page's stylesheets are copied into the new window, and
 * the window closes with the page. If the user closes it, onClosed fires so the
 * caller can fall back to the in-page panel.
 */
export function DetachedWindow({ title, width = 760, height = 720, onClosed, children }: Props) {
  const [container, setContainer] = useState<HTMLElement | null>(null);

  useEffect(() => {
    clearTimeout(closeTimer);
    const win = window.open("", WINDOW_NAME, `popup=yes,width=${width},height=${height}`);
    if (!win) {
      onClosed(); // blocked by the browser
      return;
    }
    let disposed = false;

    // Start from a clean document in case the window is being reused.
    win.document.querySelectorAll(`[${OWNED}]`).forEach((n) => n.remove());
    win.document.title = title;
    document.querySelectorAll('link[rel="stylesheet"], style').forEach((node) => {
      const copy = node.cloneNode(true) as HTMLElement;
      copy.setAttribute(OWNED, "");
      win.document.head.appendChild(copy);
    });
    win.document.body.style.margin = "0";
    const host = win.document.createElement("div");
    host.setAttribute(OWNED, "");
    win.document.body.appendChild(host);
    setContainer(host);

    const onPageHide = () => { if (!disposed) onClosed(); };
    const closeWithPage = () => win.close();
    win.addEventListener("pagehide", onPageHide);
    window.addEventListener("beforeunload", closeWithPage);

    return () => {
      disposed = true;
      win.removeEventListener("pagehide", onPageHide);
      window.removeEventListener("beforeunload", closeWithPage);
      setContainer(null);
      closeTimer = setTimeout(() => { if (!win.closed) win.close(); }, 150);
    };
    // The window is opened once per mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return container ? createPortal(children, container) : null;
}
