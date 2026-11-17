import './WriteCosts.css'

function number(value, digits = 3) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '--'
  return value.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

function overhead(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '--'
  return `${value > 0 ? '+' : ''}${number(value, 1)}%`
}

export default function WriteCosts({ report }) {
  const evaluation = report?.write_evaluation
  if (report?.write_cost_evaluated !== true || !evaluation) return null

  const { candidate, operations, protocol } = evaluation
  const indexLabel = `${candidate.table}(${candidate.columns.join(', ')})`
  const recorded = new Date(evaluation.recorded_at_utc).toLocaleString()

  return (
    <section className="write-cost-panel" aria-labelledby="write-cost-heading">
      <div className="write-cost-heading-row">
        <div>
          <h2 id="write-cost-heading">Write performance</h2>
          <p className="write-cost-description">
            Measured INSERT and UPDATE costs for the selected read candidate.
          </p>
        </div>
        <div className="write-cost-candidate">
          <span>Selected index only</span>
          <code>{indexLabel}</code>
        </div>
      </div>

      <dl className="write-cost-facts">
        <div>
          <dt>Rows per batch</dt>
          <dd>{number(evaluation.batch_rows, 0)}</dd>
        </div>
        <div>
          <dt>Measured runs per configuration</dt>
          <dd>{number(evaluation.measured_rounds, 0)}</dd>
        </div>
        <div>
          <dt>Starting rows in each trial table</dt>
          <dd>{number(evaluation.source_rows, 0)}</dd>
        </div>
      </dl>

      <p className="write-cost-timing">
        Median time per batch, including the statement and commit. Lower is better.
      </p>
      <div
        className="write-cost-table-scroll"
        role="region"
        aria-label="Write benchmark measurements"
        tabIndex={0}
      >
        <table className="write-cost-table">
          <thead>
            <tr>
              <th scope="col">Operation</th>
              <th scope="col">Baseline ms</th>
              <th scope="col">With index ms</th>
              <th scope="col">Write overhead</th>
            </tr>
          </thead>
          <tbody>
            {operations.map((operation) => (
              <tr key={operation.operation}>
                <th scope="row">{operation.label}</th>
                <td>{number(operation.baseline_median_ms)}</td>
                <td>{number(operation.indexed_median_ms)}</td>
                <td>
                  <span className={`write-cost-change ${
                    operation.overhead_pct > 0 ? 'write-cost-slower' :
                    operation.overhead_pct < 0 ? 'write-cost-faster' : 'write-cost-equal'
                  }`}>
                    {overhead(operation.overhead_pct)}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="write-cost-note">{evaluation.note}</p>
      <p className="write-cost-scope">
        Candidate rankings use read performance. These write measurements apply
        to <code>{indexLabel}</code> under the batch workload shown here.
      </p>

      <details className="write-cost-details">
        <summary>Write experiment details</summary>
        <dl>
          <dt>Recorded</dt>
          <dd>{recorded}</dd>
          <dt>Baseline</dt>
          <dd>{protocol.baseline_indexes}</dd>
          <dt>With candidate</dt>
          <dd>{protocol.indexed_layout}</dd>
          <dt>Warmup rounds</dt>
          <dd>{number(protocol.warmup_rounds, 0)}</dd>
          <dt>Table preparation</dt>
          <dd>{protocol.table_reset}; setup is excluded from timing.</dd>
          <dt>Insert IDs</dt>
          <dd>{protocol.insert_ids}</dd>
          <dt>Table fillfactor</dt>
          <dd>{number(protocol.fillfactor, 0)}</dd>
          <dt>Source report</dt>
          <dd><code>{evaluation.source_report}</code></dd>
          <dt>Linked read report</dt>
          <dd><code>{evaluation.source_read_report}</code></dd>
          <dt>Cleanup</dt>
          <dd>{evaluation.cleanup_verified ? 'Experiment schema removed' : 'Not verified'}</dd>
        </dl>
      </details>
    </section>
  )
}
