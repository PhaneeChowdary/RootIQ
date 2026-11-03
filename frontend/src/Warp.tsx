import { useEffect, useRef } from "react";

/** A quiet starfield for a case row while agents work on it. The stars stay in place, as dots over the full row.
 * Each star twinkles on its own random timer, so a few sparkle at a time and never all together; the brightest get a
 * small four-point glint at the top of a twinkle. A layer of faint dust breathes slowly behind. One canvas per working
 * row; it pauses off screen and in a hidden tab, and draws still stars for people who ask for reduced motion. */

type Star = { x: number; y: number; r: number; base: number; peak: number; tint: string;
              next: number; length: number; glint: boolean };
type Dust = { x: number; y: number; r: number; a: number; phase: number; period: number };

const TINTS = ["214,232,255", "214,232,255", "196,218,255", "255,238,218", "186,242,255"];
const STARS_PER_100PX = 7;

const between = (low: number, high: number) => low + Math.random() * (high - low);

function newStar(width: number, height: number, now: number): Star {
  const bright = Math.random() ** 2;
  return { x: between(4, width - 4), y: between(3, height - 3), r: 0.45 + bright * 0.85, base: between(0.12, 0.3),
           peak: 0.55 + bright * 0.45, tint: TINTS[Math.floor(Math.random() * TINTS.length)],
           next: now + between(0, 6000), length: between(900, 2000), glint: bright > 0.55 };
}

export function Warp() {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let width = 0, height = 0, frame = 0, visible = true;
    let stars: Star[] = [];
    let dust: Dust[] = [];

    const resize = () => {
      const box = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = box.width; height = box.height;
      canvas.width = Math.max(1, Math.round(width * dpr));
      canvas.height = Math.max(1, Math.round(height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const now = performance.now();
      stars = Array.from({ length: Math.max(8, Math.round((width / 100) * STARS_PER_100PX)) }, () => newStar(width, height, now));
      dust = Array.from({ length: Math.round(width / 7) }, () => ({ x: Math.random() * width, y: Math.random() * height,
        r: between(0.2, 0.6), a: between(0.06, 0.22), phase: Math.random() * Math.PI * 2, period: between(3000, 7000) }));
    };

    // 0 between twinkles; a smooth rise and fall from 0 to 1 and back during one
    const twinkle = (s: Star, now: number) => {
      if (now < s.next) return 0;
      const p = (now - s.next) / s.length;
      if (p >= 1) {  // the twinkle is over: wait a random time for the next one
        s.next = now + between(2500, 9000);
        s.length = between(900, 2000);
        return 0;
      }
      return Math.sin(Math.PI * p) ** 2;
    };

    const draw = (now: number) => {
      ctx.clearRect(0, 0, width, height);
      for (const d of dust) {
        const breathe = reduced ? 0.8 : 0.6 + 0.4 * Math.sin(now / d.period * Math.PI * 2 + d.phase);
        ctx.fillStyle = `rgba(200,220,255,${d.a * breathe})`;
        ctx.beginPath(); ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2); ctx.fill();
      }
      for (const s of stars) {
        const t = reduced ? 0 : twinkle(s, now);
        const alpha = s.base + (s.peak - s.base) * t;
        const r = s.r * (1 + 0.35 * t);
        if (t > 0.05) {  // a soft glow while it twinkles
          const glow = ctx.createRadialGradient(s.x, s.y, 0, s.x, s.y, r * 6);
          glow.addColorStop(0, `rgba(${s.tint},${0.35 * t * s.peak})`);
          glow.addColorStop(1, `rgba(${s.tint},0)`);
          ctx.fillStyle = glow;
          ctx.beginPath(); ctx.arc(s.x, s.y, r * 6, 0, Math.PI * 2); ctx.fill();
        }
        if (s.glint && t > 0.35) {  // a thin four-point glint at the top of the twinkle
          const arm = r * (3 + 5 * t), a = (t - 0.35) / 0.65 * 0.7 * s.peak;
          for (const [dx, dy] of [[arm, 0], [0, arm * 0.8]]) {
            const g = ctx.createLinearGradient(s.x - dx, s.y - dy, s.x + dx, s.y + dy);
            g.addColorStop(0, `rgba(${s.tint},0)`);
            g.addColorStop(0.5, `rgba(${s.tint},${a})`);
            g.addColorStop(1, `rgba(${s.tint},0)`);
            ctx.strokeStyle = g;
            ctx.lineWidth = 0.6;
            ctx.beginPath(); ctx.moveTo(s.x - dx, s.y - dy); ctx.lineTo(s.x + dx, s.y + dy); ctx.stroke();
          }
        }
        ctx.fillStyle = `rgba(${s.tint},${alpha})`;
        ctx.beginPath(); ctx.arc(s.x, s.y, r, 0, Math.PI * 2); ctx.fill();
      }
      if (!reduced && visible && !document.hidden) frame = requestAnimationFrame(draw);
    };

    const start = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(draw); };
    resize();
    if (reduced) { draw(performance.now()); return; }
    start();
    const sizes = new ResizeObserver(resize);
    sizes.observe(canvas);
    const seen = new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; if (visible) start(); });
    seen.observe(canvas);
    const onVisibility = () => { if (!document.hidden && visible) start(); };
    document.addEventListener("visibilitychange", onVisibility);
    return () => { cancelAnimationFrame(frame); sizes.disconnect(); seen.disconnect(); document.removeEventListener("visibilitychange", onVisibility); };
  }, []);

  return <canvas aria-hidden="true" className="warp" ref={ref} />;
}
