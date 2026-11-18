# Additional IndexMaxxer experiments

These scripts address the query-frequency, data-distribution and selected TPC-H experiments discussed in the proposal presentation. Scripts being present does not mean results have been obtained: use only completed JSON reports produced by successful runs. Do not replace actual output with expected improvements.

## Run the complete package on Windows

Open Docker Desktop. Extract the ZIP into the existing project root, not into frontend or a second nested project folder. The package adds experiment scripts, this guide, and a PowerShell runner. It does not replace your backend, dashboard, original results or existing README. After successful completion, the runner adds a link to this guide in README and refreshes requirements.txt.

```powershell
cd C:\Users\Priyanshu\indexmaxxer
Expand-Archive -LiteralPath "$env:USERPROFILE\Downloads\indexmaxxer-remaining-experiments.zip" -DestinationPath . -Force
.\finish_experiments.ps1
```

The runner uses `.venv\Scripts\python.exe`, starts the existing container, checks readiness, installs DuckDB 2.0.0, and runs the experiments sequentially. The TPC-H extension may need an internet download on first use. Existing packages such as psycopg2, SQLGlot and Matplotlib must remain installed. The existing `.env` supplies the PostgreSQL connection. Do not run other benchmarks simultaneously. The API and frontend do not need to be running.

If PowerShell blocks the runner, use the individual commands below. Do not weaken system execution policies to run it.

## Individual commands / resuming after a failure

```powershell
cd C:\Users\Priyanshu\indexmaxxer
.\.venv\Scripts\Activate.ps1
python -m pip install duckdb==2.0.0
python -m experiments.compare_frequency
python -m experiments.compare_skew
python -m experiments.compare_tpch --scale 0.1
python -m experiments.export_remaining
python -m pip freeze | Set-Content -Encoding ascii requirements.txt
```

Run commands one at a time; stop on an error. If an earlier experiment completed, you can resume at the failed command. The full runner reruns all three, preserving older reports. A smaller `--scale 0.01` is supported for troubleshooting the TPC-H workflow, but label that scale honestly and use the planned 0.1 run for the main comparison when possible. Setup failures cannot be counted as completed experiments.

If DuckDB extension installation fails, check internet access and retry installation without rerunning the other experiments:

```powershell
.\.venv\Scripts\python.exe -c "import duckdb; c=duckdb.connect(); c.execute('INSTALL tpch'); c.execute('LOAD tpch'); print('TPC-H extension ready')"
```

An experiment report with `status: failed` or `incomplete` is not an acceptable result. Keep its error output. Normal exception cleanup removes only the schema created by that experiment. Force-closing the process or losing the connection can prevent cleanup; inspect its reported `scratch_schema` before any manual cleanup. Never drop the original `benchmark` schema.

## 1. Query-frequency weighting

`compare_frequency.py` reuses the same candidates and per-query timing medians from a completed individual-index comparison. It calls the existing `rank_results` with recorded frequency weights and with equal weights for each query entry. It then evaluates both selections under the original frequencies.

This isolates the scoring weights. It is an offline, in-sample analysis, not a new latency experiment, and does not test candidate-generation ordering. Equal weights apply to eight query entries, not seven normalized patterns. If both methods select the same index, there is no demonstrated selection benefit on this workload. Different percentages under different weights are different workload scores, not extra speedup caused by the weighting algorithm. No favorable outcome is required.

## 2. Uniform versus skewed data

`compare_skew.py` creates two isolated datasets with 100,000 orders and 10,000 customers:

- Uniform: each customer has equal probability of receiving an order.
- Skewed: 80% probability is assigned to customers 1-100; the remaining 20% to customers 101-10000.

Customer assignments change, while order dates, statuses, amounts and insertion order remain identical. The script verifies this with a generated-data hash. The baseline retains primary-key indexes; the added index is the previously selected `orders(customer_id)`, fixed for both distributions. It is not reselected as the optimal index for skewed data.

The existing eight queries and frequencies are retained. Their literals target low customer IDs, which are hot in the skewed dataset, so returned row counts may differ across distributions. Paired baseline/indexed row counts must match within each dataset. Read changes therefore reflect both data distribution and the resulting query match fractions. Per-query output includes row counts and diagnostic index usage to make this visible.

The script runs three paired trials with two warmups and seven measured rounds per phase, alternates which phase runs first, shuffles the distribution order per trial, and records index size. Results include client execute + fetchall timing; EXPLAIN metrics come from separate runs. Only experiment schemas are removed.

## 3. Selected TPC-H-derived queries

`compare_tpch.py --scale 0.1` uses DuckDB's `tpch` extension to generate data and retrieve Q1, Q3 and Q6. SQLGlot translates those queries into PostgreSQL syntax and qualifies their tables with a unique experiment schema. DuckDB only supplies data/query text; every timed query runs on PostgreSQL.

- Q1: aggregation over line items.
- Q3: customer/orders/lineitem join and revenue ranking.
- Q6: filtered revenue aggregation.

The script imports customer, orders and lineitem with primary keys and the two foreign-key relationships among these tables. It validates imported row counts. Other tables and their foreign keys are omitted because the selected queries do not use them. The script saves actual row counts, generated CSV hashes, translated SQL and dependency versions.

Two explicitly hand-defined experimental layouts are compared with paired primary-key-only baselines:

| Layout | Added indexes |
|---|---|
| shipdate_single | lineitem(l_shipdate) |
| query_targeted | lineitem(l_shipdate, l_discount); orders(o_custkey, o_orderdate); lineitem(l_orderkey, l_shipdate) |

These are controlled index-design experiments, not automatically selected recommendations. The existing advisor is not being claimed to support all TPC-H SQL. Layouts may help, make little difference, or be slower; report the observed results.

Each layout has three paired trials, two warmups and seven measured runs per query. Query order is shuffled, phase order is alternated, and timings include execute + fetchall. All three queries have equal weights. A hash of all returned values, including duplicate multiplicity but ignoring output order, is checked between repeated runs and layouts. This stronger check applies to this script; older experiments only checked row counts. EXPLAIN and index use are recorded separately. Experiment tables and indexes are removed afterward.

This is a selected TPC-H-derived query study, not an official TPC-H performance claim. DuckDB documents small differences in its generated data and fixed query substitution parameters. There is no full 22-query suite, QphH score, refresh stream or concurrency test.

Reference: https://duckdb.org/docs/current/core_extensions/tpch

## Additional exports

`export_remaining.py` accepts completed frequency, skew and TPC-H reports, requires cleanup verification for the database experiments, and requires frequency/skew to refer to the same original individual-index report. It creates a new `results/additional_exports_<UTC timestamp>/` folder with:

- `06_frequency_weights.png/.svg/.csv` and `06_frequency_selections.csv`
- `07_data_skew.png/.svg/.csv` and `07_data_skew_queries.csv`
- `08_selected_tpch.png/.svg/.csv` and `08_selected_tpch_layouts.csv`
- `manifest.json` with source filenames and SHA-256 hashes

Whiskers show observed trial ranges, not confidence intervals. The existing five graphs remain in their original export folder. The extra plots are standalone presentation artifacts; no dashboard integration is added by this package.

## Save completed work to GitHub

After all runs and exports succeed:

```powershell
git status --short
git add experiments/compare_frequency.py experiments/compare_skew.py experiments/compare_tpch.py experiments/export_remaining.py finish_experiments.ps1 docs/ADDITIONAL_EXPERIMENTS.md README.md requirements.txt results
git commit -m "Add frequency, data skew, and selected TPC-H experiments"
git push
```

These paths do not include `.env`. Review the staged files before committing if your results folder contains unrelated files. Generated DuckDB data files are temporary; the saved reports contain measurements and provenance, not a second persistent database.

## Final presentation / report use

Use measured values, show limitations and distinguish built-in PostgreSQL functionality from the team's analysis and benchmarking code. The frequency result may show an unchanged selection. The skew experiment holds the index fixed. The TPC-H experiment uses hand-defined layouts. Do not call these global index optimization or evidence that an index always helps. Small trial counts do not establish statistical significance.

Final slides, rehearsal, truthful member contributions and the team's technical report remain separate deliverables. This guide describes software operation and experiment methods; it is not a submitted technical report.
