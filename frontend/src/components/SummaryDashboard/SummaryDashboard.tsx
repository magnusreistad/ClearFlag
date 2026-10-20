import { useEffect, useState } from 'react'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis, type TooltipContentProps } from 'recharts'
import { fetchSummary } from '../../api/summary'
import { formatRatio, formatWeek } from '../../lib/format'
import type { DispositionCounts, SummaryResponse, TrendBucket, UnflaggedDispositionCounts } from '../../types/summary'
import styles from './SummaryDashboard.module.css'

type Status = 'loading' | 'error' | 'ready'

type Series = keyof DispositionCounts

// Stack order, bottom to top, and legend order. Colors were run through the
// dataviz palette validator: adjacent pairs pass the colorblind (deutan/
// protan/tritan) and normal-vision separation checks. `ignored` is
// deliberately a low-chroma slate - it's the "set aside" judgment, matching
// its gray chip in DispositionPanel - and the amber is below 3:1 against
// white, which the legend and the visually hidden table cover.
const SERIES: { key: Series; label: string; color: string }[] = [
  { key: 'confirmed_fraud', label: 'Confirmed fraud', color: '#a8402f' },
  { key: 'false_positive', label: 'False positive', color: '#3f9a6b' },
  { key: 'ignored', label: 'Ignored', color: '#6f7292' },
  { key: 'unreviewed', label: 'Unreviewed', color: '#e2a84a' },
]

const UNFLAGGED_LABELS: Record<keyof UnflaggedDispositionCounts, string> = {
  confirmed_fraud: 'confirmed fraud',
  false_positive: 'false positive',
  ignored: 'ignored',
}

function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralForm}`
}

function TrendTooltip({ active, payload }: TooltipContentProps) {
  if (!active || !payload?.length) return null
  const bucket = payload[0].payload as TrendBucket
  return (
    <div className={styles.tooltip}>
      <p className={styles.tooltipTitle}>Week of {formatWeek(bucket.week_start)}</p>
      <ul className={styles.tooltipList}>
        {SERIES.map((series) => (
          <li key={series.key} className={styles.tooltipRow}>
            <span className={styles.swatch} data-series={series.key} aria-hidden="true" />
            <span className={styles.tooltipLabel}>{series.label}</span>
            <span className={styles.tooltipValue}>{bucket[series.key]}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

// One-sentence alternative for screen readers; the hidden table under the
// chart carries every week's numbers.
function describeTrend(trend: TrendBucket[], flaggedCount: number): string {
  const first = formatWeek(trend[0].week_start)
  const last = formatWeek(trend[trend.length - 1].week_start)
  const peak = trend.reduce((max, bucket) => (bucket.flagged > max.flagged ? bucket : max), trend[0])
  return (
    `Flagged transactions per week, ${first} to ${last}: ${plural(flaggedCount, 'flagged transaction')} ` +
    `across ${plural(trend.length, 'week')}, peaking at ${peak.flagged} in the week of ${formatWeek(peak.week_start)}. ` +
    'Each bar is split by current review status.'
  )
}

function TrendChart({ trend, flaggedCount }: { trend: TrendBucket[]; flaggedCount: number }) {
  return (
    <figure className={styles.chartFigure} aria-label={describeTrend(trend, flaggedCount)}>
      <ul className={styles.legend} aria-hidden="true">
        {SERIES.map((series) => (
          <li key={series.key} className={styles.legendItem}>
            <span className={styles.swatch} data-series={series.key} />
            {series.label}
          </li>
        ))}
      </ul>
      <div className={styles.chart} aria-hidden="true">
        <ResponsiveContainer width="100%" height={200} initialDimension={{ width: 520, height: 200 }}>
          <BarChart data={trend} margin={{ top: 4, right: 0, bottom: 0, left: -28 }} accessibilityLayer={false}>
            <CartesianGrid vertical={false} stroke="#f2f2f7" />
            <XAxis
              dataKey="week_start"
              tickFormatter={formatWeek}
              tickLine={false}
              axisLine={{ stroke: '#eff0f6' }}
              tick={{ fill: '#a0a3bd', fontSize: 11 }}
              interval="preserveStartEnd"
              minTickGap={16}
            />
            <YAxis
              allowDecimals={false}
              tickLine={false}
              axisLine={false}
              tick={{ fill: '#a0a3bd', fontSize: 11 }}
            />
            <Tooltip content={TrendTooltip} cursor={{ fill: '#f7f7fc' }} isAnimationActive={false} />
            {SERIES.map((series) => (
              <Bar
                key={series.key}
                dataKey={series.key}
                name={series.label}
                stackId="dispositions"
                fill={series.color}
                // A white hairline between stacked segments, so adjacent
                // colors never touch.
                stroke="#ffffff"
                strokeWidth={1}
                maxBarSize={24}
                isAnimationActive={false}
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>
      <table className={styles.visuallyHidden}>
        <caption>Flagged transactions per week by review status</caption>
        <thead>
          <tr>
            <th scope="col">Week of</th>
            <th scope="col">Flagged</th>
            {SERIES.map((series) => (
              <th key={series.key} scope="col">
                {series.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {trend.map((bucket) => (
            <tr key={bucket.week_start}>
              <th scope="row">{formatWeek(bucket.week_start)}</th>
              <td>{bucket.flagged}</td>
              {SERIES.map((series) => (
                <td key={series.key}>{bucket[series.key]}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  )
}

function Kpi({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className={styles.kpi}>
      <dt className={styles.kpiLabel}>{label}</dt>
      <dd className={styles.kpiValue}>{value}</dd>
      <dd className={styles.kpiDetail}>{detail}</dd>
    </div>
  )
}

function UnflaggedNote({ counts }: { counts: UnflaggedDispositionCounts }) {
  const parts = (Object.keys(UNFLAGGED_LABELS) as (keyof UnflaggedDispositionCounts)[])
    .filter((key) => counts[key] > 0)
    .map((key) => `${counts[key]} ${UNFLAGGED_LABELS[key]}`)
  const total = counts.confirmed_fraud + counts.false_positive + counts.ignored
  if (total === 0) return null
  return (
    <p className={styles.note}>
      {plural(total, 'review')} ({parts.join(', ')}) {total === 1 ? 'is' : 'are'} on transactions that are no longer
      flagged, so {total === 1 ? "it isn't" : "they aren't"} counted above.
    </p>
  )
}

function Summary({ summary }: { summary: SummaryResponse }) {
  const { dispositions } = summary
  const decided = dispositions.false_positive + dispositions.confirmed_fraud
  return (
    <>
      <dl className={styles.kpis}>
        <Kpi
          label="Flagged"
          value={String(summary.flagged_count)}
          detail={plural(summary.rule_hit_count, 'rule hit')}
        />
        <Kpi
          label="False-positive rate"
          value={formatRatio(summary.fp_rate)}
          detail={summary.fp_rate === null ? 'No judgments yet' : `${dispositions.false_positive} of ${decided} decided`}
        />
        <Kpi
          label="Coverage"
          value={formatRatio(summary.coverage)}
          detail={summary.coverage === null ? 'Nothing flagged' : `${summary.judged_count} of ${summary.flagged_count} judged`}
        />
        <Kpi label="Unreviewed" value={String(dispositions.unreviewed)} detail="Awaiting review" />
      </dl>

      <section className={styles.card} aria-labelledby="summary-trend-heading">
        <h2 id="summary-trend-heading" className={styles.cardHeading}>
          Flags by week
        </h2>
        {summary.flagged_count === 0 ? (
          <div className={styles.empty}>
            <p className={styles.stateTitle}>No flagged transactions</p>
            <p className={styles.stateBody}>When a rule flags a transaction, it'll be charted here.</p>
          </div>
        ) : (
          <TrendChart trend={summary.trend} flaggedCount={summary.flagged_count} />
        )}
      </section>

      <UnflaggedNote counts={summary.unflagged_dispositions} />
    </>
  )
}

interface SummaryDashboardProps {
  // Bumped by App after each saved disposition; any change refetches.
  refreshKey: number
}

// SCRUM-32. Flag and review totals above the feed. A refetch keeps the last
// summary on screen until the new one arrives, rather than flashing the
// loading state after every review.
export function SummaryDashboard({ refreshKey }: SummaryDashboardProps) {
  const [summary, setSummary] = useState<SummaryResponse | null>(null)
  const [status, setStatus] = useState<Status>('loading')
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    // Ignores a response that arrives after a newer request started, so an
    // older summary can't overwrite a newer one.
    let isCurrent = true
    fetchSummary()
      .then((response) => {
        if (!isCurrent) return
        setSummary(response)
        setStatus('ready')
      })
      .catch((err: unknown) => {
        if (!isCurrent) return
        setError(err instanceof Error ? err.message : 'Something went wrong.')
        setStatus('error')
      })
    return () => {
      isCurrent = false
    }
  }, [refreshKey, attempt])

  const retry = () => {
    setStatus('loading')
    setError(null)
    setAttempt((n) => n + 1)
  }

  return (
    <section className={styles.dashboard} aria-labelledby="summary-heading">
      <h1 id="summary-heading" className={styles.title}>
        Overview
      </h1>
      {status === 'loading' && (
        <div className={styles.stateCard} role="status" aria-live="polite">
          <div className={styles.spinner} aria-hidden="true" />
          <p>Loading your summary…</p>
        </div>
      )}
      {status === 'error' && (
        <div className={styles.stateCard} role="alert">
          <p className={styles.stateTitle}>Couldn't load your summary</p>
          <p className={styles.stateBody}>{error}</p>
          <button type="button" className={styles.retryButton} onClick={retry}>
            Try again
          </button>
        </div>
      )}
      {status === 'ready' && summary && <Summary summary={summary} />}
    </section>
  )
}
