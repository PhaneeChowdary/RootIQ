import {
  Activity,
  AlertTriangle,
  ArrowRight,
  BarChart3,
  BookOpen,
  Bot,
  Building2,
  Check,
  CheckCircle2,
  ClipboardCheck,
  CircleStop,
  Clock,
  Cpu,
  Download,
  ExternalLink,
  Eye,
  EyeOff,
  FileText,
  Gauge,
  History,
  Inbox,
  Info,
  KeyRound,
  Layers,
  LoaderCircle,
  LayoutDashboard,
  LogOut,
  type LucideIcon,
  MessageSquare,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  RotateCw,
  Save,
  Search,
  Send,
  Server,
  Settings,
  Code2,
  Columns2,
  Copy,
  FileDiff,
  Rows2,
  Power,
  LockOpen,
  Fingerprint,
  Archive,
  ShieldCheck,
  LockKeyhole,
  Timer,
  Users,
  UserPlus,
  Database,
  GitCommitHorizontal,
  Link2,
  Unlink2,
  Flag,
  Sparkles,
  Trash2,
  User,
  XCircle,
  X,
  Zap,
} from "lucide-react";
import { useId } from "react";

const ICONS = {
  success: CheckCircle2,
  error: AlertTriangle,
  save: Save,
  logout: LogOut,
  download: Download,
  reset: RotateCcw,
  search: Search,
  automation: RefreshCw,
  dashboard: LayoutDashboard,
  insights: BarChart3,
  about: Info,
  user: User,
  trash: Trash2,
  plus: Plus,
  spark: Sparkles,
  bot: Bot,
  play: Play,
  arrow: ArrowRight,
  check: Check,
  clock: Clock,
  inbox: Inbox,
  activity: Activity,
  zap: Zap,
  send: Send,
  file: FileText,
  layers: Layers,
  book: BookOpen,
  report: ClipboardCheck,
  message: MessageSquare,
  building: Building2,
  server: Server,
  gauge: Gauge,
  history: History,
  close: X,
  cpu: Cpu,
  key: KeyRound,
  eye: Eye,
  eyeOff: EyeOff,
  external: ExternalLink,
  loader: LoaderCircle,
  fail: XCircle,
  stop: CircleStop,
  restart: RotateCw,
  settings: Settings,
  code: Code2,
  diff: FileDiff,
  copy: Copy,
  split: Columns2,
  unified: Rows2,
  power: Power,
  unlock: LockOpen,
  fingerprint: Fingerprint,
  archive: Archive,
  shield: ShieldCheck,
  lock: LockKeyhole,
  mask: EyeOff,
  timer: Timer,
  users: Users,
  userPlus: UserPlus,
  database: Database,
  commit: GitCommitHorizontal,
  link: Link2,
  unlink: Unlink2,
  flag: Flag,
} satisfies Record<string, LucideIcon>;

export type IconKind = keyof typeof ICONS;

/** One icon set for the whole app , drawn at a consistent weight. */
export function ActionIcon({ kind, size = 16 }: { kind: IconKind; size?: number }) {
  const Icon = ICONS[kind];
  return <Icon aria-hidden="true" className={`action-icon icon-${kind}`} size={size} strokeWidth={1.6} />;
}

/** One beat of the logo signal, 24 units wide. It starts and ends on the base line (y=17), so beats join up. */
const BEAT = [[0, 17], [3, 17], [5.5, 11], [8.5, 22.5], [11.1, 15.5], [13.3, 19], [18.6, 12.2], [21, 17], [24, 17]];
const WAVE = [-24, 0, 24, 48].flatMap((dx) => BEAT.map(([x, y]) => `${x + dx},${y}`)).join(" ");

/** RootIQ mark: a heartbeat line on a dark tile, grey on the left and green on the right.
 *  - Default: the favicon file itself, so the browser tab and the mark always match.
 *  - `live`: the line moves forward slowly and without end, like a monitor. The colours stay in place:
 *    a fixed gradient shows through the moving line, so the left part is always grey-white and the right part green.
 *    The line fades in and out at the tile edges. With "Reduce motion" on, the line stays still. */
export function BrandMark({ size = 28, live = false }: { size?: number; live?: boolean }) {
  const id = useId().replace(/:/g, "");
  if (!live) return <img alt="" aria-hidden="true" className="brand-svg" width={size} height={size} src="/favicon.svg?v=2" />;
  const still = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  // The line moves left by one beat and starts again: it looks endless. Its colour gradient moves right by. 24 units.
  // the same amount at the same time, so the grey-white and the green stay in the same place on the tile.
  const motion = (to: string, attribute = "transform") =>
    still ? null : <animateTransform attributeName={attribute} type="translate" from="0 0" to={to} dur="7s" repeatCount="indefinite" />;
  return (
    <svg aria-hidden="true" className="brand-svg brand-live" width={size} height={size} viewBox="0 0 32 32">
      <defs>
        <linearGradient id={`${id}-tile`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#2b3844" /><stop offset="1" stopColor="#121a22" /></linearGradient>
        <linearGradient id={`${id}-ink`} x1="4" y1="0" x2="28" y2="0" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#9aa6b2" /><stop offset="0.5" stopColor="#e8edf2" /><stop offset="1" stopColor="#30d158" />
          {motion("24 0", "gradientTransform")}
        </linearGradient>
        <linearGradient id={`${id}-edge`} x1="3" y1="0" x2="29" y2="0" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#000" /><stop offset="0.16" stopColor="#fff" /><stop offset="0.84" stopColor="#fff" /><stop offset="1" stopColor="#000" />
        </linearGradient>
        <mask id={`${id}-fade`} maskUnits="userSpaceOnUse" x="0" y="0" width="32" height="32">
          <rect x="0" y="0" width="32" height="32" fill={`url(#${id}-edge)`} />
        </mask>
      </defs>
      <rect x="1" y="1" width="30" height="30" rx="8" fill={`url(#${id}-tile)`} />
      <rect x="1.5" y="1.5" width="29" height="29" rx="7.5" fill="none" stroke="#ffffff" strokeOpacity="0.12" />
      <g mask={`url(#${id}-fade)`}>
        <polyline points={WAVE} fill="none" stroke={`url(#${id}-ink)`} strokeWidth="2.3" strokeLinecap="round" strokeLinejoin="round">
          {motion("-24 0")}
        </polyline>
      </g>
    </svg>
  );
}
