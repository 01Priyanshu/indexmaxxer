import { useEffect, useState } from 'react'
import './QueryDetails.css'

const format = (value, digits = 3) =>
  Number.isFinite(value) ? value.toFixed(digits) : '--'

const count = (value) =>
  Number.isFinite(value) ? String(value) : '--'

const improvement = (value) =>
  Number.isFinite(value)
    ? `${value > 0 ? '+' : ''}${value.toFixed(2)}%`
    : '--'

function PlanCard({ title, measurement }) {
  const plan = measurement.plan_summary

  return (
    <article className="plan-card">
      <h4>{title}</h4>

      <dl className="plan-metrics">
        <div>
          <dt>EXPLAIN execution</dt>
          <dd>{format(plan.execution_time_ms)} ms</dd>
        </div>
        <div>
          <dt>Shared block hits</dt>
          <dd>{count(plan.shared_hit_blocks)}</dd>
        </div>
        <div>
          <dt>Shared blocks read</dt>
          <dd>{count(plan.shared_read_blocks)}</dd>
        </div>
      </dl>

      <div className="table-wrap">
        <table className="plan-table">
          <thead>
            <tr>
              <th scope="col">Plan node</th>
              <th scope="col" className="number">Est. rows</th>
              <th scope="col" className="number">Actual rows</th>
              <th scope="col" className="number">Loops</th>
            </tr>
          </thead>
          <tbody>
            {plan.nodes.map((node, position) => (
              <tr key={position}>
                <td>
                  <div style={{ paddingLeft: Math.min(node.depth, 12) * 10 }}>
                    <strong>{node.node_type}</strong>
                    {node.table && <small>Table: {node.table}</small>}
                    {node.index && <small>Index: {node.index}</small>}
                    {node.join_type && <small>Join: {node.join_type}</small>}
                  </div>
                </td>
                <td className="number">{count(node.estimated_rows_per_loop)}</td>
                <td className="number">{count(node.actual_rows_per_loop)}</td>
                <td className="number">{count(node.loops)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="plan-note">
        Node row counts are per loop. Buffer counts are taken from the root
        plan node and include child activity.
      </p>

      {plan.observations.length > 0 && (
        <ul className="plan-observations">
          {plan.observations.map((message, position) => (
            <li key={position}>{message}</li>
          ))}
        </ul>
      )}

      <details>
        <summary>View original EXPLAIN JSON</summary>
        <pre className="plan-json">
          {JSON.stringify(measurement.explain, null, 2)}
        </pre>
      </details>
    </article>
  )
}

function Comparison({ reportName, indexName }) {
  const [details, setDetails] = useState(null)
  const [queryId, setQueryId] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    const timeout = setTimeout(() => controller.abort(), 10000)

    async function load() {
      setLoading(true)
      setError('')
      setDetails(null)

      try {
        const url = `/api/comparisons/${encodeURIComponent(reportName)}/${encodeURIComponent(indexName)}`
        const response = await fetch(url, {
          signal: controller.signal,
          cache: 'no-store',
        })

        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(
            typeof body.detail === 'string'
              ? body.detail
              : `Request failed (${response.status}).`
          )
        }

        const body = await response.json()
        if (!Array.isArray(body.queries)) {
          throw new Error('Unexpected comparison report format.')
        }

        if (active) {
          setDetails(body)
          setQueryId(body.queries[0]?.id ?? '')
        }
      } catch (err) {
        if (active) {
          setError(
            controller.signal.aborted
              ? 'The request timed out. Check the backend connection.'
              : err.message
          )
        }
      } finally {
        clearTimeout(timeout)
        if (active) setLoading(false)
      }
    }

    load()

    return () => {
      active = false
      clearTimeout(timeout)
      controller.abort()
    }
  }, [reportName, indexName, attempt])

  if (loading) return <p role="status">Loading recorded query details...</p>

  if (error) {
    return (
      <div className="error" role="alert">
        <p>{error}</p>
        <button type="button" onClick={() => setAttempt((value) => value + 1)}>
          Retry
        </button>
      </div>
    )
  }

  if (!details?.queries.length) {
    return <p>No query measurements are available in this report.</p>
  }

  const selected = details.queries.find((query) => query.id === queryId)
    ?? details.queries[0]

  return (
    <>
      <p className="muted query-protocol">
        Measured repeats per query: {details.baseline_protocol.measured_rounds}
        {' baseline / '}{details.indexed_protocol.measured_rounds} indexed.
        Frequencies are weights in the workload score.
      </p>

      <div className="table-wrap">
        <table className="query-table">
          <thead>
            <tr>
              <th scope="col">Query</th>
              <th scope="col" className="number">Frequency</th>
              <th scope="col" className="number">Baseline ms</th>
              <th scope="col" className="number">Indexed ms</th>
              <th scope="col" className="number">Improvement</th>
              <th scope="col">Index in plan</th>
            </tr>
          </thead>
          <tbody>
            {details.queries.map((query) => (
              <tr
                key={query.id}
                className={query.id === selected.id ? 'selected-query' : ''}
              >
                <td>
                  <button
                    type="button"
                    className="query-link"
                    aria-pressed={query.id === selected.id}
                    onClick={() => setQueryId(query.id)}
                  >
                    {query.id}
                  </button>
                </td>
                <td className="number">{query.frequency}</td>
                <td className="number">{format(query.baseline.median_ms)}</td>
                <td className="number">{format(query.indexed.median_ms)}</td>
                <td className={`number ${query.improvement_pct < 0 ? 'change-negative' : ''}`}>
                  {improvement(query.improvement_pct)}
                </td>
                <td>{query.index_used ? 'Yes' : 'No'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="plan-note">
        Click a query ID to inspect it. Index use comes from its diagnostic
        plan; small timing changes alone do not establish an index benefit.
      </p>

      <div className="selected-query-heading">
        <h3>{selected.id}: SQL and recorded plans</h3>
        <span className={`badge ${selected.row_counts_match ? 'shortlisted' : 'filtered'}`}>
          Row counts {selected.row_counts_match ? 'match' : 'differ'}
        </span>
      </div>

      <pre className="query-sql"><code>{selected.sql}</code></pre>

      <p className="muted">
        Returned rows: {count(selected.baseline.row_count)} baseline /
        {' '}{count(selected.indexed.row_count)} indexed.
      </p>
      <p className="muted">{details.timing_note}</p>

      <div className="plan-grid" key={selected.id}>
        <PlanCard title="Baseline plan" measurement={selected.baseline} />
        <PlanCard title="With candidate index" measurement={selected.indexed} />
      </div>
    </>
  )
}

export default function QueryDetails({ report }) {
  const candidates = report.rankings
  const [indexName, setIndexName] = useState(
    report.selected_index?.index_name ?? candidates[0]?.index_name ?? ''
  )

  if (candidates.length === 0) return null

  return (
    <section className="panel query-details">
      <div className="section-heading">
        <div>
          <h2>Query comparison</h2>
          <p className="muted">
            Inspect the measurements and plans behind each candidate.
          </p>
        </div>
        <div className="query-tools">
          <label htmlFor="comparison-index">Candidate to inspect</label>
          <select
            id="comparison-index"
            value={indexName}
            onChange={(event) => setIndexName(event.target.value)}
          >
            {candidates.map((candidate) => (
              <option key={candidate.index_name} value={candidate.index_name}>
                {candidate.table}({candidate.columns.join(', ')})
              </option>
            ))}
          </select>
        </div>
      </div>

      <Comparison
        key={`${report.source_report}:${indexName}`}
        reportName={report.source_report}
        indexName={indexName}
      />
    </section>
  )
}
