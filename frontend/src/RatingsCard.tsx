import { useEffect, useState } from "react";
import { getRatingSummary, type RatingStats } from "./api";
import { ActionIcon } from "./icons";
import { Sparkline } from "./live";
import { CountUp } from "./motion";

/** Insights: how engineers rated the agent team's diagnoses in the last 30 days. */
export function RatingsCard() {
  const [stats, setStats] = useState<RatingStats | null>(null);
  useEffect(() => { getRatingSummary(30).then(setStats).catch(() => setStats(null)); }, []);
  if (!stats) return null;
  const total = stats.count;
  const share = (n: number) => (total ? `${(n / total) * 100}%` : "0%");
  return (
    <section className="card insight-card ratings-card">
      <h3 className="title-icon"><ActionIcon kind="success" size={18} />Diagnosis ratings<small className="muted"> · 30 days</small></h3>
      {total ? (
        <div className="ratings-body">
          <div className="ratings-score">
            <strong><CountUp value={Math.round((stats.score ?? 0) * 100)} />%</strong>
            <span>{total} rating{total === 1 ? "" : "s"}</span>
          </div>
          <div className="ratings-split">
            <div className="ratings-bar">
              <i className="correct" style={{ width: share(stats.counts.correct) }} />
              <i className="partly" style={{ width: share(stats.counts.partly_correct) }} />
              <i className="wrong" style={{ width: share(stats.counts.wrong) }} />
            </div>
            <div className="role-legend">
              <span><i className="correct" />Right<b>{stats.counts.correct}</b></span>
              <span><i className="partly" />Partly<b>{stats.counts.partly_correct}</b></span>
              <span><i className="wrong" />Wrong<b>{stats.counts.wrong}</b></span>
            </div>
          </div>
          {stats.weeks.length > 1 ? <div className="ratings-trend"><Sparkline values={stats.weeks.map((w) => w.score * 100)} tone="green" /></div> : null}
        </div>
      ) : <p className="muted">No ratings yet.</p>}
    </section>
  );
}
