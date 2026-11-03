import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";

const reducedMotion = () => typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/** Shows a number that counts up from its last value to the new value. */
export function CountUp({ value, duration = 700 }: { value: number; duration?: number }) {
  const [shown, setShown] = useState(reducedMotion() ? value : 0);
  const from = useRef(shown);
  useEffect(() => {
    if (reducedMotion()) { setShown(value); return; }
    const start = performance.now();
    const begin = from.current;
    let frame = 0;
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      const next = Math.round(begin + (value - begin) * eased);
      from.current = next;
      setShown(next);
      if (t < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [value, duration]);
  return <>{shown}</>;
}

/** Moves a highlight pill under the active item of a bar. The bar gets CSS variables; CSS draws the pill. */
/** Sets the class "more" on a scroll area while content is below the visible part . */
export function useScrollMore<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => el.classList.toggle("more", el.scrollHeight - el.scrollTop - el.clientHeight > 4);
    update();
    el.addEventListener("scroll", update, { passive: true });
    const sizes = new ResizeObserver(update);  // the area and its content: either can change the scroll height
    sizes.observe(el);
    for (const child of Array.from(el.children)) sizes.observe(child);
    void document.fonts?.ready.then(update);
    const timers = [requestAnimationFrame(() => requestAnimationFrame(update)), window.setTimeout(update, 500), window.setTimeout(update, 1500)];
    return () => {
      el.removeEventListener("scroll", update);
      sizes.disconnect();
      cancelAnimationFrame(timers[0]);
      timers.slice(1).forEach((t) => window.clearTimeout(t));
    };
  });
  return ref;
}

export function useSlidingIndicator(ref: RefObject<HTMLElement | null>, activeSelector: string, deps: unknown[]) {
  useLayoutEffect(() => {
    const bar = ref.current;
    if (!bar) return;
    const place = () => {
      const active = bar.querySelector<HTMLElement>(activeSelector);
      if (!active) { bar.style.setProperty("--ind-o", "0"); return; }
      bar.style.setProperty("--ind-x", `${active.offsetLeft}px`);
      bar.style.setProperty("--ind-y", `${active.offsetTop}px`);
      bar.style.setProperty("--ind-w", `${active.offsetWidth}px`);
      bar.style.setProperty("--ind-h", `${active.offsetHeight}px`);
      bar.style.setProperty("--ind-o", "1");
    };
    place();
    const observer = new ResizeObserver(place);
    observer.observe(bar);
    return () => observer.disconnect();
  }, deps);
}
