import { useEffect, useState, type RefObject } from "react";

/**
 * Tracks an element's size. The ResizeObserver comes from the element's own
 * window rather than the global one, so it keeps working when the element is
 * rendered into a detached browser window.
 */
export function useElementSize(ref: RefObject<HTMLElement | null>): { width: number; height: number } {
  const [size, setSize] = useState({ width: 0, height: 0 });
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const win = (el.ownerDocument.defaultView ?? window) as Window & typeof globalThis;
    const update = () => setSize({ width: el.clientWidth, height: el.clientHeight });
    update();
    const observer = new win.ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);
  return size;
}
