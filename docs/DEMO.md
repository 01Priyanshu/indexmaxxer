# IndexMaxxer demo guide

## Before the demo

1. Follow the README's restart instructions if demonstrating the application locally.
2. Open `http://127.0.0.1:5173` and click **Reload saved results**.
3. Confirm that the selected index, candidate table, write-performance table and query-comparison panel are visible.
4. Have the exported PNG graphs available. The recorded export is in `results/exports_20261117T235416165129Z/`.

The classroom presentation uses the instructor's laptop. A localhost address on that laptop will not reach the app running on your own laptop. Put dashboard screenshots and the exported graphs into Google Slides so the presentation is self-contained.

## Approximately two-minute dashboard walkthrough

| Step | Show | Point to explain |
| --- | --- | --- |
| 1 | Selected index and read comparison | `orders(customer_id)` was the best individual read candidate in the recorded five-candidate test. The 31.6% result refers to the frequency-weighted workload score. |
| 2 | Candidate ranking | Five physical index designs were tested individually, each against its own paired baseline. The two below-threshold candidates are retained in the results. |
| 3 | Query comparison: select `orders(customer_id)`, then Q2 | Both configurations return five rows. The recorded diagnostic plan changes from Seq Scan to Bitmap Heap Scan plus Bitmap Index Scan. Root shared-buffer hits change from 787 to 11. |
| 4 | Write performance | Three 1,000-row batch operations show the measured cost of maintaining the selected index. These measurements include commit; they do not automatically change the read-based ranking. |
| 5 | Export JSON and experiment details | Recommendations can be traced to saved reports. The displayed experiments are recorded results, not a live index-application action. |

Q2's displayed client medians and EXPLAIN execution times are different measurements. Do not combine them into a single speedup calculation.

## Graphs to use in the presentation

| Figure | Supported finding |
| --- | --- |
| `01_individual_indexes.png` | The original workload benefited most from the customer-ID index among the individual candidates tested. |
| `02_write_costs.png` | INSERT and UPDATE batches were slower with that index in this experiment. |
| `03_strategy_comparison.png` | The naive three-index configuration delivered faster reads while using more added storage than the selected single index. |
| `04_dataset_scale.png` | The fixed selected index improved the weighted read score at each tested size; the recorded percentage improvement decreased as the orders table grew. |
| `05_selectivity.png` | The candidate appeared in diagnostic plans at the three smaller match fractions. At approximately 50%, PostgreSQL chose sequential scans; the measured timing difference is unexplained. |

Show observed ranges where available. They are not confidence intervals. Preserve the unused-index result; do not relabel its timing decrease as demonstrated index benefit.

## Presentation checklist

- Target 10 minutes of speaking within the required 7-12 minute range. The total slot, including questions, is 15 minutes.
- Use Google Slides and the required filename: `CSE 510 - Group X - Final Presentation`, replacing X with the actual group number.
- Share the Google Slides link with the instructor at least one day before the scheduled presentation, following the class instructions.
- Cover the problem, the five components, implementation details, experiment setup, main results, limitations, conclusions and actual team contributions.
- Distinguish the project's analysis, candidate generation, ranking, benchmark orchestration and UI from PostgreSQL's execution engine, optimizer, statistics and B-tree indexes.
- Clearly identify any experiments or features still planned for the technical report. The current implementation has no live query editor, and completed measurements use synthetic data.
- Assign each member's contribution according to the work they actually completed. Proposal-writing assignments are not evidence of implementation ownership.

## Useful distinctions for questions

- **Why can the naive set be faster?** It contains three indexes tested together, while the current advisor selects an individual index. The comparison exposes the read-time/storage tradeoff.
- **Does 31.6% apply to every query?** No. It is a frequency-weighted aggregate over the recorded workload. Per-query measurements and plans are available in the dashboard.
- **Does a scan mean an index is missing?** A scan alone is not sufficient evidence. The project generates candidates and benchmarks them; PostgreSQL still chooses the execution plan.
- **Are results statistically significant?** The project reports repeated measurements and observed ranges. The 5% ranking threshold is a heuristic, not a significance test.
- **Is the whole database unindexed at baseline?** No. Primary-key indexes remain. Experimental comparisons measure additional index configurations against the existing layout.
