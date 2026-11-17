import { useEffect, useState } from 'react'
import './index.css'

const format = (value, digits = 2) =>
  Number.isFinite(value) ? value.toFixed(digits) : '--'

const indexLabel = (item) =>
  `${item.table}(${item.columns.join(', ')})`

const gainLabel = (value) =>
  `${value > 0 ? '+' : ''}${format(value, 1)}%`

export default function App() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [refreshKey, setRefreshKey] = useState(0)

  useEffect(() => {
    document.title = 'IndexMaxxer | Index Advisor'

    const controller = new AbortController()
    let active = true
    const timeout = setTimeout(() => controller.abort(), 10000)

    async function loadResults() {
      setLoading(true)
      setError('')

      try {
        const response = await fetch('/api/recommendations', {
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
        if (!Array.isArray(body.rankings)) {
          throw new Error('The API returned an unexpected report format.')
        }

        if (active) setData(body)
      } catch (err) {
        if (active) {
          setError(
            controller.signal.aborted
              ? 'The request timed out. Check that the backend is running.'
              : err.message
          )
        }
      } finally {
        clearTimeout(timeout)
        if (active) setLoading(false)
      }
    }

    loadResults()

    return () => {
      active = false
      clearTimeout(timeout)
      controller.abort()
    }
  }, [refreshKey])

  const best = data?.selected_index
  const maxTime = best
    ? Math.max(best.baseline_ms, best.indexed_ms, 0.001)
    : 1

  function downloadResults() {
    const blob = new Blob([JSON.stringify(data, null, 2)], {
      type: 'application/json',
    })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = 'indexmaxxer-recommendations.json'
    document.body.appendChild(link)
    link.click()
    link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  return (
    <>
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">IM</span>
          <div>
            <strong>IndexMaxxer</strong>
            <span>Database workload analysis & index advice</span>
          </div>
        </div>
        
      </header>

      <main>
        <div className="page-heading">
          <div>
            <p className="eyebrow">WORKLOAD PERFORMANCE</p>
            <h1>Find the indexes that earn their place.</h1>
            <p className="muted">
              Compare measured read benefits, storage costs, and recommendations.
            </p>
          </div>
          <button
            type="button"
            disabled={loading}
            onClick={() => setRefreshKey((value) => value + 1)}
          >
            {loading ? 'Loading...' : 'Reload saved results'}
          </button>
        </div>

        {error && (
          <div className="error" role="alert">
            {error}
            {data && ' Showing the previously loaded report below.'}
          </div>
        )}

        {loading && !data && (
          <section className="panel" role="status">
            Loading the latest completed experiment...
          </section>
        )}

        {data && (
          <>
            <div className="report-meta">
              <span>
                Experiment recorded: {new Date(data.recorded_at_utc).toLocaleString()}
              </span>
              {Object.entries(data.dataset_estimated_rows ?? {}).map(
                ([table, rows]) => (
                  <span key={table}>
                    {table}: {rows?.toLocaleString() ?? 'unknown'} estimated rows
                  </span>
                )
              )}
            </div>

            <section className="recommendation">
              <div>
                <p className="eyebrow">BEST INDIVIDUAL READ CANDIDATE</p>
                <h2>{best ? indexLabel(best) : 'No candidate passed the checks'}</h2>
                <p>
                  {best
                    ? best.reason
                    : 'Review the candidate results and rejection reasons below.'}
                </p>
              </div>
              {best && (
                <div className="hero-metric">
                  <strong>{gainLabel(best.improvement_pct)}</strong>
                  <span>measured read improvement</span>
                </div>
              )}
            </section>

            <section className="stats" aria-label="Selected candidate metrics">
              <article className="stat">
                <span>Paired baseline</span>
                <strong>{format(best?.baseline_ms, 3)} ms</strong>
                <small>Existing indexes</small>
              </article>
              <article className="stat">
                <span>With candidate index</span>
                <strong>{format(best?.indexed_ms, 3)} ms</strong>
                <small>Recorded read timing</small>
              </article>
              <article className="stat">
                <span>Candidate index size</span>
                <strong>{format(best?.index_size_bytes / 1048576)} MiB</strong>
                <small>Additional storage</small>
              </article>
              <article className="stat">
                <span>Write overhead</span>
                <strong className="status-text">
                  {data.write_cost_evaluated ? 'Evaluated' : 'Not evaluated'}
                </strong>
                <small>INSERT / UPDATE cost</small>
              </article>
            </section>

            {best && (
              <section className="panel">
                <h2>Read timing comparison</h2>
                <p className="muted">
                  Frequency-weighted mean of per-query medians. Lower is better.
                </p>
                {[
                  ['Existing indexes', best.baseline_ms, 'baseline'],
                  ['With candidate', best.indexed_ms, 'indexed'],
                ].map(([label, value, style]) => (
                  <div className="bar-row" key={label}>
                    <span>{label}</span>
                    <div className="bar-track" aria-hidden="true">
                      <div
                        className={`bar-fill ${style}`}
                        style={{ width: `${(value / maxTime) * 100}%` }}
                      />
                    </div>
                    <strong>{format(value, 3)} ms</strong>
                  </div>
                ))}
              </section>
            )}

            <section className="panel">
              <div className="section-heading">
                <div>
                  <h2>Candidate ranking</h2>
                  <p className="muted">
                    {data.rankings.length} candidates tested individually.
                    Each has its own paired baseline.
                    Minimum improvement: {format(data.minimum_improvement_pct, 0)}%.
                  </p>
                </div>
                <button
                  type="button"
                  className="secondary"
                  onClick={downloadResults}
                >
                  Export JSON
                </button>
              </div>

              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Index / explanation</th>
                      <th scope="col" className="number">Baseline ms</th>
                      <th scope="col" className="number">Indexed ms</th>
                      <th scope="col" className="number">Gain</th>
                      <th scope="col" className="number">Size MiB</th>
                      <th scope="col">Decision</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.rankings.map((item) => (
                      <tr
                        key={item.index_name}
                        className={item.index_name === best?.index_name ? 'best-row' : ''}
                      >
                        <td>
                          <code>{indexLabel(item)}</code>
                          <small>{item.reason}</small>
                        </td>
                        <td className="number">{format(item.baseline_ms, 3)}</td>
                        <td className="number">{format(item.indexed_ms, 3)}</td>
                        <td className="number">{gainLabel(item.improvement_pct)}</td>
                        <td className="number">{format(item.index_size_bytes / 1048576)}</td>
                        <td>
                          <span className={`badge ${item.eligible ? 'shortlisted' : 'filtered'}`}>
                            {item.index_name === best?.index_name
                              ? 'Best read candidate'
                              : item.eligible ? 'Shortlisted' : 'Filtered'}
                          </span>
                        </td>
                      </tr>
                    ))}
                    {data.rankings.length === 0 && (
                      <tr><td colSpan={6}>No candidates in this report.</td></tr>
                    )}
                  </tbody>
                </table>
              </div>

              <details>
                <summary>Experiment details</summary>
                <p>{data.scope}</p>
                <p>Source report: <code>{data.source_report}</code></p>
                <p>
                  The improvement threshold is a project heuristic, not a
                  statistical significance test. Benefits from individually
                  tested indexes should not be added together.
                </p>
              </details>
            </section>

            <p className="footer-note">
              Recommendations reflect this recorded workload.
              This dashboard displays results; it does not apply indexes.
            </p>
          </>
        )}
      </main>
    </>
  )
}

