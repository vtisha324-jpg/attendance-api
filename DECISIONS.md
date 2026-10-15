# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity and duplicate guard), `(department, emp_code)` for the sorted department list, `(department, joined_on)` and `(joined_on)` for headcount. `attendance_logs`: unique `(emp_code, date)` (one record per day, race-proof punch-in, employee queries), `(date desc, emp_code)` for the default list order and month ranges, `(status, date desc, emp_code)` for status filters, `(emp_code, punch_in desc)` to find the open record at punch-out. I rejected an index on `late_minutes`: the date index already narrows the data and most rows would be in it anyway.

2. **Punch-in race.** Both requests work out the same IST date and call `insert_one`. The unique `(emp_code, date)` index lets exactly one insert succeed; the other gets `DuplicateKeyError`, which I turn into 409. There is no "check first" step to race.

3. **Ties.** `$setWindowFields` with `$rank` gives tied employees the same rank. I filter `rank <= limit`, so everyone tied at the cutoff is returned, even if that is more than `limit` rows. Rows are ordered by minutes, then `emp_code`.

4. **Headcount.** The summary pipeline starts from `employees` (joined on or before month end) and uses `$lookup` into the logs. An employee with no logs gets an empty array but still stays in the group count.

5. **At 100x data.** I would keep monthly per-employee totals in a small rollup collection, updated at punch-out and regularize, so analytics stop re-reading raw logs and doing one `$lookup` per employee.
